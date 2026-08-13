"""AST-based dependency-direction enforcement for the GTK-free boundary.

Enforces the package graph in ``sshpilot/core/package_graph.py``:

* core/api/daemon contain no direct GI imports and import no UI prefixes,
* core imports only core/api/runtime_identity/platform.paths (plus
  ``CORE_DEBT``),
* daemon imports only daemon/api/core/headless helpers (plus ``DAEMON_DEBT``),
  rejecting GObject adapters such as ``Config``/``ConnectionManager``/
  ``GroupManager``/``platform_utils``,
* a real recursive closure from every ``sshpilot.daemon`` module must not reach
  a forbidden UI/GObject adapter unless that exact importer edge is registered
  as debt.

Debt is **importer-specific**: registering one edge from one file does not
authorize the same import from another file in the package, and stale entries
(deleted files or removed imports) fail like new violations.
"""

from __future__ import annotations

import ast
from collections import deque
from functools import lru_cache
from pathlib import Path

import pytest

from sshpilot.core.package_graph import (
    ALLOWED_EDGES,
    BOUNDARY_PACKAGES,
    FORBIDDEN_GI_MODULES,
    FORBIDDEN_GI_NAMES,
    FORBIDDEN_GOBJECT_ADAPTERS,
    FORBIDDEN_UI_PREFIXES,
)

# Run every scan in this module on a single xdist worker so the process-local
# parse/analysis caches are built once, not once per worker (see
# ``--dist=loadgroup`` in pytest.ini).
pytestmark = pytest.mark.xdist_group("dependency-boundary")

ROOT = Path(__file__).resolve().parents[2] / "src" / "sshpilot"

# Registered GObject-adapter / helper edges in the daemon runtime composition,
# keyed by (importer_file_rel, imported_prefix). These pull GI transitively, so
# the daemon *runtime* is not yet GI-free. Each row is removed when the owning
# migration replaces the adapter with a headless daemon service. Any *other*
# importer of the same module fails the suite.
DAEMON_DEBT: dict[tuple[str, str], str] = {
    # daemon idle/service settings still read through ``Config`` (M4). Plugin
    # loading moved out of the daemon composition with the legacy managers
    # (M3): cli.py composes the headless repository only.
    # Launch/secret compatibility moved out of core into daemon providers
    # (Task 12). Each provider file registers the exact legacy helper it still
    # uses, tagged with the migration that will remove it.
    ("daemon/connection_launch_provider.py", "sshpilot.ssh_connection_builder"): "M7",
    ("daemon/connection_launch_provider.py", "sshpilot.plugins"): "M8",
    ("daemon/connection_secret_provider.py", "sshpilot.credential_model"): "M5",
    ("daemon/connection_secret_provider.py", "sshpilot.secret_storage"): "M5",
    ("daemon/connection_secret_provider.py", "sshpilot.askpass_utils"): "M5",
    # The daemon secret service owns the selected backend through the existing
    # SecretManager (secret_storage) and reuses CredentialManager/credential
    # model for export/import. These stay M5 debt until the migration moves the
    # manager into a daemon-owned headless module; no secret value crosses the
    # wire.
    ("daemon/secret_backend_service.py", "sshpilot.secret_storage"): "M5",
    # The daemon export/import drives the existing BackupManager as its GTK-free
    # execution adapter (M6) instead of maintaining a parallel backup engine.
    # backup_manager pulls Config (a GObject adapter) transitively; the edge is
    # importer-specific debt until the engine is made daemon-native.
    ("daemon/secret_transfer.py", "sshpilot.backup_manager"): "M6",
    ("backup_manager.py", "sshpilot.config"): "M6",
    # The daemon SSH-server backup destination reuses the same headless native-
    # auth composition as the launch provider (ssh_connection_builder, M7) and
    # resolves connection passwords from the daemon's own secret manager
    # (credential_model/secret_storage, M5). No secret crosses into a frontend.
    ("daemon/secret_transfer.py", "sshpilot.ssh_connection_builder"): "M7",
    ("daemon/secret_transfer.py", "sshpilot.credential_model"): "M5",
    ("daemon/secret_transfer.py", "sshpilot.secret_storage"): "M5",
    # get_state_dir is used for the daemon log. platform_utils imports GI, so the
    # daemon runtime depends on GI for its log path; switch to the GI-free
    # sshpilot.platform.paths.get_state_dir helper when the log lands behind it.
    ("daemon/cli.py", "sshpilot.platform_utils"): "M4",
    ("daemon/launcher.py", "sshpilot.platform_utils"): "M4",
    # These edges were hidden by the old broken _module_files (double-dot names);
    # the fixed scanner reveals them. They are pre-existing coupling, not new debt.
    ("askpass_utils.py", "sshpilot.secret_storage"): "M5",
    ("credential_model.py", "sshpilot.secret_storage"): "M5",
    ("plugins/api.py", "sshpilot.plugins.host"): "M8",
}

# core is the bottom layer. ConnectionApplicationService now delegates launch
# and secret behavior to injected providers (daemon-owned compatibility), so
# the former config/plugin/builder coupling is gone. Any remaining frontend
# coupling in daemon providers is registered as DAEMON_DEBT against its own
# migration.
CORE_DEBT: dict[tuple[str, str], str] = {}


def _iter_py_files(package: str):
    base = ROOT / package
    yield from sorted(base.rglob("*.py"))


@lru_cache(maxsize=None)
def _read_source(path: Path) -> str:
    """Read a source file once; later parses reuse the cached text."""
    return path.read_text(encoding="utf-8")


@lru_cache(maxsize=None)
def _parse_source(path: Path) -> ast.Module:
    """Parse a source file once and reuse the tree across every scan test.

    The boundary/closure tests each walk every ``src/sshpilot`` file
    independently; without this cache each file is tokenised and parsed once
    per test, which dominates the runtime of these two modules.
    """
    return ast.parse(_read_source(path), filename=str(path))


def _has_triple_dot(mod: str) -> bool:
    return "..." in mod or ".." in mod


def _resolve_from(node: ast.ImportFrom, path: Path) -> str:
    """Resolve an ImportFrom node to its absolute module ('' when bare)."""
    pkg_parts = list(path.relative_to(ROOT).parts[:-1])
    if node.level:
        if node.level > 1:
            pkg_parts = pkg_parts[: -(node.level - 1)]
        base = ".".join(("sshpilot",) + tuple(pkg_parts))
        return f"{base}.{node.module}" if node.module else base
    return node.module or ""


def _collect_imports(
    path: Path,
    module_files: "dict[str, Path] | None" = None,
) -> list[tuple[str, str]]:
    """Absolute module names for Import/ImportFrom/dynamic-import calls.

    Handles ``import X``, ``from pkg import X``, ``from . import X``,
    ``from .. import X`` and ``from .mod import X``.

    When *module_files* is supplied, each alias name in an absolute
    ``from sshpilot[.sub] import name`` is also emitted as the candidate
    ``sshpilot[.sub].name`` **only if** it resolves to an existing module via
    :func:`_deepest_existing`.  This lets the closure and boundary tests
    detect ``from sshpilot import config`` as an edge to ``sshpilot.config``
    while ignoring pure-symbol names like ``__version__``.
    """
    tree = _parse_source(path)
    hits: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                hits.append(("import", alias.name))
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_from(node, path)
            if not node.module and node.level:
                # `from .. import config` / `from . import service`: each alias
                # is a member of the resolved package (module or symbol).
                if base:
                    hits.extend(("from", f"{base}.{alias.name}") for alias in node.names)
                continue
            if base:
                hits.append(("from", base))
                # Also probe base.alias as a potential sub-module import
                # (e.g. ``from sshpilot import config`` or
                # ``from sshpilot.plugins import loader``).
                if module_files is not None and base.startswith("sshpilot"):
                    for alias in node.names:
                        cand = f"{base}.{alias.name}"
                        if _deepest_existing(cand, module_files) is not None:
                            hits.append(("from", cand))
        elif isinstance(node, ast.Call):
            func = node.func
            name = None
            if isinstance(func, ast.Name) and func.id in {"__import__", "import_module"}:
                name = func.id
            elif isinstance(func, ast.Attribute) and func.attr in {"import_module", "__import__"}:
                name = func.attr
            if name and node.args:
                arg0 = node.args[0]
                if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                    hits.append(("dynamic", arg0.value))
    return hits


def _forbidden_gi(path: Path) -> list[str]:
    hits: list[str] = []
    for kind, name in _imports_for(path):
        root = name.split(".", 1)[0]
        if name in FORBIDDEN_GI_MODULES or root == "gi":
            hits.append(f"{kind} {name}")
        if name.startswith("gi.repository"):
            hits.append(f"{kind} {name}")
    tree = _parse_source(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("gi"):
            for alias in node.names:
                if alias.name in FORBIDDEN_GI_NAMES or alias.name == "*":
                    hits.append(f"from {node.module} import {alias.name}")
    return hits


def _forbidden_ui(path: Path) -> list[str]:
    hits: list[str] = []
    for _kind, name in _imports_for(path):
        if any(name == p or name.startswith(p + ".") for p in FORBIDDEN_UI_PREFIXES):
            hits.append(name)
    return hits


def _matches(prefix: str, name: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


# ---------------------------------------------------------------------------
# Project import-graph closure (for the daemon runtime)
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _module_files() -> dict[str, Path]:
    """Map every sshpilot Python source file to its dotted module name.

    Builds the name from a list of parts rather than string concatenation so
    top-level files (e.g. ``config.py``) produce ``sshpilot.config`` and not
    the double-dot form ``sshpilot..config`` that the old concatenation yielded
    when ``parts[:-1]`` was empty.

    Cached: the source tree is immutable during a run. Tests that create
    temporary probe files pass those paths directly to ``_collect_imports`` and
    never rely on them appearing in this map.
    """
    mapping: dict[str, Path] = {}
    for path in ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        parts = path.relative_to(ROOT).parts
        if parts[-1] == "__init__.py":
            name_parts = ["sshpilot"] + list(parts[:-1])
        else:
            name_parts = ["sshpilot"] + list(parts[:-1]) + [parts[-1][:-3]]
        mod = ".".join(name_parts)
        mapping[mod] = path
    return mapping


@lru_cache(maxsize=None)
def _imports_for(path: Path) -> tuple[tuple[str, str], ...]:
    """Cached import hits for one file (resolved against the module map)."""
    return tuple(_collect_imports(path, _module_files()))


def _deepest_existing(full: str, module_files: dict[str, Path]) -> str | None:
    """Resolve ``sshpilot.a.b.c`` to the deepest module that exists on disk."""
    parts = full.split(".")
    for i in range(len(parts), 1, -1):
        candidate = ".".join(parts[:i])
        if candidate in module_files:
            return candidate
    return None


def _project_closure(roots: list[str], module_files: dict[str, Path]):
    """BFS over project imports from ``roots``; returns (seen, edges).

    edges are (importer_module, imported_module) pairs within the project.
    External/stdlib imports are ignored (walk stops at the project boundary).
    """
    seen: set[str] = set()
    edges: list[tuple[str, str]] = []
    queue: deque[str] = deque(sorted(roots))
    while queue:
        mod = queue.popleft()
        if mod in seen:
            continue
        seen.add(mod)
        path = module_files.get(mod)
        if path is None:
            continue
        for _kind, name in sorted(set(_imports_for(path))):
            if not name.startswith("sshpilot") or name == "sshpilot":
                continue
            target = _deepest_existing(name, module_files)
            if target is None or target == mod:
                continue
            edges.append((mod, target))
            if target not in seen:
                queue.append(target)
    edges.sort()
    return seen, edges


def test_boundary_packages_have_no_gi_imports():
    failures: list[str] = []
    for package in BOUNDARY_PACKAGES:
        for path in _iter_py_files(package):
            for hit in _forbidden_gi(path):
                failures.append(f"{path.relative_to(ROOT)}: {hit}")
    assert not failures, "Forbidden gi/GTK imports:\n" + "\n".join(failures)


def test_dependency_direction_core_api_daemon_do_not_import_gtk():
    failures: list[str] = []
    for package in BOUNDARY_PACKAGES:
        for path in _iter_py_files(package):
            for hit in _forbidden_ui(path):
                failures.append(f"{path.relative_to(ROOT)} imports {hit}")
    assert not failures, "Forbidden dependency direction:\n" + "\n".join(failures)


def test_package_graph_manifest_lists_boundary_packages():
    assert set(BOUNDARY_PACKAGES) == {"core", "api", "daemon"}
    assert "sshpilot.gtk" in FORBIDDEN_UI_PREFIXES


def _importer_edges(rel: str) -> list[str]:
    """All absolute module names imported by one file.

    Sub-module aliases such as ``from sshpilot import config`` are detected
    while pure symbol names like ``__version__`` are ignored. Uses the cached
    per-file import analysis against ``_module_files()``.
    """
    return [name for _kind, name in _imports_for(ROOT / rel)]


def _allowed_or_debt(name: str, allowed: tuple, debt: dict, importer: str) -> bool:
    if any(_matches(p, name) for p in allowed):
        return True
    for (imp, prefix), _tag in debt.items():
        if imp == importer and _matches(prefix, name):
            return True
    return False


def test_daemon_imports_only_allowed_edges():
    """The daemon must not reach GObject/GTK adapters; new edges are rejected."""
    allowed = ALLOWED_EDGES["daemon"]
    failures: list[str] = []
    for path in sorted((ROOT / "daemon").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        for name in _importer_edges(rel):
            if not name.startswith("sshpilot") or name == "sshpilot":
                continue  # bare package ``__version__`` import is GI-free
            if not _allowed_or_debt(name, allowed, DAEMON_DEBT, rel):
                failures.append(f"{rel} imports {name}")
    assert not failures, (
        "daemon imports a module outside its allowed dependency (add headless "
        "helpers to ALLOWED_EDGES['daemon']; route GObject adapters through the "
        "daemon instead of importing them):\n"
        + "\n".join(failures)
    )


def test_daemon_debt_edges_are_importer_specific_and_exact():
    """Registered debt is per-importer; stale/phantom edges fail."""
    failures: list[str] = []
    for (imp, prefix), _tag in sorted(DAEMON_DEBT.items()):
        if not (ROOT / imp).exists():
            failures.append(f"deleted importer: {imp}")
            continue
        seen = [n for n in _importer_edges(imp) if n.startswith("sshpilot")]
        if not any(_matches(prefix, n) for n in seen):
            failures.append(f"stale debt edge: {imp} -> {prefix} (no such import)")
    assert not failures, "\n".join(failures)


def test_daemon_closure_does_not_reach_forbidden_adapters():
    """Recursive project closure from the daemon must stay on allowed edges."""
    module_files = _module_files()
    roots = sorted(
        m for m in module_files if m == "sshpilot.daemon" or m.startswith("sshpilot.daemon.")
    )
    _seen, edges = _project_closure(roots, module_files)
    forbidden = set(FORBIDDEN_UI_PREFIXES) | set(FORBIDDEN_GOBJECT_ADAPTERS)
    # A debt entry (DAEMON_DEBT or CORE_DEBT) authorizes its *subtree*: once a
    # package is reachable under a sanctioned prefix, edges within it are not
    # re-flagged. Denied-by-importer is enforced separately by the direct-edge
    # tests. Here we only assert the daemon runtime never pulls in a forbidden
    # subtree that no debt edge opens.
    sanctioned_prefixes = [prefix for (_imp, prefix), _tag in list(DAEMON_DEBT.items()) + list(CORE_DEBT.items())]
    violations: list[str] = []
    for importer, target in edges:
        if not any(_matches(f, target) for f in forbidden):
            continue
        if not any(_matches(p, target) for p in sanctioned_prefixes):
            violations.append(f"{importer} -> {target}")
    assert not violations, (
        "daemon runtime closure reaches a forbidden UI/GObject adapter without a "
        "registered debt edge authorizing its subtree:\n" + "\n".join(sorted(violations))
    )


def test_daemon_closure_has_no_cycle_beyond_scc():
    """Closure walk terminates and revisits only already-seen modules."""
    module_files = _module_files()
    roots = sorted(
        m for m in module_files if m == "sshpilot.daemon" or m.startswith("sshpilot.daemon.")
    )
    seen, edges = _project_closure(roots, module_files)
    # Every edge must connect two reached modules (BFS invariant, cycle guard).
    assert seen, "daemon closure is empty"
    for importer, target in edges:
        assert importer in seen and target in seen, (importer, target)


def test_core_imports_only_allowed_edges():
    """core is the bottom layer; it may only link core + shared model leaves."""
    allowed = ALLOWED_EDGES["core"]
    failures: list[str] = []
    for path in sorted((ROOT / "core").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        for name in _importer_edges(rel):
            if not name.startswith("sshpilot") or name == "sshpilot":
                continue
            if not _allowed_or_debt(name, allowed, CORE_DEBT, rel):
                failures.append(f"{rel} imports {name}")
    assert not failures, (
        "core imports outside its allowed edges (core/api/runtime_identity/"
        "platform.paths). Registered coupling to frontend helpers lives in "
        "CORE_DEBT and must be removed by its migration:\n"
        + "\n".join(failures)
    )


def test_core_debt_edges_are_importer_specific_and_exact():
    failures: list[str] = []
    for (imp, prefix), _tag in sorted(CORE_DEBT.items()):
        if not (ROOT / imp).exists():
            failures.append(f"deleted importer: {imp}")
            continue
        seen = [n for n in _importer_edges(imp) if n.startswith("sshpilot")]
        if not any(_matches(prefix, n) for n in seen):
            failures.append(f"stale debt edge: {imp} -> {prefix} (no such import)")
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Unit tests for the scanner helpers themselves
# ---------------------------------------------------------------------------
def test_module_files_maps_top_level_files():
    """sshpilot.config and sshpilot.askpass_utils must be present (no double-dot)."""
    mf = _module_files()
    assert "sshpilot.config" in mf, "sshpilot.config missing from _module_files()"
    assert "sshpilot.askpass_utils" in mf, "sshpilot.askpass_utils missing from _module_files()"


def test_module_files_no_double_dot():
    """No generated module name may contain a double (or triple) dot."""
    bad = [mod for mod in _module_files() if ".." in mod]
    assert not bad, f"module names with '..' in _module_files(): {bad[:5]}"


def test_collect_imports_resolves_from_package_alias():
    """``from sshpilot import config`` must produce a 'sshpilot.config' hit."""
    import tempfile, textwrap

    src = textwrap.dedent("""
        from sshpilot import config
        from sshpilot.plugins import loader
    """)
    mf = _module_files()
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False, dir=ROOT) as f:
        f.write(src)
        tmp = Path(f.name)
    try:
        names = {n for _k, n in _collect_imports(tmp, mf)}
    finally:
        tmp.unlink()
    assert "sshpilot.config" in names, "sshpilot.config not found in import hits"
    assert "sshpilot.plugins.loader" in names, "sshpilot.plugins.loader not found in import hits"


def test_collect_imports_ignores_dunder_attributes():
    """``from sshpilot import __version__`` must not create a project-module edge."""
    import tempfile

    src = "from sshpilot import __version__\n"
    mf = _module_files()
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False, dir=ROOT) as f:
        f.write(src)
        tmp = Path(f.name)
    try:
        names = {n for _k, n in _collect_imports(tmp, mf)}
    finally:
        tmp.unlink()
    project_names = {n for n in names if n.startswith("sshpilot.")}
    assert "sshpilot.__version__" not in project_names, (
        "__version__ was treated as a project module"
    )


def test_stale_exact_debt_entry_is_detected():
    """A debt entry for a non-existent import must be reported as stale."""
    phantom_debt: dict[tuple[str, str], str] = {
        ("core/connection_application_service.py", "sshpilot.nonexistent.module"): "M8",
    }
    failures = []
    for (imp, prefix), _tag in phantom_debt.items():
        if not (ROOT / imp).exists():
            failures.append(f"deleted importer: {imp}")
            continue
        seen = [n for n in _importer_edges(imp) if n.startswith("sshpilot")]
        if not any(_matches(prefix, n) for n in seen):
            failures.append(f"stale debt edge: {imp} -> {prefix} (no such import)")
    assert failures, "Expected stale-debt detection to fail for a phantom entry"

"""AST-based dependency-direction enforcement for the GTK-free boundary."""
from __future__ import annotations

import ast
from pathlib import Path

from sshpilot.core.package_graph import (
    ALLOWED_EDGES,
    BOUNDARY_PACKAGES,
    FORBIDDEN_GI_MODULES,
    FORBIDDEN_GI_NAMES,
    FORBIDDEN_GOBJECT_ADAPTERS,
    FORBIDDEN_UI_PREFIXES,
)

ROOT = Path(__file__).resolve().parents[2] / "src" / "sshpilot"

# Registered GObject-adapter imports in the daemon runtime composition. These
# pull GI transitively, so the daemon *runtime* is not yet GI-free. Each row is
# removed when the owning migration replaces the adapter with a headless
# daemon service. Any *new* daemon -> adapter edge fails the suite below.
DAEMON_DEBT: dict[str, str] = {
    "sshpilot.config": "M4",  # Config (GTK preference store) -> daemon settings
    "sshpilot.connection_manager": "M3",  # ConnectionManager -> daemon connections
    "sshpilot.groups": "M3",  # GroupManager -> daemon connections/groups
    "sshpilot.plugins": "M8",  # load_plugins (frontend plugin host) -> daemon host
    # get_state_dir is used for the daemon log. platform_utils imports GI
    # (plan_utils is GTK-facing), so the daemon runtime depends on GI for its
    # log path; switch to sshpilot.platform.paths.get_state_dir when the daemon
    # log lands behind a dedicated path helper.
    "sshpilot.platform_utils": "M4",
}

# core is the bottom layer, but ConnectionApplicationService still couples to
# frontend modules for command building, plugin capability and config. Each
# entry is removed as the owning migration replaces that coupling.
CORE_DEBT: dict[str, str] = {
    "sshpilot.plugins": "M8",  # plugins.api/registry (plugin capability)
    "sshpilot.ssh_connection_builder": "M7",  # builds ssh argv/env
    "sshpilot.config": "M4",  # instantiates Config for command building
}


def _iter_py_files(package: str):
    base = ROOT / package
    yield from sorted(base.rglob("*.py"))


def _collect_imports(path: Path) -> list[tuple[str, str]]:
    """Return (kind, module_name) for absolute, relative, aliased, and constant importlib calls."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                hits.append(("import", alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level and not node.module:
                continue
            if node.level and node.module:
                # Relative import within package — resolve against file package.
                pkg_parts = path.relative_to(ROOT).parts[:-1]
                if node.level > 1:
                    pkg_parts = pkg_parts[: -(node.level - 1)]
                base = ".".join(("sshpilot",) + pkg_parts)
                hits.append(("from", f"{base}.{node.module}" if pkg_parts else f"sshpilot.{node.module}"))
            elif node.module:
                hits.append(("from", node.module))
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
    for kind, name in _collect_imports(path):
        root = name.split(".", 1)[0]
        if name in FORBIDDEN_GI_MODULES or root == "gi":
            hits.append(f"{kind} {name}")
        if name.startswith("gi.repository"):
            hits.append(f"{kind} {name}")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("gi"):
            for alias in node.names:
                if alias.name in FORBIDDEN_GI_NAMES or alias.name == "*":
                    hits.append(f"from {node.module} import {alias.name}")
    return hits


def _forbidden_ui(path: Path) -> list[str]:
    hits: list[str] = []
    for _kind, name in _collect_imports(path):
        if any(name == p or name.startswith(p + ".") for p in FORBIDDEN_UI_PREFIXES):
            hits.append(name)
    return hits


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


def _imports(module_rel: str) -> list[str]:
    """All dotted module names a source module imports (absolute + relative)."""
    path = ROOT / module_rel
    hits: list[str] = []
    for _kind, name in _collect_imports(path):
        hits.append(name)
    return hits


def _matches(prefix: str, name: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def test_daemon_imports_only_allowed_edges():
    """The daemon must not reach GObject/GTK adapters; new edges are rejected."""
    allowed = ALLOWED_EDGES["daemon"]
    debt_prefixes = tuple(DAEMON_DEBT)
    failures: list[str] = []
    for path in sorted((ROOT / "daemon").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        for name in _imports(rel):
            if not name.startswith("sshpilot") or name == "sshpilot":
                continue  # bare package ``__version__`` import is GI-free
            ok = any(_matches(p, name) for p in allowed) or any(
                _matches(p, name) for p in debt_prefixes
            )
            if not ok:
                failures.append(f"{rel} imports {name}")
    assert not failures, (
        "daemon imports a module outside its allowed dependency (add headless "
        "helpers to ALLOWED_EDGES['daemon']; route GObject adapters through the "
        "daemon instead of importing them):\n"
        + "\n".join(failures)
    )


def test_daemon_does_not_import_unregistered_goobject_adapters():
    """GObject adapters may be used by the daemon only via registered debt."""
    for prefix in DAEMON_DEBT:
        assert prefix in FORBIDDEN_GOBJECT_ADAPTERS, prefix
    allowed_debt = frozenset(DAEMON_DEBT)
    failures: list[str] = []
    for path in sorted((ROOT / "daemon").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        for name in _imports(rel):
            for adapter in FORBIDDEN_GOBJECT_ADAPTERS:
                if _matches(adapter, name) and not any(
                    _matches(d, name) for d in allowed_debt
                ):
                    failures.append(f"{rel}: {name} -> {adapter}")
    assert not failures, (
        "daemon imports a GObject adapter outside the registered debt; move "
        "it behind a headless daemon service instead:\n" + "\n".join(failures)
    )


def test_core_imports_only_allowed_edges():
    """core is the bottom layer; it may only link core + shared model leaves."""
    allowed = ALLOWED_EDGES["core"]
    debt_prefixes = tuple(CORE_DEBT)
    failures: list[str] = []
    for path in sorted((ROOT / "core").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        for name in _imports(rel):
            if not name.startswith("sshpilot") or name == "sshpilot":
                continue
            ok = any(_matches(p, name) for p in allowed) or any(
                _matches(p, name) for p in debt_prefixes
            )
            if not ok:
                failures.append(f"{rel} imports {name}")
    assert not failures, (
        "core imports outside its allowed edges (core/api/runtime_identity/"
        "platform.paths). Registered coupling to frontend helpers lives in "
        "CORE_DEBT and must be removed by its migration:\n"
        + "\n".join(failures)
    )

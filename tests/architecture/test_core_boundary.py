"""AST-level enforcement of the sshPilot ownership boundary.

Target model:

* ``sshpilot.core``  — pure, GTK-free, stateless domain logic.
* ``sshpilot.daemon`` — the process that owns authoritative state and I/O.
* ``sshpilot.api``    — GTK-free IPC / protocol models (shared language).
* GTK-facing modules  — presenters; they render state and call the API.

The cardinal rule: **a module being GTK-free does not mean GTK should own an
instance of it.** Authoritative filesystem/runtime access belongs to the daemon.

These tests statically (AST-only, no import) prove:

1. Frontend modules import from ``sshpilot.core`` only through the small
   explicit ``ALLOWED`` set (pure validation / classification / naming /
   formatting). There is deliberately **no package-level allowlist**.
2. Every current non-allowlisted core import is registered in
   ``PENDING_MIGRATIONS`` against a migration tag (M1–M8) and *must* be
   removed as that migration lands. Registering a new backend call in frontend
   code fails the suite until it is either truly pure (moved to ``ALLOWED``)
   or routed through the daemon API.
3. Frontend does not reach into ``sshpilot.daemon`` except a tiny, enumerated
   set of diagnostic/cleanup utilities.
4. Frontend does not perform backend *operations* (SSH/SCP/SFTP subprocesses,
   secret/key/config mutations, in-process service instantiation) outside an
   explicit per-module registry ``BACKEND_OPS``. This covers work that never
   touches ``sshpilot.core`` at all.
5. ``core`` / ``daemon`` / ``api`` never import Gtk/GLib/GI directly; the
   daemon's *transitive* GObject-adapter edges are enforced separately in
   ``tests/core/test_dependency_boundary.py``.

Keeping ``ALLOWED`` / ``PENDING_MIGRATIONS`` / ``BACKEND_OPS`` accurate is a
review requirement: stale entries fail just like new unregistered violations,
so the registries cannot silently grow or rot.
"""

from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

import pytest

# Run every scan in this module on a single xdist worker so the process-local
# parse/analysis caches are built once, not once per worker (see
# ``--dist=loadgroup`` in pytest.ini).
pytestmark = pytest.mark.xdist_group("core-boundary")

SOURCE = Path(__file__).resolve().parents[2] / "src" / "sshpilot"

# Top-level packages excluded from the "frontend" scan (internal layers).
# Top-level packages excluded from the "frontend" scan (internal layers).
# ``mcp`` hosts the dev/runtime MCP servers: headless, non-GUI tool layers
# guarded by their own boundary test (tests/architecture/test_mcp_boundary.py).
_INTERNAL = {"core", "daemon", "api", "mcp", "platform", "vendor", "locale"}

KNOWN_TAGS = frozenset({"frontend"} | {"M%d" % i for i in range(1, 9)})


# ---------------------------------------------------------------------------
# 1. Explicit allowlist: pure-core helpers frontend code may import locally.
#    Keys are (module_relpath, core_submodule, symbol). No wildcards allowed.
# ---------------------------------------------------------------------------
ALLOWED: frozenset[tuple[str, str, str]] = frozenset(
    {
        # -- error mapping (presentation) -------------------------------
        ("backup_manager.py", "errors", "CoreError"),
        ("backup_manager.py", "settings", "CONFIG_VERSION"),
        # -- legacy SSH-config facade; effective behavior is canonical core --
        ("ssh_config_utils.py", "ssh_config_effective", "SSHConfigPathDiscovery"),
        ("ssh_config_utils.py", "ssh_config_effective", "collect_host_block_lines"),
        ("ssh_config_utils.py", "ssh_config_effective", "discover_ssh_config_paths"),
        ("ssh_config_utils.py", "ssh_config_effective", "diff_effective_config"),
        ("ssh_config_utils.py", "ssh_config_effective", "expand_ssh_tokens"),
        ("ssh_config_utils.py", "ssh_config_effective", "get_effective_ssh_config"),
        ("ssh_config_utils.py", "ssh_config_effective", "resolve_ssh_config_files"),
        # -- connection field validation --------------------------------
        ("ssh_connection_validator.py", "validation.connection", "SSHConnectionValidator"),
        ("ssh_connection_validator.py", "validation.connection", "ValidationResult"),
        # -- forwarding-rule validation / defaults ----------------------
        ("connection_dialog_port_forwarding.py", "forwards", "validate_forwarding_rule"),
        ("connection_dialog_port_forwarding.py", "forwards", "forwarding_rule_defaults"),
        # -- transfer conflict-policy mapping ---------------------------
        ("file_manager_window.py", "transfers", "OverwritePolicy"),
        ("file_manager_window.py", "transfers", "ui_conflict_response_to_policy"),
        # -- prompt classification policy (pure) ------------------------
        ("askpass_utils.py", "interaction", "classify_prompt"),
        ("askpass_utils.py", "interaction", "PromptKind"),
        ("askpass_utils.py", "interaction", "build_request_from_prompt"),
        ("askpass_utils.py", "interaction", "decide_headless"),
        ("gtk/interaction.py", "interaction", "InteractionOutcome"),
        ("gtk/interaction.py", "interaction", "InteractionRequest"),
        ("gtk/interaction.py", "interaction", "InteractionResponse"),
        ("gtk/interaction.py", "interaction", "PromptKind"),
        ("gtk/interaction.py", "interaction", "ResponseType"),
        ("gtk/interaction.py", "interaction", "decide_headless"),
        ("gtk/interaction.py", "interaction", "validate_response"),
        # -- pure naming helpers ----------------------------------------
        ("backup_manager.py", "connections.models", "generate_group_slug"),
        # -- pure key sniffing (no directory discovery / generation) ----
        ("key_utils.py", "keys", "SKIPPED_FILENAMES"),
        ("key_utils.py", "keys", "is_private_key"),
        ("key_utils.py", "keys", "looks_like_private_key"),
        # -- terminal-output evidence classification --------------------
        ("terminal.py", "connection_evidence", "classify_connection_evidence"),
        # -- plugin contracts (shared language, NOT authoritative I/O) --
        ("plugins/api.py", "plugins", "API_VERSION"),
        ("plugins/api.py", "plugins", "Capability"),
        ("plugins/api.py", "plugins", "FieldSpec"),
        ("plugins/api.py", "plugins", "SpawnSpec"),
        ("plugins/host.py", "plugins", "ALL_EVENTS"),
        ("plugins/host.py", "plugins", "ConnectionInfo"),
        ("plugins/host.py", "plugins", "EventBus"),
        ("plugins/host.py", "plugins", "Events"),
        ("plugins/host.py", "plugins", "SessionInfo"),
    }
)


# ---------------------------------------------------------------------------
# 2. Pending migrations for every *other* current core import from frontend.
#    Key: (module_rel, core_submodule, symbol) -> (tag, short reason).
# ---------------------------------------------------------------------------
PENDING: dict[tuple[str, str, str], tuple[str, str]] = {
    # === M3 — connections store ========================================
    # === M4 — settings / config JSON ownership ==========================
    ("config.py", "settings", "CONFIG_VERSION"): ("M4", "config version -> daemon"),
    ("config.py", "settings", "ensure_config_defaults"): ("M4", "defaults write -> daemon"),
    ("config.py", "settings", "get_default_config"): ("M4", "defaults -> daemon"),
    # === M5 — secrets backend selection / vault state ===================
    ("secret_storage.py", "secrets", "normalize_backend_name"): ("M5", "backend selection -> daemon"),
    ("secret_storage.py", "secrets", "platform_default_order"): ("M5", "backend order -> daemon"),
    ("secret_storage.py", "secrets", "decide_unlock"): ("M5", "vault unlock policy -> daemon"),
    ("secret_storage.py", "secrets", "SecretDecisionKind"): ("M5", "unlock decision -> daemon"),
    # === M6 — backup / import-export ====================================
    ("backup_manager.py", "import_export", "MergeStrategy"): ("M6", "restore plan -> daemon"),
    ("backup_manager.py", "import_export", "plan_import"): ("M6", "restore plan -> daemon"),
    ("backup_manager.py", "import_export", "atomic_write_json"): ("M6", "file write -> daemon"),
    ("backup_manager.py", "import_export", "migrate_payload"): ("M6", "payload migrate -> daemon"),
    # === M7 — ssh-process / askpass broker ==============================
    ("ssh_connection_builder.py", "ssh", "ProcessSpec"): ("M7", "process spec -> daemon"),
    ("ssh_connection_builder.py", "ssh", "AuthMethod"): ("M7", "auth spec -> daemon"),
    ("ssh_connection_builder.py", "ssh", "HostKeyMode"): ("M7", "host-key mode -> daemon"),
    ("ssh_connection_builder.py", "ssh", "LaunchMode"): ("M7", "launch mode -> daemon"),
    ("ssh_connection_builder.py", "ssh", "SSHLaunchRequest"): ("M7", "launch request -> daemon"),
    ("ssh_connection_builder.py", "ssh", "build_ssh_process_spec"): ("M7", "builder -> daemon"),
}


# ---------------------------------------------------------------------------
# 3. Daemon reachability from frontend — only enumerated utilities.
# ---------------------------------------------------------------------------
DAEMON_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        # Diagnostics: resolve the daemon socket path for Help ▸ Diagnostics.
        ("log_viewer.py", "resolve_socket_path"),
        ("daemon_quit_policy.py", "resolve_socket_path"),
        # Quit must leave nothing running: when the daemon will not stop on
        # request, the quit path escalates to signalling the process holding
        # the socket. The escalation itself lives in the daemon package.
        ("daemon_quit_policy.py", "evict_socket_owner"),
        # …reaps what a killed daemon could not (its registered children and
        # the ControlMasters it never got to retire), so quitting leaves no
        # ``ssh`` process behind…
        ("daemon_quit_policy.py", "terminate_owned_runtime"),
        # …and gates the exit itself on the authoritative proof that nothing
        # sshPilot owns is still running. Ownership lives in the daemon
        # package because that is what creates the processes.
        ("daemon_quit_policy.py", "verify_sshpilot_runtime_terminated"),
        # Fingerprints the daemon from socket peer credentials *before*
        # teardown, so one that releases its socket but keeps running cannot
        # pass verification by disappearing from the socket check.
        ("daemon_quit_policy.py", "capture_daemon_identity"),
        # Daemon-adjacent cleanup of stale askpass sockets.
        # Shared rule for the per-user runtime tree the daemon sockets,
        # askpass sockets, and ControlMaster sockets all live under —
        # centralizing keeps the three sites from diverging (an unguarded
        # makedirs under a lax umask once blocked daemon launches with
        # unsafe_socket).
        ("ssh_multiplex.py", "ensure_private_runtime_directory"),
        # App-side daemon bootstrap / launch errors (frontend-owned boundary).
        ("main.py", "DaemonLauncher"),
        ("terminal_manager.py", "DaemonLauncher"),
        ("terminal_manager.py", "DaemonLaunchError"),
        ("terminal_manager.py", "DaemonStartupFailure"),
        # Startup client-selection toast: map DaemonLaunchError.reason to a
        # human-readable message instead of leaking the raw enum value.
        ("window.py", "DaemonStartupFailure"),
        # Reserved ``secret-session`` interaction namespace: frontend dialog
        # layers filter on it so secret-backend interactions are presented by
        # the app-wide SecretsInteractionPresenter, never the session-scoped one.
        ("daemon_interaction_dialogs.py", "is_secret_service_session"),
        ("gtk/secrets_interaction_presenter.py", "is_secret_service_session"),
        # Same contract, other half: the reserved prompt title that marks a
        # master-password unlock, so the presenter can tell it apart from every
        # other daemon secret request (a two-step code, a backup passphrase)
        # and head each dialog with what it is actually asking for.
        ("gtk/secrets_interaction_presenter.py", "MASTER_PASSWORD_PROMPT_TITLE"),
    }
)


# ---------------------------------------------------------------------------
# 4. Frontend backend operations. Every module that imports subprocess, that
#    launches an SSH-family binary, that mutates known-hosts, or that
#    instantiates a stateful core service must be registered here.
#    Key: (module_rel, op) -> tag. "frontend" = legitimate frontend-owned
#    launch (browser / external terminal / diagnostics); "M#" = daemon-ownership
#    debt that this migration will remove.
# ---------------------------------------------------------------------------
SSH_BINS = {"ssh", "scp", "sftp", "ssh-copy-id", "ssh-keygen", "ssh-add", "sshfs"}
SERVICE_CLASSES = {"SecretManager", "ConnectionService", "KeyService", "BackupManager"}

BACKEND_OPS: dict[tuple[str, str], str] = {
    # -- M3 connections --------------------------------------------------
    # -- M5 secrets ------------------------------------------------------
    ("secret_storage.py", "subprocess"): "M5",
    ("secret_storage.py", "SecretManager"): "M5",
    ("bitwarden_setup.py", "subprocess"): "frontend",
    # -- M6 backup (complete: daemon-owned via SecretBackendsController) --
    # -- M7 ssh-process / askpass broker ---------------------------------
    ("agent_client.py", "subprocess"): "frontend",
    ("askpass_utils.py", "subprocess"): "M7",
    ("askpass_utils.py", "ssh_binary"): "M7",
    ("providers/system_agent.py", "subprocess"): "M7",
    ("providers/system_agent.py", "ssh_binary"): "M7",
    ("ssh_config_utils.py", "subprocess"): "M7",
    ("ssh_config_utils.py", "ssh_binary"): "M7",
    ("ssh_multiplex.py", "subprocess"): "M7",
    ("ssh_multiplex.py", "ssh_binary"): "M7",
    ("terminal.py", "subprocess"): "frontend",  # local shell presentation
    # -- M8 plugins ------------------------------------------------------
    ("plugins/api.py", "subprocess"): "frontend",  # explicitly local plugin APIs
    # -- legitimate frontend-owned launches ------------------------------
    ("platform_utils.py", "subprocess"): "frontend",
    ("wol.py", "subprocess"): "frontend",
    ("port_utils.py", "subprocess"): "frontend",
    ("startup_info.py", "subprocess"): "frontend",
    ("startup_info.py", "ssh_binary"): "frontend",  # ssh -V version probe
    ("sshpilot_agent.py", "subprocess"): "frontend",  # local getent lookups
}


# ---------------------------------------------------------------------------
# Scan helpers
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _frontend_modules() -> tuple[Path, ...]:
    """Cached frontend module list; the source tree is immutable during a run."""
    paths: list[Path] = []
    for path in SOURCE.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if path.relative_to(SOURCE).parts[0] in _INTERNAL:
            continue
        paths.append(path)
    return tuple(paths)


def _rel(path: Path) -> str:
    return "/".join(path.relative_to(SOURCE).parts)


@lru_cache(maxsize=None)
def _read_source(path: Path) -> str:
    """Read a source file once; later scans reuse the cached text."""
    return path.read_text(encoding="utf-8")


@lru_cache(maxsize=None)
def _parse_source(path: Path) -> ast.Module:
    """Parse a source file once and reuse the tree across every boundary scan.

    Each frontend/core scan below walks the same ``src/sshpilot`` files; without
    this cache every file is tokenised and parsed once per test, which dominates
    the runtime of this module (~10s of the suite).
    """
    return ast.parse(_read_source(path))


def _full_names(node: ast.AST, rel: str) -> list[str]:
    """Resolve an Import/ImportFrom node to absolute dotted module names."""
    parts = rel.split("/")[:-1]
    pkg = ("sshpilot",) + tuple(parts)
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    assert isinstance(node, ast.ImportFrom)
    if node.level:
        base = pkg[: len(pkg) - (node.level - 1)]
        prefix = ".".join(base)
        if node.module:
            full = prefix + "." + node.module
        else:
            full = prefix
        return [full]
    return [node.module or ""]


def _core_imports(tree: ast.Module, rel: str) -> list[tuple[str, str]]:
    """Return (core_submodule, symbol) for every core import in a module."""
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for full in _full_names(node, rel):
                if not (full == "sshpilot.core" or full.startswith("sshpilot.core.")):
                    continue
                sub = full[len("sshpilot.core"):].strip(".") or "core"
                if isinstance(node, ast.Import):
                    found.append((sub, "*"))
                else:
                    for alias in node.names:
                        if alias.name != "*":
                            found.append((sub, alias.name))
    return found


def _daemon_imports(tree: ast.Module, rel: str) -> list[str]:
    """Return symbol names imported from ``sshpilot.daemon``."""
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for full in _full_names(node, rel):
            if full == "sshpilot.daemon" or full.startswith("sshpilot.daemon."):
                if isinstance(node, ast.Import):
                    found.append(full.rsplit(".", 1)[-1])
                else:
                    found.extend(alias.name for alias in node.names)
    return found


def _backend_ops(tree: ast.Module) -> list[str]:
    """Detect backend operations performed in a frontend module."""
    ops: list[str] = []

    def has_subprocess() -> bool:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(a.name == "subprocess" for a in node.names):
                    return True
            elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
                return True
        return False

    if has_subprocess():
        ops.append("subprocess")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        fname = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else None
        )
        if fname in SERVICE_CLASSES:
            ops.append(fname)
        if fname in {"load_known_hosts", "save_known_hosts"}:
            ops.append("known_hosts_io")
        if fname in {"run", "Popen", "check_call", "check_output", "call"}:
            args: list[str] = []
            for arg in node.args:
                if isinstance(arg, ast.List):
                    args.extend(
                        e.value for e in arg.elts if isinstance(e, ast.Constant)
                        and isinstance(e.value, str)
                    )
                elif isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    args.append(arg.value)
            if any(b in a for a in args for b in SSH_BINS):
                ops.append("ssh_binary")
    return list(dict.fromkeys(ops))


# ---------------------------------------------------------------------------
# Per-file analysis caches (the source tree is immutable during a run, so a
# file's imports/ops never change between scans).
# ---------------------------------------------------------------------------
@lru_cache(maxsize=None)
def _core_imports_for(path: Path) -> tuple[tuple[str, str], ...]:
    return tuple(_core_imports(_parse_source(path), _rel(path)))


@lru_cache(maxsize=None)
def _daemon_imports_for(path: Path) -> tuple[str, ...]:
    return tuple(_daemon_imports(_parse_source(path), _rel(path)))


@lru_cache(maxsize=None)
def _backend_ops_for(path: Path) -> tuple[str, ...]:
    return tuple(_backend_ops(_parse_source(path)))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_frontend_core_imports_are_categorised():
    """Every frontend core import must be allowlisted or registered pending."""
    violations: list[str] = []
    registry = set(ALLOWED) | set(PENDING)
    for path in _frontend_modules():
        rel = _rel(path)
        for sub, symbol in _core_imports_for(path):
            if (rel, sub, symbol) not in registry:
                violations.append(f"  {rel}: core.{sub}.{symbol}")
    assert not violations, (
        "Frontend imports a core symbol that is neither in the explicit ALLOWED "
        "allowlist nor the PENDING_MIGRATIONS registry. Keep pure helpers local "
        "(add to ALLOWED) or route authoritative access through the daemon API:\n"
        + "\n".join(violations)
    )


# Baseline diag for macros below.
_PENDING_EXPECTED = {
    "M4": 3,
    "M5": 4,
    "M6": 4,
    "M7": 6,
}


def test_pending_tags_are_known():
    assert not PENDING or all(tag in KNOWN_TAGS for (tag, _) in PENDING.values()), (
        "PENDING entry maps to an unknown migration tag"
    )


def test_registry_matches_the_source_tree():
    """Every ALLOWED/PENDING entry must resolve (no stale, no phantom)."""
    seen: set[tuple[str, str, str]] = set()
    for path in _frontend_modules():
        rel = _rel(path)
        for sub, symbol in _core_imports_for(path):
            seen.add((rel, sub, symbol))

    stale = (set(ALLOWED) | set(PENDING)) - seen
    assert not stale, (
        "stale entries in ALLOWED/PENDING (the import no longer exists):\n"
        + "\n".join(" : ".join(x) for x in sorted(stale))
    )

    unaccounted = seen - set(ALLOWED) - set(PENDING)
    assert not unaccounted, (
        "imports present in source but missing from ALLOWED/PENDING:\n"
        + "\n".join(" : ".join(x) for x in sorted(unaccounted))
    )


def test_pending_matches_exact_per_tag_baseline():
    """PENDING counts must match the reviewed baseline exactly.

    As migrations complete, delete the whole tag's rows and drop it from
    ``_PENDING_EXPECTED``; the count may reach zero for each migration.
    """
    from collections import Counter

    actual = Counter(tag for (tag, _) in PENDING.values())
    expected = {k: v for k, v in _PENDING_EXPECTED.items() if v != 0}
    assert actual == expected, (
        f"PENDING per-tag counts changed; expected {expected}, got {dict(actual)}. "
        "Only remove rows as the owning migration lands."
    )


def test_pending_registry_cannot_grow_silently():
    """PENDING is migration debt; adding entries without a migration is a fail."""
    allowed_tags = set(_PENDING_EXPECTED)
    leaks = [tag for (tag, _) in PENDING.values() if tag not in allowed_tags or _PENDING_EXPECTED[tag] == 0]
    assert not leaks, (
        f"PENDING rows exist for a fully-migrated tag or an unknown tag: {leaks}"
    )


def test_frontend_daemon_imports_are_allowlisted():
    """Frontend must not reach into sshpilot.daemon except enumerated utilities."""
    allowed = set(DAEMON_ALLOWLIST)
    violations: list[str] = []
    for path in _frontend_modules():
        rel = _rel(path)
        for symbol in _daemon_imports_for(path):
            if (rel, symbol) not in allowed:
                violations.append(f"  {rel}: sshpilot.daemon.{symbol}")
    assert not violations, (
        "frontend reaches into sshpilot.daemon outside the explicit "
        "DAEMON_ALLOWLIST:\n" + "\n".join(violations)
    )


def test_frontend_backend_operations_are_registered():
    """Frontend may launch processes / services only through registered ops."""
    violations: list[str] = []
    stale: list[str] = []
    for path in _frontend_modules():
        rel = _rel(path)
        detected = _backend_ops_for(path)
        for op in detected:
            if (rel, op) not in BACKEND_OPS:
                violations.append(f"  {rel}: {op}")
        for (mod, op) in BACKEND_OPS:
            if mod == rel and op not in detected:
                stale.append(f"  {mod}: {op} (op no longer detected)")
    assert not violations, (
        "frontend performs an unregistered backend operation. Route SSH/SCP/"
        "SFTP launches, secret/key/config mutation and service instantiation "
        "through the daemon, or add a genuinely frontend-owned launch to "
        "BACKEND_OPS with tag 'frontend':\n" + "\n".join(violations)
    )
    assert not stale, (
        "stale BACKEND_OPS entries (op no longer detected in source):\n"
        + "\n".join(stale)
    )


def test_backend_op_tags_are_known():
    unknown = {tag for (_, _), tag in BACKEND_OPS.items() if tag not in KNOWN_TAGS}
    assert not unknown, f"unknown BACKEND_OPS tags: {sorted(unknown)}"


def test_backend_ops_debt_matches_exact_baseline():
    """Backend-op migration debt must match the reviewed baseline exactly.

    Each M# row set shrinks to zero as its migration lands; ``frontend`` ops
    stay. M1 (keys) and M2 (known-hosts) are complete, so the remaining debt
    is M3/M5–M8.
    """
    from collections import Counter

    debt = Counter(t for t in BACKEND_OPS.values() if t != "frontend")
    expected = {"M5": 2, "M7": 8}
    assert dict(debt) == expected, (
        f"BACKEND_OPS debt changed; expected {expected}, got {dict(debt)}. "
        "Only remove rows as the owning migration lands."
    )



def test_shared_operation_runtime_is_the_daemon_operation_registry():
    runtime = (SOURCE / "daemon/operation_runtime.py").read_text(encoding="utf-8")
    identity = (SOURCE / "daemon/identity_service.py").read_text(encoding="utf-8")
    assert "class OperationRuntime" in runtime
    assert "self._operations.start_operation" in identity
    assert "self._operation_registry" not in identity
    assert "self._operations =" in identity


def test_core_api_daemon_are_gi_free():
    """core/api/daemon must never import Gtk/GLib/GI directly."""
    forbidden = {"gi", "Gtk", "Adw", "Gdk", "Vte", "GLib", "GObject", "Gio", "GtkSource"}
    for package in ("core", "api", "daemon"):
        base = SOURCE / package
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            tree = _parse_source(path)
            names: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.module:
                        names.append(node.module.split(".")[0])
                    names.extend(a.name for a in node.names)
                elif isinstance(node, ast.Import):
                    names.extend(a.name.split(".")[0] for a in node.names)
            offender = [n for n in names if n in forbidden]
            assert not offender, f"{path} imports Gtk/GLib/GI symbol(s): {offender}"


def test_core_does_not_import_daemon():
    """core is a bottom layer; it must not import from sshpilot.daemon."""
    for path in (SOURCE / "core").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        hits = _daemon_imports_for(path)
        assert not hits, f"{path}: core imports daemon: {hits}"


def test_known_hosts_editor_has_no_local_file_io():
    """M2: the editor must render API data and never touch the file directly.

    Enumerated AST check for the exact legacy I/O surface: no path helper, no
    local load/save functions, and no direct ``open`` / ``Path.read_text`` /
    ``Path.write_text`` calls.
    """
    path = SOURCE / "known_hosts_editor.py"
    tree = _parse_source(path)
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.Import):
            used.update(a.asname or a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            used.update(a.asname or a.name for a in node.names)
    forbidden = {
        "get_ssh_dir",
        "load_known_hosts",
        "save_known_hosts",
        "read_text",
        "write_text",
        "read_bytes",
        "write_bytes",
        "open",
    }
    hits = sorted(forbidden & used)
    assert not hits, (
        "known_hosts_editor.py performs local known-hosts I/O (M2 incomplete): "
        + ", ".join(hits)
    )

    # The editor must not accept or store a known-hosts filesystem path.
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "__init__":
            arg_names = {arg.arg for arg in node.args.args}
            path_like = arg_names & {"path", "known_hosts_path"}
            assert not path_like, (
                "known_hosts_editor.py accepts a known-hosts filesystem path "
                "(M2 incomplete): " + ", ".join(sorted(path_like))
            )
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr in {
                    "_known_hosts_path",
                    "known_hosts_path",
                }:
                    raise AssertionError(
                        "known_hosts_editor.py stores a known-hosts filesystem "
                        "path (M2 incomplete)"
                    )


def test_key_manager_has_no_local_key_io():
    """M1: KeyManager is a daemon adapter and never touches the file system.

    Enumerated AST check for the exact legacy key surface: no core key service
    types, no path helper, no subprocess / ssh-keygen, no directory scanning,
    and no file read/write calls.
    """
    path = SOURCE / "key_manager.py"
    tree = _parse_source(path)
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.Import):
            used.update(a.asname or a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            used.update(a.asname or a.name for a in node.names)
    forbidden = {
        "KeyService",
        "KeyGenerateSpec",
        "SSHKeyInfo",
        "get_ssh_dir",
        "subprocess",
        "ssh-keygen",
        "rglob",
        "read_text",
        "read_bytes",
        "write_text",
        "write_bytes",
        "open",
    }
    hits = sorted(forbidden & used)
    assert not hits, (
        "key_manager.py performs local key I/O (M1 incomplete): "
        + ", ".join(hits)
    )

    # The constructor must not accept a key-directory path.
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "__init__":
            arg_names = {arg.arg for arg in node.args.args}
            path_like = arg_names & {"ssh_dir", "path", "key_dir"}
            assert not path_like, (
                "KeyManager.__init__ accepts a key-directory path "
                "(M1 incomplete): " + ", ".join(sorted(path_like))
            )
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr in {"ssh_dir", "key_dir"}
                ):
                    raise AssertionError(
                        "key_manager.py stores a key-directory path "
                        "(M1 incomplete)"
                    )


_KEY_IMPORT_FLOW = frozenset(
    {
        "_on_add_from_local",
        "_list_keys_worker",
        "_on_local_keys_loaded",
        "_prompt_local_key_pick",
        "_start_public_key_read",
        "_read_public_worker",
        "_on_public_key_read_ok",
        "_on_public_key_read_failed",
    }
)


def _ids_in_funcs(tree: ast.Module, names: frozenset) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name not in names:
            continue
        ids: set[str] = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Name):
                ids.add(child.id)
            elif isinstance(child, ast.Attribute):
                ids.add(child.attr)
        found[node.name] = ids
    return found


_SSH_OVERRIDE_CONFIG_KEYS = frozenset(
    {
        "ssh.connection_timeout",
        "ssh.connection_attempts",
        "ssh.keepalive_interval",
        "ssh.keepalive_count_max",
        "ssh.strict_host_key_checking",
        "ssh.batch_mode",
        "ssh.compression",
        "ssh.verbosity",
        "ssh.debug_enabled",
        "ssh.ssh_overrides",
    }
)


def test_preferences_has_no_local_ssh_override_persistence():
    """SSH overrides ownership: Preferences never composes the derived list or
    persists the nine daemon-owned fields through the local config.

    The daemon owns the semantic SSH fields; the page only reads them for
    display and submits mutations through ``SshOverridesController``.  Any
    ``compose_ssh_overrides`` call, ``ssh.ssh_overrides`` write, or
    ``config.set_setting`` of a daemon-owned override key here is ownership
    debt that fails the suite.
    """
    path = SOURCE / "preferences.py"
    tree = _parse_source(path)
    used: set[str] = set()
    strings: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.add(node.value)

    assert not ({"compose_ssh_overrides"} & used), (
        "preferences.py composes SSH overrides locally (daemon-ownership debt)"
    )
    assert not ({"ssh.ssh_overrides"} & strings), (
        "preferences.py writes ssh.ssh_overrides locally (daemon-ownership debt)"
    )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "set_setting":
            continue
        if not node.args:
            continue
        key = node.args[0]
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            assert key.value not in _SSH_OVERRIDE_CONFIG_KEYS, (
                "preferences.py persists a daemon-owned SSH override key "
                f"({key.value!r}) through Config (daemon-ownership debt)"
            )


def test_authorized_keys_window_reads_no_discovered_key_paths():
    """M1: the window never opens a daemon-discovered key's ``.pub`` file.

    Local key import resolves public text through the daemon ``read_public_key``
    API; the old path-based ``_append_pubkey_from_path`` helper is gone.
    """
    path = SOURCE / "authorized_keys_window.py"
    source = _read_source(path)
    assert "def _append_pubkey_from_path" not in source, (
        "authorized_keys_window.py still reads public keys from a path "
        "(M1 incomplete)"
    )
    tree = _parse_source(path)
    for name, ids in _ids_in_funcs(tree, _KEY_IMPORT_FLOW).items():
        assert not ({"open", "exists", "isfile"} & ids), (
            f"{name} performs a local key-file operation (M1 incomplete)"
        )

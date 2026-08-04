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
from collections import defaultdict
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "src" / "sshpilot"

# Top-level packages excluded from the "frontend" scan (internal layers).
_INTERNAL = {"core", "daemon", "api", "platform", "vendor", "locale"}

KNOWN_TAGS = frozenset({"frontend"} | {"M%d" % i for i in range(1, 9)})


# ---------------------------------------------------------------------------
# 1. Explicit allowlist: pure-core helpers frontend code may import locally.
#    Keys are (module_relpath, core_submodule, symbol). No wildcards allowed.
# ---------------------------------------------------------------------------
ALLOWED: frozenset[tuple[str, str, str]] = frozenset(
    {
        # -- error mapping (presentation) -------------------------------
        ("key_manager.py", "errors", "CoreError"),
        ("key_manager.py", "errors", "ErrorCode"),
        ("connection_manager.py", "errors", "CoreError"),
        ("backup_manager.py", "errors", "CoreError"),
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
        ("connection_manager.py", "connections", "generate_duplicate_nickname"),
        ("groups.py", "connections.models", "generate_group_slug"),
        ("backup_manager.py", "connections.models", "generate_group_slug"),
        # -- pure key sniffing (no directory discovery / generation) ----
        ("key_utils.py", "keys", "SKIPPED_FILENAMES"),
        ("key_utils.py", "keys", "is_private_key"),
        ("key_utils.py", "keys", "looks_like_private_key"),
        # -- pure SSH-overrides composition ------------------------------
        ("preferences.py", "settings", "compose_ssh_overrides"),
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
    # === M1 — keys: generation + discovery move to the daemon ===========
    ("key_manager.py", "keys", "KeyGenerateSpec"): ("M1", "key generation spec -> daemon"),
    ("key_manager.py", "keys", "KeyService"): ("M1", "instantiate key service -> daemon"),
    ("key_manager.py", "keys", "SSHKeyInfo"): ("M1", "key listing result -> daemon"),
    # === M3 — connections store ========================================
    ("connection_manager.py", "connections", "ConnectionService"): ("M3", "in-GTK store -> daemon"),
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
        # Daemon-adjacent cleanup of stale askpass sockets.
        ("askpass_server.py", "sweep_stale_askpass_sockets"),
        # App-side daemon bootstrap / launch errors (frontend-owned boundary).
        ("main.py", "DaemonLauncher"),
        ("terminal_manager.py", "DaemonLauncher"),
        ("terminal_manager.py", "DaemonLaunchError"),
        ("terminal_manager.py", "DaemonStartupFailure"),
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
    # -- M1 keys ---------------------------------------------------------
    ("key_manager.py", "KeyService"): "M1",
    # -- M3 connections --------------------------------------------------
    ("connection_manager.py", "ConnectionService"): "M3",
    # -- M5 secrets ------------------------------------------------------
    ("secret_storage.py", "subprocess"): "M5",
    ("secret_storage.py", "SecretManager"): "M5",
    ("bitwarden_setup.py", "subprocess"): "M5",
    ("rbw_setup.py", "subprocess"): "M5",
    # -- M6 backup -------------------------------------------------------
    ("window_dialogs.py", "BackupManager"): "M6",
    # -- M7 ssh-process / askpass broker ---------------------------------
    ("agent_client.py", "subprocess"): "M7",
    ("askpass_utils.py", "subprocess"): "M7",
    ("askpass_utils.py", "ssh_binary"): "M7",
    ("autocomplete.py", "subprocess"): "M7",
    ("connection_dialog.py", "subprocess"): "M7",
    ("connection_dialog.py", "ssh_binary"): "M7",
    ("daemon_sftp_backend.py", "subprocess"): "M7",
    ("file_manager/openssh_backend.py", "subprocess"): "M7",
    ("providers/system_agent.py", "subprocess"): "M7",
    ("providers/system_agent.py", "ssh_binary"): "M7",
    ("scp_utils.py", "subprocess"): "M7",
    ("sftp_utils.py", "subprocess"): "M7",
    ("ssh_config_utils.py", "subprocess"): "M7",
    ("ssh_config_utils.py", "ssh_binary"): "M7",
    ("ssh_key_fingerprint.py", "subprocess"): "M7",
    ("ssh_key_fingerprint.py", "ssh_binary"): "M7",
    ("ssh_multiplex.py", "subprocess"): "M7",
    ("ssh_multiplex.py", "ssh_binary"): "M7",
    ("terminal.py", "subprocess"): "M7",  # pre-connection cmds + SSH subprocesses
    # -- M8 plugins ------------------------------------------------------
    ("plugins/api.py", "subprocess"): "M8",
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
def _frontend_modules() -> list[Path]:
    paths: list[Path] = []
    for path in SOURCE.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if path.relative_to(SOURCE).parts[0] in _INTERNAL:
            continue
        paths.append(path)
    return paths


def _rel(path: Path) -> str:
    return "/".join(path.relative_to(SOURCE).parts)


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
# Tests
# ---------------------------------------------------------------------------
def test_frontend_core_imports_are_categorised():
    """Every frontend core import must be allowlisted or registered pending."""
    violations: list[str] = []
    registry = set(ALLOWED) | set(PENDING)
    for path in _frontend_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = _rel(path)
        for sub, symbol in _core_imports(tree, rel):
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
    "M1": 3,
    "M3": 1,
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
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = _rel(path)
        for sub, symbol in _core_imports(tree, rel):
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
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = _rel(path)
        for symbol in _daemon_imports(tree, rel):
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
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = _rel(path)
        detected = _backend_ops(tree)
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
    stay. This migration (M2) must drop the ``known_hosts_io`` row.
    """
    from collections import Counter

    debt = Counter(t for t in BACKEND_OPS.values() if t != "frontend")
    expected = {"M1": 1, "M3": 1, "M5": 4, "M6": 1, "M7": 19, "M8": 1}
    assert dict(debt) == expected, (
        f"BACKEND_OPS debt changed; expected {expected}, got {dict(debt)}. "
        "Only remove rows as the owning migration lands."
    )



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
            tree = ast.parse(path.read_text(encoding="utf-8"))
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
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = _rel(path)
        hits = _daemon_imports(tree, rel)
        assert not hits, f"{path}: core imports daemon: {hits}"


def test_known_hosts_editor_has_no_local_file_io():
    """M2: the editor must render API data and never touch the file directly.

    Enumerated AST check for the exact legacy I/O surface: no path helper, no
    local load/save functions, and no direct ``open`` / ``Path.read_text`` /
    ``Path.write_text`` calls.
    """
    path = SOURCE / "known_hosts_editor.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
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

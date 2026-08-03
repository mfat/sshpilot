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
4. ``core`` / ``daemon`` / ``api`` never import Gtk/GLib/GI; GI stays confined
   to the GTK layer and ``platform``.

Keeping ``ALLOWED`` / ``PENDING_MIGRATIONS`` accurate is a review requirement:
stale entries (a registered import that no longer exists) fail just like new
unregistered violations, so the registry cannot silently grow or rot.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "src" / "sshpilot"

# Top-level packages excluded from the "frontend" scan (internal layers).
_INTERNAL = {"core", "daemon", "api", "platform", "vendor", "locale"}


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
        # -- known-hosts parse/filter for rendering (NOT load/save) -----
        ("known_hosts_editor.py", "known_hosts", "KnownHostEntry"),
        ("known_hosts_editor.py", "known_hosts", "filter_entries"),
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
        # -- pure SSH-overrides composition -------------------------------
        ("preferences.py", "settings", "compose_ssh_overrides"),
        # -- terminal-output evidence classification ----------------------
        ("terminal.py", "connection_evidence", "classify_connection_evidence"),
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
    # === M2 — known-hosts: GTK must read/write the file on the daemon ===
    ("known_hosts_editor.py", "known_hosts", "load_known_hosts"): ("M2", "reads file -> daemon"),
    ("known_hosts_editor.py", "known_hosts", "save_known_hosts"): ("M2", "writes file -> daemon"),
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
    # === M8 — plugins ===================================================
    ("plugins/api.py", "plugins", "API_VERSION"): ("M8", "plugin contracts -> daemon host"),
    ("plugins/api.py", "plugins", "Capability"): ("M8", "plugin contracts -> daemon host"),
    ("plugins/api.py", "plugins", "FieldSpec"): ("M8", "plugin contracts -> daemon host"),
    ("plugins/api.py", "plugins", "SpawnSpec"): ("M8", "plugin contracts -> daemon host"),
    ("plugins/host.py", "plugins", "ALL_EVENTS"): ("M8", "plugin event bus -> daemon host"),
    ("plugins/host.py", "plugins", "ConnectionInfo"): ("M8", "plugin contracts -> daemon host"),
    ("plugins/host.py", "plugins", "EventBus"): ("M8", "plugin event bus -> daemon host"),
    ("plugins/host.py", "plugins", "Events"): ("M8", "plugin event bus -> daemon host"),
    ("plugins/host.py", "plugins", "SessionInfo"): ("M8", "plugin contracts -> daemon host"),
}

KNOWN_TAGS = frozenset("M%d" % i for i in range(1, 9))


# ---------------------------------------------------------------------------
# 3. Daemon reachability from frontend — only enumerated utilities.
# ---------------------------------------------------------------------------
DAEMON_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        # Diagnostics: resolve the daemon socket path for Help ▸ Diagnostics.
        ("log_viewer.py", "resolve_socket_path"),
        # Daemon-adjacent cleanup of stale askpass sockets.
        ("askpass_server.py", "sweep_stale_askpass_sockets"),
    }
)


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


def _core_imports(tree: ast.Module) -> list[tuple[str, str]]:
    """Return (core_submodule, symbol) for every import from sshpilot.core."""
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if node.module.startswith("sshpilot.core"):
            sub = node.module[len("sshpilot.core"):].strip(".") or "core"
        elif node.module == "core" or node.module.startswith("core."):
            sub = node.module[len("core."):]
        else:
            continue
        for alias in node.names:
            if alias.name != "*":
                found.append((sub, alias.name))
    return found


def _daemon_imports(tree: ast.Module) -> list[str]:
    """Return symbol names imported from ``sshpilot.daemon`` (via From)."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "sshpilot.daemon" or node.module.startswith("sshpilot.daemon."):
                found.extend(alias.name for alias in node.names)
    return found


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
        for sub, symbol in _core_imports(tree):
            if (rel, sub, symbol) not in registry:
                violations.append(f"  {rel}: core.{sub}.{symbol}")
    assert not violations, (
        "Frontend imports a core symbol that is neither in the explicit ALLOWED "
        "allowlist nor the PENDING_MIGRATIONS registry. Keep pure helpers local "
        "(add to ALLOWED) or route authoritative access through the daemon API:\n"
        + "\n".join(violations)
    )


def test_pending_tags_are_known():
    assert not PENDING or all(tag in KNOWN_TAGS for (tag, _) in PENDING.values()), (
        "PENDING entry maps to an unknown migration tag"
    )


def test_registry_matches_the_source_tree():
    """Every ALLOWED/PENDING entry must resolve (no stale, no phantom)."""
    seen: set[tuple[str, str, str]] = set()
    for path in _tree_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = _rel(path)
        for sub, symbol in _core_imports(tree):
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


def test_remaining_pending_debt_is_visible(capsys):
    """Surface the remaining daemon-ownership debt so it cannot go stale."""
    by_tag: dict[str, int] = defaultdict(int)
    for (tag, _) in PENDING.values():
        by_tag[tag] += 1
    capsys.readouterr()  # consume
    print(
        "\nRemaining core-boundary migration debt (frontend -> daemon):\n  "
        + "".join(f"{tag}={by_tag[tag]}  " for tag in sorted(by_tag))
    )


def test_frontend_daemon_imports_are_allowlisted():
    """Frontend must not reach into sshpilot.daemon except enumerated utilities."""
    allowed = set(DAEMON_ALLOWLIST)
    violations: list[str] = []
    for path in _tree_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = _rel(path)
        for symbol in _daemon_imports(tree):
            if (rel, symbol) not in allowed:
                violations.append(f"  {rel}: sshpilot.daemon.{symbol}")
    assert not violations, (
        "frontend reaches into sshpilot.daemon outside the explicit "
        "DAEMON_ALLOWLIST:\n" + "\n".join(violations)
    )


def test_core_api_daemon_are_gi_free():
    """core/api/daemon must never import Gtk/GLib/GI."""
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
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                m = node.module
                assert not (m == "sshpilot.daemon" or m.startswith("sshpilot.daemon.")), (
                    f"{path}: core imports daemon ({m})"
                )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("sshpilot.daemon"), (
                        f"{path}: core imports daemon"
                    )


def _tree_paths() -> list[Path]:
    return _frontend_modules()
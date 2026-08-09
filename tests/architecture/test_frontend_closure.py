"""Final frontend-neutral closure guard.

The older boundary test owns the complete backend-operation registry.  This
guard is deliberately narrower: it watches the active frontend surface for
new direct ownership patterns and requires every deferred compatibility edge
to have an identity in the Phase 7 audit.  It is identity-based, so adding a
second call in an unrelated GTK module fails instead of silently increasing a
numeric allowlist.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[2] / "src" / "sshpilot"
AUDIT = Path(__file__).resolve().parents[2] / "docs" / "architecture" / "frontend-closure-audit.md"
INTERNAL = {"api", "core", "daemon", "locale", "platform", "vendor"}

# These are compatibility implementations already covered by the Phase 5
# identity registries.  ``plugins/api.py`` is intentionally absent: it has
# active and dead operations mixed in one public SDK module, so it is checked
# below by function identity rather than hidden by a module exemption.
COMPATIBILITY_MODULES = frozenset(
    {
        "agent_client.py",
        "askpass_utils.py",
        "autocomplete.py",
        "backup_manager.py",
        "bitwarden_setup.py",
        "credential_manager.py",
        "credential_model.py",
        "file_manager/openssh_backend.py",
        "providers/system_agent.py",
        "scp_utils.py",
        "secret_storage.py",
        "sftp_utils.py",
        "ssh_config_utils.py",
        "ssh_multiplex.py",
        "terminal.py",
    }
)

# OS/UI integrations are explicit, narrow exceptions.  They launch an OS
# presentation or installer and do not own SSH Pilot backend state.
FRONTEND_LOCAL_SUBPROCESS = frozenset(
    {
        "platform_utils.py",
        "port_utils.py",
        "startup_info.py",
        "wol.py",
        "sshpilot_agent.py",
    }
)

KNOWN_PLUGIN_GAPS = (
    ("plugins/api.py", "run_command"),
    ("plugins/api.py", "run_command_stream"),
    ("plugins/api.py", "acquire_multiplex"),
    ("plugins/api.py", "release_multiplex"),
)

# Every backend-owning function in the plugin SDK is classified explicitly.
# The three active entries are semantic closure blockers. The dead entries are
# retained public compatibility methods with no current production caller;
# they must not silently become active again. ``ensure_local_forward`` is
# daemon-owned on its active route, but its legacy subprocess branch is kept as
# a separate, explicitly dead identity until the public plugin surface is
# retired or migrated.
PLUGIN_API_BACKEND_IDENTITIES = frozenset(
    {
        "run_command",
        "run_command_stream",
        "_spawn_stream",
        "acquire_multiplex",
        "release_multiplex",
        "copy_key_to_host",
        "get_effective_ssh_config",
        "ensure_local_forward",
    }
)
PLUGIN_API_LOCAL_FUNCTIONS = frozenset(
    {"run_local_command", "run_local_command_stream"}
)

# Stable inventory of the supported PluginContext/facade surface.  This is
# intentionally an identity list, not a generic static analyzer: adding a
# public operational route requires an explicit ownership decision here and
# in the Phase 7 matrix.  Private implementation helpers are checked by the
# process/legacy-edge scan above instead.
PLUGIN_FACADE_SURFACE = {
    # PluginContext
    "PluginContext.for_spawn": "API/daemon owned",
    "PluginContext.register_protocol": "API/daemon owned",
    "PluginContext.add_connection": "API/daemon owned",
    "PluginContext.update_connection": "API/daemon owned",
    "PluginContext.list_connections": "API/daemon owned",
    "PluginContext.open_connection": "API/daemon owned",
    "PluginContext.open_command_terminal": "API/daemon owned",
    "PluginContext.open_local_command_terminal": "legitimate frontend/platform-local",
    "PluginContext.create_group": "API/daemon owned",
    "PluginContext.add_connection_to_group": "API/daemon owned",
    "PluginContext.add_connection_group": "API/daemon owned",
    "PluginContext.generate_key": "API/daemon owned",
    "PluginContext.list_keys": "API/daemon owned",
    "PluginContext.delete_key": "API/daemon owned",
    "PluginContext.run_command": "migration required",
    "PluginContext.run_local_command": "legitimate frontend/platform-local",
    "PluginContext.run_command_stream": "migration required",
    "PluginContext.run_local_command_stream": "legitimate frontend/platform-local",
    "PluginContext.acquire_multiplex": "migration required",
    "PluginContext.release_multiplex": "migration required",
    "PluginContext.ensure_local_forward": "API/daemon owned",
    "PluginContext.get_effective_ssh_config": "dead/unreachable code",
    "PluginContext.copy_key_to_host": "dead/unreachable code",
    "PluginContext.list_sessions": "migration required",
    "PluginContext.read_terminal": "migration required",
    "PluginContext.send_terminal": "migration required",
    "PluginContext.data_dir": "legitimate frontend/platform-local",
    "PluginContext.run_on_ui_thread": "legitimate frontend/platform-local",
    "PluginContext.get_secret": "API/daemon owned",
    "PluginContext.set_secret": "API/daemon owned",
    "PluginContext.delete_secret": "API/daemon owned",
    # Facades
    "_EventsFacade.subscribe": "legitimate frontend/platform-local",
    "_EventsFacade.unsubscribe": "legitimate frontend/platform-local",
    "_UiFacade.register_page": "legitimate frontend/platform-local",
    "_UiFacade.open_page": "legitimate frontend/platform-local",
    "_UiFacade.notify": "legitimate frontend/platform-local",
    "_UiFacade.register_connection_action": "legitimate frontend/platform-local",
    "_UiFacade.open_web_tab": "legitimate frontend/platform-local",
    "_SecretStore.get": "API/daemon owned",
    "_SecretStore.set": "API/daemon owned",
    "_SecretStore.delete": "API/daemon owned",
    "_IdentityView.list": "API/daemon owned",
    "_IdentityView.is_agent_available": "API/daemon owned",
    "_SettingStore.get": "migration required",
    "_SettingStore.set": "migration required",
    "_FilesFacade.path": "legitimate frontend/platform-local",
    "_FilesFacade.exists": "legitimate frontend/platform-local",
    "_FilesFacade.read_text": "legitimate frontend/platform-local",
    "_FilesFacade.read_bytes": "legitimate frontend/platform-local",
    "_FilesFacade.write_text": "legitimate frontend/platform-local",
    "_FilesFacade.write_bytes": "legitimate frontend/platform-local",
    "_HttpFacade.get": "legitimate frontend/platform-local",
    "_HttpFacade.post": "legitimate frontend/platform-local",
}

PLUGIN_FACADE_CLASSES = frozenset(
    {identity.split(".", 1)[0] for identity in PLUGIN_FACADE_SURFACE}
)

# Supporting implementation identities are kept separate from the 53 public
# facade identities.  They are still closure blockers and must remain visible
# to the guard until the corresponding facade capability is migrated.
PLUGIN_SUPPORTING_IMPLEMENTATIONS = {
    "PluginHost.list_sessions": "migration required",
    "PluginHost.read_terminal": "migration required",
    "PluginHost.send_terminal": "migration required",
    "PluginContext._spawn_stream": "migration required",
    "PluginContext._finish_stream_early": "migration required",
}

SEMANTIC_BLOCKER_GROUPS = {
    "P7-PLUGIN-COMMAND": {"PluginContext.run_command"},
    "P7-PLUGIN-STREAM": {"PluginContext.run_command_stream"},
    "P7-PLUGIN-MUX": {
        "PluginContext.acquire_multiplex",
        "PluginContext.release_multiplex",
    },
    "P7-PLUGIN-SETTINGS": {"_SettingStore.get", "_SettingStore.set"},
    "P7-PLUGIN-SESSION-VIEW": {
        "PluginContext.list_sessions",
        "PluginContext.read_terminal",
        "PluginContext.send_terminal",
    },
}

AUDIT_FACADE_CLASSIFICATION_START = "<!-- plugin-facade-classification:start -->"
AUDIT_FACADE_CLASSIFICATION_END = "<!-- plugin-facade-classification:end -->"
AUDIT_SUPPORTING_CLASSIFICATION_START = "<!-- plugin-supporting-classification:start -->"
AUDIT_SUPPORTING_CLASSIFICATION_END = "<!-- plugin-supporting-classification:end -->"
AUDIT_REPORT_START = "<!-- phase7-plugin-report:start -->"
AUDIT_REPORT_END = "<!-- phase7-plugin-report:end -->"


def _frontend_files():
    for path in sorted(SOURCE.rglob("*.py")):
        rel = path.relative_to(SOURCE)
        if "__pycache__" in rel.parts or rel.parts[0] in INTERNAL:
            continue
        yield path, "/".join(rel.parts)


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _function_names(tree: ast.AST):
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _class_method_nodes(path: Path, class_names: set[str]):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name not in class_names:
            continue
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found[f"{node.name}.{child.name}"] = child
    return found


def _plugin_implementation_nodes():
    return {
        **_class_method_nodes(SOURCE / "plugins" / "api.py", {"PluginContext"}),
        **_class_method_nodes(SOURCE / "plugins" / "host.py", {"PluginHost"}),
    }


def _attribute_names(node: ast.AST) -> set[str]:
    return {
        child.attr
        for child in ast.walk(node)
        if isinstance(child, ast.Attribute)
    }


def _has_key_filesystem_edge(node: ast.AST) -> bool:
    """Detect direct key-file deletion edges in a PluginHost method."""
    for child in ast.walk(node):
        if isinstance(child, ast.Import) and any(
            alias.name == "os" for alias in child.names
        ):
            return True
        if isinstance(child, ast.ImportFrom) and child.module == "os":
            return True
        if isinstance(child, ast.Call):
            function = child.func
            if isinstance(function, ast.Name) and function.id in {
                "remove",
                "unlink",
            }:
                return True
            if isinstance(function, ast.Attribute) and function.attr in {
                "remove",
                "unlink",
            }:
                return True
    return False


def _audit_block(text: str, start: str, end: str) -> list[str]:
    try:
        body = text.split(start, 1)[1].split(end, 1)[0]
    except IndexError as exc:
        raise AssertionError(f"missing audit block {start}") from exc
    return [line.strip() for line in body.splitlines() if line.strip()]


def _parse_classification_block(text: str, start: str, end: str) -> dict[str, str]:
    rows = {}
    for line in _audit_block(text, start, end):
        match = re.fullmatch(r"`([^`]+)`\s*\|\s*`([^`]+)`", line)
        if match is None:
            raise AssertionError(f"malformed classification row: {line}")
        identity, status = match.groups()
        if identity in rows:
            raise AssertionError(f"duplicate classification identity: {identity}")
        rows[identity] = status
    return rows


def _parse_report_block(text: str) -> dict[str, int]:
    report = {}
    for line in _audit_block(text, AUDIT_REPORT_START, AUDIT_REPORT_END):
        match = re.fullmatch(r"([a-z /-]+):\s*(\d+)", line)
        if match is None:
            raise AssertionError(f"malformed report row: {line}")
        key, value = match.groups()
        report[key] = int(value)
    return report


def _derived_plugin_report() -> dict[str, int]:
    allowed_statuses = {
        "API/daemon owned",
        "legitimate frontend/platform-local",
        "migration required",
        "dead/unreachable code",
    }
    assert set(PLUGIN_FACADE_SURFACE.values()) <= allowed_statuses
    counts = Counter(PLUGIN_FACADE_SURFACE.values())
    migration_identities = {
        identity
        for identity, status in PLUGIN_FACADE_SURFACE.items()
        if status == "migration required"
    }
    grouped_identities = set().union(*SEMANTIC_BLOCKER_GROUPS.values())
    assert grouped_identities == migration_identities
    assert all(
        PLUGIN_FACADE_SURFACE[identity] == "migration required"
        for identity in grouped_identities
    )
    assert sum(counts.values()) == len(PLUGIN_FACADE_SURFACE)
    return {
        "plugin capabilities audited": len(PLUGIN_FACADE_SURFACE),
        "api/daemon owned": counts["API/daemon owned"],
        "legitimate frontend/platform-local": counts[
            "legitimate frontend/platform-local"
        ],
        "dead/unreachable compatibility": counts["dead/unreachable code"],
        "migration-required public identities": len(migration_identities),
        "semantic migration capabilities": len(SEMANTIC_BLOCKER_GROUPS),
    }


def _plugin_facade_surface(tree: ast.Module) -> set[str]:
    """Return public methods on the supported plugin context/facades."""
    found: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name not in PLUGIN_FACADE_CLASSES:
            continue
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not child.name.startswith("_"):
                    found.add(f"{node.name}.{child.name}")
    return found


def _plugin_backend_functions(tree: ast.Module) -> set[str]:
    """Return plugin SDK functions containing direct backend/process edges."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        has_process = False
        has_legacy_ssh_edge = False
        for child in ast.walk(node):
            if isinstance(child, ast.Import):
                if any(alias.name == "subprocess" for alias in child.names):
                    has_process = True
            elif isinstance(child, ast.ImportFrom):
                module = child.module or ""
                if module.endswith(("ssh_connection_builder", "ssh_config_utils")):
                    has_legacy_ssh_edge = True
                if module.endswith("ssh_multiplex") or module == "sshpilot.ssh_multiplex":
                    has_legacy_ssh_edge = True
                # ``from .. import ssh_multiplex`` has no ImportFrom.module;
                # the imported name is the relevant ownership edge.
                if child.module is None and any(
                    alias.name == "ssh_multiplex" for alias in child.names
                ):
                    has_legacy_ssh_edge = True
            elif isinstance(child, ast.Call):
                if (
                    isinstance(child.func, ast.Attribute)
                    and isinstance(child.func.value, ast.Name)
                    and child.func.value.id == "subprocess"
                    and child.func.attr in {"run", "Popen", "check_call", "check_output"}
                ):
                    has_process = True
        if has_process or has_legacy_ssh_edge:
            found.add(node.name)
    return found


def test_phase7_audit_documents_deferred_plugin_identities():
    text = AUDIT.read_text(encoding="utf-8")
    assert "plugins/api.py" in text
    for function in PLUGIN_API_BACKEND_IDENTITIES:
        assert function in text


def test_plugin_facade_surface_has_an_explicit_classification():
    path = SOURCE / "plugins/api.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assert _plugin_facade_surface(tree) == set(PLUGIN_FACADE_SURFACE)
    text = AUDIT.read_text(encoding="utf-8")
    assert _parse_classification_block(
        text,
        AUDIT_FACADE_CLASSIFICATION_START,
        AUDIT_FACADE_CLASSIFICATION_END,
    ) == PLUGIN_FACADE_SURFACE


def test_plugin_supporting_implementations_keep_explicit_ownership_edges():
    implementation_nodes = _plugin_implementation_nodes()
    assert set(PLUGIN_SUPPORTING_IMPLEMENTATIONS) <= set(implementation_nodes)
    text = AUDIT.read_text(encoding="utf-8")
    assert _parse_classification_block(
        text,
        AUDIT_SUPPORTING_CLASSIFICATION_START,
        AUDIT_SUPPORTING_CLASSIFICATION_END,
    ) == PLUGIN_SUPPORTING_IMPLEMENTATIONS

    host_nodes = {
        identity: node
        for identity, node in implementation_nodes.items()
        if identity.startswith("PluginHost.")
    }
    # These exact GTK/widget ownership edges are the reason the host methods
    # remain blockers.  A new adjacent method with one of these edges also
    # fails the identity check instead of becoming invisible.
    host_edges = {
        identity
        for identity, node in host_nodes.items()
        if (
            "get_content" in _attribute_names(node)
            or "feed_child_data" in _attribute_names(node)
            or {"remove", "realpath"} <= _attribute_names(node)
            or "unlink" in _attribute_names(node)
        )
    }
    assert host_edges == {
        "PluginHost.read_terminal",
        "PluginHost.send_terminal",
    }
    key_filesystem_edges = {
        identity
        for identity, node in host_nodes.items()
        if _has_key_filesystem_edge(node)
    }
    assert key_filesystem_edges == set()
    session_bookkeeping_edges = {
        identity
        for identity, node in host_nodes.items()
        if "_terminal_sessions" in _attribute_names(node)
    }
    assert session_bookkeeping_edges == {
        "PluginHost.__init__",
        "PluginHost.dispatch_session_opened",
        "PluginHost.dispatch_session_closed",
        "PluginHost.list_sessions",
    }
    assert "stop" in _attribute_names(
        implementation_nodes["PluginContext._finish_stream_early"]
    )

    api_nodes = {
        identity: node
        for identity, node in implementation_nodes.items()
        if identity.startswith("PluginContext.")
    }
    stream_process_edges = {
        identity
        for identity, node in api_nodes.items()
        if "Popen" in _attribute_names(node)
    }
    assert stream_process_edges == {
        "PluginContext._spawn_stream",
        "PluginContext.ensure_local_forward",
    }


def test_phase7_plugin_report_counts_are_derived_from_registry():
    text = AUDIT.read_text(encoding="utf-8")
    assert _parse_report_block(text) == _derived_plugin_report()


def test_active_frontend_has_no_direct_backend_process_or_secret_ownership():
    violations = []
    for path, rel in _frontend_files():
        if rel in COMPATIBILITY_MODULES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if rel == "plugins/api.py":
            unexpected = _plugin_backend_functions(tree) - (
                PLUGIN_API_BACKEND_IDENTITIES | PLUGIN_API_LOCAL_FUNCTIONS
            )
            violations.extend(
                f"{rel}: unclassified backend function {name}"
                for name in sorted(unexpected)
            )
            continue
        local_subprocess = rel in FRONTEND_LOCAL_SUBPROCESS
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "subprocess" and not local_subprocess:
                        violations.append(f"{rel}:{node.lineno}: subprocess import")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.endswith("secret_storage"):
                    violations.append(f"{rel}:{node.lineno}: secret_storage import")
                if module.endswith("ssh_multiplex"):
                    violations.append(f"{rel}:{node.lineno}: ssh_multiplex import")
            elif isinstance(node, ast.Call):
                name = _call_name(node)
                if name in {"get_secret_manager", "expire_all_masters"}:
                    violations.append(f"{rel}:{node.lineno}: backend owner {name}")
                if any(keyword in name for keyword in ("ssh", "scp", "sftp")) and name in {
                    "ssh", "scp", "sftp", "ssh_keygen", "ssh_copy_id", "ssh_add"
                }:
                    violations.append(f"{rel}:{node.lineno}: SSH process call {name}")
            elif isinstance(node, ast.keyword) and node.arg == "shell":
                if isinstance(node.value, ast.Constant) and node.value.value is True:
                    violations.append(f"{rel}:{node.lineno}: shell=True")
    assert not violations, "new frontend backend ownership detected:\n" + "\n".join(violations)


def test_relative_multiplex_import_is_an_unclassified_backend_edge():
    tree = ast.parse(
        "def new_unclassified():\n"
        "    from .. import ssh_multiplex\n"
        "    return ssh_multiplex.acquire('example')\n"
    )
    observed = _plugin_backend_functions(tree)
    assert observed == {"new_unclassified"}
    assert observed - PLUGIN_API_BACKEND_IDENTITIES


def test_plugin_gap_identities_are_still_exactly_deferred():
    path = SOURCE / "plugins/api.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = _function_names(tree)
    for module, function in KNOWN_PLUGIN_GAPS:
        assert module == "plugins/api.py"
        assert function in names
    observed = _plugin_backend_functions(tree)
    assert observed == PLUGIN_API_BACKEND_IDENTITIES | {"run_local_command"}
    assert not PLUGIN_API_BACKEND_IDENTITIES & PLUGIN_API_LOCAL_FUNCTIONS


def test_identity_view_is_daemon_client_routed_not_frontend_owned():
    """The plugin identity facade answers only from daemon-owned state.

    ``ctx.identities`` must never fall back to the frontend ``IdentityManager``
    or run ``ssh-add`` locally; its results come through ``SshPilotClient``
    (``identity.provider.keys.get`` / ``identity.providers.get``).
    """
    nodes = _class_method_nodes(SOURCE / "plugins/api.py", {"_IdentityView"})
    assert {"_IdentityView.list", "_IdentityView.is_agent_available"} <= set(nodes)

    for identity in ("_IdentityView.list", "_IdentityView.is_agent_available"):
        node = nodes[identity]
        names = {
            child.attr for child in ast.walk(node) if isinstance(child, ast.Attribute)
        }
        assert "get_identity_manager" not in names, identity
        assert "system_agent" not in names, identity
        assert "ssh_add" not in names, identity

    daemon_routes = {
        child.attr
        for child in ast.walk(nodes["_IdentityView.list"])
        if isinstance(child, ast.Attribute)
    }
    assert "list_provider_agent_keys" in daemon_routes
    availability_routes = {
        child.attr
        for child in ast.walk(nodes["_IdentityView.is_agent_available"])
        if isinstance(child, ast.Attribute)
    }
    assert "get_identity_providers" in availability_routes

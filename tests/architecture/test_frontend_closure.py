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
# ``mcp`` hosts the dev/runtime MCP servers: headless tool layers that are
# neither GTK frontends nor SSH/daemon services. They are excluded from the
# GTK frontend scans and guarded by their own boundary test
# (tests/architecture/test_mcp_boundary.py).
INTERNAL = {"api", "core", "daemon", "locale", "mcp", "platform", "vendor"}

# These are compatibility implementations already covered by the Phase 5
# identity registries.  ``plugins/api.py`` is intentionally absent: it has
# active and dead operations mixed in one public SDK module, so it is checked
# below by function identity rather than hidden by a module exemption.
COMPATIBILITY_MODULES = frozenset(
    {
        "agent_client.py",
        "askpass_utils.py",
        "backup_manager.py",
        "bitwarden_setup.py",
        "credential_manager.py",
        "credential_model.py",
        "providers/system_agent.py",
        "secret_storage.py",
        "sftp_utils.py",
        "ssh_config_utils.py",
        "ssh_multiplex.py",
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
        "terminal.py",
    }
)

KNOWN_PLUGIN_GAPS = ()

# Every backend-owning function in the plugin SDK is classified explicitly.
# Remote operations are routed through the daemon; only the intentionally local
# command stream implementation remains process-owning.
PLUGIN_API_BACKEND_IDENTITIES = frozenset()
PLUGIN_API_LOCAL_FUNCTIONS = frozenset(
    {"run_local_command", "run_local_command_stream", "_spawn_local_stream"}
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
    "PluginContext.run_command": "API/daemon owned",
    "PluginContext.run_local_command": "legitimate frontend/platform-local",
    "PluginContext.run_command_stream": "API/daemon owned",
    "PluginContext.run_local_command_stream": "legitimate frontend/platform-local",
    "PluginContext.acquire_multiplex": "dead/unreachable code",
    "PluginContext.release_multiplex": "dead/unreachable code",
    "PluginContext.ensure_local_forward": "API/daemon owned",
    "PluginContext.list_sessions": "API/daemon owned",
    "PluginContext.read_terminal": "API/daemon owned",
    "PluginContext.send_terminal": "API/daemon owned",
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
    "_SettingStore.get": "API/daemon owned",
    "_SettingStore.set": "API/daemon owned",
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
    "PluginHost.list_sessions": "API/daemon owned",
    "PluginHost.read_terminal": "API/daemon owned",
    "PluginHost.send_terminal": "API/daemon owned",
}

SEMANTIC_BLOCKER_GROUPS = {}

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
    # Host session methods are API projections; widget references remain only
    # for presentation/event compatibility.
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
    assert host_edges == set()
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
    }

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
        "PluginContext._spawn_local_stream",
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
    assert observed == (
        PLUGIN_API_BACKEND_IDENTITIES | {"run_local_command"} | {"_spawn_local_stream"}
    )
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

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
        "release_multiplex",
        "copy_key_to_host",
        "get_effective_ssh_config",
        "ensure_local_forward",
    }
)
PLUGIN_API_LOCAL_FUNCTIONS = frozenset(
    {"run_local_command", "run_local_command_stream"}
)


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


def test_plugin_gap_identities_are_still_exactly_deferred():
    path = SOURCE / "plugins/api.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = _function_names(tree)
    for module, function in KNOWN_PLUGIN_GAPS:
        assert module == "plugins/api.py"
        assert function in names
    observed = _plugin_backend_functions(tree)
    assert observed == PLUGIN_API_BACKEND_IDENTITIES | PLUGIN_API_LOCAL_FUNCTIONS - {
        "run_local_command_stream"
    }
    assert not PLUGIN_API_BACKEND_IDENTITIES & PLUGIN_API_LOCAL_FUNCTIONS

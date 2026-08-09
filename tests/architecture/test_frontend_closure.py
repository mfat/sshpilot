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
# identity registries.  They are not active GTK operation entry points.  Any
# new direct operation in the active surface below is rejected instead of
# being added here casually.
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
        "plugins/api.py",
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


def test_phase7_audit_documents_deferred_plugin_identities():
    text = AUDIT.read_text(encoding="utf-8")
    for module, function in KNOWN_PLUGIN_GAPS:
        assert module in text
        assert function in text


def test_active_frontend_has_no_direct_backend_process_or_secret_ownership():
    violations = []
    for path, rel in _frontend_files():
        if rel in COMPATIBILITY_MODULES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
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
    for module, function in KNOWN_PLUGIN_GAPS:
        path = SOURCE / module
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assert function in _function_names(tree)

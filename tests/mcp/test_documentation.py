"""Keep the coding-agent MCP quickstart discoverable and aligned with code."""

import ast
from pathlib import Path

from sshpilot.mcp.dev.server import SERVER_NAME as DEV_SERVER_NAME
from sshpilot.mcp.runtime.server import SERVER_NAME as RUNTIME_SERVER_NAME


ROOT = Path(__file__).resolve().parents[2]
QUICKSTART = ROOT / "docs" / "mcp" / "README.md"


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _dev_tool_names() -> set[str]:
    tree = ast.parse(_text("src/sshpilot/mcp/dev/server.py"))
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if isinstance(target, ast.Attribute) and target.attr == "tool":
                names.add(node.name)
    return names


def test_mcp_quickstart_is_linked_from_contributor_entry_points():
    assert QUICKSTART.is_file()
    for path in ("README.md", "CONTRIBUTING.md", "docs/README.md"):
        assert "mcp/README.md" in _text(path), path


def test_mcp_quickstart_names_every_dev_tool():
    quickstart = QUICKSTART.read_text(encoding="utf-8")
    missing = {name for name in _dev_tool_names() if f"`{name}`" not in quickstart}
    assert missing == set()


def test_mcp_quickstart_documents_runtime_policy_environment():
    quickstart = QUICKSTART.read_text(encoding="utf-8")
    for name in (
        "SSHPILOT_MCP_ROOT",
        "SSHPILOT_MCP_SOCKET",
        "SSHPILOT_MCP_READ",
        "SSHPILOT_MCP_OPERATE",
        "SSHPILOT_MCP_MUTATE",
        "SSHPILOT_MCP_CONTENT",
    ):
        assert name in quickstart


def test_mcp_server_names_match_console_scripts_and_docs():
    assert DEV_SERVER_NAME == "sshpilot-mcp-dev"
    assert RUNTIME_SERVER_NAME == "sshpilot-mcp-runtime"
    quickstart = QUICKSTART.read_text(encoding="utf-8")
    pyproject = _text("pyproject.toml")
    for name in (DEV_SERVER_NAME, RUNTIME_SERVER_NAME):
        assert name in quickstart
        assert f"{name} =" in pyproject

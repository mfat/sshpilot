"""End-to-end stdio smoke test for the dev MCP server.

Boots the real server as a subprocess over stdio (the same path an MCP client
would take) and exercises the protocol handshake, tool enumeration, and tool
invocation.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = str(REPO_ROOT / "src")

pytestmark = pytest.mark.asyncio


def _server_parameters(root: Path) -> StdioServerParameters:
    env = os.environ.copy()
    env["PYTHONPATH"] = SRC
    env["SSHPILOT_MCP_ROOT"] = str(root)
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "sshpilot.mcp.dev"],
        cwd=str(REPO_ROOT),
        env=env,
    )


async def test_stdio_handshake_and_tools():
    async with stdio_client(_server_parameters(REPO_ROOT)) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            assert init.server_info.name == "sshpilot-dev-mcp"

            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert {
                "repo_info",
                "read_text_file",
                "list_directory",
                "search_source",
                "find_symbol",
                "git_status",
                "git_log",
                "git_diff",
                "find_tests",
                "inspect_api",
                "list_api_methods",
                "trace_api_method",
                "check_api_drift",
            } <= names


async def test_repo_info_reports_confined_root():
    async with stdio_client(_server_parameters(REPO_ROOT)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("repo_info", {})
            assert not result.is_error
            text = result.content[0].text
            assert str(REPO_ROOT.resolve()) in text
            assert "True" in text or "true" in text  # git_repository


async def test_read_text_file_roundtrip():
    async with stdio_client(_server_parameters(REPO_ROOT)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "read_text_file", {"path": "AGENTS.md"}
            )
            assert not result.is_error
            assert "typed frontend-neutral API" in result.content[0].text


async def test_read_text_file_rejects_traversal():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        (root / ".git").mkdir(parents=True)
        (root / "a.txt").write_text("inside")
        secret = Path(tmp) / "secret.txt"
        secret.write_text("secret")
        async with stdio_client(_server_parameters(root)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                # A file that exists *outside* the confined root must be refused.
                result = await session.call_tool(
                    "read_text_file", {"path": "../secret.txt"}
                )
                assert result.is_error
                assert "escapes" in result.content[0].text


async def test_read_text_file_rejects_absolute(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "a.txt").write_text("hi")
    async with stdio_client(_server_parameters(root)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "read_text_file", {"path": str(root / "a.txt")}
            )
            assert result.is_error


async def test_list_directory_over_stdio():
    async with stdio_client(_server_parameters(REPO_ROOT)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "list_directory", {"path": "src/sshpilot/mcp"}
            )
            assert not result.is_error
            text = result.content[0].text
            assert "dev" in text
            assert "_scope.py" in text


async def test_search_source_over_stdio():
    async with stdio_client(_server_parameters(REPO_ROOT)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "search_source",
                {"pattern": "class RepoScope", "path_prefix": "src"},
            )
            assert not result.is_error
            assert "mcp/_scope.py" in result.content[0].text


async def test_find_symbol_over_stdio():
    async with stdio_client(_server_parameters(REPO_ROOT)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "find_symbol", {"name": "RepoScope", "kind": "class"}
            )
            assert not result.is_error
            assert "mcp/_scope.py" in result.content[0].text


async def test_git_tools_over_stdio():
    async with stdio_client(_server_parameters(REPO_ROOT)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            status = await session.call_tool("git_status", {})
            assert not status.is_error
            assert "branch" in status.content[0].text
            log = await session.call_tool("git_log", {"limit": 5})
            assert not log.is_error
            assert "commits" in log.content[0].text
            diff = await session.call_tool(
                "git_diff", {"base": "HEAD", "stat_only": True}
            )
            assert not diff.is_error


async def test_find_tests_over_stdio():
    async with stdio_client(_server_parameters(REPO_ROOT)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "find_tests", {"path": "tests/mcp"}
            )
            assert not result.is_error
            assert "test_repo_scope.py" in result.content[0].text
            assert "total" in result.content[0].text


async def test_inspect_api_over_stdio():
    async with stdio_client(_server_parameters(REPO_ROOT)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("inspect_api", {})
            assert not result.is_error
            text = result.content[0].text
            assert "protocol_version" in text
            assert "client_methods" in text


async def test_trace_api_method_over_stdio():
    async with stdio_client(_server_parameters(REPO_ROOT)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "trace_api_method", {"method": "open_session"}
            )
            assert not result.is_error
            text = result.content[0].text
            assert "sessions.open" in text
            assert "_handle_open_session" in text


async def test_check_api_drift_over_stdio():
    async with stdio_client(_server_parameters(REPO_ROOT)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("check_api_drift", {})
            assert not result.is_error
            text = result.content[0].text
            assert '"clean": true' in text
            assert "0" in text
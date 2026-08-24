"""OPERATE round-trips over the runtime MCP server against real OpenSSH.

Boots the full Phase 13 stack (ephemeral daemon + real daemon client + Alpine
sshd container), then launches ``python -m sshpilot.mcp.runtime`` as a
subprocess and drives OPERATE tools over stdio: open a session on a real
connection, observe it on the daemon, and close it. Uses password
authentication with an auto-answering helper, so no secret is ever placed in
the MCP conversation.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# ``mcp`` is an optional extra (pyproject: mcp>=2.0.0) and this module needs
# the 2.x server API that sshpilot.mcp imports. Gate on that module rather
# than on the ``mcp`` package name: an older 1.x install imports as ``mcp``
# perfectly well and would fail here at runtime instead of skipping.
pytest.importorskip("mcp.server.mcpserver")

from tests.daemon.phase10_helpers import (
    require_phase10_container,
    wait_until,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = str(REPO_ROOT / "src")


@pytest.fixture(scope="module")
def phase10_env(tmp_path_factory):
    env = require_phase10_container(tmp_path_factory.mktemp("runtime-mcp-ssh"))
    try:
        yield env
    finally:
        env.destroy()


@pytest.fixture
def stack(tmp_path, phase10_env):
    from tests.daemon.phase10_helpers import start_phase10_stack

    started = start_phase10_stack(tmp_path, env=phase10_env)
    try:
        yield started
    finally:
        started.close(destroy_env=False)


def _server_parameters(socket_path: Path):
    return _server_parameters_with(socket_path, content=False)


def _server_parameters_with(socket_path: Path, *, content: bool):
    from mcp.client.stdio import StdioServerParameters

    env = os.environ.copy()
    env["PYTHONPATH"] = SRC
    env["SSHPILOT_MCP_SOCKET"] = str(socket_path)
    env["SSHPILOT_MCP_READ"] = "1"
    env["SSHPILOT_MCP_OPERATE"] = "1"
    env["SSHPILOT_MCP_MUTATE"] = "1"
    if content:
        env["SSHPILOT_MCP_CONTENT"] = "1"
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "sshpilot.mcp.runtime"],
        cwd=str(REPO_ROOT),
        env=env,
    )


async def _read_breadth_file(session, service_id, path):
    """Return the ``content`` value for a remote file read."""
    result = await session.call_tool(
        "sftp_read_file",
        {"service_id": service_id, "path": path},
    )
    assert not result.is_error, result.content[0].text
    import json

    return json.loads(result.content[0].text).get("content")


async def test_read_file_content_redacted_without_content_opt_in(stack):
    """Remote file content stays out of model context by default.

    ``SftpReadFileResult.content`` is daemon-marked ``repr=False``; without the
    ``SSHPILOT_MCP_CONTENT`` opt-in the runtime returns ``<redacted>`` instead
    of the real bytes, so a private key or credentials file read never reaches
    the model context.
    """
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    stack.start_password_auto_answer()

    async with stdio_client(_server_parameters(stack.server.socket_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            result = await session.call_tool(
                "open_sftp", {"connection_id": str(stack.connection.id)}
            )
            assert not result.is_error, result.content[0].text
            service_id = _extract_json_id(result.content[0].text, label="SFTP service")
            stack.wait_sftp_ready(service_id)

            path = "mcp-secret-demo"
            result = await session.call_tool(
                "sftp_create_file",
                {"service_id": service_id, "path": path, "confirm": True},
            )
            assert not result.is_error, result.content[0].text

            content = await _read_breadth_file(session, service_id, path)
            assert content == "<redacted>"

            await session.call_tool("close_sftp", {"service_id": service_id})


async def test_read_file_content_returned_with_content_opt_in(stack):
    """Explicit ``SSHPILOT_MCP_CONTENT`` restores real remote file content."""
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    stack.start_password_auto_answer()

    async with stdio_client(
        _server_parameters_with(stack.server.socket_path, content=True)
    ) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            result = await session.call_tool(
                "open_sftp", {"connection_id": str(stack.connection.id)}
            )
            assert not result.is_error, result.content[0].text
            service_id = _extract_json_id(result.content[0].text, label="SFTP service")
            stack.wait_sftp_ready(service_id)

            path = "mcp-secret-demo"
            result = await session.call_tool(
                "sftp_create_file",
                {"service_id": service_id, "path": path, "confirm": True},
            )
            assert not result.is_error, result.content[0].text

            content = await _read_breadth_file(session, service_id, path)
            assert content == ""

            await session.call_tool("close_sftp", {"service_id": service_id})


async def test_open_and_close_session_over_stdio(stack):
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    stack.start_password_auto_answer()

    async with stdio_client(_server_parameters(stack.server.socket_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            result = await session.call_tool(
                "open_session", {"connection_id": str(stack.connection.id)}
            )
            assert not result.is_error, result.content[0].text
            opened = result.content[0].text
            assert "session" in opened

            watching = stack.connect_client()
            try:
                wait_until(
                    lambda: any(
                        getattr(s, "state", "").name == "RUNNING"
                        for s in watching.list_sessions()
                    ),
                    message="session did not reach RUNNING on the daemon",
                )
            finally:
                watching.close()

            session_id = _extract_session_id(opened)
            result = await session.call_tool("close_session", {"session_id": session_id})
            assert not result.is_error, result.content[0].text

            watching = stack.connect_client()
            try:
                wait_until(
                    lambda: not any(
                        getattr(s, "state", "").name in {"STARTING", "RUNNING", "CLOSING"}
                        for s in watching.list_sessions()
                    ),
                    message="session did not close on the daemon",
                )
            finally:
                watching.close()


async def test_open_and_close_sftp_over_stdio(stack):
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    stack.start_password_auto_answer()

    async with stdio_client(_server_parameters(stack.server.socket_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            result = await session.call_tool(
                "open_sftp", {"connection_id": str(stack.connection.id)}
            )
            assert not result.is_error, result.content[0].text
            opened = result.content[0].text
            service_id = _extract_json_id(opened, label="SFTP service")

            watching = stack.connect_client()
            try:
                wait_until(
                    lambda: any(
                        getattr(s, "state", "").name == "READY"
                        for s in watching.list_sftp_services()
                    ),
                    message="SFTP service did not reach READY on the daemon",
                )
            finally:
                watching.close()

            result = await session.call_tool("close_sftp", {"service_id": service_id})
            assert not result.is_error, result.content[0].text

            watching = stack.connect_client()
            try:
                wait_until(
                    lambda: not any(
                        getattr(s, "state", "").name in {"OPENING", "READY", "CLOSING"}
                        for s in watching.list_sftp_services()
                    ),
                    message="SFTP service did not close on the daemon",
                )
            finally:
                watching.close()


async def test_mutate_sftp_with_confirm_over_stdio(stack):
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    stack.start_password_auto_answer()

    async with stdio_client(_server_parameters(stack.server.socket_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            result = await session.call_tool(
                "open_sftp", {"connection_id": str(stack.connection.id)}
            )
            assert not result.is_error, result.content[0].text
            service_id = _extract_json_id(result.content[0].text, label="SFTP service")
            stack.wait_sftp_ready(service_id)

            path = "mcp-mutate-demo"

            result = await session.call_tool(
                "sftp_mkdir", {"service_id": service_id, "path": path}
            )
            assert result.is_error, "mkdir without confirm must be refused"

            result = await session.call_tool(
                "sftp_mkdir",
                {"service_id": service_id, "path": path, "confirm": True},
            )
            assert not result.is_error, result.content[0].text

            result = await session.call_tool(
                "sftp_list_directory",
                {"connection_id": str(stack.connection.id), "service_id": service_id, "path": "."},
            )
            assert not result.is_error, result.content[0].text
            assert path in result.content[0].text

            result = await session.call_tool(
                "sftp_rmdir",
                {"service_id": service_id, "path": path, "confirm": True},
            )
            assert not result.is_error, result.content[0].text

            result = await session.call_tool(
                "sftp_list_directory",
                {"connection_id": str(stack.connection.id), "service_id": service_id, "path": "."},
            )
            assert not result.is_error, result.content[0].text
            assert path not in result.content[0].text

            await session.call_tool("close_sftp", {"service_id": service_id})


async def test_read_and_mutate_breadth_over_stdio(stack):
    """Exercise READ (stat/read_file) and remaining MUTATE tools over stdio.

    create_file, rename, chmod, symlink, and remove round-trip against a real
    SSHFS/SFTP backing store and are verified back through the READ surface.
    Every MUTATE tool must refuse without ``confirm=True``.
    """
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    stack.start_password_auto_answer()

    async with stdio_client(_server_parameters(stack.server.socket_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            result = await session.call_tool(
                "open_sftp", {"connection_id": str(stack.connection.id)}
            )
            assert not result.is_error, result.content[0].text
            service_id = _extract_json_id(result.content[0].text, label="SFTP service")
            stack.wait_sftp_ready(service_id)

            # ---- READ: stat and read_file -----------------------------
            result = await session.call_tool(
                "sftp_stat",
                {"service_id": service_id, "path": "."},
            )
            assert not result.is_error, result.content[0].text
            assert "directory" in result.content[0].text.lower()

            created_path = "mcp-breadth-file.txt"
            result = await session.call_tool(
                "sftp_stat",
                {"service_id": service_id, "path": created_path},
            )
            assert result.is_error, "stat of a missing path must be an error"

            # ---- MUTATE: create_file ----------------------------------
            result = await session.call_tool(
                "sftp_create_file",
                {"service_id": service_id, "path": created_path},
            )
            assert result.is_error, "create_file without confirm must be refused"

            result = await session.call_tool(
                "sftp_create_file",
                {"service_id": service_id, "path": created_path, "confirm": True},
            )
            assert not result.is_error, result.content[0].text

            result = await session.call_tool(
                "sftp_stat",
                {"service_id": service_id, "path": created_path},
            )
            assert not result.is_error, result.content[0].text
            assert "file" in result.content[0].text.lower()

            result = await session.call_tool(
                "sftp_read_file",
                {"service_id": service_id, "path": created_path},
            )
            assert not result.is_error, result.content[0].text
            assert "content" in result.content[0].text

            # ---- MUTATE: rename, chmod, symlink, remove ---------------
            renamed_path = "mcp-breadth-renamed.txt"
            result = await session.call_tool(
                "sftp_rename",
                {
                    "service_id": service_id,
                    "source_path": created_path,
                    "destination_path": renamed_path,
                    "overwrite": True,
                },
            )
            assert result.is_error, "rename without confirm must be refused"

            result = await session.call_tool(
                "sftp_rename",
                {
                    "service_id": service_id,
                    "source_path": created_path,
                    "destination_path": renamed_path,
                    "overwrite": True,
                    "confirm": True,
                },
            )
            assert not result.is_error, result.content[0].text

            result = await session.call_tool(
                "sftp_stat",
                {"service_id": service_id, "path": renamed_path},
            )
            assert not result.is_error, result.content[0].text
            assert "file" in result.content[0].text.lower()

            result = await session.call_tool(
                "sftp_chmod",
                {"service_id": service_id, "path": renamed_path, "mode": 0o640},
            )
            assert result.is_error, "chmod without confirm must be refused"

            result = await session.call_tool(
                "sftp_chmod",
                {"service_id": service_id, "path": renamed_path, "mode": 0o640, "confirm": True},
            )
            assert not result.is_error, result.content[0].text

            link_path = "mcp-breadth-link"
            result = await session.call_tool(
                "sftp_symlink",
                {
                    "service_id": service_id,
                    "target_path": renamed_path,
                    "link_path": link_path,
                },
            )
            assert result.is_error, "symlink without confirm must be refused"

            result = await session.call_tool(
                "sftp_symlink",
                {
                    "service_id": service_id,
                    "target_path": renamed_path,
                    "link_path": link_path,
                    "confirm": True,
                },
            )
            assert not result.is_error, result.content[0].text

            result = await session.call_tool(
                "sftp_stat",
                {"service_id": service_id, "path": link_path},
            )
            assert not result.is_error, result.content[0].text

            result = await session.call_tool(
                "sftp_remove",
                {"service_id": service_id, "path": link_path},
            )
            assert result.is_error, "remove without confirm must be refused"

            result = await session.call_tool(
                "sftp_remove",
                {"service_id": service_id, "path": link_path, "confirm": True},
            )
            assert not result.is_error, result.content[0].text

            result = await session.call_tool(
                "sftp_remove",
                {"service_id": service_id, "path": renamed_path, "confirm": True},
            )
            assert not result.is_error, result.content[0].text

            result = await session.call_tool(
                "sftp_stat",
                {"service_id": service_id, "path": renamed_path},
            )
            assert result.is_error, "removed file must no longer stat"

            await session.call_tool("close_sftp", {"service_id": service_id})


def _extract_json_id(text: str, *, label: str) -> str:
    """Pull the ``id`` field out of an MCP JSON text result."""
    import json

    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        raise AssertionError(f"{label} result was not JSON: {text!r}") from None
    value = payload.get("id")
    if not value:
        raise AssertionError(f"no id in {label} result: {text!r}")
    return str(value)


def _extract_session_id(text: str) -> str:
    """Pull the daemon session id out of the MCP text result."""
    return _extract_json_id(text, label="session")
"""End-to-end stdio round-trip for the runtime MCP server.

Boots an ephemeral ``DaemonServer`` (headless core), then launches
``python -m sshpilot.mcp.runtime`` as a real subprocess (the same path an MCP
client takes) with ``SSHPILOT_MCP_SOCKET`` pointing at the daemon. Connects a
``ClientSession`` over stdio and exercises the protocol handshake, tool
enumeration, and READ tool invocation against the live daemon.
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

from sshpilot.daemon import DaemonServer
from tests.daemon.phase10_helpers import wait_until

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = str(REPO_ROOT / "src")


class _TestConnectionManager:
    def __init__(self):
        from sshpilot.api.models.connection_store import ConnectionStoreSnapshot
        from sshpilot.api.models.common import ConnectionId
        from sshpilot.api.models.connections import ConnectionHealth, ConnectionSummary
        from sshpilot.core.connections.models import ConnectionRecord

        self._summary = ConnectionSummary(
            id=ConnectionId("demo"),
            nickname="demo",
            host="demo",
            hostname="example.test",
            username="alice",
            port=22,
            protocol="ssh",
            health=ConnectionHealth.UNKNOWN,
        )
        self._snapshot = ConnectionStoreSnapshot(
            generation=0,
            connections=(self._summary,),
            groups=(),
            root_connection_ids=(ConnectionId("demo"),),
            metadata=(),
        )
        self._record = ConnectionRecord.from_dict(
            {
                "nickname": "demo",
                "hostname": "example.test",
                "username": "alice",
                "port": 22,
                "protocol": "ssh",
            },
            connection_id="demo",
        )
        self._listeners = []

    def snapshot(self):
        return self._snapshot

    def list_records(self):
        return (self._record,)

    def get_record(self, connection_id):
        return self._record if str(connection_id) == "demo" else None

    def get_editor_record(self, connection_id):
        return self.get_record(connection_id)

    def add_listener(self, callback):
        self._listeners.append(callback)

    def remove_listener(self, callback):
        self._listeners.remove(callback)


@pytest.fixture()
def daemon_socket(tmp_path):
    from sshpilot.core.connection_application_service import ConnectionApplicationService

    socket_path = tmp_path / "daemon" / "sshpilotd.sock"
    socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    socket_path.parent.chmod(0o700)

    manager = _TestConnectionManager()
    server = DaemonServer(
        lambda: ConnectionApplicationService(
            manager,
            secret_provider=manager,
            client_name="sshpilotd-stdio-test",
        ),
        socket_path=socket_path,
        idle_shutdown_seconds=0.0,
    )
    server.start_in_thread()
    try:
        yield socket_path
    finally:
        server.shutdown()
        server.wait_stopped()


def _server_parameters(socket_path: Path):
    from mcp.client.stdio import StdioServerParameters

    env = os.environ.copy()
    env["PYTHONPATH"] = SRC
    env["SSHPILOT_MCP_SOCKET"] = str(socket_path)
    env["SSHPILOT_MCP_READ"] = "1"
    env["SSHPILOT_MCP_OPERATE"] = "1"
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "sshpilot.mcp.runtime"],
        cwd=str(REPO_ROOT),
        env=env,
    )


async def test_stdio_handshake_and_read_tools(daemon_socket):
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    wait_until(lambda: daemon_socket.exists(), timeout=5.0)

    async with stdio_client(_server_parameters(daemon_socket)) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            assert init.server_info.name == "sshpilot-mcp-runtime"

            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert {
                "capabilities",
                "daemon_status",
                "list_connections",
                "list_sftp_services",
            } <= names


async def test_capabilities_over_stdio(daemon_socket):
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    wait_until(lambda: daemon_socket.exists(), timeout=5.0)

    async with stdio_client(_server_parameters(daemon_socket)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("capabilities", {})
            assert not result.is_error
            assert "sessions.read" in result.content[0].text


async def test_daemon_status_over_stdio(daemon_socket):
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    wait_until(lambda: daemon_socket.exists(), timeout=5.0)

    async with stdio_client(_server_parameters(daemon_socket)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("daemon_status", {})
            assert not result.is_error
            assert "running" in result.content[0].text


async def test_list_connections_over_stdio(daemon_socket):
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    wait_until(lambda: daemon_socket.exists(), timeout=5.0)

    async with stdio_client(_server_parameters(daemon_socket)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("list_connections", {})
            assert not result.is_error
            assert "demo" in result.content[0].text

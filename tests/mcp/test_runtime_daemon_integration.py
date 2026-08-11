"""Real-daemon integration for the runtime MCP server.

Boots an ephemeral ``DaemonServer`` (headless core, no real SSH backend) in
this process, connects a real ``DaemonClient`` to it, wraps it in a
``RuntimeHandle``, and drives the runtime MCP server over the official SDK's
in-memory streams. Exercises the full path the wire would take:
``MCP -> DaemonClient -> daemon -> core``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator
import anyio
import pytest

from sshpilot.api.daemon_client import DaemonClient
from sshpilot.daemon import DaemonServer
from sshpilot.mcp.runtime.policy import RuntimePolicy
from sshpilot.mcp.runtime.server import RuntimeHandle, create_server

pytestmark = pytest.mark.anyio


class _TestConnectionManager:
    """Headless connection repository backing the ephemeral daemon."""

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
        if str(connection_id) == "demo":
            return self._record
        return None

    def get_editor_record(self, connection_id):
        return self.get_record(connection_id)

    def add_listener(self, callback):
        self._listeners.append(callback)

    def remove_listener(self, callback):
        self._listeners.remove(callback)


def _build_server(manager):
    server = DaemonServer(
        lambda: __import__(
            "sshpilot.core.connection_application_service",
            fromlist=["ConnectionApplicationService"],
        ).ConnectionApplicationService(
            manager,
            secret_provider=manager,
            client_name="sshpilotd-test",
        ),
        socket_path=manager._socket_path,
        idle_shutdown_seconds=0.0,
    )
    return server


@asynccontextmanager
async def _daemon_session(tmp_path) -> AsyncIterator:
    """Start an ephemeral daemon, return (server, client, handle, mcp server)."""
    from mcp import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    socket_path = tmp_path / "daemon" / "sshpilotd.sock"
    socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    socket_path.parent.chmod(0o700)

    manager = _TestConnectionManager()
    manager._socket_path = socket_path
    server = _build_server(manager)
    server.start_in_thread()
    try:
        client = DaemonClient(socket_path=socket_path, client_name="mcp-test")
        handle = RuntimeHandle(
            client,
            RuntimePolicy(allow_read=True, allow_operate=True, allow_mutate=True),
        )
        mcp_server = create_server(handle)
        async with create_client_server_memory_streams() as (client_streams, server_streams):

            async def serve():
                await mcp_server._lowlevel_server.run(
                    server_streams[0],
                    server_streams[1],
                    mcp_server._lowlevel_server.create_initialization_options(),
                )

            async with anyio.create_task_group() as tg:
                tg.start_soon(serve)
                async with ClientSession(client_streams[0], client_streams[1]) as session:
                    await session.initialize()
                    yield session, client
                tg.cancel_scope.cancel()
    finally:
        client.close()
        server.shutdown()
        server.wait_stopped()


async def test_capabilities_over_real_daemon(tmp_path):
    async with _daemon_session(tmp_path) as (session, client):
        result = await session.call_tool("capabilities", {})
        assert not result.is_error
        text = result.content[0].text
        assert "sessions.read" in text
        assert "sftp.read" in text
        assert "transfers.read" in text


async def test_daemon_status_over_real_daemon(tmp_path):
    async with _daemon_session(tmp_path) as (session, client):
        result = await session.call_tool("daemon_status", {})
        assert not result.is_error
        assert "running" in result.content[0].text


async def test_list_connections_over_real_daemon(tmp_path):
    async with _daemon_session(tmp_path) as (session, client):
        result = await session.call_tool("list_connections", {})
        assert not result.is_error
        assert "demo" in result.content[0].text


async def test_direct_client_roundtrip(tmp_path):
    """Sanity: DaemonClient itself reaches the daemon (independent of MCP)."""
    async with _daemon_session(tmp_path) as (session, client):
        status = client.get_daemon_status()
        assert status
        conns = client.list_connections()
        assert any(getattr(c, "nickname", None) == "demo" for c in conns)
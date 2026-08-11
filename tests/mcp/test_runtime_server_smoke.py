"""In-process smoke tests for the runtime MCP server over the real protocol.

Boots ``create_server`` around a fake daemon-client stand-in and drives it over
the official ``mcp`` SDK's in-memory client/server streams. Confirms the
handshake, tool enumeration, READ round-trips, and that MUTATE refuses without
explicit ``confirm=True``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import anyio
import pytest

import sshpilot.mcp.runtime.server as runtime_server
from sshpilot.mcp.runtime.policy import RuntimePolicy
from sshpilot.mcp.runtime.server import RuntimeHandle, create_server

pytestmark = pytest.mark.anyio


class FakeDaemonClient:
    """Minimal stand-in exposing the daemon-client surface the runtime uses."""

    def __init__(self) -> None:
        self.calls = []

    def get_capabilities(self):
        self.calls.append("get_capabilities")
        return {"capabilities": ["connect", "sessions", "sftp"]}

    def get_daemon_status(self):
        self.calls.append("get_daemon_status")
        return {"state": "running", "version": "0.1.0-test"}

    def list_sftp_services(self):
        self.calls.append("list_sftp_services")
        return [{"service_id": "srv-1", "connection_id": "conn-1"}]


def _build_server(*, mutate: bool = True):
    client = FakeDaemonClient()
    policy = RuntimePolicy(
        allow_read=True,
        allow_operate=True,
        allow_mutate=mutate,
    )
    return create_server(RuntimeHandle(client, policy)), client


@asynccontextmanager
async def _client_session(mutate: bool = True) -> AsyncIterator:
    from mcp import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    server, client = _build_server(mutate=mutate)
    async with create_client_server_memory_streams() as (client_streams, server_streams):

        async def serve():
            await server._lowlevel_server.run(
                server_streams[0],
                server_streams[1],
                server._lowlevel_server.create_initialization_options(),
            )

        async with anyio.create_task_group() as tg:
            tg.start_soon(serve)
            async with ClientSession(client_streams[0], client_streams[1]) as session:
                await session.initialize()
                yield session, client
            tg.cancel_scope.cancel()


async def test_handshake_and_typed_tools():
    async with _client_session() as (session, client):
        init = await session.initialize()
        assert init.server_info.name == runtime_server.SERVER_NAME

        tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        assert {
            "capabilities",
            "daemon_status",
            "list_sftp_services",
            "sftp_list_directory",
            "open_session",
            "open_sftp",
            "sftp_mkdir",
            "cancel_transfer",
        } <= names


async def test_read_tool_roundtrip():
    async with _client_session() as (session, client):
        result = await session.call_tool("capabilities", {})
        assert not result.is_error
        assert "capabilities" in result.content[0].text

        result = await session.call_tool("daemon_status", {})
        assert not result.is_error
        assert "running" in result.content[0].text


async def test_mutate_requires_confirm():
    async with _client_session() as (session, client):
        result = await session.call_tool(
            "sftp_mkdir", {"service_id": "srv-1", "path": "/tmp/newdir"}
        )
        assert result.is_error
        assert "confirm" in result.content[0].text
        assert client.calls == []


async def test_mutate_refuses_when_disabled_even_with_confirm():
    async with _client_session(mutate=False) as (session, client):
        result = await session.call_tool(
            "sftp_mkdir",
            {"service_id": "srv-1", "path": "/tmp/newdir", "confirm": True},
        )
        assert result.is_error
        assert "not permitted" in result.content[0].text
        assert client.calls == []
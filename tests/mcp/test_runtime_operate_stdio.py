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
    from mcp.client.stdio import StdioServerParameters

    env = os.environ.copy()
    env["PYTHONPATH"] = SRC
    env["SSHPILOT_MCP_SOCKET"] = str(socket_path)
    env["SSHPILOT_MCP_READ"] = "1"
    env["SSHPILOT_MCP_OPERATE"] = "1"
    env["SSHPILOT_MCP_MUTATE"] = "1"
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "sshpilot.mcp.runtime"],
        cwd=str(REPO_ROOT),
        env=env,
    )


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
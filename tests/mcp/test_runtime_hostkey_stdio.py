"""Host-key TOFU round-trip over the runtime MCP server against real OpenSSH.

Boots the full Phase 13 stack (ephemeral daemon + real daemon client + Alpine
sshd container), points the connection at a config that treats the fixture host
as *unknown* (``StrictHostKeyChecking ask`` with an empty ``UserKnownHostsFile``
and ``GlobalKnownHostsFile /dev/null``), then launches
``python -m sshpilot.mcp.runtime`` as a subprocess. Opening a session over MCP
forces a real ``HOST_KEY_CONFIRMATION`` interaction to surface from OpenSSH
through the daemon's askpass broker, scoped to the session being opened.

The MCP conversation observes the interaction (``list_interactions`` /
``get_interaction``), claims it (``claim_interaction``) and releases it back
(``release_interaction``) — the model never decides host-key trust, matching
D003. The trusted in-process broker then accepts the key and the session reaches
RUNNING; MCP closes the session.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import anyio
import pytest

from tests.daemon.phase10_helpers import require_phase10_container, wait_until

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = str(REPO_ROOT / "src")


@pytest.fixture(scope="module")
def phase10_env(tmp_path_factory):
    env = require_phase10_container(tmp_path_factory.mktemp("runtime-mcp-hostkey"))
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


def _mount_unknown_host_config(stack, tmp_path: Path) -> Path:
    """Rewrite the connection config so the fixture host looks unknown.

    ``UserKnownHostsFile`` points at an empty file and ``GlobalKnownHostsFile``
    is disabled, so the Alpine sshd key is genuinely unknown to the SSH client.
    """

    known = tmp_path / "empty_known_hosts"
    known.write_text("")
    known.chmod(0o600)
    config = tmp_path / "ask_config"
    config.write_text(
        "\n".join(
            (
                f"Host {stack.env.host_alias}",
                "    HostName 127.0.0.1",
                f"    Port {stack.env.port}",
                f"    User {stack.env.username}",
                "    PreferredAuthentications password",
                "    PubkeyAuthentication no",
                "    NumberOfPasswordPrompts 1",
                f"    UserKnownHostsFile {known}",
                "    GlobalKnownHostsFile /dev/null",
                "    StrictHostKeyChecking ask",
                "    IdentitiesOnly yes",
                "    IdentityFile /dev/null",
                "    LogLevel ERROR",
            )
        )
        + "\n"
    )
    stack.connection.source = str(config)
    stack.connection.config_root = str(config)
    stack.connection.data["config_root"] = str(config)
    return config


def _json_id(text: str, *, label: str) -> str:
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        raise AssertionError(f"{label} result was not JSON: {text!r}") from None
    value = payload.get("id")
    if not value:
        raise AssertionError(f"no id in {label} result: {text!r}")
    return str(value)


def _collect_lists(items) -> list:
    """Flatten MCP content blocks into summary dicts.

    FastMCP emits one content block per list element, so a single-element
    interaction list surfaces as a bare JSON object (not ``[...]``). Parse each
    block and merge any JSON arrays with the standalone objects.
    """

    summaries: list = []
    for item in items:
        text = getattr(item, "text", None)
        if text is None:
            continue
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, list):
            summaries.extend(payload)
        elif isinstance(payload, dict):
            summaries.append(payload)
    return summaries


async def test_unknown_host_key_round_trip_over_stdio(stack, tmp_path):
    """A real host-key confirmation surfaces through MCP and is decided in the
    trusted broker, never by the model.
    """

    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    mounted = _mount_unknown_host_config(stack, tmp_path)
    assert mounted.is_file()

    stack.start_password_auto_answer()

    async with stdio_client(_server_parameters(stack.server.socket_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            result = await session.call_tool(
                "open_session", {"connection_id": str(stack.connection.id)}
            )
            assert not result.is_error, result.content[0].text
            session_id = _json_id(result.content[0].text, label="session")

            # The ssh child blocks on host-key confirmation; the daemon's
            # askpass broker must surface the interaction, scoped to the
            # session that is being opened.
            async def _pending_interactions():
                listed = await session.call_tool("list_interactions", {})
                assert not listed.is_error, listed.content[0].text
                return _collect_lists(listed.content)

            host_key_shown = False
            interaction_id = None
            deadline = time.monotonic() + 60.0
            while time.monotonic() < deadline:
                items = await _pending_interactions()
                if any(item.get("type") == "host_key_confirmation" for item in items):
                    host_key_shown = True
                    interaction_id = next(
                        item["id"] for item in items if item.get("id")
                    )
                    break
                await anyio.sleep(0.05)
            assert host_key_shown, "no host_key_confirmation interaction appeared over MCP"
            assert interaction_id is not None

            result = await session.call_tool(
                "get_interaction", {"interaction_id": interaction_id}
            )
            assert not result.is_error, result.content[0].text
            detail = result.content[0].text
            assert "host_key_confirmation" in detail
            assert session_id in detail, "interaction is not scoped to the opening session"

            result = await session.call_tool(
                "claim_interaction", {"interaction_id": interaction_id}
            )
            assert not result.is_error, result.content[0].text
            claim_text = result.content[0].text
            assert interaction_id in claim_text
            assert "responder_client_id" in claim_text
            assert "<redacted>" in claim_text, "claim nonce must not reach model context"

            result = await session.call_tool(
                "release_interaction", {"interaction_id": interaction_id}
            )
            assert not result.is_error, result.content[0].text
            assert "released" in result.content[0].text

            # The trusted frontend (test harness, standing in for the
            # human-facing client) must be attached to the session before it
            # can accept the host key — eligibility follows session ownership,
            # which the MCP-originated open assigned to the MCP client.
            from sshpilot.api.models.sessions import AttachSessionRequest

            trusted = stack.connect_client()
            try:
                trusted.attach_session(
                    AttachSessionRequest(
                        session_id=session_id,
                        request_input=False,
                    )
                )
            except Exception:
                trusted.close()
                raise

            answered = stack.answer_pending_host_keys(accept=True)
            assert answered > 0, "trusted broker accepted no host keys"
            trusted.close()

            watching = stack.connect_client()
            try:
                wait_until(
                    lambda: any(
                        getattr(s, "state", "").name == "RUNNING"
                        for s in watching.list_sessions()
                    ),
                    timeout=60.0,
                    message="session did not reach RUNNING after host-key accept",
                )
            finally:
                watching.close()

            result = await session.call_tool("close_session", {"session_id": session_id})
            assert not result.is_error, result.content[0].text
"""FIDO security-key authentication over the runtime MCP server.

Uses OpenSSH's installed ``sk-dummy.so`` provider so the test exercises the
real security-key authentication and daemon path without a physical key.
The dummy provider completes presence internally, so it does not emit the
hardware ``security_key_presence`` askpass notification.
"""

from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path

import pytest

from tests.daemon.phase10_helpers import require_phase10_container, wait_until
from tests.mcp.test_runtime_hostkey_stdio import (
    _json_id,
    _server_parameters,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

SK_DUMMY = Path("/usr/lib/openssh/regress/misc/sk-dummy/sk-dummy.so")


@pytest.fixture(scope="module")
def phase10_env(tmp_path_factory):
    env = require_phase10_container(tmp_path_factory.mktemp("runtime-mcp-fido"))
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


def _require_dummy_provider() -> None:
    if not SK_DUMMY.is_file():
        message = f"OpenSSH dummy security-key provider is unavailable: {SK_DUMMY}"
        if os.environ.get("SSHPILOT_CANONICAL_TEST_ENV") == "1":
            pytest.fail(message)
        pytest.skip(message)


@pytest.fixture(scope="module", autouse=True)
def dummy_provider_available():
    _require_dummy_provider()


def _install_dummy_key(stack, tmp_path: Path, sk_type: str) -> Path:
    key_stem = sk_type.removesuffix("-sk")

    key_path = tmp_path / f"id_{key_stem}_sk"
    result = subprocess.run(
        (
            "ssh-keygen",
            "-q",
            "-t",
            sk_type,
            "-O",
            "verify-required",
            "-f",
            str(key_path),
            "-N",
            "",
            "-C",
            "sshpilot-fido-test",
        ),
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "SSH_SK_PROVIDER": str(SK_DUMMY),
        },
    )
    assert result.returncode == 0, result.stderr or result.stdout
    public_key = key_path.with_name(f"{key_path.name}.pub").read_text().strip()

    encoded = base64.b64encode(public_key.encode()).decode()
    # The fixture helper has no stdin channel; use the container runtime's
    # shell environment to pass the public key without shell interpolation.
    installed = stack.env.exec_in_container(
        "sh",
        "-c",
        f"printf '%s' {encoded} | base64 -d > /home/phase10/.ssh/authorized_keys && "
        "chown phase10:phase10 /home/phase10/.ssh/authorized_keys && "
        "chmod 600 /home/phase10/.ssh/authorized_keys",
        timeout=10,
    )
    assert installed.returncode == 0, installed.stderr or installed.stdout

    config = tmp_path / "fido_config"
    config.write_text(
        "\n".join(
            (
                f"Host {stack.env.host_alias}",
                "    HostName 127.0.0.1",
                f"    Port {stack.env.port}",
                f"    User {stack.env.username}",
                "    PreferredAuthentications publickey",
                "    PasswordAuthentication no",
                "    PubkeyAuthentication yes",
                "    IdentitiesOnly yes",
                f"    IdentityFile {key_path}",
                f"    SecurityKeyProvider {SK_DUMMY}",
                f"    UserKnownHostsFile {stack.env.known_hosts}",
                "    GlobalKnownHostsFile /dev/null",
                "    StrictHostKeyChecking yes",
                "    LogLevel ERROR",
            )
        )
        + "\n"
    )
    config.chmod(0o600)
    stack.connection.auth_method = 0
    stack.connection.keyfile = str(key_path)
    stack.connection.identity_files = [str(key_path)]
    stack.connection.config_root = str(config)
    stack.connection.source = str(config)
    stack.connection.data.update(
        {
            "auth_method": 0,
            "keyfile": str(key_path),
            "config_root": str(config),
            "security_key_provider": str(SK_DUMMY),
        }
    )
    return config


@pytest.mark.parametrize("sk_type", ("ed25519-sk", "ecdsa-sk"))
async def test_fido_auth_round_trip_over_stdio(stack, tmp_path, sk_type):
    """MCP opens a session through real OpenSSH SK-provider authentication."""

    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    config = _install_dummy_key(stack, tmp_path, sk_type)
    assert config.is_file()

    async with stdio_client(_server_parameters(stack.server.socket_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "open_session", {"connection_id": str(stack.connection.id)}
            )
            assert not result.is_error, result.content[0].text
            session_id = _json_id(result.content[0].text, label="session")

            watching = stack.connect_client()
            try:
                wait_until(
                    lambda: any(
                        getattr(item, "state", "").name == "RUNNING"
                        for item in watching.list_sessions()
                    ),
                    timeout=60.0,
                    message="session did not reach RUNNING after dummy FIDO auth",
                )
            finally:
                watching.close()

            result = await session.call_tool("close_session", {"session_id": session_id})
            assert not result.is_error, result.content[0].text

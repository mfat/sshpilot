"""Normal OpenSSH/libfido2 FIDO authentication through a virtual HID device.

Unlike :mod:`test_runtime_fido_stdio`, this test deliberately does not set
``SSH_SK_PROVIDER`` or ``SecurityKeyProvider``.  The authenticator executable
is supplied by the test environment through ``SSHPILOT_PANDO_SOFT_FIDO2``.
"""

from __future__ import annotations

import base64
import os
import shutil
import signal
import subprocess
from pathlib import Path

import pytest

from tests.daemon.integration_environment import skip_or_fail
from tests.daemon.phase10_helpers import require_phase10_container, wait_until
from tests.mcp.test_runtime_hostkey_stdio import _json_id, _server_parameters

pytestmark = [pytest.mark.anyio, pytest.mark.integration, pytest.mark.virtual_fido]

PANDO_BINARY_ENV = "SSHPILOT_PANDO_SOFT_FIDO2"
VIRTUAL_VENDOR = "vendor=0x1234"
VIRTUAL_PRODUCT = "product=0x5678"


@pytest.fixture(scope="module")
def phase10_env(tmp_path_factory):
    env = require_phase10_container(tmp_path_factory.mktemp("runtime-mcp-fido-hid"))
    try:
        yield env
    finally:
        env.destroy()


@pytest.fixture(autouse=True)
def no_dummy_provider(monkeypatch):
    """Keep the normal OpenSSH/libfido2 path selected for every subprocess."""

    monkeypatch.delenv("SSH_SK_PROVIDER", raising=False)


@pytest.fixture
def stack(tmp_path, phase10_env):
    from tests.daemon.phase10_helpers import start_phase10_stack

    started = start_phase10_stack(tmp_path, env=phase10_env)
    try:
        yield started
    finally:
        started.close(destroy_env=False)


@pytest.fixture
def virtual_authenticator(tmp_path: Path):
    binary_text = os.environ.get(PANDO_BINARY_ENV)
    if not binary_text:
        skip_or_fail(f"{PANDO_BINARY_ENV} is not configured")

    binary = Path(binary_text)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        skip_or_fail(f"pando85 authenticator is not executable: {binary}")

    fido2_token = shutil.which("fido2-token")
    if fido2_token is None:
        skip_or_fail("fido2-token is required for virtual HID FIDO coverage")

    before = {path.resolve() for path in Path("/dev").glob("hidraw*")}
    log_path = tmp_path / "pando-soft-fido2.log"
    log = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [str(binary)],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env={key: value for key, value in os.environ.items() if key != "SSH_SK_PROVIDER"},
    )

    try:
        def discovered() -> bool:
            result = subprocess.run(
                (fido2_token, "-L"),
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            return VIRTUAL_VENDOR in result.stdout and VIRTUAL_PRODUCT in result.stdout

        wait_until(
            discovered,
            timeout=30,
            interval=0.25,
            message="pando85 authenticator did not appear through fido2-token",
        )
        yield process
    finally:
        log.close()
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)

        def device_removed() -> bool:
            return not ({path.resolve() for path in Path("/dev").glob("hidraw*")} - before)

        wait_until(
            device_removed,
            timeout=10,
            interval=0.25,
            message="pando85 hidraw device remained after authenticator shutdown",
        )


def _install_virtual_key(stack, tmp_path: Path) -> Path:
    key_path = tmp_path / "id_ecdsa_sk"
    clean_env = {key: value for key, value in os.environ.items() if key != "SSH_SK_PROVIDER"}
    result = subprocess.run(
        (
            "ssh-keygen",
            "-q",
            "-t",
            "ecdsa-sk",
            "-f",
            str(key_path),
            "-N",
            "",
            "-C",
            "sshpilot-virtual-fido-test",
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env=clean_env,
    )
    assert result.returncode == 0, result.stderr or result.stdout

    public_key = key_path.with_name(f"{key_path.name}.pub").read_text().strip()
    encoded = base64.b64encode(public_key.encode()).decode()
    installed = stack.env.exec_in_container(
        "sh",
        "-c",
        f"printf '%s' {encoded} | base64 -d > /home/phase10/.ssh/authorized_keys && "
        "chown phase10:phase10 /home/phase10/.ssh/authorized_keys && "
        "chmod 600 /home/phase10/.ssh/authorized_keys",
        timeout=10,
    )
    assert installed.returncode == 0, installed.stderr or installed.stdout

    config = tmp_path / "fido_virtual_hid_config"
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
        }
    )
    return config


async def test_fido_auth_round_trip_over_virtual_hid(stack, tmp_path, virtual_authenticator):
    """MCP reaches RUNNING through OpenSSH's built-in libfido2 path."""

    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    config = _install_virtual_key(stack, tmp_path)
    assert config.is_file()
    assert "SecurityKeyProvider" not in config.read_text()
    assert "SSH_SK_PROVIDER" not in os.environ

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
                    message="session did not reach RUNNING after virtual HID FIDO auth",
                )
            finally:
                watching.close()

            result = await session.call_tool("close_session", {"session_id": session_id})
            assert not result.is_error, result.content[0].text

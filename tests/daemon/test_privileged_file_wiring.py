"""Production-wiring tests for daemon-owned privileged file operations.

These tests drive the real ``DaemonServer`` startup path with a launch
provider, so an injected editor double cannot mask a wiring regression: the
capability must actually be advertised after the handshake, the privileged
runner must be built with the live interaction broker, and the runner must
send the sudo command through the broker-prepared launch.
"""

from __future__ import annotations

import io

import pytest

from sshpilot.api import Capability, DaemonClient
from sshpilot.api.models.common import ConnectionId, SessionId
from sshpilot.core.connection_application_service import ConnectionApplicationService
from sshpilot.daemon import DaemonServer
from sshpilot.daemon.privileged_file_service import PrivilegedFileService

CONNECTION_ID = ConnectionId("demo")
SCOPE_ID = SessionId("sftp:demo")


class _LaunchProvider:
    def __init__(self):
        self.sftp_calls = []
        self.remote_calls = []

    def prepare_sftp_launch(self, connection_id, *, interaction_policy="broker"):
        self.sftp_calls.append((connection_id, interaction_policy))
        return ("ssh", "-s", "sftp", "--", str(connection_id)), {"PATH": "/usr/bin"}

    def prepare_remote_command_launch(self, connection_id, remote_command):
        self.remote_calls.append((str(connection_id), remote_command))
        return ("ssh", str(connection_id), remote_command), {"PATH": "/usr/bin"}


class _MinimalManager:
    def __init__(self):
        self._listeners = []

    def add_listener(self, callback):
        self._listeners.append(callback)

    def snapshot(self):
        return None


def _wired_server(tmp_path):
    provider = _LaunchProvider()
    socket_dir = tmp_path / "priv-wiring"
    socket_dir.mkdir(mode=0o700)
    server = DaemonServer(
        lambda: ConnectionApplicationService(
            _MinimalManager(),
            launch_provider=provider,
            client_name="sshpilotd",
        ),
        socket_path=socket_dir / "sshpilotd.sock",
    )
    server.start_in_thread()
    return server, provider


@pytest.fixture
def wired_server(tmp_path):
    server, provider = _wired_server(tmp_path)
    yield server, provider
    server.shutdown()


def test_handshake_advertises_privileged_file_capability(wired_server, tmp_path):
    server, _provider = wired_server
    client = DaemonClient(socket_path=server.socket_path)

    supported = client.get_capabilities().supported

    assert Capability.SFTP_PRIVILEGED_FILE in supported
    assert Capability.SFTP_READ in supported
    assert Capability.SFTP_MUTATE in supported


def test_privileged_runner_is_built_with_live_broker(wired_server):
    server, _provider = wired_server

    assert server._sftp_runtime.privileged_file_supported is True
    runner = server._sftp_runtime._privileged_file_runner
    assert isinstance(runner, PrivilegedFileService)
    assert runner._broker is server._interaction_broker
    assert server._interaction_broker is not None


def test_privileged_read_flows_through_broker_prepared_launch(wired_server):
    server, provider = wired_server
    runner = server._sftp_runtime._privileged_file_runner
    captured = {}

    class _FakePopen:
        def __init__(self, argv, **kwargs):
            captured["argv"] = list(argv)
            captured["env"] = dict(kwargs.get("env") or {})
            self.stdin = io.BytesIO()
            self._out = io.BytesIO(b"root-secret\n")
            self._err = io.BytesIO(b"")
            self.returncode = 0

        @property
        def stdout(self):
            return self._out

        @property
        def stderr(self):
            return self._err

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            return self.returncode

    runner._popen = _FakePopen
    result = runner.read(
        connection_id=CONNECTION_ID,
        scope_id=SCOPE_ID,
        hostname="example.test",
        username="alice",
        port=22,
        path="/etc/root-owned.conf",
    )

    assert result.content == b"root-secret\n"
    assert result.exists is True
    assert provider.remote_calls
    assert "sudo -n -- cat -- /etc/root-owned.conf" in " ".join(captured["argv"])
    assert captured["env"].get("SSH_ASKPASS")

"""Transport-level protected-input retirement regressions."""

from __future__ import annotations

from pathlib import Path
import time

from sshpilot.api.daemon_client import DaemonClient
from sshpilot.api.models.broadcast import BroadcastCommandRequest
from sshpilot.api.models.common import ConnectionId
from sshpilot.core.connection_application_service import ConnectionApplicationService
from sshpilot.daemon import DaemonServer
from tests.daemon.conftest import TestConnectionManager as _TestConnectionManager


class _LaunchProvider:
    def prepare_remote_command_launch(self, connection_id, remote_command, **_kwargs):
        return ("ssh", str(connection_id), remote_command), {}


class _Runner:
    seen_input = []

    def run(self, _argv, _environment, _policy, **kwargs):
        value = kwargs.get("input_data")
        self.seen_input.append(bytes(value) if value is not None else None)
        return 0, "", "", False, False


def test_real_broadcast_client_transport_delivers_protected_stdin(
    tmp_path: Path, monkeypatch
):
    from sshpilot.daemon import broadcast_service

    runner = _Runner()
    monkeypatch.setattr(broadcast_service, "NativeSshCommandRunner", lambda: runner)
    manager = _TestConnectionManager()
    service = ConnectionApplicationService(
        manager,
        launch_provider=_LaunchProvider(),
        secret_provider=manager,
        client_name="test-daemon",
    )
    server = DaemonServer(
        lambda: service,
        socket_path=tmp_path / "daemon" / "sshpilotd.sock",
    )
    server.start_in_thread()
    client = DaemonClient(socket_path=server.socket_path, timeout=3)
    try:
        summary = client.start_broadcast_command(
            BroadcastCommandRequest(
                connection_ids=(ConnectionId("demo"),),
                command="cat",
            ),
            input="protected-broadcast-input",
        )
        assert summary.targets[0].connection_id == ConnectionId("demo")
        for _ in range(100):
            current = client.get_broadcast_command(summary.operation.operation_id)
            if current.targets[0].state.value in {"succeeded", "failed"}:
                break
            time.sleep(0.01)
        assert _Runner.seen_input == [b"protected-broadcast-input"]
    finally:
        client.close()
        server.shutdown()
        server.wait_stopped()

from __future__ import annotations

from pathlib import Path

from sshpilot.cli.main import run
from sshpilot.daemon import DaemonServer
from tests.helpers.fake_connection_repository import make_test_connection_service


class _LaunchProvider:
    def prepare_remote_command_launch(self, connection_id, command, *, interaction_policy):
        assert interaction_policy == "broker"
        return ("ssh", str(connection_id), command), {}


class _Runner:
    def run(self, _argv, _environment, _policy, *, cancel_event, on_process, on_output):
        assert not cancel_event.is_set()
        on_process(None)
        on_output("stdout", "daemon-cli-output\n")
        return 0, "daemon-cli-output\n", "", False, False


def test_reference_cli_exec_uses_real_daemon_broadcast_path(tmp_path, capsys):
    server = DaemonServer(
        lambda: make_test_connection_service(launch_provider=_LaunchProvider()),
        socket_path=Path(tmp_path) / "sshpilotd.sock",
    )
    server.start_in_thread()
    service = server._broadcast_service
    assert service is not None
    service._runner = _Runner()

    exit_code = run(
        ["--socket", str(server.socket_path), "exec", "demo", "--", "printf", "ok"]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == "daemon-cli-output\n"
    assert not captured.err
    server.shutdown()
    assert server.wait_stopped()

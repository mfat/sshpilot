"""CLI management commands against a running test daemon."""

from __future__ import annotations

import json
import subprocess
import sys

from sshpilot.daemon.cli import main


def test_cli_status_against_daemon_factory(daemon_factory, capsys):
    server, _manager = daemon_factory(idle_shutdown_seconds=0.0)
    exit_code = main(
        [
            "--socket",
            str(server.socket_path),
            "status",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["state"] == "ready"
    assert payload["server_instance_id"]
    server.shutdown()
    assert server.wait_stopped()


def test_cli_diagnostics_against_daemon_factory(daemon_factory, capsys):
    server, _manager = daemon_factory(idle_shutdown_seconds=0.0)
    exit_code = main(
        [
            "--socket",
            str(server.socket_path),
            "diagnostics",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["socket_bound"] is True
    assert payload["status"]["state"] == "ready"
    server.shutdown()
    assert server.wait_stopped()


def test_cli_stop_shuts_down_daemon(daemon_factory, capsys):
    server, _manager = daemon_factory(idle_shutdown_seconds=0.0)
    exit_code = main(
        [
            "--socket",
            str(server.socket_path),
            "stop",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["accepted"] is True
    assert server.wait_stopped(timeout=5.0)
    assert not captured.err.strip()


def test_cli_missing_daemon_returns_unavailable(tmp_path, capsys):
    missing = tmp_path / "missing.sock"
    exit_code = main(["--socket", str(missing), "status"])
    captured = capsys.readouterr()
    assert exit_code == 2
    payload = json.loads(captured.err)
    assert payload["available"] is False


def test_launcher_script_invokes_module():
    result = subprocess.run(
        [sys.executable, "sshpilot-daemon", "--help"],
        cwd=str(__import__("pathlib").Path(__file__).resolve().parents[2]),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "status" in result.stdout


def test_run_script_dispatches_daemon_before_gtk():
    result = subprocess.run(
        [sys.executable, "run.py", "--daemon", "--help"],
        cwd=str(__import__("pathlib").Path(__file__).resolve().parents[2]),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "Run or manage the local sshPilot daemon" in result.stdout


def test_cli_server_start_uses_environment_idle_default_when_unset(monkeypatch, tmp_path):
    """Absent ``daemon.idle_shutdown_seconds`` must not disable idle shutdown."""

    captured = {}

    class _FakeConfig:
        def get_setting(self, key, default=None):
            return default

    class _FakeServer:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            self._startup_error = None

        def serve_forever(self):
            return None

        def shutdown(self):
            return None

    monkeypatch.setattr("sshpilot.config.Config", _FakeConfig)
    monkeypatch.setattr("sshpilot.daemon.server.DaemonServer", _FakeServer)
    monkeypatch.setattr(
        "sshpilot.daemon.cli._production_core_services",
        lambda: None,
    )
    monkeypatch.setattr(
        "sshpilot.daemon.cli.run_server",
        lambda **kwargs: kwargs["serve_forever"]() or 0,
    )

    from sshpilot.daemon.lifecycle_policy import _IDLE_SHUTDOWN_UNSET

    exit_code = main(["--socket", str(tmp_path / "sshpilotd.sock")])
    assert exit_code == 0
    assert captured["idle_shutdown_seconds"] is _IDLE_SHUTDOWN_UNSET

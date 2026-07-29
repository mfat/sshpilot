"""Daemon section of the diagnostics ZIP when a daemon is reachable."""

from __future__ import annotations

import json
import zipfile

from sshpilot import log_viewer
from sshpilot import platform_utils


def test_build_diagnostics_zip_includes_daemon_when_running(
    tmp_path, monkeypatch, daemon_factory,
):
    state = tmp_path / "state"
    config = tmp_path / "config"
    state.mkdir()
    config.mkdir()
    (state / "sshpilot.log").write_text("log\n")
    (config / "config.json").write_text("{}")

    server, _manager = daemon_factory(
        idle_shutdown_seconds=0.0,
        socket_path=tmp_path / "sshpilotd.sock",
    )
    monkeypatch.setattr(log_viewer, "get_state_dir", lambda: str(state))
    monkeypatch.setattr(platform_utils, "get_config_dir", lambda: str(config))
    monkeypatch.setattr(
        "sshpilot.daemon.lifecycle.resolve_socket_path",
        lambda _override=None: server.socket_path,
    )

    dest = tmp_path / "diag.zip"
    log_viewer.build_diagnostics_zip(str(dest))

    with zipfile.ZipFile(dest) as zf:
        daemon_doc = json.loads(zf.read("daemon-diagnostics.json"))
        assert daemon_doc["available"] is True
        assert daemon_doc["diagnostics"]["status"]["state"] == "ready"

    server.shutdown()
    assert server.wait_stopped()

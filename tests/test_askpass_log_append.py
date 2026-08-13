"""Shared askpass log path used by classic helper and daemon broker."""

from __future__ import annotations

from sshpilot import askpass_utils


def test_append_askpass_log_writes_shared_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SSHPILOT_ASKPASS_LOG_DIR", str(tmp_path))
    askpass_utils._ASKPASS_LOG_PATH = None

    askpass_utils.append_askpass_log("ASKPASS: unit-test line")

    path = askpass_utils.get_askpass_log_path()
    assert path == str(tmp_path / "sshpilot-askpass.log")
    assert "ASKPASS: unit-test line" in (tmp_path / "sshpilot-askpass.log").read_text(
        encoding="utf-8"
    )


def test_append_askpass_log_swallows_io_errors(monkeypatch):
    monkeypatch.setattr(
        askpass_utils,
        "get_askpass_log_path",
        lambda: "/no/such/dir/sshpilot-askpass.log",
    )
    askpass_utils.append_askpass_log("ASKPASS: should not raise")

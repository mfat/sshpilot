"""Tests for sshpilot/rbw_setup.py — controller-only (no direct rbw execution)."""

from sshpilot import rbw_setup as rs


class FakeController:
    """Minimal daemon-backed controller shim for status probing."""

    def __init__(self, *, installed=True, configured=True, unlocked=False,
                 email="alice@example.com", base_url=""):
        self._status = {
            "installed": installed,
            "configured": configured,
            "unlocked": unlocked,
            "email": email,
            "base_url": base_url,
        }

    def rbw_status(self):
        return type("RbwStatus", (), dict(self._status))()


def test_probe_not_installed():
    s = rs.probe_rbw_status()
    assert s.cli_installed is False
    assert s.is_ready is False


def test_probe_no_controller_assumes_installed_unknown():
    """Without a controller, the presentation cannot confirm configuration — it
    reports installed-but-unconfigured so the UI offers setup rather than a false
    'ready'."""


def test_probe_installed_but_unconfigured():
    s = rs.probe_rbw_status(FakeController(configured=False, unlocked=False))
    assert s.cli_installed is True
    assert s.configured is False
    assert s.unlocked is False
    assert s.is_ready is False


def test_probe_configured_but_locked():
    s = rs.probe_rbw_status(FakeController(configured=True, unlocked=False))
    assert s.cli_installed is True
    assert s.configured is True
    assert s.unlocked is False
    assert s.is_ready is False


def test_probe_ready():
    s = rs.probe_rbw_status(FakeController(configured=True, unlocked=True))
    assert s.is_ready is True
    assert s.email == "alice@example.com"


def test_probe_controller_error_reports_not_installed():
    class Broken:
        def rbw_status(self):
            raise RuntimeError("daemon down")

    s = rs.probe_rbw_status(Broken())
    assert s.cli_installed is False
    assert s.is_ready is False

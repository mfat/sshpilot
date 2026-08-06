"""Tests for sshpilot/rbw_setup.py — controller-only (no direct rbw execution)."""

from sshpilot import bitwarden_setup as bs
from sshpilot import rbw_setup as rs
from sshpilot.api.models.secrets import RbwStatus


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


def _status(*, configured=True, unlocked=False, email="alice@example.com", message=""):
    return RbwStatus(
        installed=True,
        configured=configured,
        unlocked=unlocked,
        email=email,
        base_url="",
        message=message,
    )


def _run_immediately(monkeypatch):
    monkeypatch.setattr(rs, "_thread", lambda target: target())
    monkeypatch.setattr(rs.GLib, "idle_add", lambda callback: callback())


def test_apply_config_typed_failure_does_not_continue_to_unlock_or_sync(monkeypatch):
    calls = []
    completed = []

    class Controller:
        def rbw_configure(self, email, server):
            calls.append(("configure", email, server))
            return _status(configured=False, email="", message="rbw configuration failed")

        def rbw_unlock(self):
            calls.append(("unlock",))

        def rbw_sync(self):
            calls.append(("sync",))

    monkeypatch.setattr(bs, "progress_dialog", lambda *args: (None, lambda: calls.append(("close",))))
    monkeypatch.setattr(rs, "_login_async", lambda *args: calls.append(("login",)))
    _run_immediately(monkeypatch)

    def continue_setup(ok):
        completed.append(ok)
        if ok:
            rs._login_async(object(), Controller(), completed.append)

    rs._apply_config_async(object(), Controller(), "alice@example.com", "", continue_setup)

    assert completed == [False]
    assert calls == [("configure", "alice@example.com", ""), ("close",)]


def test_apply_config_successful_typed_result_continues_setup(monkeypatch):
    completed = []

    class Controller:
        def rbw_configure(self, email, server):
            assert (email, server) == ("alice@example.com", "https://vault.example.com")
            return _status(email=email)

    monkeypatch.setattr(bs, "progress_dialog", lambda *args: (None, lambda: None))
    _run_immediately(monkeypatch)

    rs._apply_config_async(
        object(), Controller(), "alice@example.com", "https://vault.example.com", completed.append
    )

    assert completed == [True]


def test_login_typed_unlock_failure_skips_sync(monkeypatch):
    calls = []
    completed = []

    class Controller:
        def rbw_unlock(self):
            calls.append("unlock")
            return _status(unlocked=False, message="rbw unlock failed")

        def rbw_sync(self):
            calls.append("sync")
            return _status(unlocked=True)

        def rbw_status(self):
            return _status(unlocked=False)

    monkeypatch.setattr(bs, "progress_dialog", lambda *args: (None, lambda: calls.append("close")))
    monkeypatch.setattr(rs, "_error_dialog", lambda _window, _detail, on_done: on_done(False))
    monkeypatch.setattr(rs, "_ready_dialog", lambda _window, _status, on_done: on_done(True))
    _run_immediately(monkeypatch)

    rs._login_async(object(), Controller(), completed.append)

    assert calls == ["unlock", "close"]
    assert completed == [False]


def test_login_typed_sync_failure_does_not_show_ready(monkeypatch):
    calls = []
    completed = []

    class Controller:
        def rbw_unlock(self):
            calls.append("unlock")
            return _status(unlocked=True)

        def rbw_sync(self):
            calls.append("sync")
            return _status(unlocked=True, message="rbw sync failed")

        def rbw_status(self):
            return _status(unlocked=True)

    monkeypatch.setattr(bs, "progress_dialog", lambda *args: (None, lambda: calls.append("close")))
    monkeypatch.setattr(rs, "_error_dialog", lambda _window, _detail, on_done: on_done(False))
    monkeypatch.setattr(rs, "_ready_dialog", lambda _window, _status, on_done: on_done(True))
    _run_immediately(monkeypatch)

    rs._login_async(object(), Controller(), completed.append)

    assert calls == ["unlock", "sync", "close"]
    assert completed == [False]


def test_login_successful_typed_results_show_ready(monkeypatch):
    calls = []
    completed = []

    class Controller:
        def rbw_unlock(self):
            calls.append("unlock")
            return _status(unlocked=True)

        def rbw_sync(self):
            calls.append("sync")
            return _status(unlocked=True)

        def rbw_status(self):
            return _status(unlocked=True)

    monkeypatch.setattr(bs, "progress_dialog", lambda *args: (None, lambda: calls.append("close")))
    monkeypatch.setattr(rs, "_error_dialog", lambda _window, _detail, on_done: on_done(False))
    monkeypatch.setattr(rs, "_ready_dialog", lambda _window, _status, on_done: on_done(True))
    _run_immediately(monkeypatch)

    rs._login_async(object(), Controller(), completed.append)

    assert calls == ["unlock", "sync", "close"]
    assert completed == [True]


def test_probe_not_installed():
    s = rs.probe_rbw_status()
    assert s.cli_installed is False
    assert s.is_ready is False


def test_probe_no_controller_reports_unavailable():
    """Without a controller, the presentation cannot confirm configuration."""
    status = rs.probe_rbw_status()
    assert status.cli_installed is False
    assert status.configured is False
    assert status.unlocked is False
    assert status.email == ""
    assert status.is_ready is False


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

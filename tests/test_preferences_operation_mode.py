"""Operation-mode (isolated SSH config) toggle regression tests.

The legacy in-process ``ConnectionManager.set_isolated_mode`` was retired;
the daemon resolves the SSH config root from ``ssh.use_isolated_config`` only
when the daemon itself launches. The toggle must persist the setting and, on
confirming a restart, restart the daemon (so it re-reads the setting) before
restarting the app — an app-only ``os.execv`` leaves the old daemon running.
"""

from types import SimpleNamespace

import sshpilot.api.daemon_client as daemon_client_mod
import sshpilot.platform_utils as platform_utils
from sshpilot.preferences import PreferencesWindow


class _Radio:
    def __init__(self, active):
        self._active = active

    def get_active(self):
        return self._active


class _Button:
    def get_active(self):
        return True


class _Row:
    def remove_css_class(self, cls):
        pass


def _make_prefs():
    recorded = {}
    config = SimpleNamespace(
        set_setting=lambda key, value: recorded.__setitem__(key, value),
    )
    prefs = PreferencesWindow.__new__(PreferencesWindow)
    prefs.config = config
    prefs.isolated_mode_radio = _Radio(active=True)
    prefs.default_mode_row = _Row()
    prefs.isolated_mode_row = _Row()
    prefs.restart_bodies = []
    prefs._prompt_operation_mode_restart = lambda body: prefs.restart_bodies.append(body)
    prefs._refresh_daemon_status_row = lambda: None
    return prefs, recorded


def test_operation_mode_toggle_persists_setting_and_requests_restart():
    prefs, recorded = _make_prefs()

    PreferencesWindow.on_operation_mode_toggled(prefs, _Button())

    assert recorded.get("ssh.use_isolated_config") is True
    assert prefs.restart_bodies == [
        "Restart SSH Pilot to fully apply the new operation mode."
    ]


def test_operation_mode_toggle_does_not_require_legacy_manager_mutation():
    prefs, recorded = _make_prefs()
    # The read-only presentation store has no set_isolated_mode; the toggle
    # must not reach for the retired legacy manager API.
    prefs.parent_window = SimpleNamespace(
        connection_manager=SimpleNamespace(),
    )

    PreferencesWindow.on_operation_mode_toggled(prefs, _Button())

    assert recorded.get("ssh.use_isolated_config") is True
    assert prefs.restart_bodies


def test_daemon_restart_after_operation_mode_change_restarts_app(monkeypatch):
    calls = []
    monkeypatch.setattr(platform_utils, "restart_app", lambda: calls.append("app"))
    prefs, _ = _make_prefs()

    prefs._restart_app_after_mode_change()

    assert calls == ["app"]


class _FakeDaemonClient:
    def __init__(self, restart_result):
        self._restart_result = restart_result
        self.status_checked = False
        self.restart_calls = []
        self.closed = False

    def get_daemon_status(self):
        self.status_checked = True

    def restart_daemon(self, request):
        self.restart_calls.append(request)
        return self._restart_result

    def close(self):
        self.closed = True


def test_operation_mode_restart_restarts_daemon_then_app(monkeypatch):
    app_restarts = []
    monkeypatch.setattr(platform_utils, "restart_app", lambda: app_restarts.append("app"))
    fake = _FakeDaemonClient(
        SimpleNamespace(accepted=True, confirmation=None, will_lose=())
    )
    monkeypatch.setattr(daemon_client_mod, "DaemonClient", lambda *a, **kw: fake)
    prefs, _ = _make_prefs()

    prefs._request_daemon_restart(on_complete=prefs._restart_app_after_mode_change)

    assert fake.status_checked is True
    assert len(fake.restart_calls) == 1
    assert fake.restart_calls[0].force is False
    assert fake.closed is True
    assert app_restarts == ["app"]


def test_operation_mode_restart_requires_confirmation_for_live_resources(
    monkeypatch,
):
    # With live resources and no force token, the daemon restart must be held
    # for confirmation; the app must not restart until the user forces it.
    app_restarts = []
    monkeypatch.setattr(platform_utils, "restart_app", lambda: app_restarts.append("app"))
    fake = _FakeDaemonClient(
        SimpleNamespace(accepted=False, confirmation="token", will_lose=("session",))
    )
    monkeypatch.setattr(daemon_client_mod, "DaemonClient", lambda *a, **kw: fake)

    dialog = _DialogStub()
    monkeypatch.setattr("sshpilot.preferences.Adw.AlertDialog", lambda *a, **kw: dialog)
    prefs, _ = _make_prefs()

    prefs._request_daemon_restart(on_complete=prefs._restart_app_after_mode_change)

    assert len(fake.restart_calls) == 1
    assert fake.restart_calls[0].force is False
    assert fake.closed is True
    assert dialog.presented is True
    assert app_restarts == []


class _DialogStub:
    def __init__(self):
        self.presented = False

    def add_response(self, *a):
        pass

    def set_response_appearance(self, *a):
        pass

    def set_default_response(self, *a):
        pass

    def set_close_response(self, *a):
        pass

    def connect(self, *a):
        pass

    def present(self, *a):
        self.presented = True


class _AlertDialogStub:
    """Adw.AlertDialog stand-in that records and lets tests emit responses."""

    def __init__(self):
        self.presented = False
        self._response_handlers = []

    def add_response(self, *a):
        pass

    def set_response_appearance(self, *a):
        pass

    def set_default_response(self, *a):
        pass

    def set_close_response(self, *a):
        pass

    def connect(self, signal, handler, *args):
        if signal == 'response':
            self._response_handlers.append((handler, args))

    def present(self, *a):
        self.presented = True

    def emit_response(self, response, *args):
        for handler, user_args in self._response_handlers:
            handler(self, response, *(args + user_args))


def _alert_dialog_factory(instances):
    def _make(*a, **kw):
        dialog = _AlertDialogStub()
        instances.append(dialog)
        return dialog

    return _make


def _forced_aware_restart(fake, forced):
    """Two-phase restart fake: non-force probes return the first result,
    forced calls return `forced`."""
    first = fake._restart_result

    def _restart(request):
        fake.restart_calls.append(request)
        if request.force:
            return forced
        return first

    return _restart


def _request_restart_via_force_dialog(monkeypatch, fake, forced=None):
    """Run ``_request_daemon_restart`` with a live-resources daemon and return
    the confirmation dialog plus any recorded completions."""
    if forced is not None:
        fake.restart_daemon = _forced_aware_restart(fake, forced)
    completions = []
    monkeypatch.setattr(daemon_client_mod, "DaemonClient", lambda *a, **kw: fake)
    dialogs = []
    monkeypatch.setattr(
        "sshpilot.preferences.Adw.AlertDialog", _alert_dialog_factory(dialogs)
    )
    prefs, _ = _make_prefs()

    prefs._request_daemon_restart(on_complete=lambda: completions.append("done"))

    assert dialogs, "expected the live-resources confirmation dialog"
    return dialogs, completions, fake


def test_forced_restart_accepted_runs_completion_once(monkeypatch):
    fake = _FakeDaemonClient(
        SimpleNamespace(accepted=False, confirmation="token", will_lose=("session",))
    )
    dialogs, completions, fake = _request_restart_via_force_dialog(
        monkeypatch,
        fake,
        forced=SimpleNamespace(
            accepted=True, confirmation=None, will_lose=(), message=""
        ),
    )

    dialogs[0].emit_response('force')

    assert completions == ["done"]
    assert len(fake.restart_calls) == 2
    assert fake.restart_calls[1].force is True
    assert fake.closed is True
    assert dialogs[0].presented is True


def test_forced_restart_exception_does_not_run_completion(monkeypatch):
    fake = _FakeDaemonClient(
        SimpleNamespace(accepted=False, confirmation="token", will_lose=("session",))
    )

    def _restart(request):
        fake.restart_calls.append(request)
        if request.force:
            raise RuntimeError("forced restart rpc failed")
        return SimpleNamespace(
            accepted=False, confirmation="token", will_lose=("session",)
        )

    fake.restart_daemon = _restart
    dialogs, completions, fake = _request_restart_via_force_dialog(monkeypatch, fake)

    dialogs[0].emit_response('force')

    assert completions == []
    assert len(fake.restart_calls) == 2
    assert fake.restart_calls[1].force is True
    assert fake.closed is True
    # The safe error dialog must be shown, and the confirmation dialog closed.
    assert len(dialogs) == 2
    assert dialogs[-1].presented is True


def test_forced_restart_rejected_does_not_run_completion(monkeypatch):
    fake = _FakeDaemonClient(
        SimpleNamespace(accepted=False, confirmation="token", will_lose=("session",))
    )
    dialogs, completions, fake = _request_restart_via_force_dialog(
        monkeypatch,
        fake,
        forced=SimpleNamespace(
            accepted=False,
            confirmation=None,
            will_lose=(),
            message="Restart refused",
        ),
    )

    dialogs[0].emit_response('force')

    assert completions == []
    assert len(fake.restart_calls) == 2
    assert fake.restart_calls[1].force is True
    assert fake.closed is True
    assert len(dialogs) == 2
    assert dialogs[-1].presented is True


def test_forced_restart_cancelled_skips_forced_rpc_and_completion(monkeypatch):
    fake = _FakeDaemonClient(
        SimpleNamespace(accepted=False, confirmation="token", will_lose=("session",))
    )
    dialogs, completions, fake = _request_restart_via_force_dialog(monkeypatch, fake)

    dialogs[0].emit_response('cancel')

    assert completions == []
    # Only the initial non-force probe ran; no forced restart RPC.
    assert len(fake.restart_calls) == 1
    assert fake.restart_calls[0].force is False
    assert fake.closed is True
    assert len(dialogs) == 1
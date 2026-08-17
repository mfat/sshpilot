"""Operation-mode preference tests use the typed daemon API."""

from types import SimpleNamespace

import sshpilot.api.daemon_client as daemon_client_mod
from sshpilot.api.models.daemon import OperationMode
from sshpilot.preferences import PreferencesWindow


class _Radio:
    def __init__(self, active):
        self._active = active

    def get_active(self):
        return self._active

    def set_active(self, active):
        self._active = bool(active)

    def set_sensitive(self, _sensitive):
        pass


class _CallbackRadio(_Radio):
    def __init__(self, active, callback):
        super().__init__(active)
        self._callback = callback

    def set_active(self, active):
        changed = self._active != bool(active)
        super().set_active(active)
        if changed:
            self._callback(self)


class _Button:
    def get_active(self):
        return True


class _Row:
    def remove_css_class(self, cls):
        pass


class _Bridge:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def submit(self, operation, *, on_success, on_error):
        try:
            self.requests.append(operation)
            on_success(operation())
        except Exception as error:
            on_error(error)


class _ModeClient:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def set_operation_mode(self, request):
        self.requests.append(request)
        return self.result

    def get_operation_mode(self):
        return self.result

    def get_capabilities(self):
        return SimpleNamespace(supports=lambda _capability: True)


def _make_prefs(result=None):
    if result is None:
        result = SimpleNamespace(
            accepted=True, active_mode=OperationMode.DEFAULT, message=""
        )
    recorded = {}
    config = SimpleNamespace(
        set_setting=lambda key, value: recorded.__setitem__(key, value),
    )
    prefs = PreferencesWindow.__new__(PreferencesWindow)
    prefs.config = config
    prefs.isolated_mode_radio = _Radio(active=True)
    prefs.default_mode_radio = _Radio(active=False)
    prefs.default_mode_row = _Row()
    prefs.isolated_mode_row = _Row()
    prefs.parent_window = SimpleNamespace()
    prefs.client = _ModeClient(result)
    prefs.client_bridge = _Bridge(result)
    prefs.parent_window.client = prefs.client
    prefs.parent_window.client_bridge = prefs.client_bridge
    prefs._refresh_daemon_status_row = lambda: None
    prefs._suppress_operation_mode_toggle = False
    prefs._operation_mode_request_in_flight = False
    prefs._confirmed_operation_mode = OperationMode.DEFAULT
    return prefs, recorded


def test_reconnect_reset_is_safe_before_operation_mode_page_is_built():
    """Preloaded Preferences may not have operation-mode radio widgets yet."""
    prefs = PreferencesWindow.__new__(PreferencesWindow)
    prefs._confirmed_operation_mode = OperationMode.ISOLATED
    prefs._operation_mode_request_in_flight = True

    prefs.reset_operation_mode_confirmation()

    assert prefs._confirmed_operation_mode is None
    assert prefs._operation_mode_request_in_flight is False


def test_operation_mode_toggle_uses_daemon_and_does_not_write_local_mode():
    result = SimpleNamespace(accepted=True, active_mode=OperationMode.ISOLATED, message="")
    prefs, recorded = _make_prefs(result)

    PreferencesWindow.on_operation_mode_toggled(prefs, _Button())

    assert recorded == {}
    assert prefs.client.requests[0].mode is OperationMode.ISOLATED
    assert prefs.isolated_mode_radio.get_active() is True


def test_operation_mode_failure_restores_frontend_radio_state():
    result = SimpleNamespace(
        accepted=False,
        active_mode=OperationMode.DEFAULT,
        message="sessions are active",
    )
    prefs, recorded = _make_prefs(result)

    PreferencesWindow.on_operation_mode_toggled(prefs, _Button())

    assert recorded == {}
    assert prefs.isolated_mode_radio.get_active() is False
    assert prefs.default_mode_radio.get_active() is True


def test_confirmed_initial_projection_does_not_mutate_mode():
    prefs, _recorded = _make_prefs(
        SimpleNamespace(accepted=True, active_mode=OperationMode.ISOLATED, message="")
    )
    PreferencesWindow._request_confirmed_operation_mode(prefs)
    assert prefs.client.requests == []
    assert prefs.isolated_mode_radio.get_active() is True


def test_successful_toggle_applies_radio_state_without_a_second_request():
    prefs, _recorded = _make_prefs(
        SimpleNamespace(accepted=True, active_mode=OperationMode.ISOLATED, message="")
    )
    prefs.isolated_mode_radio = _CallbackRadio(False, prefs.on_operation_mode_toggled)
    prefs.default_mode_radio = _CallbackRadio(True, prefs.on_operation_mode_toggled)

    PreferencesWindow.on_operation_mode_toggled(prefs, _Button())

    assert len(prefs.client.requests) == 1
    assert prefs.isolated_mode_radio.get_active() is True


def test_rejected_toggle_restoration_does_not_send_compensating_request():
    prefs, _recorded = _make_prefs(
        SimpleNamespace(
            accepted=False,
            active_mode=OperationMode.DEFAULT,
            message="sessions are active",
        )
    )
    prefs.parent_window._show_operation_mode_rejection = lambda detail: setattr(
        prefs, "rejection_detail", detail
    )
    prefs.isolated_mode_radio = _CallbackRadio(False, prefs.on_operation_mode_toggled)
    prefs.default_mode_radio = _CallbackRadio(True, prefs.on_operation_mode_toggled)

    PreferencesWindow.on_operation_mode_toggled(prefs, _Button())

    assert len(prefs.client.requests) == 1
    assert prefs.default_mode_radio.get_active() is True
    assert prefs.rejection_detail == "sessions are active"


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


def test_conflicting_toggle_offers_restart_instead_of_bare_rejection(monkeypatch):
    """A live-session conflict must restore the "restart to apply" UX, not
    the daemon-rejection dialog."""
    result = SimpleNamespace(
        accepted=False,
        active_mode=OperationMode.DEFAULT,
        message="Operation mode cannot change while live daemon resources are active: sessions",
        conflict=True,
    )
    prefs, _recorded = _make_prefs(result)
    prefs.isolated_mode_radio = _CallbackRadio(True, prefs.on_operation_mode_toggled)
    prefs.default_mode_radio = _CallbackRadio(False, prefs.on_operation_mode_toggled)

    def _fail_on_bare_rejection(_detail):
        raise AssertionError("bare rejection dialog should not be shown")

    prefs.parent_window._show_operation_mode_rejection = _fail_on_bare_rejection

    fake = _FakeDaemonClient(
        SimpleNamespace(accepted=True, confirmation=None, will_lose=(), message="")
    )
    monkeypatch.setattr(daemon_client_mod, "DaemonClient", lambda *a, **kw: fake)

    reconnect_calls = []
    prefs._schedule_daemon_reconnect_after_restart = lambda: reconnect_calls.append("done")

    PreferencesWindow.on_operation_mode_toggled(prefs, _Button())

    assert fake.restart_calls, "expected the daemon restart flow to run"
    assert reconnect_calls == ["done"]
    assert prefs.parent_window._requested_operation_mode is OperationMode.ISOLATED


def test_conflicting_toggle_reenables_controls_when_restart_is_declined(monkeypatch):
    """Cancelling the restart-confirmation dialog must fully cancel the mode
    change (no restart, no deferred switch) and not leave the operation-mode
    controls stuck disabled."""
    result = SimpleNamespace(
        accepted=False,
        active_mode=OperationMode.DEFAULT,
        message="Operation mode cannot change while live daemon resources are active: sessions",
        conflict=True,
    )
    prefs, _recorded = _make_prefs(result)
    prefs.isolated_mode_radio = _CallbackRadio(True, prefs.on_operation_mode_toggled)
    prefs.default_mode_radio = _CallbackRadio(False, prefs.on_operation_mode_toggled)

    fake = _FakeDaemonClient(
        SimpleNamespace(accepted=False, confirmation="token", will_lose=("sessions",))
    )
    monkeypatch.setattr(daemon_client_mod, "DaemonClient", lambda *a, **kw: fake)
    dialogs = []
    monkeypatch.setattr(
        "sshpilot.preferences.Adw.AlertDialog", _alert_dialog_factory(dialogs)
    )

    sensitivity_calls = []
    prefs._set_operation_mode_controls_sensitive = sensitivity_calls.append

    reconnect_calls = []
    prefs._schedule_daemon_reconnect_after_restart = lambda: reconnect_calls.append("done")

    PreferencesWindow.on_operation_mode_toggled(prefs, _Button())

    assert dialogs, "expected the live-resources confirmation dialog"
    dialogs[0].emit_response('cancel')

    assert reconnect_calls == []
    assert not hasattr(prefs.parent_window, "_requested_operation_mode")
    assert fake.restart_calls and fake.restart_calls[0].force is False
    assert sensitivity_calls[-1] is True


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

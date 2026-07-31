"""GTK daemon reconnect after forced restart / transport loss."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from sshpilot.api.daemon_reconnect import DaemonReconnectResult
from sshpilot.daemon.launcher import DaemonLaunchResult
from sshpilot.daemon.reconnect import ReconnectDecision, ReconnectOutcome
from sshpilot.daemon_terminal_policy import (
    DaemonTerminalReadinessReason,
    resolve_daemon_terminal_readiness,
)


class _ClosedDaemonClient:
    open_session = object()
    server_instance_id = "stale-instance"
    is_closed = True
    transport_failed = True


def test_closed_or_transport_failed_client_is_unavailable():
    readiness = resolve_daemon_terminal_readiness(
        _ClosedDaemonClient(),
        bridge=MagicMock(),
    )
    assert readiness.ready is False
    assert readiness.reason is DaemonTerminalReadinessReason.CLIENT_UNAVAILABLE


def test_request_daemon_reconnect_applies_new_client(monkeypatch):
    from sshpilot import main as main_module

    app = main_module.SshPilotApplication.__new__(main_module.SshPilotApplication)
    app.window = SimpleNamespace(_is_quitting=False, client=None, welcome_view=None)
    app._api_client_bridge = None
    app._api_client_selection = None
    app._api_daemon_launcher = object()
    app._daemon_reconnect_in_progress = False
    app._daemon_reconnect_generation = 0
    app._daemon_shutdown_intent = None
    installed = []

    def _install(client):
        installed.append(client)

    app.install_api_event_subscription = _install  # type: ignore[method-assign]

    new_client = SimpleNamespace(server_instance_id="new-instance")
    result = DaemonReconnectResult(
        client=new_client,
        launched=DaemonLaunchResult(client=new_client, process=None),
        decision=ReconnectDecision(
            outcome=ReconnectOutcome.ATTEMPT,
            message="ok",
        ),
    )

    class _Helper:
        def note_transport_loss(self):
            return None

        def reconnect(self, *, wait_for_backoff=True):
            del wait_for_backoff
            return result

    app._api_daemon_reconnect_helper = _Helper()

    def _idle(callback, *args):
        callback(*args) if args else callback()
        return 0

    monkeypatch.setattr(main_module.GLib, "idle_add", _idle)

    class _ImmediateThread:
        def __init__(self, target=None, name=None, daemon=None):
            self._target = target

        def start(self):
            if self._target is not None:
                self._target()

    monkeypatch.setattr(main_module.threading, "Thread", _ImmediateThread)

    app.request_daemon_reconnect(reason="test", immediate=True)

    assert app.window.client is new_client
    assert app._api_client_selection.client is new_client
    assert installed == [new_client]
    assert app._daemon_reconnect_in_progress is False


def test_preferences_schedules_reconnect_after_restart():
    from sshpilot.preferences import PreferencesWindow

    prefs = PreferencesWindow.__new__(PreferencesWindow)
    called = {}

    class _App:
        def request_daemon_reconnect(self, **kwargs):
            called.update(kwargs)

    prefs.get_application = lambda: _App()  # type: ignore[method-assign]
    prefs._schedule_daemon_reconnect_after_restart()
    assert called == {"reason": "preferences_restart", "immediate": True}


def test_transport_loss_suppressed_during_terminate_all(monkeypatch):
    """Terminate everything must not schedule reconnect on transport_closed."""
    from sshpilot import main as main_module
    from sshpilot.api.errors import ErrorCode, SshPilotError

    app = main_module.SshPilotApplication.__new__(main_module.SshPilotApplication)
    app.window = SimpleNamespace(
        _is_quitting=False,
        _daemon_shutdown_intent="terminate",
        _daemon_quit_decision=None,
    )
    app._daemon_shutdown_intent = "terminate"
    app._daemon_quit_decision = None
    scheduled = []

    def _request(**kwargs):
        scheduled.append(kwargs)

    app.request_daemon_reconnect = _request  # type: ignore[method-assign]
    app._on_daemon_transport_lost(
        SshPilotError(ErrorCode.TRANSPORT_CLOSED, "closed")
    )
    assert scheduled == []


def test_request_daemon_reconnect_suppressed_during_terminate_all():
    from sshpilot import main as main_module

    app = main_module.SshPilotApplication.__new__(main_module.SshPilotApplication)
    app.window = SimpleNamespace(_is_quitting=False)
    app._daemon_shutdown_intent = "terminate"
    app._daemon_reconnect_in_progress = False
    app._daemon_reconnect_generation = 0
    app._api_client_bridge = None
    called = []

    class _Helper:
        def note_transport_loss(self):
            called.append("note")

        def reconnect(self, **kwargs):
            called.append("reconnect")
            return None

    app._api_daemon_reconnect_helper = _Helper()
    app.request_daemon_reconnect(reason="transport_loss")
    assert called == []
    assert app._daemon_reconnect_in_progress is False


def test_finish_discards_reconnect_started_before_terminate(monkeypatch):
    """In-flight reconnect that finishes after terminate-all must not apply."""
    from sshpilot import main as main_module

    app = main_module.SshPilotApplication.__new__(main_module.SshPilotApplication)
    app.window = SimpleNamespace(_is_quitting=False, client=None, welcome_view=None)
    app._api_client_selection = None
    app._daemon_reconnect_in_progress = True
    app._daemon_reconnect_generation = 1
    app._daemon_shutdown_intent = "terminate"
    discarded = []

    def _discard(result):
        discarded.append(result)

    app._discard_accidental_daemon_reconnect = _discard  # type: ignore[method-assign]
    app.install_api_event_subscription = lambda client: None  # type: ignore[method-assign]

    new_client = SimpleNamespace(server_instance_id="accidental")
    result = DaemonReconnectResult(
        client=new_client,
        launched=DaemonLaunchResult(client=new_client, process=None),
        decision=ReconnectDecision(
            outcome=ReconnectOutcome.ATTEMPT,
            message="ok",
        ),
    )
    # Stale generation from before cancel_daemon_reconnect bumped it.
    assert app._finish_daemon_reconnect(result, generation=0) is False
    assert discarded == []  # generation mismatch → ignore without discard

    # Same generation but suppressed → discard accidental daemon.
    app._daemon_reconnect_generation = 0
    assert app._finish_daemon_reconnect(result, generation=0) is False
    assert discarded == [result]
    assert app.window.client is None


def test_begin_terminate_intent_cancels_reconnect():
    from sshpilot.daemon_quit_policy import (
        DaemonQuitDecision,
        begin_terminate_shutdown_intent,
    )

    cancelled = []

    class _App:
        _daemon_reconnect_generation = 0

        def cancel_daemon_reconnect(self, *, reason="shutdown"):
            cancelled.append(reason)
            self._daemon_reconnect_generation += 1

        def get_application(self):
            return self

    window = SimpleNamespace(get_application=None)
    app = _App()
    window.get_application = lambda: app
    begin_terminate_shutdown_intent(window)
    assert window._daemon_shutdown_intent == "terminate"
    assert window._daemon_quit_decision is DaemonQuitDecision.TERMINATE_ALL
    assert app._daemon_shutdown_intent == "terminate"
    assert cancelled == ["terminate_all"]


def test_run_server_returns_restart_exit_code():
    from sshpilot.daemon.cli import run_server
    from sshpilot.daemon.lifecycle_policy import RESTART_EXIT_CODE

    code = run_server(
        serve_forever=lambda: None,
        shutdown=lambda: None,
        startup_error=None,
        restart_requested=lambda: True,
    )
    assert code == RESTART_EXIT_CODE

    code = run_server(
        serve_forever=lambda: None,
        shutdown=lambda: None,
        startup_error=None,
        restart_requested=lambda: False,
    )
    assert code == 0

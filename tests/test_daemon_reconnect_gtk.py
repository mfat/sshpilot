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

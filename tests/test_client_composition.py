from types import SimpleNamespace

from sshpilot import main
from sshpilot.api import EventType, InProcessClient
from sshpilot.api.models.sessions import SessionState, SessionSummary
from sshpilot.api.client_factory import (
    CLIENT_MODE_ENVIRONMENT,
    ClientMode,
)
from sshpilot.daemon.launcher import DaemonLaunchResult
from sshpilot.main import SshPilotApplication
from sshpilot.window import MainWindow


class _Manager:
    def get_connections(self):
        return []


class _RecordingBridge:
    def submit(
        self,
        operation,
        *,
        on_success,
        on_error,
        on_discard=None,
    ):
        self.operation = operation
        self.on_success = on_success
        self.on_error = on_error
        self.on_discard = on_discard
        self.request = SimpleNamespace(cancel=lambda: None)
        return self.request

    def run(self):
        try:
            result = self.operation()
        except BaseException as error:
            self.on_error(error)
        else:
            self.on_success(result)


class _CompositionWindow:
    _compose_api_client = MainWindow._compose_api_client
    _begin_daemon_client_selection = MainWindow._begin_daemon_client_selection
    _apply_client_selection = MainWindow._apply_client_selection
    _handle_client_selection_error = MainWindow._handle_client_selection_error

    def __init__(self, app):
        self._app = app
        self.connection_manager = _Manager()
        self.group_manager = None
        self._is_quitting = False
        self.welcome_view = SimpleNamespace(
            set_client=lambda client, bridge=None: setattr(
                self,
                "welcome_selection",
                (client, bridge),
            )
        )
        self.warnings = []
        self.config = SimpleNamespace(
            get_setting=lambda key, default=None: default,
        )

    def get_application(self):
        return self._app

    def _show_client_mode_warning(self):
        self.warnings.append(self._client_mode_warning)
        self._client_mode_warning = None
        return False

    def _maybe_restore_daemon_sessions(self):
        return None


class _EventApplication:
    install_api_event_subscription = (
        SshPilotApplication.install_api_event_subscription
    )
    clear_api_event_subscription = SshPilotApplication.clear_api_event_subscription
    _handle_api_client_event = SshPilotApplication._handle_api_client_event
    _handle_api_session_event = SshPilotApplication._handle_api_session_event
    open_daemon_session_for_diagnostics = (
        SshPilotApplication.open_daemon_session_for_diagnostics
    )

    def __init__(self):
        self._api_event_subscription = None
        self.window = SimpleNamespace(
            _is_quitting=False,
            welcome_view=SimpleNamespace(
                schedule_connection_refresh=lambda: None,
                mark_connection_data_unavailable=lambda: None,
            ),
        )


def test_daemon_composition_is_deferred_then_injects_same_client(monkeypatch):
    monkeypatch.setenv(CLIENT_MODE_ENVIRONMENT, "daemon")
    daemon_client = SimpleNamespace(close=lambda: None)
    launcher_calls = []
    launcher = SimpleNamespace(
        connect_or_start=lambda: (
            launcher_calls.append(True)
            or DaemonLaunchResult(client=daemon_client, process=None)
        )
    )
    bridge = _RecordingBridge()
    app = SimpleNamespace(
        _api_client_bridge=bridge,
        _api_daemon_launcher=launcher,
    )
    window = _CompositionWindow(app)

    window._compose_api_client(app)

    assert isinstance(window.client, InProcessClient)
    assert window._api_client_selection_pending is True
    assert launcher_calls == []

    window._begin_daemon_client_selection()

    # Submission returns before launcher/readiness work is executed.
    assert launcher_calls == []
    bridge.run()

    assert launcher_calls == [True]
    assert window.client is daemon_client
    assert window.welcome_selection == (daemon_client, bridge)
    assert app._api_client_selection.client is daemon_client
    assert app._api_client_selection.mode is ClientMode.DAEMON


def test_invalid_mode_composition_stays_in_process_and_warns(monkeypatch):
    monkeypatch.setenv(CLIENT_MODE_ENVIRONMENT, "not-a-mode")
    app = SimpleNamespace()
    window = _CompositionWindow(app)

    window._compose_api_client(app)

    assert isinstance(window.client, InProcessClient)
    assert app._api_client_selection.mode is ClientMode.IN_PROCESS
    assert "compatibility mode" in window._client_mode_warning
    assert not hasattr(app, "_api_client_bridge")


def test_application_scoped_event_subscription_marshals_once_to_glib(monkeypatch):
    callbacks = []
    unsubscriptions = []
    subscribed = []

    class _Client:
        def subscribe_events(self, callback):
            subscribed.append(callback)
            return SimpleNamespace(
                unsubscribe=lambda: unsubscriptions.append(True)
            )

    monkeypatch.setattr(
        main.GLib,
        "idle_add",
        lambda callback, event_type: callbacks.append(
            (callback, event_type)
        ) or 1,
    )
    app = _EventApplication()
    client = _Client()

    app.install_api_event_subscription(client)
    subscribed[0](SimpleNamespace(type=EventType.CONNECTION_UPDATED))

    assert len(subscribed) == 1
    assert callbacks == [
        (app._handle_api_client_event, EventType.CONNECTION_UPDATED)
    ]

    app.clear_api_event_subscription()
    assert unsubscriptions == [True]


def test_application_session_event_is_marshaled_without_connection_refresh(
    monkeypatch,
):
    callbacks = []
    subscribed = []
    monkeypatch.setattr(
        main.GLib,
        "idle_add",
        lambda callback, event: callbacks.append((callback, event)) or 1,
    )

    class _Client:
        def subscribe_events(self, callback):
            subscribed.append(callback)
            return SimpleNamespace(unsubscribe=lambda: None)

    app = _EventApplication()
    event = SimpleNamespace(type=EventType.SESSION_STATE_CHANGED)
    app.install_api_event_subscription(_Client())
    subscribed[0](event)

    assert callbacks == [(app._handle_api_session_event, event)]
    assert app._handle_api_session_event(event) is False
    assert app._last_api_session_event is event


def test_session_diagnostic_open_uses_application_bridge_asynchronously():
    bridge = _RecordingBridge()
    summary = SessionSummary(
        id="session:test",
        connection_id="test",
        state=SessionState.FAILED,
    )
    client = SimpleNamespace(open_session=lambda request: summary)
    app = _EventApplication()
    app._api_client_selection = SimpleNamespace(client=client)
    app._api_client_bridge = bridge
    completed = []

    request = app.open_daemon_session_for_diagnostics(
        "test",
        on_success=completed.append,
    )

    assert request is bridge.request
    assert completed == []
    bridge.run()
    assert completed == [summary]
    assert app._last_api_session_summary is summary


def test_session_diagnostic_suppresses_late_result_during_window_shutdown():
    bridge = _RecordingBridge()
    summary = SessionSummary(
        id="session:test",
        connection_id="test",
        state=SessionState.FAILED,
    )
    app = _EventApplication()
    app.window = SimpleNamespace(_is_quitting=True)
    app._api_client_selection = SimpleNamespace(
        client=SimpleNamespace(open_session=lambda request: summary)
    )
    app._api_client_bridge = bridge
    completed = []

    app.open_daemon_session_for_diagnostics(
        "test",
        on_success=completed.append,
    )
    bridge.run()

    assert completed == []
    assert not hasattr(app, "_last_api_session_summary")

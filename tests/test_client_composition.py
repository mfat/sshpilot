from types import SimpleNamespace

from sshpilot.api import InProcessClient
from sshpilot.api.client_factory import (
    CLIENT_MODE_ENVIRONMENT,
    ClientMode,
)
from sshpilot.daemon.launcher import DaemonLaunchResult
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

    def get_application(self):
        return self._app

    def _show_client_mode_warning(self):
        self.warnings.append(self._client_mode_warning)
        self._client_mode_warning = None
        return False


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

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
    connection_projection = MagicMock()
    runtime_projection = MagicMock()
    app.window = SimpleNamespace(
        _is_quitting=False,
        client=None,
        welcome_view=None,
        connection_manager=connection_projection,
        connection_runtime_status=runtime_projection,
    )
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
    connection_projection.attach_client.assert_called_once_with(new_client)
    runtime_projection.attach_client.assert_called_once_with(new_client)
    assert app._daemon_reconnect_in_progress is False


def test_transport_loss_clears_stale_runtime_status_before_reconnect():
    from sshpilot import main as main_module
    from sshpilot.api.errors import ErrorCode, SshPilotError

    runtime_projection = MagicMock()
    app = main_module.SshPilotApplication.__new__(main_module.SshPilotApplication)
    app.window = SimpleNamespace(
        _is_quitting=False,
        _daemon_shutdown_intent=None,
        _daemon_quit_decision=None,
        connection_runtime_status=runtime_projection,
    )
    app._daemon_shutdown_intent = None
    app._daemon_quit_decision = None
    app.request_daemon_reconnect = MagicMock()

    app._on_daemon_transport_lost(
        SshPilotError(ErrorCode.TRANSPORT_CLOSED, "closed")
    )

    runtime_projection.close.assert_called_once_with()
    app.request_daemon_reconnect.assert_called_once_with(
        reason="transport_loss",
        immediate=False,
    )


def test_preferences_schedules_reconnect_after_restart(monkeypatch):
    from gi.repository import Gio
    from sshpilot.preferences import PreferencesWindow

    prefs = PreferencesWindow.__new__(PreferencesWindow)
    called = {}

    class _App:
        def request_daemon_reconnect(self, **kwargs):
            called.update(kwargs)

    monkeypatch.setattr(Gio.Application, "get_default", lambda: _App())
    prefs.parent_window = None
    prefs._schedule_daemon_reconnect_after_restart()
    assert called == {"reason": "preferences_restart", "immediate": True}


def test_preferences_schedules_reconnect_via_parent_window():
    from sshpilot.preferences import PreferencesWindow

    prefs = PreferencesWindow.__new__(PreferencesWindow)
    called = {}

    class _App:
        def request_daemon_reconnect(self, **kwargs):
            called.update(kwargs)

    class _Parent:
        def get_application(self):
            return _App()

    prefs.parent_window = _Parent()
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


def test_terminate_all_force_stop_does_not_relaunch_daemon(monkeypatch):
    """Connected app → terminate intent → force-stop → transport_closed idle → no relaunch.

    Mirrors production wiring: ``install_api_event_subscription`` queues
    ``_on_daemon_transport_lost`` via ``GLib.idle_add``. Intent must be set
    before ``stop_daemon`` so the idle callback does not ``connect_or_start``.
    """
    from sshpilot import main as main_module
    import sshpilot.daemon_quit_policy as quit_policy
    import sshpilot.shutdown as shutdown_mod
    from sshpilot.api.errors import ErrorCode, SshPilotError
    from sshpilot.api.models.daemon import (
        DaemonLifecycleState,
        DaemonResourceCounts,
        DaemonStopResult,
    )

    pending_idles: list = []
    connect_or_start_calls: list = []
    reconnect_requests: list = []

    def _idle(callback, *args):
        pending_idles.append((callback, args))
        return 0

    def _drain_idles() -> None:
        while pending_idles:
            callback, args = pending_idles.pop(0)
            callback(*args) if args else callback()

    monkeypatch.setattr(main_module.GLib, "idle_add", _idle)

    class _Client:
        def __init__(self):
            self._on_transport_lost = None
            self.server_instance_id = "connected-instance"
            self._socket_path = "/tmp/sshpilot-test-daemon.sock"

        def set_on_transport_lost(self, callback):
            self._on_transport_lost = callback

        def subscribe_events(self, _callback):
            return SimpleNamespace(close=lambda: None)

        def stop_daemon(self, request):
            assert request.force is True
            # Force-stop tears down the Unix transport; reader reports loss.
            handler = self._on_transport_lost
            if handler is not None:
                handler(
                    SshPilotError(ErrorCode.TRANSPORT_CLOSED, "daemon force-stopped")
                )
            return DaemonStopResult(
                accepted=True,
                state=DaemonLifecycleState.STOPPING,
                resources=DaemonResourceCounts(),
            )

        def list_sessions(self):
            return []

        def list_transfers(self):
            return []

        def list_forwards(self):
            return []

        def list_sftp_services(self):
            return []

    client = _Client()
    app = main_module.SshPilotApplication.__new__(main_module.SshPilotApplication)
    app.window = SimpleNamespace(
        _is_quitting=False,
        client=client,
        welcome_view=None,
        _daemon_shutdown_intent=None,
        _daemon_quit_decision=None,
        get_application=lambda: app,
    )
    app._api_client_bridge = None
    app._api_client_selection = SimpleNamespace(
        client=client, daemon_process=None
    )
    app._api_event_subscription = None
    app._daemon_reconnect_in_progress = False
    app._daemon_reconnect_generation = 0
    app._daemon_shutdown_intent = None
    app._daemon_quit_decision = None

    class _Helper:
        def note_transport_loss(self):
            reconnect_requests.append("note_transport_loss")

        def reconnect(self, **kwargs):
            del kwargs
            connect_or_start_calls.append("connect_or_start")
            new_client = SimpleNamespace(server_instance_id="relaunched")
            return DaemonReconnectResult(
                client=new_client,
                launched=DaemonLaunchResult(client=new_client, process=None),
                decision=ReconnectDecision(
                    outcome=ReconnectOutcome.ATTEMPT,
                    message="should not happen",
                ),
            )

    app._api_daemon_reconnect_helper = _Helper()
    app.install_api_event_subscription(client)

    monkeypatch.setattr(quit_policy, "_run_in_background", lambda fn: fn())
    monkeypatch.setattr(quit_policy, "_invoke_on_main", lambda fn: fn())
    monkeypatch.setattr(
        quit_policy,
        "wait_for_daemon_termination",
        lambda **_kwargs: [],
    )
    quit_calls: list = []
    monkeypatch.setattr(
        shutdown_mod,
        "cleanup_and_quit",
        lambda window: quit_calls.append(window),
    )

    # Real terminate-all: intent first, then force-stop (fires transport idle).
    quit_policy.apply_terminate_all(app.window)
    _drain_idles()

    assert app._daemon_shutdown_intent == "terminate"
    assert connect_or_start_calls == []
    assert reconnect_requests == []
    assert quit_calls == [app.window]
    assert app.window.client is client  # not replaced by a relaunched daemon


def test_queued_reconnect_aborted_before_connect_or_start(monkeypatch):
    """Reconnect already queued must not launch after terminate intent is set."""
    from sshpilot import main as main_module
    from sshpilot.daemon_quit_policy import begin_terminate_shutdown_intent

    connect_or_start_calls: list = []
    deferred_targets: list = []

    class _DeferredThread:
        def __init__(self, target=None, name=None, daemon=None):
            del name, daemon
            self._target = target

        def start(self):
            deferred_targets.append(self._target)

    monkeypatch.setattr(main_module.threading, "Thread", _DeferredThread)

    pending_idles: list = []

    def _idle(callback, *args):
        pending_idles.append((callback, args))
        return 0

    monkeypatch.setattr(main_module.GLib, "idle_add", _idle)

    app = main_module.SshPilotApplication.__new__(main_module.SshPilotApplication)
    app.window = SimpleNamespace(
        _is_quitting=False,
        client=SimpleNamespace(server_instance_id="old"),
        welcome_view=None,
        get_application=lambda: app,
    )
    app._api_client_bridge = None
    app._api_client_selection = None
    app._daemon_reconnect_in_progress = False
    app._daemon_reconnect_generation = 0
    app._daemon_shutdown_intent = None
    app._daemon_quit_decision = None

    class _Helper:
        def note_transport_loss(self):
            return None

        def reconnect(self, **kwargs):
            del kwargs
            connect_or_start_calls.append("connect_or_start")
            new_client = SimpleNamespace(server_instance_id="relaunched")
            return DaemonReconnectResult(
                client=new_client,
                launched=DaemonLaunchResult(client=new_client, process=None),
                decision=ReconnectDecision(
                    outcome=ReconnectOutcome.ATTEMPT,
                    message="should not happen",
                ),
            )

    app._api_daemon_reconnect_helper = _Helper()

    # Queue reconnect (worker not yet run) — e.g. earlier transport loss.
    app.request_daemon_reconnect(reason="transport_loss", immediate=True)
    assert len(deferred_targets) == 1
    assert app._daemon_reconnect_in_progress is True

    begin_terminate_shutdown_intent(app.window)
    assert app._daemon_shutdown_intent == "terminate"
    assert app._daemon_reconnect_generation == 1
    assert app._daemon_reconnect_in_progress is False

    # Queued worker runs after intent: must abort before connect_or_start.
    deferred_targets[0]()
    while pending_idles:
        callback, args = pending_idles.pop(0)
        callback(*args) if args else callback()

    assert connect_or_start_calls == []
    assert app.window.client.server_instance_id == "old"


def test_in_flight_reconnect_discarded_after_terminate_intent(monkeypatch):
    """Worker already inside reconnect() must discard the relaunched daemon."""
    import threading

    from sshpilot import main as main_module
    from sshpilot.daemon_quit_policy import begin_terminate_shutdown_intent

    entered = threading.Event()
    release = threading.Event()
    connect_or_start_calls: list = []
    discarded: list = []
    pending_idles: list = []
    started_threads: list = []

    def _idle(callback, *args):
        pending_idles.append((callback, args))
        return 0

    monkeypatch.setattr(main_module.GLib, "idle_add", _idle)

    class _TrackingThread(threading.Thread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            started_threads.append(self)

    monkeypatch.setattr(main_module.threading, "Thread", _TrackingThread)

    app = main_module.SshPilotApplication.__new__(main_module.SshPilotApplication)
    app.window = SimpleNamespace(
        _is_quitting=False,
        client=SimpleNamespace(server_instance_id="old"),
        welcome_view=None,
        get_application=lambda: app,
    )
    app._api_client_bridge = None
    app._api_client_selection = None
    app._daemon_reconnect_in_progress = False
    app._daemon_reconnect_generation = 0
    app._daemon_shutdown_intent = None
    app._daemon_quit_decision = None

    class _Helper:
        def note_transport_loss(self):
            return None

        def reconnect(self, **kwargs):
            del kwargs
            entered.set()
            assert release.wait(2.0)
            connect_or_start_calls.append("connect_or_start")
            new_client = SimpleNamespace(server_instance_id="accidental")
            return DaemonReconnectResult(
                client=new_client,
                launched=DaemonLaunchResult(client=new_client, process=None),
                decision=ReconnectDecision(
                    outcome=ReconnectOutcome.ATTEMPT,
                    message="too late",
                ),
            )

    app._api_daemon_reconnect_helper = _Helper()

    def _track_discard(result):
        discarded.append(result)

    app._discard_accidental_daemon_reconnect = _track_discard  # type: ignore[method-assign]

    app.request_daemon_reconnect(reason="transport_loss", immediate=True)
    assert entered.wait(2.0)
    assert len(started_threads) == 1

    begin_terminate_shutdown_intent(app.window)
    release.set()
    started_threads[0].join(timeout=2.0)
    assert not started_threads[0].is_alive()

    while pending_idles:
        callback, args = pending_idles.pop(0)
        callback(*args) if args else callback()

    assert connect_or_start_calls == ["connect_or_start"]
    assert len(discarded) == 1
    assert app.window.client.server_instance_id == "old"
    assert getattr(app, "_api_client_selection", None) is None


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

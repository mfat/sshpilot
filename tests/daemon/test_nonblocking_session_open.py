"""Non-blocking sessions.open acknowledgement tests."""

from __future__ import annotations

import threading
import time
from unittest.mock import Mock

import pytest

from sshpilot.api import DaemonClient, ErrorCode
from sshpilot.api.events import EventType
from sshpilot.api.models.common import ConnectionId, SessionId
from sshpilot.api.models.sessions import (
    CloseSessionRequest,
    OpenSessionRequest,
    SessionExitInfo,
    SessionFailure,
    SessionState,
    SessionSummary,
)
from sshpilot.api.models.terminal import TerminalDimensions
from sshpilot.terminal_session_controller import (
    DaemonTerminalSessionController,
    TerminalSessionState,
    required_daemon_terminal_capabilities,
)


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return bool(predicate())


class _Handle:
    def __init__(self, on_exit):
        self._on_exit = on_exit
        self._exit_info = None
        self._lock = threading.Lock()

    def terminate(self):
        self.exit(SessionExitInfo(exit_code=0, reason="terminated"))

    def kill(self):
        self.exit(SessionExitInfo(signal=9, reason="killed"))

    def wait(self, _timeout):
        with self._lock:
            return self._exit_info

    def exit(self, info):
        with self._lock:
            if self._exit_info is not None:
                return
            self._exit_info = info
        self._on_exit(info)


class _BlockingStartRunner:
    def __init__(self, *, fail: bool = False):
        self.start_entered = threading.Event()
        self.start_release = threading.Event()
        self.handles = []
        self.starts = 0
        self.fail = fail
        self.closed = False

    def start(self, _spec, on_exit, _on_output=None, _on_eof=None):
        self.starts += 1
        self.start_entered.set()
        self.start_release.wait(5)
        if self.fail:
            raise RuntimeError("forced startup failure")
        handle = _Handle(on_exit)
        self.handles.append(handle)
        return handle

    def close(self):
        self.closed = True
        self.start_release.set()


class _InteractionHoldingRunner:
    """Blocks in start until released, after publishing a typed interaction."""

    def __init__(self, broker_holder, interaction_type):
        self._broker_holder = broker_holder
        self._interaction_type = interaction_type
        self.start_entered = threading.Event()
        self.start_release = threading.Event()
        self.handles = []
        self.closed = False

    def start(self, spec, on_exit, _on_output=None, _on_eof=None):
        from sshpilot.api.models import (
            HostKeyPrompt,
            HostKeyStatus,
            InteractionType,
            PassphrasePrompt,
            PasswordPrompt,
        )

        broker = self._broker_holder[0]
        self.start_entered.set()
        if self._interaction_type is InteractionType.PASSWORD:
            broker.create(
                session_id=spec.session_id,
                connection_id=spec.connection_id,
                interaction_type=InteractionType.PASSWORD,
                prompt=PasswordPrompt(
                    username="alice",
                    hostname="example.test",
                    port=22,
                    attempt=1,
                    can_remember=False,
                    stored_secret_available=False,
                ),
            )
        elif self._interaction_type is InteractionType.HOST_KEY_CONFIRMATION:
            broker.create(
                session_id=spec.session_id,
                connection_id=spec.connection_id,
                interaction_type=InteractionType.HOST_KEY_CONFIRMATION,
                prompt=HostKeyPrompt(
                    hostname="example.test",
                    port=22,
                    key_type="ssh-ed25519",
                    fingerprint="SHA256:abcdefghijklmnopqrstuvwxyz0123456789ABCDE",
                    status=HostKeyStatus.UNKNOWN,
                ),
            )
        else:
            broker.create(
                session_id=spec.session_id,
                connection_id=spec.connection_id,
                interaction_type=InteractionType.PRIVATE_KEY_PASSPHRASE,
                prompt=PassphrasePrompt(
                    key_display_name="id_test",
                    key_fingerprint="SHA256:abcdefghijklmnopqrstuvwxyz0123456789ABCDE",
                    attempt=1,
                    can_remember=False,
                    stored_secret_available=False,
                ),
            )
        self.start_release.wait(5)
        handle = _Handle(on_exit)
        self.handles.append(handle)
        return handle

    def close(self):
        self.closed = True
        self.start_release.set()


def test_open_session_returns_starting_while_runner_blocked(
    daemon_factory,
    _repeat,
):
    """A slow start must acknowledge immediately and never block the lane.

    A successful ``STARTING`` acknowledgement while the runner's ``start()``
    stays blocked proves the open is not classified as failed or
    mutation-ambiguous at the transport level. Control RPCs on the same
    client remain responsive throughout. (This merges the legacy
    ``test_slow_open_does_not_raise_mutation_ambiguous`` soak: the ack
    itself is the property, so no fixed-duration wait past the request
    timeout is needed in the normal suite.)
    """
    runner = _BlockingStartRunner()
    server, _manager = daemon_factory(session_runner=runner)
    client = DaemonClient(
        socket_path=server.socket_path,
        timeout=0.4,
    )
    try:
        opened = client.open_session(
            OpenSessionRequest(connection_id=client.list_connections()[0].id)
        )
        assert opened.state is SessionState.STARTING
        assert runner.start_entered.wait(1)
        assert not runner.start_release.is_set()
        assert client.get_session(opened.id).state is SessionState.STARTING
        assert client.list_connections()

        runner.start_release.set()
        assert _wait_until(
            lambda: client.get_session(opened.id).state is SessionState.RUNNING
        )
    finally:
        runner.start_release.set()
        client.close()


@pytest.mark.parametrize(
    "interaction_type_name",
    ["PASSWORD", "HOST_KEY_CONFIRMATION", "PRIVATE_KEY_PASSPHRASE"],
)
def test_open_session_returns_while_interaction_pending(
    daemon_factory,
    interaction_type_name,
    _repeat,
):
    from sshpilot.api.models import InteractionType

    interaction_type = InteractionType[interaction_type_name]
    broker_holder = [None]
    runner = _InteractionHoldingRunner(broker_holder, interaction_type)
    server, _manager = daemon_factory(session_runner=runner)
    broker_holder[0] = server._interaction_broker
    client = DaemonClient(
        socket_path=server.socket_path,
        client_id=f"client:{interaction_type_name.lower()}:{_repeat}",
        timeout=0.4,
    )
    try:
        opened = client.open_session(
            OpenSessionRequest(connection_id=client.list_connections()[0].id)
        )
        assert opened.state is SessionState.STARTING
        assert runner.start_entered.wait(1)
        assert _wait_until(lambda: bool(client.list_interactions()))
        assert client.get_session(opened.id).state is SessionState.STARTING
        assert client.list_connections()

        runner.start_release.set()
        assert _wait_until(
            lambda: client.get_session(opened.id).state is SessionState.RUNNING
        )
    finally:
        runner.start_release.set()
        client.close()


def test_startup_failure_after_acknowledgement_is_async(
    daemon_factory,
    _repeat,
):
    runner = _BlockingStartRunner(fail=True)
    server, _manager = daemon_factory(session_runner=runner)
    client = DaemonClient(socket_path=server.socket_path, timeout=0.4)
    events = []
    client.subscribe_events(events.append)
    try:
        opened = client.open_session(
            OpenSessionRequest(connection_id=client.list_connections()[0].id)
        )
        assert opened.state is SessionState.STARTING
        assert runner.start_entered.wait(1)
        runner.start_release.set()
        assert _wait_until(
            lambda: client.get_session(opened.id).state is SessionState.FAILED
        )
        failed = client.get_session(opened.id)
        assert failed.failure.code == ErrorCode.SESSION_STARTUP_FAILED.value
        assert _wait_until(
            lambda: any(
                event.type is EventType.SESSION_STATE_CHANGED
                and getattr(event.payload, "state", None) is SessionState.FAILED
                for event in events
            )
        )
    finally:
        runner.start_release.set()
        client.close()


def test_close_while_startup_blocked(daemon_factory, _repeat):
    runner = _BlockingStartRunner()
    server, _manager = daemon_factory(session_runner=runner)
    client = DaemonClient(socket_path=server.socket_path, timeout=0.4)
    try:
        opened = client.open_session(
            OpenSessionRequest(connection_id=client.list_connections()[0].id)
        )
        assert runner.start_entered.wait(1)
        closer = threading.Thread(
            target=lambda: client.close_session(
                CloseSessionRequest(session_id=opened.id)
            ),
            daemon=True,
        )
        closer.start()
        assert _wait_until(lambda: server._session_executor.outstanding == 2)
        runner.start_release.set()
        closer.join(2)
        assert _wait_until(
            lambda: client.get_session(opened.id).state is SessionState.CLOSED
        )
    finally:
        runner.start_release.set()
        client.close()


def test_two_simultaneous_opens_create_distinct_sessions(
    daemon_factory,
    _repeat,
):
    runner = _BlockingStartRunner()
    server, _manager = daemon_factory(
        session_runner=runner,
        session_command_workers=2,
    )
    client_a = DaemonClient(
        socket_path=server.socket_path,
        client_id="client:a",
        timeout=0.4,
    )
    client_b = DaemonClient(
        socket_path=server.socket_path,
        client_id="client:b",
        timeout=0.4,
    )
    try:
        first = client_a.open_session(
            OpenSessionRequest(connection_id=client_a.list_connections()[0].id)
        )
        second = client_b.open_session(
            OpenSessionRequest(connection_id=client_b.list_connections()[0].id)
        )
        assert first.id != second.id
        assert first.state is SessionState.STARTING
        assert second.state is SessionState.STARTING
        assert _wait_until(lambda: runner.starts == 2)
        assert runner.starts == 2
        runner.start_release.set()
        assert _wait_until(
            lambda: client_a.get_session(first.id).state is SessionState.RUNNING
            and client_b.get_session(second.id).state is SessionState.RUNNING
        )
        assert len(runner.handles) == 2
    finally:
        runner.start_release.set()
        client_a.close()
        client_b.close()


def test_control_rpc_remains_responsive_while_startup_blocked(
    daemon_factory,
    _repeat,
):
    runner = _BlockingStartRunner()
    server, _manager = daemon_factory(session_runner=runner)
    opener = DaemonClient(
        socket_path=server.socket_path,
        client_id="client:opener",
        timeout=0.4,
    )
    reader = DaemonClient(
        socket_path=server.socket_path,
        client_id="client:reader",
        timeout=0.4,
    )
    try:
        opened = opener.open_session(
            OpenSessionRequest(connection_id=opener.list_connections()[0].id)
        )
        assert runner.start_entered.wait(1)
        # A handful of sequential control RPCs while start stays blocked prove
        # the control lane stays responsive — no fixed-duration soak loop.
        for _round in range(3):
            assert reader.list_connections()
            assert reader.get_session(opened.id).state is SessionState.STARTING
            assert reader.get_capabilities().supported
        assert not runner.start_release.is_set()
        runner.start_release.set()
        assert _wait_until(
            lambda: reader.get_session(opened.id).state is SessionState.RUNNING
        )
    finally:
        runner.start_release.set()
        opener.close()
        reader.close()


def test_gtk_controller_attaches_while_session_starting():
    session_id = SessionId("session:00000000-0000-4000-8000-000000000001")
    summary = SessionSummary(
        id=session_id,
        connection_id=ConnectionId("test"),
        state=SessionState.STARTING,
    )
    attach_result = Mock()
    attach_result.attachment = Mock(id="attachment:1", input_owner=True)
    attach_result.live_sequence = 0
    attach_result.available_start = 0

    client = Mock()
    capabilities = Mock()
    capabilities.supported = required_daemon_terminal_capabilities()
    client.get_capabilities.return_value = capabilities
    client.server_instance_id = "instance"
    client.open_session.return_value = summary
    client.attach_session.return_value = attach_result
    client.subscribe_events = Mock(return_value=Mock(unsubscribe=Mock()))

    delivered = []

    class _Bridge:
        def submit(self, operation, *, on_success, on_error, on_discard=None):
            try:
                result = operation()
            except BaseException as error:
                on_error(error)
            else:
                on_success(result)
            return Mock()

        def bind_terminal(self, *_args, **_kwargs):
            return Mock(close=Mock())

    bridge = _Bridge()
    errors = []
    controller = DaemonTerminalSessionController(
        client=client,
        bridge=bridge,
        connection_id=ConnectionId("test"),
        view_id="view-1",
        on_error=errors.append,
    )
    controller.open(ConnectionId("test"), TerminalDimensions(24, 80))
    assert controller.tab_state.session_id == session_id
    assert controller.state is TerminalSessionState.ACTIVE
    assert errors == []
    client.attach_session.assert_called_once()
    client.subscribe_events.assert_called_once()
    delivered.append(True)
    assert delivered


def test_gtk_controller_closes_tab_during_startup():
    session_id = SessionId("session:00000000-0000-4000-8000-000000000003")
    summary = SessionSummary(
        id=session_id,
        connection_id=ConnectionId("test"),
        state=SessionState.STARTING,
    )
    attach_result = Mock()
    attach_result.attachment = Mock(id="attachment:1", input_owner=True)
    attach_result.live_sequence = 0
    attach_result.available_start = 0

    client = Mock()
    capabilities = Mock()
    capabilities.supported = required_daemon_terminal_capabilities()
    client.get_capabilities.return_value = capabilities
    client.server_instance_id = "instance"
    client.open_session.return_value = summary
    client.attach_session.return_value = attach_result
    client.close_session.return_value = None
    client.subscribe_events = Mock(return_value=Mock(unsubscribe=Mock()))

    class _Bridge:
        def submit(self, operation, *, on_success, on_error, on_discard=None):
            try:
                result = operation()
            except BaseException as error:
                on_error(error)
            else:
                on_success(result)
            return Mock()

        def bind_terminal(self, *_args, **_kwargs):
            return Mock(close=Mock())

    controller = DaemonTerminalSessionController(
        client=client,
        bridge=_Bridge(),
        connection_id=ConnectionId("test"),
        view_id="view-1",
    )
    controller.open(ConnectionId("test"))
    assert controller.tab_state.session_id == session_id
    controller.close()
    assert controller.state in {
        TerminalSessionState.CLOSING,
        TerminalSessionState.CLOSED,
    }
    client.close_session.assert_called()


def test_gtk_controller_updates_tab_on_async_startup_failure():
    session_id = SessionId("session:00000000-0000-4000-8000-000000000002")
    summary = SessionSummary(
        id=session_id,
        connection_id=ConnectionId("test"),
        state=SessionState.STARTING,
    )
    attach_result = Mock()
    attach_result.attachment = Mock(id="attachment:1", input_owner=True)
    attach_result.live_sequence = 0
    attach_result.available_start = 0

    client = Mock()
    capabilities = Mock()
    capabilities.supported = required_daemon_terminal_capabilities()
    client.get_capabilities.return_value = capabilities
    client.server_instance_id = "instance"
    client.open_session.return_value = summary
    client.attach_session.return_value = attach_result
    event_callbacks = []

    def _subscribe(callback):
        event_callbacks.append(callback)
        return Mock(unsubscribe=Mock())

    client.subscribe_events = _subscribe

    class _Bridge:
        def submit(self, operation, *, on_success, on_error, on_discard=None):
            try:
                result = operation()
            except BaseException as error:
                on_error(error)
            else:
                on_success(result)
            return Mock()

        def bind_terminal(self, *_args, **_kwargs):
            return Mock(close=Mock())

    errors = []
    controller = DaemonTerminalSessionController(
        client=client,
        bridge=_Bridge(),
        connection_id=ConnectionId("test"),
        view_id="view-1",
        on_error=errors.append,
    )
    controller.open(ConnectionId("test"))
    assert controller.state is TerminalSessionState.ACTIVE

    failed = SessionSummary(
        id=session_id,
        connection_id=ConnectionId("test"),
        state=SessionState.FAILED,
        failure=SessionFailure(
            code=ErrorCode.SESSION_STARTUP_FAILED.value,
            message="The session process could not be started",
        ),
    )
    event = Mock(
        type=EventType.SESSION_STATE_CHANGED,
        session_id=session_id,
        payload=failed,
    )
    event_callbacks[0](event)
    assert controller.state is TerminalSessionState.FAILED
    assert len(errors) == 1
    assert errors[0].code is ErrorCode.SESSION_STARTUP_FAILED


def test_gtk_close_during_blocked_startup(daemon_factory, _repeat):
    runner = _BlockingStartRunner()
    server, _manager = daemon_factory(session_runner=runner)
    client = DaemonClient(socket_path=server.socket_path, timeout=0.4)
    try:
        opened = client.open_session(
            OpenSessionRequest(connection_id=client.list_connections()[0].id)
        )
        assert opened.state is SessionState.STARTING
        assert runner.start_entered.wait(1)

        session_id = opened.id
        closer_done = threading.Event()
        closer_error = []

        def _close():
            try:
                client.close_session(CloseSessionRequest(session_id=session_id))
            except BaseException as error:
                closer_error.append(error)
            finally:
                closer_done.set()

        threading.Thread(target=_close, daemon=True).start()
        assert _wait_until(lambda: server._session_executor.outstanding >= 2)
        runner.start_release.set()
        assert closer_done.wait(2)
        assert closer_error == []
        assert _wait_until(
            lambda: client.get_session(session_id).state is SessionState.CLOSED
        )
    finally:
        runner.start_release.set()
        client.close()

import threading
import sys
from datetime import datetime, timedelta, timezone

import pytest

from sshpilot.api import ErrorCode, InProcessClient, SshPilotError
from sshpilot.api.events import EventType
from sshpilot.api.models.common import ClientId
from sshpilot.api.models.sessions import (
    AttachSessionRequest,
    CloseSessionRequest,
    DetachSessionRequest,
    OpenSessionRequest,
    SessionExitInfo,
    SessionState,
)
from sshpilot.api.session_identity import (
    canonical_session_uuid,
    session_id_from_uuid,
    session_uuid_from_id,
)
from sshpilot.connection_identity import new_connection_uuid
from sshpilot.daemon.session_runtime import (
    SessionRuntime,
    SubprocessSessionProcessRunner,
    is_valid_session_transition,
)


class _Connection:
    def __init__(self):
        self.nickname = "demo"
        self.uuid = new_connection_uuid()
        self.host = "demo"
        self.hostname = "example.test"
        self.username = "alice"
        self.port = 22
        self.protocol = "ssh"
        self.aliases = []
        self.auth_method = 0
        self.keyfile = ""
        self.identity_files = []
        self.certificate = ""
        self.certificate_files = []
        self.x11_forwarding = False
        self.forwarding_rules = []
        self.proxy_jump = []
        self.data = {}


class _Manager:
    def __init__(self):
        self.connection = _Connection()

    def get_connections(self):
        return [self.connection]

    def connect(self, _name, _callback):
        return 1

    def disconnect(self, _handler_id):
        return None


class ControlledHandle:
    def __init__(self, on_exit, *, exit_on_terminate=True):
        self._on_exit = on_exit
        self._exit_on_terminate = exit_on_terminate
        self._exit_info = None
        self.terminated = 0
        self.killed = 0
        self._lock = threading.Lock()

    def terminate(self):
        self.terminated += 1
        if self._exit_on_terminate:
            self.exit(SessionExitInfo(exit_code=0, reason="terminated"))

    def kill(self):
        self.killed += 1
        self.exit(SessionExitInfo(signal=9, reason="killed"))

    def wait(self, timeout):
        del timeout
        with self._lock:
            return self._exit_info

    def exit(self, exit_info):
        with self._lock:
            if self._exit_info is not None:
                return
            self._exit_info = exit_info
        self._on_exit(exit_info)


class ControlledRunner:
    def __init__(self, *, fail=False, exit_on_terminate=True):
        self.fail = fail
        self.exit_on_terminate = exit_on_terminate
        self.handles = []
        self.specs = []
        self.closed = False

    def start(self, spec, on_exit):
        self.specs.append(spec)
        if self.fail:
            raise RuntimeError("sensitive runner failure")
        handle = ControlledHandle(
            on_exit,
            exit_on_terminate=self.exit_on_terminate,
        )
        self.handles.append(handle)
        return handle

    def close(self):
        self.closed = True


class SynchronousExitRunner(ControlledRunner):
    def start(self, spec, on_exit):
        handle = super().start(spec, on_exit)
        handle.exit(SessionExitInfo(exit_code=0, reason="immediate_exit"))
        return handle


class RetryableTerminationHandle(ControlledHandle):
    def __init__(self, on_exit):
        super().__init__(on_exit, exit_on_terminate=False)
        self.allow_exit = False

    def terminate(self):
        self.terminated += 1
        if self.allow_exit:
            self.exit(SessionExitInfo(exit_code=0, reason="retry_terminated"))

    def kill(self):
        self.killed += 1
        if self.allow_exit:
            self.exit(SessionExitInfo(signal=9, reason="retry_killed"))


class RetryableTerminationRunner(ControlledRunner):
    def start(self, spec, on_exit):
        self.specs.append(spec)
        handle = RetryableTerminationHandle(on_exit)
        self.handles.append(handle)
        return handle


@pytest.fixture
def runtime_parts():
    manager = _Manager()
    core = InProcessClient(manager, client_name="runtime-test")
    runner = ControlledRunner()
    runtime = SessionRuntime(core, runner=runner)
    yield runtime, core, runner
    runtime.shutdown()
    core.close()


def test_session_id_is_uuidv4_shaped_and_strict():
    identifier = session_id_from_uuid("550E8400-E29B-41D4-A716-446655440000")

    assert identifier == "session:550e8400-e29b-41d4-a716-446655440000"
    assert session_uuid_from_id(identifier) == identifier.removeprefix("session:")
    with pytest.raises(ValueError):
        canonical_session_uuid("00000000-0000-0000-0000-000000000000")
    for invalid in ("", "session:test", "other:550e8400-e29b-41d4-a716-446655440000"):
        with pytest.raises(ValueError):
            session_uuid_from_id(invalid)


def test_state_machine_accepts_only_the_documented_transitions():
    expected = {
        SessionState.CREATED: {
            SessionState.STARTING,
            SessionState.FAILED,
            SessionState.CLOSED,
        },
        SessionState.STARTING: {
            SessionState.RUNNING,
            SessionState.CLOSING,
            SessionState.EXITED,
            SessionState.FAILED,
        },
        SessionState.RUNNING: {
            SessionState.CLOSING,
            SessionState.EXITED,
            SessionState.FAILED,
        },
        SessionState.CLOSING: {
            SessionState.EXITED,
            SessionState.FAILED,
            SessionState.CLOSED,
        },
        SessionState.EXITED: {SessionState.CLOSED},
        SessionState.FAILED: {SessionState.CLOSED},
        SessionState.CLOSED: set(),
    }
    for current in SessionState:
        for target in SessionState:
            assert is_valid_session_transition(current, target) is (target in expected[current])


def test_open_list_get_and_spontaneous_exit_are_serial_and_safe(runtime_parts):
    runtime, core, runner = runtime_parts
    events = []
    runtime.subscribe_events(events.append)
    connection_id = core.list_connections()[0].id

    opened = runtime.open_session(
        OpenSessionRequest(connection_id=connection_id),
        client_id=ClientId("client:a"),
    )

    assert opened.state is SessionState.RUNNING
    assert opened.id.startswith("session:")
    assert runtime.list_sessions() == [opened]
    assert runtime.get_session(opened.id) == opened
    assert runner.specs[0].connection_id == connection_id
    assert [event.type for event in events] == [
        EventType.SESSION_CREATED,
        EventType.SESSION_STATE_CHANGED,
        EventType.SESSION_STATE_CHANGED,
    ]
    assert [event.payload.state for event in events] == [
        SessionState.CREATED,
        SessionState.STARTING,
        SessionState.RUNNING,
    ]

    runner.handles[0].exit(SessionExitInfo(exit_code=17, reason="process_exit"))

    final = runtime.get_session(opened.id)
    assert final.state is SessionState.CLOSED
    assert final.exit_info.exit_code == 17
    assert [event.type for event in events[-2:]] == [
        EventType.SESSION_EXITED,
        EventType.SESSION_CLOSED,
    ]


def test_prepare_and_start_split_keeps_runner_work_out_of_prepare(runtime_parts):
    runtime, core, runner = runtime_parts
    prepared = runtime.prepare_open_session(
        OpenSessionRequest(connection_id=core.list_connections()[0].id),
        client_id=ClientId("client:a"),
    )

    assert prepared.state is SessionState.STARTING
    assert runner.specs == []

    runtime.start_session(prepared.id)

    assert runtime.get_session(prepared.id).state is SessionState.RUNNING
    assert len(runner.specs) == 1
    with pytest.raises(SshPilotError) as caught:
        runtime.start_session(prepared.id)
    assert caught.value.code is ErrorCode.SESSION_INVALID_STATE
    assert len(runner.specs) == 1


def test_prepare_and_finish_close_keep_runner_work_out_of_prepare(runtime_parts):
    runtime, core, runner = runtime_parts
    session = runtime.open_session(
        OpenSessionRequest(connection_id=core.list_connections()[0].id),
        client_id=ClientId("client:a"),
    )

    assert runtime.prepare_close_session(
        CloseSessionRequest(session_id=session.id)
    )
    assert runtime.get_session(session.id).state is SessionState.CLOSING
    assert runner.handles[0].terminated == 0

    runtime.finish_close_session(session.id)

    assert runtime.get_session(session.id).state is SessionState.CLOSED
    assert runner.handles[0].terminated == 1


def test_startup_failure_is_a_real_failed_session_without_sensitive_details():
    manager = _Manager()
    core = InProcessClient(manager)
    runtime = SessionRuntime(core, runner=ControlledRunner(fail=True))
    try:
        opened = runtime.open_session(
            OpenSessionRequest(connection_id=core.list_connections()[0].id),
            client_id=ClientId("client:a"),
        )
        assert opened.state is SessionState.FAILED
        assert opened.failure.code == ErrorCode.SESSION_STARTUP_FAILED.value
        assert opened.failure.message == "The session process could not be started"
        assert "sensitive" not in repr(opened)
    finally:
        runtime.shutdown()
        core.close()


def test_open_rejects_missing_connection_and_unsupported_protocol():
    manager = _Manager()
    core = InProcessClient(manager)
    runtime = SessionRuntime(core, runner=ControlledRunner())
    try:
        with pytest.raises(SshPilotError) as missing:
            runtime.open_session(
                OpenSessionRequest(connection_id="connection:00000000-0000-0000-0000-000000000001"),
                client_id=ClientId("client:a"),
            )
        assert missing.value.code is ErrorCode.CONNECTION_NOT_FOUND

        manager.connection.protocol = "telnet"
        with pytest.raises(SshPilotError) as unsupported:
            runtime.open_session(
                OpenSessionRequest(connection_id=core.list_connections()[0].id),
                client_id=ClientId("client:a"),
            )
        assert unsupported.value.code is ErrorCode.UNSUPPORTED_SESSION_PROTOCOL
        assert runtime.list_sessions() == []
    finally:
        runtime.shutdown()
        core.close()


def test_process_exit_before_runner_returns_does_not_retain_a_stale_handle():
    manager = _Manager()
    core = InProcessClient(manager)
    runner = SynchronousExitRunner()
    runtime = SessionRuntime(core, runner=runner)
    try:
        opened = runtime.open_session(
            OpenSessionRequest(connection_id=core.list_connections()[0].id),
            client_id=ClientId("client:a"),
        )

        assert opened.state is SessionState.CLOSED
        runtime.close_session(CloseSessionRequest(session_id=opened.id))
        assert runner.handles[0].terminated == 1
    finally:
        runtime.shutdown()
        core.close()


def test_attachment_membership_always_creates_new_attachments_phase9(runtime_parts):
    runtime, core, _runner = runtime_parts
    session = runtime.open_session(
        OpenSessionRequest(connection_id=core.list_connections()[0].id),
        client_id=ClientId("client:a"),
    )
    request = AttachSessionRequest(session_id=session.id)

    first = runtime.attach_session(request, client_id=ClientId("client:a"))
    second = runtime.attach_session(request, client_id=ClientId("client:a"))
    other = runtime.attach_session(request, client_id=ClientId("client:b"))

    # Phase 9: Always create new attachments (each pane has its own attachment)
    assert first.attachment.id != second.attachment.id
    assert first.attachment.id != other.attachment.id
    assert second.attachment.id != other.attachment.id

    # Session summaries should be the same but attachment IDs different
    assert first.session.id == second.session.id == other.session.id

    assert first.attachment.input_owner is False
    assert other.attachment.input_owner is False

    # Test that attachments belong to the right clients
    with pytest.raises(SshPilotError) as caught:
        runtime.detach_session(
            DetachSessionRequest(
                session_id=session.id,
                attachment_id=other.attachment.id,
            ),
            client_id=ClientId("client:a"),
        )
    assert caught.value.code is ErrorCode.PERMISSION_DENIED

    # Clean up - detach client removes all attachments for that client
    runtime.detach_client(ClientId("client:b"))

    # Detach specific attachments for client:a
    runtime.detach_session(
        DetachSessionRequest(
            session_id=session.id,
            attachment_id=first.attachment.id,
        ),
        client_id=ClientId("client:a"),
    )
    runtime.detach_session(
        DetachSessionRequest(
            session_id=session.id,
            attachment_id=second.attachment.id,
        ),
        client_id=ClientId("client:a"),
    )

    # Attempting to detach non-existent attachment should be safe (idempotent)
    runtime.detach_session(
        DetachSessionRequest(
            session_id=session.id,
            attachment_id=first.attachment.id,
        ),
        client_id=ClientId("client:a"),
    )


def test_close_is_idempotent_and_terminates_only_owned_handle(runtime_parts):
    runtime, core, runner = runtime_parts
    session = runtime.open_session(
        OpenSessionRequest(connection_id=core.list_connections()[0].id),
        client_id=ClientId("client:a"),
    )
    request = CloseSessionRequest(session_id=session.id)

    runtime.close_session(request)
    runtime.close_session(request)

    assert runtime.get_session(session.id).state is SessionState.CLOSED
    assert runner.handles[0].terminated == 1
    assert runner.handles[0].killed == 0


def test_failed_termination_retains_owned_handle_for_explicit_retry():
    manager = _Manager()
    core = InProcessClient(manager)
    runner = RetryableTerminationRunner()
    runtime = SessionRuntime(core, runner=runner, close_grace_seconds=0)
    events = []
    runtime.subscribe_events(events.append)
    session = runtime.open_session(
        OpenSessionRequest(connection_id=core.list_connections()[0].id),
        client_id=ClientId("client:a"),
    )

    with pytest.raises(SshPilotError) as caught:
        runtime.close_session(CloseSessionRequest(session_id=session.id))
    assert caught.value.code is ErrorCode.SESSION_TERMINATION_FAILED
    assert runtime.get_session(session.id).state is SessionState.FAILED

    runner.handles[0].allow_exit = True
    runtime.close_session(CloseSessionRequest(session_id=session.id))

    assert runtime.get_session(session.id).state is SessionState.CLOSED
    assert runner.handles[0].terminated == 2
    assert sum(event.type is EventType.SESSION_EXITED for event in events) == 1
    assert sum(event.type is EventType.SESSION_CLOSED for event in events) == 1
    runtime.shutdown()
    core.close()


@pytest.mark.parametrize("_repeat", range(10))
def test_concurrent_close_and_process_exit_emit_one_final_pair(
    runtime_parts,
    _repeat,
):
    runtime, core, runner = runtime_parts
    events = []
    runtime.subscribe_events(events.append)
    session = runtime.open_session(
        OpenSessionRequest(connection_id=core.list_connections()[0].id),
        client_id=ClientId("client:a"),
    )
    barrier = threading.Barrier(3)

    def close_session():
        barrier.wait()
        runtime.close_session(CloseSessionRequest(session_id=session.id))

    def exit_process():
        barrier.wait()
        runner.handles[0].exit(SessionExitInfo(exit_code=0, reason="concurrent_exit"))

    close_thread = threading.Thread(target=close_session)
    exit_thread = threading.Thread(target=exit_process)
    close_thread.start()
    exit_thread.start()
    barrier.wait()
    close_thread.join(timeout=1)
    exit_thread.join(timeout=1)

    assert not close_thread.is_alive()
    assert not exit_thread.is_alive()
    assert runtime.get_session(session.id).state is SessionState.CLOSED
    assert sum(event.type is EventType.SESSION_EXITED for event in events) == 1
    assert sum(event.type is EventType.SESSION_CLOSED for event in events) == 1


def test_shutdown_from_session_subscriber_does_not_deadlock():
    manager = _Manager()
    core = InProcessClient(manager)
    runner = ControlledRunner()
    runtime = SessionRuntime(core, runner=runner)
    runtime.subscribe_events(
        lambda event: runtime.shutdown() if event.type is EventType.SESSION_CREATED else None
    )

    opened = runtime.open_session(
        OpenSessionRequest(connection_id=core.list_connections()[0].id),
        client_id=ClientId("client:a"),
    )

    assert opened.state is SessionState.CLOSED
    assert runner.handles == []
    core.close()


@pytest.mark.parametrize("_repeat", range(5))
def test_shutdown_kills_stubborn_owned_handle_within_runtime_policy(_repeat):
    manager = _Manager()
    core = InProcessClient(manager)
    runner = ControlledRunner(exit_on_terminate=False)
    runtime = SessionRuntime(
        core,
        runner=runner,
        close_grace_seconds=0,
        shutdown_timeout_seconds=0,
    )
    session = runtime.open_session(
        OpenSessionRequest(connection_id=core.list_connections()[0].id),
        client_id=ClientId("client:a"),
    )

    runtime.shutdown()

    assert runner.handles[0].terminated == 1
    assert runner.handles[0].killed == 1
    assert runner.closed is True
    with pytest.raises(SshPilotError) as caught:
        runtime.get_session(session.id)
    assert caught.value.code is ErrorCode.DAEMON_SHUTTING_DOWN
    core.close()


def test_closed_session_retention_is_bounded():
    manager = _Manager()
    core = InProcessClient(manager)
    runtime = SessionRuntime(
        core,
        runner=ControlledRunner(),
        max_retained_closed_sessions=1,
    )
    first = runtime.open_session(
        OpenSessionRequest(connection_id=core.list_connections()[0].id),
        client_id=ClientId("client:a"),
    )
    runtime.close_session(CloseSessionRequest(session_id=first.id))
    second = runtime.open_session(
        OpenSessionRequest(connection_id=core.list_connections()[0].id),
        client_id=ClientId("client:a"),
    )
    runtime.close_session(CloseSessionRequest(session_id=second.id))

    with pytest.raises(SshPilotError) as caught:
        runtime.get_session(first.id)
    assert caught.value.code is ErrorCode.SESSION_NOT_FOUND
    assert runtime.list_sessions() == [runtime.get_session(second.id)]
    runtime.shutdown()
    core.close()


def test_timestamps_are_monotonic_across_transitions():
    manager = _Manager()
    core = InProcessClient(manager)
    moments = iter(
        datetime(2030, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=index) for index in range(20)
    )
    runtime = SessionRuntime(core, runner=ControlledRunner(), clock=lambda: next(moments))
    events = []
    runtime.subscribe_events(events.append)
    try:
        runtime.open_session(
            OpenSessionRequest(connection_id=core.list_connections()[0].id),
            client_id=ClientId("client:a"),
        )
        timestamps = [event.timestamp for event in events]
        assert timestamps == sorted(timestamps)
    finally:
        runtime.shutdown()
        core.close()


def test_owned_subprocess_runner_uses_one_reaper_and_leaves_no_child():
    manager = _Manager()
    core = InProcessClient(manager)
    runner = SubprocessSessionProcessRunner(
        lambda _spec: (
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
        )
    )
    runtime = SessionRuntime(
        core,
        runner=runner,
        close_grace_seconds=0.5,
    )
    session = runtime.open_session(
        OpenSessionRequest(connection_id=core.list_connections()[0].id),
        client_id=ClientId("client:a"),
    )
    assert session.state is SessionState.RUNNING

    runtime.close_session(CloseSessionRequest(session_id=session.id))

    assert runtime.get_session(session.id).state is SessionState.CLOSED
    runtime.shutdown()
    assert not runner._thread.is_alive()
    core.close()

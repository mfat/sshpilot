import threading
import sys
from datetime import datetime, timedelta, timezone

import pytest

from sshpilot.core.connection_application_service import ConnectionApplicationService
from sshpilot.api import ErrorCode, SshPilotError
from sshpilot.api.events import EventType
from sshpilot.api.models.common import ClientId
from sshpilot.api.models.sessions import (
    AttachSessionRequest,
    CloseSessionRequest,
    DetachSessionRequest,
    OpenSessionRequest,
    SessionExitInfo,
    SessionFailure,
    SessionState,
)
from sshpilot.api.models.terminal import ResizeTerminalRequest, TerminalDimensions
from sshpilot.api.session_identity import new_session_id
from sshpilot.daemon.session_runtime import (
    SessionRuntime,
    SubprocessSessionProcessRunner,
    is_valid_session_transition,
)
from tests.helpers.fake_connection_repository import make_test_repository


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
    repo = make_test_repository()
    core = ConnectionApplicationService(repo, client_name="runtime-test")
    runner = ControlledRunner()
    runtime = SessionRuntime(core, runner=runner)
    yield runtime, core, runner
    runtime.shutdown()
    core.close()


def test_session_id_is_sequential_and_strict():
    identifier = new_session_id()
    assert identifier.startswith("session-")


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
    assert opened.id.startswith("session-")
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


def test_open_session_carries_remote_command_into_launch_spec(runtime_parts):
    runtime, core, runner = runtime_parts
    connection_id = core.list_connections()[0].id
    prepared = runtime.prepare_open_session(
        OpenSessionRequest(
            connection_id=connection_id,
            remote_command="docker exec -it web sh",
            force_tty=True,
        ),
        client_id=ClientId("client:a"),
    )

    runtime.start_session(prepared.id)

    assert len(runner.specs) == 1
    assert runner.specs[0].remote_command == "docker exec -it web sh"
    assert runner.specs[0].force_tty is True

    # Without a command the spec carries None (plain interactive shell).
    repo = make_test_repository()
    core2 = ConnectionApplicationService(repo, client_name="runtime-test")
    runner2 = ControlledRunner()
    runtime2 = SessionRuntime(core2, runner=runner2)
    try:
        prepared2 = runtime2.prepare_open_session(
            OpenSessionRequest(connection_id=core2.list_connections()[0].id),
            client_id=ClientId("client:a"),
        )
        runtime2.start_session(prepared2.id)
        assert runner2.specs[0].remote_command is None
        assert runner2.specs[0].force_tty is False
    finally:
        runtime2.shutdown()
        core2.close()


class _BlockingResizableHandle(ControlledHandle):
    """A handle that records every ``resize()`` call it receives."""

    def __init__(self, on_exit):
        super().__init__(on_exit, exit_on_terminate=False)
        self.resizes = []

    def resize(self, dimensions):
        self.resizes.append(dimensions)


class _BlockingTerminalRunner(ControlledRunner):
    """Terminal-capable runner whose ``start()`` blocks until released.

    Reproduces the startup window during which ``record.process_handle``
    is still ``None`` while ``self._runner.start(spec, ...)`` is in
    flight — the longer that call blocks (a distant host's SSH handshake
    takes far longer than a local one), the wider the window for a resize
    to race the handoff.
    """

    terminal_capable = True

    def __init__(self, *, started_event, release_event):
        super().__init__()
        self._started_event = started_event
        self._release_event = release_event

    def start(self, spec, on_exit, on_output=None, on_eof=None):
        self.specs.append(spec)
        self._started_event.set()
        assert self._release_event.wait(timeout=5), "test deadlocked"
        handle = _BlockingResizableHandle(on_exit)
        self.handles.append(handle)
        return handle


def test_resize_during_blocking_startup_reaches_the_published_handle():
    """Regression: a resize requested while the session is still STARTING
    and ``runner.start()`` hasn't returned yet must not be silently dropped.

    ``resize_terminal()`` has no live handle to call while
    ``record.process_handle`` is still ``None``, so it can only update
    ``record.launch_spec`` and return. ``start_session()`` had already read
    ``spec = record.launch_spec`` *before* calling the (blocking)
    ``runner.start(spec, ...)``, so that spec object never saw the update.
    Once the handle was published, nothing reconciled the two — the PTY
    stayed at whatever size ``spec`` had when the runner call started, and
    only a later, genuine further resize (nothing here, since the widget
    doesn't grow again on its own) would ever reach it. This is exactly the
    GH #1164 symptom reported on a distant host: the wider that host's SSH
    handshake makes the STARTING window, the more likely a resize sent as
    soon as the tab is connected lands inside it.
    """
    repo = make_test_repository()
    core = ConnectionApplicationService(repo)
    started_event = threading.Event()
    release_event = threading.Event()
    runner = _BlockingTerminalRunner(
        started_event=started_event, release_event=release_event
    )
    runtime = SessionRuntime(core, runner=runner)
    try:
        client_id = ClientId("client:a")
        prepared = runtime.prepare_open_session(
            OpenSessionRequest(
                connection_id=core.list_connections()[0].id,
                dimensions=TerminalDimensions(rows=24, columns=80),
            ),
            client_id=client_id,
        )
        attach_result = runtime.attach_session(
            AttachSessionRequest(session_id=prepared.id, request_input=True),
            client_id=client_id,
        )

        worker = threading.Thread(
            target=runtime.start_session, args=(prepared.id,)
        )
        worker.start()
        try:
            assert started_event.wait(timeout=5), "runner.start() never entered"

            # Fires while record.process_handle is still None.
            runtime.resize_terminal(
                ResizeTerminalRequest(
                    session_id=prepared.id,
                    attachment_id=attach_result.attachment.id,
                    dimensions=TerminalDimensions(rows=50, columns=200),
                ),
                client_id=client_id,
            )
        finally:
            release_event.set()
            worker.join(timeout=5)
            assert not worker.is_alive()

        assert runner.specs[0].dimensions == TerminalDimensions(
            rows=24, columns=80
        )
        handle = runner.handles[0]
        assert handle.resizes == [
            TerminalDimensions(rows=50, columns=200)
        ]
    finally:
        runtime.shutdown()
        core.close()


def test_newer_resize_after_publication_is_not_overwritten_by_a_delayed_startup_catchup():
    """Regression: the startup catch-up and a live resize_terminal() call
    can both decide to dispatch a resize for the same session once
    record.process_handle is published. Whichever *decided* first is not
    necessarily the one whose handle.resize() call executes first — a
    naive implementation that snapshots its desired dimensions once and
    applies them outside any further coordination could have its
    (older, stale) snapshot physically land *after* a newer resize's own
    call, silently reverting the terminal to a smaller size.

    This drives that exact interleaving deterministically: the startup
    catch-up is parked right as it starts dispatching (before it reads
    the dimensions it will send), a newer resize_terminal() call is sent
    and fully applied while it's parked, and only then is the catch-up
    released. The final applied size must be the newer one — proving
    dispatch always re-reads the current desired size rather than acting
    on a value decided earlier.
    """
    repo = make_test_repository()
    core = ConnectionApplicationService(repo)
    started_event = threading.Event()
    release_event = threading.Event()
    runner = _BlockingTerminalRunner(
        started_event=started_event, release_event=release_event
    )
    runtime = SessionRuntime(core, runner=runner)
    try:
        client_id = ClientId("client:a")
        prepared = runtime.prepare_open_session(
            OpenSessionRequest(
                connection_id=core.list_connections()[0].id,
                dimensions=TerminalDimensions(rows=24, columns=80),
            ),
            client_id=client_id,
        )
        attach_result = runtime.attach_session(
            AttachSessionRequest(session_id=prepared.id, request_input=True),
            client_id=client_id,
        )

        worker = threading.Thread(
            target=runtime.start_session, args=(prepared.id,)
        )
        worker.start()
        assert started_event.wait(timeout=5), "runner.start() never entered"

        # Decided while record.process_handle is still None -- this is
        # what the startup catch-up will (eventually) act on.
        runtime.resize_terminal(
            ResizeTerminalRequest(
                session_id=prepared.id,
                attachment_id=attach_result.attachment.id,
                dimensions=TerminalDimensions(rows=50, columns=200),
            ),
            client_id=client_id,
        )

        # Park the *first* call into _dispatch_resize (the startup
        # catch-up, triggered as soon as the handle is published) right
        # before it does its work, so a second, newer resize can
        # complete first.
        entered_first_dispatch = threading.Event()
        release_first_dispatch = threading.Event()
        call_count = {"n": 0}
        original_dispatch = runtime._dispatch_resize

        def _tracking_dispatch(session_id):
            call_count["n"] += 1
            if call_count["n"] == 1:
                entered_first_dispatch.set()
                assert release_first_dispatch.wait(timeout=5), "test deadlocked"
            return original_dispatch(session_id)

        runtime._dispatch_resize = _tracking_dispatch

        release_event.set()  # let runner.start() return and publish the handle
        assert entered_first_dispatch.wait(timeout=5), (
            "startup catch-up dispatch never started"
        )

        # A newer resize arrives and is fully applied while the startup
        # catch-up (decided on the older 50x200 size) is still parked.
        runtime.resize_terminal(
            ResizeTerminalRequest(
                session_id=prepared.id,
                attachment_id=attach_result.attachment.id,
                dimensions=TerminalDimensions(rows=55, columns=220),
            ),
            client_id=client_id,
        )

        release_first_dispatch.set()
        worker.join(timeout=5)
        assert not worker.is_alive()

        handle = runner.handles[0]
        assert handle.resizes, "no resize ever reached the handle"
        assert handle.resizes[-1] == TerminalDimensions(rows=55, columns=220)
    finally:
        runtime.shutdown()
        core.close()


class _SlowFirstResizeHandle(ControlledHandle):
    """A resize-capable handle whose first ``resize()`` call blocks.

    Simulates a genuinely slow (e.g. I/O-bound) resize implementation —
    not a test-only hook in ``SessionRuntime`` itself — to prove
    ``_dispatch_resize``'s per-session lock is held across the *entire*
    read-then-apply pair, not just the read: a second dispatch attempt
    that starts while the first is still inside ``resize()`` must
    genuinely block on the lock, not race ahead of it.
    """

    def __init__(self, on_exit, *, entered_event, release_event):
        super().__init__(on_exit, exit_on_terminate=False)
        self.resizes = []
        self._entered_event = entered_event
        self._release_event = release_event
        self._first = True

    def resize(self, dimensions):
        if self._first:
            self._first = False
            self._entered_event.set()
            assert self._release_event.wait(timeout=5), "test deadlocked"
        self.resizes.append(dimensions)


def test_dispatch_resize_serializes_through_the_actual_resize_call():
    """Regression: dispatch must stay locked for the whole read-then-apply
    pair, not release the lock as soon as it has read the desired size.
    If it didn't, a second dispatch could read *and apply* a value while
    the first is still mid-call, and whichever of the two ``resize()``
    calls physically finishes last would win — independent of which one
    was decided last. Here the first dispatch's ``resize()`` call itself
    blocks; a second dispatch started while it's blocked must wait for
    the lock, and once unblocked, both calls must land in decision order
    with the second one carrying the size that was current when *it*
    ran, not a value staled by waiting.
    """
    repo = make_test_repository()
    core = ConnectionApplicationService(repo)
    entered_event = threading.Event()
    release_event = threading.Event()
    handles = []

    class _Runner(ControlledRunner):
        terminal_capable = True

        def start(self, spec, on_exit, on_output=None, on_eof=None):
            self.specs.append(spec)
            handle = _SlowFirstResizeHandle(
                on_exit, entered_event=entered_event, release_event=release_event
            )
            handles.append(handle)
            return handle

    runtime = SessionRuntime(core, runner=_Runner())
    try:
        client_id = ClientId("client:a")
        opened = runtime.open_session(
            OpenSessionRequest(
                connection_id=core.list_connections()[0].id,
                dimensions=TerminalDimensions(rows=24, columns=80),
            ),
            client_id=client_id,
        )
        attach_result = runtime.attach_session(
            AttachSessionRequest(session_id=opened.id, request_input=True),
            client_id=client_id,
        )

        first = threading.Thread(
            target=runtime.resize_terminal,
            args=(
                ResizeTerminalRequest(
                    session_id=opened.id,
                    attachment_id=attach_result.attachment.id,
                    dimensions=TerminalDimensions(rows=50, columns=200),
                ),
            ),
            kwargs={"client_id": client_id},
        )
        first.start()
        assert entered_event.wait(timeout=5), "first resize() never entered"

        second = threading.Thread(
            target=runtime.resize_terminal,
            args=(
                ResizeTerminalRequest(
                    session_id=opened.id,
                    attachment_id=attach_result.attachment.id,
                    dimensions=TerminalDimensions(rows=55, columns=220),
                ),
            ),
            kwargs={"client_id": client_id},
        )
        second.start()

        release_event.set()
        first.join(timeout=5)
        second.join(timeout=5)
        assert not first.is_alive() and not second.is_alive()

        handle = handles[0]
        assert handle.resizes == [
            TerminalDimensions(rows=50, columns=200),
            TerminalDimensions(rows=55, columns=220),
        ]
    finally:
        runtime.shutdown()
        core.close()


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
    repo = make_test_repository()
    core = ConnectionApplicationService(repo)
    runtime = SessionRuntime(core, runner=ControlledRunner(fail=True))
    try:
        opened = runtime.open_session(
            OpenSessionRequest(connection_id=core.list_connections()[0].id),
            client_id=ClientId("client:a"),
        )
        assert opened.state is SessionState.FAILED
        assert type(opened.failure) is SessionFailure
        assert opened.failure.code == ErrorCode.SESSION_STARTUP_FAILED.value
        assert opened.failure.message == "The session process could not be started"
        assert "sensitive" not in repr(opened)
    finally:
        runtime.shutdown()
        core.close()


def test_open_rejects_missing_connection_and_accepts_provider_protocol():
    repo = make_test_repository()
    core = ConnectionApplicationService(repo)
    runtime = SessionRuntime(core, runner=ControlledRunner())
    try:
        with pytest.raises(SshPilotError) as missing:
            runtime.open_session(
                OpenSessionRequest(connection_id="missing"),
                client_id=ClientId("client:a"),
            )
        assert missing.value.code is ErrorCode.CONNECTION_NOT_FOUND

        repo.get_record("demo").protocol = "telnet"
        opened = runtime.open_session(
            OpenSessionRequest(connection_id=core.list_connections()[0].id),
            client_id=ClientId("client:a"),
        )
        assert opened.id is not None
        assert runtime.list_sessions()[0].id == opened.id
    finally:
        runtime.shutdown()
        core.close()


def test_process_exit_before_runner_returns_does_not_retain_a_stale_handle():
    repo = make_test_repository()
    core = ConnectionApplicationService(repo)
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
    assert runtime.client_can_interact(session.id, ClientId("client:a")) is True
    assert runtime.client_can_interact(session.id, ClientId("client:b")) is True
    assert runtime.client_can_interact(session.id, ClientId("client:c")) is False

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
    assert runtime.client_can_interact(session.id, ClientId("client:b")) is False

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
    repo = make_test_repository()
    core = ConnectionApplicationService(repo)
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
    repo = make_test_repository()
    core = ConnectionApplicationService(repo)
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
    repo = make_test_repository()
    core = ConnectionApplicationService(repo)
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
    repo = make_test_repository()
    core = ConnectionApplicationService(repo)
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


def test_global_replay_budget_trims_oldest_sessions_first():
    repo = make_test_repository()
    core = ConnectionApplicationService(repo)
    runtime = SessionRuntime(
        core,
        runner=ControlledRunner(),
        replay_bytes=64,
        global_replay_bytes=20,
    )
    connection_id = core.list_connections()[0].id
    older = runtime.open_session(
        OpenSessionRequest(connection_id=connection_id),
        client_id=ClientId("client:a"),
    )
    newer = runtime.open_session(
        OpenSessionRequest(connection_id=connection_id),
        client_id=ClientId("client:a"),
    )
    runtime._terminal_output(older.id, b"0123456789")
    runtime._terminal_output(newer.id, b"ABCDEFGHIJ")
    runtime._terminal_output(newer.id, b"abcdefghij")

    older_record = runtime._records[older.id]
    newer_record = runtime._records[newer.id]
    total = older_record.replay.retained_bytes + newer_record.replay.retained_bytes
    assert total <= 20
    assert older_record.replay.retained_bytes < 10
    assert newer_record.replay.retained_bytes > 0
    runtime.shutdown()
    core.close()


def test_timestamps_are_monotonic_across_transitions():
    repo = make_test_repository()
    core = ConnectionApplicationService(repo)
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
    repo = make_test_repository()
    core = ConnectionApplicationService(repo)
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

"""Daemon-owned session identity, lifecycle, and process ownership."""

from __future__ import annotations

import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Mapping, Optional, Protocol, Sequence

from sshpilot.api.client import SshPilotClient
from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.events import (
    CoreEvent,
    CoreEventCallback,
    EventPublisher,
    EventType,
    Subscription,
)
from sshpilot.api.models.common import (
    AttachmentId,
    ClientId,
    ConnectionId,
    SessionId,
    utc_now,
)
from sshpilot.api.models.connections import ConnectionDetails
from sshpilot.api.models.sessions import (
    AttachSessionRequest,
    AttachSessionResult,
    AttachmentInfo,
    CloseSessionRequest,
    DetachSessionRequest,
    OpenSessionRequest,
    SessionCapabilities,
    SessionExitInfo,
    SessionFailure,
    SessionState,
    SessionSummary,
)
from sshpilot.api.session_identity import (
    new_session_uuid,
    session_id_from_uuid,
    session_uuid_from_id,
)


DEFAULT_CLOSE_GRACE_SECONDS = 0.5
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 2.0
DEFAULT_MAX_RETAINED_CLOSED_SESSIONS = 100


@dataclass(frozen=True)
class SessionLaunchSpec:
    """Safe daemon-derived metadata supplied to a process runner."""

    session_id: SessionId
    connection_id: ConnectionId
    protocol: str
    hostname: str
    username: str
    port: int


class SessionProcessHandle(Protocol):
    """Exact process resource owned by one session runtime record."""

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float) -> Optional[SessionExitInfo]:
        """Return exit information, or ``None`` if still running at timeout."""
        ...


SessionExitCallback = Callable[[SessionExitInfo], None]


class SessionProcessRunner(Protocol):
    """Narrow launch boundary; terminal transport is intentionally absent."""

    def start(
        self,
        spec: SessionLaunchSpec,
        on_exit: SessionExitCallback,
    ) -> SessionProcessHandle: ...

    def close(self) -> None: ...


class UnsupportedSessionProcessRunner:
    """Production Phase 6 runner until prompt-safe PTY startup is implemented."""

    def start(
        self,
        spec: SessionLaunchSpec,
        on_exit: SessionExitCallback,
    ) -> SessionProcessHandle:
        del on_exit
        raise SshPilotError(
            ErrorCode.SESSION_STARTUP_FAILED,
            "Session startup requires terminal runtime support",
            retryable=False,
            connection_id=spec.connection_id,
            session_id=spec.session_id,
        )

    def close(self) -> None:
        return


class _OwnedSubprocessHandle:
    def __init__(
        self,
        process: subprocess.Popen,
        on_exit: SessionExitCallback,
        unregister: Callable[["_OwnedSubprocessHandle"], None],
    ) -> None:
        self._process = process
        self._on_exit = on_exit
        self._unregister = unregister
        self._lock = threading.Lock()
        self._notified = False

    def terminate(self) -> None:
        with self._lock:
            if self._process.poll() is None:
                self._process.terminate()

    def kill(self) -> None:
        with self._lock:
            if self._process.poll() is None:
                self._process.kill()

    def wait(self, timeout: float) -> Optional[SessionExitInfo]:
        try:
            return_code = self._process.wait(timeout=max(0.0, timeout))
        except subprocess.TimeoutExpired:
            return None
        exit_info = self._exit_info(return_code)
        self._notify(exit_info)
        return exit_info

    def poll_and_notify(self) -> bool:
        return_code = self._process.poll()
        if return_code is None:
            return False
        self._notify(self._exit_info(return_code))
        return True

    def _notify(self, exit_info: SessionExitInfo) -> None:
        with self._lock:
            if self._notified:
                return
            self._notified = True
        self._unregister(self)
        self._on_exit(exit_info)

    @staticmethod
    def _exit_info(return_code: int) -> SessionExitInfo:
        if return_code < 0:
            return SessionExitInfo(signal=-return_code, reason="process_signal")
        return SessionExitInfo(exit_code=return_code, reason="process_exit")


class SubprocessSessionProcessRunner:
    """Owned subprocess runner with one shared bounded-lifetime reaper thread.

    The command and environment builders are daemon-internal injection points;
    neither is reachable through the wire protocol. The default environment is
    empty so caller secrets are not inherited accidentally.
    """

    def __init__(
        self,
        command_builder: Callable[[SessionLaunchSpec], Sequence[str]],
        *,
        environment_builder: Optional[Callable[[SessionLaunchSpec], Mapping[str, str]]] = None,
        poll_interval: float = 0.02,
    ) -> None:
        if not callable(command_builder):
            raise TypeError("session command builder must be callable")
        if poll_interval <= 0:
            raise ValueError("session process poll interval must be positive")
        self._command_builder = command_builder
        self._environment_builder = environment_builder
        self._poll_interval = float(poll_interval)
        self._condition = threading.Condition()
        self._handles: set[_OwnedSubprocessHandle] = set()
        self._closed = False
        self._thread = threading.Thread(
            target=self._reaper_main,
            name="sshpilot-session-reaper",
            daemon=True,
        )
        self._thread.start()

    def start(
        self,
        spec: SessionLaunchSpec,
        on_exit: SessionExitCallback,
    ) -> SessionProcessHandle:
        argv = tuple(self._command_builder(spec))
        if not argv or any(type(item) is not str or not item for item in argv):
            raise ValueError("session process command must be a non-empty argv")
        environment = (
            dict(self._environment_builder(spec)) if self._environment_builder is not None else {}
        )
        if any(
            type(key) is not str or type(value) is not str or "\0" in key or "\0" in value
            for key, value in environment.items()
        ):
            raise ValueError("session process environment is invalid")
        with self._condition:
            if self._closed:
                raise RuntimeError("session process runner is closed")
        process = subprocess.Popen(
            argv,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            close_fds=True,
        )
        handle = _OwnedSubprocessHandle(process, on_exit, self._unregister)
        with self._condition:
            if self._closed:
                process.kill()
                process.wait()
                raise RuntimeError("session process runner is closed")
            self._handles.add(handle)
            self._condition.notify()
        return handle

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            handles = tuple(self._handles)
            self._condition.notify_all()
        for handle in handles:
            try:
                handle.kill()
                handle.wait(0.5)
            except Exception:
                continue
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)

    def _unregister(self, handle: _OwnedSubprocessHandle) -> None:
        with self._condition:
            self._handles.discard(handle)
            self._condition.notify_all()

    def _reaper_main(self) -> None:
        while True:
            with self._condition:
                if self._closed and not self._handles:
                    return
                handles = tuple(self._handles)
                if not handles:
                    self._condition.wait()
                    continue
            for handle in handles:
                handle.poll_and_notify()
            with self._condition:
                if self._handles and not self._closed:
                    self._condition.wait(self._poll_interval)


@dataclass
class _SessionRecord:
    session_id: SessionId
    connection_id: ConnectionId
    state: SessionState
    created_at: datetime
    updated_at: datetime
    started_at: Optional[datetime] = None
    exited_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    exit_info: Optional[SessionExitInfo] = None
    failure: Optional[SessionFailure] = None
    attachments: Dict[ClientId, AttachmentId] = field(default_factory=dict)
    process_handle: Optional[SessionProcessHandle] = None


_ALLOWED_TRANSITIONS = {
    SessionState.CREATED: frozenset(
        {SessionState.STARTING, SessionState.FAILED, SessionState.CLOSED}
    ),
    SessionState.STARTING: frozenset(
        {
            SessionState.RUNNING,
            SessionState.CLOSING,
            SessionState.EXITED,
            SessionState.FAILED,
        }
    ),
    SessionState.RUNNING: frozenset(
        {SessionState.CLOSING, SessionState.EXITED, SessionState.FAILED}
    ),
    SessionState.CLOSING: frozenset(
        {SessionState.EXITED, SessionState.FAILED, SessionState.CLOSED}
    ),
    SessionState.EXITED: frozenset({SessionState.CLOSED}),
    SessionState.FAILED: frozenset({SessionState.CLOSED}),
    SessionState.CLOSED: frozenset(),
}


def is_valid_session_transition(
    current: SessionState,
    target: SessionState,
) -> bool:
    """Return whether the explicit daemon lifecycle accepts one transition."""

    if not isinstance(current, SessionState) or not isinstance(target, SessionState):
        raise TypeError("session transitions require SessionState values")
    return target in _ALLOWED_TRANSITIONS[current]


class SessionRuntime:
    """Serialize daemon-lifetime session state and exact owned resources."""

    def __init__(
        self,
        core_client: SshPilotClient,
        *,
        runner: Optional[SessionProcessRunner] = None,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        uuid_factory: Callable[[], str] = new_session_uuid,
        close_grace_seconds: float = DEFAULT_CLOSE_GRACE_SECONDS,
        shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
        max_retained_closed_sessions: int = DEFAULT_MAX_RETAINED_CLOSED_SESSIONS,
    ) -> None:
        if close_grace_seconds < 0 or shutdown_timeout_seconds < 0:
            raise ValueError("session close timeouts must not be negative")
        if type(max_retained_closed_sessions) is not int or max_retained_closed_sessions < 0:
            raise ValueError("closed-session retention limit must not be negative")
        self._core_client = core_client
        self._runner = runner or UnsupportedSessionProcessRunner()
        self._clock = clock
        self._monotonic = monotonic
        self._uuid_factory = uuid_factory
        self._close_grace_seconds = float(close_grace_seconds)
        self._shutdown_timeout_seconds = float(shutdown_timeout_seconds)
        self._max_retained_closed_sessions = max_retained_closed_sessions
        self._lock = threading.RLock()
        self._publisher = EventPublisher()
        self._records: Dict[SessionId, _SessionRecord] = {}
        self._creation_order: List[SessionId] = []
        self._accepting_commands = True
        self._closed = False

    def subscribe_events(self, callback: CoreEventCallback) -> Subscription:
        with self._lock:
            if self._closed:
                raise SshPilotError(
                    ErrorCode.INVALID_REQUEST,
                    "The session runtime is closed",
                )
        return self._publisher.subscribe(callback)

    def list_sessions(self) -> List[SessionSummary]:
        with self._lock:
            self._require_accepting_reads_locked()
            return [
                self._summary_locked(self._records[session_id])
                for session_id in self._creation_order
                if session_id in self._records
            ]

    def get_session(self, session_id: SessionId) -> SessionSummary:
        session_uuid_from_id(session_id)
        with self._lock:
            self._require_accepting_reads_locked()
            return self._summary_locked(self._record_locked(session_id))

    def open_session(
        self,
        request: OpenSessionRequest,
        *,
        client_id: ClientId,
    ) -> SessionSummary:
        if type(request) is not OpenSessionRequest:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "An open session request is required",
            )
        connection = self._core_client.get_connection(request.connection_id)
        if connection.protocol != "ssh":
            raise SshPilotError(
                ErrorCode.UNSUPPORTED_SESSION_PROTOCOL,
                "The connection protocol cannot start a daemon session",
                connection_id=request.connection_id,
            )
        session_id = SessionId(session_id_from_uuid(self._uuid_factory()))
        now = self._clock()
        record = _SessionRecord(
            session_id=session_id,
            connection_id=connection.id,
            state=SessionState.CREATED,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._require_accepting_commands_locked()
            if session_id in self._records:
                raise RuntimeError("session UUID factory reused an active identifier")
            self._records[session_id] = record
            self._creation_order.append(session_id)
            created_event = self._event_locked(record, EventType.SESSION_CREATED)
            starting_event = self._transition_locked(record, SessionState.STARTING)
        self._publish((created_event, starting_event))

        with self._lock:
            if record.state is not SessionState.STARTING:
                return self._summary_locked(record)
        spec = self._launch_spec(connection, session_id)
        try:
            handle = self._runner.start(
                spec,
                lambda exit_info: self._process_exited(session_id, exit_info),
            )
            if handle is None:
                raise TypeError("session runner returned no process handle")
        except SshPilotError as error:
            self._startup_failed(
                record,
                error.code,
                "The session process could not be started",
            )
        except Exception:
            self._startup_failed(
                record,
                ErrorCode.SESSION_STARTUP_FAILED,
                "The session process could not be started",
            )
        else:
            terminate_after_start = False
            events: List[CoreEvent] = []
            with self._lock:
                if record.state is SessionState.STARTING:
                    record.process_handle = handle
                    record.started_at = self._clock()
                    events.append(self._transition_locked(record, SessionState.RUNNING))
                elif record.state is SessionState.CLOSING:
                    record.process_handle = handle
                    terminate_after_start = True
            self._publish(events)
            if terminate_after_start:
                self._terminate_record(
                    record,
                    deadline=self._monotonic() + self._close_grace_seconds,
                    raise_on_failure=False,
                )
        with self._lock:
            return self._summary_locked(record)

    def attach_session(
        self,
        request: AttachSessionRequest,
        *,
        client_id: ClientId,
    ) -> AttachSessionResult:
        if type(request) is not AttachSessionRequest:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "An attach session request is required",
            )
        session_uuid_from_id(request.session_id)
        with self._lock:
            self._require_accepting_commands_locked()
            record = self._record_locked(request.session_id)
            if record.state in {
                SessionState.EXITED,
                SessionState.FAILED,
                SessionState.CLOSED,
            }:
                raise SshPilotError(
                    ErrorCode.SESSION_ALREADY_CLOSED,
                    "The session no longer accepts attachments",
                    session_id=request.session_id,
                )
            attachment_id = record.attachments.get(client_id)
            if attachment_id is None:
                attachment_id = AttachmentId(f"attachment:{uuid.uuid4()}")
                record.attachments[client_id] = attachment_id
                record.updated_at = self._clock()
            return AttachSessionResult(
                session=self._summary_locked(record),
                attachment=AttachmentInfo(
                    id=attachment_id,
                    session_id=record.session_id,
                    client_id=client_id,
                    input_owner=False,
                ),
            )

    def detach_session(
        self,
        request: DetachSessionRequest,
        *,
        client_id: ClientId,
    ) -> None:
        if type(request) is not DetachSessionRequest:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "A detach session request is required",
            )
        session_uuid_from_id(request.session_id)
        with self._lock:
            self._require_accepting_commands_locked()
            record = self._record_locked(request.session_id)
            current = record.attachments.get(client_id)
            if current is None:
                return
            if current != request.attachment_id:
                raise SshPilotError(
                    ErrorCode.PERMISSION_DENIED,
                    "The attachment does not belong to this client",
                    session_id=request.session_id,
                )
            del record.attachments[client_id]
            record.updated_at = self._clock()

    def close_session(self, request: CloseSessionRequest) -> None:
        if type(request) is not CloseSessionRequest:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "A close session request is required",
            )
        session_uuid_from_id(request.session_id)
        self._close_session_id(
            request.session_id,
            deadline=self._monotonic() + self._close_grace_seconds,
            raise_on_failure=True,
        )

    def detach_client(self, client_id: Optional[ClientId]) -> None:
        if client_id is None:
            return
        with self._lock:
            for record in self._records.values():
                if record.attachments.pop(client_id, None) is not None:
                    record.updated_at = self._clock()

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._accepting_commands = False
            session_ids = tuple(self._creation_order)
        deadline = self._monotonic() + self._shutdown_timeout_seconds
        for session_id in session_ids:
            self._close_session_id(
                session_id,
                deadline=deadline,
                raise_on_failure=False,
                allow_shutdown=True,
            )
        with self._lock:
            for record in self._records.values():
                record.attachments.clear()
            self._closed = True
        try:
            self._runner.close()
        finally:
            self._publisher.close()

    def _close_session_id(
        self,
        session_id: SessionId,
        *,
        deadline: float,
        raise_on_failure: bool,
        allow_shutdown: bool = False,
    ) -> None:
        events: List[CoreEvent] = []
        handle: Optional[SessionProcessHandle] = None
        with self._lock:
            if not allow_shutdown:
                self._require_accepting_commands_locked()
            record = self._record_locked(session_id)
            if record.state is SessionState.CLOSED:
                return
            if record.state in {SessionState.CREATED, SessionState.EXITED}:
                events.append(self._transition_locked(record, SessionState.CLOSED))
            elif record.state is SessionState.FAILED:
                handle = record.process_handle
                if handle is None:
                    events.append(self._transition_locked(record, SessionState.CLOSED))
            elif record.state in {SessionState.STARTING, SessionState.RUNNING}:
                events.append(self._transition_locked(record, SessionState.CLOSING))
                handle = record.process_handle
            elif record.state is SessionState.CLOSING:
                handle = record.process_handle
        self._publish(events)
        if handle is None:
            with self._lock:
                if record.state is SessionState.CLOSING:
                    events = [self._transition_locked(record, SessionState.CLOSED)]
                else:
                    events = []
            self._publish(events)
            return
        self._terminate_record(
            record,
            deadline=deadline,
            raise_on_failure=raise_on_failure,
        )

    def _terminate_record(
        self,
        record: _SessionRecord,
        *,
        deadline: float,
        raise_on_failure: bool,
    ) -> None:
        handle = record.process_handle
        if handle is None:
            return
        try:
            handle.terminate()
            remaining = max(0.0, deadline - self._monotonic())
            exit_info = handle.wait(remaining)
            if exit_info is None:
                handle.kill()
                remaining = max(0.0, deadline - self._monotonic())
                exit_info = handle.wait(remaining)
            if exit_info is None:
                raise RuntimeError("owned session process did not exit")
        except Exception:
            events: List[CoreEvent] = []
            with self._lock:
                if record.state not in {
                    SessionState.FAILED,
                    SessionState.CLOSED,
                }:
                    record.failure = SessionFailure(
                        code=ErrorCode.SESSION_TERMINATION_FAILED.value,
                        message="The session process could not be terminated",
                    )
                    events.append(self._transition_locked(record, SessionState.FAILED))
            self._publish(events)
            if raise_on_failure:
                raise SshPilotError(
                    ErrorCode.SESSION_TERMINATION_FAILED,
                    "The session process could not be terminated",
                    session_id=record.session_id,
                ) from None
            return
        self._process_exited(record.session_id, exit_info)

    def _process_exited(
        self,
        session_id: SessionId,
        exit_info: SessionExitInfo,
    ) -> None:
        if type(exit_info) is not SessionExitInfo:
            exit_info = SessionExitInfo(reason="process_exit")
        events: List[CoreEvent] = []
        with self._lock:
            record = self._records.get(session_id)
            if record is None or record.state in {
                SessionState.EXITED,
                SessionState.CLOSED,
            }:
                return
            record.exit_info = exit_info
            record.process_handle = None
            if record.state is SessionState.FAILED:
                events.append(
                    self._event_locked(
                        record,
                        EventType.SESSION_EXITED,
                        payload=exit_info,
                    )
                )
                events.append(self._transition_locked(record, SessionState.CLOSED))
            else:
                events.append(self._transition_locked(record, SessionState.EXITED))
                events.append(self._transition_locked(record, SessionState.CLOSED))
        self._publish(events)

    def _startup_failed(
        self,
        record: _SessionRecord,
        code: ErrorCode,
        message: str,
    ) -> None:
        with self._lock:
            if record.state is not SessionState.STARTING:
                return
            record.failure = SessionFailure(code=code.value, message=message)
            event = self._transition_locked(record, SessionState.FAILED)
        self._publish((event,))

    def _transition_locked(
        self,
        record: _SessionRecord,
        new_state: SessionState,
    ) -> CoreEvent:
        if not is_valid_session_transition(record.state, new_state):
            raise RuntimeError(
                f"invalid session transition {record.state.value}->{new_state.value}"
            )
        now = self._clock()
        record.state = new_state
        record.updated_at = now
        if new_state is SessionState.EXITED:
            record.exited_at = now
        elif new_state is SessionState.CLOSED:
            record.closed_at = now
            record.attachments.clear()
            self._evict_closed_locked()
        event_type = EventType.SESSION_STATE_CHANGED
        payload = self._summary_locked(record)
        if new_state is SessionState.EXITED:
            event_type = EventType.SESSION_EXITED
            payload = record.exit_info or SessionExitInfo(reason="process_exit")
        elif new_state is SessionState.CLOSED:
            event_type = EventType.SESSION_CLOSED
        return self._event_locked(record, event_type, payload=payload)

    def _event_locked(
        self,
        record: _SessionRecord,
        event_type: EventType,
        *,
        payload=None,
    ) -> CoreEvent:
        return CoreEvent(
            type=event_type,
            payload=payload if payload is not None else self._summary_locked(record),
            sequence=0,
            connection_id=record.connection_id,
            session_id=record.session_id,
        )

    def _publish(self, events) -> None:
        for event in events:
            try:
                self._publisher.publish(
                    event.type,
                    event.payload,
                    request_id=event.request_id,
                    connection_id=event.connection_id,
                    session_id=event.session_id,
                )
            except RuntimeError:
                return

    def _record_locked(self, session_id: SessionId) -> _SessionRecord:
        record = self._records.get(session_id)
        if record is None:
            raise SshPilotError(
                ErrorCode.SESSION_NOT_FOUND,
                "The requested session does not exist",
                session_id=session_id,
            )
        return record

    @staticmethod
    def _summary_locked(record: _SessionRecord) -> SessionSummary:
        return SessionSummary(
            id=record.session_id,
            connection_id=record.connection_id,
            state=record.state,
            created_at=record.created_at,
            capabilities=SessionCapabilities(),
            exit_info=record.exit_info,
            failure=record.failure,
            attachment_count=len(record.attachments),
        )

    def _evict_closed_locked(self) -> None:
        closed = [
            session_id
            for session_id in self._creation_order
            if (
                session_id in self._records
                and self._records[session_id].state is SessionState.CLOSED
            )
        ]
        excess = len(closed) - self._max_retained_closed_sessions
        for session_id in closed[: max(0, excess)]:
            self._records.pop(session_id, None)
            try:
                self._creation_order.remove(session_id)
            except ValueError:
                pass

    @staticmethod
    def _launch_spec(
        connection: ConnectionDetails,
        session_id: SessionId,
    ) -> SessionLaunchSpec:
        return SessionLaunchSpec(
            session_id=session_id,
            connection_id=connection.id,
            protocol=connection.protocol,
            hostname=connection.hostname,
            username=connection.username,
            port=connection.port,
        )

    def _require_accepting_commands_locked(self) -> None:
        if not self._accepting_commands or self._closed:
            raise SshPilotError(
                ErrorCode.DAEMON_SHUTTING_DOWN,
                "The session runtime is shutting down",
                retryable=True,
            )

    def _require_accepting_reads_locked(self) -> None:
        if self._closed:
            raise SshPilotError(
                ErrorCode.DAEMON_SHUTTING_DOWN,
                "The session runtime is shut down",
                retryable=True,
            )

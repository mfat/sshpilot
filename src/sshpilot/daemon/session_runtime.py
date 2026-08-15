"""Daemon-owned session identity, lifecycle, and process ownership."""

from __future__ import annotations

import subprocess
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from collections import defaultdict
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Set, Tuple

from sshpilot.api.client import SshPilotClient
from sshpilot.runtime_identity import new_attachment_id
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
    require_identifier,
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
from sshpilot.api.models.terminal import (
    BroadcastTerminalInputRequest,
    ReplayBounds,
    ReplayRequest,
    ReplayResult,
    ResizeTerminalRequest,
    TerminalDimensions,
    TerminalInput,
    TerminalOutput,
)
from sshpilot.api.session_identity import new_session_id
from sshpilot.core.connection_evidence import classify_connection_evidence
from sshpilot.core.ssh_diagnostics import SshDiagnosticResult, SshDiagnosticState
from sshpilot.logging_support import log_context

from .terminal_stream import (
    DEFAULT_GLOBAL_REPLAY_BYTES,
    DEFAULT_SESSION_REPLAY_BYTES,
    ReplaySlice,
    TerminalReplayBuffer,
)


DEFAULT_CLOSE_GRACE_SECONDS = 0.5
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 2.0
DEFAULT_MAX_RETAINED_CLOSED_SESSIONS = 100

logger = logging.getLogger(__name__)


@dataclass
class _AttachmentRecord:
    """Individual attachment record for multi-attachment support."""
    id: AttachmentId
    client_id: ClientId
    want_output: bool = False
    is_input_owner: bool = False


@dataclass(frozen=True)
class SessionLaunchSpec:
    """Safe daemon-derived metadata supplied to a process runner."""

    session_id: SessionId
    connection_id: ConnectionId
    protocol: str
    hostname: str
    username: str
    port: int
    dimensions: Optional[TerminalDimensions] = None
    remote_command: Optional[str] = None
    force_tty: bool = False


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
    attachments: Dict[AttachmentId, _AttachmentRecord] = field(default_factory=dict)
    client_attachments: Dict[ClientId, Set[AttachmentId]] = field(default_factory=lambda: defaultdict(set))
    process_handle: Optional[SessionProcessHandle] = None
    launch_spec: Optional[SessionLaunchSpec] = None
    startup_scheduled: bool = False
    startup_started: bool = False
    close_scheduled: bool = False
    replay: TerminalReplayBuffer = field(default_factory=TerminalReplayBuffer)
    terminal_capable: bool = False
    pty_eof: bool = False
    input_owner_attachment_id: Optional[AttachmentId] = None
    output_clients: Set[ClientId] = field(default_factory=set)
    originating_client_id: Optional[ClientId] = None
    # Output produced before RUNNING by the legacy blocking auth gate. The
    # event-driven connection-evidence gate streams STARTING output directly.
    deferred_live_output: List[TerminalOutput] = field(default_factory=list)
    # OpenSSH diagnostics readiness (see SshReadinessManager). When engaged,
    # the private ``-v -E`` stream is the authoritative readiness source and
    # PTY connection evidence is demoted to a fallback.
    readiness_engaged: bool = False
    diagnostic_primary: bool = False
    pty_fallback: bool = False
    authenticated: bool = False
    diagnostic_failure_detail: Optional[str] = None
    diagnostic_result: Optional[SshDiagnosticResult] = None


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
        runner: Optional[Any] = None,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        id_factory: Callable[[], str] = new_session_id,
        close_grace_seconds: float = DEFAULT_CLOSE_GRACE_SECONDS,
        shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
        max_retained_closed_sessions: int = DEFAULT_MAX_RETAINED_CLOSED_SESSIONS,
        replay_bytes: int = DEFAULT_SESSION_REPLAY_BYTES,
        global_replay_bytes: int = DEFAULT_GLOBAL_REPLAY_BYTES,
        readiness_manager: Optional[Any] = None,
    ) -> None:
        if close_grace_seconds < 0 or shutdown_timeout_seconds < 0:
            raise ValueError("session close timeouts must not be negative")
        if type(max_retained_closed_sessions) is not int or max_retained_closed_sessions < 0:
            raise ValueError("closed-session retention limit must not be negative")
        self._core_client = core_client
        self._runner: Any = runner or UnsupportedSessionProcessRunner()
        self._clock = clock
        self._monotonic = monotonic
        self._id_factory = id_factory
        self._close_grace_seconds = float(close_grace_seconds)
        self._shutdown_timeout_seconds = float(shutdown_timeout_seconds)
        self._max_retained_closed_sessions = max_retained_closed_sessions
        if type(replay_bytes) is not int or replay_bytes < 1:
            raise ValueError("terminal replay byte limit must be positive")
        if type(global_replay_bytes) is not int or global_replay_bytes < 1:
            raise ValueError("global terminal replay byte limit must be positive")
        self._replay_bytes = replay_bytes
        self._global_replay_bytes = global_replay_bytes
        # Optional auth gate: (session_id, is_alive) -> bool. When set,
        # RUNNING is deferred until ControlMaster proves authentication.
        self._auth_gate: Optional[Callable[..., bool]] = None
        self._authenticated_callback: Optional[Callable[[SessionId], None]] = None
        self._auth_gate_timeout_seconds = 60.0
        self._connection_evidence_gate = False
        self._readiness_manager = readiness_manager
        self._lock = threading.RLock()
        self._publisher = EventPublisher()
        self._records: Dict[SessionId, _SessionRecord] = {}
        self._creation_order: List[SessionId] = []
        self._accepting_commands = True
        self._closed = False
        self._terminal_callbacks: Dict[int, Callable[[TerminalOutput], None]] = {}
        self._next_terminal_callback = 1

    def subscribe_events(self, callback: CoreEventCallback) -> Subscription:
        with self._lock:
            if self._closed:
                raise SshPilotError(
                    ErrorCode.INVALID_REQUEST,
                    "The session runtime is closed",
                )
        return self._publisher.subscribe(callback)

    @property
    def terminal_supported(self) -> bool:
        return bool(getattr(self._runner, "terminal_capable", False))

    def subscribe_terminal(
        self,
        callback: Callable[[TerminalOutput], None],
    ) -> Subscription:
        if not callable(callback):
            raise TypeError("terminal callback must be callable")
        with self._lock:
            if self._closed:
                raise SshPilotError(
                    ErrorCode.INVALID_REQUEST,
                    "The session runtime is closed",
                )
            token = self._next_terminal_callback
            self._next_terminal_callback += 1
            self._terminal_callbacks[token] = callback

        def _cancel() -> None:
            with self._lock:
                self._terminal_callbacks.pop(token, None)

        return Subscription(_cancel)

    def list_sessions(self) -> List[SessionSummary]:
        with self._lock:
            self._require_accepting_reads_locked()
            return [
                self._summary_locked(self._records[session_id])
                for session_id in self._creation_order
                if session_id in self._records
            ]

    def get_session(self, session_id: SessionId) -> SessionSummary:
        require_identifier(session_id, "session id")
        with self._lock:
            self._require_accepting_reads_locked()
            return self._summary_locked(self._record_locked(session_id))

    def open_session(
        self,
        request: OpenSessionRequest,
        *,
        client_id: ClientId,
    ) -> SessionSummary:
        """Compatibility helper that performs prepare/start synchronously."""

        prepared = self.prepare_open_session(request, client_id=client_id)
        self.start_session(prepared.id)
        with self._lock:
            return self._summary_locked(self._record_locked(prepared.id))

    def prepare_open_session(
        self,
        request: OpenSessionRequest,
        *,
        client_id: ClientId,
    ) -> SessionSummary:
        """Create one starting record without calling the process runner."""

        if type(request) is not OpenSessionRequest:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "An open session request is required",
            )
        connection = self._core_client.get_connection(request.connection_id)
        session_id = SessionId(self._id_factory())
        now = self._clock()
        record = _SessionRecord(
            session_id=session_id,
            connection_id=connection.id,
            state=SessionState.CREATED,
            created_at=now,
            updated_at=now,
            launch_spec=self._launch_spec(
                connection,
                session_id,
                request.dimensions,
                request.remote_command,
                request.force_tty,
            ),
            startup_scheduled=True,
            replay=TerminalReplayBuffer(self._replay_bytes),
            originating_client_id=client_id,
        )
        with self._lock:
            self._require_accepting_commands_locked()
            if session_id in self._records:
                raise RuntimeError("session id factory reused an active identifier")
            self._records[session_id] = record
            self._creation_order.append(session_id)
            created_event = self._event_locked(record, EventType.SESSION_CREATED)
            starting_event = self._transition_locked(record, SessionState.STARTING)
        self._publish((created_event, starting_event))
        with self._lock:
            return self._summary_locked(record)

    def set_auth_gate(
        self,
        gate: Optional[Callable[..., bool]],
        *,
        timeout_seconds: float = 60.0,
    ) -> None:
        """Install the ControlMaster auth gate used before ``RUNNING``."""

        if timeout_seconds <= 0:
            raise ValueError("auth gate timeout must be positive")
        self._auth_gate = gate
        self._auth_gate_timeout_seconds = float(timeout_seconds)

    def enable_connection_evidence_gate(self) -> None:
        """Require real terminal evidence before STARTING becomes RUNNING.

        Unlike the older blocking auth-gate hook, this mode is output-driven:
        the startup worker returns after spawning the PTY, authentication UI and
        terminal I/O remain live, and ``_terminal_output`` promotes the session.
        """

        self._connection_evidence_gate = True

    def set_authenticated_callback(
        self, callback: Optional[Callable[[SessionId], None]]
    ) -> None:
        """Notify the owner once terminal evidence confirms authentication."""

        self._authenticated_callback = callback

    @staticmethod
    def _recent_terminal_text_locked(
        record: _SessionRecord,
        maximum_bytes: int = 4000,
    ) -> str:
        end = record.replay.end_sequence
        start = max(record.replay.start_sequence, end - maximum_bytes)
        replay = record.replay.replay(start, maximum_bytes)
        return b"".join(chunk for _sequence, chunk in replay.chunks).decode(
            "utf-8", errors="replace"
        )

    def start_session(self, session_id: SessionId) -> None:
        """Run the potentially blocking startup step on a command worker."""

        require_identifier(session_id, "session id")
        with self._lock:
            record = self._record_locked(session_id)
            if not record.startup_scheduled or record.startup_started:
                raise SshPilotError(
                    ErrorCode.SESSION_INVALID_STATE,
                    "Session startup is not pending",
                    session_id=session_id,
                )
            if record.state not in {SessionState.STARTING, SessionState.CLOSING}:
                record.startup_scheduled = False
                return
            record.startup_scheduled = False
            record.startup_started = True
            record.terminal_capable = bool(
                getattr(self._runner, "terminal_capable", False)
            )
            spec = record.launch_spec
            if spec is None:
                raise RuntimeError("starting session has no launch specification")
        try:
            readiness = self._readiness_manager
            if readiness is not None:
                readiness.subscribe(
                    session_id,
                    self._on_ssh_diagnostic_result,
                    self._on_ssh_grace_expired,
                )
            if getattr(self._runner, "terminal_capable", False):
                handle = self._runner.start(
                    spec,
                    lambda exit_info: self._process_exited(session_id, exit_info),
                    lambda data: self._terminal_output(session_id, data),
                    lambda: self._terminal_eof(session_id),
                )
            else:
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
            authenticated = True
            readiness = self._readiness_manager
            with self._lock:
                if (
                    readiness is not None
                    and readiness.is_engaged(session_id)
                ):
                    record.readiness_engaged = True
                    record.diagnostic_primary = True
            evidence_gated = bool(
                self._connection_evidence_gate and record.terminal_capable
            )
            diagnostic_gated = bool(record.diagnostic_primary)
            gate = (
                None
                if (evidence_gated or diagnostic_gated)
                else self._auth_gate
            )
            if gate is not None:
                authenticated = bool(
                    gate(
                        session_id,
                        is_alive=lambda: self._handle_is_alive(handle),
                        timeout=self._auth_gate_timeout_seconds,
                    )
                )
            if not authenticated:
                cancel_code = self._startup_failure_code(session_id)
                self._startup_failed(
                    record,
                    cancel_code,
                    (
                        "The session authentication was cancelled"
                        if cancel_code is ErrorCode.OPERATION_CANCELLED
                        else "The session did not complete authentication"
                    ),
                )
                self._terminate_handle(
                    handle,
                    deadline=self._monotonic() + self._close_grace_seconds,
                )
                return
            events: List[CoreEvent] = []
            notify_authenticated = False
            deferred_outputs: List[TerminalOutput] = []
            deferred_callbacks: Tuple[Callable[[TerminalOutput], None], ...] = ()
            diagnostic_failed = False
            finish_readiness = False
            terminate_after_start = False
            with self._lock:
                if record.state is SessionState.STARTING:
                    record.process_handle = handle
                    record.started_at = self._clock()
                    promote = False
                    if diagnostic_gated:
                        result = record.diagnostic_result
                        if (
                            result is not None
                            and result.state is SshDiagnosticState.FAILED
                        ):
                            if record.diagnostic_failure_detail is None:
                                record.diagnostic_failure_detail = result.detail
                            record.failure = SessionFailure(
                                code=self._startup_failure_code(
                                    session_id
                                ).value,
                                message=(
                                    result.detail
                                    or "The session did not complete authentication"
                                ),
                            )
                            events.append(
                                self._transition_locked(
                                    record, SessionState.FAILED
                                )
                            )
                            diagnostic_failed = True
                        elif (
                            result is not None
                            and result.state
                            in {
                                SshDiagnosticState.AUTHENTICATED,
                                SshDiagnosticState.MUX_SESSION_OPENED,
                            }
                        ):
                            record.authenticated = True
                            promote = True
                        else:
                            promote = False
                    elif evidence_gated:
                        promote = (
                            self._connection_evidence_locked(record).verdict
                            == "connected"
                        )
                    else:
                        promote = True
                    if promote:
                        event, notify = self._promote_authenticated_locked(record)
                        events.append(event)
                        notify_authenticated = notify
                        deferred_outputs = list(record.deferred_live_output)
                        record.deferred_live_output.clear()
                        deferred_callbacks = tuple(
                            self._terminal_callbacks.values()
                        )
                        finish_readiness = diagnostic_gated
                elif record.state is SessionState.CLOSING:
                    record.process_handle = handle
                    record.deferred_live_output.clear()
                elif record.state in {
                    SessionState.EXITED,
                    SessionState.FAILED,
                    SessionState.CLOSED,
                }:
                    # The session left STARTING while the worker was busy
                    # (e.g. the process died and drained the PTY first): reap
                    # the freshly spawned handle.
                    terminate_after_start = True
            self._publish(events)
            if notify_authenticated and self._authenticated_callback is not None:
                self._authenticated_callback(session_id)
            for output in deferred_outputs:
                for callback in deferred_callbacks:
                    try:
                        callback(output)
                    except Exception:
                        continue
            if diagnostic_failed:
                self._finish_readiness(session_id)
                self._terminate_handle(
                    handle,
                    deadline=self._monotonic() + self._close_grace_seconds,
                )
                return
            if finish_readiness:
                self._finish_readiness(session_id)
            if terminate_after_start:
                self._terminate_handle(
                    handle,
                    deadline=self._monotonic() + self._close_grace_seconds,
                )

    def _promote_authenticated_locked(
        self,
        record: _SessionRecord,
    ) -> Tuple[Optional[CoreEvent], bool]:
        """Promote a STARTING session to RUNNING after authentication is proven.

        Returns ``(event, notify_authenticated)``.  ``notify_authenticated``
        is True exactly when the promotion transition fired, so the owner
        authenticated callback never fires for a FAILED / EXITED / CLOSED /
        CLOSING outcome.
        """
        if record.state is not SessionState.STARTING:
            return None, False
        event = self._transition_locked(record, SessionState.RUNNING)
        return event, True

    def _on_ssh_diagnostic_result(
        self,
        session_id: SessionId,
        result: SshDiagnosticResult,
    ) -> None:
        """Handle a decisive OpenSSH diagnostics verdict (monitor thread).

        AUTHENTICATED / MUX_SESSION_OPENED promote a STARTING session to
        RUNNING; a trusted FAILED detail fails it.  Either outcome finishes
        the diagnostics lease once it is no longer parked.  A decisive result
        that races the process handle is parked on the record (lease stays
        engaged) and applied atomically by :meth:`start_session` once the
        handle is stored; the authenticated callback only ever fires for the
        successful STARTING -> RUNNING transition.
        """
        if result.state is SshDiagnosticState.PENDING:
            return
        events: List[CoreEvent] = []
        parked = False
        notify_authenticated = False
        with self._lock:
            record = self._records.get(session_id)
            if record is None or record.state in {
                SessionState.EXITED,
                SessionState.CLOSED,
            }:
                return
            # A decisive verdict that races the process handle is parked on the
            # record with the lease still engaged; ``start_session`` applies it
            # atomically once the handle is stored.  This holds even when the
            # verdict arrives before ``diagnostic_primary`` is determined (e.g.
            # the subscribe-time immediate delivery), because the manager lease
            # is already engaged at that point.
            parked = (
                record.state is SessionState.STARTING
                and record.process_handle is None
            )
            if result.state is SshDiagnosticState.FAILED:
                if record.diagnostic_failure_detail is None:
                    record.diagnostic_failure_detail = result.detail
                if parked:
                    record.diagnostic_result = result
                elif (
                    record.state is SessionState.STARTING
                    and record.diagnostic_primary
                ):
                    record.failure = SessionFailure(
                        code=self._startup_failure_code(session_id).value,
                        message=(
                            result.detail
                            or "The session did not complete authentication"
                        ),
                    )
                    events.append(
                        self._transition_locked(record, SessionState.FAILED)
                    )
                else:
                    record.diagnostic_result = result
            elif result.state in {
                SshDiagnosticState.AUTHENTICATED,
                SshDiagnosticState.MUX_SESSION_OPENED,
            }:
                record.authenticated = True
                if parked:
                    record.diagnostic_result = result
                elif (
                    record.state is SessionState.STARTING
                    and record.diagnostic_primary
                ):
                    event, notify = self._promote_authenticated_locked(record)
                    events.append(event)
                    notify_authenticated = notify
                else:
                    record.diagnostic_result = result
        if not parked:
            self._finish_readiness(session_id)
        self._publish(events)
        if notify_authenticated and self._authenticated_callback is not None:
            self._authenticated_callback(session_id)
        if events:
            self._flush_deferred_output(session_id)

    def _on_ssh_grace_expired(self, session_id: SessionId) -> None:
        """Fall back to PTY connection evidence when diagnostics stay pending.

        Runs on the readiness grace timer thread.  The watch and diagnostics
        file are deliberately kept armed: the session runs under the PTY
        classifier from here on, but a late decisive verdict can still promote
        (or fail) a session that is still STARTING.  The lease is released by
        the eventual promotion/failure/exit/close, never here.
        """
        events: List[CoreEvent] = []
        notify_authenticated = False
        with self._lock:
            record = self._records.get(session_id)
            if record is None or record.state is not SessionState.STARTING:
                return
            record.pty_fallback = True
            if (
                record.process_handle is not None
                and self._connection_evidence_locked(record).verdict
                == "connected"
            ):
                event, notify = self._promote_authenticated_locked(record)
                events.append(event)
                notify_authenticated = notify
        self._publish(events)
        if notify_authenticated and self._authenticated_callback is not None:
            self._authenticated_callback(session_id)
        if events:
            self._flush_deferred_output(session_id)

    def _finish_readiness(self, session_id: SessionId) -> None:
        """Idempotently release the session's OpenSSH diagnostics lease."""
        readiness = self._readiness_manager
        if readiness is None:
            return
        try:
            readiness.finish(session_id)
        except Exception:
            logger.debug("readiness finish failed for %r", session_id, exc_info=True)

    def _flush_deferred_output(self, session_id: SessionId) -> None:
        """Deliver output buffered while a session waited for RUNNING."""
        with self._lock:
            record = self._records.get(session_id)
            if record is None or record.state is not SessionState.RUNNING:
                return
            deferred = list(record.deferred_live_output)
            record.deferred_live_output.clear()
            callbacks = tuple(self._terminal_callbacks.values())
        for output in deferred:
            for callback in callbacks:
                try:
                    callback(output)
                except Exception:
                    continue

    @staticmethod
    def _handle_is_alive(handle: SessionProcessHandle) -> bool:
        poll = getattr(handle, "poll", None)
        if not callable(poll):
            return True
        try:
            return poll() is None
        except Exception:
            return False

    def _startup_failure_code(self, session_id: SessionId) -> ErrorCode:
        """Classify auth-gate failure: cancelled interaction vs generic fail."""

        # Best-effort: inspect recent interactions via optional broker hook.
        classifier = getattr(self, "_auth_failure_classifier", None)
        if callable(classifier):
            try:
                code = classifier(session_id)
                if isinstance(code, ErrorCode):
                    return code
            except Exception:
                pass
        return ErrorCode.SESSION_STARTUP_FAILED

    def reject_pending_start(self, session_id: SessionId) -> None:
        """Mark an unscheduled startup failed after executor saturation."""

        self.fail_pending_start(
            session_id,
            SshPilotError(
                ErrorCode.SERVER_BUSY,
                "The daemon session command queue is full",
                retryable=True,
                session_id=session_id,
            ),
        )

    def fail_pending_start(
        self,
        session_id: SessionId,
        error: BaseException,
    ) -> None:
        """Mark a prepared startup failed after acknowledgement."""

        if isinstance(error, SshPilotError):
            code = error.code
            message = error.message
        else:
            code = ErrorCode.SESSION_STARTUP_FAILED
            message = "The session process could not be started"
        with self._lock:
            try:
                record = self._record_locked(session_id)
            except SshPilotError:
                return
            if record.startup_started or record.state is not SessionState.STARTING:
                return
            record.startup_scheduled = False
            record.terminal_capable = False
            record.failure = SessionFailure(code=code.value, message=message)
            event = self._transition_locked(record, SessionState.FAILED)
        self._finish_readiness(session_id)
        self._publish((event,))

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
        require_identifier(request.session_id, "session id")
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
            # Always create a new attachment (Phase 9: each pane has its own attachment)
            attachment_id = AttachmentId(new_attachment_id())

            # Create attachment record
            attachment_record = _AttachmentRecord(
                id=attachment_id,
                client_id=client_id,
                want_output=request.want_terminal_output,
                is_input_owner=False,
            )

            # Store attachment
            record.attachments[attachment_id] = attachment_record
            record.client_attachments[client_id].add(attachment_id)
            record.updated_at = self._clock()

            if request.want_terminal_output:
                if not record.terminal_capable and record.state is not SessionState.STARTING:
                    raise SshPilotError(
                        ErrorCode.TERMINAL_UNAVAILABLE,
                        "The session does not own a terminal stream",
                        session_id=request.session_id,
                    )
                record.output_clients.add(client_id)

            # Grant input ownership if requested and available
            if (
                request.request_input
                and self.terminal_supported
                and record.input_owner_attachment_id is None
            ):
                record.input_owner_attachment_id = attachment_id
                attachment_record.is_input_owner = True
            replay = record.replay.replay(
                min(request.from_sequence, record.replay.end_sequence),
                record.replay.max_bytes,
            )
            return AttachSessionResult(
                session=self._summary_locked(record),
                attachment=AttachmentInfo(
                    id=attachment_id,
                    session_id=record.session_id,
                    client_id=client_id,
                    input_owner=attachment_record.is_input_owner,
                ),
                available_start=replay.available_start,
                live_sequence=replay.live_sequence,
                replay_truncated=request.from_sequence < replay.available_start,
                eof=replay.eof,
            )

    def attach_replay(
        self,
        request: AttachSessionRequest,
        *,
        client_id: ClientId,
    ) -> Tuple[AttachSessionResult, ReplaySlice]:
        result = self.attach_session(request, client_id=client_id)
        with self._lock:
            record = self._record_locked(request.session_id)
            replay = record.replay.replay(
                min(request.from_sequence, record.replay.end_sequence),
                record.replay.max_bytes,
            )
        return result, replay

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
        require_identifier(request.session_id, "session id")
        with self._lock:
            self._require_accepting_commands_locked()
            record = self._record_locked(request.session_id)
            attachment_record = record.attachments.get(request.attachment_id)
            if attachment_record is None:
                return
            if attachment_record.client_id != client_id:
                raise SshPilotError(
                    ErrorCode.PERMISSION_DENIED,
                    "The attachment does not belong to this client",
                    session_id=request.session_id,
                )

            # Remove the attachment
            del record.attachments[request.attachment_id]
            record.client_attachments[client_id].discard(request.attachment_id)

            # Update output clients if this was the last attachment wanting output for this client
            if attachment_record.want_output:
                client_still_has_output = any(
                    record.attachments[att_id].want_output
                    for att_id in record.client_attachments[client_id]
                    if att_id in record.attachments
                )
                if not client_still_has_output:
                    record.output_clients.discard(client_id)

            # Release input ownership if this attachment owned it
            if record.input_owner_attachment_id == request.attachment_id:
                record.input_owner_attachment_id = None

            record.updated_at = self._clock()

    def receives_terminal(self, session_id: SessionId, client_id: ClientId) -> bool:
        with self._lock:
            record = self._records.get(session_id)
            return bool(record is not None and client_id in record.output_clients)

    def client_can_interact(
        self,
        session_id: SessionId,
        client_id: ClientId,
    ) -> bool:
        """Return whether a handshaken client is attached to this session."""

        with self._lock:
            record = self._records.get(session_id)
            return bool(
                record is not None
                and (
                    client_id == record.originating_client_id
                    or bool(record.client_attachments.get(client_id))
                )
            )

    def send_terminal_input(
        self,
        request: TerminalInput,
        *,
        client_id: ClientId,
    ) -> None:
        if type(request) is not TerminalInput:
            raise SshPilotError(ErrorCode.INVALID_REQUEST, "Terminal input is invalid")
        with self._lock:
            self._require_accepting_commands_locked()
            record = self._record_locked(request.session_id)
            handle = self._terminal_input_handle_locked(
                record,
                session_id=request.session_id,
                attachment_id=request.attachment_id,
                client_id=client_id,
            )
            if handle is None:
                return
        write = getattr(handle, "write", None)
        if not callable(write):
            raise SshPilotError(
                ErrorCode.TERMINAL_UNAVAILABLE,
                "The session does not own a writable terminal",
                session_id=request.session_id,
            )
        if not write(request.data):
            raise SshPilotError(
                ErrorCode.TERMINAL_INPUT_BACKPRESSURE,
                "The terminal input queue is full",
                retryable=True,
                session_id=request.session_id,
            )

    def broadcast_terminal_input(
        self,
        request: BroadcastTerminalInputRequest,
        *,
        client_id: ClientId,
    ) -> None:
        """Write one command to the PTY of each existing owned session."""
        if type(request) is not BroadcastTerminalInputRequest:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "Terminal broadcast input is invalid",
            )
        data = (request.command + "\n").encode("utf-8")
        with self._lock:
            self._require_accepting_commands_locked()
            handles = []
            for session_id in request.session_ids:
                record = self._record_locked(session_id)
                if record.state not in {SessionState.STARTING, SessionState.RUNNING}:
                    raise SshPilotError(
                        ErrorCode.SESSION_INVALID_STATE,
                        "The session is not accepting terminal input",
                        session_id=session_id,
                    )
                attachment_id = record.input_owner_attachment_id
                if attachment_id is None:
                    raise SshPilotError(
                        ErrorCode.TERMINAL_INPUT_OWNER_REQUIRED,
                        "An input-owning terminal attachment is required",
                        session_id=session_id,
                    )
                handle = self._terminal_input_handle_locked(
                    record,
                    session_id=session_id,
                    attachment_id=attachment_id,
                    client_id=client_id,
                )
                if handle is not None:
                    handles.append((session_id, handle))

        for session_id, handle in handles:
            write = getattr(handle, "write", None)
            if not callable(write):
                raise SshPilotError(
                    ErrorCode.TERMINAL_UNAVAILABLE,
                    "The session does not own a writable terminal",
                    session_id=session_id,
                )
            if not write(data):
                raise SshPilotError(
                    ErrorCode.TERMINAL_INPUT_BACKPRESSURE,
                    "The terminal input queue is full",
                    retryable=True,
                    session_id=session_id,
                )

    def _terminal_input_handle_locked(
        self,
        record: _SessionRecord,
        *,
        session_id: SessionId,
        attachment_id: AttachmentId,
        client_id: ClientId,
    ):
        attachment_record = record.attachments.get(attachment_id)
        if attachment_record is None or attachment_record.client_id != client_id:
            raise SshPilotError(
                ErrorCode.TERMINAL_ATTACHMENT_REQUIRED,
                "A matching terminal attachment is required",
                session_id=session_id,
            )
        if record.input_owner_attachment_id != attachment_id:
            raise SshPilotError(
                ErrorCode.TERMINAL_INPUT_OWNER_REQUIRED,
                "The attachment does not own terminal input",
                session_id=session_id,
            )
        # A blocking auth gate has not assigned its handle yet, so early
        # input cannot be delivered. The output-driven evidence gate does
        # assign it while STARTING and permits native PTY authentication.
        if record.state is SessionState.STARTING:
            if not (self._connection_evidence_gate and record.process_handle is not None):
                return None
        elif record.state is not SessionState.RUNNING:
            raise SshPilotError(
                ErrorCode.SESSION_INVALID_STATE,
                "The session is not accepting terminal input",
                session_id=session_id,
            )
        if record.process_handle is None:
            raise SshPilotError(
                ErrorCode.SESSION_INVALID_STATE,
                "The session is not accepting terminal input",
                session_id=session_id,
            )
        return record.process_handle

    def resize_terminal(
        self,
        request: ResizeTerminalRequest,
        *,
        client_id: ClientId,
    ) -> None:
        if type(request) is not ResizeTerminalRequest:
            raise SshPilotError(ErrorCode.INVALID_REQUEST, "Terminal resize is invalid")
        with self._lock:
            self._require_accepting_commands_locked()
            record = self._record_locked(request.session_id)
            if request.attachment_id is None:
                raise SshPilotError(
                    ErrorCode.TERMINAL_ATTACHMENT_REQUIRED,
                    "A terminal attachment is required",
                    session_id=request.session_id,
                )
            attachment_record = record.attachments.get(request.attachment_id)
            if attachment_record is None or attachment_record.client_id != client_id:
                raise SshPilotError(
                    ErrorCode.TERMINAL_ATTACHMENT_REQUIRED,
                    "A matching terminal attachment is required",
                    session_id=request.session_id,
                )
            if record.input_owner_attachment_id != request.attachment_id:
                raise SshPilotError(
                    ErrorCode.TERMINAL_INPUT_OWNER_REQUIRED,
                    "The attachment does not own terminal resize",
                    session_id=request.session_id,
                )
            if record.process_handle is None:
                spec = record.launch_spec
                if spec is None or record.state not in {
                    SessionState.CREATED,
                    SessionState.STARTING,
                }:
                    raise SshPilotError(
                        ErrorCode.TERMINAL_UNAVAILABLE,
                        "The session does not own a resizable terminal",
                        session_id=request.session_id,
                    )
                record.launch_spec = SessionLaunchSpec(
                    session_id=spec.session_id,
                    connection_id=spec.connection_id,
                    protocol=spec.protocol,
                    hostname=spec.hostname,
                    username=spec.username,
                    port=spec.port,
                    dimensions=request.dimensions,
                    remote_command=spec.remote_command,
                    force_tty=spec.force_tty,
                )
                return
            handle = record.process_handle
        resize = getattr(handle, "resize", None)
        if not callable(resize):
            raise SshPilotError(
                ErrorCode.TERMINAL_UNAVAILABLE,
                "The session does not own a resizable terminal",
                session_id=request.session_id,
            )
        try:
            resize(request.dimensions)
        except RuntimeError:
            raise SshPilotError(
                ErrorCode.TERMINAL_INPUT_BACKPRESSURE,
                "The terminal I/O queue is busy",
                retryable=True,
                session_id=request.session_id,
            ) from None

    def claim_input(
        self,
        session_id: SessionId,
        attachment_id: AttachmentId,
        client_id: ClientId,
    ) -> None:
        """Claim input ownership for an attachment. Only when unowned; no forced takeover."""
        with self._lock:
            self._require_accepting_commands_locked()
            record = self._record_locked(session_id)

            # Verify attachment belongs to client
            attachment_record = record.attachments.get(attachment_id)
            if attachment_record is None or attachment_record.client_id != client_id:
                raise SshPilotError(
                    ErrorCode.TERMINAL_ATTACHMENT_REQUIRED,
                    "A matching terminal attachment is required",
                    session_id=session_id,
                )

            # Check if input is already owned
            if record.input_owner_attachment_id is not None:
                raise SshPilotError(
                    ErrorCode.TERMINAL_INPUT_OWNER_EXISTS,
                    "Terminal input is already owned by another attachment",
                    session_id=session_id,
                )

            # Grant input ownership
            record.input_owner_attachment_id = attachment_id
            attachment_record.is_input_owner = True
            record.updated_at = self._clock()

    def release_input(
        self,
        session_id: SessionId,
        attachment_id: AttachmentId,
        client_id: ClientId,
    ) -> None:
        """Release input ownership. Current owner only."""
        with self._lock:
            self._require_accepting_commands_locked()
            record = self._record_locked(session_id)

            # Verify attachment belongs to client
            attachment_record = record.attachments.get(attachment_id)
            if attachment_record is None or attachment_record.client_id != client_id:
                raise SshPilotError(
                    ErrorCode.TERMINAL_ATTACHMENT_REQUIRED,
                    "A matching terminal attachment is required",
                    session_id=session_id,
                )

            # Check if this attachment owns input
            if record.input_owner_attachment_id != attachment_id:
                raise SshPilotError(
                    ErrorCode.TERMINAL_INPUT_OWNER_REQUIRED,
                    "The attachment does not own terminal input",
                    session_id=session_id,
                )

            # Release input ownership
            record.input_owner_attachment_id = None
            attachment_record.is_input_owner = False
            record.updated_at = self._clock()

    def replay_terminal(
        self,
        request: ReplayRequest,
        *,
        client_id: ClientId,
    ) -> Tuple[ReplayResult, ReplaySlice]:
        if type(request) is not ReplayRequest:
            raise SshPilotError(ErrorCode.INVALID_REQUEST, "Replay request is invalid")
        with self._lock:
            record = self._record_locked(request.session_id)
            attachment_record = record.attachments.get(request.attachment_id)
            if (
                attachment_record is None
                or attachment_record.client_id != client_id
            ):
                raise SshPilotError(
                    ErrorCode.TERMINAL_ATTACHMENT_REQUIRED,
                    "A matching terminal attachment is required",
                    session_id=request.session_id,
                )
            requested = (
                record.replay.start_sequence
                if request.after_sequence is None
                else request.after_sequence
            )
            try:
                replay = record.replay.replay(requested, request.max_bytes)
            except ValueError:
                raise SshPilotError(
                    ErrorCode.TERMINAL_SEQUENCE_OUT_OF_RANGE,
                    "The requested terminal sequence is unavailable",
                    session_id=request.session_id,
                ) from None
            result = ReplayResult(
                session_id=request.session_id,
                first_sequence=replay.returned_start,
                next_sequence=replay.returned_end,
                bounds=ReplayBounds(
                    earliest_sequence=replay.available_start,
                    latest_sequence=replay.live_sequence,
                    retained_bytes=record.replay.retained_bytes,
                ),
                truncated=replay.truncated,
                eof=replay.eof,
            )
        return result, replay

    def close_session(self, request: CloseSessionRequest) -> None:
        """Compatibility helper that accepts and completes close synchronously."""

        if self.prepare_close_session(request):
            self.finish_close_session(request.session_id)

    def prepare_close_session(self, request: CloseSessionRequest) -> bool:
        """Accept close without calling terminate, kill, or wait."""

        if type(request) is not CloseSessionRequest:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "A close session request is required",
            )
        require_identifier(request.session_id, "session id")
        events: List[CoreEvent] = []
        with self._lock:
            self._require_accepting_commands_locked()
            record = self._record_locked(request.session_id)
            if record.state is SessionState.CLOSED or record.close_scheduled:
                return False
            if record.state in {SessionState.CREATED, SessionState.EXITED}:
                events.append(self._transition_locked(record, SessionState.CLOSED))
            elif record.state is SessionState.FAILED and record.process_handle is None:
                events.append(self._transition_locked(record, SessionState.CLOSED))
            else:
                if record.state in {SessionState.STARTING, SessionState.RUNNING}:
                    events.append(
                        self._transition_locked(record, SessionState.CLOSING)
                    )
                record.close_scheduled = True
        self._publish(events)
        return record.close_scheduled

    def finish_close_session(self, session_id: SessionId) -> None:
        """Perform accepted blocking termination on a command worker."""

        require_identifier(session_id, "session id")
        self._close_session_id(
            session_id,
            deadline=self._monotonic() + self._close_grace_seconds,
            raise_on_failure=True,
            allow_shutdown=True,
        )

    def reject_pending_close(self, session_id: SessionId) -> None:
        """Make an executor-rejected close explicit and retryable."""

        with self._lock:
            record = self._record_locked(session_id)
            if not record.close_scheduled:
                return
            record.close_scheduled = False
            if record.state is SessionState.CLOSING:
                record.failure = SessionFailure(
                    code=ErrorCode.SERVER_BUSY.value,
                    message="The daemon session command queue is full",
                )
                event = self._transition_locked(record, SessionState.FAILED)
            else:
                event = None
        if event is not None:
            self._publish((event,))

    def detach_client(self, client_id: Optional[ClientId]) -> None:
        if client_id is None:
            return
        with self._lock:
            for record in self._records.values():
                # Remove all attachments for this client
                attachment_ids_to_remove = record.client_attachments.get(client_id, set()).copy()
                if attachment_ids_to_remove:
                    for attachment_id in attachment_ids_to_remove:
                        attachment_record = record.attachments.get(attachment_id)
                        if attachment_record:
                            # Release input ownership if this attachment owned it
                            if record.input_owner_attachment_id == attachment_id:
                                record.input_owner_attachment_id = None
                            # Remove the attachment
                            del record.attachments[attachment_id]

                    # Clean up client tracking
                    del record.client_attachments[client_id]
                    record.output_clients.discard(client_id)
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
                record.client_attachments.clear()
                record.output_clients.clear()
                record.input_owner_attachment_id = None
            self._terminal_callbacks.clear()
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
            self._finish_readiness(session_id)
            return
        self._terminate_record(
            record,
            deadline=deadline,
            raise_on_failure=raise_on_failure,
        )
        self._finish_readiness(session_id)

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
            graceful_deadline = min(
                deadline,
                self._monotonic() + self._close_grace_seconds,
            )
            remaining = max(0.0, graceful_deadline - self._monotonic())
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
                record.close_scheduled = False
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

    def _terminate_handle(
        self,
        handle: SessionProcessHandle,
        *,
        deadline: float,
    ) -> None:
        """Best-effort cleanup for a handle obtained after final session state."""

        try:
            handle.terminate()
            remaining = max(0.0, deadline - self._monotonic())
            exit_info = handle.wait(remaining)
            if exit_info is None:
                handle.kill()
                remaining = max(0.0, deadline - self._monotonic())
                handle.wait(remaining)
        except Exception:
            return

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
            if record.terminal_capable and not record.pty_eof:
                return
            events.extend(self._final_exit_events_locked(record))
        self._finish_readiness(session_id)
        self._publish(events)

    def _terminal_output(self, session_id: SessionId, data: bytes) -> None:
        if not data:
            return
        events: List[CoreEvent] = []
        notify_authenticated = False
        with self._lock:
            record = self._records.get(session_id)
            if record is None or record.pty_eof:
                return
            start, _end = record.replay.append(data)
            self._enforce_global_replay_budget_locked(prefer=session_id)
            output = TerminalOutput(
                session_id=session_id,
                sequence=start,
                data=data,
            )
            if (
                record.state is SessionState.STARTING
                and record.diagnostic_primary
                and not record.pty_fallback
            ):
                if record.process_handle is None:
                    record.deferred_live_output.append(output)
                    return
                callbacks = tuple(self._terminal_callbacks.values())
            elif (
                record.state is SessionState.STARTING
                and self._connection_evidence_gate
            ):
                if (
                    record.process_handle is not None
                    and self._connection_evidence_locked(record).verdict
                    == "connected"
                ):
                    event, notify = self._promote_authenticated_locked(record)
                    events.append(event)
                    notify_authenticated = notify
                callbacks = tuple(self._terminal_callbacks.values())
            elif record.state is not SessionState.RUNNING:
                record.deferred_live_output.append(output)
                return
            else:
                callbacks = tuple(self._terminal_callbacks.values())
        self._publish(events)
        if notify_authenticated and self._authenticated_callback is not None:
            self._authenticated_callback(session_id)
        if events:
            self._flush_deferred_output(session_id)
        for callback in callbacks:
            try:
                callback(output)
            except Exception:
                continue

    def _global_replay_retained_locked(self) -> int:
        return sum(record.replay.retained_bytes for record in self._records.values())

    def _enforce_global_replay_budget_locked(
        self, *, prefer: Optional[SessionId] = None
    ) -> None:
        """Trim oldest sessions first when the daemon-wide replay budget is exceeded."""

        total = self._global_replay_retained_locked()
        if total <= self._global_replay_bytes:
            return
        excess = total - self._global_replay_bytes
        # Prefer trimming older sessions; keep the writer session until last.
        order = [
            session_id
            for session_id in self._creation_order
            if session_id in self._records and session_id != prefer
        ]
        if prefer is not None and prefer in self._records:
            order.append(prefer)
        for session_id in order:
            if excess <= 0:
                break
            record = self._records[session_id]
            retained = record.replay.retained_bytes
            if retained <= 0:
                continue
            target = max(0, retained - excess)
            freed = record.replay.trim_to(target)
            excess -= freed

    def _terminal_eof(self, session_id: SessionId) -> None:
        events: List[CoreEvent] = []
        with self._lock:
            record = self._records.get(session_id)
            if record is None or record.pty_eof:
                return
            record.pty_eof = True
            sequence = record.replay.mark_eof()
            callbacks = tuple(self._terminal_callbacks.values())
            output = TerminalOutput(
                session_id=session_id,
                sequence=sequence,
                data=b"",
                eof=True,
            )
            if record.exit_info is not None:
                events.extend(self._final_exit_events_locked(record))
        for callback in callbacks:
            try:
                callback(output)
            except Exception:
                continue
        if events:
            self._finish_readiness(session_id)
        self._publish(events)

    def _final_exit_events_locked(
        self,
        record: _SessionRecord,
    ) -> List[CoreEvent]:
        events: List[CoreEvent] = []
        if record.state in {SessionState.EXITED, SessionState.CLOSED}:
            return events
        exit_info = record.exit_info or SessionExitInfo(reason="process_exit")
        if (
            record.state is SessionState.STARTING
            and (self._connection_evidence_gate or record.diagnostic_primary)
        ):
            evidence = self._connection_evidence_locked(record)
            failure_reason: Optional[str] = None
            if record.diagnostic_failure_detail:
                failure_reason = record.diagnostic_failure_detail
            elif evidence.verdict == "failed" or exit_info.exit_code not in {
                None,
                0,
            }:
                failure_reason = evidence.failure_reason
            if failure_reason is not None:
                failure_code = self._startup_failure_code(record.session_id)
                record.failure = SessionFailure(
                    code=failure_code.value,
                    message=(
                        failure_reason
                        or (
                            "The session authentication was cancelled"
                            if failure_code is ErrorCode.OPERATION_CANCELLED
                            else ""
                        )
                        or "The session did not complete authentication"
                    ),
                )
                events.append(self._transition_locked(record, SessionState.FAILED))
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
        return events

    @classmethod
    def _connection_evidence_locked(
        cls,
        record: _SessionRecord,
    ):
        return classify_connection_evidence(cls._recent_terminal_text_locked(record))

    def _startup_failed(
        self,
        record: _SessionRecord,
        code: ErrorCode,
        message: str,
    ) -> None:
        with self._lock:
            if record.state is not SessionState.STARTING:
                return
            record.startup_scheduled = False
            record.terminal_capable = False
            record.deferred_live_output.clear()
            record.failure = SessionFailure(code=code.value, message=message)
            event = self._transition_locked(record, SessionState.FAILED)
        self._finish_readiness(record.session_id)
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
        previous_state = record.state
        now = self._clock()
        record.state = new_state
        record.updated_at = now
        if new_state is SessionState.EXITED:
            record.exited_at = now
        elif new_state is SessionState.CLOSED:
            record.closed_at = now
            record.attachments.clear()
            record.client_attachments.clear()
            record.output_clients.clear()
            record.input_owner_attachment_id = None
            self._evict_closed_locked()
        with log_context(
            session=record.session_id,
            client=record.originating_client_id,
            connection=record.connection_id,
        ):
            logger.info(
                "session state changed from=%s to=%s",
                previous_state.value,
                new_state.value,
            )
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
        input_owner = None
        if record.input_owner_attachment_id is not None:
            attachment_record = record.attachments.get(record.input_owner_attachment_id)
            if attachment_record is not None:
                from sshpilot.api.models.sessions import InputOwner

                input_owner = InputOwner(
                    client_id=attachment_record.client_id,
                    attachment_id=attachment_record.id,
                )
        supported = frozenset(
            {
                "terminal.output",
                "terminal.input",
                "terminal.resize",
                "terminal.replay",
            }
            if record.terminal_capable
            else ()
        )
        return SessionSummary(
            id=record.session_id,
            connection_id=record.connection_id,
            state=record.state,
            created_at=record.created_at,
            input_owner=input_owner,
            capabilities=SessionCapabilities(supported=supported),
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
        dimensions: Optional[TerminalDimensions],
        remote_command: Optional[str] = None,
        force_tty: bool = False,
    ) -> SessionLaunchSpec:
        return SessionLaunchSpec(
            session_id=session_id,
            connection_id=connection.id,
            protocol=connection.protocol,
            hostname=connection.hostname,
            username=connection.username,
            port=connection.port,
            dimensions=dimensions,
            remote_command=remote_command,
            force_tty=force_tty,
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

"""Daemon-owned port-forward lifecycle: dedicated ``ssh -N -L/-R/-D`` processes.

Mirrors :mod:`sshpilot.daemon.session_runtime`'s shape (a ``prepare_open`` /
``start`` pair for non-blocking startup, an explicit state machine, public
events) but launches a purpose-built, remote-command-less ``ssh -N`` process
per forward instead of an interactive terminal session. Auth/host-key trust
reuses :class:`~sshpilot.daemon.session_runtime.SessionLaunchSpec` and the
existing :class:`~sshpilot.daemon.interaction_broker.InteractionBroker`
unchanged, exactly like SFTP services (see ``sftp_runtime.py``'s module
docstring for the ``session_id``-as-correlation-key rationale reused here).
"""

from __future__ import annotations

import logging
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Set, Tuple

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
    ClientId,
    ConnectionId,
    ForwardId,
    SessionId,
    utc_now,
)
from sshpilot.api.models.operations import (
    CloseForwardRequest,
    ForwardState,
    ForwardSummary,
    ForwardType,
    OpenForwardRequest,
    ServiceFailure,
)
from sshpilot.api.forward_identity import new_forward_id
from sshpilot.logging_support import log_context

from .session_runtime import SessionLaunchSpec

logger = logging.getLogger(__name__)

DEFAULT_MAX_RETAINED_CLOSED_FORWARDS = 100
DEFAULT_TERMINATE_GRACE_SECONDS = 2.0
# Local/dynamic ACTIVE waits for a real bind; auth + host-key prompts need
# headroom comparable to SFTP connect (not a short process-alive smoke window).
DEFAULT_FORWARD_ACTIVE_TIMEOUT_SECONDS = 30.0
# Remote forwards bind on the SSH server. After ExitOnForwardFailure=yes, a
# short process-alive window is the honest client-visible ACTIVE signal.
DEFAULT_REMOTE_FORWARD_STABLE_SECONDS = 0.5
_BIND_ANY_HOSTS = frozenset({"", "*", "0.0.0.0", "::"})

ForwardExitCallback = Callable[[Optional[int]], None]


class ForwardProcessHandle(Protocol):
    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float) -> Optional[int]:
        """Return the exit code, or ``None`` if still running at timeout."""
        ...

    def poll(self) -> Optional[int]:
        """Return the exit code without blocking, or ``None`` if still running."""
        ...


class ForwardProcessRunner(Protocol):
    """Narrow launch boundary, mirroring ``SessionProcessRunner``."""

    def start(
        self,
        spec: SessionLaunchSpec,
        on_exit: ForwardExitCallback,
    ) -> ForwardProcessHandle: ...

    def close(self) -> None: ...


class UnsupportedForwardProcessRunner:
    """Default runner until a daemon launch builder is wired in."""

    def start(
        self,
        spec: SessionLaunchSpec,
        on_exit: ForwardExitCallback,
    ) -> ForwardProcessHandle:
        del on_exit
        raise SshPilotError(
            ErrorCode.FORWARD_STARTUP_FAILED,
            "Forward startup requires daemon runtime support",
            connection_id=spec.connection_id,
        )

    def close(self) -> None:
        return


class _OwnedForwardProcess:
    def __init__(
        self,
        process: "subprocess.Popen",
        on_exit: ForwardExitCallback,
        unregister: Callable[["_OwnedForwardProcess"], None],
    ) -> None:
        self._process = process
        self._on_exit = on_exit
        self._unregister = unregister
        self._lock = threading.Lock()
        self._notified = False
        self._reaper = threading.Thread(
            target=self._reap, name="sshpilot-forward-reaper", daemon=True
        )
        self._reaper.start()

    def _reap(self) -> None:
        return_code = self._process.wait()
        self._notify(return_code)

    def _notify(self, return_code: Optional[int]) -> None:
        with self._lock:
            if self._notified:
                return
            self._notified = True
        self._unregister(self)
        self._on_exit(return_code)

    def terminate(self) -> None:
        if self._process.poll() is None:
            try:
                self._process.terminate()
            except Exception:  # pragma: no cover - best effort
                pass

    def kill(self) -> None:
        if self._process.poll() is None:
            try:
                self._process.kill()
            except Exception:  # pragma: no cover - best effort
                pass

    def wait(self, timeout: float) -> Optional[int]:
        try:
            return self._process.wait(timeout=max(0.0, timeout))
        except subprocess.TimeoutExpired:
            return None

    def poll(self) -> Optional[int]:
        return self._process.poll()


class SubprocessForwardProcessRunner:
    """Spawn ``ssh -N -L/-R/-D … -o ExitOnForwardFailure=yes``.

    ``command_builder`` is the daemon-internal injection point (never reached
    through the wire protocol) that returns the canonical argv/env for one
    forward — production wiring goes through the same
    ``InteractionBroker``/askpass path as terminal sessions and SFTP
    (see ``DaemonServer._prepare_forward_launch``).
    """

    def __init__(
        self,
        command_builder: Callable[[SessionLaunchSpec], Tuple[Sequence[str], Dict[str, str]]],
    ) -> None:
        if not callable(command_builder):
            raise TypeError("forward command builder must be callable")
        self._command_builder = command_builder
        self._lock = threading.Lock()
        self._handles: Set[_OwnedForwardProcess] = set()
        self._closed = False

    def start(
        self,
        spec: SessionLaunchSpec,
        on_exit: ForwardExitCallback,
    ) -> ForwardProcessHandle:
        argv, environment = self._command_builder(spec)
        argv = tuple(argv)
        if not argv or any(type(item) is not str or not item for item in argv):
            raise SshPilotError(
                ErrorCode.FORWARD_STARTUP_FAILED,
                "The forward launch command is invalid",
                connection_id=spec.connection_id,
            )
        with self._lock:
            if self._closed:
                raise RuntimeError("forward process runner is closed")
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=dict(environment),
            close_fds=True,
        )
        handle = _OwnedForwardProcess(process, on_exit, self._unregister)
        with self._lock:
            if self._closed:
                handle.terminate()
                raise RuntimeError("forward process runner is closed")
            self._handles.add(handle)
        return handle

    def _unregister(self, handle: _OwnedForwardProcess) -> None:
        with self._lock:
            self._handles.discard(handle)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            handles = tuple(self._handles)
            self._handles.clear()
        for handle in handles:
            handle.terminate()
            if handle.wait(DEFAULT_TERMINATE_GRACE_SECONDS) is None:
                handle.kill()
                handle.wait(DEFAULT_TERMINATE_GRACE_SECONDS)


@dataclass
class _ForwardRecord:
    forward_id: ForwardId
    connection_id: ConnectionId
    forward_type: ForwardType
    bind_host: str
    bind_port: int
    destination_host: Optional[str]
    destination_port: Optional[int]
    state: ForwardState
    created_at: datetime
    owner_client_id: Optional[ClientId] = None
    active_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    failure: Optional[ServiceFailure] = None
    handle: Optional[ForwardProcessHandle] = None
    launch_spec: Optional[SessionLaunchSpec] = None
    close_scheduled: bool = False


_ALLOWED_TRANSITIONS = {
    ForwardState.CREATED: frozenset(
        {ForwardState.STARTING, ForwardState.FAILED, ForwardState.CLOSED}
    ),
    ForwardState.STARTING: frozenset(
        {ForwardState.ACTIVE, ForwardState.CLOSING, ForwardState.FAILED}
    ),
    ForwardState.ACTIVE: frozenset({ForwardState.CLOSING, ForwardState.FAILED}),
    ForwardState.CLOSING: frozenset({ForwardState.CLOSED, ForwardState.FAILED}),
    ForwardState.FAILED: frozenset({ForwardState.CLOSED}),
    ForwardState.CLOSED: frozenset(),
}

_TRANSITION_EVENT_TYPES = {
    ForwardState.STARTING: EventType.FORWARD_STARTING,
    ForwardState.ACTIVE: EventType.FORWARD_ACTIVE,
    ForwardState.CLOSED: EventType.FORWARD_CLOSED,
    ForwardState.FAILED: EventType.FORWARD_FAILED,
    # CLOSING has no dedicated public event type; transitions into it are
    # applied silently (see _transition_locked).
}


def is_valid_forward_transition(current: ForwardState, target: ForwardState) -> bool:
    if not isinstance(current, ForwardState) or not isinstance(target, ForwardState):
        raise TypeError("forward transitions require ForwardState values")
    return target in _ALLOWED_TRANSITIONS[current]


class ForwardRuntime:
    """Serialize daemon-lifetime forward state and owned ``ssh -N`` processes."""

    def __init__(
        self,
        core_client: SshPilotClient,
        *,
        runner: Optional[Any] = None,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        id_factory: Callable[[], ForwardId] = new_forward_id,
        shutdown_timeout_seconds: float = 3.0,
        max_retained_closed_forwards: int = DEFAULT_MAX_RETAINED_CLOSED_FORWARDS,
        active_timeout_seconds: float = DEFAULT_FORWARD_ACTIVE_TIMEOUT_SECONDS,
        remote_stable_seconds: float = DEFAULT_REMOTE_FORWARD_STABLE_SECONDS,
    ) -> None:
        if shutdown_timeout_seconds < 0:
            raise ValueError("forward shutdown timeout must not be negative")
        if type(max_retained_closed_forwards) is not int or max_retained_closed_forwards < 0:
            raise ValueError("closed-forward retention limit must not be negative")
        if active_timeout_seconds <= 0:
            raise ValueError("forward active-detection timeout must be positive")
        if remote_stable_seconds < 0:
            raise ValueError("remote forward stable window must not be negative")
        self._core_client = core_client
        self._runner: Any = runner or UnsupportedForwardProcessRunner()
        self._clock = clock
        self._monotonic = monotonic
        self._id_factory = id_factory
        self._shutdown_timeout_seconds = float(shutdown_timeout_seconds)
        self._max_retained_closed_forwards = max_retained_closed_forwards
        self._active_timeout_seconds = float(active_timeout_seconds)
        self._remote_stable_seconds = float(remote_stable_seconds)
        self._lock = threading.RLock()
        self._publisher = EventPublisher()
        self._records: Dict[ForwardId, _ForwardRecord] = {}
        self._creation_order: List[ForwardId] = []
        self._accepting_commands = True
        self._closed = False

    def subscribe_events(self, callback: CoreEventCallback) -> Subscription:
        with self._lock:
            if self._closed:
                raise SshPilotError(
                    ErrorCode.INVALID_REQUEST,
                    "The forward runtime is closed",
                )
        return self._publisher.subscribe(callback)

    # -- reads --------------------------------------------------------
    def list_forwards(self) -> List[ForwardSummary]:
        with self._lock:
            self._require_accepting_reads_locked()
            return [
                self._summary_locked(self._records[forward_id])
                for forward_id in self._creation_order
                if forward_id in self._records
            ]

    def get_forward(self, forward_id: ForwardId) -> ForwardSummary:
        with self._lock:
            self._require_accepting_reads_locked()
            return self._summary_locked(self._record_locked(forward_id))

    # -- lifecycle ------------------------------------------------------
    def prepare_open_forward(
        self,
        request: OpenForwardRequest,
        *,
        client_id: ClientId,
    ) -> ForwardSummary:
        if type(request) is not OpenForwardRequest:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "An open forward request is required",
            )
        connection = self._core_client.get_connection(request.connection_id)
        if connection.protocol != "ssh":
            raise SshPilotError(
                ErrorCode.UNSUPPORTED_SESSION_PROTOCOL,
                "The connection protocol cannot start a forward",
                connection_id=request.connection_id,
            )
        forward_id = self._id_factory()
        now = self._clock()
        record = _ForwardRecord(
            forward_id=forward_id,
            connection_id=connection.id,
            forward_type=request.type,
            bind_host=request.bind_host,
            bind_port=request.bind_port,
            destination_host=request.destination_host,
            destination_port=request.destination_port,
            state=ForwardState.CREATED,
            created_at=now,
            owner_client_id=client_id,
            # The forward id doubles as the interaction/eligibility key (see
            # module docstring) — no new broker trust path is introduced.
            launch_spec=SessionLaunchSpec(
                session_id=SessionId(str(forward_id)),
                connection_id=connection.id,
                protocol=connection.protocol,
                hostname=connection.hostname,
                username=connection.username,
                port=connection.port,
            ),
        )
        with self._lock:
            self._require_accepting_commands_locked()
            if forward_id in self._records:
                raise RuntimeError("forward id factory reused an active identifier")
            self._records[forward_id] = record
            self._creation_order.append(forward_id)
            created_event = self._event_locked(record, EventType.FORWARD_CREATED)
            starting_event = self._transition_locked(record, ForwardState.STARTING)
        self._publish((created_event, starting_event))
        with self._lock:
            return self._summary_locked(record)

    def start_forward(self, forward_id: ForwardId) -> None:
        with self._lock:
            record = self._record_locked(forward_id)
            if record.state not in {ForwardState.STARTING, ForwardState.CLOSING}:
                return
            spec = record.launch_spec
            currently_starting = record.state is ForwardState.STARTING
        if currently_starting:
            try:
                self._check_bind_available(record)
            except SshPilotError as error:
                self._fail(record, error.code, error.message)
                return
        try:
            handle = self._runner.start(
                spec,
                lambda return_code, fid=forward_id: self._on_process_exit(fid, return_code),
            )
            if handle is None:
                raise TypeError("forward runner returned no process handle")
        except SshPilotError as error:
            self._fail(record, error.code, error.message)
            return
        except Exception:
            self._fail(
                record,
                ErrorCode.FORWARD_STARTUP_FAILED,
                "The forward could not be started",
            )
            return
        with self._lock:
            if record.state not in {ForwardState.STARTING, ForwardState.CLOSING}:
                should_terminate = True
            else:
                record.handle = handle
                should_terminate = False
                closing = record.state is ForwardState.CLOSING
        if should_terminate:
            handle.terminate()
            return
        if closing:
            return
        if not self._detect_active(record):
            self._fail(
                record,
                ErrorCode.FORWARD_STARTUP_FAILED,
                "The forward did not become active",
            )
            return
        with self._lock:
            if record.state is not ForwardState.STARTING:
                return
            record.active_at = self._clock()
            event = self._transition_locked(record, ForwardState.ACTIVE)
        self._publish((event,))

    def reject_pending_start(self, forward_id: ForwardId) -> None:
        self.fail_pending_start(
            forward_id,
            SshPilotError(
                ErrorCode.SERVER_BUSY,
                "The daemon forward command queue is full",
                retryable=True,
            ),
        )

    def fail_pending_start(self, forward_id: ForwardId, error: BaseException) -> None:
        if isinstance(error, SshPilotError):
            code = error.code
            message = error.message
        else:
            code = ErrorCode.FORWARD_STARTUP_FAILED
            message = "The forward could not be started"
        record = self._records.get(forward_id)
        if record is not None:
            self._fail(record, code, message)

    def _check_bind_available(self, record: _ForwardRecord) -> None:
        if record.forward_type is ForwardType.REMOTE:
            return  # The bind happens on the remote host; nothing local to check.
        host = record.bind_host
        target = "0.0.0.0" if host in _BIND_ANY_HOSTS else host
        family = socket.AF_INET6 if ":" in target else socket.AF_INET
        probe = socket.socket(family, socket.SOCK_STREAM)
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((target, record.bind_port))
        except OSError as exc:
            raise SshPilotError(
                ErrorCode.FORWARD_BIND_FAILED,
                "The requested local port is already in use",
                connection_id=record.connection_id,
            ) from exc
        finally:
            probe.close()

    def _detect_active(self, record: _ForwardRecord) -> bool:
        """Wait until the forward is truthfully usable.

        Local and dynamic forwards must accept a TCP connect on the bind
        address before ``ACTIVE`` — process-alive alone is not enough
        (auth can still be in progress). Remote forwards bind on the SSH
        server, so a short process-alive window after
        ``ExitOnForwardFailure=yes`` is the honest signal OpenSSH exposes.
        """

        started = self._monotonic()
        deadline = started + self._active_timeout_seconds
        check_bind = record.forward_type is not ForwardType.REMOTE
        remote_ready_at = started + self._remote_stable_seconds
        while self._monotonic() < deadline:
            with self._lock:
                handle = record.handle
            if handle is None or handle.poll() is not None:
                return False
            if check_bind:
                if self._can_connect(record.bind_host, record.bind_port):
                    return True
            elif self._monotonic() >= remote_ready_at:
                return True
            time.sleep(0.05)
        with self._lock:
            handle = record.handle
        if handle is None or handle.poll() is not None:
            return False
        if check_bind:
            return self._can_connect(record.bind_host, record.bind_port)
        return True

    @staticmethod
    def _can_connect(host: str, port: int) -> bool:
        target = "127.0.0.1" if host in _BIND_ANY_HOSTS else host
        try:
            with socket.create_connection((target, port), timeout=0.2):
                return True
        except OSError:
            return False

    def _on_process_exit(self, forward_id: ForwardId, return_code: Optional[int]) -> None:
        with self._lock:
            record = self._records.get(forward_id)
            if record is None or record.state in {ForwardState.CLOSED, ForwardState.FAILED}:
                return
            record.handle = None
            if record.state is ForwardState.CLOSING:
                event = self._transition_locked(record, ForwardState.CLOSED)
                self._publish((event,))
                return
        # STARTING or ACTIVE exited on its own — an unexpected failure.
        self._fail(
            record,
            ErrorCode.FORWARD_NOT_ACTIVE,
            self._exit_message(return_code),
        )

    @staticmethod
    def _exit_message(return_code: Optional[int]) -> str:
        if return_code is None:
            return "The forward process exited unexpectedly"
        if return_code < 0:
            return f"The forward process was terminated by signal {-return_code}"
        return f"The forward process exited with status {return_code}"

    def _fail(self, record: _ForwardRecord, code: Any, message: str) -> None:
        with self._lock:
            if record.state in {ForwardState.CLOSED, ForwardState.FAILED}:
                return
            record.handle = None
            record.failure = ServiceFailure(
                code=code.value if isinstance(code, ErrorCode) else str(code),
                message=message,
            )
            event = self._transition_locked(record, ForwardState.FAILED)
        self._publish((event,))

    # -- close --------------------------------------------------------
    def prepare_close_forward(
        self,
        request: CloseForwardRequest,
        *,
        client_id: ClientId,
    ) -> bool:
        if type(request) is not CloseForwardRequest:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "A close forward request is required",
            )
        events: List[Optional[CoreEvent]] = []
        with self._lock:
            self._require_accepting_commands_locked()
            record = self._record_locked(request.forward_id)
            self._require_owner(record, client_id)
            if record.state is ForwardState.CLOSED or record.close_scheduled:
                return False
            if record.state is ForwardState.CREATED:
                events.append(self._transition_locked(record, ForwardState.CLOSED))
            elif record.state is ForwardState.FAILED and record.handle is None:
                events.append(self._transition_locked(record, ForwardState.CLOSED))
            else:
                if record.state in {ForwardState.STARTING, ForwardState.ACTIVE}:
                    events.append(
                        self._transition_locked(record, ForwardState.CLOSING)
                    )
                record.close_scheduled = True
        self._publish(events)
        return record.close_scheduled

    def finish_close_forward(self, forward_id: ForwardId) -> None:
        self._close_forward_id(forward_id, allow_shutdown=True)

    def reject_pending_close(self, forward_id: ForwardId) -> None:
        with self._lock:
            record = self._record_locked(forward_id)
            if not record.close_scheduled:
                return
            record.close_scheduled = False
            if record.state is ForwardState.CLOSING:
                record.failure = ServiceFailure(
                    code=ErrorCode.SERVER_BUSY.value,
                    message="The daemon forward command queue is full",
                )
                event = self._transition_locked(record, ForwardState.FAILED)
            else:
                event = None
        if event is not None:
            self._publish((event,))

    def claim_forward(
        self,
        forward_id: ForwardId,
        *,
        client_id: ClientId,
    ) -> ForwardSummary:
        """Claim ownership of an orphaned forward.

        A forward becomes orphaned when its originating client disconnects
        (``detach_client`` clears ``owner_client_id``). Any subsequent client
        may call ``claim_forward`` to become the new owner, after which it
        can close or mutate the forward.
        """
        with self._lock:
            self._require_accepting_commands_locked()
            record = self._record_locked(forward_id)
            if record.state is ForwardState.CLOSED:
                raise SshPilotError(
                    ErrorCode.INVALID_REQUEST,
                    "Cannot claim a closed forward",
                    connection_id=record.connection_id,
                    details={"forward_id": forward_id},
                )
            if record.owner_client_id is not None and record.owner_client_id != client_id:
                raise SshPilotError(
                    ErrorCode.SERVICE_OWNER_REQUIRED,
                    "Only the originating client may claim this forward",
                    connection_id=record.connection_id,
                    details={"forward_id": forward_id},
                )
            record.owner_client_id = client_id
            return self._summary_locked(record)

    def _close_forward_id(self, forward_id: ForwardId, *, allow_shutdown: bool = False) -> None:
        events: List[Optional[CoreEvent]] = []
        handle: Optional[ForwardProcessHandle] = None
        with self._lock:
            if not allow_shutdown:
                self._require_accepting_commands_locked()
            record = self._record_locked(forward_id)
            if record.state is ForwardState.CLOSED:
                return
            if record.state is ForwardState.CREATED:
                events.append(self._transition_locked(record, ForwardState.CLOSED))
            elif record.state is ForwardState.FAILED:
                handle = record.handle
                if handle is None:
                    events.append(self._transition_locked(record, ForwardState.CLOSED))
            elif record.state in {ForwardState.STARTING, ForwardState.ACTIVE}:
                events.append(self._transition_locked(record, ForwardState.CLOSING))
                handle = record.handle
            elif record.state is ForwardState.CLOSING:
                handle = record.handle
        self._publish(events)
        if handle is None:
            return
        # The owned process's reaper thread performs the final CLOSING ->
        # CLOSED transition once the process actually exits (see
        # _on_process_exit), so unexpected exits and requested closes share
        # one code path.
        handle.terminate()
        if handle.wait(DEFAULT_TERMINATE_GRACE_SECONDS) is None:
            handle.kill()

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._accepting_commands = False
            forward_ids = tuple(self._creation_order)
        for forward_id in forward_ids:
            self._close_forward_id(forward_id, allow_shutdown=True)
        deadline = self._monotonic() + self._shutdown_timeout_seconds
        while self._monotonic() < deadline:
            with self._lock:
                if all(
                    record.state in {ForwardState.CLOSED, ForwardState.FAILED}
                    for record in self._records.values()
                ):
                    break
            time.sleep(0.05)
        with self._lock:
            self._closed = True
        try:
            self._runner.close()
        finally:
            self._publisher.close()

    # -- eligibility ----------------------------------------------------
    def client_can_interact(self, forward_id: ForwardId, client_id: ClientId) -> bool:
        with self._lock:
            record = self._records.get(forward_id)
            return bool(record is not None and record.owner_client_id == client_id)

    def detach_client(self, client_id: Optional[ClientId]) -> None:
        """Orphan all forwards owned by the disconnecting client.

        The forwarding process keeps running; ownership is cleared so any
        reconnecting client can claim and manage the resource.
        """
        if client_id is None:
            return
        with self._lock:
            for record in self._records.values():
                if record.owner_client_id == client_id:
                    record.owner_client_id = None

    # -- helpers ------------------------------------------------------
    @staticmethod
    def _require_owner(record: _ForwardRecord, client_id: ClientId) -> None:
        if record.owner_client_id is not None and record.owner_client_id != client_id:
            raise SshPilotError(
                ErrorCode.SERVICE_OWNER_REQUIRED,
                "Only the originating client may mutate this forward",
                connection_id=record.connection_id,
                details={"forward_id": record.forward_id},
            )

    def _transition_locked(
        self,
        record: _ForwardRecord,
        new_state: ForwardState,
    ) -> Optional[CoreEvent]:
        if not is_valid_forward_transition(record.state, new_state):
            raise RuntimeError(
                f"invalid forward transition {record.state.value}->{new_state.value}"
            )
        previous_state = record.state
        record.state = new_state
        if new_state is ForwardState.CLOSED:
            record.closed_at = self._clock()
            self._evict_closed_locked()
        with log_context(
            forward=record.forward_id,
            client=record.owner_client_id,
            connection=record.connection_id,
        ):
            logger.info(
                "forward state changed from=%s to=%s",
                previous_state.value,
                new_state.value,
            )
        event_type = _TRANSITION_EVENT_TYPES.get(new_state)
        if event_type is None:
            return None
        return self._event_locked(record, event_type)

    def _event_locked(self, record: _ForwardRecord, event_type: EventType) -> CoreEvent:
        return CoreEvent(
            type=event_type,
            payload=self._summary_locked(record),
            sequence=0,
            connection_id=record.connection_id,
            # Reuses CoreEvent.session_id as the generic interaction/eligibility
            # correlation key (see module docstring) — not a terminal session.
            session_id=SessionId(str(record.forward_id)),
        )

    def _publish(self, events) -> None:
        for event in events:
            if event is None:
                continue
            try:
                self._publisher.publish(
                    event.type,
                    event.payload,
                    connection_id=event.connection_id,
                    session_id=event.session_id,
                )
            except RuntimeError:
                return

    def _record_locked(self, forward_id: ForwardId) -> _ForwardRecord:
        record = self._records.get(forward_id)
        if record is None:
            raise SshPilotError(
                ErrorCode.FORWARD_NOT_FOUND,
                "The requested forward does not exist",
                details={"forward_id": forward_id},
            )
        return record

    @staticmethod
    def _summary_locked(record: _ForwardRecord) -> ForwardSummary:
        return ForwardSummary(
            id=record.forward_id,
            connection_id=record.connection_id,
            type=record.forward_type,
            state=record.state,
            bind_host=record.bind_host,
            bind_port=record.bind_port,
            destination_host=record.destination_host,
            destination_port=record.destination_port,
            created_at=record.created_at,
            active_at=record.active_at,
            closed_at=record.closed_at,
            owner_client_id=record.owner_client_id,
            failure=record.failure,
        )

    def _evict_closed_locked(self) -> None:
        closed = [
            forward_id
            for forward_id in self._creation_order
            if (
                forward_id in self._records
                and self._records[forward_id].state is ForwardState.CLOSED
            )
        ]
        excess = len(closed) - self._max_retained_closed_forwards
        for forward_id in closed[: max(0, excess)]:
            self._records.pop(forward_id, None)
            try:
                self._creation_order.remove(forward_id)
            except ValueError:
                pass

    def _require_accepting_commands_locked(self) -> None:
        if not self._accepting_commands or self._closed:
            raise SshPilotError(
                ErrorCode.DAEMON_SHUTTING_DOWN,
                "The forward runtime is shutting down",
                retryable=True,
            )

    def _require_accepting_reads_locked(self) -> None:
        if self._closed:
            raise SshPilotError(
                ErrorCode.DAEMON_SHUTTING_DOWN,
                "The forward runtime is shut down",
                retryable=True,
            )

"""Daemon-owned SFTP service lifecycle (``ssh … -s sftp``) and remote FS ops.

Mirrors the shape of :mod:`sshpilot.daemon.session_runtime`: a
``prepare_open``/``start`` pair for non-blocking startup, an explicit state
machine with public events, and a pluggable process runner so this module can
be unit-tested without spawning real ``ssh`` subprocesses. All remote
filesystem operations are blocking network calls and must be invoked from a
daemon command worker (see ``dispatch.py``); they are serialized per service
by :class:`~sshpilot.daemon.command_executor.BoundedCommandExecutor` using the
service id as the command key. File *replacements* are additionally serialized
per target inside the runtime: the complete compare-and-replace sequence (read,
revision compare, backup, temporary write, atomic replace, publish) runs under
a per-target lock keyed by target kind, service id, and canonical/validated
path, so concurrent same-revision replacements yield one success and one
``FILE_REVISION_CONFLICT`` without blocking unrelated services or paths.

Interaction/host-key trust reuses :class:`~sshpilot.daemon.session_runtime.SessionLaunchSpec`
and the existing :class:`~sshpilot.daemon.interaction_broker.InteractionBroker`
unchanged — an SFTP service's public id is passed as the spec's
``session_id`` so the broker (and this module's own ``CoreEvent``s) can key
interaction eligibility exactly like a terminal session. No new askpass or
trust path is introduced.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import posixpath
import stat as stat_module
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    SessionId,
    SftpServiceId,
    utc_now,
)
from sshpilot.api.models.operations import (
    AttachSftpRequest,
    CloseSftpRequest,
    ListDirectoryRequest,
    ListDirectoryResult,
    OpenSftpRequest,
    OperationKind,
    OperationSummary,
    RemoteFileEntry,
    RemoteFileType,
    SftpFailure,
    SftpFailureCode,
    SftpChmodRequest,
    SftpCopyRequest,
    SftpCreateFileRequest,
    SftpCreateFileResult,
    SftpDirectorySizeRequest,
    SftpDirectorySizeResult,
    SftpFileAccess,
    SftpFileTarget,
    SftpPathRequest,
    SftpReadFileRequest,
    SftpReadFileResult,
    SftpRenameRequest,
    SftpReplaceFileRequest,
    SftpReplaceFileResult,
    SftpServiceState,
    SftpServiceSummary,
    SftpSymlinkRequest,
)
from sshpilot.api.remote_path import (
    RemotePathError,
    remote_path_basename,
    remote_path_join,
    validate_remote_path,
)
from sshpilot.api.sftp_identity import new_sftp_id
from sshpilot.sftp import protocol as sftp_proto
from sshpilot.sftp.client import OpenSSHSFTPClient
from sshpilot.logging_support import log_context

from .operation_runtime import OperationCancelled
from .session_runtime import SessionLaunchSpec

from .process_registry import KIND_SFTP, forget_owned_process, record_owned_process_or_abandon

logger = logging.getLogger(__name__)

DEFAULT_MAX_RETAINED_CLOSED_SERVICES = 50
DEFAULT_LIST_LIMIT = 2000
DEFAULT_MAX_FILE_BYTES = 1024 * 1024
DEFAULT_CONNECT_TIMEOUT_SECONDS = 30.0
DEFAULT_TERMINATE_GRACE_SECONDS = 2.0
DEFAULT_PROCESS_POLL_INTERVAL = 0.02
_LOCAL_AUTHORIZED_KEYS_MARKER = "~/.ssh/authorized_keys"

SftpExitCallback = Callable[[Optional[int]], None]


def _file_revision(content: bytes, exists: bool) -> str:
    if not exists:
        return "absent"
    return hashlib.sha256(content).hexdigest()


def _coarse_progress(processed: int, pending: int) -> float:
    """Monotonic bounded walk progress from completed/pending entry counts."""
    if pending <= 0:
        return 1.0
    return min(0.99, processed / (processed + pending))


def _read_local_authorized_keys() -> tuple[str, bytes, int | None]:
    path = os.path.expanduser(_LOCAL_AUTHORIZED_KEYS_MARKER)
    try:
        with open(path, "rb") as handle:
            content = handle.read(DEFAULT_MAX_FILE_BYTES + 1)
        mode = os.stat(path).st_mode & 0o7777
    except FileNotFoundError:
        return path, b"", None
    except OSError as exc:
        raise SshPilotError(
            ErrorCode.REMOTE_PERMISSION_DENIED,
            "The local authorized_keys file could not be read",
        ) from exc
    if len(content) > DEFAULT_MAX_FILE_BYTES:
        raise SshPilotError(
            ErrorCode.FILE_CONTENT_TOO_LARGE,
            "The authorized_keys file is too large",
        )
    return path, content, mode


class SftpProcessHandle(Protocol):
    """Owned SFTP transport: a live client plus its underlying process."""

    client: OpenSSHSFTPClient

    def terminate(self) -> None: ...

    def wait(self, timeout: float) -> bool:
        """Return True once the underlying process has exited."""
        ...


class SftpProcessRunner(Protocol):
    """Narrow launch boundary, mirroring ``SessionProcessRunner``."""

    def start(
        self, spec: SessionLaunchSpec, on_exit: SftpExitCallback
    ) -> SftpProcessHandle: ...

    def close(self) -> None: ...


class UnsupportedSftpProcessRunner:
    """Default runner until a daemon launch builder is wired in."""

    def start(
        self, spec: SessionLaunchSpec, on_exit: SftpExitCallback
    ) -> SftpProcessHandle:
        del on_exit
        raise SshPilotError(
            ErrorCode.SFTP_SERVICE_NOT_READY,
            "SFTP service startup requires daemon runtime support",
            connection_id=spec.connection_id,
        )

    def close(self) -> None:
        return


class _SubprocessSftpHandle:
    def __init__(
        self,
        process: "subprocess.Popen",
        client: OpenSSHSFTPClient,
        on_exit: SftpExitCallback,
        unregister: Callable[["_SubprocessSftpHandle"], None],
    ) -> None:
        self._process = process
        self.client = client
        self._on_exit = on_exit
        self._unregister = unregister
        self._lock = threading.Lock()
        self._terminated = False
        self._notified = False

    def terminate(self) -> None:
        with self._lock:
            if self._terminated:
                return
            self._terminated = True
        try:
            self.client.close()
        except Exception:  # pragma: no cover - best effort
            pass
        if self._process.poll() is None:
            try:
                self._process.terminate()
            except Exception:  # pragma: no cover - best effort
                pass

    def wait(self, timeout: float) -> bool:
        try:
            return_code = self._process.wait(timeout=max(0.0, timeout))
        except subprocess.TimeoutExpired:
            return False
        self._notify(return_code)
        return True

    def poll_and_notify(self) -> bool:
        return_code = self._process.poll()
        if return_code is None:
            return False
        self._notify(return_code)
        return True

    def _notify(self, return_code: int) -> None:
        with self._lock:
            if self._notified:
                return
            self._notified = True
        forget_owned_process(self._process.pid)
        self._unregister(self)
        self._on_exit(return_code)


class SubprocessSftpProcessRunner:
    """Spawn ``ssh … -s sftp`` and perform the blocking SFTP handshake.

    ``command_builder`` is the daemon-internal injection point (never reached
    through the wire protocol) that returns the canonical argv/env for one
    SFTP launch — production wiring goes through the same
    ``InteractionBroker``/askpass path as terminal sessions (see
    ``DaemonServer._prepare_sftp_launch``).

    A shared reaper thread polls live handles the same way
    ``SubprocessSessionProcessRunner`` does, so an unexpected ``ssh`` exit
    (network drop, remote reboot) fails the SFTP service immediately instead
    of leaving the file-manager tab READY until the next operation.
    """

    def __init__(
        self,
        command_builder: Callable[[SessionLaunchSpec], Tuple[Sequence[str], Dict[str, str]]],
        *,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        poll_interval: float = DEFAULT_PROCESS_POLL_INTERVAL,
    ) -> None:
        if not callable(command_builder):
            raise TypeError("SFTP command builder must be callable")
        if poll_interval <= 0:
            raise ValueError("SFTP process poll interval must be positive")
        self._command_builder = command_builder
        self._connect_timeout = float(connect_timeout)
        self._poll_interval = float(poll_interval)
        self._condition = threading.Condition()
        self._handles: Set[_SubprocessSftpHandle] = set()
        self._closed = False
        self._thread = threading.Thread(
            target=self._reaper_main,
            name="sshpilot-sftp-reaper",
            daemon=True,
        )
        self._thread.start()

    def start(
        self, spec: SessionLaunchSpec, on_exit: SftpExitCallback
    ) -> SftpProcessHandle:
        argv, environment = self._command_builder(spec)
        argv = tuple(argv)
        if not argv or any(type(item) is not str or not item for item in argv):
            raise SshPilotError(
                ErrorCode.SFTP_SERVICE_NOT_READY,
                "The SFTP launch command is invalid",
                connection_id=spec.connection_id,
            )
        with self._condition:
            if self._closed:
                raise RuntimeError("SFTP process runner is closed")
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=dict(environment),
            close_fds=True,
        )
        client = OpenSSHSFTPClient(
            process.stdin,
            process.stdout,
            on_close=lambda: self._terminate_process(process),
        )
        try:
            client.start()
        except Exception as exc:
            self._terminate_process(process)
            try:
                process.wait(timeout=self._connect_timeout)
            except Exception:  # pragma: no cover - defensive
                process.kill()
            raise SshPilotError(
                ErrorCode.SFTP_SERVICE_NOT_READY,
                "The SFTP session could not be established",
                connection_id=spec.connection_id,
            ) from exc
        record_owned_process_or_abandon(process, kind=KIND_SFTP)
        handle = _SubprocessSftpHandle(process, client, on_exit, self._unregister)
        with self._condition:
            if self._closed:
                handle.terminate()
                handle.wait(DEFAULT_TERMINATE_GRACE_SECONDS)
                raise RuntimeError("SFTP process runner is closed")
            self._handles.add(handle)
            self._condition.notify()
        return handle

    @staticmethod
    def _terminate_process(process: "subprocess.Popen") -> None:
        forget_owned_process(process.pid)
        if process.poll() is None:
            try:
                process.terminate()
            except Exception:  # pragma: no cover - best effort
                pass

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            handles = tuple(self._handles)
            self._condition.notify_all()
        for handle in handles:
            try:
                handle.terminate()
                handle.wait(DEFAULT_TERMINATE_GRACE_SECONDS)
            except Exception:  # pragma: no cover - best effort
                continue
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)

    def _unregister(self, handle: _SubprocessSftpHandle) -> None:
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
class _SftpRecord:
    service_id: SftpServiceId
    connection_id: ConnectionId
    state: SftpServiceState
    created_at: datetime
    started_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    owner_client_id: Optional[ClientId] = None
    attached_clients: Set[ClientId] = field(default_factory=set)
    failure: Optional[SftpFailure] = None
    handle: Optional[SftpProcessHandle] = None
    launch_spec: Optional[SessionLaunchSpec] = None
    close_scheduled: bool = False
    # Resolved home directory (REALPATH(".")), used to expand ``~`` paths.
    home: Optional[str] = None


@dataclass
class _TargetLockEntry:
    """One per-target replacement mutex plus its reference count."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    users: int = 0


_ALLOWED_TRANSITIONS = {
    SftpServiceState.CREATED: frozenset(
        {SftpServiceState.STARTING, SftpServiceState.FAILED, SftpServiceState.CLOSED}
    ),
    SftpServiceState.STARTING: frozenset(
        {SftpServiceState.READY, SftpServiceState.CLOSING, SftpServiceState.FAILED}
    ),
    SftpServiceState.READY: frozenset(
        {SftpServiceState.CLOSING, SftpServiceState.FAILED}
    ),
    SftpServiceState.CLOSING: frozenset(
        {SftpServiceState.CLOSED, SftpServiceState.FAILED}
    ),
    SftpServiceState.FAILED: frozenset({SftpServiceState.CLOSED}),
    SftpServiceState.CLOSED: frozenset(),
}


def is_valid_sftp_transition(current: SftpServiceState, target: SftpServiceState) -> bool:
    if not isinstance(current, SftpServiceState) or not isinstance(target, SftpServiceState):
        raise TypeError("SFTP transitions require SftpServiceState values")
    return target in _ALLOWED_TRANSITIONS[current]


_ERRNO_TO_ERROR_CODE = {
    errno.ENOENT: ErrorCode.REMOTE_PATH_NOT_FOUND,
    errno.EACCES: ErrorCode.REMOTE_PERMISSION_DENIED,
    errno.EPIPE: ErrorCode.SFTP_PROTOCOL_LOST,
}

_SFTP_STATUS_TO_ERROR_CODE = {
    sftp_proto.FX_NO_SUCH_FILE: ErrorCode.REMOTE_PATH_NOT_FOUND,
    sftp_proto.FX_PERMISSION_DENIED: ErrorCode.REMOTE_PERMISSION_DENIED,
    sftp_proto.FX_NO_CONNECTION: ErrorCode.SFTP_PROTOCOL_LOST,
    sftp_proto.FX_CONNECTION_LOST: ErrorCode.SFTP_PROTOCOL_LOST,
    sftp_proto.FX_OP_UNSUPPORTED: ErrorCode.REMOTE_UNSUPPORTED_OPERATION,
}

_GENERIC_SFTP_STATUS_TEXT = frozenset(
    {
        "",
        "Failure",
        "OK",
        "EOF",
        "No such file",
        "Permission denied",
        "Bad message",
        "No connection",
        "Connection lost",
        "Operation unsupported",
    }
)


class _SftpSummaryError(SshPilotError):
    """A direct-compatible error with structured summary presentation data."""

    def __init__(
        self,
        error_code: ErrorCode,
        message: str,
        failure_code: SftpFailureCode,
        *,
        parameters: Optional[Dict[str, str]] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(error_code, message, details=details)
        self.summary_failure = SftpFailure(
            code=failure_code,
            error_code=error_code,
            parameters=parameters or {},
        )


def _specific_server_diagnostic(error: SshPilotError) -> str:
    details = error.details
    if details.get("server_message_is_specific") is not True:
        return ""
    diagnostic = details.get("server_message")
    return diagnostic if type(diagnostic) is str else ""


def _lifecycle_failure(error: SshPilotError) -> SftpFailure:
    code = {
        ErrorCode.SERVER_BUSY: SftpFailureCode.COMMAND_QUEUE_FULL,
        ErrorCode.SFTP_PROTOCOL_LOST: SftpFailureCode.CONNECTION_LOST,
        ErrorCode.SFTP_SERVICE_NOT_FOUND: SftpFailureCode.SERVICE_NOT_FOUND,
    }.get(error.code, SftpFailureCode.SESSION_ESTABLISHMENT_FAILED)
    return SftpFailure(
        code=code,
        error_code=error.code,
        diagnostic=_specific_server_diagnostic(error),
    )


def _operation_failure(error: BaseException) -> SftpFailure:
    structured = getattr(error, "summary_failure", None)
    if type(structured) is SftpFailure:
        return structured
    if isinstance(error, SshPilotError):
        code = {
            ErrorCode.SFTP_PROTOCOL_LOST: SftpFailureCode.CONNECTION_LOST,
            ErrorCode.SFTP_SERVICE_NOT_FOUND: SftpFailureCode.SERVICE_NOT_FOUND,
            ErrorCode.SFTP_SERVICE_NOT_READY: SftpFailureCode.SERVICE_NOT_READY,
            ErrorCode.SERVICE_OWNER_REQUIRED: SftpFailureCode.SERVICE_OWNER_REQUIRED,
            ErrorCode.REMOTE_PATH_NOT_FOUND: SftpFailureCode.PATH_NOT_FOUND,
            ErrorCode.REMOTE_PERMISSION_DENIED: SftpFailureCode.PERMISSION_DENIED,
            ErrorCode.PERMISSION_DENIED: SftpFailureCode.PERMISSION_DENIED,
            ErrorCode.REMOTE_UNSUPPORTED_OPERATION: (
                SftpFailureCode.UNSUPPORTED_OPERATION
            ),
        }.get(error.code, SftpFailureCode.COMMAND_FAILED)
        return SftpFailure(
            code=code,
            error_code=error.code,
            diagnostic=_specific_server_diagnostic(error),
        )
    return SftpFailure(
        code=SftpFailureCode.OPERATION_FAILED_UNEXPECTEDLY,
        error_code=ErrorCode.INTERNAL_ERROR,
    )


def _validate_path(value: Any, field_name: str = "remote path") -> str:
    """Validate a remote path, converting failures into a stable error code.

    Blocking filesystem ops run on a command worker (see module docstring),
    so a bare ``ValueError`` here would otherwise surface to the client as an
    opaque ``INTERNAL_ERROR`` (see ``DaemonServer._submit_deferred``).
    """

    try:
        return validate_remote_path(value, field_name=field_name)
    except RemotePathError as exc:
        raise SshPilotError(ErrorCode.INVALID_REQUEST, str(exc)) from exc


def _remote_path_is_descendant(source: str, destination: str) -> bool:
    source = posixpath.normpath(source)
    destination = posixpath.normpath(destination)
    if source in ("", ".", "/"):
        return False
    return destination == source or destination.startswith(source.rstrip("/") + "/")


def _file_type(mode: int) -> RemoteFileType:
    if stat_module.S_ISDIR(mode):
        return RemoteFileType.DIRECTORY
    if stat_module.S_ISLNK(mode):
        return RemoteFileType.SYMLINK
    if stat_module.S_ISSOCK(mode):
        return RemoteFileType.SOCKET
    if stat_module.S_ISFIFO(mode):
        return RemoteFileType.FIFO
    if stat_module.S_ISBLK(mode):
        return RemoteFileType.BLOCK
    if stat_module.S_ISCHR(mode):
        return RemoteFileType.CHARACTER
    if stat_module.S_ISREG(mode):
        return RemoteFileType.REGULAR
    return RemoteFileType.UNKNOWN


class SftpServiceRuntime:
    """Serialize daemon-lifetime SFTP service state and owned SSH processes."""

    def __init__(
        self,
        core_client: SshPilotClient,
        *,
        runner: Optional[Any] = None,
        privileged_file_runner: Optional[Any] = None,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        id_factory: Callable[[], SftpServiceId] = new_sftp_id,
        shutdown_timeout_seconds: float = 3.0,
        max_retained_closed_services: int = DEFAULT_MAX_RETAINED_CLOSED_SERVICES,
        list_limit: int = DEFAULT_LIST_LIMIT,
        operation_lifecycle: Optional[Any] = None,
    ) -> None:
        if shutdown_timeout_seconds < 0:
            raise ValueError("SFTP shutdown timeout must not be negative")
        if type(max_retained_closed_services) is not int or max_retained_closed_services < 0:
            raise ValueError("closed-service retention limit must not be negative")
        if type(list_limit) is not int or list_limit < 1:
            raise ValueError("SFTP list limit must be positive")
        self._core_client = core_client
        self._runner: Any = runner or UnsupportedSftpProcessRunner()
        self._privileged_file_runner: Any = privileged_file_runner
        self._clock = clock
        self._monotonic = monotonic
        self._id_factory = id_factory
        self._shutdown_timeout_seconds = float(shutdown_timeout_seconds)
        self._max_retained_closed_services = max_retained_closed_services
        self._list_limit = list_limit
        self._operation_lifecycle: Any = operation_lifecycle
        self._lock = threading.RLock()
        self._publisher = EventPublisher()
        self._records: Dict[SftpServiceId, _SftpRecord] = {}
        self._creation_order: List[SftpServiceId] = []
        self._target_locks: Dict[Tuple[Any, ...], _TargetLockEntry] = {}
        self._accepting_commands = True
        self._closed = False
        self._authenticated_callback: Optional[Callable[[SessionId], None]] = None

    def set_authenticated_callback(
        self, callback: Optional[Callable[[SessionId], None]]
    ) -> None:
        """Notify the owner once an SFTP handshake confirms authentication.

        Mirrors ``SessionRuntime.set_authenticated_callback`` — without this,
        a "Remember password" checked on the SFTP askpass prompt (file
        manager) is captured into the broker's pending-remember state but
        never flushed to the password store, because only terminal sessions
        called ``InteractionBroker.mark_authenticated``.
        """

        self._authenticated_callback = callback

    def subscribe_events(self, callback: CoreEventCallback) -> Subscription:
        with self._lock:
            if self._closed:
                raise SshPilotError(
                    ErrorCode.INVALID_REQUEST,
                    "The SFTP runtime is closed",
                )
        return self._publisher.subscribe(callback)

    # -- reads --------------------------------------------------------
    def list_services(self) -> List[SftpServiceSummary]:
        with self._lock:
            self._require_accepting_reads_locked()
            return [
                self._summary_locked(self._records[service_id])
                for service_id in self._creation_order
                if service_id in self._records
            ]

    def get_service(self, service_id: SftpServiceId) -> SftpServiceSummary:
        with self._lock:
            self._require_accepting_reads_locked()
            return self._summary_locked(self._record_locked(service_id))

    # -- lifecycle ------------------------------------------------------
    def prepare_open_service(
        self,
        request: OpenSftpRequest,
        *,
        client_id: ClientId,
    ) -> SftpServiceSummary:
        """Create one starting record without calling the process runner."""

        if type(request) is not OpenSftpRequest:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "An open SFTP request is required",
            )
        connection = self._core_client.get_connection(request.connection_id)
        if connection.protocol != "ssh":
            raise SshPilotError(
                ErrorCode.UNSUPPORTED_SESSION_PROTOCOL,
                "The connection protocol cannot start an SFTP service",
                connection_id=request.connection_id,
            )
        service_id = self._id_factory()
        now = self._clock()
        record = _SftpRecord(
            service_id=service_id,
            connection_id=connection.id,
            state=SftpServiceState.CREATED,
            created_at=now,
            owner_client_id=client_id,
            attached_clients={client_id},
            # The SFTP id doubles as the interaction/eligibility key (see
            # module docstring) — no new broker trust path is introduced.
            launch_spec=SessionLaunchSpec(
                session_id=SessionId(str(service_id)),
                connection_id=connection.id,
                protocol=connection.protocol,
                hostname=connection.hostname,
                username=connection.username,
                port=connection.port,
            ),
        )
        with self._lock:
            self._require_accepting_commands_locked()
            if service_id in self._records:
                raise RuntimeError("SFTP id factory reused an active identifier")
            self._records[service_id] = record
            self._creation_order.append(service_id)
            created_event = self._event_locked(record, EventType.SFTP_CREATED)
            starting_event = self._transition_locked(record, SftpServiceState.STARTING)
        self._publish((created_event, starting_event))
        with self._lock:
            return self._summary_locked(record)

    def start_service(self, service_id: SftpServiceId) -> None:
        """Run the blocking connect/handshake step on a command worker."""

        with self._lock:
            record = self._record_locked(service_id)
            if record.state not in {SftpServiceState.STARTING, SftpServiceState.CLOSING}:
                return
            spec = record.launch_spec
            if spec is None:
                raise RuntimeError("starting SFTP service has no launch specification")
        try:
            handle = self._runner.start(
                spec,
                lambda return_code, sid=service_id: self._on_process_exit(
                    sid, return_code
                ),
            )
            if handle is None:
                raise TypeError("SFTP runner returned no process handle")
        except SshPilotError as error:
            self._startup_failed(record, _lifecycle_failure(error))
            return
        except Exception:
            self._startup_failed(
                record,
                SftpFailure(
                    code=SftpFailureCode.SESSION_ESTABLISHMENT_FAILED,
                    error_code=ErrorCode.SFTP_SERVICE_NOT_READY,
                ),
            )
            return
        terminate_after_start = True
        became_ready = False
        events: List[CoreEvent] = []
        with self._lock:
            if record.state is SftpServiceState.STARTING:
                record.handle = handle
                record.started_at = self._clock()
                events.append(self._transition_locked(record, SftpServiceState.READY))
                terminate_after_start = False
                became_ready = True
            elif record.state is SftpServiceState.CLOSING:
                record.handle = handle
                terminate_after_start = False
        self._publish(events)
        if became_ready and self._authenticated_callback is not None:
            self._authenticated_callback(spec.session_id)
        if terminate_after_start:
            try:
                handle.terminate()
                handle.wait(DEFAULT_TERMINATE_GRACE_SECONDS)
            except Exception:  # pragma: no cover - best effort
                pass

    def reject_pending_start(self, service_id: SftpServiceId) -> None:
        self.fail_pending_start(
            service_id,
            SshPilotError(
                ErrorCode.SERVER_BUSY,
                "The daemon SFTP command queue is full",
                retryable=True,
            ),
        )

    def fail_pending_start(self, service_id: SftpServiceId, error: BaseException) -> None:
        if isinstance(error, SshPilotError):
            failure = _lifecycle_failure(error)
        else:
            failure = SftpFailure(
                code=SftpFailureCode.SESSION_ESTABLISHMENT_FAILED,
                error_code=ErrorCode.SFTP_SERVICE_NOT_READY,
            )
        self._startup_failed(self._records.get(service_id), failure, guarded=True)

    def _on_process_exit(
        self, service_id: SftpServiceId, return_code: Optional[int]
    ) -> None:
        """Fail a live SFTP service as soon as its ``ssh`` child exits.

        Terminals discover a dead session the same way (process reaper →
        SESSION_EXITED). Without this, a file-manager tab stayed READY until
        the next listdir hit a broken pipe.
        """
        del return_code
        with self._lock:
            record = self._records.get(service_id)
            if record is None:
                return
            state = record.state
        if state is SftpServiceState.STARTING:
            self._startup_failed(
                record,
                SftpFailure(
                    code=SftpFailureCode.SESSION_ESTABLISHMENT_FAILED,
                    error_code=ErrorCode.SFTP_SERVICE_NOT_READY,
                ),
            )
            return
        if state is not SftpServiceState.READY:
            return
        self._fail_dead_connection(
            record,
            SshPilotError(
                ErrorCode.SFTP_PROTOCOL_LOST,
                "The SFTP connection was lost",
                connection_id=record.connection_id,
            ),
        )

    def _startup_failed(
        self,
        record: Optional[_SftpRecord],
        failure: SftpFailure,
        *,
        guarded: bool = False,
    ) -> None:
        del guarded
        with self._lock:
            if record is None or record.state is not SftpServiceState.STARTING:
                return
            record.failure = failure
            event = self._transition_locked(record, SftpServiceState.FAILED)
        self._publish((event,))

    # -- attach/detach ----------------------------------------------------
    def attach_service(
        self,
        request: AttachSftpRequest,
        *,
        client_id: ClientId,
    ) -> SftpServiceSummary:
        if type(request) is not AttachSftpRequest:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "An attach SFTP request is required",
            )
        with self._lock:
            self._require_accepting_commands_locked()
            record = self._record_locked(request.service_id)
            if record.state is SftpServiceState.CLOSED:
                raise SshPilotError(
                    ErrorCode.SFTP_SERVICE_NOT_FOUND,
                    "The SFTP service is closed",
                    connection_id=record.connection_id,
                )
            record.attached_clients.add(client_id)
            return self._summary_locked(record)

    def detach_service(self, service_id: SftpServiceId, *, client_id: ClientId) -> None:
        with self._lock:
            record = self._records.get(service_id)
            if record is None:
                return
            record.attached_clients.discard(client_id)

    def detach_client(self, client_id: Optional[ClientId]) -> None:
        """Remove a disconnected client's attachments and orphan its services.

        The SFTP service keeps running; ownership is cleared so any
        reconnecting client can claim and manage the resource.
        """

        if client_id is None:
            return
        with self._lock:
            for record in self._records.values():
                record.attached_clients.discard(client_id)
                if record.owner_client_id == client_id:
                    record.owner_client_id = None

    def client_can_interact(self, service_id: SftpServiceId, client_id: ClientId) -> bool:
        with self._lock:
            record = self._records.get(service_id)
            return bool(
                record is not None
                and (
                    client_id == record.owner_client_id
                    or client_id in record.attached_clients
                )
            )

    def acquire_active_client(
        self,
        service_id: SftpServiceId,
        client_id: ClientId,
    ) -> Tuple[OpenSSHSFTPClient, ConnectionId]:
        """Return the live SFTP client for a READY service the caller may use.

        Used by :class:`~sshpilot.daemon.transfer_runtime.TransferRuntime` to
        stream bytes through an existing SFTP session without duplicating
        this runtime's readiness/ownership checks or opening a second
        connection.
        """

        record = self._ready_record_for_read(service_id, client_id)
        return record.handle.client, record.connection_id

    # -- close --------------------------------------------------------
    def prepare_close_service(
        self,
        request: CloseSftpRequest,
        *,
        client_id: ClientId,
    ) -> bool:
        if type(request) is not CloseSftpRequest:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "A close SFTP request is required",
            )
        events: List[CoreEvent] = []
        with self._lock:
            self._require_accepting_commands_locked()
            record = self._record_locked(request.service_id)
            self._require_owner(record, client_id)
            if record.state is SftpServiceState.CLOSED or record.close_scheduled:
                return False
            if record.state is SftpServiceState.CREATED:
                events.append(self._transition_locked(record, SftpServiceState.CLOSED))
            elif record.state is SftpServiceState.FAILED and record.handle is None:
                events.append(self._transition_locked(record, SftpServiceState.CLOSED))
            else:
                if record.state in {SftpServiceState.STARTING, SftpServiceState.READY}:
                    events.append(
                        self._transition_locked(record, SftpServiceState.CLOSING)
                    )
                record.close_scheduled = True
        self._publish(events)
        return record.close_scheduled

    def finish_close_service(self, service_id: SftpServiceId) -> None:
        self._close_service_id(service_id, raise_on_failure=True, allow_shutdown=True)

    def reject_pending_close(self, service_id: SftpServiceId) -> None:
        with self._lock:
            record = self._record_locked(service_id)
            if not record.close_scheduled:
                return
            record.close_scheduled = False
            if record.state is SftpServiceState.CLOSING:
                record.failure = SftpFailure(
                    code=SftpFailureCode.COMMAND_QUEUE_FULL,
                    error_code=ErrorCode.SERVER_BUSY,
                )
                event = self._transition_locked(record, SftpServiceState.FAILED)
            else:
                event = None
        if event is not None:
            self._publish((event,))

    def _close_service_id(
        self,
        service_id: SftpServiceId,
        *,
        raise_on_failure: bool,
        allow_shutdown: bool = False,
    ) -> None:
        events: List[CoreEvent] = []
        handle: Optional[SftpProcessHandle] = None
        with self._lock:
            if not allow_shutdown:
                self._require_accepting_commands_locked()
            record = self._record_locked(service_id)
            if record.state is SftpServiceState.CLOSED:
                return
            if record.state is SftpServiceState.CREATED:
                events.append(self._transition_locked(record, SftpServiceState.CLOSED))
            elif record.state is SftpServiceState.FAILED:
                handle = record.handle
                if handle is None:
                    events.append(self._transition_locked(record, SftpServiceState.CLOSED))
            elif record.state in {SftpServiceState.STARTING, SftpServiceState.READY}:
                events.append(self._transition_locked(record, SftpServiceState.CLOSING))
                handle = record.handle
            elif record.state is SftpServiceState.CLOSING:
                handle = record.handle
        self._publish(events)
        if handle is None:
            with self._lock:
                if record.state is SftpServiceState.CLOSING:
                    events = [self._transition_locked(record, SftpServiceState.CLOSED)]
                else:
                    events = []
            self._publish(events)
            return
        try:
            handle.terminate()
            if not handle.wait(DEFAULT_TERMINATE_GRACE_SECONDS):
                raise RuntimeError("owned SFTP process did not exit")
        except Exception:
            with self._lock:
                record.close_scheduled = False
                if record.state not in {SftpServiceState.FAILED, SftpServiceState.CLOSED}:
                    record.failure = SftpFailure(
                        code=SftpFailureCode.PROCESS_TERMINATION_FAILED,
                        error_code=ErrorCode.SFTP_SERVICE_NOT_READY,
                    )
                    events = [self._transition_locked(record, SftpServiceState.FAILED)]
                else:
                    events = []
            self._publish(events)
            if raise_on_failure:
                raise SshPilotError(
                    ErrorCode.SFTP_SERVICE_NOT_READY,
                    "The SFTP process could not be terminated",
                    connection_id=record.connection_id,
                ) from None
            return
        with self._lock:
            record.handle = None
            events = [self._transition_locked(record, SftpServiceState.CLOSED)]
        self._publish(events)

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._accepting_commands = False
            service_ids = tuple(self._creation_order)
        for service_id in service_ids:
            self._close_service_id(service_id, raise_on_failure=False, allow_shutdown=True)
        with self._lock:
            for record in self._records.values():
                record.attached_clients.clear()
            self._closed = True
        try:
            self._runner.close()
        finally:
            self._publisher.close()

    # -- bounded file content operations ----------------------------------
    def read_file(
        self,
        request: SftpReadFileRequest,
        *,
        client_id: ClientId,
    ) -> SftpReadFileResult:
        if type(request) is not SftpReadFileRequest:
            raise SshPilotError(ErrorCode.INVALID_REQUEST, "A SFTP read file request is required")
        if request.target is SftpFileTarget.LOCAL_AUTHORIZED_KEYS:
            if request.path != _LOCAL_AUTHORIZED_KEYS_MARKER:
                raise SshPilotError(ErrorCode.INVALID_REQUEST, "Only the local authorized_keys target is supported")
            path, content, mode = _read_local_authorized_keys()
            return SftpReadFileResult(
                target=request.target,
                path=path,
                content=content.decode("utf-8", errors="replace"),
                exists=mode is not None,
                revision=_file_revision(content, mode is not None),
                size=len(content),
                mode=mode,
            )
        path = _validate_path(request.path)
        if request.access is SftpFileAccess.SUDO:
            key = (SftpFileTarget.REMOTE, request.service_id, path)
            with self._serialized_target(key):
                self._require_accepting_commands()
                record = self._ready_record_for_read(request.service_id, client_id)
                return self._read_privileged(request, path, record)
        record = self._ready_record_for_read(request.service_id, client_id)
        client = record.handle.client
        try:
            attr = client.stat(path)
            if not stat_module.S_ISREG(attr.st_mode or 0):
                raise SshPilotError(
                    ErrorCode.REMOTE_IS_DIRECTORY,
                    "The requested remote path is not a regular file",
                    connection_id=record.connection_id,
                )
            if attr.st_size is not None and attr.st_size > DEFAULT_MAX_FILE_BYTES:
                raise SshPilotError(
                    ErrorCode.FILE_CONTENT_TOO_LARGE,
                    "The remote file is too large",
                    connection_id=record.connection_id,
                )
            with client.file(path, "rb") as handle:
                content = handle.read(DEFAULT_MAX_FILE_BYTES + 1)
        except SshPilotError:
            raise
        except Exception as exc:
            if getattr(exc, "errno", None) == errno.ENOENT:
                content = b""
                return SftpReadFileResult(
                    target=request.target,
                    path=path,
                    content="",
                    exists=False,
                    revision=_file_revision(content, False),
                    size=0,
                    mode=None,
                )
            raise self._map_error(exc, record) from exc
        if len(content) > DEFAULT_MAX_FILE_BYTES:
            raise SshPilotError(
                ErrorCode.FILE_CONTENT_TOO_LARGE,
                "The remote file is too large",
                connection_id=record.connection_id,
            )
        mode = (attr.st_mode & 0o7777) if attr.st_mode else None
        return SftpReadFileResult(
            target=request.target,
            path=path,
            content=content.decode("utf-8", errors="replace"),
            exists=True,
            revision=_file_revision(content, True),
            size=len(content),
            mode=mode,
        )

    def _read_privileged(
        self,
        request: SftpReadFileRequest,
        path: str,
        record: _SftpRecord,
    ) -> SftpReadFileResult:
        runner = self._require_privileged_runner(record)
        spec = record.launch_spec
        content = runner.read(
            connection_id=record.connection_id,
            scope_id=SessionId(str(record.service_id)),
            hostname=spec.hostname if spec is not None else "",
            username=spec.username if spec is not None else "",
            port=spec.port if spec is not None else 22,
            path=path,
        )
        payload = content.content
        return SftpReadFileResult(
            target=request.target,
            path=path,
            content=payload.decode("utf-8", errors="replace"),
            exists=content.exists,
            revision=_file_revision(payload, content.exists),
            size=len(payload),
            mode=content.mode,
        )

    def replace_file(
        self,
        request: SftpReplaceFileRequest,
        *,
        client_id: ClientId,
    ) -> SftpReplaceFileResult:
        if type(request) is not SftpReplaceFileRequest:
            raise SshPilotError(ErrorCode.INVALID_REQUEST, "A SFTP replace file request is required")
        payload = request.content.encode("utf-8")
        if len(payload) > DEFAULT_MAX_FILE_BYTES:
            raise SshPilotError(ErrorCode.FILE_CONTENT_TOO_LARGE, "The replacement file is too large")
        if request.target is SftpFileTarget.LOCAL_AUTHORIZED_KEYS:
            if request.path != _LOCAL_AUTHORIZED_KEYS_MARKER:
                raise SshPilotError(ErrorCode.INVALID_REQUEST, "Only the local authorized_keys target is supported")
            key = (
                SftpFileTarget.LOCAL_AUTHORIZED_KEYS,
                os.path.abspath(os.path.expanduser(_LOCAL_AUTHORIZED_KEYS_MARKER)),
            )
            with self._serialized_target(key):
                self._require_accepting_commands()
                return self._replace_local_authorized_keys(request, payload)
        path = _validate_path(request.path)
        key = (SftpFileTarget.REMOTE, request.service_id, path)
        with self._serialized_target(key):
            record = self._ready_record_for_mutation(request.service_id, client_id)
            if request.access is SftpFileAccess.SUDO:
                return self._replace_privileged(request, payload, path, record)
            return self._replace_remote_file(request, payload, path, record)

    def _replace_privileged(
        self,
        request: SftpReplaceFileRequest,
        payload: bytes,
        path: str,
        record: _SftpRecord,
    ) -> SftpReplaceFileResult:
        runner = self._require_privileged_runner(record)
        spec = record.launch_spec
        result = runner.replace(
            connection_id=record.connection_id,
            scope_id=SessionId(str(record.service_id)),
            hostname=spec.hostname if spec is not None else "",
            username=spec.username if spec is not None else "",
            port=spec.port if spec is not None else 22,
            path=path,
            payload=payload,
            expected_revision=request.expected_revision,
            backup=request.backup,
        )
        return SftpReplaceFileResult(
            target=request.target,
            path=path,
            revision=result.revision,
            size=result.size,
            backup_path=result.backup_path,
        )

    def create_file(
        self,
        request: SftpCreateFileRequest,
        *,
        client_id: ClientId,
    ) -> SftpCreateFileResult:
        """Create an empty remote file through the daemon-owned SFTP session."""
        if type(request) is not SftpCreateFileRequest:
            raise SshPilotError(ErrorCode.INVALID_REQUEST, "A SFTP create file request is required")
        record = self._ready_record_for_mutation(request.service_id, client_id)
        path = _validate_path(request.path)
        client = record.handle.client
        try:
            handle = client.open_handle(
                path,
                sftp_proto.FXF_WRITE | sftp_proto.FXF_CREAT | sftp_proto.FXF_EXCL,
            )
            try:
                client.close_handle(handle)
            except Exception:
                pass
        except (sftp_proto.SFTPError, FileNotFoundError) as exc:
            # An exclusive-create open atomically fails when the path already
            # exists (or the parent cannot be written); report the canonical
            # structured error instead of truncating a racing file.
            exists = False
            try:
                attr = client.stat(path)
                exists = True
            except Exception:
                exists = False
            if exists:
                raise SshPilotError(
                    ErrorCode.REMOTE_PATH_EXISTS,
                    "The remote file already exists",
                    connection_id=record.connection_id,
                    details={"path": path},
                ) from exc
            raise self._map_error(exc, record) from exc
        except SshPilotError:
            raise
        except Exception as exc:
            raise self._map_error(exc, record) from exc
        mode = 0o644
        try:
            attr = client.stat(path)
            mode = stat_module.S_IMODE(attr.st_mode)
        except Exception:
            pass
        return SftpCreateFileResult(path=path, mode=mode)

    def _require_privileged_runner(self, record: _SftpRecord):
        runner = self._privileged_file_runner
        if runner is None:
            raise SshPilotError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "Privileged file operations are unavailable",
                connection_id=record.connection_id,
                details={"service_id": record.service_id},
            )
        return runner

    @property
    def privileged_file_supported(self) -> bool:
        return self._privileged_file_runner is not None

    def _replace_remote_file(
        self,
        request: SftpReplaceFileRequest,
        payload: bytes,
        path: str,
        record: _SftpRecord,
    ) -> SftpReplaceFileResult:
        client = record.handle.client
        try:
            current = self._read_remote_bytes(client, path)
            current_content, current_mode = current
            current_revision = _file_revision(current_content, current_mode is not None)
            if current_revision != request.expected_revision:
                raise SshPilotError(
                    ErrorCode.FILE_REVISION_CONFLICT,
                    "The remote file changed since it was read",
                    connection_id=record.connection_id,
                )
            parent = posixpath.dirname(path)
            if parent:
                try:
                    client.mkdir(parent, 0o700)
                except Exception as exc:
                    # SFTP v3 has no EEXIST status: servers answer a bare
                    # FX_FAILURE when the directory already exists, which
                    # SFTPError maps to EIO — never EEXIST/EISDIR, so an errno
                    # check here can never pass. Confirm the parent is a
                    # usable directory instead; only a genuinely missing or
                    # non-directory parent is an error.
                    try:
                        parent_attr = client.stat(parent)
                    except Exception:
                        raise self._map_error(exc, record) from exc
                    if not stat_module.S_ISDIR(parent_attr.st_mode or 0):
                        raise self._map_error(exc, record) from exc
            backup_path = None
            if request.backup and current_mode is not None:
                backup_path = f"{path}.bak-{time.time_ns()}"
                try:
                    self._write_remote_bytes(client, backup_path, current_content, 0o600)
                except Exception as exc:
                    raise SshPilotError(
                        ErrorCode.FILE_BACKUP_FAILED,
                        "The remote file backup could not be created",
                        connection_id=record.connection_id,
                    ) from exc
            temporary = f"{path}.sshpilot.tmp-{time.time_ns()}"
            try:
                self._write_remote_bytes(client, temporary, payload, 0o600)
                try:
                    client.posix_rename(temporary, path)
                except Exception:
                    try:
                        client.remove(path)
                    except Exception:
                        pass
                    client.rename(temporary, path)
                try:
                    client.chmod(path, 0o600)
                except Exception:
                    logger.debug("Could not enforce remote file mode", exc_info=True)
            except SshPilotError:
                raise
            except Exception as exc:
                try:
                    client.remove(temporary)
                except Exception:
                    pass
                raise SshPilotError(
                    ErrorCode.FILE_REPLACEMENT_FAILED,
                    "The remote file could not be replaced",
                    connection_id=record.connection_id,
                ) from exc
            return SftpReplaceFileResult(
                target=request.target,
                path=path,
                revision=_file_revision(payload, True),
                size=len(payload),
                backup_path=backup_path,
            )
        except SshPilotError:
            raise
        except Exception as exc:
            raise self._map_error(exc, record) from exc

    @staticmethod
    def _read_remote_bytes(client: OpenSSHSFTPClient, path: str) -> tuple[bytes, int | None]:
        try:
            attr = client.stat(path)
        except Exception as exc:
            if getattr(exc, "errno", None) == errno.ENOENT:
                return b"", None
            raise
        if not stat_module.S_ISREG(attr.st_mode or 0):
            raise SshPilotError(ErrorCode.REMOTE_IS_DIRECTORY, "The remote path is not a regular file")
        if attr.st_size is not None and attr.st_size > DEFAULT_MAX_FILE_BYTES:
            raise SshPilotError(ErrorCode.FILE_CONTENT_TOO_LARGE, "The remote file is too large")
        with client.file(path, "rb") as handle:
            content = handle.read(DEFAULT_MAX_FILE_BYTES + 1)
        if len(content) > DEFAULT_MAX_FILE_BYTES:
            raise SshPilotError(ErrorCode.FILE_CONTENT_TOO_LARGE, "The remote file is too large")
        return content, ((attr.st_mode or 0) & 0o7777)

    @staticmethod
    def _write_remote_bytes(client: OpenSSHSFTPClient, path: str, content: bytes, mode: int) -> None:
        with client.file(path, "wb") as handle:
            client.chmod(path, mode)
            handle.write(content)
        client.chmod(path, mode)

    @staticmethod
    def _replace_local_authorized_keys(
        request: SftpReplaceFileRequest,
        payload: bytes,
    ) -> SftpReplaceFileResult:
        path, current, mode = _read_local_authorized_keys()
        current_revision = _file_revision(current, mode is not None)
        if current_revision != request.expected_revision:
            raise SshPilotError(
                ErrorCode.FILE_REVISION_CONFLICT,
                "The local authorized_keys file changed since it was read",
            )
        directory = os.path.dirname(path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        backup_path = None
        if request.backup and mode is not None:
            backup_path = f"{path}.bak-{time.time_ns()}"
            try:
                with open(backup_path, "wb") as handle:
                    handle.write(current)
                os.chmod(backup_path, 0o600)
            except OSError as exc:
                raise SshPilotError(
                    ErrorCode.FILE_BACKUP_FAILED,
                    "The local authorized_keys backup could not be created",
                ) from exc
        temporary = f"{path}.sshpilot.tmp-{time.time_ns()}"
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        except OSError as exc:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise SshPilotError(
                ErrorCode.FILE_REPLACEMENT_FAILED,
                "The local authorized_keys file could not be replaced",
            ) from exc
        return SftpReplaceFileResult(
            target=request.target,
            path=path,
            revision=_file_revision(payload, True),
            size=len(payload),
            backup_path=backup_path,
        )

    # -- remote filesystem operations -------------------------------------
    def _expand_tilde_path(self, record: _SftpRecord, path: str) -> str:
        """Expand a leading ``~``/``~/`` using the service's home directory.

        OpenSSH's sftp-server performs no tilde expansion in OPENDIR (the
        ``expand-path@openssh.com`` extension is opt-in and the app's client
        does not use it), so a literal ``~`` sent to ``FXP_OPENDIR`` fails
        with FX_NO_SUCH_FILE. Resolve the home once per service via
        REALPATH(".") -- the sftp-server's cwd is the user's home -- and
        expand here, mirroring ``DaemonSftpManager._resolve_home()``.

        Only ``list_directory`` expands today; the other path-accepting
        operations (stat/read/write/remove/...) still pass ``~`` through raw.
        """
        if not (path == "~" or path.startswith("~/")):
            return path
        home = record.home
        if home is None:
            try:
                home = record.handle.client.realpath(".")
            except Exception:
                home = None
            if home:
                with self._lock:
                    if record.home is None:
                        record.home = home
        if not home:
            return path
        if path == "~":
            return home
        expanded = home.rstrip("/") + "/" + path[2:]
        return expanded.rstrip("/") or home

    def list_directory(
        self,
        request: ListDirectoryRequest,
        *,
        client_id: ClientId,
    ) -> ListDirectoryResult:
        if type(request) is not ListDirectoryRequest:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "A list directory request is required",
            )
        if request.service_id is None:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "A SFTP service id is required to list a directory",
            )
        record = self._ready_record_for_read(request.service_id, client_id)
        path = self._expand_tilde_path(record, _validate_path(request.path))
        limit = min(request.limit or self._list_limit, self._list_limit)
        client = record.handle.client
        try:
            attrs = client.listdir_attr(path)
        except Exception as exc:
            raise self._map_error(exc, record) from exc
        truncated = len(attrs) > limit
        entries = []
        for attr in attrs[:limit]:
            name = attr.filename or ""
            entries.append(
                RemoteFileEntry(
                    name=name,
                    path=remote_path_join(path, name),
                    file_type=_file_type(attr.st_mode or 0),
                    size=(int(attr.st_size) if attr.st_size is not None else None),
                    mode=((attr.st_mode & 0o7777) if attr.st_mode else None),
                    uid=attr.st_uid,
                    gid=attr.st_gid,
                    modified_at=(
                        datetime.fromtimestamp(attr.st_mtime, tz=timezone.utc)
                        if attr.st_mtime
                        else None
                    ),
                )
            )
        return ListDirectoryResult(
            path=path,
            entries=tuple(entries),
            truncated=truncated,
            next_cursor=None,
        )

    def stat_path(
        self,
        request: SftpPathRequest,
        *,
        client_id: ClientId,
        follow_symlinks: bool = True,
    ) -> RemoteFileEntry:
        if type(request) is not SftpPathRequest:
            raise SshPilotError(ErrorCode.INVALID_REQUEST, "A SFTP path request is required")
        record = self._ready_record_for_read(request.service_id, client_id)
        path = _validate_path(request.path)
        client = record.handle.client
        try:
            attr = client.stat(path) if follow_symlinks else client.lstat(path)
        except Exception as exc:
            raise self._map_error(exc, record) from exc
        return self._entry_from_attr(path, attr)

    def lstat_path(self, request: SftpPathRequest, *, client_id: ClientId) -> RemoteFileEntry:
        return self.stat_path(request, client_id=client_id, follow_symlinks=False)

    def directory_size(
        self,
        request: SftpDirectorySizeRequest,
        *,
        client_id: ClientId,
        progress: Optional[Callable[[float], None]] = None,
        cancel: Optional[Callable[[], bool]] = None,
    ) -> SftpDirectorySizeResult:
        """Recursively summarise a remote directory tree owned by the daemon.

        Symlinked entries are never descended into (matching the transfer
        runtime's no-follow policy), so the walk cannot loop on cycles or
        escape the requested tree. When running as a long-lived operation the
        walk reports coarse progress and cooperates with cancellation.
        """
        if type(request) is not SftpDirectorySizeRequest:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST, "A SFTP directory size request is required"
            )
        record = self._ready_record_for_read(request.service_id, client_id)
        path = _validate_path(request.path)
        client = record.handle.client
        try:
            try:
                root_attr = client.lstat(path)
            except Exception as exc:
                raise self._map_error(exc, record) from exc
            if root_attr.is_symlink() or not root_attr.is_dir():
                raise _SftpSummaryError(
                    ErrorCode.VALIDATION_FAILED,
                    "Directory size requires a real directory path, not a symbolic link",
                    SftpFailureCode.DIRECTORY_SIZE_REQUIRES_REAL_DIRECTORY,
                    details={"service_id": record.service_id},
                )
            total, file_count, directory_count = self._walk_directory_size(
                client, path, progress=progress, cancel=cancel
            )
        except OperationCancelled:
            raise
        except SshPilotError:
            raise
        except Exception as exc:
            raise self._map_error(exc, record) from exc
        return SftpDirectorySizeResult(
            path=path,
            size_bytes=total,
            file_count=file_count,
            directory_count=directory_count,
        )

    @staticmethod
    def _walk_directory_size(
        client,
        path: str,
        *,
        progress: Optional[Callable[[float], None]] = None,
        cancel: Optional[Callable[[], bool]] = None,
    ) -> Tuple[int, int, int]:
        """Iterative best-effort tree summary shared by sync and operation paths."""
        total = 0
        file_count = 0
        directory_count = 0
        processed = 0
        pending = [path]
        while pending:
            if cancel is not None and cancel():
                raise OperationCancelled()
            current = pending.pop()
            try:
                attrs = client.listdir_attr(current)
            except Exception as exc:
                if current == path:
                    raise exc
                continue
            processed += 1
            for attr in attrs:
                if attr.is_dir() and not attr.is_symlink():
                    directory_count += 1
                    pending.append(remote_path_join(current, attr.filename or ""))
                else:
                    file_count += 1
                    total += int(attr.st_size or 0)
            if progress is not None:
                progress(_coarse_progress(processed, len(pending)))
        if progress is not None:
            progress(1.0)
        return total, file_count, directory_count

    def start_directory_size(
        self,
        request: SftpDirectorySizeRequest,
        *,
        client_id: ClientId,
    ) -> OperationSummary:
        """Start a daemon operation that recursively measures a remote tree.

        The heavy walk runs on the shared operation worker (not the SFTP
        command stream), reporting progress and honouring cancellation. The
        measured result is attached to the succeeded summary's ``result``.
        """
        if type(request) is not SftpDirectorySizeRequest:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST, "A SFTP directory size request is required"
            )
        path = _validate_path(request.path)
        record = self._ready_record_for_read(request.service_id, client_id)
        runtime = self._require_operation_lifecycle()

        def _body(handle) -> str:
            handle.report("Measuring directory…", 0.0)

            def _should_cancel() -> bool:
                handle.raise_if_cancelled()
                return False

            with self._serialized_target((request.service_id, "sftp-operation")):
                self._ready_record_for_read(request.service_id, client_id)
                handle.raise_if_cancelled()
                result = self.directory_size(
                    request,
                    client_id=client_id,
                    progress=lambda fraction: handle.report(
                        "Measuring directory…", fraction
                    ),
                    cancel=_should_cancel,
                )
            from sshpilot.api.transport.codec import sftp_directory_size_result_to_wire

            handle.set_result(sftp_directory_size_result_to_wire(result))
            return (
                f"Measured {result.file_count} files, "
                f"{result.directory_count} directories"
            )

        return runtime.start_operation(
            OperationKind.SFTP_DIRECTORY_SIZE,
            _body,
            connection_id=record.connection_id,
            owner_client_id=client_id,
            message=f"Measuring {path}",
            failure_mapper=_operation_failure,
        )

    def _require_operation_lifecycle(self):
        runtime = self._operation_lifecycle
        if runtime is None:
            raise SshPilotError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "Long-running SFTP operations are not available",
            )
        return runtime

    def realpath(self, request: SftpPathRequest, *, client_id: ClientId) -> str:
        if type(request) is not SftpPathRequest:
            raise SshPilotError(ErrorCode.INVALID_REQUEST, "A SFTP path request is required")
        record = self._ready_record_for_read(request.service_id, client_id)
        path = _validate_path(request.path)
        try:
            return record.handle.client.realpath(path)
        except Exception as exc:
            raise self._map_error(exc, record) from exc

    def readlink(self, request: SftpPathRequest, *, client_id: ClientId) -> str:
        if type(request) is not SftpPathRequest:
            raise SshPilotError(ErrorCode.INVALID_REQUEST, "A SFTP path request is required")
        record = self._ready_record_for_read(request.service_id, client_id)
        path = _validate_path(request.path)
        try:
            return record.handle.client.readlink(path)
        except Exception as exc:
            raise self._map_error(exc, record) from exc

    def mkdir(self, request: SftpPathRequest, *, client_id: ClientId) -> None:
        record = self._ready_record_for_mutation(request.service_id, client_id)
        path = _validate_path(request.path)
        try:
            record.handle.client.mkdir(path)
        except Exception as exc:
            raise self._map_error(exc, record) from exc

    def rmdir(self, request: SftpPathRequest, *, client_id: ClientId) -> None:
        record = self._ready_record_for_mutation(request.service_id, client_id)
        path = _validate_path(request.path)
        try:
            record.handle.client.rmdir(path)
        except Exception as exc:
            raise self._map_error(exc, record) from exc

    def copy(
        self,
        request: SftpCopyRequest,
        *,
        client_id: ClientId,
        progress: Optional[Callable[[float], None]] = None,
        cancel: Optional[Callable[[], bool]] = None,
    ) -> None:
        if type(request) is not SftpCopyRequest:
            raise SshPilotError(ErrorCode.INVALID_REQUEST, "A SFTP copy request is required")
        record = self._ready_record_for_mutation(request.service_id, client_id)
        source = _validate_path(request.source_path)
        destination = _validate_path(request.destination_path)
        if request.recursive and _remote_path_is_descendant(source, destination):
            raise _SftpSummaryError(
                ErrorCode.VALIDATION_FAILED,
                "A directory cannot be copied into itself",
                SftpFailureCode.DIRECTORY_CANNOT_BE_COPIED_INTO_ITSELF,
                details={"service_id": record.service_id},
            )
        client = record.handle.client
        copied = 0
        pending_files = 0

        def _report_copy_progress() -> None:
            if progress is not None:
                progress(_coarse_progress(copied, pending_files))

        def _copy_file(source_path: str, destination_path: str) -> None:
            nonlocal copied, pending_files
            if cancel is not None and cancel():
                raise OperationCancelled()
            with client.open(source_path, "rb") as source_file, client.open(
                destination_path, "wb"
            ) as destination_file:
                while True:
                    chunk = source_file.read(32768)
                    if not chunk:
                        break
                    destination_file.write(chunk)
            copied += 1
            _report_copy_progress()

        def _copy_directory(source_path: str, destination_path: str) -> None:
            nonlocal pending_files
            if cancel is not None and cancel():
                raise OperationCancelled()
            client.mkdir(destination_path)
            entries = client.listdir_attr(source_path)
            pending_files += sum(
                1 for entry in entries if not (entry.is_dir() and not entry.is_symlink())
            )
            for entry in entries:
                child_source = posixpath.join(source_path, entry.filename)
                child_destination = posixpath.join(destination_path, entry.filename)
                if entry.is_dir() and not entry.is_symlink():
                    _copy_directory(child_source, child_destination)
                else:
                    _copy_file(child_source, child_destination)

        try:
            # lstat (never stat) the root: a symlink must never be classified
            # as a directory here, otherwise a directory-symlink source would
            # be walked as a real tree and (for a move) the cleanup pass would
            # delete through the link into its target, violating the no-follow
            # policy. A non-recursive copy of a symlink still falls through to
            # ``_copy_file`` below, matching prior single-entry link handling.
            source_attr = client.lstat(source)
            if source_attr.is_symlink() and request.recursive:
                raise _SftpSummaryError(
                    ErrorCode.VALIDATION_FAILED,
                    "Recursive copy of a symbolic link is not supported",
                    SftpFailureCode.RECURSIVE_COPY_SYMLINK_UNSUPPORTED,
                    details={"service_id": record.service_id},
                )
            try:
                client.stat(destination)
            except Exception as exc:
                if not (
                    isinstance(exc, sftp_proto.SFTPError)
                    and getattr(exc, "errno", None) == errno.ENOENT
                ):
                    raise
            else:
                raise _SftpSummaryError(
                    ErrorCode.REMOTE_PATH_EXISTS,
                    "The destination already exists",
                    SftpFailureCode.DESTINATION_ALREADY_EXISTS,
                    details={"service_id": record.service_id},
                )
            if source_attr.is_dir():
                if not request.recursive:
                    raise _SftpSummaryError(
                        ErrorCode.VALIDATION_FAILED,
                        "Recursive copy is required for directories",
                        SftpFailureCode.RECURSIVE_COPY_REQUIRED,
                        details={"service_id": record.service_id},
                    )
                _copy_directory(source, destination)
            else:
                if request.recursive:
                    raise _SftpSummaryError(
                        ErrorCode.VALIDATION_FAILED,
                        "Recursive copy requires a directory source",
                        SftpFailureCode.RECURSIVE_COPY_REQUIRES_DIRECTORY_SOURCE,
                        details={"service_id": record.service_id},
                    )
                _copy_file(source, destination)
            if progress is not None:
                progress(1.0)
            if request.move:
                if source_attr.is_dir():
                    # Same lstat-based, no-follow walker used by ``remove()``,
                    # so move cleanup can never descend through a symlink.
                    self._remove_recursive(client, source, cancel=cancel)
                else:
                    client.remove(source)
        except OperationCancelled:
            raise
        except SshPilotError:
            raise
        except Exception as exc:
            raise self._map_error(exc, record) from exc

    def start_copy(
        self,
        request: SftpCopyRequest,
        *,
        client_id: ClientId,
    ) -> OperationSummary:
        """Start a daemon operation that recursively copies (or moves) a tree.

        The recursive walk runs on the shared operation worker with progress
        reporting and cooperative cancellation instead of blocking the SFTP
        command stream.
        """
        if type(request) is not SftpCopyRequest:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST, "A SFTP copy request is required"
            )
        source = _validate_path(request.source_path)
        destination = _validate_path(request.destination_path)
        record = self._ready_record_for_mutation(request.service_id, client_id)
        if request.recursive and _remote_path_is_descendant(source, destination):
            raise _SftpSummaryError(
                ErrorCode.VALIDATION_FAILED,
                "A directory cannot be copied into itself",
                SftpFailureCode.DIRECTORY_CANNOT_BE_COPIED_INTO_ITSELF,
                details={"service_id": record.service_id},
            )
        runtime = self._require_operation_lifecycle()

        def _body(handle) -> str:
            verb = "Moving" if request.move else "Copying"
            handle.report(f"{verb}…", 0.0)

            def _should_cancel() -> bool:
                handle.raise_if_cancelled()
                return False

            with self._serialized_target((request.service_id, "sftp-operation")):
                self._ready_record_for_mutation(request.service_id, client_id)
                handle.raise_if_cancelled()
                self.copy(
                    request,
                    client_id=client_id,
                    progress=lambda fraction: handle.report(f"{verb}…", fraction),
                    cancel=_should_cancel,
                )
            return f"{verb} complete"

        return runtime.start_operation(
            OperationKind.SFTP_COPY_TREE,
            _body,
            connection_id=record.connection_id,
            owner_client_id=client_id,
            message=f"{'Moving' if request.move else 'Copying'} {source}",
            failure_mapper=_operation_failure,
        )

    def remove(
        self,
        request: SftpPathRequest,
        *,
        client_id: ClientId,
        progress: Optional[Callable[[float], None]] = None,
        cancel: Optional[Callable[[], bool]] = None,
    ) -> None:
        record = self._ready_record_for_mutation(request.service_id, client_id)
        path = _validate_path(request.path)
        client = record.handle.client
        try:
            if request.recursive:
                self._remove_recursive(client, path, progress=progress, cancel=cancel)
            else:
                client.remove(path)
        except OperationCancelled:
            raise
        except SshPilotError:
            raise
        except Exception as exc:
            raise self._map_error(exc, record) from exc

    def start_remove(
        self,
        request: SftpPathRequest,
        *,
        client_id: ClientId,
    ) -> OperationSummary:
        """Start a daemon operation that recursively deletes a remote tree.

        The walk runs on the shared operation worker with progress reporting
        and cooperative cancellation instead of blocking the SFTP command
        stream.
        """
        if type(request) is not SftpPathRequest:
            raise SshPilotError(ErrorCode.INVALID_REQUEST, "A SFTP path request is required")
        path = _validate_path(request.path)
        record = self._ready_record_for_mutation(request.service_id, client_id)
        runtime = self._require_operation_lifecycle()

        def _body(handle) -> str:
            handle.report("Deleting…", 0.0)

            def _should_cancel() -> bool:
                handle.raise_if_cancelled()
                return False

            with self._serialized_target((request.service_id, "sftp-operation")):
                self._ready_record_for_mutation(request.service_id, client_id)
                handle.raise_if_cancelled()
                self.remove(
                    request,
                    client_id=client_id,
                    progress=lambda fraction: handle.report("Deleting…", fraction),
                    cancel=_should_cancel,
                )
            return "Deleted"

        return runtime.start_operation(
            OperationKind.SFTP_REMOVE_TREE,
            _body,
            connection_id=record.connection_id,
            owner_client_id=client_id,
            message=f"Deleting {path}",
            failure_mapper=_operation_failure,
        )

    def _remove_recursive(
        self,
        client,
        path: str,
        *,
        progress: Optional[Callable[[float], None]] = None,
        cancel: Optional[Callable[[], bool]] = None,
    ) -> None:
        """Delete a remote tree with lstat so symlinks are never followed.

        A symlink is removed as a link (like ``rm -r``), never recursed into,
        which keeps cycles and escapes out of the tree impossible.
        """
        try:
            attr = client.lstat(path)
        except (FileNotFoundError, sftp_proto.SFTPError) as exc:
            if not isinstance(exc, sftp_proto.SFTPError) or exc.code == sftp_proto.FX_NO_SUCH_FILE:
                return
            raise
        if not attr.is_dir() or attr.is_symlink():
            client.remove(path)
            return
        entries = client.listdir_attr(path)
        processed = 0
        for entry in entries:
            if cancel is not None and cancel():
                raise OperationCancelled()
            child = path.rstrip("/") + "/" + entry.filename
            if entry.is_dir() and not entry.is_symlink():
                self._remove_recursive(client, child, progress=progress, cancel=cancel)
            else:
                try:
                    client.remove(child)
                except (FileNotFoundError, sftp_proto.SFTPError) as exc:
                    if not isinstance(exc, sftp_proto.SFTPError) or exc.code != sftp_proto.FX_NO_SUCH_FILE:
                        raise
            processed += 1
            if progress is not None:
                progress(_coarse_progress(processed, len(entries)))
        client.rmdir(path)

    def rename(self, request: SftpRenameRequest, *, client_id: ClientId) -> None:
        if type(request) is not SftpRenameRequest:
            raise SshPilotError(ErrorCode.INVALID_REQUEST, "A SFTP rename request is required")
        record = self._ready_record_for_mutation(request.service_id, client_id)
        source = _validate_path(request.source_path)
        destination = _validate_path(request.destination_path)
        client = record.handle.client
        try:
            if request.overwrite:
                client.posix_rename(source, destination)
            else:
                client.rename(source, destination)
        except Exception as exc:
            raise self._map_error(exc, record) from exc

    def chmod(self, request: SftpChmodRequest, *, client_id: ClientId) -> None:
        if type(request) is not SftpChmodRequest:
            raise SshPilotError(ErrorCode.INVALID_REQUEST, "A SFTP chmod request is required")
        record = self._ready_record_for_mutation(request.service_id, client_id)
        path = _validate_path(request.path)
        try:
            record.handle.client.chmod(path, request.mode)
        except Exception as exc:
            raise self._map_error(exc, record) from exc

    def symlink(self, request: SftpSymlinkRequest, *, client_id: ClientId) -> None:
        if type(request) is not SftpSymlinkRequest:
            raise SshPilotError(ErrorCode.INVALID_REQUEST, "A SFTP symlink request is required")
        record = self._ready_record_for_mutation(request.service_id, client_id)
        target = _validate_path(request.target_path)
        link = _validate_path(request.link_path)
        try:
            record.handle.client.symlink(target, link)
        except Exception as exc:
            raise self._map_error(exc, record) from exc

    # -- helpers ------------------------------------------------------
    @contextmanager
    def _serialized_target(self, key: Tuple[Any, ...]):
        """Serialize the compare-and-replace sequence for one file target.

        The per-target mutex is tracked under the runtime state lock but is
        acquired *outside* it, so blocking on or waiting inside the critical
        section never holds the global state lock across filesystem or
        network I/O. The reference count includes waiters, so an entry is
        only removed once no thread can still be holding or waiting on it.
        """

        with self._lock:
            entry = self._target_locks.get(key)
            if entry is None:
                entry = _TargetLockEntry()
                self._target_locks[key] = entry
            entry.users += 1
        try:
            entry.lock.acquire()
        except BaseException:
            with self._lock:
                entry.users -= 1
                if entry.users <= 0:
                    self._target_locks.pop(key, None)
            raise
        try:
            yield
        finally:
            entry.lock.release()
            with self._lock:
                entry.users -= 1
                if entry.users <= 0:
                    self._target_locks.pop(key, None)

    def _require_accepting_commands(self) -> None:
        """Re-validate runtime state after a mutation acquired its target lock."""
        with self._lock:
            self._require_accepting_commands_locked()

    def _entry_from_attr(self, path: str, attr: sftp_proto.SFTPAttributes) -> RemoteFileEntry:
        return RemoteFileEntry(
            name=remote_path_basename(path),
            path=path,
            file_type=_file_type(attr.st_mode or 0),
            size=(int(attr.st_size) if attr.st_size is not None else None),
            mode=((attr.st_mode & 0o7777) if attr.st_mode else None),
            uid=attr.st_uid,
            gid=attr.st_gid,
            modified_at=(
                datetime.fromtimestamp(attr.st_mtime, tz=timezone.utc)
                if attr.st_mtime
                else None
            ),
        )

    def _map_error(self, exc: Exception, record: _SftpRecord) -> SshPilotError:
        details = {"service_id": record.service_id}
        if isinstance(exc, sftp_proto.SFTPError):
            # Keep the server's status code and message in details: the wire
            # message is often the generic FX name, and without these every
            # SFTP failure is indistinguishable from the client side.
            details["sftp_status"] = int(exc.code)
            server_message = str(exc)
            if server_message:
                details["server_message"] = server_message
            code = _SFTP_STATUS_TO_ERROR_CODE.get(exc.code)
            if code is None:
                code = _ERRNO_TO_ERROR_CODE.get(
                    getattr(exc, "errno", None), ErrorCode.SFTP_COMMAND_FAILED
                )
                if (
                    server_message
                    and server_message not in _GENERIC_SFTP_STATUS_TEXT
                ):
                    details["server_message_is_specific"] = True
            error = SshPilotError(
                code,
                code.value,
                details=details,
                connection_id=record.connection_id,
            )
        elif isinstance(exc, (EOFError, OSError)):
            error = SshPilotError(
                ErrorCode.SFTP_PROTOCOL_LOST,
                ErrorCode.SFTP_PROTOCOL_LOST.value,
                details=details,
                connection_id=record.connection_id,
            )
        else:
            error = SshPilotError(
                ErrorCode.SFTP_PROTOCOL_ERROR,
                ErrorCode.SFTP_PROTOCOL_ERROR.value,
                details=details,
                connection_id=record.connection_id,
            )
        if error.code is ErrorCode.SFTP_PROTOCOL_LOST:
            # A live operation just proved the underlying process/connection
            # is dead. The process reaper usually gets there first; this
            # covers protocol EOF while the ssh child is still listed as
            # alive. Flip to FAILED so the file-manager Retry UI appears
            # without waiting for another click.
            # Direct ErrorData keeps the stable code placeholder introduced by
            # the direct-error migration. The lifecycle event below uses the
            # dedicated structured SFTP failure contract.
            self._fail_dead_connection(
                record,
                SshPilotError(
                    error.code,
                    "The SFTP connection was lost",
                    details=error.details,
                    connection_id=record.connection_id,
                ),
            )
        return error

    def _fail_dead_connection(self, record: _SftpRecord, error: SshPilotError) -> None:
        event = None
        with self._lock:
            if record.state is SftpServiceState.READY:
                record.failure = SftpFailure(
                    code=SftpFailureCode.CONNECTION_LOST,
                    error_code=error.code,
                    diagnostic=_specific_server_diagnostic(error),
                )
                event = self._transition_locked(record, SftpServiceState.FAILED)
        if event is not None:
            self._publish((event,))

    def _ready_record_for_read(
        self,
        service_id: SftpServiceId,
        client_id: ClientId,
    ) -> _SftpRecord:
        with self._lock:
            record = self._record_locked(service_id)
            if client_id != record.owner_client_id and client_id not in record.attached_clients:
                raise SshPilotError(
                    ErrorCode.SFTP_SERVICE_NOT_FOUND,
                    "The SFTP service was not found",
                    connection_id=record.connection_id,
                )
            if record.state is not SftpServiceState.READY or record.handle is None:
                raise SshPilotError(
                    ErrorCode.SFTP_SERVICE_NOT_READY,
                    "The SFTP service is not ready",
                    connection_id=record.connection_id,
                )
            return record

    def _ready_record_for_mutation(
        self,
        service_id: SftpServiceId,
        client_id: ClientId,
    ) -> _SftpRecord:
        record = self._ready_record_for_read(service_id, client_id)
        self._require_owner(record, client_id)
        return record

    @staticmethod
    def _require_owner(record: _SftpRecord, client_id: ClientId) -> None:
        if record.owner_client_id != client_id:
            raise SshPilotError(
                ErrorCode.SERVICE_OWNER_REQUIRED,
                "Only the originating client may mutate this SFTP service",
                connection_id=record.connection_id,
                details={"service_id": record.service_id},
            )

    def _transition_locked(
        self,
        record: _SftpRecord,
        new_state: SftpServiceState,
    ) -> CoreEvent:
        if not is_valid_sftp_transition(record.state, new_state):
            raise RuntimeError(
                f"invalid SFTP transition {record.state.value}->{new_state.value}"
            )
        previous_state = record.state
        now = self._clock()
        record.state = new_state
        if new_state is SftpServiceState.CLOSED:
            record.closed_at = now
            record.attached_clients.clear()
            self._evict_closed_locked()
        with log_context(
            sftp_service=record.service_id,
            client=record.owner_client_id,
            connection=record.connection_id,
        ):
            logger.info(
                "sftp service state changed from=%s to=%s",
                previous_state.value,
                new_state.value,
            )
        event_type = EventType.SFTP_STATE_CHANGED
        if new_state is SftpServiceState.CLOSED:
            event_type = EventType.SFTP_CLOSED
        elif new_state is SftpServiceState.FAILED:
            event_type = EventType.SFTP_FAILED
        return self._event_locked(record, event_type)

    def _event_locked(self, record: _SftpRecord, event_type: EventType) -> CoreEvent:
        return CoreEvent(
            type=event_type,
            payload=self._summary_locked(record),
            sequence=0,
            connection_id=record.connection_id,
            # Reuses CoreEvent.session_id as the generic interaction/eligibility
            # correlation key (see module docstring) — not a terminal session.
            session_id=SessionId(str(record.service_id)),
        )

    def _publish(self, events) -> None:
        for event in events:
            try:
                self._publisher.publish(
                    event.type,
                    event.payload,
                    connection_id=event.connection_id,
                    session_id=event.session_id,
                )
            except RuntimeError:
                return

    def _record_locked(self, service_id: SftpServiceId) -> _SftpRecord:
        record = self._records.get(service_id)
        if record is None:
            raise SshPilotError(
                ErrorCode.SFTP_SERVICE_NOT_FOUND,
                "The requested SFTP service does not exist",
                details={"service_id": service_id},
            )
        return record

    @staticmethod
    def _summary_locked(record: _SftpRecord) -> SftpServiceSummary:
        return SftpServiceSummary(
            id=record.service_id,
            connection_id=record.connection_id,
            state=record.state,
            created_at=record.created_at,
            started_at=record.started_at,
            closed_at=record.closed_at,
            attachment_count=len(record.attached_clients),
            owner_client_id=record.owner_client_id,
            failure=record.failure,
        )

    def _evict_closed_locked(self) -> None:
        closed = [
            service_id
            for service_id in self._creation_order
            if (
                service_id in self._records
                and self._records[service_id].state is SftpServiceState.CLOSED
            )
        ]
        excess = len(closed) - self._max_retained_closed_services
        for service_id in closed[: max(0, excess)]:
            self._records.pop(service_id, None)
            try:
                self._creation_order.remove(service_id)
            except ValueError:
                pass

    def _require_accepting_commands_locked(self) -> None:
        if not self._accepting_commands or self._closed:
            raise SshPilotError(
                ErrorCode.DAEMON_SHUTTING_DOWN,
                "The SFTP runtime is shutting down",
                retryable=True,
            )

    def _require_accepting_reads_locked(self) -> None:
        if self._closed:
            raise SshPilotError(
                ErrorCode.DAEMON_SHUTTING_DOWN,
                "The SFTP runtime is shut down",
                retryable=True,
            )

"""Daemon-owned SFTP service lifecycle (``ssh … -s sftp``) and remote FS ops.

Mirrors the shape of :mod:`sshpilot.daemon.session_runtime`: a
``prepare_open``/``start`` pair for non-blocking startup, an explicit state
machine with public events, and a pluggable process runner so this module can
be unit-tested without spawning real ``ssh`` subprocesses. All remote
filesystem operations are blocking network calls and must be invoked from a
daemon command worker (see ``dispatch.py``); they are serialized per service
by :class:`~sshpilot.daemon.command_executor.BoundedCommandExecutor` using the
service id as the command key.

Interaction/host-key trust reuses :class:`~sshpilot.daemon.session_runtime.SessionLaunchSpec`
and the existing :class:`~sshpilot.daemon.interaction_broker.InteractionBroker`
unchanged — an SFTP service's public id is passed as the spec's
``session_id`` so the broker (and this module's own ``CoreEvent``s) can key
interaction eligibility exactly like a terminal session. No new askpass or
trust path is introduced.
"""

from __future__ import annotations

import errno
import logging
import stat as stat_module
import subprocess
import threading
import time
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
    RemoteFileEntry,
    RemoteFileType,
    ServiceFailure,
    SftpChmodRequest,
    SftpPathRequest,
    SftpRenameRequest,
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

from .session_runtime import SessionLaunchSpec

logger = logging.getLogger(__name__)

DEFAULT_MAX_RETAINED_CLOSED_SERVICES = 50
DEFAULT_LIST_LIMIT = 2000
DEFAULT_CONNECT_TIMEOUT_SECONDS = 30.0
DEFAULT_TERMINATE_GRACE_SECONDS = 2.0


class SftpProcessHandle(Protocol):
    """Owned SFTP transport: a live client plus its underlying process."""

    client: OpenSSHSFTPClient

    def terminate(self) -> None: ...

    def wait(self, timeout: float) -> bool:
        """Return True once the underlying process has exited."""
        ...


class SftpProcessRunner(Protocol):
    """Narrow launch boundary, mirroring ``SessionProcessRunner``."""

    def start(self, spec: SessionLaunchSpec) -> SftpProcessHandle: ...

    def close(self) -> None: ...


class UnsupportedSftpProcessRunner:
    """Default runner until a daemon launch builder is wired in."""

    def start(self, spec: SessionLaunchSpec) -> SftpProcessHandle:
        raise SshPilotError(
            ErrorCode.SFTP_SERVICE_NOT_READY,
            "SFTP service startup requires daemon runtime support",
            connection_id=spec.connection_id,
        )

    def close(self) -> None:
        return


class _SubprocessSftpHandle:
    def __init__(self, process: "subprocess.Popen", client: OpenSSHSFTPClient) -> None:
        self._process = process
        self.client = client
        self._lock = threading.Lock()
        self._terminated = False

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
            self._process.wait(timeout=max(0.0, timeout))
            return True
        except subprocess.TimeoutExpired:
            return False


class SubprocessSftpProcessRunner:
    """Spawn ``ssh … -s sftp`` and perform the blocking SFTP handshake.

    ``command_builder`` is the daemon-internal injection point (never reached
    through the wire protocol) that returns the canonical argv/env for one
    SFTP launch — production wiring goes through the same
    ``InteractionBroker``/askpass path as terminal sessions (see
    ``DaemonServer._prepare_sftp_launch``).
    """

    def __init__(
        self,
        command_builder: Callable[[SessionLaunchSpec], Tuple[Sequence[str], Dict[str, str]]],
        *,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    ) -> None:
        if not callable(command_builder):
            raise TypeError("SFTP command builder must be callable")
        self._command_builder = command_builder
        self._connect_timeout = float(connect_timeout)
        self._lock = threading.Lock()
        self._handles: Set[_SubprocessSftpHandle] = set()
        self._closed = False

    def start(self, spec: SessionLaunchSpec) -> SftpProcessHandle:
        argv, environment = self._command_builder(spec)
        argv = tuple(argv)
        if not argv or any(type(item) is not str or not item for item in argv):
            raise SshPilotError(
                ErrorCode.SFTP_SERVICE_NOT_READY,
                "The SFTP launch command is invalid",
                connection_id=spec.connection_id,
            )
        with self._lock:
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
        handle = _SubprocessSftpHandle(process, client)
        with self._lock:
            if self._closed:
                handle.terminate()
                raise RuntimeError("SFTP process runner is closed")
            self._handles.add(handle)
        return handle

    @staticmethod
    def _terminate_process(process: "subprocess.Popen") -> None:
        if process.poll() is None:
            try:
                process.terminate()
            except Exception:  # pragma: no cover - best effort
                pass

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            handles = tuple(self._handles)
            self._handles.clear()
        for handle in handles:
            try:
                handle.terminate()
                handle.wait(DEFAULT_TERMINATE_GRACE_SECONDS)
            except Exception:  # pragma: no cover - best effort
                continue


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
    failure: Optional[ServiceFailure] = None
    handle: Optional[SftpProcessHandle] = None
    launch_spec: Optional[SessionLaunchSpec] = None
    close_scheduled: bool = False


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
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        id_factory: Callable[[], SftpServiceId] = new_sftp_id,
        shutdown_timeout_seconds: float = 3.0,
        max_retained_closed_services: int = DEFAULT_MAX_RETAINED_CLOSED_SERVICES,
        list_limit: int = DEFAULT_LIST_LIMIT,
    ) -> None:
        if shutdown_timeout_seconds < 0:
            raise ValueError("SFTP shutdown timeout must not be negative")
        if type(max_retained_closed_services) is not int or max_retained_closed_services < 0:
            raise ValueError("closed-service retention limit must not be negative")
        if type(list_limit) is not int or list_limit < 1:
            raise ValueError("SFTP list limit must be positive")
        self._core_client = core_client
        self._runner: Any = runner or UnsupportedSftpProcessRunner()
        self._clock = clock
        self._monotonic = monotonic
        self._id_factory = id_factory
        self._shutdown_timeout_seconds = float(shutdown_timeout_seconds)
        self._max_retained_closed_services = max_retained_closed_services
        self._list_limit = list_limit
        self._lock = threading.RLock()
        self._publisher = EventPublisher()
        self._records: Dict[SftpServiceId, _SftpRecord] = {}
        self._creation_order: List[SftpServiceId] = []
        self._accepting_commands = True
        self._closed = False

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
            handle = self._runner.start(spec)
            if handle is None:
                raise TypeError("SFTP runner returned no process handle")
        except SshPilotError as error:
            self._startup_failed(record, error.code, error.message)
            return
        except Exception:
            self._startup_failed(
                record,
                ErrorCode.SFTP_SERVICE_NOT_READY,
                "The SFTP session could not be established",
            )
            return
        terminate_after_start = True
        events: List[CoreEvent] = []
        with self._lock:
            if record.state is SftpServiceState.STARTING:
                record.handle = handle
                record.started_at = self._clock()
                events.append(self._transition_locked(record, SftpServiceState.READY))
                terminate_after_start = False
            elif record.state is SftpServiceState.CLOSING:
                record.handle = handle
                terminate_after_start = False
        self._publish(events)
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
            code = error.code
            message = error.message
        else:
            code = ErrorCode.SFTP_SERVICE_NOT_READY
            message = "The SFTP session could not be established"
        self._startup_failed(self._records.get(service_id), code, message, guarded=True)

    def _startup_failed(
        self,
        record: Optional[_SftpRecord],
        code: ErrorCode,
        message: str,
        *,
        guarded: bool = False,
    ) -> None:
        with self._lock:
            if record is None or record.state is not SftpServiceState.STARTING:
                return
            record.failure = ServiceFailure(code=code.value, message=message)
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
                record.failure = ServiceFailure(
                    code=ErrorCode.SERVER_BUSY.value,
                    message="The daemon SFTP command queue is full",
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
                    record.failure = ServiceFailure(
                        code=ErrorCode.SFTP_SERVICE_NOT_READY.value,
                        message="The SFTP process could not be terminated",
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

    # -- remote filesystem operations -------------------------------------
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
        path = _validate_path(request.path)
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

    def remove(self, request: SftpPathRequest, *, client_id: ClientId) -> None:
        record = self._ready_record_for_mutation(request.service_id, client_id)
        path = _validate_path(request.path)
        try:
            record.handle.client.remove(path)
        except Exception as exc:
            raise self._map_error(exc, record) from exc

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
            code = _ERRNO_TO_ERROR_CODE.get(
                getattr(exc, "errno", None), ErrorCode.SFTP_COMMAND_FAILED
            )
            return SshPilotError(
                code,
                "The SFTP command failed",
                details=details,
                connection_id=record.connection_id,
            )
        if isinstance(exc, (EOFError, OSError)):
            return SshPilotError(
                ErrorCode.SFTP_PROTOCOL_LOST,
                "The SFTP connection was lost",
                details=details,
                connection_id=record.connection_id,
            )
        return SshPilotError(
            ErrorCode.SFTP_PROTOCOL_ERROR,
            "The SFTP command failed",
            details=details,
            connection_id=record.connection_id,
        )

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
        now = self._clock()
        record.state = new_state
        if new_state is SftpServiceState.CLOSED:
            record.closed_at = now
            record.attached_clients.clear()
            self._evict_closed_locked()
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

"""Daemon-owned file-transfer lifecycle over an existing READY SFTP service.

Transfers stream bytes through the same :class:`~sshpilot.sftp.client.OpenSSHSFTPClient`
an :class:`~sshpilot.daemon.sftp_runtime.SftpServiceRuntime` service already
owns (the client pipelines requests, so a transfer's reads/writes never block
that service's other operations). Unlike sessions/SFTP/forwards, a transfer's
actual byte-copy loop runs on a dedicated per-transfer thread rather than a
``BoundedCommandExecutor`` worker, since a large transfer can run for minutes
and must not starve the shared command queue; the executor operation merely
hands work to the bounded transfer worker pool and returns immediately (see
``run_transfer``). At most ``max_concurrent_transfers`` copy threads run at
once globally; each SFTP service also has its own cap (Dropbear SSH channels
serialize to one transfer — OpenWrt often pairs Dropbear with an OpenSSH
``sftp-server``, so SFTP extensions alone cannot decide). Additional accepted
transfers wait in ``_pending_run`` up to the combined in-flight capacity.
"""

from __future__ import annotations

import logging
import os
import stat
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple, Union

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.events import (
    CoreEvent,
    CoreEventCallback,
    EventPublisher,
    EventType,
    Subscription,
)
from sshpilot.api.models.common import ClientId, ConnectionId, SftpServiceId, TransferId, utc_now
from sshpilot.api.models.operations import (
    ScpFailure,
    ScpFailureCode,
    SftpFailure,
    SftpFailureCode,
)
from sshpilot.api.models.transfers import (
    CancelTransferRequest,
    StartScpTransferRequest,
    StartTransferRequest,
    TransferBackend,
    TransferConflictPolicy,
    TransferDirection,
    TransferState,
    TransferSummary,
)
from sshpilot.logging_support import log_context
from sshpilot.api.remote_path import remote_path_dirname, remote_path_join
from sshpilot.api.transfer_identity import new_transfer_id
from sshpilot.sftp import protocol as sftp_proto
from sshpilot.sftp.client import AtomicUploadItem
from sshpilot.sftp.server_limits import transfer_concurrency_for_remote_software

from .sftp_runtime import SftpServiceRuntime

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENT_TRANSFERS = 4
DROPBEAR_MAX_CONCURRENT_TRANSFERS = 1
DEFAULT_MAX_QUEUED_TRANSFERS = 32
DEFAULT_MAX_RETAINED_COMPLETED_TRANSFERS = 200
DEFAULT_CHUNK_SIZE = 32768
DEFAULT_PROGRESS_MIN_INTERVAL_SECONDS = 0.2
DEFAULT_PROGRESS_MIN_BYTES = 1024 * 1024
_TEMP_PREFIX = ".sshpilot-tmp-"
_TERMINAL_STATES = frozenset(
    {
        TransferState.COMPLETED,
        TransferState.CANCELLED,
        TransferState.FAILED,
    }
)


class _TransferCancelled(Exception):
    """Raised inside a copy loop when cancellation is observed mid-transfer."""


def _current_umask() -> int:
    """The process umask, read without the racy set-and-restore of os.umask."""
    try:
        with open("/proc/self/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("Umask:"):
                    return int(line.split()[1], 8)
    except (OSError, ValueError):
        pass
    return 0o022


def _apply_downloaded_metadata(temp_path: str, local_dst: str, attr) -> None:
    """Give a finished download its permissions and times before it replaces
    *local_dst*.

    ``mkstemp`` creates the temp ``0600``. A replaced file keeps its own mode;
    a new one gets the remote mode narrowed by the umask, as OpenSSH's
    ``sftp get`` does. The remote mtime is kept.
    """
    try:
        mode = stat.S_IMODE(os.stat(local_dst).st_mode)
    except OSError:
        remote_mode = getattr(attr, "st_mode", None)
        base = stat.S_IMODE(remote_mode) & 0o777 if remote_mode is not None else 0o666
        mode = base & ~_current_umask()
    os.chmod(temp_path, mode)
    mtime = getattr(attr, "st_mtime", None)
    if mtime is not None:
        atime = getattr(attr, "st_atime", None)
        os.utime(temp_path, (atime if atime is not None else mtime, mtime))


class _TransferSkipped(Exception):
    """Raised when a conflict policy of SKIP means no bytes should be copied."""


class _SftpTransferError(Exception):
    """Carry a structured SFTP transfer failure to the lifecycle worker."""

    def __init__(self, failure: SftpFailure) -> None:
        super().__init__(failure.code.value)
        self.failure = failure


class _ScpTransferError(Exception):
    """Carry a structured native SCP failure to the lifecycle worker."""

    def __init__(self, failure: ScpFailure) -> None:
        super().__init__(failure.code.value)
        self.failure = failure


def _sftp_transfer_error(
    code: SftpFailureCode,
    error_code: ErrorCode,
    *,
    parameters: Optional[Dict[str, str]] = None,
    diagnostic: str = "",
) -> _SftpTransferError:
    return _SftpTransferError(
        SftpFailure(
            code=code,
            error_code=error_code,
            parameters=parameters or {},
            diagnostic=diagnostic,
        )
    )


def _sftp_service_failure(error: SshPilotError) -> SftpFailure:
    code = {
        ErrorCode.SERVER_BUSY: SftpFailureCode.TRANSFER_QUEUE_FULL,
        ErrorCode.SFTP_PROTOCOL_LOST: SftpFailureCode.CONNECTION_LOST,
        ErrorCode.SFTP_SERVICE_NOT_FOUND: SftpFailureCode.SERVICE_NOT_FOUND,
        ErrorCode.SFTP_SERVICE_NOT_READY: SftpFailureCode.SERVICE_NOT_READY,
        ErrorCode.SERVICE_OWNER_REQUIRED: SftpFailureCode.SERVICE_OWNER_REQUIRED,
    }.get(error.code, SftpFailureCode.TRANSFER_START_FAILED)
    return SftpFailure(code=code, error_code=error.code)


def _scp_start_failure(error: BaseException) -> ScpFailure:
    if isinstance(error, SshPilotError):
        code = {
            ErrorCode.SERVER_BUSY: ScpFailureCode.COMMAND_QUEUE_FULL,
            ErrorCode.UNSUPPORTED_CAPABILITY: ScpFailureCode.UNAVAILABLE,
            ErrorCode.DAEMON_SHUTTING_DOWN: ScpFailureCode.DAEMON_SHUTTING_DOWN,
        }.get(error.code, ScpFailureCode.TRANSFER_START_FAILED)
        error_code = error.code
    else:
        code = ScpFailureCode.TRANSFER_START_FAILED
        error_code = ErrorCode.TRANSFER_IO_FAILED
    return ScpFailure(code=code, error_code=error_code)


def _scp_runtime_failure(error: SshPilotError, *, phase: str) -> ScpFailure:
    details = error.details
    detail_code = details.get("scp_failure_code")
    try:
        code = ScpFailureCode(detail_code)
    except (TypeError, ValueError):
        if phase == "target":
            code = {
                ErrorCode.CONNECTION_NOT_FOUND: ScpFailureCode.CONNECTION_NOT_FOUND,
                ErrorCode.VALIDATION_FAILED: ScpFailureCode.HOST_IDENTIFIER_MISSING,
            }.get(error.code, ScpFailureCode.TARGET_PREPARATION_FAILED)
        else:
            code = {
                ErrorCode.CONNECTION_NOT_FOUND: ScpFailureCode.CONNECTION_NOT_FOUND,
                ErrorCode.UNSUPPORTED_SESSION_PROTOCOL: (
                    ScpFailureCode.SSH_CONNECTION_REQUIRED
                ),
                ErrorCode.VALIDATION_FAILED: ScpFailureCode.HOST_IDENTIFIER_MISSING,
                ErrorCode.UNSUPPORTED_CAPABILITY: ScpFailureCode.UNAVAILABLE,
                ErrorCode.SESSION_STARTUP_FAILED: (
                    ScpFailureCode.TRANSFER_PREPARATION_FAILED
                ),
                ErrorCode.INVALID_REQUEST: (
                    ScpFailureCode.TRANSFER_PREPARATION_FAILED
                ),
            }.get(error.code, ScpFailureCode.TRANSFER_FAILED)
    diagnostic = details.get("diagnostic", "")
    if type(diagnostic) is not str:
        diagnostic = ""
    return ScpFailure(
        code=code,
        error_code=error.code,
        diagnostic=diagnostic.replace("\x00", ""),
    )


_ALLOWED_TRANSITIONS = {
    TransferState.QUEUED: frozenset(
        {TransferState.STARTING, TransferState.CANCELLING, TransferState.FAILED}
    ),
    TransferState.STARTING: frozenset(
        {TransferState.RUNNING, TransferState.CANCELLING, TransferState.FAILED}
    ),
    TransferState.RUNNING: frozenset(
        {
            TransferState.CANCELLING,
            TransferState.COMPLETED,
            TransferState.FAILED,
        }
    ),
    TransferState.CANCELLING: frozenset(
        {TransferState.CANCELLED, TransferState.FAILED}
    ),
    TransferState.PAUSED: frozenset(),
    TransferState.CANCELLED: frozenset(),
    TransferState.COMPLETED: frozenset(),
    TransferState.FAILED: frozenset(),
}

_TRANSITION_EVENT_TYPES = {
    TransferState.STARTING: EventType.TRANSFER_STARTED,
    TransferState.COMPLETED: EventType.TRANSFER_COMPLETED,
    TransferState.CANCELLED: EventType.TRANSFER_CANCELLED,
    TransferState.FAILED: EventType.TRANSFER_FAILED,
    # CANCELLING has no dedicated public event type; applied silently.
    # RUNNING is entered after STARTING without a second TRANSFER_STARTED event.
}


def is_valid_transfer_transition(current: TransferState, target: TransferState) -> bool:
    if not isinstance(current, TransferState) or not isinstance(target, TransferState):
        raise TypeError("transfer transitions require TransferState values")
    return target in _ALLOWED_TRANSITIONS[current]


@dataclass
class _TransferRecord:
    transfer_id: TransferId
    connection_id: ConnectionId
    sftp_service_id: SftpServiceId
    direction: TransferDirection
    remote_path: str
    local_path: str
    conflict_policy: TransferConflictPolicy
    source_display: str
    destination_display: str
    state: TransferState
    created_at: datetime
    owner_client_id: Optional[ClientId] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    bytes_total: Optional[int] = None
    bytes_completed: int = 0
    failure: Optional[Union[ScpFailure, SftpFailure]] = None
    cancel_requested: bool = False
    local_temp_path: Optional[str] = None
    remote_temp_path: Optional[str] = None
    last_progress_monotonic: float = 0.0
    last_progress_bytes: int = 0
    recursive: bool = False
    backend: TransferBackend = TransferBackend.SFTP
    scp_request: Optional[StartScpTransferRequest] = None
    scp_cancel_event: Optional[threading.Event] = None
    # Per-SFTP-service concurrent-transfer cap (Dropbear → 1). SCP uses the
    # global pool only and leaves this at the runtime default.
    service_concurrency_limit: int = DEFAULT_MAX_CONCURRENT_TRANSFERS


class TransferRuntime:
    """Serialize daemon-lifetime transfer state and drive the byte-copy loop."""

    def __init__(
        self,
        sftp_runtime: SftpServiceRuntime,
        *,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        id_factory: Callable[[], TransferId] = new_transfer_id,
        shutdown_timeout_seconds: float = 5.0,
        max_concurrent_transfers: int = DEFAULT_MAX_CONCURRENT_TRANSFERS,
        max_concurrent_transfers_provider: Optional[Callable[[], int]] = None,
        max_queued_transfers: int = DEFAULT_MAX_QUEUED_TRANSFERS,
        max_retained_completed_transfers: int = DEFAULT_MAX_RETAINED_COMPLETED_TRANSFERS,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        progress_min_interval_seconds: float = DEFAULT_PROGRESS_MIN_INTERVAL_SECONDS,
        progress_min_bytes: int = DEFAULT_PROGRESS_MIN_BYTES,
        scp_backend=None,
    ) -> None:
        if shutdown_timeout_seconds < 0:
            raise ValueError("transfer shutdown timeout must not be negative")
        if type(max_concurrent_transfers) is not int or max_concurrent_transfers < 1:
            raise ValueError("max concurrent transfers must be a positive int")
        if type(max_queued_transfers) is not int or max_queued_transfers < 1:
            raise ValueError("max queued transfers must be a positive int")
        if (
            type(max_retained_completed_transfers) is not int
            or max_retained_completed_transfers < 0
        ):
            raise ValueError("completed-transfer retention limit must not be negative")
        if type(chunk_size) is not int or chunk_size < 1:
            raise ValueError("transfer chunk size must be positive")
        self._sftp_runtime = sftp_runtime
        self._clock = clock
        self._monotonic = monotonic
        self._id_factory = id_factory
        self._shutdown_timeout_seconds = float(shutdown_timeout_seconds)
        self._max_concurrent_transfers = max_concurrent_transfers
        self._max_concurrent_transfers_provider = max_concurrent_transfers_provider
        self._max_queued_transfers = max_queued_transfers
        self._max_retained_completed_transfers = max_retained_completed_transfers
        self._chunk_size = chunk_size
        self._progress_min_interval_seconds = float(progress_min_interval_seconds)
        self._progress_min_bytes = progress_min_bytes
        self._scp_backend = scp_backend
        self._lock = threading.RLock()
        self._publisher = EventPublisher()
        self._records: Dict[TransferId, _TransferRecord] = {}
        self._creation_order: List[TransferId] = []
        self._worker_threads: Dict[TransferId, threading.Thread] = {}
        self._pending_run: List[TransferId] = []
        self._accepting_commands = True
        self._closed = False

    def _effective_max_concurrent_transfers(self) -> int:
        """Return the live worker cap, preferring a settings provider when set."""
        provider = self._max_concurrent_transfers_provider
        if provider is not None:
            try:
                value = int(provider())
            except Exception:  # pragma: no cover - defensive fallback
                value = self._max_concurrent_transfers
            if value >= 1:
                return value
        return self._max_concurrent_transfers

    def subscribe_events(self, callback: CoreEventCallback) -> Subscription:
        with self._lock:
            if self._closed:
                raise SshPilotError(
                    ErrorCode.INVALID_REQUEST,
                    "The transfer runtime is closed",
                )
        return self._publisher.subscribe(callback)

    # -- reads --------------------------------------------------------
    def list_transfers(self) -> List[TransferSummary]:
        with self._lock:
            self._require_accepting_reads_locked()
            return [
                self._summary_locked(self._records[transfer_id])
                for transfer_id in self._creation_order
                if transfer_id in self._records
            ]

    def get_transfer(self, transfer_id: TransferId) -> TransferSummary:
        with self._lock:
            self._require_accepting_reads_locked()
            return self._summary_locked(self._record_locked(transfer_id))

    def client_can_interact(
        self,
        transfer_id: TransferId,
        client_id: ClientId,
    ) -> bool:
        """Whether *client_id* may claim interactions scoped to a transfer.

        Mirrors the session/SFTP/forward/operation runtimes: only the owning
        client of a native SCP transfer may see and answer the prompts its
        worker raises (password, passphrase, host-key, FIDO presence). Only
        native SCP transfers create a ``transfer-`` scoped askpass context;
        SFTP-backed transfers scope their interactions to the SFTP service id
        instead. A transfer recorded without an owner is not claimable by any
        client, and unknown transfer ids are always denied — a ``transfer-``
        prefixed scope that never belonged to a registered native SCP transfer
        stays invisible.
        """
        with self._lock:
            record = self._records.get(transfer_id)
            if record is None or record.owner_client_id is None:
                return False
            if record.scp_request is None:
                return False
            return record.owner_client_id == client_id

    # -- lifecycle ------------------------------------------------------
    def prepare_start_transfer(
        self,
        request: StartTransferRequest,
        *,
        client_id: ClientId,
    ) -> TransferSummary:
        if type(request) is not StartTransferRequest:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "A start transfer request is required",
            )
        # Shared core policy — recursive unsupported, paths validated, queue limits.
        try:
            from sshpilot.core.transfers import (
                OverwritePolicy,
                PathRef,
                TransferDirection as CoreDirection,
                TransferRequest,
            )

            if request.direction is TransferDirection.UPLOAD:
                core_req = TransferRequest(
                    direction=CoreDirection.UPLOAD,
                    source=PathRef(request.local_path, is_remote=False),
                    destination=PathRef(request.remote_path, is_remote=True),
                    overwrite={
                        TransferConflictPolicy.FAIL: OverwritePolicy.FAIL,
                        TransferConflictPolicy.OVERWRITE: OverwritePolicy.OVERWRITE,
                        TransferConflictPolicy.SKIP: OverwritePolicy.SKIP,
                        TransferConflictPolicy.RENAME: OverwritePolicy.RENAME,
                    }.get(request.conflict_policy, OverwritePolicy.FAIL),
                    recursive=bool(request.recursive),
                )
            else:
                core_req = TransferRequest(
                    direction=CoreDirection.DOWNLOAD,
                    source=PathRef(request.remote_path, is_remote=True),
                    destination=PathRef(request.local_path, is_remote=False),
                    overwrite={
                        TransferConflictPolicy.FAIL: OverwritePolicy.FAIL,
                        TransferConflictPolicy.OVERWRITE: OverwritePolicy.OVERWRITE,
                        TransferConflictPolicy.SKIP: OverwritePolicy.SKIP,
                        TransferConflictPolicy.RENAME: OverwritePolicy.RENAME,
                    }.get(request.conflict_policy, OverwritePolicy.FAIL),
                    recursive=bool(request.recursive),
                )
            core_req.validate(check_local_filesystem=False)
        except Exception as exc:
            from sshpilot.core.errors import CoreError

            if isinstance(exc, CoreError):
                raise SshPilotError(
                    ErrorCode.INVALID_REQUEST,
                    str(exc),
                ) from exc
            raise
        _client, connection_id = self._sftp_runtime.acquire_active_client(
            request.sftp_service_id, client_id
        )
        service_limit = transfer_concurrency_for_remote_software(
            self._sftp_runtime.remote_ssh_software(
                request.sftp_service_id, client_id
            ),
            default=self._effective_max_concurrent_transfers(),
            dropbear=min(
                DROPBEAR_MAX_CONCURRENT_TRANSFERS,
                self._effective_max_concurrent_transfers(),
            ),
        )
        transfer_id = self._id_factory()
        now = self._clock()
        local_display = os.path.basename(request.local_path.rstrip("/\\")) or "file"
        if request.direction is TransferDirection.UPLOAD:
            source_display, destination_display = local_display, request.remote_path
        else:
            source_display, destination_display = request.remote_path, local_display
        record = _TransferRecord(
            transfer_id=transfer_id,
            connection_id=connection_id,
            sftp_service_id=request.sftp_service_id,
            direction=request.direction,
            remote_path=request.remote_path,
            local_path=request.local_path,
            conflict_policy=request.conflict_policy,
            source_display=source_display,
            destination_display=destination_display,
            state=TransferState.QUEUED,
            created_at=now,
            owner_client_id=client_id,
            recursive=bool(request.recursive),
            service_concurrency_limit=service_limit,
        )
        with self._lock:
            self._require_accepting_commands_locked()
            self._admit_record_locked(record)
            self._records[transfer_id] = record
            self._creation_order.append(transfer_id)
            created_event = self._event_locked(record, EventType.TRANSFER_CREATED)
        self._publish((created_event,))
        with self._lock:
            return self._summary_locked(record)

    def prepare_start_scp_transfer(
        self,
        request: StartScpTransferRequest,
        *,
        client_id: ClientId,
    ) -> TransferSummary:
        if type(request) is not StartScpTransferRequest:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "An SCP transfer request is required",
            )
        if self._scp_backend is None:
            raise SshPilotError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "Native SCP transfers are unavailable",
            )
        transfer_id = self._id_factory()
        now = self._clock()
        source_display = ", ".join(request.sources)
        destination_display = request.destination
        record = _TransferRecord(
            transfer_id=transfer_id,
            connection_id=request.connection_id,
            sftp_service_id=None,
            direction=request.direction,
            remote_path=request.destination,
            local_path=request.destination,
            conflict_policy=request.conflict_policy,
            source_display=source_display,
            destination_display=destination_display,
            state=TransferState.QUEUED,
            created_at=now,
            owner_client_id=client_id,
            backend=TransferBackend.NATIVE_SCP,
            scp_request=request,
            scp_cancel_event=threading.Event(),
        )
        with self._lock:
            self._require_accepting_commands_locked()
            self._admit_record_locked(record)
            self._records[transfer_id] = record
            self._creation_order.append(transfer_id)
            created_event = self._event_locked(record, EventType.TRANSFER_CREATED)
        self._publish((created_event,))
        return self._summary_locked(record)

    def run_transfer(self, transfer_id: TransferId) -> None:
        """Fast executor operation: schedule the copy loop on the worker pool."""

        events: List[Optional[CoreEvent]] = []
        thread_to_start: Optional[threading.Thread] = None
        with self._lock:
            record = self._record_locked(transfer_id)
            if record.state is TransferState.CANCELLING:
                record.completed_at = self._clock()
                events.append(self._transition_locked(record, TransferState.CANCELLED))
            elif record.state is TransferState.QUEUED:
                # Stay QUEUED until a worker slot exists; STARTING only when assigned.
                thread_to_start = self._schedule_or_queue_locked(transfer_id)
                if thread_to_start is not None:
                    record.started_at = self._clock()
                    events.append(
                        self._transition_locked(record, TransferState.STARTING)
                    )
            elif record.state is TransferState.STARTING:
                # Already assigned; ensure a worker is registered.
                if transfer_id not in self._worker_threads:
                    thread_to_start = self._register_worker_thread_locked(transfer_id)
            else:
                return
        self._publish(events)
        if thread_to_start is not None:
            thread_to_start.start()

    def reject_pending_start(self, transfer_id: TransferId) -> None:
        self.fail_pending_start(
            transfer_id,
            SshPilotError(
                ErrorCode.SERVER_BUSY,
                "The daemon transfer command queue is full",
                retryable=True,
            ),
        )

    def fail_pending_start(self, transfer_id: TransferId, error: BaseException) -> None:
        record = self._records.get(transfer_id)
        if record is None:
            return
        if record.backend is TransferBackend.SFTP:
            failure = (
                _sftp_service_failure(error)
                if isinstance(error, SshPilotError)
                else SftpFailure(
                    code=SftpFailureCode.TRANSFER_START_FAILED,
                    error_code=ErrorCode.TRANSFER_IO_FAILED,
                )
            )
            self._fail_sftp(record, failure)
            return
        self._fail_scp(
            record,
            _scp_start_failure(error),
        )

    def _admit_record_locked(self, record: _TransferRecord) -> None:
        from sshpilot.core.transfers import TransferQueuePolicy

        max_concurrent = self._effective_max_concurrent_transfers()
        capacity = max_concurrent + self._max_queued_transfers
        inflight = self._count_inflight_locked()
        policy = TransferQueuePolicy(
            max_queued=capacity,
            max_concurrent=max_concurrent,
        )
        if policy.admit(inflight, 0) is not None:
            raise SshPilotError(
                ErrorCode.SERVER_BUSY,
                "The transfer queue is full; try again when an in-flight transfer finishes",
                retryable=True,
            )
        if record.transfer_id in self._records:
            raise RuntimeError("transfer id factory reused an active identifier")

    # -- copy loop (dedicated thread) ------------------------------------
    def _transfer_worker(self, transfer_id: TransferId) -> None:
        record = self._records.get(transfer_id)
        if record is None:
            return
        # Worker assigned and validating — enter STARTING if still queued, then RUNNING
        # once bytes may move.
        events: List[Optional[CoreEvent]] = []
        with self._lock:
            if record.state is TransferState.QUEUED:
                if record.started_at is None:
                    record.started_at = self._clock()
                events.append(self._transition_locked(record, TransferState.STARTING))
            if record.state is TransferState.STARTING:
                events.append(self._transition_locked(record, TransferState.RUNNING))
            elif record.state is not TransferState.RUNNING:
                return
        self._publish(events)
        client = None
        if record.backend is TransferBackend.SFTP:
            try:
                client, _ = self._sftp_runtime.acquire_active_client(
                    record.sftp_service_id, record.owner_client_id
                )
            except SshPilotError as error:
                self._fail_sftp(record, _sftp_service_failure(error))
                return
        try:
            if record.backend is TransferBackend.NATIVE_SCP:
                self._run_scp(record)
            elif record.recursive:
                if record.direction is TransferDirection.UPLOAD:
                    self._run_recursive_upload(record, client)
                else:
                    self._run_recursive_download(record, client)
            elif record.direction is TransferDirection.UPLOAD:
                self._run_upload(record, client)
            else:
                self._run_download(record, client)
        except _TransferSkipped:
            self._finish_completed(record)
        except _TransferCancelled:
            self._finish_cancelled(record)
        except _SftpTransferError as error:
            self._fail_sftp(record, error.failure)
        except _ScpTransferError as error:
            self._fail_scp(record, error.failure)
        except SshPilotError as error:
            if error.code is ErrorCode.OPERATION_CANCELLED:
                self._finish_cancelled(record)
            elif record.backend is TransferBackend.SFTP:
                self._fail_sftp(record, _sftp_service_failure(error))
            else:
                self._fail_scp(
                    record,
                    ScpFailure(
                        code=ScpFailureCode.TRANSFER_FAILED,
                        error_code=error.code,
                    ),
                )
        except Exception:
            with log_context(
                transfer=transfer_id,
                client=record.owner_client_id,
                sftp_service=record.sftp_service_id,
            ):
                logger.exception("transfer failed")
            if record.backend is TransferBackend.SFTP:
                self._fail_sftp(
                    record,
                    SftpFailure(
                        code=SftpFailureCode.TRANSFER_FAILED,
                        error_code=ErrorCode.TRANSFER_IO_FAILED,
                    ),
                )
            else:
                self._fail_scp(
                    record,
                    ScpFailure(
                        code=ScpFailureCode.UNEXPECTED_FAILURE,
                        error_code=ErrorCode.INTERNAL_ERROR,
                    ),
                )
        else:
            self._finish_completed(record)
        finally:
            thread_to_start: Optional[threading.Thread] = None
            promote_events: List[Optional[CoreEvent]] = []
            with self._lock:
                self._worker_threads.pop(transfer_id, None)
                next_id = self._take_next_runnable_locked()
                if next_id is not None:
                    next_record = self._records.get(next_id)
                    if next_record is not None and next_record.state is TransferState.QUEUED:
                        if next_record.started_at is None:
                            next_record.started_at = self._clock()
                        promote_events.append(
                            self._transition_locked(next_record, TransferState.STARTING)
                        )
                        thread_to_start = self._register_worker_thread_locked(next_id)
            self._publish(promote_events)
            if thread_to_start is not None:
                thread_to_start.start()

    def _run_scp(self, record: _TransferRecord) -> None:
        if self._scp_backend is None or record.scp_request is None:
            raise _ScpTransferError(
                ScpFailure(
                    code=ScpFailureCode.UNAVAILABLE,
                    error_code=ErrorCode.UNSUPPORTED_CAPABILITY,
                )
            )
        try:
            target = self._scp_backend.target_for_connection(record.connection_id)
        except SshPilotError as error:
            raise _ScpTransferError(
                _scp_runtime_failure(error, phase="target")
            ) from error
        try:
            self._scp_backend.run(
                record.scp_request,
                connection_target=target,
                connection_id=record.connection_id,
                transfer_id=record.transfer_id,
                cancel_event=record.scp_cancel_event,
            )
        except SshPilotError as error:
            if error.code is ErrorCode.OPERATION_CANCELLED:
                raise
            raise _ScpTransferError(
                _scp_runtime_failure(error, phase="transfer")
            ) from error
        with self._lock:
            record.bytes_completed = record.bytes_total or 0

    def _run_download(self, record: _TransferRecord, client) -> None:
        local_path = self._resolve_local_destination(record, record.local_path)
        copied = self._copy_remote_to_local(record, client, record.remote_path, local_path)
        with self._lock:
            record.bytes_completed = copied

    def _run_upload(self, record: _TransferRecord, client) -> None:
        local_path = record.local_path
        if not os.path.isfile(local_path):
            raise _sftp_transfer_error(
                SftpFailureCode.LOCAL_SOURCE_FILE_NOT_FOUND,
                ErrorCode.TRANSFER_IO_FAILED,
            )
        with self._lock:
            record.bytes_total = os.path.getsize(local_path)
        destination, existing_mode = self._resolve_remote_destination(
            record, client, record.remote_path
        )
        copied = self._copy_local_to_remote(
            record, client, local_path, destination, existing_mode=existing_mode
        )
        with self._lock:
            record.bytes_completed = copied

    # -- recursive transfers ------------------------------------------------

    def _run_recursive_upload(self, record: _TransferRecord, client) -> None:
        """Copy a local directory tree to a remote destination directory.

        The root is ``lstat``-checked so a symlink root is rejected instead of
        being walked as a real tree. Nested symlinked directories are not
        descended into (``os.walk`` default) and symlinked files are
        transferred by content, so a link can never pull an unrelated tree or a
        cycle into the upload.
        """
        local_root = record.local_path
        try:
            local_info = os.lstat(local_root)
        except OSError:
            raise _sftp_transfer_error(
                SftpFailureCode.LOCAL_SOURCE_DIRECTORY_NOT_FOUND,
                ErrorCode.TRANSFER_IO_FAILED,
            ) from None
        if stat.S_ISLNK(local_info.st_mode):
            raise _sftp_transfer_error(
                SftpFailureCode.RECURSIVE_UPLOAD_SYMLINK_UNSUPPORTED,
                ErrorCode.TRANSFER_IO_FAILED,
            )
        if not stat.S_ISDIR(local_info.st_mode):
            raise _sftp_transfer_error(
                SftpFailureCode.LOCAL_SOURCE_NOT_DIRECTORY,
                ErrorCode.TRANSFER_IO_FAILED,
            )
        remote_root = record.remote_path
        self._check_cancel(record)

        directories: List[tuple] = []
        files: List[tuple] = []
        total = 0
        for root, dirs, names in os.walk(local_root):
            rel_dir = os.path.relpath(root, local_root)
            remote_dir = remote_root if rel_dir == "." else self._join_remote(remote_root, rel_dir)
            depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
            directories.append((depth, remote_dir))
            for name in names:
                local_abs = os.path.join(root, name)
                remote_path = self._join_remote(remote_dir, name)
                size = 0
                try:
                    size = os.path.getsize(local_abs)
                except OSError:
                    pass
                total += size
                files.append((local_abs, remote_path, size))
        with self._lock:
            record.bytes_total = total

        self._ensure_remote_dirs(record, client, directories)

        completed = 0
        # Small files are RTT-bound when uploaded one-by-one (≈6 serial
        # round trips each). When the client can pipeline control-plane
        # requests, batch them so a window of files shares those RTTs.
        # Larger files keep the serial pipelined-WRITE path.
        pipeline_max = int(getattr(client, "max_write_length", 0) or DEFAULT_CHUNK_SIZE)
        can_pipeline = callable(getattr(client, "atomic_upload_many", None)) and callable(
            getattr(client, "stat_many", None)
        )
        small: List[tuple] = []
        large: List[tuple] = []
        if can_pipeline:
            for entry in files:
                if entry[2] <= pipeline_max:
                    small.append(entry)
                else:
                    large.append(entry)
        else:
            large = list(files)

        if small:
            completed = self._upload_small_files_pipelined(
                record, client, small, completed
            )

        for local_abs, remote_path, size in large:
            self._check_cancel(record)
            try:
                destination, existing_mode = self._resolve_remote_destination(
                    record, client, remote_path
                )
            except _TransferSkipped:
                completed += size
                continue
            copied = self._copy_local_to_remote(
                record,
                client,
                local_abs,
                destination,
                base=completed,
                existing_mode=existing_mode,
            )
            completed += copied
            self._report_progress(record, completed)
        with self._lock:
            record.bytes_completed = completed

    def _upload_small_files_pipelined(
        self,
        record: _TransferRecord,
        client,
        files: List[tuple],
        base: int,
    ) -> int:
        """Resolve conflicts then atomic-upload *files* with overlapped RTTs."""
        from sshpilot.core.transfers import ConflictDecision, OverwritePolicy, decide_conflict

        overwrite = {
            TransferConflictPolicy.FAIL: OverwritePolicy.FAIL,
            TransferConflictPolicy.OVERWRITE: OverwritePolicy.OVERWRITE,
            TransferConflictPolicy.SKIP: OverwritePolicy.SKIP,
            TransferConflictPolicy.RENAME: OverwritePolicy.RENAME,
        }.get(record.conflict_policy, OverwritePolicy.FAIL)

        completed = base
        # RENAME may need several STAT probes per file; keep that serial.
        if record.conflict_policy is TransferConflictPolicy.RENAME:
            for local_abs, remote_path, size in files:
                self._check_cancel(record)
                try:
                    destination, existing_mode = self._resolve_remote_destination(
                        record, client, remote_path
                    )
                except _TransferSkipped:
                    completed += size
                    continue
                item = self._atomic_upload_item(
                    local_abs, destination, existing_mode=existing_mode
                )
                file_bytes = 0

                def _on_one_file_bytes(_item: AtomicUploadItem, nbytes: int) -> None:
                    nonlocal file_bytes
                    file_bytes += nbytes
                    self._report_progress(record, completed + file_bytes)

                client.atomic_upload_many(
                    [item],
                    on_file_bytes=_on_one_file_bytes,
                    check_cancel=lambda: self._check_cancel(record),
                )
                completed += file_bytes
                self._report_progress(record, completed)
            return completed

        remote_paths = [remote for _local, remote, _size in files]
        self._check_cancel(record)
        # Like _resolve_remote_destination: any STAT error reads as absent.
        attrs_list = client.stat_many(remote_paths, missing_on_error=True)
        items: List[AtomicUploadItem] = []
        skipped_bytes = 0
        for (local_abs, remote_path, size), existing_attr in zip(files, attrs_list):
            exists = existing_attr is not None
            existing_mode = getattr(existing_attr, "st_mode", None) if exists else None
            if existing_mode is not None:
                existing_mode = stat.S_IMODE(existing_mode)
            decision = decide_conflict(exists, overwrite)
            if decision is ConflictDecision.SKIP:
                skipped_bytes += size
                continue
            if decision is ConflictDecision.FAIL:
                raise _sftp_transfer_error(
                    SftpFailureCode.REMOTE_DESTINATION_EXISTS,
                    ErrorCode.TRANSFER_CONFLICT,
                    parameters={"path": remote_path},
                )
            if decision is ConflictDecision.PROCEED:
                items.append(
                    self._atomic_upload_item(
                        local_abs, remote_path, existing_mode=existing_mode
                    )
                )
            else:
                raise AssertionError("unhandled transfer conflict policy")

        completed += skipped_bytes
        if skipped_bytes:
            self._report_progress(record, completed)
        if not items:
            return completed

        # atomic_upload_many removes its own temps on failure or cancel.
        bytes_in_batch = completed

        def _on_file_bytes(_item: AtomicUploadItem, nbytes: int) -> None:
            nonlocal bytes_in_batch
            bytes_in_batch += nbytes
            self._report_progress(record, bytes_in_batch)

        client.atomic_upload_many(
            items,
            on_file_bytes=_on_file_bytes,
            check_cancel=lambda: self._check_cancel(record),
        )
        completed = bytes_in_batch
        self._report_progress(record, completed)
        return completed

    @staticmethod
    def _atomic_upload_item(
        local_abs: str, remote_dst: str, *, existing_mode: Optional[int]
    ) -> AtomicUploadItem:
        local_info = os.stat(local_abs)
        create_mode = 0o600 if existing_mode is not None else stat.S_IMODE(local_info.st_mode)
        remote_dir = remote_path_dirname(remote_dst)
        temp_name = f"{_TEMP_PREFIX}{new_transfer_id()}"
        remote_temp = (
            temp_name if remote_dir in (".", "") else remote_path_join(remote_dir, temp_name)
        )
        return AtomicUploadItem(
            local_path=local_abs,
            remote_temp=remote_temp,
            remote_dst=remote_dst,
            create_mode=create_mode,
            existing_mode=existing_mode,
            atime=int(local_info.st_atime),
            mtime=int(local_info.st_mtime),
        )

    def _run_recursive_download(self, record: _TransferRecord, client) -> None:
        """Copy a remote directory tree to a local destination directory.

        The root is ``lstat``-checked so a symlink root is rejected instead of
        being classified as a directory and walked into its target. Nested
        symlinked entries are never descended into: a link to a directory is
        treated as a file (dereferenced by the read), so the walk cannot follow
        a cycle or escape the requested tree.
        """
        remote_root = record.remote_path
        local_root = record.local_path
        try:
            source_attr = client.lstat(remote_root)
        except Exception as exc:
            raise _sftp_transfer_error(
                SftpFailureCode.REMOTE_SOURCE_DIRECTORY_UNREADABLE,
                ErrorCode.TRANSFER_IO_FAILED,
            ) from exc
        if source_attr.is_symlink():
            raise _sftp_transfer_error(
                SftpFailureCode.RECURSIVE_DOWNLOAD_SYMLINK_UNSUPPORTED,
                ErrorCode.TRANSFER_IO_FAILED,
            )
        if not source_attr.is_dir():
            raise _sftp_transfer_error(
                SftpFailureCode.REMOTE_SOURCE_NOT_DIRECTORY,
                ErrorCode.TRANSFER_IO_FAILED,
            )
        self._check_cancel(record)
        try:
            os.makedirs(local_root, exist_ok=True)
        except OSError as exc:
            raise _sftp_transfer_error(
                SftpFailureCode.LOCAL_DESTINATION_DIRECTORY_CREATION_FAILED,
                ErrorCode.TRANSFER_CONFLICT,
            ) from exc

        files: List[tuple] = []
        total = 0
        pending: List[tuple] = [(remote_root, local_root)]

        def _collect(remote_dir: str, local_dir: str, listing) -> None:
            nonlocal total
            for entry in listing:
                self._check_cancel(record)
                child_remote = self._join_remote(remote_dir, entry.filename)
                child_local = os.path.join(local_dir, entry.filename)
                if entry.is_dir() and not entry.is_symlink():
                    os.makedirs(child_local, exist_ok=True)
                    next_level.append((child_remote, child_local))
                else:
                    size = entry.st_size or 0
                    files.append((child_remote, child_local, size, entry))
                    total += size

        # Breadth-first, one directory level at a time: a pipelining client
        # lists a whole level together instead of paying OPENDIR / READDIR /
        # CLOSE round trips per directory.
        list_many = getattr(client, "listdir_many", None)
        while pending:
            self._check_cancel(record)
            next_level: List[tuple] = []
            if callable(list_many):
                listings = list_many([remote for remote, _local in pending])
            else:
                listings = [client.listdir_attr(remote) for remote, _local in pending]
            for (remote_dir, local_dir), listing in zip(pending, listings):
                _collect(remote_dir, local_dir, listing)
            pending = next_level
        with self._lock:
            record.bytes_total = total

        # Small regular files are RTT-bound one by one (OPEN / READ / READ-EOF
        # / CLOSE each), so read them in pipelined windows. Symlinks keep the
        # serial path: their listing attrs describe the link, not the target.
        small: List[tuple] = []
        large: List[tuple] = []
        read_many = getattr(client, "read_small_files", None)
        limit = client.small_read_limit() if callable(read_many) else -1
        for entry in files:
            attr = entry[3]
            if not attr.is_symlink() and 0 <= entry[2] <= limit:
                small.append(entry)
            else:
                large.append(entry)

        completed = 0
        if small:
            completed = self._download_small_files_pipelined(record, client, small, completed)
        for remote_abs, local_abs, size, attr in large:
            self._check_cancel(record)
            parent = os.path.dirname(local_abs) or "."
            os.makedirs(parent, exist_ok=True)
            try:
                destination = self._resolve_local_destination(record, local_abs)
            except _TransferSkipped:
                completed += size
                continue
            copied = self._copy_remote_to_local(
                record,
                client,
                remote_abs,
                destination,
                base=completed,
                attr=None if attr.is_symlink() else attr,
            )
            completed += copied
            self._report_progress(record, completed)
        with self._lock:
            record.bytes_completed = completed

    @staticmethod
    def _join_remote(base: str, *parts: str) -> str:
        result = base.rstrip("/") or "/"
        for part in parts:
            cleaned = part.strip("/")
            if cleaned:
                result = result.rstrip("/") + "/" + cleaned
        return result

    def _ensure_remote_dirs(
        self, record: _TransferRecord, client, directories: List[tuple]
    ) -> None:
        """Create the ``(depth, remote_dir)`` tree, reusing existing dirs.

        With a pipelining client each depth level is one STAT batch plus one
        MKDIR batch (parents are always a level ahead of their children);
        otherwise directories are checked one by one.
        """
        if not (
            callable(getattr(client, "stat_many", None))
            and callable(getattr(client, "mkdir_many", None))
        ):
            for _depth, remote_dir in directories:
                self._check_cancel(record)
                self._ensure_remote_dir(record, client, remote_dir)
            return
        levels: Dict[int, List[str]] = {}
        for depth, remote_dir in directories:
            levels.setdefault(depth, []).append(remote_dir)
        for depth in sorted(levels):
            self._check_cancel(record)
            level = levels[depth]
            missing: List[str] = []
            for remote_dir, attr in zip(level, client.stat_many(level)):
                if attr is None:
                    missing.append(remote_dir)
                elif not attr.is_dir():
                    raise _sftp_transfer_error(
                        SftpFailureCode.REMOTE_FILE_BLOCKS_DIRECTORY,
                        ErrorCode.TRANSFER_CONFLICT,
                        parameters={"remote_dir": remote_dir},
                    )
            if not missing:
                continue
            for remote_dir, exc in zip(missing, client.mkdir_many(missing)):
                if exc is not None:
                    raise _sftp_transfer_error(
                        SftpFailureCode.REMOTE_DIRECTORY_CREATION_FAILED,
                        ErrorCode.TRANSFER_IO_FAILED,
                        parameters={"remote_dir": remote_dir},
                    ) from exc

    def _ensure_remote_dir(self, record: _TransferRecord, client, remote_dir: str) -> None:
        """Create a remote directory tree for uploads, reusing existing dirs."""
        try:
            attr = client.stat(remote_dir)
            if not attr.is_dir():
                raise _sftp_transfer_error(
                    SftpFailureCode.REMOTE_FILE_BLOCKS_DIRECTORY,
                    ErrorCode.TRANSFER_CONFLICT,
                    parameters={"remote_dir": remote_dir},
                )
            return
        except sftp_proto.SFTPError as exc:
            if exc.code != sftp_proto.FX_NO_SUCH_FILE:
                raise
        except SshPilotError:
            raise
        except Exception:
            pass
        try:
            client.mkdir(remote_dir)
        except Exception as exc:
            raise _sftp_transfer_error(
                SftpFailureCode.REMOTE_DIRECTORY_CREATION_FAILED,
                ErrorCode.TRANSFER_IO_FAILED,
                parameters={"remote_dir": remote_dir},
            ) from exc

    # -- per-file copy (atomic temp + rename) -------------------------------

    def _download_small_files_pipelined(
        self, record: _TransferRecord, client, files: List[tuple], base: int
    ) -> int:
        """Read *files* ``(remote, local, size, attr)`` in pipelined windows
        and commit each through a local temp, like :meth:`_copy_remote_to_local`."""
        completed = base
        # RENAME picks names by probing the disk, so it must run as each file
        # is written (an earlier file may take the next free name); the other
        # policies are decided up front so skipped files are never read.
        rename = record.conflict_policy is TransferConflictPolicy.RENAME
        wanted: List[tuple] = []
        for remote_abs, local_abs, size, attr in files:
            if rename:
                wanted.append((remote_abs, local_abs, size, attr))
                continue
            try:
                destination = self._resolve_local_destination(record, local_abs)
            except _TransferSkipped:
                completed += size
                continue
            wanted.append((remote_abs, destination, size, attr))
        if completed != base:
            self._report_progress(record, completed)

        batches = client.read_small_files(
            [(remote_abs, size) for remote_abs, _local, size, _attr in wanted],
            check_cancel=lambda: self._check_cancel(record),
        )
        for start, contents in batches:
            for offset, data in enumerate(contents):
                self._check_cancel(record)
                _remote, destination, _size, attr = wanted[start + offset]
                if rename:
                    destination = self._resolve_local_destination(record, destination)
                self._write_downloaded_file(record, destination, data, attr)
                completed += len(data)
                self._report_progress(record, completed)
        return completed

    def _write_downloaded_file(
        self, record: _TransferRecord, local_dst: str, data: bytes, attr
    ) -> None:
        parent = os.path.dirname(local_dst) or "."
        os.makedirs(parent, exist_ok=True)
        fd, temp_path = self._mkstemp(parent)
        with self._lock:
            record.local_temp_path = temp_path
        try:
            with os.fdopen(fd, "wb") as tmp_file:
                tmp_file.write(data)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            _apply_downloaded_metadata(temp_path, local_dst, attr)
            os.replace(temp_path, local_dst)
        except BaseException:
            self._cleanup_local_temp(record)
            raise
        with self._lock:
            record.local_temp_path = None

    def _copy_remote_to_local(
        self,
        record: _TransferRecord,
        client,
        remote_src: str,
        local_dst: str,
        base: int = 0,
        *,
        attr=None,
    ) -> int:
        """Download through a local temp. *attr*, when the caller already has
        the source's (non-symlink) attributes from a listing, saves a STAT."""
        parent = os.path.dirname(local_dst) or "."
        os.makedirs(parent, exist_ok=True)
        if attr is None:
            try:
                attr = client.stat(remote_src)
                with self._lock:
                    if record.bytes_total is None:
                        record.bytes_total = int(attr.st_size) if attr.st_size is not None else None
            except Exception:
                pass
        fd, temp_path = self._mkstemp(parent)
        with self._lock:
            record.local_temp_path = temp_path
        offset = 0
        try:
            with os.fdopen(fd, "wb") as tmp_file:
                handle = client.open_handle(remote_src, sftp_proto.FXF_READ)
                try:
                    self._check_cancel(record)
                    # Pipelined: several reads in flight, not one round trip
                    # per chunk.
                    for chunk in client.iter_read(handle):
                        self._check_cancel(record)
                        tmp_file.write(chunk)
                        offset += len(chunk)
                        self._report_progress(record, base + offset)
                finally:
                    client.close_handle(handle)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            _apply_downloaded_metadata(temp_path, local_dst, attr)
            os.replace(temp_path, local_dst)
        except BaseException:
            self._cleanup_local_temp(record)
            raise
        with self._lock:
            record.local_temp_path = None
        return offset

    def _copy_local_to_remote(
        self,
        record: _TransferRecord,
        client,
        local_src: str,
        remote_dst: str,
        base: int = 0,
        *,
        existing_mode: Optional[int] = None,
    ) -> int:
        """Upload through a temp file renamed over *remote_dst*.

        The rename replaces the target's inode, so its permissions would be
        lost: a replaced file keeps *existing_mode* (the temp is private until
        then), and a new file gets the local file's mode, narrowed by the
        server's umask as OpenSSH's ``sftp put`` does. The local mtime is kept.

        Commit uses ``atomic_rename`` (OpenSSH posix-rename when available,
        otherwise remove + standard RENAME) so non-OpenSSH SFTP servers still
        accept the upload.
        """
        local_info = os.stat(local_src)
        create_mode = 0o600 if existing_mode is not None else stat.S_IMODE(local_info.st_mode)
        remote_dir = remote_path_dirname(remote_dst)
        temp_name = f"{_TEMP_PREFIX}{new_transfer_id()}"
        remote_temp = (
            temp_name if remote_dir in (".", "") else remote_path_join(remote_dir, temp_name)
        )
        with self._lock:
            record.remote_temp_path = remote_temp
        offset = 0
        handle = client.open_handle(
            remote_temp,
            sftp_proto.FXF_WRITE | sftp_proto.FXF_CREAT | sftp_proto.FXF_TRUNC,
            sftp_proto.SFTPAttributes(st_mode=create_mode),
        )
        try:
            # Pipelined: several writes in flight, not one round trip per
            # chunk. Progress counts bytes sent; flush() waits for the acks.
            writer = client.pipelined_writer(handle)
            block = max(self._chunk_size, getattr(client, "max_write_length", 0))
            with open(local_src, "rb") as source:
                while True:
                    self._check_cancel(record)
                    chunk = source.read(block)
                    if not chunk:
                        break
                    writer.write(chunk)
                    offset += len(chunk)
                    self._report_progress(record, base + offset)
            writer.flush()
            self._apply_uploaded_metadata(client, handle, local_info, existing_mode)
        except BaseException:
            client.close_handle(handle)
            self._cleanup_remote_temp(record)
            raise
        client.close_handle(handle)
        try:
            # OpenSSH: posix-rename. Others (mod_sftp, AWS Transfer, …):
            # remove+FXP_RENAME — see OpenSSHSFTPClient.atomic_rename.
            client.atomic_rename(remote_temp, remote_dst)
        except Exception:
            self._cleanup_remote_temp(record)
            raise
        with self._lock:
            record.remote_temp_path = None
        return offset

    @staticmethod
    def _apply_uploaded_metadata(client, handle, local_info, existing_mode) -> None:
        """Set the kept mode and the local times on the finished temp file.

        Best effort: a server that refuses leaves the temp's private create
        mode, never a wider one.
        """
        attr = sftp_proto.SFTPAttributes(
            st_mode=existing_mode,
            st_atime=int(local_info.st_atime),
            st_mtime=int(local_info.st_mtime),
        )
        try:
            client.fsetstat(handle, attr)
        except sftp_proto.SFTPError as exc:
            if exc.code == sftp_proto.FX_CONNECTION_LOST:
                raise
            logger.debug("Could not set uploaded file attributes: %s", exc)

    def _resolve_local_destination(self, record: _TransferRecord, path: str) -> str:
        from sshpilot.core.transfers import ConflictDecision, OverwritePolicy, decide_conflict

        exists = os.path.exists(path)
        overwrite = {
            TransferConflictPolicy.FAIL: OverwritePolicy.FAIL,
            TransferConflictPolicy.OVERWRITE: OverwritePolicy.OVERWRITE,
            TransferConflictPolicy.SKIP: OverwritePolicy.SKIP,
            TransferConflictPolicy.RENAME: OverwritePolicy.RENAME,
        }.get(record.conflict_policy, OverwritePolicy.FAIL)
        decision = decide_conflict(exists, overwrite)
        if decision is ConflictDecision.PROCEED:
            return path
        if decision is ConflictDecision.FAIL:
            raise _sftp_transfer_error(
                SftpFailureCode.LOCAL_DESTINATION_EXISTS,
                ErrorCode.TRANSFER_CONFLICT,
                parameters={"path": path},
            )
        if decision is ConflictDecision.SKIP:
            raise _TransferSkipped()
        if decision is ConflictDecision.RENAME:
            base, ext = os.path.splitext(path)
            for index in range(1, 1000):
                candidate = f"{base} ({index}){ext}"
                if not os.path.exists(candidate):
                    return candidate
            raise _sftp_transfer_error(
                SftpFailureCode.NO_FREE_LOCAL_FILENAME,
                ErrorCode.TRANSFER_CONFLICT,
            )
        raise AssertionError("unhandled transfer conflict policy")

    def _resolve_remote_destination(
        self, record: _TransferRecord, client, path: str
    ) -> Tuple[str, Optional[int]]:
        """Return the path to upload to and, when it replaces an existing
        file, that file's permission bits (which the upload keeps)."""
        from sshpilot.core.transfers import ConflictDecision, OverwritePolicy, decide_conflict

        existing_attr = None
        try:
            existing_attr = client.stat(path)
            exists = True
        except sftp_proto.SFTPError:
            exists = False
        except Exception:
            exists = False
        existing_mode = getattr(existing_attr, "st_mode", None)
        if existing_mode is not None:
            existing_mode = stat.S_IMODE(existing_mode)
        overwrite = {
            TransferConflictPolicy.FAIL: OverwritePolicy.FAIL,
            TransferConflictPolicy.OVERWRITE: OverwritePolicy.OVERWRITE,
            TransferConflictPolicy.SKIP: OverwritePolicy.SKIP,
            TransferConflictPolicy.RENAME: OverwritePolicy.RENAME,
        }.get(record.conflict_policy, OverwritePolicy.FAIL)
        decision = decide_conflict(exists, overwrite)
        if decision is ConflictDecision.PROCEED:
            return path, existing_mode
        if decision is ConflictDecision.FAIL:
            raise _sftp_transfer_error(
                SftpFailureCode.REMOTE_DESTINATION_EXISTS,
                ErrorCode.TRANSFER_CONFLICT,
                parameters={"path": path},
            )
        if decision is ConflictDecision.SKIP:
            raise _TransferSkipped()
        if decision is ConflictDecision.RENAME:
            base, dot, ext = path.rpartition(".")
            stem, ext = (base, f".{ext}") if dot else (path, "")
            for index in range(1, 1000):
                candidate = f"{stem} ({index}){ext}"
                try:
                    client.stat(candidate)
                except Exception:
                    return candidate, None
            raise _sftp_transfer_error(
                SftpFailureCode.NO_FREE_REMOTE_FILENAME,
                ErrorCode.TRANSFER_CONFLICT,
            )
        raise AssertionError("unhandled transfer conflict policy")

    @staticmethod
    def _mkstemp(parent: str):
        import tempfile

        return tempfile.mkstemp(prefix=_TEMP_PREFIX, dir=parent)

    def _check_cancel(self, record: _TransferRecord) -> None:
        with self._lock:
            if record.cancel_requested:
                raise _TransferCancelled()

    def _report_progress(self, record: _TransferRecord, bytes_completed: int) -> None:
        now = self._monotonic()
        with self._lock:
            record.bytes_completed = bytes_completed
            elapsed = now - record.last_progress_monotonic
            delta = bytes_completed - record.last_progress_bytes
            if (
                elapsed < self._progress_min_interval_seconds
                and delta < self._progress_min_bytes
            ):
                return
            record.last_progress_monotonic = now
            record.last_progress_bytes = bytes_completed
            event = self._event_locked(record, EventType.TRANSFER_PROGRESS)
        self._publish((event,))

    def _cleanup_local_temp(self, record: _TransferRecord) -> None:
        with self._lock:
            path = record.local_temp_path
            record.local_temp_path = None
        if path:
            try:
                os.remove(path)
            except OSError:
                pass

    def _cleanup_remote_temp(self, record: _TransferRecord) -> None:
        with self._lock:
            path = record.remote_temp_path
            record.remote_temp_path = None
        if not path:
            return
        try:
            client, _ = self._sftp_runtime.acquire_active_client(
                record.sftp_service_id, record.owner_client_id
            )
            client.remove(path)
        except Exception:  # pragma: no cover - best-effort cleanup
            pass

    def _finish_completed(self, record: _TransferRecord) -> None:
        with self._lock:
            # Skipped (conflict policy) or single-file transfers may not have
            # accounted every byte during the run; a completed transfer reports
            # every byte as handled so progress reaches 100%.
            if record.bytes_total is not None:
                record.bytes_completed = max(
                    record.bytes_completed or 0, record.bytes_total
                )
            if record.state is TransferState.CANCELLING:
                record.completed_at = self._clock()
                event = self._transition_locked(record, TransferState.CANCELLED)
            elif record.state in {TransferState.RUNNING, TransferState.STARTING}:
                record.completed_at = self._clock()
                event = self._transition_locked(record, TransferState.COMPLETED)
            else:
                return
        self._publish((event,))

    def _finish_cancelled(self, record: _TransferRecord) -> None:
        self._cleanup_local_temp(record)
        self._cleanup_remote_temp(record)
        events: List[Optional[CoreEvent]] = []
        with self._lock:
            if record.state in {TransferState.CANCELLED, TransferState.FAILED, TransferState.COMPLETED}:
                return
            # Shutdown (and any cancel_requested path) may still be RUNNING /
            # STARTING / QUEUED; walk through CANCELLING before CANCELLED.
            if record.state in {
                TransferState.QUEUED,
                TransferState.STARTING,
                TransferState.RUNNING,
            }:
                events.append(self._transition_locked(record, TransferState.CANCELLING))
            if record.state is not TransferState.CANCELLING:
                return
            record.completed_at = self._clock()
            record.cancel_requested = False
            events.append(self._transition_locked(record, TransferState.CANCELLED))
        self._publish(events)

    def _fail_scp(self, record: _TransferRecord, failure: ScpFailure) -> None:
        self._cleanup_local_temp(record)
        self._cleanup_remote_temp(record)
        with self._lock:
            if record.state in _TERMINAL_STATES:
                return
            record.failure = failure
            record.completed_at = self._clock()
            event = self._transition_locked(record, TransferState.FAILED)
        self._publish((event,))

    def _fail_sftp(self, record: _TransferRecord, failure: SftpFailure) -> None:
        self._cleanup_local_temp(record)
        self._cleanup_remote_temp(record)
        with self._lock:
            if record.state in _TERMINAL_STATES:
                return
            record.failure = failure
            record.completed_at = self._clock()
            event = self._transition_locked(record, TransferState.FAILED)
        self._publish((event,))

    # -- cancel -----------------------------------------------------------
    def prepare_cancel_transfer(
        self,
        request: CancelTransferRequest,
        *,
        client_id: ClientId,
    ) -> bool:
        if type(request) is not CancelTransferRequest:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "A cancel transfer request is required",
            )
        events: List[Optional[CoreEvent]] = []
        with self._lock:
            self._require_accepting_commands_locked()
            record = self._record_locked(request.transfer_id)
            self._require_owner(record, client_id)
            if record.state in _TERMINAL_STATES or record.state is TransferState.CANCELLING:
                return False
            record.cancel_requested = True
            if record.scp_cancel_event is not None:
                record.scp_cancel_event.set()
            events.append(self._transition_locked(record, TransferState.CANCELLING))
            # Queued-but-not-started transfers finish without a worker thread.
            if request.transfer_id in self._pending_run:
                self._pending_run.remove(request.transfer_id)
                record.completed_at = self._clock()
                events.append(self._transition_locked(record, TransferState.CANCELLED))
        self._publish(events)
        return True

    # -- shutdown -----------------------------------------------------
    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._accepting_commands = False
            self._pending_run.clear()
            for record in self._records.values():
                if record.state not in _TERMINAL_STATES:
                    record.cancel_requested = True
                    if record.scp_cancel_event is not None:
                        record.scp_cancel_event.set()
            threads = tuple(self._worker_threads.values())
        deadline = self._monotonic() + self._shutdown_timeout_seconds
        for thread in threads:
            remaining = max(0.0, deadline - self._monotonic())
            thread.join(remaining)
        with self._lock:
            stuck = [
                record
                for record in self._records.values()
                if record.state not in _TERMINAL_STATES
            ]
        for record in stuck:
            if record.backend is TransferBackend.SFTP:
                self._fail_sftp(
                    record,
                    SftpFailure(
                        code=SftpFailureCode.DAEMON_SHUTTING_DOWN,
                        error_code=ErrorCode.DAEMON_SHUTTING_DOWN,
                    ),
                )
            else:
                self._fail_scp(
                    record,
                    ScpFailure(
                        code=ScpFailureCode.DAEMON_SHUTTING_DOWN,
                        error_code=ErrorCode.DAEMON_SHUTTING_DOWN,
                    ),
                )
        with self._lock:
            self._closed = True
        self._publisher.close()

    # -- helpers ------------------------------------------------------
    def _count_inflight_locked(self) -> int:
        return sum(
            1 for record in self._records.values() if record.state not in _TERMINAL_STATES
        )

    def _count_service_workers_locked(self, sftp_service_id: SftpServiceId) -> int:
        count = 0
        for transfer_id in self._worker_threads:
            record = self._records.get(transfer_id)
            if (
                record is not None
                and record.backend is TransferBackend.SFTP
                and record.sftp_service_id == sftp_service_id
            ):
                count += 1
        return count

    def _can_start_locked(self, record: _TransferRecord) -> bool:
        if len(self._worker_threads) >= self._effective_max_concurrent_transfers():
            return False
        if record.backend is TransferBackend.SFTP:
            running = self._count_service_workers_locked(record.sftp_service_id)
            if running >= max(1, int(record.service_concurrency_limit)):
                return False
        return True

    def _schedule_or_queue_locked(self, transfer_id: TransferId) -> Optional[threading.Thread]:
        record = self._records.get(transfer_id)
        if record is not None and self._can_start_locked(record):
            return self._register_worker_thread_locked(transfer_id)
        self._pending_run.append(transfer_id)
        return None

    def _register_worker_thread_locked(self, transfer_id: TransferId) -> threading.Thread:
        thread = threading.Thread(
            target=self._transfer_worker,
            args=(transfer_id,),
            name=f"sshpilot-transfer-{transfer_id}",
            daemon=True,
        )
        self._worker_threads[transfer_id] = thread
        return thread

    def _take_next_runnable_locked(self) -> Optional[TransferId]:
        """Pop the oldest queued transfer that fits global + per-service caps."""
        kept: List[TransferId] = []
        selected: Optional[TransferId] = None
        for transfer_id in self._pending_run:
            record = self._records.get(transfer_id)
            if record is None:
                continue
            if record.state is not TransferState.QUEUED or record.cancel_requested:
                continue
            if selected is None and self._can_start_locked(record):
                selected = transfer_id
                continue
            kept.append(transfer_id)
        self._pending_run = kept
        return selected

    def _promote_queued_locked(self, transfer_id: TransferId) -> Optional[threading.Thread]:
        """Assign a worker to a previously queued transfer."""

        record = self._records.get(transfer_id)
        if record is None or record.state is not TransferState.QUEUED:
            return None
        if not self._can_start_locked(record):
            # Put it back; a later completion may free a service slot.
            self._pending_run.insert(0, transfer_id)
            return None
        if record.started_at is None:
            record.started_at = self._clock()
        self._transition_locked(record, TransferState.STARTING)
        return self._register_worker_thread_locked(transfer_id)

    @staticmethod
    def _require_owner(record: _TransferRecord, client_id: ClientId) -> None:
        if record.owner_client_id is not None and record.owner_client_id != client_id:
            raise SshPilotError(
                ErrorCode.SERVICE_OWNER_REQUIRED,
                "Only the originating client may mutate this transfer",
                connection_id=record.connection_id,
                details={"transfer_id": record.transfer_id},
            )

    def detach_client(self, client_id: Optional[ClientId]) -> None:
        """Orphan all transfers owned by the disconnecting client.

        The transfer keeps running; ownership is cleared so any
        reconnecting client can claim and manage the resource.
        """
        if client_id is None:
            return
        with self._lock:
            for record in self._records.values():
                if record.owner_client_id == client_id:
                    record.owner_client_id = None

    def _transition_locked(
        self,
        record: _TransferRecord,
        new_state: TransferState,
    ) -> Optional[CoreEvent]:
        if not is_valid_transfer_transition(record.state, new_state):
            raise RuntimeError(
                f"invalid transfer transition {record.state.value}->{new_state.value}"
            )
        record.state = new_state
        if new_state in _TERMINAL_STATES:
            self._evict_completed_locked()
        event_type = _TRANSITION_EVENT_TYPES.get(new_state)
        if event_type is None:
            return None
        return self._event_locked(record, event_type)

    def _event_locked(self, record: _TransferRecord, event_type: EventType) -> CoreEvent:
        return CoreEvent(
            type=event_type,
            payload=self._summary_locked(record),
            sequence=0,
            connection_id=record.connection_id,
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
                )
            except RuntimeError:
                return

    def _record_locked(self, transfer_id: TransferId) -> _TransferRecord:
        record = self._records.get(transfer_id)
        if record is None:
            raise SshPilotError(
                ErrorCode.TRANSFER_NOT_FOUND,
                "The requested transfer does not exist",
                details={"transfer_id": transfer_id},
            )
        return record

    @staticmethod
    def _summary_locked(record: _TransferRecord) -> TransferSummary:
        return TransferSummary(
            id=record.transfer_id,
            connection_id=record.connection_id,
            sftp_service_id=record.sftp_service_id,
            backend=record.backend,
            direction=record.direction,
            state=record.state,
            source_display=record.source_display,
            destination_display=record.destination_display,
            bytes_total=record.bytes_total,
            bytes_completed=record.bytes_completed,
            created_at=record.created_at,
            started_at=record.started_at,
            completed_at=record.completed_at,
            owner_client_id=record.owner_client_id,
            failure=record.failure,
        )

    def _evict_completed_locked(self) -> None:
        completed = [
            transfer_id
            for transfer_id in self._creation_order
            if (
                transfer_id in self._records
                and self._records[transfer_id].state in _TERMINAL_STATES
            )
        ]
        excess = len(completed) - self._max_retained_completed_transfers
        for transfer_id in completed[: max(0, excess)]:
            self._records.pop(transfer_id, None)
            try:
                self._creation_order.remove(transfer_id)
            except ValueError:
                pass

    def _require_accepting_commands_locked(self) -> None:
        if not self._accepting_commands or self._closed:
            raise SshPilotError(
                ErrorCode.DAEMON_SHUTTING_DOWN,
                "The transfer runtime is shutting down",
                retryable=True,
            )

    def _require_accepting_reads_locked(self) -> None:
        if self._closed:
            raise SshPilotError(
                ErrorCode.DAEMON_SHUTTING_DOWN,
                "The transfer runtime is shut down",
                retryable=True,
            )

"""File-transfer models for daemon-owned upload/download operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Tuple, Union

from .common import (
    ClientId,
    ConnectionId,
    SftpServiceId,
    TransferId,
    require_identifier,
    utc_now,
)
from .operations import ServiceFailure, SftpFailure


class TransferBackend(str, Enum):
    SFTP = "sftp"
    NATIVE_SCP = "native_scp"


class TransferDirection(str, Enum):
    UPLOAD = "upload"
    DOWNLOAD = "download"


class TransferState(str, Enum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class TransferConflictPolicy(str, Enum):
    FAIL = "fail"
    OVERWRITE = "overwrite"
    SKIP = "skip"
    RENAME = "rename"


class TransferLocalMode(str, Enum):
    """How local bytes are supplied or received for a transfer."""

    DAEMON_PATH = "daemon_path"
    BINARY_STREAM = "binary_stream"


@dataclass(frozen=True)
class TransferSummary:
    id: TransferId
    connection_id: ConnectionId
    sftp_service_id: Optional[SftpServiceId]
    direction: TransferDirection
    state: TransferState
    source_display: str
    destination_display: str
    backend: TransferBackend = TransferBackend.SFTP
    bytes_total: Optional[int] = None
    bytes_completed: int = 0
    created_at: datetime = field(default_factory=utc_now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    owner_client_id: Optional[ClientId] = None
    failure: Optional[Union[ServiceFailure, SftpFailure]] = None
    # Legacy field retained for older schema readers.
    bytes_transferred: Optional[int] = None
    total_bytes: Optional[int] = None

    def __post_init__(self) -> None:
        require_identifier(self.id, "transfer id")
        require_identifier(self.connection_id, "connection id")
        if self.sftp_service_id is not None:
            require_identifier(self.sftp_service_id, "SFTP service id")
        if not isinstance(self.backend, TransferBackend):
            raise TypeError("transfer backend must be a TransferBackend")
        if self.backend is TransferBackend.SFTP and self.sftp_service_id is None:
            raise ValueError("SFTP transfers require an SFTP service id")
        if not isinstance(self.direction, TransferDirection):
            raise TypeError("transfer direction must be a TransferDirection")
        if not isinstance(self.state, TransferState):
            raise TypeError("transfer state must be a TransferState")
        if type(self.source_display) is not str or not self.source_display:
            raise ValueError("source_display must be a non-empty string")
        if type(self.destination_display) is not str or not self.destination_display:
            raise ValueError("destination_display must be a non-empty string")
        completed = self.bytes_completed
        if self.bytes_transferred is not None:
            completed = self.bytes_transferred
        if type(completed) is not int or completed < 0:
            raise ValueError("completed byte count must not be negative")
        total = self.bytes_total if self.bytes_total is not None else self.total_bytes
        if total is not None and (type(total) is not int or total < 0):
            raise ValueError("total byte count must not be negative")
        if type(self.created_at) is not datetime or self.created_at.tzinfo is None:
            raise ValueError("transfer creation time must be timezone-aware")
        if self.started_at is not None and (
            type(self.started_at) is not datetime or self.started_at.tzinfo is None
        ):
            raise ValueError("transfer started_at must be timezone-aware or None")
        if self.completed_at is not None and (
            type(self.completed_at) is not datetime or self.completed_at.tzinfo is None
        ):
            raise ValueError("transfer completed_at must be timezone-aware or None")
        if self.owner_client_id is not None:
            require_identifier(self.owner_client_id, "transfer owner client id")
        if self.failure is not None and type(self.failure) not in {
            ServiceFailure,
            SftpFailure,
        }:
            raise TypeError(
                "transfer failure must be ServiceFailure, SftpFailure, or None"
            )
        if self.failure is not None:
            expected_failure_type = (
                SftpFailure
                if self.backend is TransferBackend.SFTP
                else ServiceFailure
            )
            if type(self.failure) is not expected_failure_type:
                raise TypeError(
                    "transfer failure type does not match the transfer backend"
                )


@dataclass(frozen=True)
class StartTransferRequest:
    connection_id: ConnectionId
    sftp_service_id: SftpServiceId
    direction: TransferDirection
    remote_path: str
    local_path: str
    conflict_policy: TransferConflictPolicy = TransferConflictPolicy.FAIL
    recursive: bool = False
    local_mode: TransferLocalMode = TransferLocalMode.DAEMON_PATH

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")
        require_identifier(self.sftp_service_id, "SFTP service id")
        if not isinstance(self.direction, TransferDirection):
            raise TypeError("transfer direction must be a TransferDirection")
        if not self.remote_path or "\x00" in self.remote_path:
            raise ValueError("remote_path must be a non-empty NUL-free string")
        if not self.local_path or "\x00" in self.local_path:
            raise ValueError("local_path must be a non-empty NUL-free string")
        if not isinstance(self.conflict_policy, TransferConflictPolicy):
            raise TypeError("conflict_policy must be a TransferConflictPolicy")
        if type(self.recursive) is not bool:
            raise TypeError("recursive must be a boolean")
        if not isinstance(self.local_mode, TransferLocalMode):
            raise TypeError("local_mode must be a TransferLocalMode")
        if self.local_mode is not TransferLocalMode.DAEMON_PATH:
            raise ValueError("binary streaming mode is not implemented in Phase 10")


@dataclass(frozen=True)
class StartScpTransferRequest:
    connection_id: ConnectionId
    direction: TransferDirection
    sources: Tuple[str, ...]
    destination: str
    conflict_policy: TransferConflictPolicy = TransferConflictPolicy.OVERWRITE
    recursive: bool = False

    MAX_SOURCES = 64
    MAX_ENCODED_PATH_BYTES = 64 * 1024

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")
        if not isinstance(self.direction, TransferDirection):
            raise TypeError("transfer direction must be a TransferDirection")
        if type(self.sources) is not tuple or not self.sources:
            raise ValueError("SCP sources must be a non-empty tuple")
        if len(self.sources) > self.MAX_SOURCES:
            raise ValueError("SCP source count exceeds the limit")
        if type(self.destination) is not str or not self.destination:
            raise ValueError("SCP destination must be a non-empty string")
        paths = (*self.sources, self.destination)
        if any(type(path) is not str or not path or "\x00" in path for path in paths):
            raise ValueError("SCP paths must be non-empty NUL-free strings")
        if sum(len(path.encode("utf-8")) for path in paths) > self.MAX_ENCODED_PATH_BYTES:
            raise ValueError("SCP paths exceed the encoded size limit")
        if not isinstance(self.conflict_policy, TransferConflictPolicy):
            raise TypeError("conflict_policy must be a TransferConflictPolicy")
        if self.conflict_policy is not TransferConflictPolicy.OVERWRITE:
            raise ValueError("native SCP supports overwrite only")
        if type(self.recursive) is not bool:
            raise TypeError("recursive must be a boolean")


@dataclass(frozen=True)
class CancelTransferRequest:
    transfer_id: TransferId

    def __post_init__(self) -> None:
        require_identifier(self.transfer_id, "transfer id")

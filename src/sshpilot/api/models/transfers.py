"""Schema-only file-transfer models for later protocol phases."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from .common import ConnectionId, TransferId, require_identifier, utc_now


class TransferDirection(str, Enum):
    UPLOAD = "upload"
    DOWNLOAD = "download"


class TransferState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TransferSummary:
    id: TransferId
    connection_id: ConnectionId
    direction: TransferDirection
    state: TransferState
    bytes_transferred: int = 0
    total_bytes: Optional[int] = None
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        require_identifier(self.id, "transfer id")
        require_identifier(self.connection_id, "connection id")
        if self.bytes_transferred < 0:
            raise ValueError("transferred byte count must not be negative")
        if self.total_bytes is not None and self.total_bytes < 0:
            raise ValueError("total byte count must not be negative")


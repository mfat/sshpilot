"""Transfer policy models (GTK-free).

Daemon runtime executes transfers; GTK renders progress. Both consume these
shared request/policy models. Recursive transfers are explicitly unsupported
until implemented end-to-end.
"""
from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Optional


class TransferDirection(str, Enum):
    UPLOAD = "upload"
    DOWNLOAD = "download"


class TransferState(str, Enum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class OverwritePolicy(str, Enum):
    FAIL = "fail"
    OVERWRITE = "overwrite"
    SKIP = "skip"
    RENAME = "rename"


class TransferErrorKind(str, Enum):
    VALIDATION = "validation"
    CONFLICT = "conflict"
    CANCELLED = "cancelled"
    IO = "io"
    UNSUPPORTED = "unsupported"
    QUEUE_FULL = "queue_full"
    UNKNOWN = "unknown"


_INVALID_REMOTE = re.compile(r"[\x00]")


@dataclass(frozen=True)
class PathRef:
    """Local or remote path description."""

    path: str
    is_remote: bool = False

    def normalized(self) -> "PathRef":
        raw = (self.path or "").strip()
        if self.is_remote:
            # Collapse duplicate slashes but keep absolute root.
            while "//" in raw:
                raw = raw.replace("//", "/")
            return PathRef(path=raw or "/", is_remote=True)
        return PathRef(path=os.path.expanduser(raw), is_remote=False)


@dataclass(frozen=True)
class TransferRequest:
    direction: TransferDirection
    source: PathRef
    destination: PathRef
    overwrite: OverwritePolicy = OverwritePolicy.FAIL
    recursive: bool = False
    atomic: bool = True

    def validate(self) -> None:
        from ..errors import CoreError, ErrorCode

        if self.recursive:
            raise CoreError(
                ErrorCode.UNSUPPORTED_OPERATION,
                "Recursive transfers are not implemented",
            )
        src = self.source.normalized()
        dst = self.destination.normalized()
        if not src.path or _INVALID_REMOTE.search(src.path):
            raise CoreError(ErrorCode.VALIDATION_ERROR, "Invalid source path")
        if not dst.path or _INVALID_REMOTE.search(dst.path):
            raise CoreError(ErrorCode.VALIDATION_ERROR, "Invalid destination path")
        if self.direction == TransferDirection.UPLOAD:
            if src.is_remote or not dst.is_remote:
                raise CoreError(
                    ErrorCode.VALIDATION_ERROR,
                    "Upload requires local source and remote destination",
                )
            if not os.path.exists(src.path):
                raise CoreError(ErrorCode.VALIDATION_ERROR, f"Local path does not exist: {src.path}")
            if not os.path.isfile(src.path):
                raise CoreError(
                    ErrorCode.VALIDATION_ERROR,
                    "Upload source must be a regular file (recursive not supported)",
                )
        elif self.direction == TransferDirection.DOWNLOAD:
            if not src.is_remote or dst.is_remote:
                raise CoreError(
                    ErrorCode.VALIDATION_ERROR,
                    "Download requires remote source and local destination",
                )
            parent = os.path.dirname(dst.path) or "."
            if not os.path.isdir(parent):
                raise CoreError(
                    ErrorCode.VALIDATION_ERROR,
                    f"Local destination directory does not exist: {parent}",
                )


@dataclass
class Progress:
    bytes_completed: int = 0
    bytes_total: Optional[int] = None

    def advance(self, completed: int) -> "Progress":
        completed = max(int(completed), self.bytes_completed)  # monotonic
        return Progress(bytes_completed=completed, bytes_total=self.bytes_total)


@dataclass
class TransferSummary:
    id: str
    direction: TransferDirection
    state: TransferState
    source: str
    destination: str
    progress: Progress = field(default_factory=Progress)
    error_kind: Optional[TransferErrorKind] = None
    message: str = ""


_ALLOWED_TRANSITIONS = {
    TransferState.QUEUED: {TransferState.STARTING, TransferState.CANCELLED, TransferState.FAILED},
    TransferState.STARTING: {
        TransferState.RUNNING,
        TransferState.CANCELLING,
        TransferState.FAILED,
        TransferState.CANCELLED,
    },
    TransferState.RUNNING: {
        TransferState.CANCELLING,
        TransferState.COMPLETED,
        TransferState.FAILED,
        TransferState.CANCELLED,
    },
    TransferState.CANCELLING: {TransferState.CANCELLED, TransferState.FAILED, TransferState.COMPLETED},
    TransferState.CANCELLED: set(),
    TransferState.COMPLETED: set(),
    TransferState.FAILED: set(),
}


def transition(summary: TransferSummary, new_state: TransferState) -> TransferSummary:
    allowed = _ALLOWED_TRANSITIONS.get(summary.state, set())
    if new_state not in allowed:
        from ..errors import CoreError, ErrorCode

        raise CoreError(
            ErrorCode.VALIDATION_ERROR,
            f"Invalid transfer transition {summary.state.value} → {new_state.value}",
        )
    return replace(summary, state=new_state)


def atomic_temp_name(destination: str) -> str:
    """Generate a sibling temporary name for atomic replacement."""
    base = os.path.basename(destination) or "transfer"
    parent = os.path.dirname(destination) or "."
    return os.path.join(parent, f".{base}.sshpilot-tmp-{uuid.uuid4().hex[:8]}")


@dataclass
class TransferQueuePolicy:
    max_queued: int = 32
    max_concurrent: int = 2

    def admit(self, queued_count: int, running_count: int) -> Optional[TransferErrorKind]:
        if queued_count >= self.max_queued:
            return TransferErrorKind.QUEUE_FULL
        return None

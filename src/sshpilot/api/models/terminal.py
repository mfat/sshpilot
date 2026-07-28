"""Terminal byte-stream and replay schemas."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .common import AttachmentId, SessionId, require_identifier, utc_now


MAX_TERMINAL_DIMENSION = 10_000
MAX_REPLAY_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class TerminalDimensions:
    rows: int
    columns: int

    def __post_init__(self) -> None:
        if not 1 <= self.rows <= MAX_TERMINAL_DIMENSION:
            raise ValueError("terminal rows are outside the supported range")
        if not 1 <= self.columns <= MAX_TERMINAL_DIMENSION:
            raise ValueError("terminal columns are outside the supported range")


@dataclass(frozen=True)
class TerminalInput:
    session_id: SessionId
    attachment_id: AttachmentId
    data: bytes = field(repr=False)

    def __post_init__(self) -> None:
        require_identifier(self.session_id, "session id")
        require_identifier(self.attachment_id, "attachment id")
        if not isinstance(self.data, bytes):
            raise TypeError("terminal input must be bytes")


@dataclass(frozen=True)
class TerminalOutput:
    session_id: SessionId
    sequence: int
    data: bytes = field(repr=False)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        require_identifier(self.session_id, "session id")
        if self.sequence < 0:
            raise ValueError("terminal output sequence must not be negative")
        if not isinstance(self.data, bytes):
            raise TypeError("terminal output must be bytes")


@dataclass(frozen=True)
class ResizeTerminalRequest:
    session_id: SessionId
    attachment_id: AttachmentId
    dimensions: TerminalDimensions

    def __post_init__(self) -> None:
        require_identifier(self.session_id, "session id")
        require_identifier(self.attachment_id, "attachment id")


@dataclass(frozen=True)
class ReplayRequest:
    session_id: SessionId
    after_sequence: Optional[int] = None
    max_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        require_identifier(self.session_id, "session id")
        if self.after_sequence is not None and self.after_sequence < 0:
            raise ValueError("replay sequence must not be negative")
        if not 1 <= self.max_bytes <= MAX_REPLAY_BYTES:
            raise ValueError("replay byte limit is outside the supported range")


@dataclass(frozen=True)
class ReplayBounds:
    earliest_sequence: int
    latest_sequence: int
    retained_bytes: int

    def __post_init__(self) -> None:
        if self.earliest_sequence < 0 or self.latest_sequence < 0:
            raise ValueError("replay sequence bounds must not be negative")
        if self.latest_sequence < self.earliest_sequence:
            raise ValueError("latest replay sequence precedes earliest sequence")
        if self.retained_bytes < 0:
            raise ValueError("retained byte count must not be negative")


@dataclass(frozen=True)
class ReplayResult:
    session_id: SessionId
    data: bytes = field(repr=False)
    first_sequence: int
    next_sequence: int
    bounds: ReplayBounds
    truncated: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes):
            raise TypeError("terminal replay data must be bytes")
        if self.first_sequence < 0 or self.next_sequence < self.first_sequence:
            raise ValueError("invalid terminal replay sequence range")

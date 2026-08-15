"""Terminal byte-stream and replay schemas."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Tuple

from .common import AttachmentId, SessionId, require_identifier, utc_now


MAX_TERMINAL_DIMENSION = 1_000
MAX_REPLAY_BYTES = 16 * 1024 * 1024
MAX_TERMINAL_BROADCAST_TARGETS = 256
MAX_TERMINAL_BROADCAST_COMMAND_BYTES = 65_536


@dataclass(frozen=True)
class ExternalTerminalLaunchSpec:
    """Daemon-prepared, non-secret launch data for an external terminal."""

    argv: Tuple[str, ...]
    environment: Tuple[Tuple[str, str], ...] = ()
    display_name: str = ""
    secret_autofill_supported: bool = False

    def __post_init__(self) -> None:
        if type(self.argv) is not tuple or not self.argv:
            raise ValueError("external terminal argv must be a non-empty tuple")
        for argument in self.argv:
            if type(argument) is not str or not argument or "\x00" in argument:
                raise ValueError("external terminal argv contains an invalid value")
        if type(self.environment) is not tuple:
            raise TypeError("external terminal environment must be a tuple")
        names = set()
        for item in self.environment:
            if type(item) is not tuple or len(item) != 2:
                raise ValueError("external terminal environment entries are pairs")
            name, value = item
            if (
                type(name) is not str
                or not name
                or "\x00" in name
                or type(value) is not str
                or "\x00" in value
            ):
                raise ValueError("external terminal environment contains an invalid value")
            if name in names:
                raise ValueError("external terminal environment contains duplicates")
            names.add(name)
        if type(self.display_name) is not str or "\x00" in self.display_name:
            raise ValueError("external terminal display name is invalid")
        if type(self.secret_autofill_supported) is not bool:
            raise TypeError("secret_autofill_supported must be a boolean")


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
class BroadcastTerminalInputRequest:
    """Write one command to the existing interactive terminal sessions."""

    session_ids: Tuple[SessionId, ...]
    command: str = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.session_ids) is not tuple or not self.session_ids:
            raise ValueError("terminal broadcast requires at least one session id")
        if len(self.session_ids) > MAX_TERMINAL_BROADCAST_TARGETS:
            raise ValueError("terminal broadcast target limit exceeded")
        seen: set[str] = set()
        for session_id in self.session_ids:
            require_identifier(session_id, "session id")
            if session_id in seen:
                raise ValueError("duplicate terminal broadcast session id")
            seen.add(session_id)
        if type(self.command) is not str or not self.command.strip() or "\x00" in self.command:
            raise ValueError("terminal broadcast command must be non-empty and contain no NUL")
        if len(self.command.encode("utf-8")) > MAX_TERMINAL_BROADCAST_COMMAND_BYTES:
            raise ValueError("terminal broadcast command size limit exceeded")


@dataclass(frozen=True)
class TerminalOutput:
    session_id: SessionId
    sequence: int
    data: bytes = field(repr=False)
    created_at: datetime = field(default_factory=utc_now)
    replay: bool = False
    eof: bool = False

    def __post_init__(self) -> None:
        require_identifier(self.session_id, "session id")
        if self.sequence < 0:
            raise ValueError("terminal output sequence must not be negative")
        if not isinstance(self.data, bytes):
            raise TypeError("terminal output must be bytes")
        if type(self.replay) is not bool or type(self.eof) is not bool:
            raise TypeError("terminal output flags must be booleans")

    @property
    def next_sequence(self) -> int:
        return self.sequence + len(self.data)


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
    attachment_id: Optional[AttachmentId] = None
    after_sequence: Optional[int] = None
    max_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        require_identifier(self.session_id, "session id")
        if self.attachment_id is not None:
            require_identifier(self.attachment_id, "attachment id")
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
    first_sequence: int
    next_sequence: int
    bounds: ReplayBounds
    data: bytes = field(default=b"", repr=False)
    truncated: bool = False
    eof: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes):
            raise TypeError("terminal replay data must be bytes")
        if self.first_sequence < 0 or self.next_sequence < self.first_sequence:
            raise ValueError("invalid terminal replay sequence range")
        if type(self.truncated) is not bool or type(self.eof) is not bool:
            raise TypeError("terminal replay flags must be booleans")


@dataclass(frozen=True)
class ClaimTerminalInputRequest:
    session_id: SessionId
    attachment_id: AttachmentId

    def __post_init__(self) -> None:
        require_identifier(self.session_id, "session id")
        require_identifier(self.attachment_id, "attachment id")


@dataclass(frozen=True)
class ReleaseTerminalInputRequest:
    session_id: SessionId
    attachment_id: AttachmentId

    def __post_init__(self) -> None:
        require_identifier(self.session_id, "session id")
        require_identifier(self.attachment_id, "attachment id")

"""Frontend-neutral session lifecycle models for Protocol v1."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import FrozenSet, Optional

from .common import (
    AttachmentId,
    ClientId,
    ConnectionId,
    SessionId,
    require_identifier,
    utc_now,
)
from .terminal import TerminalDimensions


class SessionState(str, Enum):
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    CLOSING = "closing"
    EXITED = "exited"
    FAILED = "failed"
    CLOSED = "closed"


@dataclass(frozen=True)
class OpenSessionRequest:
    connection_id: ConnectionId
    dimensions: Optional[TerminalDimensions] = None
    remote_command: Optional[str] = None
    force_tty: bool = False

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")
        if self.dimensions is not None and type(self.dimensions) is not TerminalDimensions:
            raise TypeError("initial terminal dimensions must be TerminalDimensions or None")
        if self.remote_command is not None and not str(self.remote_command).strip():
            raise ValueError("remote command must be a non-empty string or None")
        if type(self.force_tty) is not bool:
            raise TypeError("force tty must be a boolean")


@dataclass(frozen=True)
class InputOwner:
    client_id: ClientId
    attachment_id: AttachmentId

    def __post_init__(self) -> None:
        require_identifier(self.client_id, "client id")
        require_identifier(self.attachment_id, "attachment id")


@dataclass(frozen=True)
class SessionCapabilities:
    supported: FrozenSet[str] = frozenset()

    def __post_init__(self) -> None:
        if type(self.supported) is not frozenset:
            raise TypeError("session capabilities must be a frozenset")
        for capability in self.supported:
            require_identifier(capability, "session capability")


@dataclass(frozen=True)
class SessionExitInfo:
    exit_code: Optional[int] = None
    signal: Optional[int] = None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.exit_code is not None and type(self.exit_code) is not int:
            raise TypeError("session exit code must be an integer or None")
        if self.exit_code is not None and self.exit_code < 0:
            raise ValueError("session exit code must not be negative")
        if self.signal is not None and (type(self.signal) is not int or self.signal <= 0):
            raise ValueError("session exit signal must be positive or None")
        if self.exit_code is not None and self.signal is not None:
            raise ValueError("session exit code and signal are mutually exclusive")
        if type(self.reason) is not str:
            raise TypeError("session exit reason must be a string")


@dataclass(frozen=True)
class SessionFailure:
    code: str
    message: str

    def __post_init__(self) -> None:
        require_identifier(self.code, "session failure code")
        require_identifier(self.message, "session failure message")


@dataclass(frozen=True)
class SessionSummary:
    id: SessionId
    connection_id: ConnectionId
    state: SessionState
    created_at: datetime = field(default_factory=utc_now)
    input_owner: Optional[InputOwner] = None
    capabilities: SessionCapabilities = field(default_factory=SessionCapabilities)
    exit_info: Optional[SessionExitInfo] = None
    failure: Optional[SessionFailure] = None
    attachment_count: int = 0

    def __post_init__(self) -> None:
        require_identifier(self.id, "session id")
        require_identifier(self.connection_id, "connection id")
        if not isinstance(self.state, SessionState):
            raise TypeError("session state must be a SessionState")
        if type(self.created_at) is not datetime or self.created_at.tzinfo is None:
            raise ValueError("session creation time must be timezone-aware")
        if self.input_owner is not None and type(self.input_owner) is not InputOwner:
            raise TypeError("session input owner must be InputOwner or None")
        if type(self.capabilities) is not SessionCapabilities:
            raise TypeError("session capabilities must be SessionCapabilities")
        if self.exit_info is not None and type(self.exit_info) is not SessionExitInfo:
            raise TypeError("session exit information must be SessionExitInfo or None")
        if self.failure is not None and type(self.failure) is not SessionFailure:
            raise TypeError("session failure must be SessionFailure or None")
        if type(self.attachment_count) is not int or self.attachment_count < 0:
            raise ValueError("session attachment count must not be negative")


@dataclass(frozen=True)
class AttachSessionRequest:
    session_id: SessionId
    request_input: bool = True
    want_terminal_output: bool = False
    from_sequence: int = 0

    def __post_init__(self) -> None:
        require_identifier(self.session_id, "session id")
        if type(self.request_input) is not bool:
            raise TypeError("request input must be a boolean")
        if type(self.want_terminal_output) is not bool:
            raise TypeError("terminal output preference must be a boolean")
        if type(self.from_sequence) is not int or self.from_sequence < 0:
            raise ValueError("terminal replay sequence must not be negative")


@dataclass(frozen=True)
class AttachmentInfo:
    id: AttachmentId
    session_id: SessionId
    client_id: ClientId
    input_owner: bool

    def __post_init__(self) -> None:
        require_identifier(self.id, "attachment id")
        require_identifier(self.session_id, "session id")
        require_identifier(self.client_id, "client id")
        if type(self.input_owner) is not bool:
            raise TypeError("attachment input owner must be a boolean")


@dataclass(frozen=True)
class AttachSessionResult:
    session: SessionSummary
    attachment: AttachmentInfo
    available_start: int = 0
    live_sequence: int = 0
    replay_truncated: bool = False
    eof: bool = False

    def __post_init__(self) -> None:
        if type(self.session) is not SessionSummary:
            raise TypeError("attach result session must be SessionSummary")
        if type(self.attachment) is not AttachmentInfo:
            raise TypeError("attach result attachment must be AttachmentInfo")
        if self.attachment.session_id != self.session.id:
            raise ValueError("attachment and session identifiers must match")
        if (
            type(self.available_start) is not int
            or type(self.live_sequence) is not int
            or self.available_start < 0
            or self.live_sequence < self.available_start
        ):
            raise ValueError("attach result contains invalid terminal bounds")
        if type(self.replay_truncated) is not bool or type(self.eof) is not bool:
            raise TypeError("attach terminal state flags must be booleans")


@dataclass(frozen=True)
class DetachSessionRequest:
    session_id: SessionId
    attachment_id: AttachmentId

    def __post_init__(self) -> None:
        require_identifier(self.session_id, "session id")
        require_identifier(self.attachment_id, "attachment id")


@dataclass(frozen=True)
class CloseSessionRequest:
    session_id: SessionId

    def __post_init__(self) -> None:
        require_identifier(self.session_id, "session id")

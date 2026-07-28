"""Schema-only terminal-session models for protocol v1."""

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


class SessionState(str, Enum):
    CREATING = "creating"
    CONNECTING = "connecting"
    WAITING_FOR_INTERACTION = "waiting_for_interaction"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    DISCONNECTED = "disconnected"
    FAILED = "failed"
    CLOSING = "closing"
    CLOSED = "closed"


@dataclass(frozen=True)
class OpenSessionRequest:
    connection_id: ConnectionId
    client_id: ClientId

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")
        require_identifier(self.client_id, "client id")


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


@dataclass(frozen=True)
class SessionExitInfo:
    exit_code: Optional[int] = None
    signal: Optional[int] = None
    reason: str = ""


@dataclass(frozen=True)
class SessionSummary:
    id: SessionId
    connection_id: ConnectionId
    state: SessionState
    created_at: datetime = field(default_factory=utc_now)
    input_owner: Optional[InputOwner] = None
    capabilities: SessionCapabilities = field(default_factory=SessionCapabilities)
    exit_info: Optional[SessionExitInfo] = None

    def __post_init__(self) -> None:
        require_identifier(self.id, "session id")
        require_identifier(self.connection_id, "connection id")


@dataclass(frozen=True)
class AttachSessionRequest:
    session_id: SessionId
    client_id: ClientId
    request_input: bool = True

    def __post_init__(self) -> None:
        require_identifier(self.session_id, "session id")
        require_identifier(self.client_id, "client id")


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


@dataclass(frozen=True)
class AttachSessionResult:
    session: SessionSummary
    attachment: AttachmentInfo


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

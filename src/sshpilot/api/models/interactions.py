"""Authentication and user-interaction protocol schemas."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Tuple

from .common import (
    ClientId,
    InteractionId,
    RequestId,
    SessionId,
    require_identifier,
    utc_now,
)


class InteractionKind(str, Enum):
    PASSWORD = "password"
    KEY_PASSPHRASE = "key_passphrase"
    HOST_KEY_CONFIRMATION = "host_key_confirmation"
    KEYBOARD_INTERACTIVE = "keyboard_interactive"
    OVERWRITE_CONFIRMATION = "overwrite_confirmation"
    PLUGIN_QUESTION = "plugin_question"


class InteractionStatus(str, Enum):
    PENDING = "pending"
    ANSWERED = "answered"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    REJECTED = "rejected"


@dataclass(frozen=True)
class InteractionRequest:
    id: InteractionId
    request_id: RequestId
    kind: InteractionKind
    message: str
    secret: bool
    allow_empty: bool = False
    choices: Tuple[str, ...] = ()
    session_id: Optional[SessionId] = None
    originating_client_id: Optional[ClientId] = None
    created_at: datetime = field(default_factory=utc_now)
    expires_at: Optional[datetime] = None
    status: InteractionStatus = InteractionStatus.PENDING

    def __post_init__(self) -> None:
        require_identifier(self.id, "interaction id")
        require_identifier(self.request_id, "request id")
        if not self.message:
            raise ValueError("interaction message must not be empty")
        if self.status is not InteractionStatus.PENDING:
            raise ValueError("new interaction requests must be pending")
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("interaction expiry must be after creation")


@dataclass(frozen=True)
class InteractionResponse:
    interaction_id: InteractionId
    status: InteractionStatus
    value: Optional[str] = field(default=None, repr=False)
    choice: Optional[str] = None

    def __post_init__(self) -> None:
        require_identifier(self.interaction_id, "interaction id")
        if self.status is InteractionStatus.PENDING:
            raise ValueError("an interaction response cannot be pending")
        if self.status is not InteractionStatus.ANSWERED and (
            self.value is not None or self.choice is not None
        ):
            raise ValueError("non-answered interactions cannot carry an answer")


@dataclass(frozen=True)
class InteractionCancellation:
    interaction_id: InteractionId
    reason: str = ""


@dataclass(frozen=True)
class InteractionTimeout:
    interaction_id: InteractionId
    expired_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class InteractionRejection:
    interaction_id: InteractionId
    reason: str


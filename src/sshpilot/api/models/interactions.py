"""Authentication and user-interaction protocol schemas."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional, Tuple, Union

from .common import (
    ClientId,
    ConnectionId,
    InteractionId,
    RequestId,
    SessionId,
    require_identifier,
    utc_now,
)


class InteractionType(str, Enum):
    HOST_KEY_CONFIRMATION = "host_key_confirmation"
    PASSWORD = "password"
    PRIVATE_KEY_PASSPHRASE = "private_key_passphrase"
    KEYBOARD_INTERACTIVE = "keyboard_interactive"
    SECURITY_KEY_PRESENCE = "security_key_presence"
    CONFIRMATION = "confirmation"


class ExecutionInteractionMode(str, Enum):
    """Controls whether an OpenSSH operation may publish user prompts."""

    INTERACTIVE = "interactive"
    AUTOFILL_ONLY = "autofill_only"


class InteractionState(str, Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    ANSWERED = "answered"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"


class HostKeyStatus(str, Enum):
    UNKNOWN = "unknown"
    CHANGED = "changed"
    REVOKED = "revoked"


class HostKeyDecision(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"


class SecretDecision(str, Enum):
    SUBMIT = "submit"
    CANCEL = "cancel"


class RememberPolicy(str, Enum):
    DO_NOT_STORE = "do_not_store"
    STORE_AFTER_SUCCESS = "store_after_success"
    REPLACE_STORED_AFTER_SUCCESS = "replace_stored_after_success"
    DELETE_STORED_SECRET = "delete_stored_secret"


class SecretPromptKind(str, Enum):
    """Stable presentation identifiers for daemon-owned secret prompts."""

    BITWARDEN_SIGN_IN = "bitwarden_sign_in"
    BITWARDEN_AUTHENTICATION_CHALLENGE = "bitwarden_authentication_challenge"
    BITWARDEN_TWO_STEP_LOGIN = "bitwarden_two_step_login"
    BITWARDEN_API_KEY = "bitwarden_api_key"
    BITWARDEN_UNLOCK = "bitwarden_unlock"
    KEEPASS_DATABASE_CREATE = "keepass_database_create"
    KEEPASS_UNLOCK = "keepass_unlock"
    REMEMBER_MASTER_PASSWORD = "remember_master_password"
    BACKUP_ENCRYPT = "backup_encrypt"
    BACKUP_DECRYPT = "backup_decrypt"


_SECRET_PROMPT_PARAMETER_KEYS = {
    SecretPromptKind.BITWARDEN_SIGN_IN: frozenset({"email"}),
    SecretPromptKind.BITWARDEN_AUTHENTICATION_CHALLENGE: frozenset(),
    SecretPromptKind.BITWARDEN_TWO_STEP_LOGIN: frozenset({"email"}),
    SecretPromptKind.BITWARDEN_API_KEY: frozenset({"client_id"}),
    SecretPromptKind.BITWARDEN_UNLOCK: frozenset(),
    SecretPromptKind.KEEPASS_DATABASE_CREATE: frozenset(),
    SecretPromptKind.KEEPASS_UNLOCK: frozenset(),
    SecretPromptKind.REMEMBER_MASTER_PASSWORD: frozenset({"name"}),
    SecretPromptKind.BACKUP_ENCRYPT: frozenset(),
    SecretPromptKind.BACKUP_DECRYPT: frozenset(),
}
if set(_SECRET_PROMPT_PARAMETER_KEYS) != set(SecretPromptKind):
    raise RuntimeError("secret prompt parameter keys must cover every SecretPromptKind")


@dataclass(frozen=True)
class HostKeyPrompt:
    hostname: str
    port: int
    key_type: str
    fingerprint: str
    status: HostKeyStatus

    def __post_init__(self) -> None:
        _require_safe_display(self.hostname, "host-key hostname", 255)
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("host-key port is invalid")
        _require_safe_display(self.key_type, "host-key type", 128)
        if (
            type(self.fingerprint) is not str
            or not self.fingerprint.startswith("SHA256:")
            or len(self.fingerprint) > 128
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in self.fingerprint
            )
        ):
            raise ValueError("host-key fingerprint is invalid")
        if not isinstance(self.status, HostKeyStatus):
            raise TypeError("host-key status is invalid")


@dataclass(frozen=True)
class PasswordPrompt:
    username: str
    hostname: str
    port: int
    attempt: int
    can_remember: bool
    stored_secret_available: bool
    secret_prompt_kind: Optional[SecretPromptKind] = None
    secret_prompt_parameters: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.secret_prompt_parameters, Mapping):
            raise TypeError("secret prompt parameters must be a mapping")
        parameters = dict(self.secret_prompt_parameters)
        if self.secret_prompt_kind is None:
            if parameters:
                raise ValueError("ordinary password prompts cannot carry parameters")
            _require_safe_display(self.username, "password username", 255)
            _require_safe_display(self.hostname, "password hostname", 255)
        else:
            if not isinstance(self.secret_prompt_kind, SecretPromptKind):
                raise TypeError("secret prompt kind is invalid")
            if self.username or self.hostname:
                raise ValueError("structured secret prompts cannot carry rendered text")
            expected = _SECRET_PROMPT_PARAMETER_KEYS[self.secret_prompt_kind]
            if set(parameters) != expected:
                raise ValueError("secret prompt parameters do not match the prompt kind")
            for key, value in parameters.items():
                _require_safe_display(value, f"secret prompt parameter {key}", 255)
        object.__setattr__(
            self,
            "secret_prompt_parameters",
            MappingProxyType(parameters),
        )
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("password port is invalid")
        if type(self.attempt) is not int or self.attempt < 1:
            raise ValueError("password attempt must be positive")
        if type(self.can_remember) is not bool:
            raise TypeError("password remember availability must be boolean")
        if type(self.stored_secret_available) is not bool:
            raise TypeError("password stored-secret availability must be boolean")


@dataclass(frozen=True)
class PassphrasePrompt:
    key_display_name: str
    key_fingerprint: Optional[str]
    attempt: int
    can_remember: bool
    stored_secret_available: bool
    confirmation_required: bool = False

    def __post_init__(self) -> None:
        _require_safe_display(self.key_display_name, "key display name", 255)
        if self.key_fingerprint is not None and (
            type(self.key_fingerprint) is not str
            or not self.key_fingerprint.startswith("SHA256:")
            or len(self.key_fingerprint) > 128
        ):
            raise ValueError("key fingerprint is invalid")
        if type(self.attempt) is not int or self.attempt < 1:
            raise ValueError("passphrase attempt must be positive")
        if type(self.can_remember) is not bool:
            raise TypeError("passphrase remember availability must be boolean")
        if type(self.stored_secret_available) is not bool:
            raise TypeError("passphrase stored-secret availability must be boolean")
        if type(self.confirmation_required) is not bool:
            raise TypeError("passphrase confirmation requirement must be boolean")


@dataclass(frozen=True)
class ChallengePrompt:
    text: str
    attempt: int

    def __post_init__(self) -> None:
        _require_safe_display(self.text, "challenge prompt", 4096, multiline=True)
        if type(self.attempt) is not int or self.attempt < 1:
            raise ValueError("challenge attempt must be positive")


@dataclass(frozen=True)
class PresencePrompt:
    text: str

    def __post_init__(self) -> None:
        _require_safe_display(self.text, "presence prompt", 4096)


@dataclass(frozen=True)
class ConfirmationPrompt:
    text: str

    def __post_init__(self) -> None:
        _require_safe_display(self.text, "confirmation prompt", 4096, multiline=True)


InteractionPrompt = Union[
    HostKeyPrompt, PasswordPrompt, PassphrasePrompt, ChallengePrompt, PresencePrompt,
    ConfirmationPrompt,
]


def _require_safe_display(
    value: str, name: str, maximum: int, *, multiline: bool = False
) -> None:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > maximum
        or any(
            (ord(character) < 32 and not (multiline and character in "\n\t"))
            or ord(character) == 127
            for character in value
        )
    ):
        raise ValueError(f"{name} is invalid")


@dataclass(frozen=True)
class InteractionSummary:
    id: InteractionId
    session_id: SessionId
    connection_id: ConnectionId
    type: InteractionType
    state: InteractionState
    created_at: datetime
    expires_at: datetime
    attempt: int
    prompt: InteractionPrompt
    responder_client_id: Optional[ClientId] = None

    def __post_init__(self) -> None:
        require_identifier(self.id, "interaction id")
        require_identifier(self.session_id, "session id")
        require_identifier(self.connection_id, "connection id")
        if not isinstance(self.type, InteractionType):
            raise TypeError("interaction type is invalid")
        if not isinstance(self.state, InteractionState):
            raise TypeError("interaction state is invalid")
        if type(self.created_at) is not datetime or type(self.expires_at) is not datetime:
            raise TypeError("interaction timestamps must be datetimes")
        if self.created_at.utcoffset() is None or self.expires_at.utcoffset() is None:
            raise ValueError("interaction timestamps must be timezone-aware")
        if self.expires_at <= self.created_at:
            raise ValueError("interaction expiry must follow creation")
        if type(self.attempt) is not int or self.attempt < 1:
            raise ValueError("interaction attempt must be positive")
        expected_prompt = {
            InteractionType.HOST_KEY_CONFIRMATION: HostKeyPrompt,
            InteractionType.PASSWORD: PasswordPrompt,
            InteractionType.PRIVATE_KEY_PASSPHRASE: PassphrasePrompt,
            InteractionType.KEYBOARD_INTERACTIVE: ChallengePrompt,
            InteractionType.SECURITY_KEY_PRESENCE: PresencePrompt,
            InteractionType.CONFIRMATION: ConfirmationPrompt,
        }[self.type]
        if type(self.prompt) is not expected_prompt:
            raise TypeError("interaction prompt does not match its type")
        if isinstance(self.prompt, (PasswordPrompt, PassphrasePrompt, ChallengePrompt)):
            if self.prompt.attempt != self.attempt:
                raise ValueError("interaction prompt attempt is inconsistent")
        if self.state is InteractionState.CLAIMED:
            if self.responder_client_id is None:
                raise ValueError("claimed interaction requires a responder")
            require_identifier(self.responder_client_id, "responder client id")
        elif self.state is InteractionState.PENDING and self.responder_client_id is not None:
            raise ValueError("pending interaction cannot have a responder")


@dataclass(frozen=True)
class InteractionClaim:
    interaction_id: InteractionId
    responder_client_id: ClientId
    nonce: str = field(repr=False)
    expires_at: datetime

    def __post_init__(self) -> None:
        require_identifier(self.interaction_id, "interaction id")
        require_identifier(self.responder_client_id, "responder client id")
        if type(self.expires_at) is not datetime or self.expires_at.utcoffset() is None:
            raise ValueError("interaction claim expiry must be timezone-aware")
        if len(self.nonce) != 32:
            raise ValueError("interaction claim nonce is malformed")
        try:
            bytes.fromhex(self.nonce)
        except ValueError:
            raise ValueError("interaction claim nonce is malformed") from None


@dataclass(frozen=True)
class InteractionDecisionRequest:
    interaction_id: InteractionId
    host_key_decision: Optional[HostKeyDecision] = None
    secret_decision: Optional[SecretDecision] = None
    remember_policy: RememberPolicy = RememberPolicy.DO_NOT_STORE

    def __post_init__(self) -> None:
        require_identifier(self.interaction_id, "interaction id")
        if (self.host_key_decision is None) == (self.secret_decision is None):
            raise ValueError("exactly one typed interaction decision is required")
        if (
            self.host_key_decision is not None
            and not isinstance(self.host_key_decision, HostKeyDecision)
        ):
            raise TypeError("host-key decision is invalid")
        if (
            self.secret_decision is not None
            and not isinstance(self.secret_decision, SecretDecision)
        ):
            raise TypeError("secret decision is invalid")
        if not isinstance(self.remember_policy, RememberPolicy):
            raise TypeError("remember policy is invalid")


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

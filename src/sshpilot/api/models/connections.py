"""Frontend-neutral connection request and response models."""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from .common import ConnectionId, require_identifier


class ConnectionHealth(str, Enum):
    """Persistent host availability, distinct from terminal session state."""

    UNKNOWN = "unknown"
    CHECKING = "checking"
    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"


class AuthenticationMethod(str, Enum):
    KEY = "key"
    PASSWORD = "password"


@dataclass(frozen=True)
class GroupReference:
    id: str
    name: str = ""

    def __post_init__(self) -> None:
        require_identifier(self.id, "group id")


@dataclass(frozen=True)
class ConnectionSummary:
    """The deliberate, secret-free connection shape used by list views."""

    id: ConnectionId
    nickname: str
    host: str
    hostname: str
    username: str
    port: int
    protocol: str = "ssh"
    health: ConnectionHealth = ConnectionHealth.UNKNOWN
    groups: Tuple[GroupReference, ...] = ()

    def __post_init__(self) -> None:
        require_identifier(self.id, "connection id")
        if not self.nickname.strip():
            raise ValueError("connection nickname must not be empty")
        if not 1 <= self.port <= 65535:
            raise ValueError("connection port must be between 1 and 65535")
        if not self.protocol.strip():
            raise ValueError("connection protocol must not be empty")

    @property
    def display_target(self) -> str:
        host = self.hostname or self.host or self.nickname
        return f"{self.username}@{host}" if self.username else host


@dataclass(frozen=True)
class ConnectionDetails(ConnectionSummary):
    """Full v1 connection response without secret values or sensitive paths."""

    aliases: Tuple[str, ...] = ()
    authentication_method: AuthenticationMethod = AuthenticationMethod.KEY
    identity_configured: bool = False
    certificate_configured: bool = False
    x11_forwarding: bool = False
    forwarding_rule_count: int = 0
    proxy_jump: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.forwarding_rule_count < 0:
            raise ValueError("forwarding rule count must not be negative")


@dataclass(frozen=True)
class CreateConnectionRequest:
    nickname: str
    hostname: str
    username: str = ""
    port: int = 22
    protocol: str = "ssh"

    def __post_init__(self) -> None:
        if type(self.nickname) is not str or not self.nickname.strip():
            raise ValueError("connection nickname must not be empty")
        if type(self.hostname) is not str or not self.hostname.strip():
            raise ValueError("connection hostname must not be empty")
        if type(self.username) is not str:
            raise TypeError("connection username must be a string")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("connection port must be between 1 and 65535")
        if type(self.protocol) is not str or not self.protocol.strip():
            raise ValueError("connection protocol must not be empty")


@dataclass(frozen=True)
class UpdateConnectionRequest:
    nickname: Optional[str] = None
    hostname: Optional[str] = None
    username: Optional[str] = None
    port: Optional[int] = None

    def __post_init__(self) -> None:
        if all(
            value is None
            for value in (self.nickname, self.hostname, self.username, self.port)
        ):
            raise ValueError("connection update must contain at least one field")
        if self.nickname is not None and (
            type(self.nickname) is not str or not self.nickname.strip()
        ):
            raise ValueError("connection nickname must not be empty")
        if self.hostname is not None and (
            type(self.hostname) is not str or not self.hostname.strip()
        ):
            raise ValueError("connection hostname must not be empty")
        if self.username is not None and type(self.username) is not str:
            raise TypeError("connection username must be a string")
        if self.port is not None and (
            type(self.port) is not int or not 1 <= self.port <= 65535
        ):
            raise ValueError("connection port must be between 1 and 65535")


@dataclass(frozen=True)
class DeleteConnectionRequest:
    connection_id: ConnectionId

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")


@dataclass(frozen=True)
class DeleteConnectionResult:
    connection_id: ConnectionId
    deleted: bool

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")
        if type(self.deleted) is not bool:
            raise TypeError("connection deleted result must be a boolean")


@dataclass(frozen=True)
class ConnectionValidationError:
    field: str
    code: str
    message: str


@dataclass(frozen=True)
class ConnectionValidationResult:
    valid: bool
    errors: Tuple[ConnectionValidationError, ...] = ()

    def __post_init__(self) -> None:
        if self.valid and self.errors:
            raise ValueError("a valid result cannot contain validation errors")

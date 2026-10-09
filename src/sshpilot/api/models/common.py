"""Common frontend-neutral protocol value objects."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import NewType, Optional


ConnectionId = NewType("ConnectionId", str)
SessionId = NewType("SessionId", str)
RequestId = NewType("RequestId", str)
InteractionId = NewType("InteractionId", str)
TransferId = NewType("TransferId", str)
SftpServiceId = NewType("SftpServiceId", str)
ForwardId = NewType("ForwardId", str)
ClientId = NewType("ClientId", str)
AttachmentId = NewType("AttachmentId", str)


def utc_now() -> datetime:
    """Return an aware UTC timestamp for protocol records."""

    return datetime.now(timezone.utc)


def require_identifier(value: str, field_name: str) -> str:
    """Validate a public opaque identifier without interpreting its contents."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def has_control_characters(value: str, *, allow_newlines: bool = False) -> bool:
    """True when *value* holds a C0 control character or DEL (tab is allowed).

    A line break in a value that lands on one ssh_config line starts a new
    directive -- ``HostName x\\n  ProxyCommand ...`` runs a local command --
    so single-line fields must never carry one.
    """
    for char in value:
        code = ord(char)
        if char == "\t" or (allow_newlines and char == "\n"):
            continue
        if code < 0x20 or code == 0x7F:
            return True
    return False


def validate_single_line(value: str, field_name: str) -> str:
    """Reject line breaks and other control characters in a one-line field."""
    if isinstance(value, str) and has_control_characters(value):
        raise ValueError(f"{field_name} must not contain line breaks or control characters")
    return value


def validate_ssh_host_alias(value: str, field_name: str = "connection nickname") -> str:
    """Validate an SSH ``Host`` token at every authoritative boundary."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    normalized = value.strip()
    if normalized.startswith("-"):
        raise ValueError(f"{field_name} must not begin with '-' because OpenSSH may parse it as an option")
    if "\x00" in normalized:
        raise ValueError(f"{field_name} must not contain NUL")
    validate_single_line(normalized, field_name)
    return normalized


@dataclass(frozen=True)
class ClientInfo:
    """Describes the frontend/client implementation."""

    name: str
    version: str
    client_id: Optional[ClientId] = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("client name must not be empty")
        if not self.version.strip():
            raise ValueError("client version must not be empty")


@dataclass(frozen=True)
class CoreInfo:
    """Describes the core implementation reached by a client."""

    name: str
    version: str
    implementation: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("core name must not be empty")
        if not self.version.strip():
            raise ValueError("core version must not be empty")
        if not self.implementation.strip():
            raise ValueError("core implementation must not be empty")


@dataclass(frozen=True)
class CompatibilityResult:
    """Compatibility result for one client/core protocol pairing."""

    compatible: bool
    protocol_version: str
    message: str = ""

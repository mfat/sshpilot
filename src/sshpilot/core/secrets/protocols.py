"""Secret backend protocols (GTK-free; no libsecret / GI import)."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Protocol, runtime_checkable


class SecretCapability(str, Enum):
    STORE = "store"
    LOOKUP = "lookup"
    DELETE = "delete"
    SESSION = "session"
    LIST = "list"


@dataclass(frozen=True)
class SecretRef:
    """Normalized secret reference — never carries the secret value."""

    account: str
    service: str = "sshpilot"
    kind: str = "password"  # password | passphrase | sudo | master


@dataclass(frozen=True)
class BackendInfo:
    name: str
    available: bool
    session_backed: bool = False
    capabilities: frozenset[SecretCapability] = frozenset(
        {SecretCapability.STORE, SecretCapability.LOOKUP, SecretCapability.DELETE}
    )
    label: str = ""


@runtime_checkable
class SecretBackendProtocol(Protocol):
    """Platform backends implement this; core never imports concrete OS APIs."""

    name: str
    session_backed: bool

    def is_available(self) -> bool: ...

    def is_unlocked(self) -> bool: ...

    def store(self, ref: SecretRef, secret: str) -> bool: ...

    def lookup(self, ref: SecretRef) -> Optional[str]: ...

    def delete(self, ref: SecretRef) -> bool: ...


@dataclass(frozen=True)
class FallbackDecision:
    """Explicit, observable fallback — never silent."""

    from_backend: str
    to_backend: str
    reason: str

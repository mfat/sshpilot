"""Immutable API models for daemon-owned secret-backend management.

These are **metadata and management** DTOs.  They never carry secret values:
no passwords, master passwords, session tokens, two-factor codes, API client
secrets, auth-challenge secrets, transformed keys, or decrypted data.  A
backend's locked/unlocked state is reported as a boolean, never as a token.

The canonical field model (field→config-key mapping, valid ranges, canonical
defaults, strict normalization, revision) is owned by
``sshpilot.core.secrets.management`` so the API values, the persisted
``secrets.*`` config keys, and the revision token always agree.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Tuple

from sshpilot.core.secrets.management import (
    EDITABLE_FIELDS,
    INTEGER_RANGES,
    VALID_BACKEND_VALUES,
)

# Re-exported field-model contracts (consumed by the daemon service).
EDITABLE_FIELDS = EDITABLE_FIELDS
_VALID_BACKEND_VALUES = VALID_BACKEND_VALUES


# ---------------------------------------------------------------------------
# Error codes narrowly scoped to this module.
# ---------------------------------------------------------------------------

REVISION_CONFLICT = "revision_conflict"
SETTINGS_MALFORMED = "settings_malformed"
SETTINGS_PERSISTENCE_FAILED = "settings_persistence_failed"
BACKEND_UNAVAILABLE = "backend_unavailable"
INTERACTION_REQUIRED = "interaction_required"
INTERACTION_CANCELLED = "interaction_cancelled"
LOGIN_REQUIRED = "login_required"


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_boolean(value: Any, field: str) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{field} must be a boolean, got {type(value).__name__}")


def _validate_integer(value: Any, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer, got {type(value).__name__}")
    lo, hi = INTEGER_RANGES[field]
    if not (lo <= value <= hi):
        raise ValueError(f"{field} must be between {lo} and {hi}, got {value}")


def _validate_text(value: Any, field: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string, got {type(value).__name__}")


def _validate_patch_fields(patch: Mapping[str, Any]) -> None:
    """Reject unknown patch fields and type-check values."""
    unknown = set(patch.keys()) - EDITABLE_FIELDS
    if unknown:
        raise ValueError(f"unknown patch fields: {sorted(unknown)}")
    for key, value in patch.items():
        if key == "remember_in_keyring":
            _validate_boolean(value, key)
        elif key in INTEGER_RANGES:
            _validate_integer(value, key)
        elif key == "backend":
            if not isinstance(value, str) or value not in _VALID_BACKEND_VALUES:
                raise ValueError(
                    f"backend must be one of {sorted(_VALID_BACKEND_VALUES)}, "
                    f"got {value!r}"
                )
        else:
            _validate_text(value, key)


# ---------------------------------------------------------------------------
# Public DTOs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SecretConfiguration:
    """Immutable snapshot of daemon-owned ``secrets.*`` configuration.

    Paths and profile names only — never secret values.
    """

    revision: str
    backend: str
    session_timeout: int
    remember_in_keyring: bool
    bitwarden_profile: str
    bitwarden_server: str
    keepassxc_database: str
    keepassxc_keyfile: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "revision": self.revision,
            "backend": self.backend,
            "session_timeout": self.session_timeout,
            "remember_in_keyring": self.remember_in_keyring,
            "bitwarden_profile": self.bitwarden_profile,
            "bitwarden_server": self.bitwarden_server,
            "keepassxc_database": self.keepassxc_database,
            "keepassxc_keyfile": self.keepassxc_keyfile,
        }

    def __post_init__(self) -> None:
        if not isinstance(self.revision, str) or not self.revision.strip():
            raise ValueError("revision must be a non-empty string")
        _validate_integer(self.session_timeout, "session_timeout")
        _validate_boolean(self.remember_in_keyring, "remember_in_keyring")
        if not isinstance(self.backend, str) or self.backend not in _VALID_BACKEND_VALUES:
            raise ValueError(
                f"backend must be one of {sorted(_VALID_BACKEND_VALUES)}, "
                f"got {self.backend!r}"
            )
        _validate_text(self.bitwarden_profile, "bitwarden_profile")
        _validate_text(self.bitwarden_server, "bitwarden_server")
        _validate_text(self.keepassxc_database, "keepassxc_database")
        _validate_text(self.keepassxc_keyfile, "keepassxc_keyfile")


@dataclass(frozen=True)
class UpdateSecretConfigurationRequest:
    """A partial update to daemon-owned secret configuration.

    ``patch`` contains only the fields to change.  ``expected_revision`` enables
    optimistic concurrency control: the service rejects the update if the
    current revision does not match.
    """

    patch: Mapping[str, object]
    expected_revision: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.patch, Mapping):
            raise TypeError("patch must be a mapping")
        if self.patch:
            _validate_patch_fields(self.patch)
        if self.expected_revision is not None and (
            not isinstance(self.expected_revision, str)
            or not self.expected_revision.strip()
        ):
            raise ValueError("expected_revision must be a non-empty string or None")
        object.__setattr__(self, "patch", MappingProxyType(dict(self.patch)))


@dataclass(frozen=True)
class SecretBackendDescriptor:
    """Metadata describing one registered secret backend.  No secret values."""

    name: str
    label: str
    available: bool
    selected: bool
    session_backed: bool
    locked: bool
    needs_unlock: bool
    login_required: bool
    persists_secrets: bool
    capabilities: Tuple[str, ...]
    diagnostic: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "available": self.available,
            "selected": self.selected,
            "session_backed": self.session_backed,
            "locked": self.locked,
            "needs_unlock": self.needs_unlock,
            "login_required": self.login_required,
            "persists_secrets": self.persists_secrets,
            "capabilities": list(self.capabilities),
            "diagnostic": self.diagnostic,
        }

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("backend name must be a non-empty string")
        _validate_text(self.label, "label")
        _validate_boolean(self.available, "available")
        _validate_boolean(self.selected, "selected")
        _validate_boolean(self.session_backed, "session_backed")
        _validate_boolean(self.locked, "locked")
        _validate_boolean(self.needs_unlock, "needs_unlock")
        _validate_boolean(self.login_required, "login_required")
        _validate_boolean(self.persists_secrets, "persists_secrets")
        _validate_text(self.diagnostic, "diagnostic")
        object.__setattr__(
            self, "capabilities", tuple(self.capabilities)
        )


@dataclass(frozen=True)
class SecretBackendRegistry:
    """The full backend registry plus the effective/selected state."""

    backends: Tuple[SecretBackendDescriptor, ...]
    effective_backend: str
    selected_backend: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "backends": [b.to_dict() for b in self.backends],
            "effective_backend": self.effective_backend,
            "selected_backend": self.selected_backend,
        }

    def __post_init__(self) -> None:
        if not isinstance(self.effective_backend, str) or not self.effective_backend:
            raise ValueError("effective_backend must be a non-empty string")
        if not isinstance(self.selected_backend, str) or not self.selected_backend:
            raise ValueError("selected_backend must be a non-empty string")
        object.__setattr__(self, "backends", tuple(self.backends))
        for backend in self.backends:
            if type(backend) is not SecretBackendDescriptor:
                raise TypeError("backends must be SecretBackendDescriptor values")


@dataclass(frozen=True)
class SecretBackendState:
    """Runtime state of the selected secret backend.  No secret values."""

    selected_backend: str
    effective_backend: str
    locked: bool
    needs_unlock: bool
    login_required: bool
    session_timeout: int
    remember_in_keyring: bool
    persists_secrets: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "selected_backend": self.selected_backend,
            "effective_backend": self.effective_backend,
            "locked": self.locked,
            "needs_unlock": self.needs_unlock,
            "login_required": self.login_required,
            "session_timeout": self.session_timeout,
            "remember_in_keyring": self.remember_in_keyring,
            "persists_secrets": self.persists_secrets,
        }

    def __post_init__(self) -> None:
        if not isinstance(self.selected_backend, str) or not self.selected_backend:
            raise ValueError("selected_backend must be a non-empty string")
        if not isinstance(self.effective_backend, str) or not self.effective_backend:
            raise ValueError("effective_backend must be a non-empty string")
        for field in ("locked", "needs_unlock", "login_required",
                      "remember_in_keyring", "persists_secrets"):
            _validate_boolean(getattr(self, field), field)
        _validate_integer(self.session_timeout, "session_timeout")


class UnlockResultKind(str, Enum):
    UNLOCKED = "unlocked"
    INTERACTION_REQUIRED = "interaction_required"
    LOGIN_REQUIRED = "login_required"
    BACKEND_UNAVAILABLE = "backend_unavailable"


@dataclass(frozen=True)
class SecretUnlockResult:
    """Outcome of a secret-backend unlock request.  Never carries a secret."""

    kind: UnlockResultKind
    backend: str
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.value,
            "backend": self.backend,
            "message": self.message,
        }

    def __post_init__(self) -> None:
        if not isinstance(self.kind, UnlockResultKind):
            raise TypeError("kind must be an UnlockResultKind")
        _validate_text(self.backend, "backend")
        _validate_text(self.message, "message")


class SecretOperationState(str, Enum):
    SUCCESS = "success"
    LOGIN_REQUIRED = "login_required"
    INTERACTION_REQUIRED = "interaction_required"
    FAILED = "failed"


@dataclass(frozen=True)
class SecretOperationResult:
    """Safe outcome of a backend lifecycle operation.  No secret values."""

    state: SecretOperationState
    backend: str
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state.value,
            "backend": self.backend,
            "message": self.message,
        }

    def __post_init__(self) -> None:
        if not isinstance(self.state, SecretOperationState):
            raise TypeError("state must be a SecretOperationState")
        _validate_text(self.backend, "backend")
        _validate_text(self.message, "message")


@dataclass(frozen=True)
class BitwardenStatus:
    """Safe Bitwarden account/lifecycle status."""

    logged_in: bool
    unlocked: bool
    needs_login: bool
    email: str
    server_url: str
    profile: str
    twofa_required: bool = False
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "logged_in": self.logged_in,
            "unlocked": self.unlocked,
            "needs_login": self.needs_login,
            "email": self.email,
            "server_url": self.server_url,
            "profile": self.profile,
            "twofa_required": self.twofa_required,
            "message": self.message,
        }

    def __post_init__(self) -> None:
        for field in ("logged_in", "unlocked", "needs_login", "twofa_required"):
            _validate_boolean(getattr(self, field), field)
        _validate_text(self.email, "email")
        _validate_text(self.server_url, "server_url")
        _validate_text(self.profile, "profile")
        _validate_text(self.message, "message")


@dataclass(frozen=True)
class RbwStatus:
    """Safe rbw account/lifecycle status."""

    installed: bool
    configured: bool
    unlocked: bool
    email: str
    base_url: str
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "installed": self.installed,
            "configured": self.configured,
            "unlocked": self.unlocked,
            "email": self.email,
            "base_url": self.base_url,
            "message": self.message,
        }

    def __post_init__(self) -> None:
        for field in ("installed", "configured", "unlocked"):
            _validate_boolean(getattr(self, field), field)
        _validate_text(self.email, "email")
        _validate_text(self.base_url, "base_url")
        _validate_text(self.message, "message")


@dataclass(frozen=True)
class SecretTransferResult:
    """Safe outcome of a daemon-owned secret export/import.

    Contains only paths, counts and warnings — never secret values or
    credential records.
    """

    operation: str  # "export" | "import"
    path: str
    counts: Mapping[str, int]
    warnings: Tuple[str, ...]
    status: SecretOperationState
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "operation": self.operation,
            "path": self.path,
            "counts": dict(self.counts),
            "warnings": list(self.warnings),
            "status": self.status.value,
            "message": self.message,
        }

    def __post_init__(self) -> None:
        if self.operation not in ("export", "import"):
            raise ValueError("operation must be 'export' or 'import'")
        _validate_text(self.path, "path")
        if not isinstance(self.counts, Mapping):
            raise TypeError("counts must be a mapping")
        object.__setattr__(self, "counts", MappingProxyType(dict(self.counts)))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        if not isinstance(self.status, SecretOperationState):
            raise TypeError("status must be a SecretOperationState")
        _validate_text(self.message, "message")

"""Immutable API models for daemon-owned SSH identity management.

The canonical field model (field→config-key mapping, defaults, strict
normalization, revision) is owned by ``sshpilot.core.settings.identity`` so
the API values, the persisted config keys, and the revision token always
agree.  This module keeps the API-level validation and public DTOs.

Provider descriptors expose safe metadata only: no agent protocol data, no
private-key material, and no sensitive environment contents cross the API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple

from sshpilot.core.settings.identity import (
    AUTO_PROVIDER,
    CUSTOM_PROVIDER,
    EDITABLE_FIELDS,
    normalize_custom_socket,
    normalize_provider,
)

from .common import ConnectionId, require_identifier
from .keys import KeyId, KeyStoreScope

# Re-exported field-model contract (consumed by the daemon service).
EDITABLE_FIELDS = EDITABLE_FIELDS
AUTO_PROVIDER = AUTO_PROVIDER
CUSTOM_PROVIDER = CUSTOM_PROVIDER

# ---------------------------------------------------------------------------
# Error codes narrowly scoped to this module.
# ---------------------------------------------------------------------------

REVISION_CONFLICT = "revision_conflict"
SETTINGS_MALFORMED = "settings_malformed"
SETTINGS_PERSISTENCE_FAILED = "settings_persistence_failed"
AGENT_UNAVAILABLE = "agent_unavailable"
SSH_COPY_ID_UNAVAILABLE = "ssh_copy_id_unavailable"
AUTHORIZED_KEYS_STALE = "authorized_keys_stale"
AUTHORIZED_KEYS_LINE_NOT_FOUND = "authorized_keys_line_not_found"

# ---------------------------------------------------------------------------
# Provider registry and selection state
# ---------------------------------------------------------------------------


def _validate_revision(value: object, field_name: str = "revision") -> None:
    if not isinstance(value, str):
        raise TypeError(
            f"{field_name} must be a string, got {type(value).__name__}"
        )
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")


def _validate_safe_text(value: object, field_name: str, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise TypeError(
            f"{field_name} must be a string, got {type(value).__name__}"
        )
    if "\x00" in value:
        raise ValueError(f"{field_name} must not contain NUL")
    if not allow_empty and not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


@dataclass(frozen=True)
class IdentityProviderDescriptor:
    """Safe metadata describing one selectable identity provider."""

    provider_id: str
    label: str
    available: bool
    selected: bool
    effective_agent_socket: str
    custom_socket_required: bool
    capabilities: Tuple[str, ...]
    detail: str = ""

    def __post_init__(self) -> None:
        _validate_safe_text(self.provider_id, "provider id", allow_empty=False)
        _validate_safe_text(self.label, "provider label", allow_empty=False)
        for flag_name in ("available", "selected", "custom_socket_required"):
            if type(getattr(self, flag_name)) is not bool:
                raise TypeError(f"{flag_name} must be a boolean")
        _validate_safe_text(
            self.effective_agent_socket,
            "effective agent socket",
            allow_empty=True,
        )
        if type(self.capabilities) is not tuple:
            raise TypeError("provider capabilities must be a tuple")
        for capability in self.capabilities:
            _validate_safe_text(capability, "provider capability", allow_empty=False)
        _validate_safe_text(self.detail, "provider detail", allow_empty=True)


@dataclass(frozen=True)
class IdentityProviderRegistry:
    """Ordered provider descriptors plus the selection revision."""

    providers: Tuple[IdentityProviderDescriptor, ...]
    revision: str

    def __post_init__(self) -> None:
        if type(self.providers) is not tuple:
            raise TypeError("providers must be a tuple")
        for provider in self.providers:
            if type(provider) is not IdentityProviderDescriptor:
                raise TypeError("providers must be IdentityProviderDescriptor")
        ids = [provider.provider_id for provider in self.providers]
        if len(set(ids)) != len(ids):
            raise ValueError("providers must not contain duplicate ids")
        _validate_revision(self.revision)


@dataclass(frozen=True)
class IdentityState:
    """Daemon-owned identity selection state (metadata only)."""

    provider: str
    custom_socket: str
    effective_agent_socket: str
    agent_available: bool
    ssh_copy_id_available: bool
    revision: str

    def __post_init__(self) -> None:
        _validate_safe_text(self.provider, "provider", allow_empty=False)
        _validate_safe_text(self.custom_socket, "custom socket", allow_empty=True)
        _validate_safe_text(
            self.effective_agent_socket,
            "effective agent socket",
            allow_empty=True,
        )
        if type(self.agent_available) is not bool:
            raise TypeError("agent_available must be a boolean")
        if type(self.ssh_copy_id_available) is not bool:
            raise TypeError("ssh_copy_id_available must be a boolean")
        _validate_revision(self.revision)


@dataclass(frozen=True)
class UpdateIdentitySelectionRequest:
    """A revision-checked identity provider selection update."""

    provider: str
    expected_revision: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", normalize_provider(self.provider))
        if self.expected_revision is not None:
            _validate_revision(self.expected_revision, "expected_revision")


@dataclass(frozen=True)
class UpdateIdentityConfigurationRequest:
    """A revision-checked custom agent socket configuration update."""

    custom_socket: str
    expected_revision: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "custom_socket", normalize_custom_socket(self.custom_socket)
        )
        if self.expected_revision is not None:
            _validate_revision(self.expected_revision, "expected_revision")


# ---------------------------------------------------------------------------
# Agent keys (via the native ``ssh-add`` utility)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentKey:
    """One key currently loaded in the selected agent (metadata only)."""

    fingerprint: str
    comment: str
    key_type: str

    def __post_init__(self) -> None:
        _validate_safe_text(self.fingerprint, "agent key fingerprint", allow_empty=False)
        _validate_safe_text(self.comment, "agent key comment", allow_empty=True)
        _validate_safe_text(self.key_type, "agent key type", allow_empty=True)


@dataclass(frozen=True)
class AgentKeyList:
    """The keys currently loaded in the selected agent."""

    keys: Tuple[AgentKey, ...]

    def __post_init__(self) -> None:
        if type(self.keys) is not tuple:
            raise TypeError("agent key list must be a tuple")
        for key in self.keys:
            if type(key) is not AgentKey:
                raise TypeError("agent key list entries must be AgentKey")


@dataclass(frozen=True)
class ListProviderAgentKeysRequest:
    """List the keys loaded in one named identity provider's agent.

    Read-only, scoped to an explicit provider id (``'auto'`` = the system
    ssh-agent inherited by the daemon) so a plugin may observe a specific
    provider regardless of which provider is currently selected.
    """

    provider_id: str

    def __post_init__(self) -> None:
        _validate_safe_text(self.provider_id, "provider id", allow_empty=False)


@dataclass(frozen=True)
class AgentKeyMutationRequest:
    """Add or remove one daemon-known key in the selected agent.

    The frontend supplies an opaque daemon ``KeyId`` plus the semantic store
    scope — never a filesystem path; the daemon resolves the key itself.
    """

    key_id: KeyId
    scope: KeyStoreScope = KeyStoreScope.DEFAULT

    def __post_init__(self) -> None:
        require_identifier(self.key_id, "key id")
        if type(self.scope) is not KeyStoreScope:
            raise TypeError("key store scope must be a KeyStoreScope")


# ---------------------------------------------------------------------------
# Public-key deployment (via the native ``ssh-copy-id`` utility)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeployKeyRequest:
    """Deploy one daemon-known public key to a saved connection.

    The connection is identified by its SSH ``Host`` alias so OpenSSH
    evaluates the user's normal configuration; the key is an opaque daemon
    ``KeyId`` resolved by the daemon's key store.
    """

    connection_id: ConnectionId
    key_id: KeyId
    scope: KeyStoreScope = KeyStoreScope.DEFAULT
    force: bool = False

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")
        require_identifier(self.key_id, "key id")
        if type(self.scope) is not KeyStoreScope:
            raise TypeError("key store scope must be a KeyStoreScope")
        if type(self.force) is not bool:
            raise TypeError("force must be a boolean")


# ---------------------------------------------------------------------------
# Remote authorized-keys management
# ---------------------------------------------------------------------------


class AuthorizedKeyLineKind(str, Enum):
    KEY = "key"
    COMMENT = "comment"
    BLANK = "blank"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AuthorizedKeyEntry:
    """One line of a remote ``authorized_keys`` file.

    ``line_id`` is a stable opaque digest of the exact original line; removal
    addresses lines exclusively through it.  Unknown and malformed lines stay
    representable (``kind`` = ``UNKNOWN``) and are never silently dropped.
    """

    line_id: str
    kind: AuthorizedKeyLineKind
    raw_line: str = field(repr=False)
    key_type: str = ""
    fingerprint: str = ""
    comment: str = ""
    disabled: bool = False

    def __post_init__(self) -> None:
        _validate_safe_text(self.line_id, "authorized key line id", allow_empty=False)
        if not isinstance(self.kind, AuthorizedKeyLineKind):
            raise TypeError("authorized key kind must be an AuthorizedKeyLineKind")
        if "\x00" in self.raw_line or "\n" in self.raw_line or "\r" in self.raw_line:
            raise ValueError("authorized key raw line must be a single line")
        for field_name in ("key_type", "fingerprint", "comment"):
            _validate_safe_text(getattr(self, field_name), field_name, allow_empty=True)
        if type(self.disabled) is not bool:
            raise TypeError("disabled must be a boolean")


@dataclass(frozen=True)
class AuthorizedKeyList:
    """A remote ``authorized_keys`` listing with a whole-file revision."""

    connection_id: ConnectionId
    entries: Tuple[AuthorizedKeyEntry, ...]
    file_revision: str

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")
        if type(self.entries) is not tuple:
            raise TypeError("authorized key entries must be a tuple")
        for entry in self.entries:
            if type(entry) is not AuthorizedKeyEntry:
                raise TypeError("authorized key entries must be AuthorizedKeyEntry")
        _validate_revision(self.file_revision, "file_revision")


@dataclass(frozen=True)
class ListAuthorizedKeysRequest:
    """List the remote ``authorized_keys`` of one saved connection."""

    connection_id: ConnectionId

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")


@dataclass(frozen=True)
class RemoveAuthorizedKeyRequest:
    """Remove exactly one remote ``authorized_keys`` line.

    ``file_revision`` must match the revision returned by the listing the
    ``line_id`` came from; the mutation is rejected when the remote file
    changed in between.
    """

    connection_id: ConnectionId
    line_id: str
    file_revision: str

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")
        _validate_safe_text(self.line_id, "authorized key line id", allow_empty=False)
        _validate_revision(self.file_revision, "file_revision")

"""Immutable API models for daemon-owned global SSH overrides.

The canonical field model (field→config-key mapping, valid ranges, host-key
enum, canonical defaults, strict normalization, revision) is owned by
``sshpilot.core.settings.ssh_overrides`` so the API values, the persisted
config keys, and the revision token always agree.  This module keeps the
API-level validation and public DTOs.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Tuple

from sshpilot.core.settings.ssh_overrides import (
    EDITABLE_FIELDS,
    FIELD_TO_CONFIG_KEY,
    INTEGER_RANGES,
    VALID_HOST_KEY_VALUES,
    compute_ssh_overrides_revision,
)

# Re-exported field-model contracts (consumed by the daemon service).
# ``EDITABLE_FIELDS`` is public API; the underscore-prefixed names are kept for
# internal compatibility with the codec and should not be part of the public
# surface.
EDITABLE_FIELDS = EDITABLE_FIELDS
_FIELD_TO_CONFIG_KEY: Dict[str, str] = dict(FIELD_TO_CONFIG_KEY)
_INTEGER_RANGES: Dict[str, tuple[int, int]] = dict(INTEGER_RANGES)
_VALID_HK_VALUES = VALID_HOST_KEY_VALUES


def _compute_revision(data: Dict[str, Any]) -> str:
    """Deterministic revision from the semantic fields only.

    The revision is a hex SHA-256 prefix (12 chars) of the canonical JSON
    representation of the semantic fields.
    """
    return compute_ssh_overrides_revision(data)


# ---------------------------------------------------------------------------
# Error codes narrowly scoped to this module.
# ---------------------------------------------------------------------------

REVISION_CONFLICT = "revision_conflict"
SETTINGS_MALFORMED = "settings_malformed"
SETTINGS_PERSISTENCE_FAILED = "settings_persistence_failed"


def _validate_boolean(value: Any, field: str) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{field} must be a boolean, got {type(value).__name__}")


def _validate_revision(value: Any) -> None:
    if not isinstance(value, str):
        raise TypeError(f"revision must be a string, got {type(value).__name__}")
    if not value.strip():
        raise ValueError("revision must not be empty")


def _validate_integer(value: Any, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer, got {type(value).__name__}")
    lo, hi = _INTEGER_RANGES[field]
    if not (lo <= value <= hi):
        raise ValueError(
            f"{field} must be between {lo} and {hi}, got {value}"
        )


def _validate_strict_host_key(value: Any) -> None:
    if not isinstance(value, str) or value not in _VALID_HK_VALUES:
        raise ValueError(
            f"strict_host_key_checking must be one of "
            f"{sorted(_VALID_HK_VALUES)}, got {value!r}"
        )


def _validate_patch_fields(patch: Mapping[str, Any]) -> None:
    """Reject unknown patch fields and type-check values."""
    unknown = set(patch.keys()) - EDITABLE_FIELDS
    if unknown:
        raise ValueError(
            f"unknown patch fields: {sorted(unknown)}"
        )
    for key, value in patch.items():
        if key in ("batch_mode", "compression", "debug_enabled"):
            _validate_boolean(value, key)
        elif key == "strict_host_key_checking":
            _validate_strict_host_key(value)
        elif key in _INTEGER_RANGES:
            _validate_integer(value, key)


@dataclass(frozen=True)
class GlobalSshOverrides:
    """Immutable snapshot of daemon-owned global SSH overrides.

    Every field uses the public API name; the persisted config key mapping
    lives in the core ``sshpilot.core.settings.ssh_overrides`` module.
    """

    revision: str
    connect_timeout: int
    connection_attempts: int
    server_alive_interval: int
    server_alive_count_max: int
    strict_host_key_checking: str
    batch_mode: bool
    compression: bool
    verbosity: int
    debug_enabled: bool
    applies_immediately: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "revision": self.revision,
            "connect_timeout": self.connect_timeout,
            "connection_attempts": self.connection_attempts,
            "server_alive_interval": self.server_alive_interval,
            "server_alive_count_max": self.server_alive_count_max,
            "strict_host_key_checking": self.strict_host_key_checking,
            "batch_mode": self.batch_mode,
            "compression": self.compression,
            "verbosity": self.verbosity,
            "debug_enabled": self.debug_enabled,
            "applies_immediately": self.applies_immediately,
        }

    def __post_init__(self) -> None:
        # Validate on construction (frozen dataclass uses object.__setattr__).
        _validate_revision(self.revision)
        _validate_integer(self.connect_timeout, "connect_timeout")
        _validate_integer(self.connection_attempts, "connection_attempts")
        _validate_integer(self.server_alive_interval, "server_alive_interval")
        _validate_integer(self.server_alive_count_max, "server_alive_count_max")
        _validate_strict_host_key(self.strict_host_key_checking)
        _validate_boolean(self.batch_mode, "batch_mode")
        _validate_boolean(self.compression, "compression")
        _validate_integer(self.verbosity, "verbosity")
        _validate_boolean(self.debug_enabled, "debug_enabled")
        _validate_boolean(self.applies_immediately, "applies_immediately")


@dataclass(frozen=True)
class UpdateGlobalSshOverridesRequest:
    """A partial update to global SSH overrides.

    ``patch`` contains only the fields to change (partial semantics).  The
    mapping is copied and frozen on construction: mutating the caller's
    original dictionary afterwards does not change the request, and the
    request's own ``patch`` is read-only.
    ``expected_revision`` enables optimistic concurrency control: the
    service rejects the update if the current revision does not match.
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
            raise ValueError(
                "expected_revision must be a non-empty string or None"
            )
        object.__setattr__(
            self, "patch", MappingProxyType(dict(self.patch))
        )


# ---------------------------------------------------------------------------
# Assistant access (AI MCP clients)
# ---------------------------------------------------------------------------


def _validate_connection_ids(value: Any) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items = (value,)
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        raise TypeError("allowed_connection_ids must be a list of connection ids")
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if type(item) is not str or not item.strip():
            raise ValueError("connection ids must be non-empty strings")
        cleaned = item.strip()
        if "\x00" in cleaned:
            raise ValueError("connection ids must not contain NUL")
        if cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return tuple(result)


@dataclass(frozen=True)
class AssistantAccessSettings:
    """Daemon-owned policy for AI assistant MCP access."""

    revision: str
    enabled: bool
    allowed_connection_ids: Tuple[str, ...]
    operate: bool
    mutate: bool
    content: bool
    http_listen: bool = False
    http_port: int = 0
    http_token: str = ""
    #: Runtime-only: True while the localhost Streamable HTTP sidecar is up.
    http_listening: bool = False
    #: Runtime-only: actual bound TCP port (may differ from http_port when 0).
    http_bound_port: Optional[int] = None
    #: Runtime-only: ``http://127.0.0.1:<bound>/mcp`` when listening.
    http_url: str = ""

    def __post_init__(self) -> None:
        _validate_revision(self.revision)
        _validate_boolean(self.enabled, "enabled")
        _validate_boolean(self.operate, "operate")
        _validate_boolean(self.mutate, "mutate")
        _validate_boolean(self.content, "content")
        _validate_boolean(self.http_listen, "http_listen")
        _validate_boolean(self.http_listening, "http_listening")
        object.__setattr__(
            self,
            "allowed_connection_ids",
            _validate_connection_ids(self.allowed_connection_ids),
        )
        object.__setattr__(self, "http_port", _validate_http_port(self.http_port))
        object.__setattr__(self, "http_token", _validate_http_token(self.http_token))
        if self.http_bound_port is not None:
            object.__setattr__(
                self,
                "http_bound_port",
                _validate_http_port(self.http_bound_port, allow_zero=False),
            )
        if type(self.http_url) is not str:
            raise TypeError("http_url must be a string")


@dataclass(frozen=True)
class UpdateAssistantAccessRequest:
    """Replace the assistant-access policy snapshot.

    Unlike SSH overrides, this is a full replace of the known fields so the
    Preferences UI can send the exact allowlist after checkbox edits.
    """

    enabled: bool
    allowed_connection_ids: Tuple[str, ...]
    operate: bool = True
    mutate: bool = False
    content: bool = False
    http_listen: bool = False
    http_port: int = 0
    regenerate_token: bool = False
    expected_revision: Optional[str] = None

    def __post_init__(self) -> None:
        _validate_boolean(self.enabled, "enabled")
        _validate_boolean(self.operate, "operate")
        _validate_boolean(self.mutate, "mutate")
        _validate_boolean(self.content, "content")
        _validate_boolean(self.http_listen, "http_listen")
        _validate_boolean(self.regenerate_token, "regenerate_token")
        object.__setattr__(
            self,
            "allowed_connection_ids",
            _validate_connection_ids(self.allowed_connection_ids),
        )
        object.__setattr__(self, "http_port", _validate_http_port(self.http_port))
        if self.expected_revision is not None and (
            not isinstance(self.expected_revision, str)
            or not self.expected_revision.strip()
        ):
            raise ValueError(
                "expected_revision must be a non-empty string or None"
            )


def _validate_http_port(value: Any, *, allow_zero: bool = True) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("http_port must be an integer")
    lo = 0 if allow_zero else 1
    if not lo <= value <= 65535:
        raise ValueError(f"http_port must be between {lo} and 65535")
    return value


def _validate_http_token(value: Any) -> str:
    if type(value) is not str:
        raise TypeError("http_token must be a string")
    # Empty is allowed on wire before the daemon has minted one.
    if value == "":
        return value
    cleaned = value.strip()
    if not cleaned or "\x00" in cleaned or any(ch.isspace() for ch in cleaned):
        raise ValueError("http_token must be a non-empty token without whitespace")
    if len(cleaned) > 256:
        raise ValueError("http_token is too long")
    return cleaned

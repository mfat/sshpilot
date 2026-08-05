"""Immutable API models for daemon-owned global SSH overrides."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

# ---------------------------------------------------------------------------
# Valid host-key-checking values accepted by OpenSSH.
# ---------------------------------------------------------------------------

_VALID_HK_VALUES = frozenset({"accept-new", "yes", "no", "ask"})

# ---------------------------------------------------------------------------
# Semantic field name → persisted config key mapping.
# ---------------------------------------------------------------------------

_FIELD_TO_CONFIG_KEY: Dict[str, str] = {
    "connect_timeout": "ssh.connection_timeout",
    "connection_attempts": "ssh.connection_attempts",
    "server_alive_interval": "ssh.keepalive_interval",
    "server_alive_count_max": "ssh.keepalive_count_max",
    "strict_host_key_checking": "ssh.strict_host_key_checking",
    "batch_mode": "ssh.batch_mode",
    "compression": "ssh.compression",
    "verbosity": "ssh.verbosity",
    "debug_enabled": "ssh.debug_enabled",
}

# Fields exposed through the API model (the public surface).
EDITABLE_FIELDS = frozenset(_FIELD_TO_CONFIG_KEY.keys())

# Integer fields with their documented valid ranges.
_INTEGER_RANGES: Dict[str, tuple[int, int]] = {
    "connect_timeout": (0, 600),
    "connection_attempts": (0, 100),
    "server_alive_interval": (0, 86400),
    "server_alive_count_max": (0, 100),
    "verbosity": (0, 3),
}

# ---------------------------------------------------------------------------
# Error codes narrowly scoped to this module.
# ---------------------------------------------------------------------------

REVISION_CONFLICT = "revision_conflict"
SETTINGS_MALFORMED = "settings_malformed"
SETTINGS_PERSISTENCE_FAILED = "settings_persistence_failed"


def _validate_boolean(value: Any, field: str) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{field} must be a boolean, got {type(value).__name__}")


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


def _compute_revision(data: Dict[str, Any]) -> str:
    """Deterministic revision from the semantic fields only.

    The revision is a hex SHA-256 prefix (12 chars) of the canonical JSON
    representation of the semantic fields.
    """
    canonical = json.dumps(
        {k: data[k] for k in sorted(_FIELD_TO_CONFIG_KEY)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


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
    lives in ``_FIELD_TO_CONFIG_KEY``.
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
        _validate_integer(self.connect_timeout, "connect_timeout")
        _validate_integer(self.connection_attempts, "connection_attempts")
        _validate_integer(self.server_alive_interval, "server_alive_interval")
        _validate_integer(self.server_alive_count_max, "server_alive_count_max")
        _validate_strict_host_key(self.strict_host_key_checking)
        _validate_boolean(self.batch_mode, "batch_mode")
        _validate_boolean(self.compression, "compression")
        _validate_integer(self.verbosity, "verbosity")
        _validate_boolean(self.debug_enabled, "debug_enabled")


@dataclass(frozen=True)
class UpdateGlobalSshOverridesRequest:
    """A partial update to global SSH overrides.

    ``patch`` contains only the fields to change (partial semantics).
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

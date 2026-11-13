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
from typing import Any, Dict, Mapping, Optional

from sshpilot.core.settings.ssh_overrides import (
    EDITABLE_FIELDS,
    FIELD_TO_CONFIG_KEY,
    INTEGER_RANGES,
    VALID_ADDRESS_FAMILY_VALUES,
    VALID_HOST_KEY_VALUES,
    VALID_YES_NO_VALUES,
    YES_NO_FIELDS,
    compute_ssh_overrides_revision,
    normalize_extra_options,
)

# Re-exported field-model contracts (consumed by the daemon service).
# ``EDITABLE_FIELDS`` is public API; the underscore-prefixed names are kept for
# internal compatibility with the codec and should not be part of the public
# surface.
EDITABLE_FIELDS = EDITABLE_FIELDS
_FIELD_TO_CONFIG_KEY: Dict[str, str] = dict(FIELD_TO_CONFIG_KEY)
_INTEGER_RANGES: Dict[str, tuple[int, int]] = dict(INTEGER_RANGES)
_VALID_HK_VALUES = VALID_HOST_KEY_VALUES
_VALID_YES_NO_VALUES = VALID_YES_NO_VALUES
_YES_NO_FIELDS = YES_NO_FIELDS


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
            f"{sorted(_VALID_HK_VALUES)} (empty = do not configure), "
            f"got {value!r}"
        )


def _validate_yes_no(value: Any, field: str) -> None:
    if not isinstance(value, str) or value not in _VALID_YES_NO_VALUES:
        raise ValueError(
            f"{field} must be one of {sorted(_VALID_YES_NO_VALUES)} "
            f"(empty = don't set), got {value!r}"
        )


def _validate_address_family(value: Any) -> None:
    if not isinstance(value, str) or value not in VALID_ADDRESS_FAMILY_VALUES:
        raise ValueError(
            f"address_family must be one of {sorted(VALID_ADDRESS_FAMILY_VALUES)} "
            f"(empty = not set), got {value!r}"
        )


def _validate_extra_options(value: Any) -> tuple:
    """Shape-check free-form options; returns them canonical (``Keyword=value``)."""
    return normalize_extra_options(value)


def _validate_patch_fields(patch: Mapping[str, Any]) -> None:
    """Reject unknown patch fields and type-check values."""
    unknown = set(patch.keys()) - EDITABLE_FIELDS
    if unknown:
        raise ValueError(
            f"unknown patch fields: {sorted(unknown)}"
        )
    for key, value in patch.items():
        if key in ("debug_enabled", "legacy_algorithms"):
            _validate_boolean(value, key)
        elif key == "address_family":
            _validate_address_family(value)
        elif key == "extra_options":
            _validate_extra_options(value)
        elif key in _YES_NO_FIELDS:
            _validate_yes_no(value, key)
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
    strict_host_key_checking: str
    compression: str
    verbosity: int
    debug_enabled: bool
    applies_immediately: bool = True
    #: Free-form options, each ``Keyword=value``, sent last as ``-o`` arguments.
    extra_options: tuple = ()
    #: ``""`` (not sent), ``inet`` (IPv4 only) or ``inet6`` (IPv6 only).
    address_family: str = ""
    #: Re-enable old key exchange, host key and cipher algorithms.
    legacy_algorithms: bool = False
    #: Read-only: every ``Keyword=value`` a launch sends ahead of a
    #: connection's own options (the fields above as composed, the keepalive
    #: pair, the legacy-algorithm and multiplexing options). OpenSSH keeps the
    #: first value per keyword, so order matters. Not part of the revision.
    forced_options: tuple = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "revision": self.revision,
            "connect_timeout": self.connect_timeout,
            "strict_host_key_checking": self.strict_host_key_checking,
            "compression": self.compression,
            "verbosity": self.verbosity,
            "debug_enabled": self.debug_enabled,
            "applies_immediately": self.applies_immediately,
            "extra_options": list(self.extra_options),
            "address_family": self.address_family,
            "legacy_algorithms": self.legacy_algorithms,
            "forced_options": list(self.forced_options),
        }

    def __post_init__(self) -> None:
        # Validate on construction (frozen dataclass uses object.__setattr__).
        _validate_revision(self.revision)
        _validate_integer(self.connect_timeout, "connect_timeout")
        _validate_strict_host_key(self.strict_host_key_checking)
        _validate_yes_no(self.compression, "compression")
        _validate_integer(self.verbosity, "verbosity")
        _validate_boolean(self.debug_enabled, "debug_enabled")
        _validate_boolean(self.applies_immediately, "applies_immediately")
        _validate_address_family(self.address_family)
        _validate_boolean(self.legacy_algorithms, "legacy_algorithms")
        object.__setattr__(
            self, "extra_options", _validate_extra_options(self.extra_options)
        )
        forced = self.forced_options
        if isinstance(forced, (str, bytes)) or not isinstance(forced, (list, tuple)):
            raise TypeError("forced_options must be a list of strings")
        if any(type(item) is not str or "=" not in item for item in forced):
            raise ValueError("forced_options entries must be Keyword=value")
        object.__setattr__(self, "forced_options", tuple(forced))


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

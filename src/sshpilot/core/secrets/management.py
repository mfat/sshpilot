"""Daemon-owned secret-backend management: field model, normalization, revision.

This is the single GTK-free owner of the semantic secret-management fields:

* the public field name → persisted ``secrets.*`` config key mapping,
* the canonical defaults for every editable field,
* the strict conversion that turns a raw ``secrets`` subtree into normalized
  semantic values, and
* the deterministic revision token over those normalized values.

The API layer and the daemon service both consume these contracts so the values
used by the public DTOs and the revision token always agree.  This module never
carries secret material — it manages *selection and configuration*, not values.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Mapping

from .policy import normalize_backend_name

# ---------------------------------------------------------------------------
# Canonical field model
# ---------------------------------------------------------------------------

# Semantic public field name -> dotted persisted config key.
FIELD_TO_CONFIG_KEY: Dict[str, str] = {
    "backend": "secrets.backend",
    "session_timeout": "secrets.session_timeout",
    "remember_in_keyring": "secrets.remember_in_keyring",
    "bitwarden_profile": "secrets.bitwarden.profile",
    "bitwarden_server": "secrets.bitwarden.server",
    "keepassxc_database": "secrets.keepassxc.database",
    "keepassxc_keyfile": "secrets.keepassxc.keyfile",
}

# Fields exposed through the management API (the public surface).
EDITABLE_FIELDS = frozenset(FIELD_TO_CONFIG_KEY.keys())

# Integer fields with their documented valid ranges.
INTEGER_RANGES: Dict[str, tuple[int, int]] = {
    # minutes of idle before the cached unlock token is dropped; 0 = until exit.
    "session_timeout": (0, 10080),
}

# Valid backend selection values (normalized via ``normalize_backend_name``).
VALID_BACKEND_VALUES = frozenset(
    {"auto", "libsecret", "keyring", "pass", "bitwarden", "rbw", "keepassxc", "agent"}
)

# Canonical semantic defaults. A missing raw key normalizes to exactly these
# values, so "field absent" and "field set to its default" are equivalent for
# the snapshot and the revision token.
DEFAULT_SECRET_SETTINGS: Dict[str, Any] = {
    "backend": "auto",
    "session_timeout": 0,
    "remember_in_keyring": False,
    "bitwarden_profile": "",
    "bitwarden_server": "",
    "keepassxc_database": "",
    "keepassxc_keyfile": "",
}


# ---------------------------------------------------------------------------
# Strict normalization (single conversion used by model + revision)
# ---------------------------------------------------------------------------

def normalize_secret_settings(secrets: Mapping[str, Any]) -> Dict[str, Any]:
    """Strictly convert a raw ``secrets`` subtree to normalized semantic values.

    Returns exactly the public field names with canonical values. Missing keys
    normalize to :data:`DEFAULT_SECRET_SETTINGS`; ``backend`` is normalized via
    :func:`normalize_backend_name` (so a legacy ``vaultwarden`` migrates to
    ``bitwarden``).

    Raises ``TypeError`` for non-boolean booleans / non-integer integers and
    ``ValueError`` for out-of-range integers or an unknown backend name.
    """
    semantic: Dict[str, Any] = {}
    for field in sorted(FIELD_TO_CONFIG_KEY):
        raw_key = FIELD_TO_CONFIG_KEY[field].split(".", 1)[1]
        value = _nested_lookup(secrets, raw_key) if isinstance(secrets, Mapping) else None

        if field in INTEGER_RANGES:
            if value is None:
                semantic[field] = DEFAULT_SECRET_SETTINGS[field]
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(
                    f"{field} must be an integer, got {type(value).__name__}"
                )
            lo, hi = INTEGER_RANGES[field]
            if not (lo <= value <= hi):
                raise ValueError(f"{field} must be between {lo} and {hi}")
            semantic[field] = value
        elif field == "backend":
            if value is None:
                semantic[field] = DEFAULT_SECRET_SETTINGS[field]
                continue
            normalized = normalize_backend_name(value)
            if normalized not in VALID_BACKEND_VALUES:
                raise ValueError(
                    f"backend must be one of {sorted(VALID_BACKEND_VALUES)}, got {value!r}"
                )
            semantic[field] = normalized
        elif field == "remember_in_keyring":
            if value is None:
                semantic[field] = DEFAULT_SECRET_SETTINGS[field]
                continue
            if not isinstance(value, bool):
                raise TypeError(
                    f"{field} must be a boolean, got {type(value).__name__}"
                )
            semantic[field] = value
        else:  # path-like text fields (profile / server / database / keyfile)
            if value is None:
                semantic[field] = DEFAULT_SECRET_SETTINGS[field]
                continue
            if not isinstance(value, str):
                raise TypeError(
                    f"{field} must be a string, got {type(value).__name__}"
                )
            semantic[field] = value.strip()

    return semantic


def _nested_lookup(container: Mapping[str, Any], dotted_key: str) -> Any:
    """Traverse a dotted key relative to *container* (no leading namespace)."""
    value: Any = container
    for part in dotted_key.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def compute_secret_settings_revision(semantic: Mapping[str, Any]) -> str:
    """Deterministic revision over the normalized semantic values.

    The revision is a hex SHA-256 prefix (12 chars) of the canonical JSON of the
    sorted field set, so two semantically equal states always share a revision
    and no-op updates keep it stable.
    """
    canonical = json.dumps(
        {k: semantic[k] for k in sorted(FIELD_TO_CONFIG_KEY)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]

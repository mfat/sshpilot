"""SSH identity-provider settings: field model, normalization, revision.

This is the single GTK-free owner of the semantic identity settings:

* the public field name → persisted config key mapping,
* the canonical defaults for every editable field,
* the strict conversion that turns a raw ``identity`` subtree into normalized
  semantic values, and
* the deterministic revision token over those normalized values.

The API layer and the daemon identity service both consume these contracts so
the values used by ``IdentityState`` and the revision token always agree.

Selection semantics are inherited from :mod:`sshpilot.identity`: ``'auto'``
(the default) and any unknown provider name resolve to the system ssh-agent,
so the agent is never silently disabled.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Mapping

# ---------------------------------------------------------------------------
# Canonical field model
# ---------------------------------------------------------------------------

# Semantic public field name -> dotted persisted config key.
FIELD_TO_CONFIG_KEY: Dict[str, str] = {
    "provider": "identity.provider",
    "custom_socket": "identity.agent_socket",
}

# Fields exposed through the API model (the public surface).
EDITABLE_FIELDS = frozenset(FIELD_TO_CONFIG_KEY.keys())

# Canonical semantic defaults (mirror ``core.settings.defaults``).
DEFAULT_IDENTITY_SETTINGS: Dict[str, Any] = {
    "provider": "auto",
    "custom_socket": "",
}

# Selection that always means "the system ssh-agent".
AUTO_PROVIDER = "auto"

# The free-form fixed-socket selection; requires ``custom_socket``.
CUSTOM_PROVIDER = "custom"

_PROVIDER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Custom socket paths may be absolute paths or ``~``-relative; anything else
# would be silently ignored by OpenSSH's agent lookup.
_SOCKET_PATTERN = re.compile(r"^[~/][^\x00]*$")
_MAX_SOCKET_LENGTH = 512


def normalize_provider(value: Any) -> str:
    """Strictly normalize one provider selection.

    Unknown (but well-formed) names are preserved: the identity manager
    resolves them to the system ssh-agent at use time, preserving the
    historical never-silently-disable semantics.
    """
    if not isinstance(value, str):
        raise TypeError(
            f"provider must be a string, got {type(value).__name__}"
        )
    normalized = value.strip().lower()
    if not normalized:
        return AUTO_PROVIDER
    if not _PROVIDER_PATTERN.fullmatch(normalized):
        raise ValueError(
            "provider must be a lowercase provider identifier"
        )
    return normalized


def normalize_custom_socket(value: Any) -> str:
    """Strictly normalize the custom agent socket path (``''`` = unset)."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError(
            f"custom_socket must be a string, got {type(value).__name__}"
        )
    normalized = value.strip()
    if not normalized:
        return ""
    if "\x00" in normalized:
        raise ValueError("custom_socket must not contain NUL")
    if len(normalized) > _MAX_SOCKET_LENGTH:
        raise ValueError("custom_socket is too long")
    if not _SOCKET_PATTERN.match(normalized):
        raise ValueError(
            "custom_socket must be an absolute or ~-relative path"
        )
    return normalized


def normalize_identity_settings(identity: Mapping[str, Any]) -> Dict[str, Any]:
    """Strictly convert a raw ``identity`` subtree to normalized values.

    Returns exactly the public field names with canonical values; missing keys
    normalize to :data:`DEFAULT_IDENTITY_SETTINGS` so "field absent" and
    "field set to its default" share a revision.
    """
    raw = identity if isinstance(identity, Mapping) else {}
    provider = raw.get("provider")
    custom_socket = raw.get("agent_socket")
    return {
        "provider": (
            AUTO_PROVIDER if provider is None else normalize_provider(provider)
        ),
        "custom_socket": normalize_custom_socket(custom_socket),
    }


def compute_identity_revision(semantic: Mapping[str, Any]) -> str:
    """Deterministic revision over the normalized identity values.

    The revision is a hex SHA-256 prefix (12 chars) of the canonical JSON of
    the sorted field set, so two semantically equal states always share a
    revision and no-op updates keep it stable.
    """
    canonical = json.dumps(
        {k: semantic[k] for k in sorted(FIELD_TO_CONFIG_KEY)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]

"""Secret-backend policy decisions (GTK-free; no secret values)."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Sequence


class SecretBackendName(str, Enum):
    AUTO = "auto"
    LIBSECRET = "libsecret"
    KEYRING = "keyring"
    PASS = "pass"
    BITWARDEN = "bitwarden"
    KEEPASSXC = "keepassxc"
    AGENT = "agent"


class SecretDecisionKind(str, Enum):
    READY = "ready"
    UNLOCK_REQUIRED = "unlock_required"
    INTERACTION_REQUIRED = "interaction_required"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    MIGRATION_REQUIRED = "migration_required"
    PERMISSION_DENIED = "permission_denied"
    LOCKED = "locked"


@dataclass(frozen=True)
class SecretPolicyDecision:
    """Outcome of a secret-policy check — never carries secret material."""

    kind: SecretDecisionKind
    backend: str


# Legacy config values that should migrate to bitwarden.
_LEGACY_BACKEND_ALIASES = {
    "vaultwarden": SecretBackendName.BITWARDEN.value,
}


def normalize_backend_name(name: Optional[str]) -> str:
    """Normalize a configured backend name (aliases + lowercasing)."""
    raw = (name or SecretBackendName.AUTO.value).strip().lower()
    return _LEGACY_BACKEND_ALIASES.get(raw, raw or SecretBackendName.AUTO.value)


def platform_default_order(*, platform: Optional[str] = None) -> List[str]:
    """Backend probe order for ``auto`` selection."""
    plat = (platform or sys.platform).lower()
    if plat.startswith("darwin"):
        return [SecretBackendName.KEYRING.value]
    # Linux / other: prefer libsecret, then keyring.
    return [SecretBackendName.LIBSECRET.value, SecretBackendName.KEYRING.value]


def resolve_store_order(
    selected: Optional[str],
    *,
    platform: Optional[str] = None,
) -> List[str]:
    """Backends to try for ``store`` under the given selection."""
    name = normalize_backend_name(selected)
    if name in ("", SecretBackendName.AUTO.value):
        return platform_default_order(platform=platform)
    return [name]


def resolve_lookup_order(
    selected: Optional[str],
    *,
    platform: Optional[str] = None,
    available: Optional[Sequence[str]] = None,
) -> List[str]:
    """Backends to consult for ``lookup``/``delete``.

    ``auto`` falls through every available backend so secrets are not orphaned
    after a selection change. Explicit selection is exclusive.
    """
    name = normalize_backend_name(selected)
    if name not in ("", SecretBackendName.AUTO.value):
        return [name]
    defaults = platform_default_order(platform=platform)
    if available is None:
        return defaults
    # Preserve default order, then any other available backends.
    seen = set()
    order: List[str] = []
    for n in list(defaults) + list(available):
        if n not in seen:
            seen.add(n)
            order.append(n)
    return order


def decide_unlock(
    *,
    backend: str,
    session_backed: bool,
    is_unlocked: bool,
    available: bool,
) -> SecretPolicyDecision:
    """Decide whether a secret operation can proceed or needs interaction."""
    name = normalize_backend_name(backend)
    if not available:
        return SecretPolicyDecision(
            kind=SecretDecisionKind.BACKEND_UNAVAILABLE,
            backend=name,
        )
    # Session vaults collect a master password in-app. rbw is session-backed
    # (same GTK dialog as Bitwarden/KeePass) but keep the name gate so a locked
    # agent still blocks startup/connect if session_backed is ever unset.
    lockable = session_backed or name == "rbw"
    if lockable and not is_unlocked:
        return SecretPolicyDecision(
            kind=SecretDecisionKind.UNLOCK_REQUIRED,
            backend=name,
        )
    return SecretPolicyDecision(kind=SecretDecisionKind.READY, backend=name)

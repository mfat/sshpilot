"""Shared credential model + spec<->credential translation.

Kept separate from both :mod:`credential_manager` (the orchestrator) and
:mod:`credential_adapters` (the per-backend adapters) so they can all import it without a
cycle. GTK-free.

A :class:`Credential` is the normalized shape; the helpers translate between it and the
backend-level ``SecretSpec`` (``secret_storage``):

- :func:`spec_to_credential` — from a known spec + value (the connection-derived path).
- :func:`credential_from_attributes` — from raw enumerated attributes + value (the backend
  ``iter_credentials`` / adapter ``load_all`` path).
- :func:`credential_to_spec` — reverse, for adapter ``save``/``delete``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .secret_storage import (
    SecretSpec,
    password_spec,
    passphrase_spec,
    sudo_password_spec,
)

# Normalized credential types (mapped from the SecretSpec ``type`` attribute).
TYPE_PASSWORD = "password"   # host login password  (spec type ssh_password)
TYPE_SUDO = "sudo"           # sudo password         (spec type sudo_password)
TYPE_KEY = "key"             # SSH key passphrase    (spec type key_passphrase)

_TYPE_FROM_SPEC = {
    "ssh_password": TYPE_PASSWORD,
    "sudo_password": TYPE_SUDO,
    "key_passphrase": TYPE_KEY,
}
_SPEC_FROM_TYPE = {v: k for k, v in _TYPE_FROM_SPEC.items()}


def _conn_attr(conn, name: str, default: str = "") -> str:
    """Read a string field from a Connection or a plain dict."""
    if isinstance(conn, dict):
        return str(conn.get(name) or default)
    return str(getattr(conn, name, None) or default)


def canonical_password_host(conn) -> str:
    """Canonical keyring host for an SSH connection password.

    Matches :meth:`Connection.get_effective_host`: ``hostname`` → ``host`` →
    ``nickname``. New passwords are always stored under this key; legacy entries
  under older aliases are migrated on read.
    """
    for key in ("hostname", "host", "nickname"):
        value = _conn_attr(conn, key).strip()
        if value:
            return value
    get_eff = getattr(conn, "get_effective_host", None)
    if callable(get_eff):
        try:
            return str(get_eff() or "").strip()
        except Exception:
            pass
    return ""


def password_host_candidates(conn) -> List[str]:
    """Host identifiers to probe when looking up a legacy SSH password.

    Order: effective host, ``hostname``, ``host``, ``nickname`` — first hit wins;
    a non-canonical hit is re-stored under :func:`canonical_password_host`.
    """
    get_eff = getattr(conn, "get_effective_host", None)
    raw = [
        get_eff() if callable(get_eff) else "",
        _conn_attr(conn, "hostname"),
        _conn_attr(conn, "host"),
        _conn_attr(conn, "nickname"),
    ]
    seen: set = set()
    ordered: List[str] = []
    for h in raw:
        h = (h or "").strip()
        if h and h not in seen:
            seen.add(h)
            ordered.append(h)
    return ordered


@dataclass
class Credential:
    """One normalized, sshPilot-stored secret.

    ``id`` is the backend account key (``user@host`` / ``sudo:user@host`` / canonical key
    path). ``host``/``username`` are set for password & sudo creds and ``None`` for a key
    passphrase. ``metadata`` carries the source backend, the spec label, the raw spec type,
    and (where known) ``key_path``, ``port``, ``uri``, the referencing ``connection``(s), and
    ``orphan`` (enumerated with no matching connection).
    """

    id: str
    type: str
    host: Optional[str] = None
    username: Optional[str] = None
    secret: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


# --- attributes/spec -> Credential -------------------------------------------

def _account_for(raw_type: str, attrs: Dict[str, str]) -> str:
    """Reconstruct the backend account key from spec attributes (same formula the spec
    builders use, so it round-trips even for email-style usernames)."""
    host = attrs.get("host") or ""
    user = attrs.get("username") or ""
    if raw_type == "ssh_password":
        return f"{user}@{host}"
    if raw_type == "sudo_password":
        return f"sudo:{user}@{host}"
    if raw_type == "key_passphrase":
        return attrs.get("key_path") or ""
    return attrs.get("key_path") or f"{user}@{host}"


def credential_from_attributes(attributes: Dict[str, str], value: str, backend_name: str,
                               *, label: Optional[str] = None) -> Credential:
    """Build a Credential from raw enumerated attributes + value (the ``iter_credentials``
    path). ``id`` is reconstructed from the attributes."""
    attrs = attributes or {}
    raw_type = attrs.get("type", "")
    ctype = _TYPE_FROM_SPEC.get(raw_type, raw_type or "unknown")
    host = attrs.get("host") or None
    username = attrs.get("username") or None
    key_path = attrs.get("key_path") or None

    metadata: Dict[str, Any] = {"backend": backend_name, "raw_type": raw_type}
    if label:
        metadata["label"] = label
    if key_path:
        metadata["key_path"] = key_path
    return Credential(
        id=_account_for(raw_type, attrs),
        type=ctype,
        host=host,
        username=username,
        secret=value,
        metadata=metadata,
    )


def spec_to_credential(spec: SecretSpec, value: str, backend_name: str, *,
                       connection=None) -> Credential:
    """Build a Credential from a known spec + value (the connection-derived path), enriching
    with connection metadata (port / uri / nickname) when a connection is supplied."""
    attrs = spec.attributes or {}
    cred = credential_from_attributes(attrs, value, backend_name, label=spec.label)
    cred.id = spec.keyring_account                      # authoritative id from the spec
    if connection is not None:
        port = getattr(connection, "port", None)
        nickname = getattr(connection, "nickname", "") or None
        if port:
            cred.metadata["port"] = port
        if nickname:
            cred.metadata["connection"] = nickname
        if cred.host and cred.username:
            suffix = ""
            try:
                if port and int(port) != 22:
                    suffix = f":{int(port)}"
            except (TypeError, ValueError):
                suffix = ""
            cred.metadata["uri"] = f"ssh://{cred.username}@{cred.host}{suffix}"
    return cred


# --- Credential -> spec (adapter save/delete) --------------------------------

def credential_to_spec(cred: Credential) -> SecretSpec:
    """Reverse mapping for ``save``/``delete``: rebuild the backend ``SecretSpec`` for a
    Credential. Raises ``ValueError`` for an unknown type."""
    if cred.type == TYPE_PASSWORD:
        return password_spec(cred.host or "", cred.username or "")
    if cred.type == TYPE_SUDO:
        return sudo_password_spec(cred.host or "", cred.username or "")
    if cred.type == TYPE_KEY:
        return passphrase_spec(cred.metadata.get("key_path") or cred.id)
    raise ValueError(f"cannot build a spec for credential type {cred.type!r}")

"""Credential manager: a normalized, read-only view of every sshPilot-stored secret.

sshPilot stores host passwords, sudo passwords and SSH key passphrases through the pluggable
secret backends (:mod:`secret_storage`), each under an opaque ``SecretSpec``. This module
gathers them into one flat :class:`Credential` shape by *deriving* the specs sshPilot manages
(from its connections and SSH keys) and resolving each across **all available backends** — the
foundation for exporting/migrating credentials to other vaults (Bitwarden, KeePass, …).

It is GTK-free and dependency-injected (it never imports GTK or constructs app objects), so it
is unit-testable: pass any connection source exposing ``get_connections()`` (or a plain list of
connections), and optionally a :class:`SecretManager` and extra key paths.

Eager: :meth:`CredentialManager.list_credentials` resolves every secret value as it lists.
It reads only what is currently available — a locked session vault (e.g. Bitwarden) simply
contributes nothing; this never prompts or forces an unlock.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .secret_storage import (
    SecretSpec,
    get_secret_manager,
    password_spec,
    passphrase_spec,
    sudo_password_spec,
)

logger = logging.getLogger(__name__)

# Normalized credential types (mapped from the SecretSpec ``type`` attribute).
TYPE_PASSWORD = "password"   # host login password  (spec type ssh_password)
TYPE_SUDO = "sudo"           # sudo password         (spec type sudo_password)
TYPE_KEY = "key"             # SSH key passphrase    (spec type key_passphrase)

_TYPE_FROM_SPEC = {
    "ssh_password": TYPE_PASSWORD,
    "sudo_password": TYPE_SUDO,
    "key_passphrase": TYPE_KEY,
}


@dataclass
class Credential:
    """One normalized, sshPilot-stored secret.

    ``id`` is the backend account key (``user@host`` / ``sudo:user@host`` / canonical key
    path). ``host``/``username`` are set for password & sudo creds and ``None`` for a key
    passphrase. ``metadata`` carries the source backend, the spec label, the raw spec type,
    and (where known) ``key_path``, ``port``, ``uri`` and the referencing ``connection``(s).
    """

    id: str
    type: str
    host: Optional[str] = None
    username: Optional[str] = None
    secret: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


def _canonical_key_path(key_path: str) -> str:
    """Canonical key path used as the passphrase account — mirrors
    ``askpass_utils._normalize_key_path_for_storage`` (kept local so this module stays
    GTK-free and dependency-light)."""
    expanded = os.path.expanduser(key_path or "")
    try:
        return os.path.realpath(expanded)
    except Exception:
        return os.path.abspath(expanded)


class CredentialManager:
    """Builds a normalized list of sshPilot-stored credentials across all backends."""

    def __init__(self, connection_manager, *, secret_manager=None,
                 extra_key_paths: Iterable[str] = ()):
        self._connections_source = connection_manager
        self._secrets = secret_manager if secret_manager is not None else get_secret_manager()
        self._extra_key_paths = list(extra_key_paths or ())

    # -- public API -------------------------------------------------------
    def list_credentials(self) -> List[Credential]:
        """Every sshPilot-stored credential, normalized and eager-loaded.

        Resolves each secret now (across all available backends); entries with no stored
        secret are omitted. Deduped by ``(id, type)`` — a value present in several backends
        collapses to one credential recording the first backend found.
        """
        out: Dict[Tuple[str, str], Credential] = {}
        connections = self._get_connections()
        for conn in connections:
            try:
                self._collect_connection_passwords(conn, out)
            except Exception:
                logger.debug("collecting passwords for a connection failed", exc_info=True)
        self._collect_key_passphrases(connections, out)
        return list(out.values())

    # -- connection passwords + sudo -------------------------------------
    def _collect_connection_passwords(self, conn, out: Dict[Tuple[str, str], Credential]) -> None:
        username = getattr(conn, "username", "") or ""
        for builder in (password_spec, sudo_password_spec):
            for host in self._host_candidates(conn):
                spec = builder(host, username)
                found = self._lookup(spec)
                if found is None:
                    continue
                value, backend = found
                cred = self._credential_from(spec, value, backend, connection=conn)
                out.setdefault((cred.id, cred.type), cred)
                break   # first host-variant hit wins for this (connection, kind)

    @staticmethod
    def _host_candidates(conn) -> List[str]:
        # Secrets are stored inconsistently under hostname/host/nickname (the connection
        # write path prefers hostname; read paths like sftp_utils probe all three) — so try
        # them in that order and stop at the first that has the secret.
        get_eff = getattr(conn, "get_effective_host", None)
        raw = [
            get_eff() if callable(get_eff) else "",
            getattr(conn, "hostname", "") or "",
            getattr(conn, "host", "") or "",
            getattr(conn, "nickname", "") or "",
        ]
        seen: set = set()
        ordered: List[str] = []
        for h in raw:
            h = (h or "").strip()
            if h and h not in seen:
                seen.add(h)
                ordered.append(h)
        return ordered

    # -- key passphrases --------------------------------------------------
    def _collect_key_passphrases(self, connections, out: Dict[Tuple[str, str], Credential]) -> None:
        # canonical key path -> set of connection nicknames referencing it
        refs: Dict[str, set] = {}
        for conn in connections:
            try:
                label = getattr(conn, "nickname", "") or getattr(conn, "host", "") or ""
                for kp in self._connection_key_paths(conn):
                    refs.setdefault(_canonical_key_path(kp), set()).add(label)
            except Exception:
                logger.debug("collecting key paths for a connection failed", exc_info=True)
        for kp in self._extra_key_paths:
            refs.setdefault(_canonical_key_path(kp), set())

        for path, nicknames in refs.items():
            if not path:
                continue
            spec = passphrase_spec(path)
            found = self._lookup(spec)
            if found is None:
                continue
            value, backend = found
            cred = self._credential_from(spec, value, backend)
            cred.metadata["connections"] = sorted(n for n in nicknames if n)
            out.setdefault((cred.id, cred.type), cred)

    @staticmethod
    def _connection_key_paths(conn) -> List[str]:
        paths: List[str] = []
        kf = getattr(conn, "keyfile", "") or ""
        if kf:
            paths.append(kf)
        for p in (getattr(conn, "identity_files", None) or []):
            if p:
                paths.append(p)
        return paths

    # -- helpers ----------------------------------------------------------
    def _lookup(self, spec: SecretSpec) -> Optional[Tuple[str, str]]:
        try:
            return self._secrets.lookup_everywhere(spec)
        except Exception:
            logger.debug("lookup_everywhere failed for %s", getattr(spec, "label", spec),
                         exc_info=True)
            return None

    def _get_connections(self) -> List[Any]:
        getter = getattr(self._connections_source, "get_connections", None)
        if callable(getter):
            try:
                return list(getter())
            except Exception:
                logger.debug("get_connections() failed", exc_info=True)
                return []
        try:                              # allow a plain iterable of connections
            return list(self._connections_source or [])
        except Exception:
            return []

    @staticmethod
    def _credential_from(spec: SecretSpec, value: str, backend_name: str, *,
                         connection=None) -> Credential:
        attrs = spec.attributes or {}
        raw_type = attrs.get("type", "")
        ctype = _TYPE_FROM_SPEC.get(raw_type, raw_type or "unknown")
        host = attrs.get("host") or None
        username = attrs.get("username") or None
        key_path = attrs.get("key_path") or None

        metadata: Dict[str, Any] = {
            "backend": backend_name,
            "raw_type": raw_type,
            "label": spec.label,
        }
        if key_path:
            metadata["key_path"] = key_path
        if connection is not None:
            port = getattr(connection, "port", None)
            nickname = getattr(connection, "nickname", "") or None
            if port:
                metadata["port"] = port
            if nickname:
                metadata["connection"] = nickname
            if host and username:
                suffix = ""
                try:
                    if port and int(port) != 22:
                        suffix = f":{int(port)}"
                except (TypeError, ValueError):
                    suffix = ""
                metadata["uri"] = f"ssh://{username}@{host}{suffix}"

        return Credential(
            id=spec.keyring_account,
            type=ctype,
            host=host,
            username=username,
            secret=value,
            metadata=metadata,
        )

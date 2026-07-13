"""Credential manager: a normalized, read-only view of every sshPilot-stored secret.

sshPilot stores host passwords, sudo passwords and SSH key passphrases through the pluggable
secret backends (:mod:`secret_storage`), each under an opaque ``SecretSpec``. This module
gathers them into one flat :class:`Credential` shape by *deriving* the specs sshPilot manages
(from its connections and SSH keys) and resolving each across **all available backends** — the
foundation for exporting/migrating credentials to other vaults (Bitwarden, KeePass, …).

It is GTK-free and dependency-injected (it never imports GTK or constructs app objects), so it
is unit-testable: pass any connection source exposing ``get_connections()`` (or a plain list of
connections), and optionally a :class:`SecretManager` and extra key paths.

SSH password host keys are canonicalized at connect time via
:meth:`ConnectionManager.store_connection_password` /
:meth:`ConnectionManager.get_connection_password` (see ``credential_model.canonical_password_host``).
This module still probes legacy host aliases when listing connection-derived passwords.

Eager: :meth:`CredentialManager.list_credentials` resolves every secret value as it lists.
It reads only what is currently available — a locked session vault (e.g. Bitwarden) simply
contributes nothing; this never prompts or forces an unlock.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Tuple

from .secret_storage import (
    get_secret_manager,
    key_path_lookup_candidates,
    normalize_key_path_for_storage,
    password_spec,
    passphrase_spec,
    sudo_password_spec,
)
# Credential model + spec<->credential translation live in credential_model so the adapters
# and this orchestrator can share them without a cycle. Re-exported here for back-compat.
from .credential_model import (  # noqa: F401
    Credential,
    TYPE_PASSWORD,
    TYPE_SUDO,
    TYPE_KEY,
    password_host_candidates,
    spec_to_credential,
)
from .credential_adapters import SecretBackendAdapter

logger = logging.getLogger(__name__)


class CredentialManager:
    """Builds a normalized list of sshPilot-stored credentials across all backends."""

    def __init__(self, connection_manager, *, secret_manager=None,
                 extra_key_paths: Iterable[str] = ()):
        self._connections_source = connection_manager
        self._secrets = secret_manager if secret_manager is not None else get_secret_manager()
        self._extra_key_paths = list(extra_key_paths or ())

    # -- public API -------------------------------------------------------
    def list_credentials(self, *, include_orphans: bool = True) -> List[Credential]:
        """Every sshPilot-stored credential, normalized and eager-loaded.

        Merges two discovery passes, deduped by ``(id, type)``:
        1. **Connection-derived** — look up each connection's password/sudo and each key's
           passphrase across all available backends (rich metadata: connection/port/uri).
        2. **Enumeration** (``include_orphans``) — for each available backend whose adapter can
           enumerate (libsecret/pass/bitwarden), ``load_all()`` to surface **orphans** (stored
           secrets with no matching connection); these are tagged ``metadata['orphan']=True``.

        Pass ``include_orphans=False`` (e.g. a connection-scoped backup) to skip pass 2 and
        return only the credentials of the given connections. Connection-derived entries win on
        collision (richer metadata). Entries with no stored secret are omitted.
        """
        out: Dict[Tuple[str, str], Credential] = {}
        connections = self._get_connections()
        for conn in connections:
            try:
                self._collect_connection_passwords(conn, out)
            except Exception:
                logger.debug("collecting passwords for a connection failed", exc_info=True)
        self._collect_key_passphrases(connections, out)
        if include_orphans:
            self._merge_enumeration(out)
        return list(out.values())

    def _merge_enumeration(self, out: Dict[Tuple[str, str], Credential]) -> None:
        """Add enumerated orphans from every available backend that can enumerate."""
        try:
            backends = self._secrets.all_available_backends()
        except Exception:
            logger.debug("listing available backends failed", exc_info=True)
            return
        for backend in backends:
            adapter = SecretBackendAdapter(backend)
            if not adapter.can_enumerate:
                continue
            for cred in adapter.load_all():
                key = (cred.id, cred.type)
                if key in out:
                    continue                       # connection-derived already has it (richer)
                cred.metadata["orphan"] = True
                out[key] = cred

    # -- connection passwords + sudo -------------------------------------
    def _collect_connection_passwords(self, conn, out: Dict[Tuple[str, str], Credential]) -> None:
        username = getattr(conn, "username", "") or ""
        for builder in (password_spec, sudo_password_spec):
            for host in password_host_candidates(conn):
                spec = builder(host, username)
                found = self._lookup(spec)
                if found is None:
                    continue
                value, backend = found
                cred = spec_to_credential(spec, value, backend, connection=conn)
                out.setdefault((cred.id, cred.type), cred)
                break   # first host-variant hit wins for this (connection, kind)

    # -- key passphrases --------------------------------------------------
    def _collect_key_passphrases(self, connections, out: Dict[Tuple[str, str], Credential]) -> None:
        # canonical key path -> set of connection nicknames referencing it
        refs: Dict[str, set] = {}
        for conn in connections:
            try:
                label = getattr(conn, "nickname", "") or getattr(conn, "host", "") or ""
                for kp in self._connection_key_paths(conn):
                    refs.setdefault(normalize_key_path_for_storage(kp), set()).add(label)
            except Exception:
                logger.debug("collecting key paths for a connection failed", exc_info=True)
        for kp in self._extra_key_paths:
            refs.setdefault(normalize_key_path_for_storage(kp), set())

        for path, nicknames in refs.items():
            if not path:
                continue
            # Probe the portable (~) title AND the legacy absolute one, so a passphrase saved
            # under either form is included in the export.
            found = None
            for candidate in key_path_lookup_candidates(path):
                found = self._lookup(passphrase_spec(candidate))
                if found is not None:
                    break
            if found is None:
                continue
            value, backend = found
            # Export under the canonical (portable) identity regardless of which title matched,
            # so restore re-stores it portably.
            cred = spec_to_credential(passphrase_spec(path), value, backend)
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
        for p in (getattr(conn, "resolved_identity_files", None) or []):
            if p and p not in paths:
                paths.append(p)
        return paths

    # -- helpers ----------------------------------------------------------
    def _lookup(self, spec):
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

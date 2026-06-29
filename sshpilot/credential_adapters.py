"""Credential-centric backend adapters: ``load_all`` / ``save`` / ``delete`` / ``watch_changes``.

Each adapter presents one secret store as :class:`Credential` operations:

- :class:`SecretBackendAdapter` wraps a registered ``SecretBackend`` (spec-keyed
  store/lookup/delete) and adds enumeration where the backend supports it (its
  ``iter_credentials`` hook). keyring/agent have no hook → ``can_enumerate=False``.
- :class:`KdbxAdapter` is a standalone pykeepass-backed export/import target (a ``.kdbx``
  file), **not** a connect-time backend in this phase.

``watch_changes(callback)`` has a **no-op default**; real change observation (libsecret D-Bus
signals, a KDBX file monitor, Bitwarden polling) is a later phase.
"""

from __future__ import annotations

import logging
from typing import Callable, List, Optional

from .credential_model import (
    Credential,
    TYPE_KEY,
    credential_from_attributes,
    credential_to_spec,
)

logger = logging.getLogger(__name__)

try:  # optional dependency — only needed by KdbxAdapter
    from pykeepass import PyKeePass
    from pykeepass.exceptions import CredentialsError
except Exception:  # pragma: no cover - optional dependency
    PyKeePass = None

    class CredentialsError(Exception):
        pass

# Custom KeePass entry property that preserves the sshPilot credential type across a round-trip.
_KDBX_TYPE_PROP = "sshpilot_type"


def _noop_unsubscribe() -> None:
    return None


class BackendAdapter:
    """Credential-centric view of one secret store."""

    name: str = "base"
    can_enumerate: bool = False

    def is_available(self) -> bool:
        raise NotImplementedError

    def load_all(self) -> List[Credential]:
        """Enumerate every credential in this store. ``[]`` when the store can't enumerate."""
        return []

    def save(self, credential: Credential) -> bool:
        raise NotImplementedError

    def delete(self, credential: Credential) -> bool:
        raise NotImplementedError

    def watch_changes(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Observe external changes and invoke ``callback`` when they happen. Default: no-op.
        Returns an unsubscribe callable. Real implementations land in a later phase."""
        return _noop_unsubscribe


class SecretBackendAdapter(BackendAdapter):
    """Adapter over a registered ``SecretBackend`` (libsecret/keyring/pass/bitwarden/agent)."""

    def __init__(self, backend):
        self._backend = backend
        self.name = getattr(backend, "name", "backend")
        self.can_enumerate = callable(getattr(backend, "iter_credentials", None))

    def is_available(self) -> bool:
        try:
            return bool(self._backend.is_available())
        except Exception:
            return False

    def load_all(self) -> List[Credential]:
        itr = getattr(self._backend, "iter_credentials", None)
        if not callable(itr):
            return []
        out: List[Credential] = []
        try:
            rows = itr() or []
        except Exception:
            logger.debug("iter_credentials failed for %s", self.name, exc_info=True)
            return []
        for attributes, value in rows:
            if not value:
                continue
            out.append(credential_from_attributes(attributes, value, self.name))
        return out

    def save(self, credential: Credential) -> bool:
        if credential.secret is None:
            return False
        return bool(self._backend.store(credential_to_spec(credential), credential.secret))

    def delete(self, credential: Credential) -> bool:
        return bool(self._backend.delete(credential_to_spec(credential)))


class KdbxAdapter(BackendAdapter):
    """Standalone KeePass (``.kdbx``) export/import target via ``pykeepass``.

    Constructed with a database path + master password (+ optional key file); opens lazily.
    ``load_all()`` enumerates every entry (mapped to a Credential; the sshPilot type is
    preserved via a custom property when we wrote it, else defaults to a password).
    ``save``/``delete`` write to a dedicated group (default ``sshPilot``) and persist the file.
    """

    name = "keepassxc"
    can_enumerate = True

    def __init__(self, path, password=None, keyfile=None, *, group="sshPilot"):
        self._path = path
        self._password = password
        self._keyfile = keyfile
        self._group_name = group
        self._kp = None

    def is_available(self) -> bool:
        return PyKeePass is not None and bool(self._path)

    def _db(self):
        if self._kp is None:
            if PyKeePass is None:
                raise RuntimeError("pykeepass is not installed")
            self._kp = PyKeePass(self._path, password=self._password, keyfile=self._keyfile)
        return self._kp

    @staticmethod
    def _custom(entry, key) -> Optional[str]:
        getter = getattr(entry, "get_custom_property", None)
        if callable(getter):
            try:
                return getter(key)
            except Exception:
                return None
        return None

    @staticmethod
    def _host_user(entry):
        username = entry.username or ""
        host = ""
        url = entry.url or ""
        if url:
            rest = url.split("://", 1)[-1]                 # ssh://user@host[:port][/...]
            if "@" in rest:
                rest = rest.split("@", 1)[1]
            host = rest.split("/", 1)[0].split(":", 1)[0]
        return host, username

    def load_all(self) -> List[Credential]:
        kp = self._db()
        out: List[Credential] = []
        for entry in (kp.entries or []):
            value = entry.password
            if not value:
                continue
            ctype = self._custom(entry, _KDBX_TYPE_PROP) or ""
            title = entry.title or ""
            if ctype == TYPE_KEY:
                out.append(Credential(
                    id=title, type=TYPE_KEY, secret=value,
                    metadata={"backend": self.name, "title": title, "key_path": title}))
                continue
            host, username = self._host_user(entry)
            account = title or (f"{username}@{host}" if username and host else "")
            out.append(Credential(
                id=account, type=(ctype or "password"),
                host=host or None, username=username or None, secret=value,
                metadata={"backend": self.name, "title": title, "url": entry.url}))
        return out

    def _group(self, kp):
        grp = kp.find_groups(name=self._group_name, first=True)
        if grp is None:
            grp = kp.add_group(kp.root_group, self._group_name)
        return grp

    def save(self, credential: Credential) -> bool:
        if credential.secret is None:
            return False
        kp = self._db()
        title = credential.id
        entry = kp.find_entries(title=title, first=True)
        if entry is None:
            entry = kp.add_entry(self._group(kp), title,
                                 credential.username or "", credential.secret)
        else:
            entry.password = credential.secret
            if credential.username:
                entry.username = credential.username
        if credential.host and credential.username:
            entry.url = f"ssh://{credential.username}@{credential.host}"
        setter = getattr(entry, "set_custom_property", None)
        if callable(setter):
            try:
                setter(_KDBX_TYPE_PROP, credential.type)
            except Exception:
                pass
        kp.save()
        return True

    def delete(self, credential: Credential) -> bool:
        kp = self._db()
        entry = kp.find_entries(title=credential.id, first=True)
        if entry is None:
            return False
        kp.delete_entry(entry)
        kp.save()
        return True

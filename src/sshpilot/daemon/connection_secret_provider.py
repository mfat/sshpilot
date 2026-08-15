"""Daemon connection-secret provider.

The core ``ConnectionApplicationService`` delegates password/passphrase/plugin
secret operations to an injected secret provider; this module is the daemon's
implementation. It resolves connections through a headless
``ConnectionRecord`` resolver and persists secrets through the legacy secret
subsystem (``secret_storage`` / ``credential_model`` / ``askpass_utils``).

This is M5 compatibility debt: the provider may use the existing secret
subsystem until the M5 migration owns secret storage. No secret value is ever
logged, serialized to the API, or included in ``repr``.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional

from ..api.models.connections import ConnectionId
from ..core.connections.models import ConnectionRecord


def _string(value: Any) -> str:
    return str(value or "").strip()


class DaemonConnectionSecretProvider:
    """Secret contract implementation backed by the legacy secret subsystem."""

    def __init__(
        self,
        resolver: Callable[[ConnectionId], Optional[ConnectionRecord]],
        *,
        secret_manager_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        if resolver is None:
            raise ValueError("a connection resolver is required")
        self._resolver = resolver
        if secret_manager_factory is None:
            from ..secret_storage import get_secret_manager

            secret_manager_factory = get_secret_manager
        self._secret_manager_factory = secret_manager_factory
        self._session_passwords: Dict[ConnectionId, tuple[float, str]] = {}
        self._session_password_lock = threading.RLock()
        self._session_password_ttl = 3600.0

    # -- connection passwords ------------------------------------------------

    def _record(self, connection_id: ConnectionId) -> Optional[ConnectionRecord]:
        return self._resolver(connection_id)

    def _record_dict(self, record: ConnectionRecord) -> Dict[str, Any]:
        """Compatibility mapping for ``credential_model`` host helpers."""
        return {
            "hostname": _string(getattr(record, "hostname", "")),
            "host": _string(getattr(record, "host", "")) or _string(record.nickname),
            "nickname": _string(record.nickname),
            "username": _string(getattr(record, "username", "")),
        }

    def lookup_connection_password(
        self, connection_id: ConnectionId
    ) -> Optional[str]:
        record = self._record(connection_id)
        if record is None:
            return None
        now = time.monotonic()
        with self._session_password_lock:
            session_value = self._session_passwords.get(connection_id)
            if session_value is not None:
                expires, value = session_value
                if expires > now:
                    return value
                self._session_passwords.pop(connection_id, None)
        user = _string(record.username)
        if not user:
            return None
        from ..credential_model import canonical_password_host, password_host_candidates
        from ..secret_storage import password_spec

        manager = self._secret_manager_factory()
        conn = self._record_dict(record)
        canonical = canonical_password_host(conn)
        for host in password_host_candidates(conn) or ([canonical] if canonical else []):
            if not host:
                continue
            value = manager.lookup(password_spec(host, user))
            if value:
                if canonical and host != canonical:
                    try:
                        if manager.store(password_spec(canonical, user), value):
                            manager.delete(password_spec(host, user))
                    except Exception:
                        pass
                return value
        return None

    def set_session_connection_password(
        self, connection_id: ConnectionId, password: bytearray
    ) -> bool:
        """Keep a credential in daemon memory only until expiry/restart."""
        try:
            record = self._record(connection_id)
            if record is None or type(password) is not bytearray:
                return False
            value = password.decode("utf-8")
            if not value or "\x00" in value:
                return False
            with self._session_password_lock:
                self._session_passwords[connection_id] = (
                    time.monotonic() + self._session_password_ttl,
                    value,
                )
            return True
        except UnicodeDecodeError:
            return False
        finally:
            if isinstance(password, bytearray):
                password[:] = b"\0" * len(password)
                password.clear()

    def clear_session_connection_password(self, connection_id: ConnectionId) -> bool:
        """Forget a daemon-memory-only credential without touching persistence."""
        with self._session_password_lock:
            self._session_passwords.pop(connection_id, None)
        return True

    def has_connection_password(self, connection_id: ConnectionId) -> bool:
        return self.lookup_connection_password(connection_id) is not None

    def store_connection_password(
        self,
        connection_id: ConnectionId,
        password: str,
        *,
        previous_hostname: str = "",
        previous_host: str = "",
        previous_username: str = "",
    ) -> bool:
        record = self._record(connection_id)
        if record is None:
            return False
        from ..credential_model import canonical_password_host, password_host_candidates
        from ..secret_storage import password_spec

        # The primary key uses the record's *current* identity. The previous_*
        # fields describe the old identity and only feed the cleanup set.
        user = _string(record.username).strip()
        if not user or not password:
            return False
        conn = self._record_dict(record)
        canonical = canonical_password_host(conn)
        if not canonical:
            return False
        manager = self._secret_manager_factory()
        stored = manager.store(password_spec(canonical, user), password)
        if stored:
            cleanup = {
                (host, user)
                for host in password_host_candidates(conn)
                if host and host != canonical
            }
            if previous_hostname or previous_host:
                prev_user = _string(previous_username) or user
                prev_host = _string(previous_hostname) or _string(previous_host)
                if prev_host and (prev_host != canonical or prev_user != user):
                    cleanup.add((prev_host, prev_user))
            for host, cleanup_user in cleanup:
                try:
                    manager.delete(password_spec(host, cleanup_user))
                except Exception:
                    pass
        if stored:
            self.clear_session_connection_password(connection_id)
        return bool(stored)

    def delete_connection_password(
        self,
        connection_id: ConnectionId,
        *,
        previous_hostname: str = "",
        previous_host: str = "",
        previous_username: str = "",
    ) -> bool:
        record = self._record(connection_id)
        from ..credential_model import password_host_candidates
        from ..secret_storage import password_spec

        manager = self._secret_manager_factory()
        if record is not None:
            # Delete under the record's current username (the primary identity).
            user = _string(record.username)
            conn = self._record_dict(record)
            if user:
                for host in password_host_candidates(conn):
                    if host:
                        manager.delete(password_spec(host, user))
        if previous_hostname or previous_host:
            prev_host = _string(previous_hostname) or _string(previous_host)
            prev_user = _string(previous_username) or (
                _string(record.username) if record is not None else ""
            )
            if prev_host and prev_user:
                try:
                    manager.delete(password_spec(prev_host, prev_user))
                except Exception:
                    pass
        # Idempotent by contract: an absent credential satisfies the request.
        self.clear_session_connection_password(connection_id)
        return True

    # -- key passphrases ------------------------------------------------------

    def lookup_key_passphrase(self, key_path: str) -> Optional[str]:
        from ..askpass_utils import lookup_passphrase

        if not key_path:
            return None
        passphrase = lookup_passphrase(key_path)
        return passphrase if passphrase else None

    def has_key_passphrase(self, key_path: str) -> bool:
        return self.lookup_key_passphrase(key_path) is not None

    def store_key_passphrase(self, key_path: str, passphrase: str) -> bool:
        from ..askpass_utils import store_passphrase

        if not key_path or not isinstance(passphrase, str):
            return False
        return bool(store_passphrase(key_path, passphrase))

    def delete_key_passphrase(self, key_path: str) -> bool:
        from ..askpass_utils import clear_passphrase

        if not key_path:
            return True
        # Idempotent without masking a failed backend deletion: a missing value
        # is already in the requested state; False for a known value is a real
        # persistence failure.
        existing = self.lookup_key_passphrase(key_path)
        if existing is None:
            return True
        return bool(clear_passphrase(key_path))

    # -- plugin secrets --------------------------------------------------------

    @staticmethod
    def _plugin_secret_host(plugin_id: str) -> str:
        return f"sshpilot-plugin/{_string(plugin_id)}"

    def store_plugin_secret(self, plugin_id: str, key: str, value: str) -> bool:
        from ..secret_storage import password_spec

        if not plugin_id or not key or not isinstance(value, str):
            return False
        manager = self._secret_manager_factory()
        return bool(manager.store(password_spec(self._plugin_secret_host(plugin_id), key), value))

    def get_plugin_secret(self, plugin_id: str, key: str) -> Optional[str]:
        from ..secret_storage import password_spec

        if not plugin_id or not key:
            return None
        manager = self._secret_manager_factory()
        return manager.lookup(password_spec(self._plugin_secret_host(plugin_id), key))

    def delete_plugin_secret(self, plugin_id: str, key: str) -> bool:
        from ..secret_storage import password_spec

        if not plugin_id or not key:
            return True
        manager = self._secret_manager_factory()
        return bool(manager.delete(password_spec(self._plugin_secret_host(plugin_id), key)))

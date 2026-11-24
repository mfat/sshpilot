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

import logging
import threading
import time
from typing import Any, Callable, Dict, Iterable, Mapping, Optional

from ..api.models.connections import ConnectionId
from ..core.connections.models import ConnectionRecord

logger = logging.getLogger(__name__)


def _string(value: Any) -> str:
    return str(value or "").strip()


#: Connection metadata key. ``False`` means the user cleared this connection's
#: saved password: the per-host keyring entry is not used for it. The entry is
#: keyed only on ``username@host`` and so may be shared with other connections
#: (an SSH and an RDP connection to one Windows host, two ports on one
#: machine), which is why clearing cannot always delete it.
USE_SAVED_LOGIN_KEY = "use_saved_login"


class DaemonConnectionSecretProvider:
    """Secret contract implementation backed by the legacy secret subsystem."""

    def __init__(
        self,
        resolver: Callable[[ConnectionId], Optional[ConnectionRecord]],
        *,
        secret_manager_factory: Optional[Callable[[], Any]] = None,
        profile_password_lookup: Optional[Callable[[ConnectionId], Optional[str]]] = None,
        records: Optional[Callable[[], Iterable[ConnectionRecord]]] = None,
        metadata_lookup: Optional[Callable[[ConnectionId], Mapping[str, Any]]] = None,
        metadata_update: Optional[Callable[[ConnectionId, Mapping[str, Any]], Any]] = None,
    ) -> None:
        if resolver is None:
            raise ValueError("a connection resolver is required")
        self._resolver = resolver
        if secret_manager_factory is None:
            from ..secret_storage import get_secret_manager

            secret_manager_factory = get_secret_manager
        self._secret_manager_factory = secret_manager_factory
        # A linked login profile's password is shared by all its connections
        # and takes precedence over a per-host entry.
        self._profile_password_lookup = profile_password_lookup
        # Without these the provider cannot see other connections or record
        # an opt-out, and keeps the old behaviour: delete, and always autofill.
        self._records = records
        self._metadata_lookup = metadata_lookup
        self._metadata_update = metadata_update
        self._session_passwords: Dict[ConnectionId, tuple[float, str]] = {}
        self._session_password_lock = threading.RLock()
        self._session_password_ttl = 3600.0

    # -- connection passwords ------------------------------------------------

    def _record(self, connection_id: ConnectionId) -> Optional[ConnectionRecord]:
        return self._resolver(connection_id)

    def _saved_login_off(self, connection_id: ConnectionId) -> bool:
        if self._metadata_lookup is None:
            return False
        try:
            metadata = self._metadata_lookup(connection_id) or {}
        except Exception:
            logger.debug("Connection metadata unavailable", exc_info=True)
            return False
        return metadata.get(USE_SAVED_LOGIN_KEY) is False

    def _set_saved_login_off(self, connection_id: ConnectionId, off: bool) -> None:
        if self._metadata_update is None or self._saved_login_off(connection_id) is off:
            return
        try:
            self._metadata_update(
                connection_id, {USE_SAVED_LOGIN_KEY: False if off else None}
            )
        except Exception:
            logger.warning(
                "Could not record the saved-password choice connection=%s",
                connection_id,
            )

    def _entry_used_elsewhere(self, host: str, user: str, record: ConnectionRecord) -> bool:
        """Whether a connection other than *record* reads ``user@host``.

        A connection reads every entry among its host candidates, so any of
        them matching counts -- unless that connection opted out.
        """
        if self._records is None:
            return False
        from ..credential_model import password_host_candidates

        try:
            others = tuple(self._records())
        except Exception:
            # Unknown is treated as shared: keeping a secret is recoverable,
            # deleting one another connection needs is not.
            logger.debug("Connection records unavailable", exc_info=True)
            return True
        for other in others:
            if other.id == record.id or _string(other.username) != user:
                continue
            if host not in password_host_candidates(self._record_dict(other)):
                continue
            if not self._saved_login_off(ConnectionId(other.id)):
                return True
        return False

    def _delete_unless_shared(
        self, manager: Any, host: str, user: str, record: Optional[ConnectionRecord]
    ) -> None:
        from ..secret_storage import password_spec

        if record is not None and self._entry_used_elsewhere(host, user, record):
            logger.info(
                "Connection password kept connection=%s: another connection "
                "uses the same saved login",
                record.id,
            )
            return
        manager.delete(password_spec(host, user))

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
        if self._profile_password_lookup is not None:
            try:
                profile_value = self._profile_password_lookup(connection_id)
            except Exception:
                logger.warning(
                    "Login profile password lookup failed connection=%s", connection_id
                )
                profile_value = None
            if profile_value:
                return profile_value
        if self._saved_login_off(connection_id):
            return None
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
                            self._delete_unless_shared(manager, host, user, record)
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
        # Every refusal below is logged: a password the user asked to save must
        # never fail silently, and the caller only sees a bare False.
        record = self._record(connection_id)
        if record is None:
            logger.warning(
                "Connection password not stored connection=%s: no such connection",
                connection_id,
            )
            return False
        from ..credential_model import canonical_password_host, password_host_candidates
        from ..secret_storage import password_spec

        # The primary key uses the record's *current* identity. The previous_*
        # fields describe the old identity and only feed the cleanup set.
        user = _string(record.username).strip()
        if not user:
            logger.warning(
                "Connection password not stored connection=%s: the connection "
                "has no username to key the secret on",
                connection_id,
            )
            return False
        if not password:
            logger.warning(
                "Connection password not stored connection=%s: the password is empty",
                connection_id,
            )
            return False
        conn = self._record_dict(record)
        canonical = canonical_password_host(conn)
        if not canonical:
            logger.warning(
                "Connection password not stored connection=%s user=%s: the "
                "connection has no resolvable host to key the secret on",
                connection_id,
                user,
            )
            return False
        manager = self._secret_manager_factory()
        stored = manager.store(password_spec(canonical, user), password)
        if not stored:
            # The manager names the backends it tried; name the connection.
            logger.warning(
                "Connection password not stored connection=%s target=%s@%s: "
                "secure storage rejected the write",
                connection_id,
                user,
                canonical,
            )
        else:
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
                    self._delete_unless_shared(manager, host, cleanup_user, record)
                except Exception:
                    pass
        if stored:
            self.clear_session_connection_password(connection_id)
            self._set_saved_login_off(connection_id, False)
        return bool(stored)

    def delete_connection_password(
        self,
        connection_id: ConnectionId,
        *,
        previous_hostname: str = "",
        previous_host: str = "",
        previous_username: str = "",
    ) -> bool:
        """Stop *connection_id* using a saved login password.

        The keyring entry is deleted only when no other connection reads it;
        either way the connection is marked so it stops reading it, which
        also keeps it from picking up an entry another connection saves later.
        """
        record = self._record(connection_id)
        from ..credential_model import password_host_candidates

        manager = self._secret_manager_factory()
        if record is not None:
            # Delete under the record's current username (the primary identity).
            user = _string(record.username)
            conn = self._record_dict(record)
            if user:
                for host in password_host_candidates(conn):
                    if host:
                        self._delete_unless_shared(manager, host, user, record)
        if previous_hostname or previous_host:
            prev_host = _string(previous_hostname) or _string(previous_host)
            prev_user = _string(previous_username) or (
                _string(record.username) if record is not None else ""
            )
            if prev_host and prev_user:
                try:
                    self._delete_unless_shared(manager, prev_host, prev_user, record)
                except Exception:
                    pass
        if record is not None:
            self._set_saved_login_off(connection_id, True)
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


class DaemonLoginProfileSecretStore:
    """Login profile secrets in the active secret backend.

    Profile secrets use the plugin-secret convention: a login password spec
    keyed on the pseudo host ``profile_secret_host(profile_id)``, so every
    secret backend, backup, and the Credential Manager handle them unchanged.
    """

    def __init__(self, secret_manager_factory: Optional[Callable[[], Any]] = None) -> None:
        if secret_manager_factory is None:
            from ..secret_storage import get_secret_manager

            secret_manager_factory = get_secret_manager
        self._secret_manager_factory = secret_manager_factory

    @staticmethod
    def _spec(profile_id: str, account: str):
        from ..core.login_profiles.models import profile_secret_host
        from ..secret_storage import password_spec

        return password_spec(profile_secret_host(profile_id), account)

    def store(self, profile_id: str, account: str, value: str) -> bool:
        return bool(self._secret_manager_factory().store(self._spec(profile_id, account), value))

    def lookup(self, profile_id: str, account: str) -> Optional[str]:
        return self._secret_manager_factory().lookup(self._spec(profile_id, account)) or None

    def delete(self, profile_id: str, account: str) -> bool:
        return bool(self._secret_manager_factory().delete(self._spec(profile_id, account)))

"""Read-only, daemon-backed connection presentation state for GTK."""

from __future__ import annotations

from threading import RLock
from typing import Callable, Dict, Iterable, Optional, Tuple
import logging

from sshpilot.api.events import CoreEvent, EventType
from sshpilot.api.models.connections import ConnectionSummary

_CONNECTION_EVENTS = frozenset({EventType.CONNECTION_CREATED, EventType.CONNECTION_UPDATED,
                                EventType.CONNECTION_DELETED})


class ConnectionPresentationStore:
    """Replaceable projection of one daemon instance's immutable DTO graph.

    The store has intentionally no configuration, secret-storage, migration,
    or process APIs. ``on_changed`` lets a GTK adapter replace its list model.
    """

    def __init__(self, *,
                 dispatch: Optional[Callable[[Callable[[], None]], object]] = None,
                 on_changed: Optional[Callable[[Tuple[ConnectionSummary, ...]], None]] = None):
        self._lock = RLock()
        self._by_id: Dict[str, ConnectionSummary] = {}
        self._client = None
        self._subscription = None
        self._generation = 0
        self._last_sequence = -1
        self._refresh_token = 0
        self._refresh_generation = 0
        self._pending_events = []
        self._on_changed = on_changed
        # Core clients publish events on their dispatch thread.  Frontends must
        # explicitly choose where observers run; the immediate default keeps
        # this toolkit-independent and makes headless tests deterministic.
        self._dispatch = dispatch or (lambda callback: callback())
        self._handlers = {}
        self._next_handler_id = 1

    @property
    def connections(self) -> Tuple[ConnectionSummary, ...]:
        return self.snapshot()

    def snapshot(self) -> Tuple[ConnectionSummary, ...]:
        with self._lock:
            return tuple(self._by_id.values())

    def get_connections(self) -> Tuple[ConnectionSummary, ...]:
        return self.snapshot()

    def get_connection_by_id(self, connection_id: str) -> Optional[ConnectionSummary]:
        with self._lock:
            return self._by_id.get(connection_id)

    get_connection_by_uuid = get_connection_by_id
    find_connection_by_nickname = get_connection_by_id

    def rebuild(self, connections: Iterable[ConnectionSummary]) -> None:
        """Atomically rebuild from daemon DTOs."""
        replacement: Dict[str, ConnectionSummary] = {}
        for connection in connections:
            if not isinstance(connection, ConnectionSummary):
                raise TypeError("presentation store only accepts ConnectionSummary DTOs")
            replacement[connection.id] = connection
        with self._lock:
            previous = self._by_id
            self._by_id = replacement
            snapshot = tuple(replacement.values())
        self._notify(snapshot)
        for connection_id in previous.keys() - replacement.keys():
            self._emit("connection-removed", previous[connection_id])
        for connection_id in replacement.keys() - previous.keys():
            self._emit("connection-added", replacement[connection_id])
        for connection_id in replacement.keys() & previous.keys():
            if replacement[connection_id] != previous[connection_id]:
                self._emit("connection-updated", replacement[connection_id])

    def connect_after(self, signal_name: str, callback: Callable) -> int:
        """Small signal adapter retained for GTK presentation consumers."""
        handler_id = self._next_handler_id
        self._next_handler_id += 1
        self._handlers[handler_id] = (signal_name, callback)
        return handler_id

    def disconnect(self, handler_id: int) -> None:
        self._handlers.pop(handler_id, None)

    def attach_client(self, client) -> Tuple[ConnectionSummary, ...]:
        """Subscribe to a daemon and perform a full authoritative refresh."""
        with self._lock:
            old_subscription = self._subscription
            self._subscription = None
            self._generation += 1
            generation = self._generation
            self._client = client
            self._last_sequence = -1
            self._refresh_generation = generation
            self._pending_events = []
        if old_subscription is not None:
            old_subscription.unsubscribe()
        instance_id = getattr(client, "server_instance_id", None)
        subscription = client.subscribe_events(
            lambda event: self._accept_event(event, generation, instance_id)
        )
        try:
            snapshot = self._resynchronize(client, generation, instance_id, None)
        except BaseException:
            subscription.unsubscribe()
            raise
        with self._lock:
            if generation != self._generation:
                subscription.unsubscribe()
            else:
                self._subscription = subscription
        return snapshot

    def refresh(self) -> Tuple[ConnectionSummary, ...]:
        with self._lock:
            client = self._client
            generation = self._generation
            instance_id = getattr(client, "server_instance_id", None)
            if client is not None:
                self._refresh_generation = generation
                self._refresh_token += 1
                token = self._refresh_token
                self._pending_events = []
            else:
                return self.snapshot()

        try:
            return self._resynchronize(client, generation, instance_id, token)
        except BaseException:
            # A failed snapshot must not strand the store in buffering mode.
            with self._lock:
                pending = tuple(self._pending_events)
                if self._refresh_generation == generation and self._refresh_token == token:
                    self._pending_events = []
                    self._refresh_generation = 0
            for event in sorted(pending, key=lambda item: item.sequence):
                self._accept_event(event, generation, instance_id)
            raise

    def _resynchronize(self, client, generation: int,
                       instance_id: Optional[str],
                       token: Optional[int] = None) -> Tuple[ConnectionSummary, ...]:
        """Replace a snapshot and buffered events without a live-mode gap."""
        connections = client.list_connections()
        replacement: Dict[str, ConnectionSummary] = {}
        for connection in connections:
            if not isinstance(connection, ConnectionSummary):
                raise TypeError("presentation store only accepts ConnectionSummary DTOs")
            replacement[connection.id] = connection

        with self._lock:
            if generation != self._generation or client is not self._client:
                return tuple(self._by_id.values())
            if token is not None and self._refresh_token != token:
                return tuple(self._by_id.values())
            previous = self._by_id
            self._by_id = replacement
            # Keep the lock while draining. Event callbacks arriving during the
            # replay block here and observe live mode only after replay ends.
            pending = sorted(self._pending_events, key=lambda item: item.sequence)
            self._pending_events = []
            for event in pending:
                self._apply_event_locked(event)
            self._refresh_generation = 0
            current = dict(self._by_id)
            snapshot = tuple(current.values())

        self._publish_projection_change(previous, current, snapshot, is_resync=True)
        return snapshot

    def _publish_projection_change(self, previous, replacement, snapshot, is_resync=False) -> None:
        self._notify(snapshot)
        if is_resync:
            self._emit("projection-reset", None)
            return
        for connection_id in previous.keys() - replacement.keys():
            self._emit("connection-removed", previous[connection_id])
        for connection_id in replacement.keys() - previous.keys():
            self._emit("connection-added", replacement[connection_id])
        for connection_id in replacement.keys() & previous.keys():
            if replacement[connection_id] != previous[connection_id]:
                self._emit("connection-updated", replacement[connection_id])

    def close(self) -> None:
        with self._lock:
            self._generation += 1
            subscription, self._subscription = self._subscription, None
            self._client = None
        if subscription is not None:
            subscription.unsubscribe()

    def _accept_event(self, event: CoreEvent, generation: int, instance_id: Optional[str]) -> None:
        if event.type not in _CONNECTION_EVENTS:
            return
        with self._lock:
            current_instance = getattr(self._client, "server_instance_id", None)
            if generation != self._generation or current_instance != instance_id:
                return
            if self._refresh_generation == generation:
                self._pending_events.append(event)
                return
            if not self._apply_event_locked(event):
                return
            signal_name = ("connection-removed" if event.type is EventType.CONNECTION_DELETED
                           else "connection-added" if event.type is EventType.CONNECTION_CREATED
                           else "connection-updated")
            snapshot = tuple(self._by_id.values())
        self._notify(snapshot)
        self._emit(signal_name, event.payload)

    def _apply_event_locked(self, event: CoreEvent) -> bool:
        if event.sequence <= self._last_sequence:
            return False
        self._last_sequence = event.sequence
        if event.type is EventType.CONNECTION_DELETED:
            self._by_id.pop(event.payload.id, None)
        else:
            self._by_id[event.payload.id] = event.payload
        return True

    def _notify(self, snapshot: Tuple[ConnectionSummary, ...]) -> None:
        if self._on_changed is not None:
            with self._lock:
                gen = self._generation
            def wrapped():
                with self._lock:
                    if self._generation != gen: return
                self._on_changed(snapshot)
            self._dispatch(wrapped)

    def _emit(self, signal_name: str, connection: Optional[ConnectionSummary]) -> None:
        with self._lock:
            handlers = tuple(self._handlers.values())
            gen = self._generation
        for registered_name, callback in handlers:
            if registered_name == signal_name:
                def wrapped(cb=callback, c=connection):
                    with self._lock:
                        if self._generation != gen: return
                    cb(self, c)
                self._dispatch(wrapped)

    def store_password(self, host: str, username: str, password: str):
        """Store a password via the selected secret backend (see secret_storage)."""
        from ..secret_storage import get_secret_manager, password_spec
        stored = get_secret_manager().store(password_spec(host, username), password)
        if not stored:
            logging.getLogger(__name__).warning("No secure storage backend available; password not stored")
        return stored

    def store_connection_password(self, connection, password: str,
                                  username: Optional[str] = None,
                                  previous_connection=None) -> bool:
        """Store a connection's SSH password under the canonical host key.

        Clears legacy copies stored under older host aliases (nickname, etc.) and,
        when an edited connection changed host or username, its previous identity.
        """
        from ..credential_model import canonical_password_host, password_host_candidates

        user = (username or getattr(connection, 'username', '') or '').strip()
        canonical = canonical_password_host(connection)
        if not canonical or not user or not password:
            return False
        stored = self.store_password(canonical, user, password)
        if stored:
            cleanup = {
                (host, user)
                for host in password_host_candidates(connection)
                if host and host != canonical
            }
            if previous_connection:
                previous_user = (
                    previous_connection.get('username')
                    if isinstance(previous_connection, dict)
                    else getattr(previous_connection, 'username', '')
                ) or user
                cleanup.update(
                    (host, previous_user)
                    for host in password_host_candidates(previous_connection)
                    if host and (host != canonical or previous_user != user)
                )
            for host, cleanup_user in cleanup:
                self.delete_password(host, cleanup_user)
        return stored

    def get_password(self, host: str, username: str) -> Optional[str]:
        """Retrieve a password via the selected secret backend."""
        from ..secret_storage import get_secret_manager, password_spec
        return get_secret_manager().lookup(password_spec(host, username))

    def get_connection_password(self, connection,
                                username: Optional[str] = None) -> Optional[str]:
        """Look up a connection's SSH password, migrating legacy host aliases on hit."""
        from ..credential_model import canonical_password_host, password_host_candidates

        user = (username or getattr(connection, 'username', '') or '').strip()
        if not user:
            return None
        canonical = canonical_password_host(connection)
        for host in password_host_candidates(connection) or ([canonical] if canonical else []):
            if not host:
                continue
            value = self.get_password(host, user)
            if value:
                if canonical and host != canonical:
                    if self.store_password(canonical, user, value):
                        self.delete_password(host, user)
                return value
        return None

    def delete_password(self, host: str, username: str) -> bool:
        """Delete a stored password from all available secret backends."""
        from ..secret_storage import get_secret_manager, password_spec
        removed_any = get_secret_manager().delete(password_spec(host, username))
        if removed_any:
            logging.getLogger(__name__).debug(f"Deleted stored password for {username}@{host}")
        return removed_any

    def delete_connection_passwords(self, connection,
                                    username: Optional[str] = None) -> bool:
        """Delete a connection's SSH password from every host alias and backend."""
        from ..credential_model import password_host_candidates

        user = (username or getattr(connection, 'username', '') or '').strip()
        removed = False
        for host in password_host_candidates(connection):
            if host and user and self.delete_password(host, user):
                removed = True
        return removed

    # --- Plugin secrets ----------------------------------------------------
    #
    # Namespaced per plugin id so a plugin can never read another plugin's
    # (or a connection's) secrets. Reuses the store_password() path — which routes
    # through the configurable secret backend (see secret_storage.py) — with a
    # reserved host identifier: real SSH hosts are stored under their hostname,
    # plugin secrets under 'sshpilot-plugin/<id>'.

    @staticmethod
    def _plugin_secret_host(plugin_id: str) -> str:
        return f"sshpilot-plugin/{plugin_id}"

    def store_plugin_secret(self, plugin_id: str, key: str, value: str) -> bool:
        return bool(self.store_password(self._plugin_secret_host(plugin_id), key, value))

    def get_plugin_secret(self, plugin_id: str, key: str) -> Optional[str]:
        return self.get_password(self._plugin_secret_host(plugin_id), key)

    def delete_plugin_secret(self, plugin_id: str, key: str) -> bool:
        return self.delete_password(self._plugin_secret_host(plugin_id), key)

"""Read-only, daemon-backed connection presentation state for GTK."""

from __future__ import annotations

from threading import RLock
from typing import Callable, Dict, Iterable, Optional, Tuple

from sshpilot.api.events import CoreEvent, EventType
from sshpilot.api.models.connections import ConnectionSummary

_CONNECTION_EVENTS = frozenset({EventType.CONNECTION_CREATED, EventType.CONNECTION_UPDATED,
                                EventType.CONNECTION_DELETED})


class ConnectionPresentationStore:
    """Replaceable projection of one daemon instance's immutable DTO graph.

    The store has intentionally no configuration, secret-storage, migration,
    or process APIs. ``on_changed`` lets a GTK adapter replace its list model.
    """

    def __init__(self, *, on_changed: Optional[Callable[[Tuple[ConnectionSummary, ...]], None]] = None):
        self._lock = RLock()
        self._by_id: Dict[str, ConnectionSummary] = {}
        self._client = None
        self._subscription = None
        self._generation = 0
        self._last_sequence = -1
        self._refresh_generation = 0
        self._pending_events = []
        self._on_changed = on_changed
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
            self.rebuild(client.list_connections())
        except BaseException:
            subscription.unsubscribe()
            raise
        with self._lock:
            pending = tuple(self._pending_events)
            self._pending_events = []
            self._refresh_generation = 0
            if generation != self._generation:
                subscription.unsubscribe()
            else:
                self._subscription = subscription
        for event in sorted(pending, key=lambda item: item.sequence):
            self._accept_event(event, generation, instance_id)
        return self.snapshot()

    def refresh(self) -> Tuple[ConnectionSummary, ...]:
        with self._lock:
            client = self._client
        if client is not None:
            self.rebuild(client.list_connections())
        return self.snapshot()

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
            if event.sequence <= self._last_sequence:
                return
            self._last_sequence = event.sequence
            if event.type is EventType.CONNECTION_DELETED:
                self._by_id.pop(event.payload.id, None)
                signal_name = "connection-removed"
            else:
                self._by_id[event.payload.id] = event.payload
                signal_name = ("connection-added" if event.type is EventType.CONNECTION_CREATED
                               else "connection-updated")
            snapshot = tuple(self._by_id.values())
        self._notify(snapshot)
        self._emit(signal_name, event.payload)

    def _notify(self, snapshot: Tuple[ConnectionSummary, ...]) -> None:
        if self._on_changed is not None:
            self._on_changed(snapshot)

    def _emit(self, signal_name: str, connection: ConnectionSummary) -> None:
        for registered_name, callback in tuple(self._handlers.values()):
            if registered_name == signal_name:
                callback(self, connection)

"""Read-only, daemon-backed connection presentation state for GTK."""

from __future__ import annotations

from threading import RLock
from typing import Callable, Iterable, Optional
from dataclasses import replace

from sshpilot.api.events import CoreEvent, EventType
from sshpilot.api.models.connection_store import ConnectionStoreSnapshot
from sshpilot.api.models.connections import (
    ConnectionId,
    ConnectionSummary,
)

_CONNECTION_EVENTS = frozenset(
    {
        EventType.CONNECTION_CREATED,
        EventType.CONNECTION_UPDATED,
        EventType.CONNECTION_DELETED,
        EventType.CONNECTION_STORE_CHANGED,
    }
)


class ConnectionPresentationStore:
    """Atomic projection of one daemon instance's immutable store snapshot."""

    def __init__(
        self,
        *,
        dispatch: Optional[Callable[[Callable[[], None]], object]] = None,
        on_changed: Optional[Callable[[ConnectionStoreSnapshot], None]] = None,
    ):
        self._lock = RLock()
        self._store_snapshot = ConnectionStoreSnapshot(
            generation=0,
            connections=(),
            groups=(),
            root_connection_ids=(),
            metadata=(),
        )
        self._client = None
        self._subscription = None
        self._attach_generation = 0
        self._last_sequence = -1
        self._buffering = False
        self._pending_events: list[CoreEvent] = []
        self._coherent_events = False
        self._on_changed = on_changed
        self._dispatch = dispatch or (lambda callback: callback())
        self._handlers = {}
        self._next_handler_id = 1

    def snapshot(self) -> ConnectionStoreSnapshot:
        with self._lock:
            return self._store_snapshot

    @property
    def connections(self):
        return self.snapshot().connections

    @property
    def groups(self):
        return self.snapshot().groups

    @property
    def root_connection_ids(self):
        return self.snapshot().root_connection_ids

    @property
    def metadata(self):
        return self.snapshot().metadata

    @property
    def generation(self) -> int:
        return self.snapshot().generation

    def get_connections(self):
        return self.connections

    def get_connection_by_id(self, connection_id: str) -> Optional[ConnectionSummary]:
        return next((item for item in self.connections if item.id == connection_id), None)

    get_connection_by_uuid = get_connection_by_id
    def find_connection_by_nickname(self, nickname: str) -> Optional[ConnectionSummary]:
        return next(
            (
                item for item in self.connections
                if item.id == nickname
                or item.ssh_alias == nickname
                or item.nickname == nickname
                or item.display_name == nickname
            ),
            None,
        )

    def get_metadata(self, connection_id: str):
        item = self.find_connection_by_nickname(connection_id) or self.get_connection_by_id(connection_id)
        key = item.id if item is not None else connection_id
        return next(
            (item.values for item in self.metadata if item.connection_id == key),
            {},
        )

    def get_connection_groups(self, connection_id: str):
        item = self.find_connection_by_nickname(connection_id) or self.get_connection_by_id(connection_id)
        key = item.id if item is not None else connection_id
        return tuple(
            group for group in self.groups if key in group.connection_ids
        )

    def get_group(self, group_id: str):
        return next((group for group in self.groups if group.id == group_id), None)

    def get_groups(self):
        return self.groups

    def rebuild(self, connections: Iterable[ConnectionSummary]) -> None:
        """Compatibility helper for callers that only have connection DTOs."""
        values = tuple(connections)
        if any(type(item) is not ConnectionSummary for item in values):
            raise TypeError("presentation store only accepts ConnectionSummary DTOs")
        snapshot = ConnectionStoreSnapshot(
            generation=self.generation,
            connections=values,
            groups=(),
            root_connection_ids=tuple(item.id for item in values),
            metadata=(),
        )
        self._install_snapshot(snapshot, emit_reset=False)

    def _install_snapshot(self, snapshot: ConnectionStoreSnapshot, *, emit_reset: bool):
        with self._lock:
            self._store_snapshot = snapshot
        self._notify(snapshot)
        if emit_reset:
            self._emit("projection-reset", None)

    def connect_after(self, signal_name: str, callback: Callable) -> int:
        handler_id = self._next_handler_id
        self._next_handler_id += 1
        self._handlers[handler_id] = (signal_name, callback)
        return handler_id

    def disconnect(self, handler_id: int) -> None:
        self._handlers.pop(handler_id, None)

    def attach_client(self, client) -> ConnectionStoreSnapshot:
        with self._lock:
            old_subscription = self._subscription
            self._subscription = None
            self._attach_generation += 1
            generation = self._attach_generation
            self._client = client
            self._last_sequence = -1
            self._buffering = True
            self._pending_events = []
        if old_subscription is not None:
            old_subscription.unsubscribe()
        instance_id = getattr(client, "server_instance_id", None)
        self._coherent_events = callable(
            getattr(client, "get_connection_store_snapshot", None)
        )
        subscription = client.subscribe_events(
            lambda event: self._accept_event(event, generation, instance_id)
        )
        try:
            snapshot = self._fetch_snapshot(client)
            self._install_fetched_snapshot(snapshot, client, generation, instance_id)
        except BaseException:
            with self._lock:
                self._client = None
                self._buffering = False
                self._pending_events = []
            subscription.unsubscribe()
            raise
        with self._lock:
            if generation != self._attach_generation:
                subscription.unsubscribe()
            else:
                self._subscription = subscription
        return self.snapshot()

    def refresh(self) -> ConnectionStoreSnapshot:
        with self._lock:
            client = self._client
            generation = self._attach_generation
            if client is None:
                return self.snapshot()
            instance_id = getattr(client, "server_instance_id", None)
            self._buffering = True
            self._pending_events = []
        try:
            snapshot = self._fetch_snapshot(client)
            self._install_fetched_snapshot(snapshot, client, generation, instance_id)
            return self.snapshot()
        except BaseException:
            with self._lock:
                pending = tuple(self._pending_events)
                self._pending_events = list(pending)
                self._buffering = False
            if not self._coherent_events:
                with self._lock:
                    self._pending_events = []
                for event in sorted(pending, key=lambda item: item.sequence):
                    self._accept_event(event, generation, instance_id)
            raise

    @staticmethod
    def _fetch_snapshot(client) -> ConnectionStoreSnapshot:
        getter = getattr(client, "get_connection_store_snapshot", None)
        if callable(getter):
            snapshot = getter()
        else:
            connections = tuple(client.list_connections())
            if any(type(item) is not ConnectionSummary for item in connections):
                raise TypeError("presentation store only accepts ConnectionSummary DTOs")
            snapshot = ConnectionStoreSnapshot(
                generation=0,
                connections=connections,
                groups=(),
                root_connection_ids=tuple(item.id for item in connections),
                metadata=(),
            )
        if type(snapshot) is not ConnectionStoreSnapshot:
            raise TypeError("daemon returned an invalid connection-store snapshot")
        return snapshot

    def _install_fetched_snapshot(self, snapshot, client, generation, instance_id):
        with self._lock:
            if generation != self._attach_generation or client is not self._client:
                return
            self._store_snapshot = snapshot
            pending = sorted(self._pending_events, key=lambda item: item.sequence)
            self._pending_events = []
            self._buffering = False
            if self._coherent_events:
                coherent = [
                    event for event in pending
                    if event.type is EventType.CONNECTION_STORE_CHANGED
                ]
                if coherent:
                    self._apply_event_locked(coherent[-1])
            else:
                for event in pending:
                    self._apply_event_locked(event)
            current = self._store_snapshot
        self._notify(current)
        self._emit("projection-reset", None)

    def close(self) -> None:
        with self._lock:
            self._attach_generation += 1
            subscription, self._subscription = self._subscription, None
            self._client = None
            self._buffering = False
            self._pending_events = []
        if subscription is not None:
            subscription.unsubscribe()

    def _accept_event(self, event: CoreEvent, generation: int, instance_id: Optional[str]):
        if event.type not in _CONNECTION_EVENTS:
            return
        with self._lock:
            if generation != self._attach_generation:
                return
            if getattr(self._client, "server_instance_id", None) != instance_id:
                return
            if self._buffering:
                self._pending_events.append(event)
                return
            if self._coherent_events and event.type is not EventType.CONNECTION_STORE_CHANGED:
                self._pending_events.append(event)
                return
            if self._coherent_events and event.type is EventType.CONNECTION_STORE_CHANGED:
                self._pending_events = []
            if not self._apply_event_locked(event):
                return
            snapshot = self._store_snapshot
        self._notify(snapshot)
        if event.type is EventType.CONNECTION_STORE_CHANGED:
            self._emit("projection-reset", None)
        else:
            signal = {
                EventType.CONNECTION_CREATED: "connection-added",
                EventType.CONNECTION_UPDATED: "connection-updated",
                EventType.CONNECTION_DELETED: "connection-removed",
            }[event.type]
            self._emit(signal, event.payload)

    def _apply_event_locked(self, event: CoreEvent) -> bool:
        if event.sequence <= self._last_sequence:
            return False
        self._last_sequence = event.sequence
        if event.type is EventType.CONNECTION_STORE_CHANGED:
            if type(event.payload) is not ConnectionStoreSnapshot:
                return False
            if event.payload.generation <= self._store_snapshot.generation:
                return False
            self._store_snapshot = event.payload
            return True
        current = self._store_snapshot
        values = list(current.connections)
        item_id = event.payload.id
        values = [item for item in values if item.id != item_id]
        if event.type is not EventType.CONNECTION_DELETED:
            if type(event.payload) is not ConnectionSummary:
                return False
            values.append(event.payload)
        root_ids = tuple(
            cid for cid in current.root_connection_ids if cid != item_id
        )
        if event.type is not EventType.CONNECTION_CREATED and item_id in {
            cid for group in current.groups for cid in group.connection_ids
        }:
            root_ids = current.root_connection_ids
        elif event.type is EventType.CONNECTION_CREATED and not event.payload.groups:
            root_ids = root_ids + (ConnectionId(item_id),)
        groups = current.groups
        if event.type is EventType.CONNECTION_DELETED:
            groups = tuple(
                replace(
                    group,
                    connection_ids=tuple(
                        cid for cid in group.connection_ids if cid != item_id
                    ),
                )
                for group in groups
            )
        metadata = current.metadata
        if event.type is EventType.CONNECTION_DELETED:
            metadata = tuple(
                item for item in current.metadata if item.connection_id != item_id
            )
        self._store_snapshot = ConnectionStoreSnapshot(
            generation=current.generation,
            connections=tuple(values),
            groups=groups,
            root_connection_ids=root_ids,
            metadata=metadata,
        )
        return True

    def _notify(self, snapshot: ConnectionStoreSnapshot) -> None:
        if self._on_changed is None:
            return
        with self._lock:
            generation = self._attach_generation

        def wrapped():
            with self._lock:
                if generation != self._attach_generation:
                    return
            self._on_changed(snapshot)

        self._dispatch(wrapped)

    def _emit(self, signal_name: str, connection) -> None:
        with self._lock:
            handlers = tuple(self._handlers.values())
            generation = self._attach_generation
        for registered_name, callback in handlers:
            if registered_name != signal_name:
                continue

            def wrapped(cb=callback, value=connection):
                with self._lock:
                    if generation != self._attach_generation:
                        return
                cb(self, value)

            self._dispatch(wrapped)

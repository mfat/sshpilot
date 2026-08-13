"""Tests for the daemon-backed read-only connection presentation store.

``ConnectionPresentationStore`` projects immutable ``ConnectionStoreSnapshot``
values from the daemon. It never mutates DTOs, never persists anything, and
does not discover SSH keys or read the SSH config (those belong to the key
service / repository).
"""

from datetime import datetime, timezone
from queue import Queue
from threading import Barrier, Thread

from sshpilot.api.events import CoreEvent, EventType
from sshpilot.api.models.connection_store import (
    ConnectionMetadataSummary,
    ConnectionStoreSnapshot,
    GroupSummary,
)
from sshpilot.api.models.connections import ConnectionSummary
from sshpilot.gtk.connection_store import ConnectionPresentationStore


def summary(connection_id, nickname=None):
    return ConnectionSummary(
        id=connection_id, nickname=nickname or connection_id, host=connection_id,
        hostname=f"{connection_id}.example", username="user", port=22,
    )


def group(group_id, name, connection_ids):
    return GroupSummary(
        id=group_id, name=name, connection_ids=tuple(connection_ids)
    )


def snapshot(connections, groups=(), root=None, metadata=(), generation=0):
    return ConnectionStoreSnapshot(
        generation=generation,
        connections=tuple(connections),
        groups=tuple(groups),
        root_connection_ids=(
            tuple(root)
            if root is not None
            else tuple(c.id for c in connections)
        ),
        metadata=tuple(metadata),
    )


class Subscription:
    def __init__(self):
        self.closed = False

    def unsubscribe(self):
        self.closed = True


class Client:
    """Event-stream client without a coherent snapshot getter (fallback path)."""

    def __init__(self, instance_id, connections):
        self.server_instance_id = instance_id
        self.connections = list(connections)
        self.callback = None
        self.subscription = Subscription()
        self.list_calls = 0

    def subscribe_events(self, callback):
        self.callback = callback
        return self.subscription

    def list_connections(self):
        self.list_calls += 1
        return list(self.connections)

    def emit(self, event_type, payload, sequence):
        self.callback(CoreEvent(event_type, payload, sequence,
                                datetime.now(timezone.utc)))


class SnapshotClient:
    """Coherent daemon client exposing an authoritative store snapshot."""

    def __init__(self, instance_id, snapshot):
        self.server_instance_id = instance_id
        self._snapshot = snapshot
        self.subscription = Subscription()
        self.callback = None

    def subscribe_events(self, callback):
        self.callback = callback
        return self.subscription

    def get_connection_store_snapshot(self):
        return self._snapshot


# ---------------------------------------------------------------------------
# Snapshot surface
# ---------------------------------------------------------------------------


def test_initial_snapshot_is_empty():
    store = ConnectionPresentationStore()
    snap = store.snapshot()
    assert isinstance(snap, ConnectionStoreSnapshot)
    assert snap.connections == ()
    assert snap.groups == ()
    assert snap.root_connection_ids == ()
    assert snap.metadata == ()


def test_store_rebuilds_entirely_from_immutable_daemon_dtos():
    store = ConnectionPresentationStore()
    dto = summary("one")
    store.rebuild([dto])

    snap = store.snapshot()
    assert isinstance(snap, ConnectionStoreSnapshot)
    assert snap.connections == (dto,)
    assert store.connections == (dto,)
    assert store.get_connection_by_id("one") is dto
    assert isinstance(store.connections, tuple)


def test_apply_snapshot_exposes_connections_groups_root_and_metadata():
    conns = (summary("one"), summary("two"))
    g1 = group("g1", "Prod", ["one"])
    meta = ConnectionMetadataSummary(connection_id="one", values={"tags": ["prod"]})
    snap = snapshot(conns, groups=(g1,), root=["two"], metadata=(meta,), generation=3)
    store = ConnectionPresentationStore()
    store.attach_client(SnapshotClient("daemon-a", snap))

    assert store.snapshot().generation == 3
    assert store.connections == conns
    assert store.groups == (g1,)
    assert store.root_connection_ids == ("two",)
    assert store.metadata == (meta,)


def test_lookup_by_id_and_nickname():
    store = ConnectionPresentationStore()
    store.rebuild([summary("one")])
    assert store.get_connection_by_id("one").nickname == "one"
    assert store.find_connection_by_nickname("one").id == "one"
    assert store.find_connection_by_nickname("missing") is None


def test_metadata_lookup():
    meta = ConnectionMetadataSummary(connection_id="one", values={"tags": ["prod"]})
    store = ConnectionPresentationStore()
    store.attach_client(SnapshotClient(
        "daemon-a",
        snapshot((summary("one"),), metadata=(meta,)),
    ))
    # Safe metadata is frozen on the way in: lists become tuples.
    assert store.get_metadata("one") == {"tags": ("prod",)}
    assert store.get_metadata("missing") == {}


def test_group_projection():
    g1 = group("g1", "Prod", ["one"])
    g2 = group("g2", "Web", ["one", "two"])
    store = ConnectionPresentationStore()
    store.attach_client(SnapshotClient(
        "daemon-a",
        snapshot((summary("one"), summary("two")), groups=(g1, g2), root=[]),
    ))
    assert [g.id for g in store.get_connection_groups("one")] == ["g1", "g2"]
    assert [g.id for g in store.get_connection_groups("two")] == ["g2"]
    assert store.get_group("g1") == g1
    assert store.get_group("missing") is None


def test_replacing_snapshot_removes_stale_rows():
    client = SnapshotClient(
        "daemon-a",
        snapshot((summary("one"), summary("two")), groups=(group("g1", "Prod", ["one"]),), root=["two"]),
    )
    store = ConnectionPresentationStore()
    store.attach_client(client)
    assert len(store.connections) == 2

    client._snapshot = snapshot((summary("two"),))
    store.refresh()
    assert [c.id for c in store.connections] == ["two"]
    assert store.groups == ()
    assert store.get_connection_by_id("one") is None


def test_connection_dtos_are_not_mutated_locally():
    dto = summary("one")
    store = ConnectionPresentationStore()
    store.rebuild([dto])
    store.get_connection_by_id("one")
    assert dto.id == "one"
    assert store.snapshot().connections[0] is dto


# ---------------------------------------------------------------------------
# Daemon event stream (fallback client path)
# ---------------------------------------------------------------------------


def test_daemon_events_update_presentation_store():
    changed = []
    client = Client("daemon-a", [summary("one")])
    store = ConnectionPresentationStore(on_changed=changed.append)
    store.attach_client(client)
    two = summary("two")

    client.emit(EventType.CONNECTION_CREATED, two, 1)
    client.emit(EventType.CONNECTION_DELETED, summary("one"), 2)

    assert store.snapshot().connections == (two,)
    assert isinstance(changed[-1], ConnectionStoreSnapshot)
    assert changed[-1].connections == (two,)


def test_reconnect_refreshes_and_ignores_old_daemon_events():
    old = Client("daemon-old", [summary("old")])
    new = Client("daemon-new", [summary("new")])
    store = ConnectionPresentationStore()
    store.attach_client(old)
    old_callback = old.callback

    store.attach_client(new)
    old_callback(CoreEvent(EventType.CONNECTION_CREATED, summary("stale"), 99,
                           datetime.now(timezone.utc)))

    assert old.subscription.closed
    assert new.list_calls == 1
    assert store.snapshot().connections == (summary("new"),)


def test_observers_are_marshaled_through_injected_dispatcher():
    scheduled = Queue()
    delivered = []
    client = Client("daemon-a", [])
    store = ConnectionPresentationStore(
        dispatch=scheduled.put,
        on_changed=delivered.append,
    )

    store.attach_client(client)
    assert delivered == []
    scheduled.get_nowait()()
    assert delivered[0].connections == ()

    worker = Thread(target=client.emit, args=(
        EventType.CONNECTION_CREATED, summary("worker"), 1,
    ))
    worker.start()
    worker.join()

    assert delivered == [delivered[0]]
    scheduled.get_nowait()()
    assert isinstance(delivered[-1], ConnectionStoreSnapshot)
    assert delivered[-1].connections == (summary("worker"),)


def test_coherent_store_event_updates_snapshot_and_emits_projection_reset():
    old = summary("old")
    new = summary("new")
    client = SnapshotClient("daemon-a", snapshot((old,), generation=1))
    store = ConnectionPresentationStore()
    store.attach_client(client)
    emitted = []
    store.connect_after(
        "projection-reset",
        lambda _store, value: emitted.append(value),
    )

    client._snapshot = snapshot((new,), generation=2)
    client.callback(
        CoreEvent(
            EventType.CONNECTION_STORE_CHANGED,
            client._snapshot,
            1,
            datetime.now(timezone.utc),
        )
    )

    assert store.snapshot().connections == (new,)
    assert emitted == [None]


def test_refresh_replays_event_arriving_while_snapshot_is_in_flight():
    entered = Barrier(2)
    release = Barrier(2)
    client = Client("daemon-a", [summary("old")])
    store = ConnectionPresentationStore()
    store.attach_client(client)
    original_list = client.list_connections

    def blocked_list():
        result = original_list()
        entered.wait()
        release.wait()
        return result

    client.list_connections = blocked_list
    refresh_thread = Thread(target=store.refresh)
    refresh_thread.start()
    entered.wait()
    client.emit(EventType.CONNECTION_DELETED, summary("old"), 10)
    client.emit(EventType.CONNECTION_CREATED, summary("new"), 11)
    release.wait()
    refresh_thread.join()

    assert store.snapshot().connections == (summary("new"),)


def test_refresh_emits_one_projection_reset_instead_of_item_signals():
    client = Client("daemon-a", [summary("old")])
    store = ConnectionPresentationStore()
    store.attach_client(client)
    emitted = []
    for signal_name in (
        "connection-added",
        "connection-removed",
        "connection-updated",
        "projection-reset",
    ):
        store.connect_after(
            signal_name,
            lambda _store, connection, name=signal_name: emitted.append(
                (name, connection)
            ),
        )

    client.connections = [summary("new")]
    store.refresh()

    assert emitted == [("projection-reset", None)]


# ---------------------------------------------------------------------------
# Boundary guarantees
# ---------------------------------------------------------------------------


def test_gtk_window_does_not_create_or_reload_backend_manager():
    source = open("src/sshpilot/window.py", encoding="utf-8").read()
    assert "ConnectionManager(" not in source
    assert ".load_ssh_config(" not in source
    assert "self._setup_ssh_config_monitor()" not in source


def test_store_does_not_discover_keys_or_touch_config():
    store = ConnectionPresentationStore()
    # Key discovery moved to the key service (covered in tests/core);
    # the presentation store exposes no such surface.
    assert not hasattr(store, "load_ssh_keys")

from datetime import datetime, timezone

import pytest

from sshpilot.api.events import CoreEvent, EventType
from sshpilot.api.models.connections import ConnectionSummary
from sshpilot.gtk.connection_store import ConnectionPresentationStore


def summary(connection_id, nickname=None):
    return ConnectionSummary(
        id=connection_id, nickname=nickname or connection_id, host=connection_id,
        hostname=f"{connection_id}.example", username="user", port=22,
    )


class Subscription:
    def __init__(self):
        self.closed = False

    def unsubscribe(self):
        self.closed = True


class Client:
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


def test_store_rebuilds_entirely_from_immutable_daemon_dtos():
    store = ConnectionPresentationStore()
    dto = summary("one")
    store.rebuild([dto])

    assert store.snapshot()[0].dto is dto
    assert store.get_connection_by_id("one").dto is dto
    assert isinstance(store.connections, tuple)
    store.get_connection_by_id("one").tags = ["Production"]
    assert store.get_connection_by_id("one").tags == ("Production",)
    with pytest.raises(AttributeError, match="read-only"):
        store.get_connection_by_id("one").hostname = "local-edit.example"


def test_daemon_events_update_presentation_store():
    changed = []
    client = Client("daemon-a", [summary("one")])
    store = ConnectionPresentationStore(on_changed=changed.append)
    store.attach_client(client)
    two = summary("two")

    client.emit(EventType.CONNECTION_CREATED, two, 1)
    client.emit(EventType.CONNECTION_DELETED, summary("one"), 2)

    assert tuple(item.dto for item in store.snapshot()) == (two,)
    assert tuple(item.dto for item in changed[-1]) == (two,)


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
    assert tuple(item.dto for item in store.snapshot()) == (summary("new"),)


def test_gtk_window_does_not_create_or_reload_backend_manager():
    source = open("src/sshpilot/window.py", encoding="utf-8").read()
    assert "ConnectionManager(" not in source
    assert ".load_ssh_config(" not in source
    assert "self._setup_ssh_config_monitor()" not in source

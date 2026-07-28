import socket
import threading
import time

import pytest

from sshpilot.api import DaemonClient, ErrorCode, EventType
from sshpilot.api.models import ConnectionSummary


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        threading.Event().wait(0.005)
    return bool(predicate())


@pytest.mark.parametrize(
    "signal_name,event_type",
    [
        ("connection-added", EventType.CONNECTION_CREATED),
        ("connection-updated", EventType.CONNECTION_UPDATED),
        ("connection-removed", EventType.CONNECTION_DELETED),
    ],
)
def test_connection_events_arrive_while_client_is_idle(
    daemon_factory,
    signal_name,
    event_type,
):
    server, manager = daemon_factory()
    client = DaemonClient(socket_path=server.socket_path)
    received = []
    delivered = threading.Event()
    subscription = client.subscribe_events(
        lambda event: (received.append(event), delivered.set())
    )

    manager.emit(signal_name, manager.connections[0])

    assert delivered.wait(2)
    assert received[0].type is event_type
    assert received[0].sequence == 0
    assert type(received[0].payload) is ConnectionSummary
    assert received[0].payload.nickname == "demo"
    assert "password" not in repr(received[0].payload)
    subscription.close()
    client.close()


def test_healthy_clients_receive_the_same_daemon_sequence(daemon_factory):
    server, manager = daemon_factory()
    clients = [
        DaemonClient(socket_path=server.socket_path),
        DaemonClient(socket_path=server.socket_path),
    ]
    received = [[], []]
    delivered = [threading.Event(), threading.Event()]
    subscriptions = [
        client.subscribe_events(
            lambda event, index=index: (
                received[index].append(event),
                delivered[index].set(),
            )
        )
        for index, client in enumerate(clients)
    ]

    manager.emit("connection-updated", manager.connections[0])

    assert all(event.wait(2) for event in delivered)
    assert [events[0].sequence for events in received] == [0, 0]
    assert received[0][0].payload == received[1][0].payload
    for subscription in subscriptions:
        subscription.close()
    for client in clients:
        client.close()


def test_one_client_disconnect_does_not_affect_other_event_delivery(
    daemon_factory,
):
    server, manager = daemon_factory()
    disconnected = DaemonClient(socket_path=server.socket_path)
    healthy = DaemonClient(socket_path=server.socket_path)
    received = []
    delivered = threading.Event()
    healthy.subscribe_events(
        lambda event: (received.append(event), delivered.set())
    )

    disconnected.close()
    manager.emit("connection-added", manager.connections[0])

    assert delivered.wait(2)
    assert received[0].type is EventType.CONNECTION_CREATED
    healthy.close()


def test_handshake_incomplete_peer_receives_no_runtime_events(daemon_factory):
    server, manager = daemon_factory()
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    peer.settimeout(0.1)
    peer.connect(str(server.socket_path))

    manager.emit("connection-added", manager.connections[0])

    with pytest.raises(socket.timeout):
        peer.recv(1)
    peer.close()


def test_new_client_gets_no_replay_and_starts_at_current_global_sequence(
    daemon_factory,
):
    server, manager = daemon_factory()
    manager.emit("connection-updated", manager.connections[0])

    client = DaemonClient(socket_path=server.socket_path)
    received = []
    delivered = threading.Event()
    client.subscribe_events(
        lambda event: (received.append(event), delivered.set())
    )
    assert client.list_connections()[0].nickname == "demo"

    manager.emit("connection-updated", manager.connections[0])

    assert delivered.wait(2)
    assert [event.sequence for event in received] == [1]
    client.close()


def test_event_and_response_share_one_ordered_transport_stream(daemon_factory):
    server, manager = daemon_factory()
    emit_during_read = True

    def get_connections():
        nonlocal emit_during_read
        if emit_during_read:
            emit_during_read = False
            manager.emit("connection-updated", manager.connections[0])
        return list(manager.connections)

    manager.get_connections = get_connections
    client = DaemonClient(socket_path=server.socket_path)
    received = []
    delivered = threading.Event()
    subscription = client.subscribe_events(
        lambda event: (received.append(event), delivered.set())
    )

    emit_during_read = True
    listed = client.list_connections()

    assert delivered.wait(2)
    assert listed[0].nickname == "demo"
    assert received[0].type is EventType.CONNECTION_UPDATED
    assert client.list_connections()[0] == listed[0]
    subscription.close()
    client.close()


def test_slow_subscriber_does_not_block_response_reader(daemon_factory):
    server, manager = daemon_factory()
    client = DaemonClient(socket_path=server.socket_path)
    entered = threading.Event()
    release = threading.Event()

    def block(_event):
        entered.set()
        assert release.wait(2)

    subscription = client.subscribe_events(block)
    manager.emit("connection-updated", manager.connections[0])
    assert entered.wait(2)

    started = time.monotonic()
    listed = client.list_connections()
    elapsed = time.monotonic() - started

    release.set()
    assert listed[0].nickname == "demo"
    assert elapsed < 1
    subscription.close()
    client.close()


def test_subscriber_failure_does_not_stop_events_or_responses(
    daemon_factory,
    caplog,
):
    server, manager = daemon_factory()
    client = DaemonClient(socket_path=server.socket_path)
    received = []
    delivered = threading.Event()

    def fail(_event):
        raise RuntimeError("deliberate subscriber failure")

    client.subscribe_events(fail)
    subscription = client.subscribe_events(
        lambda event: (received.append(event), delivered.set())
    )

    manager.emit("connection-added", manager.connections[0])
    assert delivered.wait(2)
    assert client.list_connections()[0].nickname == "demo"
    assert "deliberate subscriber failure" in caplog.text
    subscription.close()
    client.close()


def test_close_from_subscriber_callback_does_not_deadlock(daemon_factory):
    server, manager = daemon_factory()
    client = DaemonClient(socket_path=server.socket_path)
    closed = threading.Event()

    def close_client(_event):
        client.close()
        closed.set()

    client.subscribe_events(close_client)
    manager.emit("connection-removed", manager.connections[0])

    assert closed.wait(2)
    assert _wait_until(lambda: client._close_complete)


def test_one_persistent_reader_owns_all_socket_receive(
    daemon_factory,
    monkeypatch,
):
    import sshpilot.api.daemon_client as daemon_client_module

    server, manager = daemon_factory()
    reader_threads = set()
    original_receive = daemon_client_module.receive_frame

    def record_receive(transport):
        reader_threads.add(threading.get_ident())
        return original_receive(transport)

    monkeypatch.setattr(
        daemon_client_module,
        "receive_frame",
        record_receive,
    )
    client = DaemonClient(socket_path=server.socket_path)
    delivered = threading.Event()
    client.subscribe_events(lambda _event: delivered.set())

    assert client.list_connections()
    manager.emit("connection-updated", manager.connections[0])
    assert delivered.wait(2)
    assert len(reader_threads) == 1
    assert threading.get_ident() not in reader_threads
    client.close()


def test_client_event_handoff_overflow_closes_continuity(daemon_factory):
    server, manager = daemon_factory()
    client = DaemonClient(
        socket_path=server.socket_path,
        event_dispatch_limit=1,
    )
    entered = threading.Event()
    release = threading.Event()
    received = []

    def block_first(event):
        received.append(event)
        if event.type is EventType.CONNECTION_UPDATED and not entered.is_set():
            entered.set()
            assert release.wait(2)

    client.subscribe_events(block_first)
    manager.emit("connection-updated", manager.connections[0])
    assert entered.wait(2)

    manager.emit("connection-updated", manager.connections[0])
    manager.emit("connection-updated", manager.connections[0])
    assert _wait_until(lambda: client._transport_failed)
    release.set()

    assert _wait_until(
        lambda: any(
            event.type is EventType.ERROR_OCCURRED for event in received
        )
    )
    assert received[-1].payload["code"] == ErrorCode.PROTOCOL_ERROR.value
    client.close()

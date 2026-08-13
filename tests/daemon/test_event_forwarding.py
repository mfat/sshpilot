import socket
import threading
import time

import pytest

from sshpilot.api import DaemonClient, ErrorCode, EventType
from sshpilot.api.models import ConnectionSummary
from sshpilot.api.models.connection_store import ConnectionStoreSnapshot
from sshpilot.daemon import DaemonServer
from tests.helpers.fake_connection_repository import FakeConnectionRepository, _record


def _make_daemon(tmp_path):
    repo = FakeConnectionRepository([_record()])
    socket_path = tmp_path / "daemon" / "sshpilotd.sock"
    socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _core_factory():
        from sshpilot.core.connection_application_service import ConnectionApplicationService

        return ConnectionApplicationService(repo, client_name="sshpilotd")

    server = DaemonServer(_core_factory, socket_path=socket_path)
    server.start_in_thread()
    return server, repo


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
    tmp_path,
    signal_name,
    event_type,
):
    server, repo = _make_daemon(tmp_path)
    client = DaemonClient(socket_path=server.socket_path)
    try:
        received = []
        delivered = threading.Event()
        subscription = client.subscribe_events(
            lambda event: (received.append(event), delivered.set())
        )

        if signal_name == "connection-added":
            repo.create_connection(
                {"nickname": "other", "hostname": "other.example", "username": "user", "port": 22}
            )
        elif signal_name == "connection-updated":
            repo.update_connection("demo", {"hostname": "updated.example"})
        else:  # connection-removed
            demo_id = client.list_connections()[0].id
            repo.delete_connection(str(demo_id))

        assert delivered.wait(2)
        assert received[0].type is event_type
        assert type(received[0].payload) is ConnectionSummary
        assert "password" not in repr(received[0].payload)
        subscription.close()
    finally:
        client.close()
        server.shutdown()
        server.wait_stopped()


def test_connection_store_changed_event_reaches_idle_client(tmp_path):
    server, repo = _make_daemon(tmp_path)
    client = DaemonClient(socket_path=server.socket_path)
    try:
        received = []
        delivered = threading.Event()
        subscription = client.subscribe_events(
            lambda event: (received.append(event), delivered.set())
        )

        repo.update_connection("demo", {"hostname": "updated.example"})

        assert delivered.wait(2)
        store_events = [
            event for event in received
            if event.type is EventType.CONNECTION_STORE_CHANGED
        ]
        assert store_events
        assert type(store_events[0].payload) is ConnectionStoreSnapshot
        assert store_events[0].payload.connections[0].hostname == "updated.example"
        subscription.close()
    finally:
        client.close()
        server.shutdown()
        server.wait_stopped()


def test_healthy_clients_receive_the_same_daemon_sequence(tmp_path):
    server, repo = _make_daemon(tmp_path)
    clients = [
        DaemonClient(socket_path=server.socket_path),
        DaemonClient(socket_path=server.socket_path),
    ]
    try:
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

        repo.update_connection("demo", {"hostname": "updated.example"})

        assert all(event.wait(2) for event in delivered)
        assert received[0][0].sequence == received[1][0].sequence
        assert received[0][0].payload == received[1][0].payload
        for subscription in subscriptions:
            subscription.close()
    finally:
        for client in clients:
            client.close()
        server.shutdown()
        server.wait_stopped()


def test_one_client_disconnect_does_not_affect_other_event_delivery(tmp_path):
    server, repo = _make_daemon(tmp_path)
    disconnected = DaemonClient(socket_path=server.socket_path)
    healthy = DaemonClient(socket_path=server.socket_path)
    try:
        received = []
        delivered = threading.Event()
        healthy.subscribe_events(
            lambda event: (received.append(event), delivered.set())
        )

        disconnected.close()
        repo.create_connection(
            {"nickname": "other", "hostname": "other.example", "username": "user", "port": 22}
        )

        assert delivered.wait(2)
        assert received[0].type is EventType.CONNECTION_CREATED
    finally:
        healthy.close()
        server.shutdown()
        server.wait_stopped()


def test_handshake_incomplete_peer_receives_no_runtime_events(tmp_path):
    server, repo = _make_daemon(tmp_path)
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    peer.settimeout(0.1)
    try:
        peer.connect(str(server.socket_path))

        repo.create_connection(
            {"nickname": "other", "hostname": "other.example", "username": "user", "port": 22}
        )

        with pytest.raises(socket.timeout):
            peer.recv(1)
    finally:
        peer.close()
        server.shutdown()
        server.wait_stopped()


def test_new_client_gets_no_replay_and_starts_at_current_global_sequence(tmp_path):
    server, repo = _make_daemon(tmp_path)
    try:
        repo.update_connection("demo", {"hostname": "updated.example"})

        client = DaemonClient(socket_path=server.socket_path)
        try:
            received = []
            delivered = threading.Event()
            client.subscribe_events(
                lambda event: (received.append(event), delivered.set())
            )
            assert client.list_connections()[0].nickname == "demo"

            repo.update_connection("demo", {"hostname": "updated-again.example"})

            assert delivered.wait(2)
            assert all(event.sequence >= 1 for event in received)
        finally:
            client.close()
    finally:
        server.shutdown()
        server.wait_stopped()


def test_event_and_response_share_one_ordered_transport_stream(tmp_path):
    server, repo = _make_daemon(tmp_path)
    client = DaemonClient(socket_path=server.socket_path)
    try:
        received = []
        delivered = threading.Event()
        subscription = client.subscribe_events(
            lambda event: (received.append(event), delivered.set())
        )

        listed = client.list_connections()

        repo.update_connection("demo", {"hostname": "updated.example"})

        assert delivered.wait(2)
        assert listed[0].nickname == "demo"
        assert received[0].type is EventType.CONNECTION_UPDATED
        subscription.close()
    finally:
        client.close()
        server.shutdown()
        server.wait_stopped()


def test_slow_subscriber_does_not_block_response_reader(tmp_path):
    server, repo = _make_daemon(tmp_path)
    client = DaemonClient(socket_path=server.socket_path)
    try:
        entered = threading.Event()
        release = threading.Event()

        def block(_event):
            entered.set()
            assert release.wait(2)

        subscription = client.subscribe_events(block)
        repo.update_connection("demo", {"hostname": "updated.example"})
        assert entered.wait(2)

        started = time.monotonic()
        listed = client.list_connections()
        elapsed = time.monotonic() - started

        release.set()
        assert listed[0].nickname == "demo"
        assert elapsed < 1
        subscription.close()
    finally:
        client.close()
        server.shutdown()
        server.wait_stopped()


def test_subscriber_failure_does_not_stop_events_or_responses(tmp_path, caplog):
    server, repo = _make_daemon(tmp_path)
    client = DaemonClient(socket_path=server.socket_path)
    try:
        received = []
        delivered = threading.Event()

        def fail(_event):
            raise RuntimeError("deliberate subscriber failure")

        client.subscribe_events(fail)
        subscription = client.subscribe_events(
            lambda event: (received.append(event), delivered.set())
        )

        repo.create_connection(
            {"nickname": "other", "hostname": "other.example", "username": "user", "port": 22}
        )
        assert delivered.wait(2)
        assert client.list_connections()[0].nickname == "demo"
        assert "deliberate subscriber failure" in caplog.text
        subscription.close()
    finally:
        client.close()
        server.shutdown()
        server.wait_stopped()


def test_close_from_subscriber_callback_does_not_deadlock(tmp_path):
    server, repo = _make_daemon(tmp_path)
    client = DaemonClient(socket_path=server.socket_path)
    closed = threading.Event()

    def close_client(_event):
        client.close()
        closed.set()

    client.subscribe_events(close_client)
    repo.delete_connection("demo")

    assert closed.wait(2)
    assert _wait_until(lambda: client._close_complete)
    server.shutdown()
    server.wait_stopped()


def test_one_persistent_reader_owns_all_socket_receive(tmp_path, monkeypatch):
    import sshpilot.api.daemon_client as daemon_client_module

    server, repo = _make_daemon(tmp_path)
    reader_threads = set()
    original_receive = daemon_client_module.receive_frame

    def record_receive(transport):
        reader_threads.add(threading.get_ident())
        return original_receive(transport)

    monkeypatch.setattr(daemon_client_module, "receive_frame", record_receive)
    client = DaemonClient(socket_path=server.socket_path)
    try:
        delivered = threading.Event()
        client.subscribe_events(lambda _event: delivered.set())

        assert client.list_connections()
        repo.update_connection("demo", {"hostname": "updated.example"})
        assert delivered.wait(2)
        assert len(reader_threads) == 1
        assert threading.get_ident() not in reader_threads
    finally:
        client.close()
        server.shutdown()
        server.wait_stopped()


def test_client_event_handoff_overflow_closes_continuity(tmp_path):
    server, repo = _make_daemon(tmp_path)
    client = DaemonClient(
        socket_path=server.socket_path,
        event_dispatch_limit=2,
    )
    entered = threading.Event()
    release = threading.Event()
    received = []

    def block_first(event):
        received.append(event)
        if event.type in {
            EventType.CONNECTION_UPDATED,
            EventType.CONNECTION_STORE_CHANGED,
        } and not entered.is_set():
            entered.set()
            assert release.wait(2)

    client.subscribe_events(block_first)
    repo.update_connection("demo", {"hostname": "updated.example"})
    assert entered.wait(2)

    repo.update_connection("demo", {"hostname": "updated-2.example"})
    repo.update_connection("demo", {"hostname": "updated-3.example"})
    assert _wait_until(lambda: client._transport_failed)
    release.set()

    assert _wait_until(
        lambda: any(
            event.type is EventType.ERROR_OCCURRED for event in received
        )
    )
    assert received[-1].payload["code"] == ErrorCode.PROTOCOL_ERROR.value
    client.close()
    server.shutdown()
    server.wait_stopped()

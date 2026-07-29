import socket
import threading

import pytest

from sshpilot.api import Capability, DaemonClient, ErrorCode, SshPilotError


def test_start_connect_disconnect_and_clean_stop(daemon_factory):
    server, _manager = daemon_factory()
    client = DaemonClient(socket_path=server.socket_path)

    assert client.get_capabilities().supported == frozenset(
        {
            Capability.CONNECTIONS_READ,
            Capability.CONNECTIONS_EVENTS,
            Capability.CONNECTIONS_WRITE,
            Capability.SESSIONS_READ,
            Capability.SESSIONS_WRITE,
            Capability.SESSIONS_EVENTS,
            Capability.TERMINAL_OUTPUT,
            Capability.TERMINAL_INPUT,
            Capability.TERMINAL_RESIZE,
            Capability.TERMINAL_REPLAY,
        }
    )
    client.close()
    server.shutdown()

    assert server.wait_stopped()
    assert not server.socket_path.exists()


def test_multiple_sequential_and_simultaneous_clients(daemon_factory):
    server, _manager = daemon_factory()
    first = DaemonClient(socket_path=server.socket_path)
    assert first.list_connections()[0].nickname == "demo"
    first.close()

    second = DaemonClient(socket_path=server.socket_path)
    third = DaemonClient(socket_path=server.socket_path)
    assert second.list_connections() == third.list_connections()
    second.close()
    third.close()


@pytest.mark.parametrize("_repeat", range(10))
def test_one_client_serializes_concurrent_callers(daemon_factory, _repeat):
    server, _manager = daemon_factory()
    client = DaemonClient(socket_path=server.socket_path)
    results = []
    failures = []

    def _read():
        try:
            results.append(client.list_connections()[0].nickname)
        except Exception as error:
            failures.append(error)

    workers = [threading.Thread(target=_read) for _index in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(2)

    assert results == ["demo"] * 4
    assert failures == []
    client.close()


def test_stop_with_active_client_converts_later_call_to_transport_error(
    daemon_factory,
):
    server, _manager = daemon_factory()
    client = DaemonClient(socket_path=server.socket_path)
    server.shutdown()
    assert server.wait_stopped()

    with pytest.raises(SshPilotError) as caught:
        client.list_connections()

    assert caught.value.code is ErrorCode.TRANSPORT_CLOSED
    assert caught.value.retryable is True
    client.close()


def test_shutdown_wakes_request_pending_in_client_reader(daemon_factory):
    server, manager = daemon_factory()
    client = DaemonClient(socket_path=server.socket_path, timeout=2)
    entered = threading.Event()
    release = threading.Event()
    failures = []

    def blocked_connections():
        entered.set()
        assert release.wait(2)
        return list(manager.connections)

    manager.get_connections = blocked_connections

    def request():
        try:
            client.list_connections()
        except SshPilotError as error:
            failures.append(error)

    worker = threading.Thread(target=request)
    worker.start()
    assert entered.wait(2)
    server.shutdown()
    release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert failures
    assert failures[0].code is ErrorCode.TRANSPORT_CLOSED
    assert server.wait_stopped()
    client.close()


def test_calls_after_client_close_are_rejected(daemon_factory):
    server, _manager = daemon_factory()
    client = DaemonClient(socket_path=server.socket_path)
    client.close()

    with pytest.raises(SshPilotError) as caught:
        client.list_connections()

    assert caught.value.code is ErrorCode.INVALID_REQUEST


def test_daemon_restart_after_clean_shutdown(daemon_factory):
    first, manager = daemon_factory()
    path = first.socket_path
    first.shutdown()
    assert first.wait_stopped()

    second, _manager = daemon_factory(manager=manager, socket_path=path)
    client = DaemonClient(socket_path=path)

    assert client.list_connections()[0].nickname == "demo"
    client.close()
    second.shutdown()
    assert second.wait_stopped()


def test_daemon_unavailable_is_structured(tmp_path):
    path = tmp_path / "missing.sock"

    with pytest.raises(SshPilotError) as caught:
        DaemonClient(socket_path=path, timeout=0.1)

    assert caught.value.code is ErrorCode.DAEMON_UNAVAILABLE
    assert caught.value.retryable is True
    assert str(path) not in caught.value.message


def test_server_close_during_handshake_is_structured(tmp_path):
    path = tmp_path / "closing.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen()

    def _close_peer():
        peer, _ = listener.accept()
        peer.close()
        listener.close()

    thread = threading.Thread(target=_close_peer)
    thread.start()
    with pytest.raises(SshPilotError) as caught:
        DaemonClient(socket_path=path, timeout=1)
    thread.join(2)

    assert caught.value.code is ErrorCode.TRANSPORT_CLOSED


def test_handshake_timeout_closes_ambiguous_transport(tmp_path):
    path = tmp_path / "timeout.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen()
    peer_closed = threading.Event()

    def _hold_peer():
        peer, _ = listener.accept()
        try:
            while peer.recv(1024):
                pass
        finally:
            peer_closed.set()
            peer.close()
            listener.close()

    thread = threading.Thread(target=_hold_peer)
    thread.start()
    with pytest.raises(SshPilotError) as caught:
        DaemonClient(socket_path=path, timeout=0.05)

    assert caught.value.code is ErrorCode.TRANSPORT_TIMEOUT
    assert caught.value.retryable is True
    assert peer_closed.wait(2)
    thread.join(2)


def test_internal_manager_failure_is_safe_on_wire_and_in_logs(
    daemon_factory,
    caplog,
):
    class FailingManager:
        def get_connections(self):
            raise RuntimeError("token=must-not-appear")

    server, _manager = daemon_factory(manager=FailingManager())
    client = DaemonClient(socket_path=server.socket_path)

    with pytest.raises(SshPilotError) as caught:
        client.list_connections()

    assert caught.value.code is ErrorCode.INTERNAL_ERROR
    assert "must-not-appear" not in caught.value.message
    assert "must-not-appear" not in repr(caught.value)
    assert "must-not-appear" not in caplog.text
    client.close()


def test_core_close_failure_cannot_leak_socket_or_sensitive_log_data(
    tmp_path,
    caplog,
):
    class FailingCloseCore:
        def close(self):
            raise RuntimeError("password=must-not-appear")

    from sshpilot.daemon import DaemonServer

    socket_dir = tmp_path / "close-failure"
    socket_dir.mkdir(mode=0o700)
    socket_path = socket_dir / "sshpilotd.sock"
    server = DaemonServer(FailingCloseCore, socket_path=socket_path)
    server.start_in_thread()
    server.shutdown()

    assert server.wait_stopped()
    assert not socket_path.exists()
    assert "must-not-appear" not in caplog.text

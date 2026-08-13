import socket
import threading
import time

import pytest

from sshpilot.api import DaemonClient, ErrorCode, SshPilotError
from sshpilot.api.models.common import RequestId
from sshpilot.api.models.sessions import (
    CloseSessionRequest,
    OpenSessionRequest,
    SessionExitInfo,
    SessionState,
)
from sshpilot.daemon.command_executor import (
    BoundedCommandExecutor,
    DeferredCommand,
)
from sshpilot.daemon.server import (
    DaemonServer,
    _ClientConnection,
    _DeferredCompletion,
)


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return bool(predicate())


class _Handle:
    def __init__(self, on_exit, *, block_close=False):
        self._on_exit = on_exit
        self._block_close = block_close
        self.close_entered = threading.Event()
        self.close_release = threading.Event()
        self._exit_info = None
        self._lock = threading.Lock()

    def terminate(self):
        self.close_entered.set()

    def kill(self):
        self.exit(SessionExitInfo(signal=9, reason="killed"))

    def wait(self, timeout):
        if self._block_close and self._exit_info is None:
            self.close_release.wait(timeout)
        with self._lock:
            return self._exit_info

    def exit(self, info):
        with self._lock:
            if self._exit_info is not None:
                return
            self._exit_info = info
        self._on_exit(info)


class _BlockingStartRunner:
    def __init__(self):
        self.start_entered = threading.Event()
        self.start_release = threading.Event()
        self.handles = []
        self.closed = False

    def start(self, _spec, on_exit):
        self.start_entered.set()
        self.start_release.wait(3)
        handle = _Handle(on_exit)
        self.handles.append(handle)
        return handle

    def close(self):
        self.closed = True
        self.start_release.set()
        for handle in self.handles:
            handle.close_release.set()


class _BlockingCloseRunner:
    def __init__(self):
        self.handles = []

    def start(self, _spec, on_exit):
        handle = _Handle(on_exit, block_close=True)
        self.handles.append(handle)
        return handle

    def close(self):
        for handle in self.handles:
            handle.close_release.set()
            handle.exit(SessionExitInfo(exit_code=0, reason="shutdown"))


def _run_in_thread(operation):
    result = []
    error = []
    done = threading.Event()

    def _run():
        try:
            result.append(operation())
        except BaseException as caught:
            error.append(caught)
        finally:
            done.set()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread, done, result, error


def test_blocked_start_does_not_delay_other_clients(daemon_factory, _repeat):
    runner = _BlockingStartRunner()
    server, _manager = daemon_factory(
        session_runner=runner,
        session_command_workers=2,
    )
    client_a = DaemonClient(
        socket_path=server.socket_path,
        client_id="client:a",
        timeout=0.5,
    )
    client_b = DaemonClient(socket_path=server.socket_path, client_id="client:b")
    client_c = None
    try:
        opened = client_a.open_session(
            OpenSessionRequest(connection_id=client_a.list_connections()[0].id)
        )
        assert opened.state is SessionState.STARTING
        assert runner.start_entered.wait(1)
        assert client_b.list_connections()
        sessions = client_b.list_sessions()
        assert len(sessions) == 1
        assert sessions[0].state is SessionState.STARTING
        assert client_b.get_session(sessions[0].id).id == sessions[0].id
        client_c = DaemonClient(
            socket_path=server.socket_path,
            client_id="client:c",
        )
        assert client_c.list_connections()
        assert not runner.start_release.is_set()

        runner.start_release.set()
        assert _wait_until(
            lambda: client_b.get_session(opened.id).state is SessionState.RUNNING
        )
    finally:
        runner.start_release.set()
        client_a.close()
        client_b.close()
        if client_c is not None:
            client_c.close()


def test_blocked_close_does_not_delay_other_clients(daemon_factory, _repeat):
    runner = _BlockingCloseRunner()
    server, _manager = daemon_factory(
        session_runner=runner,
        session_command_workers=2,
    )
    client_a = DaemonClient(socket_path=server.socket_path, client_id="client:a")
    client_b = DaemonClient(socket_path=server.socket_path, client_id="client:b")
    client_c = None
    try:
        opened = client_a.open_session(
            OpenSessionRequest(connection_id=client_a.list_connections()[0].id)
        )
        assert _wait_until(
            lambda: client_a.get_session(opened.id).state is SessionState.RUNNING
        )
        thread, done, _result, error = _run_in_thread(
            lambda: client_a.close_session(
                CloseSessionRequest(session_id=opened.id)
            )
        )
        assert runner.handles[0].close_entered.wait(1)
        assert client_b.list_connections()
        assert client_b.get_session(opened.id).state is SessionState.CLOSING
        client_c = DaemonClient(
            socket_path=server.socket_path,
            client_id="client:c",
        )
        assert client_c.list_sessions()[0].id == opened.id
        assert not done.is_set()

        runner.handles[0].exit(
            SessionExitInfo(exit_code=0, reason="released")
        )
        runner.handles[0].close_release.set()
        assert done.wait(1)
        assert error == []
        assert _wait_until(
            lambda: client_b.get_session(opened.id).state is SessionState.CLOSED
        )
        thread.join(1)
    finally:
        for handle in runner.handles:
            handle.close_release.set()
        client_a.close()
        client_b.close()
        if client_c is not None:
            client_c.close()


def test_close_waits_in_the_same_session_lane_as_start(daemon_factory, _repeat):
    runner = _BlockingStartRunner()
    server, _manager = daemon_factory(
        session_runner=runner,
        session_command_workers=2,
    )
    opener = DaemonClient(
        socket_path=server.socket_path,
        client_id="client:open",
        timeout=0.5,
    )
    closer = DaemonClient(socket_path=server.socket_path, client_id="client:close")
    close_thread = None
    try:
        opened = opener.open_session(
            OpenSessionRequest(connection_id=opener.list_connections()[0].id)
        )
        assert opened.state is SessionState.STARTING
        assert runner.start_entered.wait(1)
        session_id = opened.id
        close_thread, close_done, _close_result, close_error = _run_in_thread(
            lambda: closer.close_session(
                CloseSessionRequest(session_id=session_id)
            )
        )
        assert _wait_until(lambda: server._session_executor.outstanding == 2)
        assert runner.handles == []
        assert not close_done.is_set()

        runner.start_release.set()
        assert close_done.wait(1)
        assert close_error == []
        assert _wait_until(
            lambda: closer.get_session(session_id).state is SessionState.CLOSED
        )
        assert runner.handles[0].close_entered.is_set()
        close_thread.join(1)
    finally:
        runner.start_release.set()
        opener.close()
        closer.close()


def test_executor_queue_full_is_safe_and_reads_remain_live(
    daemon_factory,
    _repeat,
):
    runner = _BlockingStartRunner()
    server, _manager = daemon_factory(
        session_runner=runner,
        session_command_workers=1,
        session_command_queue_limit=1,
    )
    client_a = DaemonClient(
        socket_path=server.socket_path,
        client_id="client:a",
        timeout=0.5,
    )
    client_b = DaemonClient(socket_path=server.socket_path, client_id="client:b")
    try:
        first = client_a.open_session(
            OpenSessionRequest(connection_id=client_a.list_connections()[0].id)
        )
        assert first.state is SessionState.STARTING
        assert runner.start_entered.wait(1)
        with pytest.raises(SshPilotError) as caught:
            client_b.open_session(
                OpenSessionRequest(
                    connection_id=client_b.list_connections()[0].id
                )
            )
        assert caught.value.code is ErrorCode.SERVER_BUSY
        assert caught.value.retryable is True
        assert len(client_b.list_sessions()) == 2
        assert any(
            session.state is SessionState.FAILED
            and session.failure.code == ErrorCode.SERVER_BUSY.value
            for session in client_b.list_sessions()
        )
        assert server._session_executor.outstanding == 1

        runner.start_release.set()
        assert _wait_until(lambda: server._session_executor.outstanding == 0)
        assert _wait_until(
            lambda: client_a.get_session(first.id).state is SessionState.RUNNING
        )
    finally:
        runner.start_release.set()
        client_a.close()
        client_b.close()


def test_peer_disconnect_during_background_start_keeps_session_alive(
    daemon_factory,
    _repeat,
):
    runner = _BlockingStartRunner()
    server, _manager = daemon_factory(session_runner=runner)
    client_a = DaemonClient(
        socket_path=server.socket_path,
        client_id="client:a",
        timeout=0.5,
    )
    observer = DaemonClient(
        socket_path=server.socket_path,
        client_id="client:observer",
    )
    replacement = None
    try:
        opened = client_a.open_session(
            OpenSessionRequest(connection_id=client_a.list_connections()[0].id)
        )
        assert opened.state is SessionState.STARTING
        assert runner.start_entered.wait(1)
        old_tokens = {
            state.token
            for state in server._clients_by_token.values()
            if state.protocol.client_id == "client:a"
        }
        assert len(old_tokens) == 1
        client_a.close()
        assert _wait_until(
            lambda: old_tokens.isdisjoint(server._clients_by_token)
        )
        replacement = DaemonClient(
            socket_path=server.socket_path,
            client_id="client:replacement",
        )
        new_tokens = {
            state.token
            for state in server._clients_by_token.values()
            if state.protocol.client_id == "client:replacement"
        }
        assert len(new_tokens) == 1
        assert old_tokens.isdisjoint(new_tokens)

        runner.start_release.set()
        assert replacement.list_connections()
        assert _wait_until(
            lambda: observer.get_session(opened.id).state is SessionState.RUNNING
        )
    finally:
        runner.start_release.set()
        client_a.close()
        observer.close()
        if replacement is not None:
            replacement.close()


def test_peer_token_prevents_file_descriptor_slot_reuse():
    server = DaemonServer(lambda: None)
    server_side, peer_side = socket.socketpair()
    try:
        request_id = RequestId("request:old")
        replacement = _ClientConnection(server_side, token=2)
        replacement.pending_request_ids.add(RequestId("request:new"))
        file_descriptor = server_side.fileno()
        server._clients[file_descriptor] = replacement
        server._clients_by_token[2] = replacement
        server._completion_queue.put_nowait(
            _DeferredCompletion(
                peer_token=1,
                protocol_version="1.0",
                request_id=request_id,
                result={"must": "not-arrive"},
            )
        )

        server._drain_completions()

        assert not replacement.output
        assert replacement.pending_request_ids == {RequestId("request:new")}
    finally:
        server_side.close()
        peer_side.close()


def test_deferred_request_is_completed_at_most_once():
    server = DaemonServer(lambda: None)
    server_side, peer_side = socket.socketpair()
    try:
        request_id = RequestId("request:one")
        state = _ClientConnection(server_side, token=1)
        state.pending_request_ids.add(request_id)
        server._clients[server_side.fileno()] = state
        server._clients_by_token[state.token] = state
        completion = _DeferredCompletion(
            peer_token=state.token,
            protocol_version="1.0",
            request_id=request_id,
            result={"accepted": True},
        )
        server._completion_queue.put_nowait(completion)
        server._completion_queue.put_nowait(completion)

        server._drain_completions()

        assert len(state.output) == 1
        assert request_id not in state.pending_request_ids
    finally:
        server_side.close()
        peer_side.close()


def test_shutdown_unblocks_active_start_and_stops_workers(
    daemon_factory,
    _repeat,
):
    runner = _BlockingStartRunner()
    server, _manager = daemon_factory(
        session_runner=runner,
        session_command_workers=1,
        session_command_queue_limit=4,
        session_shutdown_timeout=1.0,
    )
    client_a = DaemonClient(
        socket_path=server.socket_path,
        client_id="client:a",
        timeout=0.5,
    )
    client_b = DaemonClient(
        socket_path=server.socket_path,
        client_id="client:b",
        timeout=0.5,
    )
    observer = DaemonClient(
        socket_path=server.socket_path,
        client_id="client:observer",
    )
    opened_a = client_a.open_session(
        OpenSessionRequest(connection_id=client_a.list_connections()[0].id)
    )
    assert opened_a.state is SessionState.STARTING
    assert runner.start_entered.wait(1)
    executor = server._session_executor
    opened_b = client_b.open_session(
        OpenSessionRequest(connection_id=client_b.list_connections()[0].id)
    )
    assert opened_b.state is SessionState.STARTING
    assert _wait_until(lambda: executor.outstanding == 2)
    assert len(observer.list_sessions()) == 2

    server.shutdown()

    assert server.wait_stopped(timeout=2)
    assert runner.closed is True
    assert executor.outstanding == 0
    assert all(not thread.is_alive() for thread in executor._threads)
    client_a.close()
    client_b.close()
    observer.close()


def test_shutdown_during_blocked_close_is_bounded(daemon_factory, _repeat):
    runner = _BlockingCloseRunner()
    server, _manager = daemon_factory(
        session_runner=runner,
        session_shutdown_timeout=1.0,
    )
    client = DaemonClient(socket_path=server.socket_path)
    opened = client.open_session(
        OpenSessionRequest(connection_id=client.list_connections()[0].id)
    )
    assert _wait_until(
        lambda: client.get_session(opened.id).state is SessionState.RUNNING
    )
    _thread, done, _result, _error = _run_in_thread(
        lambda: client.close_session(CloseSessionRequest(session_id=opened.id))
    )
    assert runner.handles[0].close_entered.wait(1)
    executor = server._session_executor

    server.shutdown()

    assert server.wait_stopped(timeout=2)
    assert done.wait(1)
    assert executor.outstanding == 0
    assert all(not thread.is_alive() for thread in executor._threads)
    client.close()


def test_keyed_executor_does_not_occupy_workers_with_same_key_waiters():
    executor = BoundedCommandExecutor(max_workers=2, max_commands=3)
    first_entered = threading.Event()
    other_entered = threading.Event()
    release = threading.Event()
    same_key_started = threading.Event()

    def _blocking():
        first_entered.set()
        release.wait(1)

    def _blocking_other():
        other_entered.set()
        release.wait(1)

    assert executor.submit(
        DeferredCommand(
            key="session:a",
            operation=_blocking,
            on_complete=lambda _value: None,
            on_error=lambda _error: None,
            on_cancel=lambda: None,
        )
    )
    assert executor.submit(
        DeferredCommand(
            key="session:a",
            operation=lambda: same_key_started.set(),
            on_complete=lambda _value: None,
            on_error=lambda _error: None,
            on_cancel=lambda: None,
        )
    )
    assert executor.submit(
        DeferredCommand(
            key="session:b",
            operation=_blocking_other,
            on_complete=lambda _value: None,
            on_error=lambda _error: None,
            on_cancel=lambda: None,
        )
    )
    assert first_entered.wait(1)
    assert other_entered.wait(1)
    assert not same_key_started.is_set()
    assert not executor.submit(
        DeferredCommand(
            key="session:c",
            operation=lambda: None,
            on_complete=lambda _value: None,
            on_error=lambda _error: None,
            on_cancel=lambda: None,
        )
    )

    release.set()
    assert _wait_until(same_key_started.is_set)
    assert executor.shutdown(timeout=1)


def test_executor_shutdown_requested_from_completion_does_not_deadlock():
    executor = BoundedCommandExecutor(max_workers=1, max_commands=1)
    callback_done = threading.Event()

    def _complete(_value):
        executor.shutdown(timeout=0)
        callback_done.set()

    assert executor.submit(
        DeferredCommand(
            key="session:a",
            operation=lambda: None,
            on_complete=_complete,
            on_error=lambda _error: None,
            on_cancel=lambda: None,
        )
    )

    assert callback_done.wait(1)
    assert _wait_until(
        lambda: all(not thread.is_alive() for thread in executor._threads)
    )

"""Minimal lifecycle coverage for ForwardRuntime with a mocked process runner."""

import threading

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ClientId, ConnectionId
from sshpilot.api.models.operations import (
    CloseForwardRequest,
    ForwardState,
    ForwardType,
    OpenForwardRequest,
)
from sshpilot.daemon.forward_runtime import ForwardRuntime


class _Connection:
    def __init__(self):
        self.id = ConnectionId("demo")
        self.protocol = "ssh"
        self.hostname = "example.test"
        self.username = "alice"
        self.port = 22


class _CoreClient:
    def __init__(self):
        self.connection = _Connection()

    def get_connection(self, _connection_id):
        return self.connection


class _FakeForwardHandle:
    def __init__(self, on_exit):
        self._on_exit = on_exit
        self._exit_code = None
        self._lock = threading.Lock()
        self.terminated = 0

    def terminate(self):
        self.terminated += 1
        self._exit(0)

    def kill(self):
        self._exit(-9)

    def wait(self, timeout):
        del timeout
        with self._lock:
            return self._exit_code

    def poll(self):
        with self._lock:
            return self._exit_code

    def _exit(self, code):
        with self._lock:
            if self._exit_code is not None:
                return
            self._exit_code = code
        self._on_exit(code)


class _FakeForwardRunner:
    def __init__(self):
        self.handles = []
        self.closed = False

    def start(self, spec, on_exit):
        handle = _FakeForwardHandle(on_exit)
        self.handles.append(handle)
        return handle

    def close(self):
        self.closed = True


def _make_runtime():
    runner = _FakeForwardRunner()
    runtime = ForwardRuntime(_CoreClient(), runner=runner, active_timeout_seconds=0.1)
    return runtime, runner


def _open_request():
    return OpenForwardRequest(
        connection_id=ConnectionId("demo"),
        type=ForwardType.REMOTE,
        bind_host="127.0.0.1",
        bind_port=2222,
        destination_host="internal.test",
        destination_port=80,
    )


def test_prepare_open_forward_returns_starting_summary():
    runtime, _runner = _make_runtime()
    owner = ClientId("client:owner")
    summary = runtime.prepare_open_forward(_open_request(), client_id=owner)
    assert summary.state is ForwardState.STARTING


def test_start_forward_transitions_to_active():
    runtime, runner = _make_runtime()
    owner = ClientId("client:owner")
    summary = runtime.prepare_open_forward(_open_request(), client_id=owner)
    runtime.start_forward(summary.id)
    assert runtime.get_forward(summary.id).state is ForwardState.ACTIVE
    assert len(runner.handles) == 1


def test_close_forward_terminates_process():
    runtime, runner = _make_runtime()
    owner = ClientId("client:owner")
    summary = runtime.prepare_open_forward(_open_request(), client_id=owner)
    runtime.start_forward(summary.id)
    close_request = CloseForwardRequest(forward_id=summary.id)
    assert runtime.prepare_close_forward(close_request, client_id=owner)
    runtime.finish_close_forward(summary.id)
    assert runtime.get_forward(summary.id).state is ForwardState.CLOSED
    assert runner.handles[0].terminated == 1


def test_close_forward_requires_owner():
    runtime, _runner = _make_runtime()
    owner = ClientId("client:owner")
    other = ClientId("client:other")
    summary = runtime.prepare_open_forward(_open_request(), client_id=owner)
    close_request = CloseForwardRequest(forward_id=summary.id)
    with pytest.raises(SshPilotError) as excinfo:
        runtime.prepare_close_forward(close_request, client_id=other)
    assert excinfo.value.code is ErrorCode.SERVICE_OWNER_REQUIRED


def test_client_can_interact_only_owner():
    runtime, _runner = _make_runtime()
    owner = ClientId("client:owner")
    other = ClientId("client:other")
    summary = runtime.prepare_open_forward(_open_request(), client_id=owner)
    assert runtime.client_can_interact(summary.id, owner) is True
    assert runtime.client_can_interact(summary.id, other) is False


def test_local_forward_not_active_without_bind():
    """Local ACTIVE requires a listening bind — process-alive alone is insufficient."""

    import socket

    runner = _FakeForwardRunner()
    runtime = ForwardRuntime(_CoreClient(), runner=runner, active_timeout_seconds=0.15)
    owner = ClientId("client:owner")
    # Bind an ephemeral port, note it, then release so nothing listens.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    bind_port = int(probe.getsockname()[1])
    probe.close()
    request = OpenForwardRequest(
        connection_id=ConnectionId("demo"),
        type=ForwardType.LOCAL,
        bind_host="127.0.0.1",
        bind_port=bind_port,
        destination_host="internal.test",
        destination_port=80,
    )
    summary = runtime.prepare_open_forward(request, client_id=owner)
    runtime.start_forward(summary.id)
    assert runtime.get_forward(summary.id).state is ForwardState.FAILED
    assert runtime.get_forward(summary.id).failure is not None
    assert (
        str(runtime.get_forward(summary.id).failure.code)
        == ErrorCode.FORWARD_STARTUP_FAILED.value
    )

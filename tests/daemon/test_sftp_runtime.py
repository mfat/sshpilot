"""Minimal lifecycle coverage for SftpServiceRuntime with a mocked process runner."""

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ClientId, ConnectionId
from sshpilot.api.models.operations import (
    AttachSftpRequest,
    CloseSftpRequest,
    OpenSftpRequest,
    SftpPathRequest,
    SftpServiceState,
)
from sshpilot.daemon.sftp_runtime import SftpServiceRuntime


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


class _FakeSftpClient:
    def __init__(self):
        self.mkdir_calls = []
        self.closed = False

    def mkdir(self, path):
        self.mkdir_calls.append(path)

    def close(self):
        self.closed = True


class _FakeSftpHandle:
    def __init__(self, client):
        self.client = client
        self.terminated = 0

    def terminate(self):
        self.terminated += 1
        self.client.close()

    def wait(self, timeout):
        del timeout
        return True


class _FakeSftpRunner:
    def __init__(self):
        self.handles = []
        self.closed = False

    def start(self, spec):
        handle = _FakeSftpHandle(_FakeSftpClient())
        self.handles.append(handle)
        return handle

    def close(self):
        self.closed = True


def _make_runtime():
    runner = _FakeSftpRunner()
    runtime = SftpServiceRuntime(_CoreClient(), runner=runner)
    return runtime, runner


def _open_request():
    return OpenSftpRequest(connection_id=ConnectionId("demo"))


def test_prepare_open_service_returns_starting_summary():
    runtime, _runner = _make_runtime()
    owner = ClientId("client:owner")
    summary = runtime.prepare_open_service(_open_request(), client_id=owner)
    assert summary.state is SftpServiceState.STARTING


def test_start_service_transitions_to_ready():
    runtime, runner = _make_runtime()
    owner = ClientId("client:owner")
    summary = runtime.prepare_open_service(_open_request(), client_id=owner)
    runtime.start_service(summary.id)
    assert runtime.get_service(summary.id).state is SftpServiceState.READY
    assert len(runner.handles) == 1


def test_close_service_terminates_process():
    runtime, runner = _make_runtime()
    owner = ClientId("client:owner")
    summary = runtime.prepare_open_service(_open_request(), client_id=owner)
    runtime.start_service(summary.id)
    close_request = CloseSftpRequest(service_id=summary.id)
    assert runtime.prepare_close_service(close_request, client_id=owner)
    runtime.finish_close_service(summary.id)
    assert runtime.get_service(summary.id).state is SftpServiceState.CLOSED
    assert runner.handles[0].terminated == 1


def test_mutation_requires_owner():
    runtime, _runner = _make_runtime()
    owner = ClientId("client:owner")
    other = ClientId("client:other")
    summary = runtime.prepare_open_service(_open_request(), client_id=owner)
    runtime.start_service(summary.id)
    runtime.attach_service(AttachSftpRequest(service_id=summary.id), client_id=other)
    with pytest.raises(SshPilotError) as excinfo:
        runtime.mkdir(
            SftpPathRequest(service_id=summary.id, path="/tmp/demo"),
            client_id=other,
        )
    assert excinfo.value.code is ErrorCode.SERVICE_OWNER_REQUIRED


def test_owner_can_mutate():
    runtime, runner = _make_runtime()
    owner = ClientId("client:owner")
    summary = runtime.prepare_open_service(_open_request(), client_id=owner)
    runtime.start_service(summary.id)
    runtime.mkdir(
        SftpPathRequest(service_id=summary.id, path="/tmp/demo"),
        client_id=owner,
    )
    assert runner.handles[0].client.mkdir_calls == ["/tmp/demo"]

"""Minimal lifecycle coverage for SftpServiceRuntime with a mocked process runner."""

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ClientId, ConnectionId
from sshpilot.api.models.operations import (
    AttachSftpRequest,
    CloseSftpRequest,
    OpenSftpRequest,
    SftpCopyRequest,
    SftpPathRequest,
    SftpServiceState,
)
from sshpilot.sftp import protocol as sftp_proto
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


class _Attr:
    def __init__(self, name, is_dir=False):
        self.filename = name
        self._is_dir = is_dir
        self.st_mode = 0o040755 if is_dir else 0o100644
        self.st_size = 0

    def is_dir(self):
        return self._is_dir


class _File:
    def __init__(self, client, path, mode):
        self.client = client
        self.path = path
        self.mode = mode
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, size=None):
        data = self.client.files.get(self.path, b"")
        if size is None:
            chunk = data[self.offset:]
        else:
            chunk = data[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk

    def write(self, data):
        self.client.files[self.path] = data


class _FakeSftpClient:
    def __init__(self):
        self.mkdir_calls = []
        self.remove_calls = []
        self.closed = False
        self.files = {"/source.txt": b"payload"}
        self.directories = {"/"}

    def stat(self, path):
        if path in self.directories:
            return _Attr(path, is_dir=True)
        if path in self.files:
            return _Attr(path)
        raise sftp_proto.SFTPError(sftp_proto.FX_NO_SUCH_FILE, "missing")

    def listdir_attr(self, path):
        prefix = path.rstrip("/") + "/"
        result = []
        for child in sorted(self.directories | set(self.files)):
            if child.startswith(prefix) and "/" not in child[len(prefix):]:
                result.append(_Attr(child[len(prefix):], child in self.directories))
        return result

    def mkdir(self, path):
        self.mkdir_calls.append(path)
        self.directories.add(path)

    def open(self, path, mode):
        return _File(self, path, mode)

    def remove(self, path):
        self.remove_calls.append(path)
        self.files.pop(path, None)

    def rmdir(self, path):
        self.directories.discard(path)

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


def test_owner_can_copy_and_move_remote_file():
    runtime, runner = _make_runtime()
    owner = ClientId("client:owner")
    summary = runtime.prepare_open_service(_open_request(), client_id=owner)
    runtime.start_service(summary.id)
    request = SftpCopyRequest(
        service_id=summary.id,
        source_path="/source.txt",
        destination_path="/copy.txt",
    )
    runtime.copy(request, client_id=owner)
    assert runner.handles[0].client.files["/copy.txt"] == b"payload"
    runtime.copy(
        SftpCopyRequest(
            service_id=summary.id,
            source_path="/copy.txt",
            destination_path="/moved.txt",
            move=True,
        ),
        client_id=owner,
    )
    assert "/copy.txt" not in runner.handles[0].client.files
    assert runner.handles[0].client.files["/moved.txt"] == b"payload"


def test_remote_copy_rejects_existing_destination_and_self_directory():
    runtime, runner = _make_runtime()
    owner = ClientId("client:owner")
    summary = runtime.prepare_open_service(_open_request(), client_id=owner)
    runtime.start_service(summary.id)
    runner.handles[0].client.files["/existing.txt"] = b"existing"
    with pytest.raises(SshPilotError) as conflict:
        runtime.copy(
            SftpCopyRequest(
                service_id=summary.id,
                source_path="/source.txt",
                destination_path="/existing.txt",
            ),
            client_id=owner,
        )
    assert conflict.value.code is ErrorCode.REMOTE_PATH_EXISTS
    runner.handles[0].client.directories.add("/tree")
    with pytest.raises(SshPilotError) as self_copy:
        runtime.copy(
            SftpCopyRequest(
                service_id=summary.id,
                source_path="/tree",
                destination_path="/tree/child",
                recursive=True,
            ),
            client_id=owner,
        )
    assert self_copy.value.code is ErrorCode.VALIDATION_FAILED

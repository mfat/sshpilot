"""Minimal lifecycle coverage for TransferRuntime with a mocked SFTP client."""

import tempfile
import time
from types import SimpleNamespace

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ClientId, ConnectionId
from sshpilot.api.models.operations import OpenSftpRequest
from sshpilot.api.models.transfers import (
    CancelTransferRequest,
    StartTransferRequest,
    TransferDirection,
    TransferState,
)
from sshpilot.daemon.sftp_runtime import SftpServiceRuntime
from sshpilot.daemon.transfer_runtime import TransferRuntime

_TERMINAL_STATES = frozenset(
    {TransferState.COMPLETED, TransferState.CANCELLED, TransferState.FAILED}
)


class _Connection:
    def __init__(self):
        self.id = ConnectionId("connection:demo")
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
    """Enough of the OpenSSH SFTP surface for an upload to run end to end."""

    def __init__(self):
        self.files = {}

    def stat(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return SimpleNamespace(st_size=len(self.files[path]))

    def open_handle(self, path, _flags):
        self.files.setdefault(path, b"")
        return path

    def write(self, handle, offset, chunk):
        data = self.files.get(handle, b"")
        if offset > len(data):
            data = data + b"\0" * (offset - len(data))
        self.files[handle] = data[:offset] + chunk

    def close_handle(self, _handle):
        return None

    def posix_rename(self, old, new):
        self.files[new] = self.files.pop(old, b"")

    def remove(self, path):
        self.files.pop(path, None)


class _FakeSftpHandle:
    def __init__(self, client):
        self.client = client

    def terminate(self):
        return None

    def wait(self, timeout):
        del timeout
        return True


class _FakeSftpRunner:
    def __init__(self, client):
        self._client = client

    def start(self, _spec):
        return _FakeSftpHandle(self._client)

    def close(self):
        return None


def _make_ready_sftp_service(owner):
    client = _FakeSftpClient()
    sftp_runtime = SftpServiceRuntime(_CoreClient(), runner=_FakeSftpRunner(client))
    summary = sftp_runtime.prepare_open_service(
        OpenSftpRequest(connection_id=ConnectionId("connection:demo")),
        client_id=owner,
    )
    sftp_runtime.start_service(summary.id)
    return sftp_runtime, summary.id, client


def _wait_for_terminal_state(runtime, transfer_id, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        summary = runtime.get_transfer(transfer_id)
        if summary.state in _TERMINAL_STATES:
            return summary
        time.sleep(0.01)
    raise AssertionError("transfer did not reach a terminal state in time")


def test_prepare_start_transfer_returns_starting_summary():
    owner = ClientId("client:owner")
    sftp_runtime, service_id, _client = _make_ready_sftp_service(owner)
    transfer_runtime = TransferRuntime(sftp_runtime)
    with tempfile.NamedTemporaryFile(delete=False) as source:
        source.write(b"hello world")
        local_path = source.name
    request = StartTransferRequest(
        connection_id=ConnectionId("connection:demo"),
        sftp_service_id=service_id,
        direction=TransferDirection.UPLOAD,
        remote_path="/remote/file.txt",
        local_path=local_path,
    )
    summary = transfer_runtime.prepare_start_transfer(request, client_id=owner)
    assert summary.state is TransferState.STARTING


def test_run_transfer_completes_upload():
    owner = ClientId("client:owner")
    sftp_runtime, service_id, client = _make_ready_sftp_service(owner)
    transfer_runtime = TransferRuntime(sftp_runtime)
    with tempfile.NamedTemporaryFile(delete=False) as source:
        source.write(b"hello world")
        local_path = source.name
    request = StartTransferRequest(
        connection_id=ConnectionId("connection:demo"),
        sftp_service_id=service_id,
        direction=TransferDirection.UPLOAD,
        remote_path="/remote/file.txt",
        local_path=local_path,
    )
    prepared = transfer_runtime.prepare_start_transfer(request, client_id=owner)
    transfer_runtime.run_transfer(prepared.id)
    summary = _wait_for_terminal_state(transfer_runtime, prepared.id)
    assert summary.state is TransferState.COMPLETED
    assert client.files["/remote/file.txt"] == b"hello world"


def test_cancel_before_run_marks_transfer_cancelled():
    owner = ClientId("client:owner")
    sftp_runtime, service_id, _client = _make_ready_sftp_service(owner)
    transfer_runtime = TransferRuntime(sftp_runtime)
    with tempfile.NamedTemporaryFile(delete=False) as source:
        source.write(b"hello world")
        local_path = source.name
    request = StartTransferRequest(
        connection_id=ConnectionId("connection:demo"),
        sftp_service_id=service_id,
        direction=TransferDirection.UPLOAD,
        remote_path="/remote/file.txt",
        local_path=local_path,
    )
    prepared = transfer_runtime.prepare_start_transfer(request, client_id=owner)
    assert transfer_runtime.prepare_cancel_transfer(
        CancelTransferRequest(transfer_id=prepared.id), client_id=owner
    )
    transfer_runtime.run_transfer(prepared.id)
    summary = transfer_runtime.get_transfer(prepared.id)
    assert summary.state is TransferState.CANCELLED


def test_cancel_requires_owner():
    owner = ClientId("client:owner")
    other = ClientId("client:other")
    sftp_runtime, service_id, _client = _make_ready_sftp_service(owner)
    transfer_runtime = TransferRuntime(sftp_runtime)
    with tempfile.NamedTemporaryFile(delete=False) as source:
        source.write(b"hello world")
        local_path = source.name
    request = StartTransferRequest(
        connection_id=ConnectionId("connection:demo"),
        sftp_service_id=service_id,
        direction=TransferDirection.UPLOAD,
        remote_path="/remote/file.txt",
        local_path=local_path,
    )
    prepared = transfer_runtime.prepare_start_transfer(request, client_id=owner)
    with pytest.raises(SshPilotError) as excinfo:
        transfer_runtime.prepare_cancel_transfer(
            CancelTransferRequest(transfer_id=prepared.id), client_id=other
        )
    assert excinfo.value.code is ErrorCode.SERVICE_OWNER_REQUIRED

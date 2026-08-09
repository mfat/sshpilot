"""Minimal lifecycle coverage for TransferRuntime with a mocked SFTP client."""

import os
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ClientId, ConnectionId, TransferId
from sshpilot.api.models.operations import OpenSftpRequest
from sshpilot.api.models.transfers import (
    CancelTransferRequest,
    StartScpTransferRequest,
    StartTransferRequest,
    TransferBackend,
    TransferConflictPolicy,
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


class _BlockingSftpClient(_FakeSftpClient):
    """Blocks the first write until released so concurrency can be observed."""

    def __init__(self):
        super().__init__()
        self.write_started = threading.Event()
        self.allow_write = threading.Event()
        self.active_writes = 0
        self._active_lock = threading.Lock()
        self.max_active_writes = 0

    def write(self, handle, offset, chunk):
        with self._active_lock:
            self.active_writes += 1
            self.max_active_writes = max(self.max_active_writes, self.active_writes)
        self.write_started.set()
        try:
            if not self.allow_write.wait(timeout=5.0):
                raise TimeoutError("blocking SFTP write was not released")
            super().write(handle, offset, chunk)
        finally:
            with self._active_lock:
                self.active_writes -= 1


class _FakeSftpHandle:
    def __init__(self, client):
        self.client = client

    def terminate(self):
        return None

    def wait(self, timeout):
        del timeout
        return True


class _RecursiveSftpClient(_FakeSftpClient):
    """SFTP surface with real directory semantics for recursive transfers."""

    def __init__(self):
        super().__init__()
        self.dirs = set()
        self.mkdirs = []
        self.links = {}
        self.listdir_paths = []

    def mkdir(self, path, _mode=None):
        self.dirs.add(path)
        self.mkdirs.append(path)

    def stat(self, path):
        import errno

        if path in self.files:
            return SimpleNamespace(
                st_size=len(self.files[path]),
                is_dir=lambda: False,
                is_symlink=lambda: False,
            )
        if path in self.dirs:
            return SimpleNamespace(
                st_size=0,
                is_dir=lambda: True,
                is_symlink=lambda: False,
            )
        raise OSError(errno.ENOENT, path)

    def lstat(self, path):
        if path in self.links:
            return SimpleNamespace(
                st_size=0,
                is_dir=lambda: True,
                is_symlink=lambda: True,
            )
        return self.stat(path)

    def listdir_attr(self, path):
        entries = []
        self.listdir_paths.append(path)
        prefix = path.rstrip("/") + "/" if path != "/" else "/"
        names = set(self.files) | self.dirs | set(self.links)
        for full in sorted(names):
            if not full.startswith(prefix):
                continue
            rest = full[len(prefix):]
            if "/" in rest or rest in (".", ".."):
                continue
            if full in self.files:
                entries.append(SimpleNamespace(
                    filename=rest,
                    st_size=len(self.files[full]),
                    is_dir=lambda: False,
                    is_symlink=lambda: False,
                ))
            elif full in self.links:
                entries.append(SimpleNamespace(
                    filename=rest,
                    st_size=0,
                    is_dir=lambda: True,
                    is_symlink=lambda: True,
                ))
            else:
                entries.append(SimpleNamespace(
                    filename=rest,
                    st_size=0,
                    is_dir=lambda: True,
                    is_symlink=lambda: False,
                ))
        if not entries and path not in self.dirs and path not in self.files:
            raise FileNotFoundError(path)
        return entries

    def open_handle(self, path, flags):
        from sshpilot.sftp import protocol as sftp_proto

        if flags & sftp_proto.FXF_READ:
            if path not in self.files:
                raise FileNotFoundError(path)
            return ("r", path)
        self.files.setdefault(path, b"")
        return ("w", path)

    def read(self, handle, offset, length):
        _kind, path = handle
        return self.files.get(path, b"")[offset:offset + length]

    def write(self, handle, offset, chunk):
        _kind, path = handle
        data = self.files.get(path, b"")
        if offset > len(data):
            data = data + b"\0" * (offset - len(data))
        self.files[path] = data[:offset] + chunk

    def close_handle(self, _handle):
        return None

    def posix_rename(self, old, new):
        if old in self.files:
            self.files[new] = self.files.pop(old, b"")
        if old in self.dirs:
            self.dirs.discard(old)
            self.dirs.add(new)


class _FakeSftpRunner:
    def __init__(self, client):
        self._client = client

    def start(self, _spec):
        return _FakeSftpHandle(self._client)

    def close(self):
        return None


def _make_ready_sftp_service(owner, client=None):
    client = client if client is not None else _FakeSftpClient()
    sftp_runtime = SftpServiceRuntime(_CoreClient(), runner=_FakeSftpRunner(client))
    summary = sftp_runtime.prepare_open_service(
        OpenSftpRequest(connection_id=ConnectionId("demo")),
        client_id=owner,
    )
    sftp_runtime.start_service(summary.id)
    return sftp_runtime, summary.id, client


def _upload_request(service_id, local_path, remote_path="/remote/file.txt"):
    return StartTransferRequest(
        connection_id=ConnectionId("demo"),
        sftp_service_id=service_id,
        direction=TransferDirection.UPLOAD,
        remote_path=remote_path,
        local_path=local_path,
    )


def _temp_source(payload=b"hello world"):
    with tempfile.NamedTemporaryFile(delete=False) as source:
        source.write(payload)
        return source.name


def _wait_for_terminal_state(runtime, transfer_id, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        summary = runtime.get_transfer(transfer_id)
        if summary.state in _TERMINAL_STATES:
            return summary
        time.sleep(0.01)
    raise AssertionError("transfer did not reach a terminal state in time")


def _wait_until(predicate, timeout=2.0, message="condition not met in time"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(message)


class _FakeScpBackend:
    supported = True

    def __init__(self):
        self.runs = []

    def target_for_connection(self, connection_id):
        assert connection_id == "demo"
        return "alice@example.test"

    def run(self, request, *, connection_target, connection_id, transfer_id, cancel_event):
        self.runs.append((request, connection_target, connection_id, transfer_id, cancel_event))
        return SimpleNamespace(returncode=0, stderr="")


class _BlockingScpBackend(_FakeScpBackend):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.stopped = threading.Event()

    def run(self, request, *, connection_target, connection_id, transfer_id, cancel_event):
        self.runs.append((request, connection_target, connection_id, transfer_id, cancel_event))
        self.started.set()
        while not cancel_event.is_set():
            self.stopped.wait(0.01)
        self.stopped.set()
        raise SshPilotError(ErrorCode.OPERATION_CANCELLED, "cancelled")


def _scp_request():
    return StartScpTransferRequest(
        connection_id=ConnectionId("demo"),
        direction=TransferDirection.UPLOAD,
        sources=("/tmp/source file",),
        destination="/var/tmp/drop",
    )


def test_shutdown_signals_running_scp_and_reaps_worker():
    owner = ClientId("client:owner")
    sftp_runtime, _service_id, _client = _make_ready_sftp_service(owner)
    backend = _BlockingScpBackend()
    transfer_runtime = TransferRuntime(
        sftp_runtime,
        scp_backend=backend,
        shutdown_timeout_seconds=1.0,
    )
    prepared = transfer_runtime.prepare_start_scp_transfer(
        _scp_request(), client_id=owner
    )
    transfer_runtime.run_transfer(prepared.id)
    assert backend.started.wait(2)

    transfer_runtime.shutdown()

    assert backend.runs[0][4].is_set()
    assert backend.stopped.is_set()
    assert prepared.id not in transfer_runtime._worker_threads
    assert transfer_runtime._records[prepared.id].state in _TERMINAL_STATES
    transfer_runtime.shutdown()


def test_finish_completed_resolves_cancelling_as_cancelled():
    owner = ClientId("client:owner")
    sftp_runtime, _service_id, _client = _make_ready_sftp_service(owner)
    backend = _FakeScpBackend()
    transfer_runtime = TransferRuntime(sftp_runtime, scp_backend=backend)
    prepared = transfer_runtime.prepare_start_scp_transfer(
        _scp_request(), client_id=owner
    )
    record = transfer_runtime._records[prepared.id]
    with transfer_runtime._lock:
        record.state = TransferState.CANCELLING
    transfer_runtime._finish_completed(record)
    assert transfer_runtime.get_transfer(prepared.id).state is TransferState.CANCELLED


def test_prepare_and_run_scp_transfer_uses_shared_lifecycle_without_sftp():
    owner = ClientId("client:owner")
    sftp_runtime, _service_id, _client = _make_ready_sftp_service(owner)
    backend = _FakeScpBackend()
    transfer_runtime = TransferRuntime(sftp_runtime, scp_backend=backend)

    prepared = transfer_runtime.prepare_start_scp_transfer(
        _scp_request(), client_id=owner
    )
    assert prepared.backend is TransferBackend.NATIVE_SCP
    assert prepared.sftp_service_id is None

    transfer_runtime.run_transfer(prepared.id)
    summary = _wait_for_terminal_state(transfer_runtime, prepared.id)

    assert summary.state is TransferState.COMPLETED
    assert len(backend.runs) == 1
    assert backend.runs[0][1] == "alice@example.test"


def test_cancel_scp_transfer_signals_backend_before_terminal_state():
    owner = ClientId("client:owner")
    sftp_runtime, _service_id, _client = _make_ready_sftp_service(owner)
    backend = _FakeScpBackend()
    transfer_runtime = TransferRuntime(sftp_runtime, scp_backend=backend)
    prepared = transfer_runtime.prepare_start_scp_transfer(
        _scp_request(), client_id=owner
    )

    assert transfer_runtime.prepare_cancel_transfer(
        CancelTransferRequest(transfer_id=prepared.id), client_id=owner
    )
    transfer_runtime.run_transfer(prepared.id)
    assert prepared.id not in transfer_runtime._worker_threads
    assert transfer_runtime.get_transfer(prepared.id).state is TransferState.CANCELLED


def test_prepare_start_transfer_returns_queued_summary():
    owner = ClientId("client:owner")
    sftp_runtime, service_id, _client = _make_ready_sftp_service(owner)
    transfer_runtime = TransferRuntime(sftp_runtime)
    local_path = _temp_source()
    summary = transfer_runtime.prepare_start_transfer(
        _upload_request(service_id, local_path), client_id=owner
    )
    assert summary.state is TransferState.QUEUED
    assert summary.started_at is None


def test_run_transfer_completes_upload():
    owner = ClientId("client:owner")
    sftp_runtime, service_id, client = _make_ready_sftp_service(owner)
    transfer_runtime = TransferRuntime(sftp_runtime)
    local_path = _temp_source()
    prepared = transfer_runtime.prepare_start_transfer(
        _upload_request(service_id, local_path), client_id=owner
    )
    transfer_runtime.run_transfer(prepared.id)
    summary = _wait_for_terminal_state(transfer_runtime, prepared.id)
    assert summary.state is TransferState.COMPLETED
    assert client.files["/remote/file.txt"] == b"hello world"


def test_cancel_before_run_marks_transfer_cancelled():
    owner = ClientId("client:owner")
    sftp_runtime, service_id, _client = _make_ready_sftp_service(owner)
    transfer_runtime = TransferRuntime(sftp_runtime)
    local_path = _temp_source()
    prepared = transfer_runtime.prepare_start_transfer(
        _upload_request(service_id, local_path), client_id=owner
    )
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
    local_path = _temp_source()
    prepared = transfer_runtime.prepare_start_transfer(
        _upload_request(service_id, local_path), client_id=owner
    )
    with pytest.raises(SshPilotError) as excinfo:
        transfer_runtime.prepare_cancel_transfer(
            CancelTransferRequest(transfer_id=prepared.id), client_id=other
        )
    assert excinfo.value.code is ErrorCode.SERVICE_OWNER_REQUIRED


def test_prepare_start_rejects_when_over_capacity():
    owner = ClientId("client:owner")
    sftp_runtime, service_id, _client = _make_ready_sftp_service(owner)
    transfer_runtime = TransferRuntime(
        sftp_runtime,
        max_concurrent_transfers=1,
        max_queued_transfers=1,
    )
    local_path = _temp_source()
    first = transfer_runtime.prepare_start_transfer(
        _upload_request(service_id, local_path, "/remote/a.txt"), client_id=owner
    )
    second = transfer_runtime.prepare_start_transfer(
        _upload_request(service_id, local_path, "/remote/b.txt"), client_id=owner
    )
    assert first.state is TransferState.QUEUED
    assert second.state is TransferState.QUEUED
    with pytest.raises(SshPilotError) as excinfo:
        transfer_runtime.prepare_start_transfer(
            _upload_request(service_id, local_path, "/remote/c.txt"), client_id=owner
        )
    assert excinfo.value.code is ErrorCode.SERVER_BUSY
    assert "transfer queue is full" in excinfo.value.message
    assert len(transfer_runtime.list_transfers()) == 2


def test_queued_transfer_stays_queued_until_worker_assigned():
    owner = ClientId("client:owner")
    client = _BlockingSftpClient()
    sftp_runtime, service_id, _ = _make_ready_sftp_service(owner, client=client)
    transfer_runtime = TransferRuntime(
        sftp_runtime,
        max_concurrent_transfers=1,
        max_queued_transfers=2,
    )
    first = transfer_runtime.prepare_start_transfer(
        _upload_request(service_id, _temp_source(b"one"), "/remote/first.txt"),
        client_id=owner,
    )
    second = transfer_runtime.prepare_start_transfer(
        _upload_request(service_id, _temp_source(b"two"), "/remote/second.txt"),
        client_id=owner,
    )
    transfer_runtime.run_transfer(first.id)
    _wait_until(
        client.write_started.is_set,
        message="first transfer never reached write",
    )
    assert transfer_runtime.get_transfer(first.id).state is TransferState.RUNNING
    transfer_runtime.run_transfer(second.id)
    _wait_until(
        lambda: second.id in transfer_runtime._pending_run,
        message="second transfer was not queued behind the active worker",
    )
    assert transfer_runtime.get_transfer(second.id).state is TransferState.QUEUED
    assert transfer_runtime.get_transfer(second.id).started_at is None
    client.allow_write.set()
    _wait_for_terminal_state(transfer_runtime, first.id)
    _wait_for_terminal_state(transfer_runtime, second.id)


def test_cancel_queued_transfer_without_worker_thread():
    owner = ClientId("client:owner")
    client = _BlockingSftpClient()
    sftp_runtime, service_id, _ = _make_ready_sftp_service(owner, client=client)
    transfer_runtime = TransferRuntime(
        sftp_runtime,
        max_concurrent_transfers=1,
        max_queued_transfers=2,
    )
    local_path = _temp_source()
    first = transfer_runtime.prepare_start_transfer(
        _upload_request(service_id, local_path, "/remote/first.txt"), client_id=owner
    )
    second = transfer_runtime.prepare_start_transfer(
        _upload_request(service_id, local_path, "/remote/second.txt"), client_id=owner
    )
    transfer_runtime.run_transfer(first.id)
    _wait_until(
        client.write_started.is_set,
        message="first transfer never reached write",
    )
    transfer_runtime.run_transfer(second.id)
    _wait_until(
        lambda: second.id in transfer_runtime._pending_run,
        message="second transfer was not queued",
    )
    assert transfer_runtime.prepare_cancel_transfer(
        CancelTransferRequest(transfer_id=second.id), client_id=owner
    )
    second_summary = transfer_runtime.get_transfer(second.id)
    assert second_summary.state is TransferState.CANCELLED
    assert second.id not in transfer_runtime._pending_run
    assert second.id not in transfer_runtime._worker_threads

    client.allow_write.set()
    first_summary = _wait_for_terminal_state(transfer_runtime, first.id)
    assert first_summary.state is TransferState.COMPLETED


def test_stress_more_starts_than_limit():
    owner = ClientId("client:owner")
    client = _BlockingSftpClient()
    sftp_runtime, service_id, _ = _make_ready_sftp_service(owner, client=client)
    max_concurrent = 2
    max_queued = 3
    capacity = max_concurrent + max_queued
    transfer_runtime = TransferRuntime(
        sftp_runtime,
        max_concurrent_transfers=max_concurrent,
        max_queued_transfers=max_queued,
    )
    local_path = _temp_source(b"x" * 64)
    prepared = []
    for index in range(capacity):
        prepared.append(
            transfer_runtime.prepare_start_transfer(
                _upload_request(service_id, local_path, f"/remote/file-{index}.txt"),
                client_id=owner,
            )
        )
    with pytest.raises(SshPilotError) as excinfo:
        transfer_runtime.prepare_start_transfer(
            _upload_request(service_id, local_path, "/remote/overflow.txt"),
            client_id=owner,
        )
    assert excinfo.value.code is ErrorCode.SERVER_BUSY

    for summary in prepared:
        transfer_runtime.run_transfer(summary.id)

    _wait_until(
        lambda: len(transfer_runtime._worker_threads) == max_concurrent,
        message="worker pool did not fill to max concurrent",
    )
    assert len(transfer_runtime._pending_run) == max_queued
    assert client.max_active_writes <= max_concurrent

    client.allow_write.set()
    for summary in prepared:
        final = _wait_for_terminal_state(transfer_runtime, summary.id, timeout=5.0)
        assert final.state is TransferState.COMPLETED


def test_constructor_rejects_non_positive_limits():
    owner = ClientId("client:owner")
    sftp_runtime, _service_id, _client = _make_ready_sftp_service(owner)
    with pytest.raises(ValueError, match="max concurrent transfers"):
        TransferRuntime(sftp_runtime, max_concurrent_transfers=0)
    with pytest.raises(ValueError, match="max queued transfers"):
        TransferRuntime(sftp_runtime, max_queued_transfers=0)


def test_shutdown_cancels_active_transfer_deterministically():
    """Race: shutdown while a transfer is mid-write must finish terminal."""

    owner = ClientId("client:owner")
    client = _BlockingSftpClient()
    sftp_runtime, service_id, _ = _make_ready_sftp_service(owner, client=client)
    transfer_runtime = TransferRuntime(
        sftp_runtime,
        max_concurrent_transfers=1,
        max_queued_transfers=1,
        shutdown_timeout_seconds=2.0,
    )
    local_path = _temp_source(b"shutdown-race")
    prepared = transfer_runtime.prepare_start_transfer(
        _upload_request(service_id, local_path, "/remote/shutdown.txt"),
        client_id=owner,
    )
    transfer_runtime.run_transfer(prepared.id)
    _wait_until(
        client.write_started.is_set,
        message="transfer never reached write before shutdown",
    )
    # Release the write after shutdown is requested so the worker observes cancel.
    def _release_soon():
        time.sleep(0.05)
        client.allow_write.set()

    threading.Thread(target=_release_soon, daemon=True).start()
    transfer_runtime.shutdown()
    record = transfer_runtime._records[prepared.id]
    assert record.state in _TERMINAL_STATES
    assert record.state in {TransferState.CANCELLED, TransferState.FAILED}


# -- recursive transfers ------------------------------------------------------


def _tree_source(root, tree):
    """Create ``tree`` (relpath -> bytes) under ``root``; returns the root dir."""
    root = tempfile.mkdtemp(prefix="sshpilot-tree-")
    for rel, payload in tree.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path) or root, exist_ok=True)
        with open(path, "wb") as f:
            f.write(payload)
    return root


def _tree_request(service_id, local_path, remote_path, direction, policy):
    return StartTransferRequest(
        connection_id=ConnectionId("demo"),
        sftp_service_id=service_id,
        direction=direction,
        remote_path=remote_path,
        local_path=local_path,
        conflict_policy=policy,
        recursive=True,
    )


def test_recursive_upload_copies_directory_tree():
    owner = ClientId("client:owner")
    client = _RecursiveSftpClient()
    sftp_runtime, service_id, _ = _make_ready_sftp_service(owner, client=client)
    transfer_runtime = TransferRuntime(sftp_runtime)
    local_root = _tree_source(None, {
        "a.txt": b"aaa",
        "sub/b.txt": b"bbbb",
        "sub/deep/c.txt": b"ccccc",
    })

    prepared = transfer_runtime.prepare_start_transfer(
        _tree_request(service_id, local_root, "/remote/dst", TransferDirection.UPLOAD,
                      TransferConflictPolicy.OVERWRITE),
        client_id=owner,
    )
    assert prepared.state is TransferState.QUEUED
    transfer_runtime.run_transfer(prepared.id)
    summary = _wait_for_terminal_state(transfer_runtime, prepared.id)

    assert summary.state is TransferState.COMPLETED
    assert summary.bytes_total == 12
    assert client.files["/remote/dst/a.txt"] == b"aaa"
    assert client.files["/remote/dst/sub/b.txt"] == b"bbbb"
    assert client.files["/remote/dst/sub/deep/c.txt"] == b"ccccc"
    assert "/remote/dst" in client.dirs
    assert "/remote/dst/sub/deep" in client.dirs


def test_recursive_upload_skips_existing_files_with_skip_policy():
    owner = ClientId("client:owner")
    client = _RecursiveSftpClient()
    client.files["/remote/dst/a.txt"] = b"existing"
    client.dirs.add("/remote/dst")
    sftp_runtime, service_id, _ = _make_ready_sftp_service(owner, client=client)
    transfer_runtime = TransferRuntime(sftp_runtime)
    local_root = _tree_source(None, {"a.txt": b"aaa", "b.txt": b"bbb"})

    prepared = transfer_runtime.prepare_start_transfer(
        _tree_request(service_id, local_root, "/remote/dst", TransferDirection.UPLOAD,
                      TransferConflictPolicy.SKIP),
        client_id=owner,
    )
    transfer_runtime.run_transfer(prepared.id)
    summary = _wait_for_terminal_state(transfer_runtime, prepared.id)

    assert summary.state is TransferState.COMPLETED
    assert client.files["/remote/dst/a.txt"] == b"existing"
    assert client.files["/remote/dst/b.txt"] == b"bbb"


def test_recursive_upload_missing_source_fails():
    owner = ClientId("client:owner")
    sftp_runtime, service_id, _client = _make_ready_sftp_service(owner)
    transfer_runtime = TransferRuntime(sftp_runtime)

    prepared = transfer_runtime.prepare_start_transfer(
        _tree_request(service_id, "/no/such/dir", "/remote/dst", TransferDirection.UPLOAD,
                      TransferConflictPolicy.OVERWRITE),
        client_id=owner,
    )
    transfer_runtime.run_transfer(prepared.id)
    summary = _wait_for_terminal_state(transfer_runtime, prepared.id)

    assert summary.state is TransferState.FAILED
    assert summary.failure.code == ErrorCode.TRANSFER_IO_FAILED.value


def test_recursive_download_copies_directory_tree(tmp_path):
    owner = ClientId("client:owner")
    client = _RecursiveSftpClient()
    client.files.update({
        "/home/user/docs/a.txt": b"aaa",
        "/home/user/docs/sub/b.txt": b"bbbb",
    })
    client.dirs.update({"/home/user/docs", "/home/user/docs/sub"})
    sftp_runtime, service_id, _ = _make_ready_sftp_service(owner, client=client)
    transfer_runtime = TransferRuntime(sftp_runtime)
    local_dst = tmp_path / "docs-copy"

    prepared = transfer_runtime.prepare_start_transfer(
        _tree_request(service_id, str(local_dst), "/home/user/docs", TransferDirection.DOWNLOAD,
                      TransferConflictPolicy.OVERWRITE),
        client_id=owner,
    )
    transfer_runtime.run_transfer(prepared.id)
    summary = _wait_for_terminal_state(transfer_runtime, prepared.id)

    assert summary.state is TransferState.COMPLETED
    assert summary.bytes_total == 7
    assert (local_dst / "a.txt").read_bytes() == b"aaa"
    assert (local_dst / "sub" / "b.txt").read_bytes() == b"bbbb"


def test_recursive_download_renames_existing_files(tmp_path):
    owner = ClientId("client:owner")
    client = _RecursiveSftpClient()
    client.files["/home/user/docs/a.txt"] = b"remote"
    client.dirs.add("/home/user/docs")
    sftp_runtime, service_id, _ = _make_ready_sftp_service(owner, client=client)
    transfer_runtime = TransferRuntime(sftp_runtime)
    local_dst = tmp_path / "docs-copy"
    local_dst.mkdir()
    (local_dst / "a.txt").write_text("local")

    prepared = transfer_runtime.prepare_start_transfer(
        _tree_request(service_id, str(local_dst), "/home/user/docs", TransferDirection.DOWNLOAD,
                      TransferConflictPolicy.RENAME),
        client_id=owner,
    )
    transfer_runtime.run_transfer(prepared.id)
    summary = _wait_for_terminal_state(transfer_runtime, prepared.id)

    assert summary.state is TransferState.COMPLETED
    assert (local_dst / "a.txt").read_text() == "local"
    assert (local_dst / "a (1).txt").read_bytes() == b"remote"


def test_recursive_download_source_not_directory_fails(tmp_path):
    owner = ClientId("client:owner")
    client = _RecursiveSftpClient()
    client.files["/home/user/file.txt"] = b"x"
    sftp_runtime, service_id, _ = _make_ready_sftp_service(owner, client=client)
    transfer_runtime = TransferRuntime(sftp_runtime)

    prepared = transfer_runtime.prepare_start_transfer(
        _tree_request(service_id, str(tmp_path / "dst"), "/home/user/file.txt",
                      TransferDirection.DOWNLOAD, TransferConflictPolicy.OVERWRITE),
        client_id=owner,
    )
    transfer_runtime.run_transfer(prepared.id)
    summary = _wait_for_terminal_state(transfer_runtime, prepared.id)

    assert summary.state is TransferState.FAILED


def test_recursive_transfer_cancel_marks_cancelled(tmp_path):
    owner = ClientId("client:owner")
    client = _RecursiveSftpClient()
    client.files["/home/user/docs/a.txt"] = b"aaa"
    client.dirs.add("/home/user/docs")
    sftp_runtime, service_id, _ = _make_ready_sftp_service(owner, client=client)
    transfer_runtime = TransferRuntime(sftp_runtime)

    prepared = transfer_runtime.prepare_start_transfer(
        _tree_request(service_id, str(tmp_path / "dst"), "/home/user/docs",
                      TransferDirection.DOWNLOAD, TransferConflictPolicy.OVERWRITE),
        client_id=owner,
    )
    assert transfer_runtime.prepare_cancel_transfer(
        CancelTransferRequest(transfer_id=prepared.id), client_id=owner
    )
    transfer_runtime.run_transfer(prepared.id)
    summary = transfer_runtime.get_transfer(prepared.id)
    assert summary.state is TransferState.CANCELLED


def test_non_recursive_dir_upload_rejected():
    owner = ClientId("client:owner")
    sftp_runtime, service_id, _client = _make_ready_sftp_service(owner)
    transfer_runtime = TransferRuntime(sftp_runtime)
    local_root = _tree_source(None, {"a.txt": b"a"})

    prepared = transfer_runtime.prepare_start_transfer(
        _upload_request(service_id, local_root, "/remote/dst"),
        client_id=owner,
    )
    transfer_runtime.run_transfer(prepared.id)
    summary = _wait_for_terminal_state(transfer_runtime, prepared.id)

    assert summary.state is TransferState.FAILED
    assert summary.failure.code == ErrorCode.TRANSFER_IO_FAILED.value


def test_recursive_upload_skip_policy_reaches_full_progress():
    owner = ClientId("client:owner")
    client = _RecursiveSftpClient()
    client.files["/remote/dst/a.txt"] = b"existing"
    client.dirs.add("/remote/dst")
    sftp_runtime, service_id, _ = _make_ready_sftp_service(owner, client=client)
    transfer_runtime = TransferRuntime(sftp_runtime)
    local_root = _tree_source(None, {"a.txt": b"aaa", "b.txt": b"bbb"})

    prepared = transfer_runtime.prepare_start_transfer(
        _tree_request(service_id, local_root, "/remote/dst", TransferDirection.UPLOAD,
                      TransferConflictPolicy.SKIP),
        client_id=owner,
    )
    transfer_runtime.run_transfer(prepared.id)
    summary = _wait_for_terminal_state(transfer_runtime, prepared.id)

    assert summary.state is TransferState.COMPLETED
    assert summary.bytes_total == 6
    assert summary.bytes_completed == summary.bytes_total


def test_recursive_download_skip_policy_reaches_full_progress(tmp_path):
    owner = ClientId("client:owner")
    client = _RecursiveSftpClient()
    client.files.update({
        "/home/user/docs/a.txt": b"remote-a",
        "/home/user/docs/b.txt": b"bbb",
    })
    client.dirs.add("/home/user/docs")
    sftp_runtime, service_id, _ = _make_ready_sftp_service(owner, client=client)
    transfer_runtime = TransferRuntime(sftp_runtime)
    local_dst = tmp_path / "docs-copy"
    local_dst.mkdir()
    (local_dst / "a.txt").write_text("local-a")

    prepared = transfer_runtime.prepare_start_transfer(
        _tree_request(service_id, str(local_dst), "/home/user/docs",
                      TransferDirection.DOWNLOAD, TransferConflictPolicy.SKIP),
        client_id=owner,
    )
    transfer_runtime.run_transfer(prepared.id)
    summary = _wait_for_terminal_state(transfer_runtime, prepared.id)

    assert summary.state is TransferState.COMPLETED
    assert summary.bytes_total == 11
    assert summary.bytes_completed == summary.bytes_total
    assert (local_dst / "a.txt").read_text() == "local-a"


def test_recursive_download_never_descends_into_symlinked_dirs(tmp_path):
    owner = ClientId("client:owner")
    client = _RecursiveSftpClient()
    client.files.update({
        "/home/user/docs/a.txt": b"aaa",
        "/home/user/secret/s.txt": b"secret",
        "/home/user/docs/linkdir": b"dereferenced",
    })
    client.dirs.update({"/home/user/docs", "/home/user/secret"})
    client.links["/home/user/docs/linkdir"] = "/home/user/secret"
    sftp_runtime, service_id, _ = _make_ready_sftp_service(owner, client=client)
    transfer_runtime = TransferRuntime(sftp_runtime)
    local_dst = tmp_path / "docs-copy"

    prepared = transfer_runtime.prepare_start_transfer(
        _tree_request(service_id, str(local_dst), "/home/user/docs",
                      TransferDirection.DOWNLOAD, TransferConflictPolicy.OVERWRITE),
        client_id=owner,
    )
    transfer_runtime.run_transfer(prepared.id)
    summary = _wait_for_terminal_state(transfer_runtime, prepared.id)

    assert summary.state is TransferState.COMPLETED
    assert (local_dst / "a.txt").read_bytes() == b"aaa"
    assert (local_dst / "linkdir").read_bytes() == b"dereferenced"
    assert not (local_dst / "secret").exists()
    assert not (local_dst / "s.txt").exists()
    assert client.listdir_paths == ["/home/user/docs"]


def test_recursive_upload_rejects_symlink_root(tmp_path):
    owner = ClientId("client:owner")
    client = _RecursiveSftpClient()
    sftp_runtime, service_id, _ = _make_ready_sftp_service(owner, client=client)
    transfer_runtime = TransferRuntime(sftp_runtime)
    target = _tree_source(None, {"secret.txt": b"secret"})
    link = tmp_path / "upload-link"
    link.symlink_to(target, target_is_directory=True)

    prepared = transfer_runtime.prepare_start_transfer(
        _tree_request(service_id, str(link), "/remote/dst", TransferDirection.UPLOAD,
                      TransferConflictPolicy.OVERWRITE),
        client_id=owner,
    )
    transfer_runtime.run_transfer(prepared.id)
    summary = _wait_for_terminal_state(transfer_runtime, prepared.id)

    assert summary.state is TransferState.FAILED
    assert summary.failure.code == ErrorCode.TRANSFER_IO_FAILED.value
    assert client.files == {}


def test_recursive_download_rejects_symlink_root(tmp_path):
    owner = ClientId("client:owner")
    client = _RecursiveSftpClient()
    client.files.update({
        "/home/user/secret/s.txt": b"secret",
    })
    client.dirs.update({"/home/user/secret", "/home/user"})
    client.links["/home/user/download-link"] = "/home/user/secret"
    sftp_runtime, service_id, _ = _make_ready_sftp_service(owner, client=client)
    transfer_runtime = TransferRuntime(sftp_runtime)
    local_dst = tmp_path / "dst"

    prepared = transfer_runtime.prepare_start_transfer(
        _tree_request(service_id, str(local_dst), "/home/user/download-link",
                      TransferDirection.DOWNLOAD, TransferConflictPolicy.OVERWRITE),
        client_id=owner,
    )
    transfer_runtime.run_transfer(prepared.id)
    summary = _wait_for_terminal_state(transfer_runtime, prepared.id)

    assert summary.state is TransferState.FAILED
    assert summary.failure.code == ErrorCode.TRANSFER_IO_FAILED.value
    assert not local_dst.exists()


def test_ensure_remote_dir_handles_real_sftp_missing_code():
    from sshpilot.sftp import protocol as sftp_proto

    owner = ClientId("client:owner")

    class _RealStatClient(_RecursiveSftpClient):
        def stat(self, path):
            if path in self.files or path in self.dirs:
                return super().stat(path)
            raise sftp_proto.SFTPError(sftp_proto.FX_NO_SUCH_FILE, "No such file")

    client = _RealStatClient()
    sftp_runtime, service_id, _ = _make_ready_sftp_service(owner, client=client)
    transfer_runtime = TransferRuntime(sftp_runtime)
    local_root = _tree_source(None, {"a.txt": b"aaa"})

    prepared = transfer_runtime.prepare_start_transfer(
        _tree_request(service_id, local_root, "/remote/newdir", TransferDirection.UPLOAD,
                      TransferConflictPolicy.OVERWRITE),
        client_id=owner,
    )
    transfer_runtime.run_transfer(prepared.id)
    summary = _wait_for_terminal_state(transfer_runtime, prepared.id)

    assert summary.state is TransferState.COMPLETED
    assert "/remote/newdir" in client.dirs
    assert client.files["/remote/newdir/a.txt"] == b"aaa"


def test_finish_completed_reconciles_single_file_skip():
    owner = ClientId("client:owner")
    client = _RecursiveSftpClient()
    client.files["/remote/file.txt"] = b"existing"
    sftp_runtime, service_id, _ = _make_ready_sftp_service(owner, client=client)
    transfer_runtime = TransferRuntime(sftp_runtime)
    local_source = _temp_source(b"new content")

    prepared = transfer_runtime.prepare_start_transfer(
        StartTransferRequest(
            connection_id=ConnectionId("demo"),
            sftp_service_id=service_id,
            direction=TransferDirection.UPLOAD,
            remote_path="/remote/file.txt",
            local_path=local_source,
            conflict_policy=TransferConflictPolicy.SKIP,
        ),
        client_id=owner,
    )
    transfer_runtime.run_transfer(prepared.id)
    summary = _wait_for_terminal_state(transfer_runtime, prepared.id)

    assert summary.state is TransferState.COMPLETED
    assert summary.bytes_total == 11
    assert summary.bytes_completed == summary.bytes_total


def test_transfer_runtime_client_can_interact_owner_only():
    owner = ClientId("client:owner")
    other = ClientId("client:other")
    sftp_runtime, _service_id, _client = _make_ready_sftp_service(owner)
    backend = _FakeScpBackend()
    transfer_runtime = TransferRuntime(sftp_runtime, scp_backend=backend)
    try:
        prepared = transfer_runtime.prepare_start_scp_transfer(
            _scp_request(), client_id=owner
        )
        assert transfer_runtime.client_can_interact(prepared.id, owner)
        assert not transfer_runtime.client_can_interact(prepared.id, other)
        assert not transfer_runtime.client_can_interact(
            TransferId("transfer-999999"), owner
        )
    finally:
        transfer_runtime.shutdown()


def test_transfer_runtime_client_can_interact_denies_sftp_backed_transfers():
    """SFTP-backed transfers scope to the SFTP service id, never ``transfer-``."""
    owner = ClientId("client:owner")
    sftp_runtime, service_id, _client = _make_ready_sftp_service(owner)
    transfer_runtime = TransferRuntime(sftp_runtime)
    try:
        prepared = transfer_runtime.prepare_start_transfer(
            StartTransferRequest(
                connection_id=ConnectionId("demo"),
                sftp_service_id=service_id,
                direction=TransferDirection.UPLOAD,
                remote_path="/remote/a.txt",
                local_path=_temp_source(b"data"),
                conflict_policy=TransferConflictPolicy.OVERWRITE,
            ),
            client_id=owner,
        )
        # A ``transfer-`` scope was never created for this transfer; the owner
        # must not be able to claim interactions under it.
        assert not transfer_runtime.client_can_interact(prepared.id, owner)
    finally:
        transfer_runtime.shutdown()

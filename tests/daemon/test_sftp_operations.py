"""Daemon operation lifecycle for recursive SFTP size/remove/copy.

These are the headless scenarios that prove recursive tree work runs on the
shared operation worker with progress, cancellation, and a typed result
instead of blocking the SFTP command stream.
"""

from __future__ import annotations

import posixpath
import threading
import time
from types import SimpleNamespace

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ClientId, ConnectionId
from sshpilot.api.models.operations import (
    OperationKind,
    OperationState,
    OpenSftpRequest,
    SftpCopyRequest,
    SftpDirectorySizeRequest,
    SftpPathRequest,
    is_terminal_operation_state,
)
from sshpilot.daemon.operation_runtime import OperationRuntime
from sshpilot.daemon.sftp_runtime import SftpServiceRuntime
from sshpilot.sftp import protocol as sftp_proto

OWNER = ClientId("client:owner")


class _FileHandle:
    def __init__(self, fs, path, mode):
        self._fs = fs
        self._path = path
        self._mode = mode
        if mode == "rb":
            self._data = fs.files.get(path, b"")
            self._pos = 0
        else:
            fs.files.setdefault(path, b"")
            self._data = bytearray(fs.files.get(path, b""))

    def read(self, size=None):
        if size is None:
            data = self._data[self._pos:]
        else:
            data = self._data[self._pos : self._pos + size]
        self._pos += len(data)
        return data

    def write(self, data):
        self._data.extend(data)

    def close(self):
        if self._mode == "wb":
            self._fs.files[self._path] = bytes(self._data)
            self._fs._register_child(self._path)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


class _FsClient:
    """In-memory remote filesystem double for tree walks and copies.

    ``links`` marks a path as a symlink; an optional ``link_targets`` entry
    lets ``stat`` (follow) resolve it to a real target, mirroring how a real
    SFTP server's ``stat`` differs from ``lstat`` for a symlink. Without a
    registered target a link is just a dangling/opaque leaf, as before.
    """

    def __init__(self):
        self.files = {}
        self.dirs = {}
        self.links = set()
        self.link_targets = {}

    def _attr(self, path, *, follow=False):
        if follow and path in self.link_targets:
            return self._attr(self.link_targets[path], follow=follow)
        is_link = path in self.links
        if path in self.files:
            return SimpleNamespace(
                st_size=len(self.files[path]),
                is_dir=lambda: False,
                is_symlink=lambda: is_link,
            )
        if is_link:
            return SimpleNamespace(
                st_size=0, is_dir=lambda: False, is_symlink=lambda: True
            )
        if path in self.dirs:
            return SimpleNamespace(
                st_size=0, is_dir=lambda: True, is_symlink=lambda: False
            )
        raise sftp_proto.SFTPError(sftp_proto.FX_NO_SUCH_FILE)

    def lstat(self, path):
        return self._attr(path, follow=False)

    def stat(self, path):
        return self._attr(path, follow=True)

    def listdir_attr(self, path):
        if path not in self.dirs:
            raise sftp_proto.SFTPError(sftp_proto.FX_NO_SUCH_FILE)
        attrs = []
        for name in self.dirs[path]:
            child = posixpath.join(path, name)
            attr = self._attr(child)
            attrs.append(SimpleNamespace(filename=name, **attr.__dict__))
        return attrs

    def _unregister_child(self, path):
        parent = posixpath.dirname(path)
        name = posixpath.basename(path)
        if parent in self.dirs:
            self.dirs[parent].discard(name)

    def remove(self, path):
        self.files.pop(path, None)
        self.links.discard(path)
        self._unregister_child(path)
        if path in self.dirs:
            raise sftp_proto.SFTPError(sftp_proto.FX_FAILURE)

    def rmdir(self, path):
        if path not in self.dirs or self.dirs[path]:
            raise sftp_proto.SFTPError(sftp_proto.FX_FAILURE)
        self.dirs.pop(path, None)
        self._unregister_child(path)

    def mkdir(self, path):
        self.dirs.setdefault(path, set())
        self._register_child(path)

    def _register_child(self, path):
        parent = posixpath.dirname(path)
        name = posixpath.basename(path)
        if parent in self.dirs and name:
            self.dirs[parent].add(name)

    def open(self, path, mode):
        return _FileHandle(self, path, mode)

    def add_dir(self, path, children=()):
        self.dirs.setdefault(path, set()).update(children)

    def add_file(self, path, content=b""):
        self.files[path] = content

    def add_link(self, path, target=None):
        self.links.add(path)
        if target is not None:
            self.link_targets[path] = target


class _CoreClient:
    def __init__(self):
        self.connection = SimpleNamespace(
            id="conn-1", protocol="ssh", hostname="host", username="alice", port=22
        )

    def get_connection(self, _connection_id):
        return self.connection


class _Runner:
    def __init__(self, client):
        self.client = client
        self.closed = False

    def start(self, _spec):
        return SimpleNamespace(client=self.client)

    def close(self):
        self.closed = True


def _make_runtime(client):
    ops = OperationRuntime()
    runtime = SftpServiceRuntime(
        _CoreClient(), runner=_Runner(client), operation_lifecycle=ops
    )
    summary = runtime.prepare_open_service(
        OpenSftpRequest(ConnectionId("conn-1")), client_id=OWNER
    )
    runtime.start_service(summary.id)
    return runtime, ops, summary


def _await_terminal(ops, operation_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        summary = ops.get_operation(operation_id)
        if is_terminal_operation_state(summary.state):
            return summary
        time.sleep(0.01)
    raise AssertionError(f"operation {operation_id} did not finish")


def _tree_client():
    fs = _FsClient()
    fs.add_dir("/tree", {"a.txt", "sub", "link"})
    fs.add_file("/tree/a.txt", b"hello")
    fs.add_dir("/tree/sub", {"b.txt"})
    fs.add_file("/tree/sub/b.txt", b"world!")
    fs.add_link("/tree/link")
    return fs


def test_directory_size_operation_delivers_result_payload():
    fs = _tree_client()
    runtime, ops, summary = _make_runtime(fs)
    started = runtime.start_directory_size(
        SftpDirectorySizeRequest(summary.id, "/tree"), client_id=OWNER
    )
    assert started.kind is OperationKind.SFTP_DIRECTORY_SIZE
    done = _await_terminal(ops, started.operation_id)
    assert done.state is OperationState.SUCCEEDED
    assert done.result["path"] == "/tree"
    assert done.result["size_bytes"] == 11
    assert done.result["file_count"] == 3
    assert done.result["directory_count"] == 1
    assert done.progress == 1.0


def test_directory_size_operation_missing_root_fails_structured():
    fs = _FsClient()
    runtime, ops, summary = _make_runtime(fs)
    started = runtime.start_directory_size(
        SftpDirectorySizeRequest(summary.id, "/nope"), client_id=OWNER
    )
    done = _await_terminal(ops, started.operation_id)
    assert done.state is OperationState.FAILED
    assert done.failure.code == ErrorCode.REMOTE_PATH_NOT_FOUND.value


def test_start_directory_size_without_lifecycle_is_unsupported():
    runtime = SftpServiceRuntime(_CoreClient(), runner=_Runner(_FsClient()))
    summary = runtime.prepare_open_service(
        OpenSftpRequest(ConnectionId("conn-1")), client_id=OWNER
    )
    runtime.start_service(summary.id)
    with pytest.raises(SshPilotError) as raised:
        runtime.start_directory_size(
            SftpDirectorySizeRequest(summary.id, "/tree"), client_id=OWNER
        )
    assert raised.value.code is ErrorCode.UNSUPPORTED_CAPABILITY


def test_directory_size_operation_cancel_is_cooperative():
    fs = _tree_client()
    release = threading.Event()
    original = fs.listdir_attr

    def _slow_listdir(path):
        if path == "/tree":
            release.wait(timeout=5.0)
        return original(path)

    fs.listdir_attr = _slow_listdir
    runtime, ops, summary = _make_runtime(fs)
    started = runtime.start_directory_size(
        SftpDirectorySizeRequest(summary.id, "/tree"), client_id=OWNER
    )
    for _ in range(100):
        current = ops.get_operation(started.operation_id)
        if current.state is OperationState.RUNNING:
            break
        time.sleep(0.01)
    ops.cancel_operation(started.operation_id)
    release.set()
    done = _await_terminal(ops, started.operation_id)
    assert done.state is OperationState.CANCELLED


def test_remove_tree_operation_deletes_recursively():
    fs = _tree_client()
    runtime, ops, summary = _make_runtime(fs)
    started = runtime.start_remove(
        SftpPathRequest(summary.id, "/tree", recursive=True), client_id=OWNER
    )
    assert started.kind is OperationKind.SFTP_REMOVE_TREE
    done = _await_terminal(ops, started.operation_id)
    assert done.state is OperationState.SUCCEEDED
    assert fs.dirs == {}
    assert fs.files == {}


def test_remove_tree_operation_missing_path_is_idempotent():
    fs = _FsClient()
    runtime, ops, summary = _make_runtime(fs)
    started = runtime.start_remove(
        SftpPathRequest(summary.id, "/nope", recursive=True), client_id=OWNER
    )
    done = _await_terminal(ops, started.operation_id)
    assert done.state is OperationState.SUCCEEDED


def test_copy_tree_operation_copies_recursively():
    fs = _tree_client()
    runtime, ops, summary = _make_runtime(fs)
    started = runtime.start_copy(
        SftpCopyRequest(
            summary.id, "/tree", "/destination", recursive=True, move=False
        ),
        client_id=OWNER,
    )
    assert started.kind is OperationKind.SFTP_COPY_TREE
    done = _await_terminal(ops, started.operation_id)
    assert done.state is OperationState.SUCCEEDED
    assert fs.files["/destination/a.txt"] == b"hello"
    assert fs.files["/destination/sub/b.txt"] == b"world!"
    assert fs.files["/tree/a.txt"] == b"hello"


def test_move_tree_operation_removes_source_after_copy():
    fs = _tree_client()
    runtime, ops, summary = _make_runtime(fs)
    started = runtime.start_copy(
        SftpCopyRequest(
            summary.id, "/tree", "/destination", recursive=True, move=True
        ),
        client_id=OWNER,
    )
    done = _await_terminal(ops, started.operation_id)
    assert done.state is OperationState.SUCCEEDED
    assert fs.files["/destination/a.txt"] == b"hello"
    assert "/tree/a.txt" not in fs.files
    assert fs.dirs == {"/destination": {"a.txt", "sub", "link"}, "/destination/sub": {"b.txt"}}


def test_copy_tree_operation_rejects_descendant_destination():
    fs = _tree_client()
    runtime, ops, summary = _make_runtime(fs)
    with pytest.raises(SshPilotError) as raised:
        runtime.start_copy(
            SftpCopyRequest(
                summary.id, "/tree", "/tree/sub/copy", recursive=True
            ),
            client_id=OWNER,
        )
    assert raised.value.code is ErrorCode.VALIDATION_FAILED


def _root_symlink_client():
    """``/link`` is a symlink whose target, ``/important-data``, is a real
    directory with real content -- the no-follow regression scenario: naive
    root classification via a following ``stat()`` would see ``/link`` as a
    directory and walk straight into someone else's tree.
    """
    fs = _FsClient()
    fs.add_dir("/important-data", {"secret.txt"})
    fs.add_file("/important-data/secret.txt", b"do-not-delete")
    fs.add_link("/link", target="/important-data")
    return fs


def test_copy_tree_operation_rejects_recursive_root_symlink():
    fs = _root_symlink_client()
    runtime, ops, summary = _make_runtime(fs)
    started = runtime.start_copy(
        SftpCopyRequest(summary.id, "/link", "/destination", recursive=True, move=False),
        client_id=OWNER,
    )
    done = _await_terminal(ops, started.operation_id)
    assert done.state is OperationState.FAILED
    assert done.failure.code == ErrorCode.VALIDATION_FAILED.value
    assert "/destination" not in fs.dirs
    assert fs.files["/important-data/secret.txt"] == b"do-not-delete"


def test_move_tree_operation_rejects_root_symlink_and_never_touches_target():
    fs = _root_symlink_client()
    runtime, ops, summary = _make_runtime(fs)
    started = runtime.start_copy(
        SftpCopyRequest(summary.id, "/link", "/destination", recursive=True, move=True),
        client_id=OWNER,
    )
    done = _await_terminal(ops, started.operation_id)
    assert done.state is OperationState.FAILED
    assert done.failure.code == ErrorCode.VALIDATION_FAILED.value
    # The real target directory must survive untouched: the no-follow policy
    # means the root symlink is rejected before any walk, so move cleanup
    # never gets a chance to delete through the link into /important-data.
    assert fs.dirs["/important-data"] == {"secret.txt"}
    assert fs.files["/important-data/secret.txt"] == b"do-not-delete"


def test_directory_size_operation_rejects_root_symlink():
    fs = _root_symlink_client()
    runtime, ops, summary = _make_runtime(fs)
    started = runtime.start_directory_size(
        SftpDirectorySizeRequest(summary.id, "/link"), client_id=OWNER
    )
    done = _await_terminal(ops, started.operation_id)
    assert done.state is OperationState.FAILED
    assert done.failure.code == ErrorCode.VALIDATION_FAILED.value

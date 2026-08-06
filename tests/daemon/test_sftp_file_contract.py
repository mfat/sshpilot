from __future__ import annotations

import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ClientId, ConnectionId
from sshpilot.api.models.operations import (
    OpenSftpRequest,
    SftpFileTarget,
    SftpReadFileRequest,
    SftpReplaceFileRequest,
)
from sshpilot.daemon.sftp_runtime import SftpServiceRuntime


def test_local_authorized_keys_read_is_bounded_and_revisioned(tmp_path, monkeypatch):
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    path = ssh_dir / "authorized_keys"
    path.write_text("comment\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))

    runtime = SftpServiceRuntime(SimpleNamespace())
    result = runtime.read_file(
        SftpReadFileRequest(SftpFileTarget.LOCAL_AUTHORIZED_KEYS, "~/.ssh/authorized_keys"),
        client_id=ClientId("client:test"),
    )

    assert result.exists is True
    assert result.content == "comment\n"
    assert result.revision
    assert result.size == len(result.content.encode())


def test_local_replace_rejects_stale_revision(tmp_path, monkeypatch):
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    path = ssh_dir / "authorized_keys"
    path.write_text("old\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))

    runtime = SftpServiceRuntime(SimpleNamespace())
    with pytest.raises(SshPilotError) as raised:
        runtime.replace_file(
            SftpReplaceFileRequest(
                SftpFileTarget.LOCAL_AUTHORIZED_KEYS,
                "~/.ssh/authorized_keys",
                "new\n",
                "wrong-revision",
                backup=True,
            ),
            client_id=ClientId("client:test"),
        )
    assert raised.value.code is ErrorCode.FILE_REVISION_CONFLICT
    assert path.read_text(encoding="utf-8") == "old\n"


def test_local_replace_is_atomic_secure_and_backed_up(tmp_path, monkeypatch):
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir(mode=0o755)
    path = ssh_dir / "authorized_keys"
    path.write_text("old\n", encoding="utf-8")
    os.chmod(path, 0o644)
    monkeypatch.setenv("HOME", str(tmp_path))

    runtime = SftpServiceRuntime(SimpleNamespace())
    old = runtime.read_file(
        SftpReadFileRequest(SftpFileTarget.LOCAL_AUTHORIZED_KEYS, "~/.ssh/authorized_keys"),
        client_id=ClientId("client:test"),
    )
    result = runtime.replace_file(
        SftpReplaceFileRequest(
            SftpFileTarget.LOCAL_AUTHORIZED_KEYS,
            "~/.ssh/authorized_keys",
            "new\n",
            old.revision,
            backup=True,
        ),
        client_id=ClientId("client:test"),
    )

    assert path.read_text(encoding="utf-8") == "new\n"
    assert path.stat().st_mode & 0o777 == 0o600
    assert ssh_dir.stat().st_mode & 0o777 == 0o700
    assert result.backup_path
    assert Path(result.backup_path).read_text(encoding="utf-8") == "old\n"
    assert not list(ssh_dir.glob("*.sshpilot.tmp-*"))


def test_local_replace_cleans_up_per_target_sync_state(tmp_path, monkeypatch):
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    path = ssh_dir / "authorized_keys"
    path.write_text("old\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))

    runtime = SftpServiceRuntime(SimpleNamespace())
    old = runtime.read_file(
        SftpReadFileRequest(SftpFileTarget.LOCAL_AUTHORIZED_KEYS, "~/.ssh/authorized_keys"),
        client_id=ClientId("client:test"),
    )
    runtime.replace_file(
        SftpReplaceFileRequest(
            SftpFileTarget.LOCAL_AUTHORIZED_KEYS,
            "~/.ssh/authorized_keys",
            "new\n",
            old.revision,
            backup=True,
        ),
        client_id=ClientId("client:test"),
    )
    assert runtime._target_locks == {}


def test_local_target_rejects_arbitrary_paths():
    with pytest.raises(ValueError):
        SftpReadFileRequest(SftpFileTarget.LOCAL_AUTHORIZED_KEYS, "/etc/passwd")


def test_local_concurrent_same_revision_replacements_serialize(tmp_path, monkeypatch):
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    path = ssh_dir / "authorized_keys"
    path.write_text("original\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))

    runtime = SftpServiceRuntime(SimpleNamespace())
    original = runtime.read_file(
        SftpReadFileRequest(
            SftpFileTarget.LOCAL_AUTHORIZED_KEYS, "~/.ssh/authorized_keys"
        ),
        client_id=ClientId("client:test"),
    )

    barrier = threading.Barrier(2)
    results = []
    errors = []

    def replace(content):
        barrier.wait()
        try:
            result = runtime.replace_file(
                SftpReplaceFileRequest(
                    SftpFileTarget.LOCAL_AUTHORIZED_KEYS,
                    "~/.ssh/authorized_keys",
                    content,
                    original.revision,
                    backup=True,
                ),
                client_id=ClientId("client:test"),
            )
            results.append((content, result))
        except SshPilotError as exc:
            errors.append((content, exc.code))

    first = threading.Thread(target=replace, args=("winner-a\n",))
    second = threading.Thread(target=replace, args=("winner-b\n",))
    first.start()
    second.start()
    first.join(10)
    second.join(10)

    assert not first.is_alive() and not second.is_alive()
    assert len(results) == 1
    assert sorted(code for _, code in errors) == [ErrorCode.FILE_REVISION_CONFLICT]

    winner_content, winner_result = results[0]
    assert winner_content == path.read_text(encoding="utf-8")
    assert winner_result.size == len(winner_content.encode())
    assert path.stat().st_mode & 0o777 == 0o600
    assert ssh_dir.stat().st_mode & 0o777 == 0o700
    backups = list(ssh_dir.glob("authorized_keys.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "original\n"
    assert not list(ssh_dir.glob("*.sshpilot.tmp-*"))


class _RemoteConnection:
    def __init__(self):
        self.id = ConnectionId("demo")
        self.protocol = "ssh"
        self.hostname = "example.test"
        self.username = "alice"
        self.port = 22


class _RemoteCoreClient:
    def __init__(self):
        self.connection = _RemoteConnection()

    def get_connection(self, _connection_id):
        return self.connection


class _RemoteSftpClient:
    """Thread-safe in-memory SFTP double used by the runtime's file ops."""

    def __init__(self):
        self.files = {}
        self.modes = {}
        self.closed = False
        self._active = 0
        self.max_active = 0
        self._active_lock = threading.Lock()

    def _enter_op(self):
        with self._active_lock:
            if self.closed:
                raise OSError("sftp connection closed")
            self._active += 1
            self.max_active = max(self.max_active, self._active)

    def _exit_op(self):
        with self._active_lock:
            self._active -= 1

    def stat(self, path):
        self._enter_op()
        try:
            if path not in self.files:
                raise FileNotFoundError(path)
            return SimpleNamespace(
                st_size=len(self.files[path]),
                st_mode=0o100600 | self.modes.get(path, 0o600),
                st_uid=0,
                st_mtime=0,
            )
        finally:
            self._exit_op()

    def file(self, path, mode):
        self._enter_op()
        try:
            if mode == "wb":
                self.files.setdefault(path, b"")
                self.modes.setdefault(path, 0o600)
                return _RemoteFileHandle(self, path, write=True)
            self.files.setdefault(path, b"")
            return _RemoteFileHandle(self, path, write=False)
        finally:
            self._exit_op()

    def chmod(self, path, mode):
        self._enter_op()
        try:
            self.modes[path] = mode
        finally:
            self._exit_op()

    def mkdir(self, path, _mode):
        self._enter_op()
        try:
            if path in self.files or path in self.modes:
                return
            self.modes[path] = 0o700
        finally:
            self._exit_op()

    def posix_rename(self, old, new):
        self._enter_op()
        try:
            self.files[new] = self.files.pop(old, b"")
            self.modes[new] = self.modes.pop(old, 0o600)
        finally:
            self._exit_op()

    def rename(self, old, new):
        self.posix_rename(old, new)

    def remove(self, path):
        self._enter_op()
        try:
            self.files.pop(path, None)
            self.modes.pop(path, None)
        finally:
            self._exit_op()

    def close(self):
        self.closed = True


class _RemoteFileHandle:
    def __init__(self, client, path, *, write):
        self._client = client
        self._path = path
        self._write = write
        self._data = b"" if write else bytes(client.files.get(path, b""))
        self._offset = 0

    def read(self, _size=None):
        data = self._data
        return data

    def write(self, data):
        self._data = self._data[: self._offset] + data
        self._offset += len(data)

    def close(self):
        if self._write:
            self._client.files[self._path] = self._data
            self._client.modes[self._path] = 0o600

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


class _BlockingRemoteSftpClient(_RemoteSftpClient):
    """Blocks the first temp-file write so a same-target race can be observed."""

    def __init__(self):
        super().__init__()
        self.first_write_blocked = threading.Event()
        self.allow_write = threading.Event()
        self._wb_blocked = False

    def file(self, path, mode):
        if mode == "wb" and not self._wb_blocked:
            self._wb_blocked = True
            self.first_write_blocked.set()
            if not self.allow_write.wait(timeout=5.0):
                raise TimeoutError("blocking SFTP write was not released")
        return super().file(path, mode)


class _RemoteFakeHandle:
    def __init__(self, client):
        self.client = client
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.client.close()

    def wait(self, timeout):
        del timeout
        return True


class _RemoteFakeRunner:
    def __init__(self, client):
        self.client = client
        self.handles = []
        self.closed = False

    def start(self, spec):
        handle = _RemoteFakeHandle(self.client)
        self.handles.append(handle)
        return handle

    def close(self):
        self.closed = True


def _remote_runtime(client=None):
    client = client or _RemoteSftpClient()
    runner = _RemoteFakeRunner(client)
    runtime = SftpServiceRuntime(_RemoteCoreClient(), runner=runner)
    summary = runtime.prepare_open_service(
        OpenSftpRequest(ConnectionId("demo")), client_id=ClientId("client:owner")
    )
    runtime.start_service(summary.id)
    return runtime, runner, summary


def test_remote_same_revision_concurrent_replacements_serialize():
    client = _BlockingRemoteSftpClient()
    runtime, _, summary = _remote_runtime(client)
    client.files["/remote/target"] = b"original\n"
    client.modes["/remote/target"] = 0o600

    original = runtime.read_file(
        SftpReadFileRequest(
            SftpFileTarget.REMOTE, "/remote/target", summary.id
        ),
        client_id=ClientId("client:owner"),
    )
    assert original.exists

    barrier = threading.Barrier(2)
    results = []
    errors = []

    def replace(content):
        barrier.wait()
        try:
            result = runtime.replace_file(
                SftpReplaceFileRequest(
                    SftpFileTarget.REMOTE,
                    "/remote/target",
                    content,
                    original.revision,
                    backup=True,
                    service_id=summary.id,
                ),
                client_id=ClientId("client:owner"),
            )
            results.append((content, result))
        except SshPilotError as exc:
            errors.append((content, exc.code))

    first = threading.Thread(target=replace, args=("winner-a\n",))
    second = threading.Thread(target=replace, args=("winner-b\n",))
    first.start()
    second.start()

    assert client.first_write_blocked.wait(timeout=5.0)
    assert client.max_active == 1

    client.allow_write.set()
    first.join(10)
    second.join(10)

    assert not first.is_alive() and not second.is_alive()
    assert len(results) == 1
    assert sorted(code for _, code in errors) == [ErrorCode.FILE_REVISION_CONFLICT]
    assert client.max_active == 1

    winner_content, winner_result = results[0]
    assert client.files["/remote/target"] == winner_content.encode()
    assert winner_result.size == len(winner_content.encode())
    assert not any(name.endswith(".sshpilot.tmp-") for name in client.files)


def test_remote_unrelated_targets_proceed_independently():
    client = _BlockingRemoteSftpClient()
    runtime, _, summary = _remote_runtime(client)
    client.files["/remote/a"] = b"a\n"
    client.files["/remote/b"] = b"b\n"
    client.modes["/remote/a"] = 0o600
    client.modes["/remote/b"] = 0o600

    revision_a = runtime.read_file(
        SftpReadFileRequest(SftpFileTarget.REMOTE, "/remote/a", summary.id),
        client_id=ClientId("client:owner"),
    ).revision
    revision_b = runtime.read_file(
        SftpReadFileRequest(SftpFileTarget.REMOTE, "/remote/b", summary.id),
        client_id=ClientId("client:owner"),
    ).revision

    results = []
    errors = []

    def replace_a():
        try:
            results.append(
                runtime.replace_file(
                    SftpReplaceFileRequest(
                        SftpFileTarget.REMOTE,
                        "/remote/a",
                        "a-new\n",
                        revision_a,
                        backup=True,
                        service_id=summary.id,
                    ),
                    client_id=ClientId("client:owner"),
                )
            )
        except SshPilotError as exc:
            errors.append(exc.code)

    def replace_b():
        try:
            results.append(
                runtime.replace_file(
                    SftpReplaceFileRequest(
                        SftpFileTarget.REMOTE,
                        "/remote/b",
                        "b-new\n",
                        revision_b,
                        backup=True,
                        service_id=summary.id,
                    ),
                    client_id=ClientId("client:owner"),
                )
            )
        except SshPilotError as exc:
            errors.append(exc.code)

    first = threading.Thread(target=replace_a)
    second = threading.Thread(target=replace_b)
    first.start()

    assert client.first_write_blocked.wait(timeout=5.0)

    second.start()
    second.join(10)
    assert not second.is_alive()
    assert client.files["/remote/b"] == b"b-new\n"

    client.allow_write.set()
    first.join(10)
    assert not first.is_alive()
    assert client.files["/remote/a"] == b"a-new\n"
    assert not errors
    assert len(results) == 2


def test_shutdown_rejects_waiting_remote_replacement_on_closed_handle():
    client = _BlockingRemoteSftpClient()
    runtime, _, summary = _remote_runtime(client)
    client.files["/remote/target"] = b"original\n"
    client.modes["/remote/target"] = 0o600

    original = runtime.read_file(
        SftpReadFileRequest(SftpFileTarget.REMOTE, "/remote/target", summary.id),
        client_id=ClientId("client:owner"),
    )

    outcomes = []

    def replace(content):
        try:
            result = runtime.replace_file(
                SftpReplaceFileRequest(
                    SftpFileTarget.REMOTE,
                    "/remote/target",
                    content,
                    original.revision,
                    backup=True,
                    service_id=summary.id,
                ),
                client_id=ClientId("client:owner"),
            )
            outcomes.append(("ok", content, result))
        except SshPilotError as exc:
            outcomes.append(("error", content, exc.code))

    executing = threading.Thread(target=replace, args=("first\n",))
    executing.start()
    assert client.first_write_blocked.wait(timeout=5.0)

    waiting = threading.Thread(target=replace, args=("second\n",))
    waiting.start()

    runtime.shutdown()
    client.allow_write.set()
    executing.join(10)
    waiting.join(10)

    assert not executing.is_alive() and not waiting.is_alive()
    assert len(outcomes) == 2

    error_codes = [code for kind, _, code in outcomes if kind == "error"]
    assert error_codes
    assert all(
        code is not None
        and code
        not in {
            ErrorCode.INVALID_REQUEST,
            ErrorCode.FILE_CONTENT_TOO_LARGE,
            ErrorCode.FILE_REVISION_CONFLICT,
        }
        for code in error_codes
    )
    waiting_codes = {
        code for kind, content, code in outcomes if kind == "error" and content == "second\n"
    }
    assert ErrorCode.SFTP_SERVICE_NOT_READY in waiting_codes


def test_shutdown_rejects_waiting_local_replacement(tmp_path, monkeypatch):
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    path = ssh_dir / "authorized_keys"
    path.write_text("original\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))

    runtime = SftpServiceRuntime(SimpleNamespace())
    original = runtime.read_file(
        SftpReadFileRequest(SftpFileTarget.LOCAL_AUTHORIZED_KEYS, "~/.ssh/authorized_keys"),
        client_id=ClientId("client:test"),
    )
    key = (
        SftpFileTarget.LOCAL_AUTHORIZED_KEYS,
        os.path.abspath(os.path.expanduser("~/.ssh/authorized_keys")),
    )
    holder = runtime._serialized_target(key)
    holder.__enter__()
    try:
        outcomes = []

        def replace():
            try:
                runtime.replace_file(
                    SftpReplaceFileRequest(
                        SftpFileTarget.LOCAL_AUTHORIZED_KEYS,
                        "~/.ssh/authorized_keys",
                        "new\n",
                        original.revision,
                        backup=True,
                    ),
                    client_id=ClientId("client:test"),
                )
                outcomes.append("ok")
            except SshPilotError as exc:
                outcomes.append(exc.code)

        worker = threading.Thread(target=replace)
        worker.start()
        runtime.shutdown()
        holder.__exit__(None, None, None)
        worker.join(10)
    finally:
        try:
            holder.__exit__(None, None, None)
        except Exception:
            pass

    assert not worker.is_alive()
    assert outcomes == [ErrorCode.DAEMON_SHUTTING_DOWN]
    assert path.read_text(encoding="utf-8") == "original\n"

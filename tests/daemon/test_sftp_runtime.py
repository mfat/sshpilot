"""Minimal lifecycle coverage for SftpServiceRuntime with a mocked process runner."""

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ClientId, ConnectionId
from sshpilot.api.models.operations import (
    AttachSftpRequest,
    CloseSftpRequest,
    ListDirectoryRequest,
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
    def __init__(self, name, is_dir=False, is_link=False):
        self.filename = name
        self._is_dir = is_dir
        self._is_link = is_link
        if is_link:
            self.st_mode = 0o120777
        else:
            self.st_mode = 0o040755 if is_dir else 0o100644
        self.st_size = 0
        self.st_uid = 0
        self.st_gid = 0
        self.st_mtime = None

    def is_dir(self):
        return self._is_dir

    def is_symlink(self):
        return self._is_link


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
        self.symlinks = {}
        # The real OpenSSHSFTPClient.realpath(".") returns the sftp-server's
        # cwd (the user's home); the fake mirrors that.
        self.cwd = "/"

    def realpath(self, path):
        return self.cwd if path == "." else path

    def stat(self, path):
        if path in self.directories:
            return _Attr(path, is_dir=True)
        if path in self.symlinks:
            return _Attr(path, is_dir=False, is_link=False)
        if path in self.files:
            return _Attr(path)
        raise sftp_proto.SFTPError(sftp_proto.FX_NO_SUCH_FILE, "missing")

    def lstat(self, path):
        if path in self.symlinks:
            return _Attr(path, is_dir=False, is_link=True)
        return self.stat(path)

    def listdir_attr(self, path):
        prefix = path.rstrip("/") + "/"
        result = []
        for child in sorted(self.directories | set(self.files) | set(self.symlinks)):
            if child.startswith(prefix) and "/" not in child[len(prefix):]:
                if child in self.symlinks:
                    result.append(_Attr(child[len(prefix):], is_link=True))
                else:
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
        self.symlinks.pop(path, None)

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
        self.on_exit = None

    def start(self, spec, on_exit=None):
        del spec
        self.on_exit = on_exit
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


def test_remove_recursive_deletes_tree_files_then_dirs():
    runtime, runner = _make_runtime()
    owner = ClientId("client:owner")
    summary = runtime.prepare_open_service(_open_request(), client_id=owner)
    runtime.start_service(summary.id)
    client = runner.handles[0].client
    client.files.update({"/tree/a.txt": b"a", "/tree/sub/b.txt": b"b", "/tree/root.txt": b"r"})
    client.directories.update({"/tree", "/tree/sub"})

    runtime.remove(
        SftpPathRequest(service_id=summary.id, path="/tree", recursive=True),
        client_id=owner,
    )

    assert set(client.remove_calls) == {"/tree/a.txt", "/tree/sub/b.txt", "/tree/root.txt"}
    assert "/tree" not in client.directories
    assert "/tree/sub" not in client.directories
    assert "/tree/a.txt" not in client.files
    assert "/tree/sub/b.txt" not in client.files
    assert "/tree/root.txt" not in client.files


def test_remove_recursive_never_follows_symlinks():
    runtime, runner = _make_runtime()
    owner = ClientId("client:owner")
    summary = runtime.prepare_open_service(_open_request(), client_id=owner)
    runtime.start_service(summary.id)
    client = runner.handles[0].client
    client.files["/tree/plain.txt"] = b"p"
    client.symlinks["/tree/escaped-link"] = None
    client.directories.update({"/tree", "/elsewhere"})
    client.files["/elsewhere/secret.txt"] = b"s"

    runtime.remove(
        SftpPathRequest(service_id=summary.id, path="/tree", recursive=True),
        client_id=owner,
    )

    assert "/tree/escaped-link" in client.remove_calls
    assert "/elsewhere/secret.txt" not in client.remove_calls
    assert "/elsewhere" in client.directories


def test_remove_recursive_missing_path_is_idempotent():
    runtime, runner = _make_runtime()
    owner = ClientId("client:owner")
    summary = runtime.prepare_open_service(_open_request(), client_id=owner)
    runtime.start_service(summary.id)
    client = runner.handles[0].client

    runtime.remove(
        SftpPathRequest(service_id=summary.id, path="/absent", recursive=True),
        client_id=owner,
    )

    assert client.remove_calls == []
    assert client.directories == {"/"}


def test_remove_recursive_single_file_is_removed():
    runtime, runner = _make_runtime()
    owner = ClientId("client:owner")
    summary = runtime.prepare_open_service(_open_request(), client_id=owner)
    runtime.start_service(summary.id)
    client = runner.handles[0].client

    runtime.remove(
        SftpPathRequest(service_id=summary.id, path="/source.txt", recursive=True),
        client_id=owner,
    )

    assert "/source.txt" in client.remove_calls
    assert "/source.txt" not in client.files


# ---------------------------------------------------------------------------
# list_directory: tilde (``~``) expansion
# ---------------------------------------------------------------------------
# OpenSSH's sftp-server performs no tilde expansion in OPENDIR, so a literal
# ``~`` sent to ``FXP_OPENDIR`` fails with FX_NO_SUCH_FILE. The runtime must
# resolve the home (REALPATH("."), the sftp-server cwd) and expand ``~``/
# ``~/`` before listing -- mirroring DaemonSftpManager._resolve_home().


def _ready_service(runtime, runner):
    owner = ClientId("client:owner")
    summary = runtime.prepare_open_service(_open_request(), client_id=owner)
    runtime.start_service(summary.id)
    return owner, summary, runner.handles[0].client


def test_list_directory_expands_tilde_to_resolved_home():
    runtime, runner = _make_runtime()
    owner, summary, client = _ready_service(runtime, runner)
    client.cwd = "/home/alice"
    client.directories.add("/home/alice")
    client.directories.add("/home/alice/docs")

    result = runtime.list_directory(
        ListDirectoryRequest(
            connection_id=ConnectionId("demo"),
            service_id=summary.id,
            path="~",
        ),
        client_id=owner,
    )

    # The home path, not the raw tilde, must reach the SFTP client.
    assert result.path == "/home/alice"
    assert [entry.name for entry in result.entries] == ["docs"]


def test_list_directory_expands_tilde_slash_subpath():
    runtime, runner = _make_runtime()
    owner, summary, client = _ready_service(runtime, runner)
    client.cwd = "/home/alice"
    client.directories.add("/home/alice")
    client.directories.add("/home/alice/docs")
    client.directories.add("/home/alice/docs/reports")

    result = runtime.list_directory(
        ListDirectoryRequest(
            connection_id=ConnectionId("demo"),
            service_id=summary.id,
            path="~/docs",
        ),
        client_id=owner,
    )

    assert result.path == "/home/alice/docs"
    assert [entry.name for entry in result.entries] == ["reports"]


def test_list_directory_passes_absolute_and_plain_paths_through():
    runtime, runner = _make_runtime()
    owner, summary, client = _ready_service(runtime, runner)
    client.directories.add("/var/tmp")

    absolute = runtime.list_directory(
        ListDirectoryRequest(
            connection_id=ConnectionId("demo"),
            service_id=summary.id,
            path="/var/tmp",
        ),
        client_id=owner,
    )
    assert absolute.path == "/var/tmp"

    plain = runtime.list_directory(
        ListDirectoryRequest(
            connection_id=ConnectionId("demo"),
            service_id=summary.id,
            path=".",
        ),
        client_id=owner,
    )
    assert plain.path == "."


def test_list_directory_home_resolution_failure_falls_back_to_raw_path():
    runtime, runner = _make_runtime()
    owner, summary, client = _ready_service(runtime, runner)

    def _boom(_path):
        raise sftp_proto.SFTPError(sftp_proto.FX_FAILURE, "realpath failed")

    client.realpath = _boom

    result = runtime.list_directory(
        ListDirectoryRequest(
            connection_id=ConnectionId("demo"),
            service_id=summary.id,
            path="~",
        ),
        client_id=owner,
    )

    # Graceful fallback: the raw path is passed through rather than crashing.
    assert result.path == "~"


def test_list_directory_resolves_home_once_per_service():
    runtime, runner = _make_runtime()
    owner, summary, client = _ready_service(runtime, runner)
    client.cwd = "/home/alice"
    client.directories.add("/home/alice")
    realpath_calls = []
    original = client.realpath

    def _counting(path):
        realpath_calls.append(path)
        return original(path)

    client.realpath = _counting
    request = ListDirectoryRequest(
        connection_id=ConnectionId("demo"),
        service_id=summary.id,
        path="~",
    )

    runtime.list_directory(request, client_id=owner)
    runtime.list_directory(request, client_id=owner)

    assert realpath_calls == ["."]


def test_unexpected_process_exit_fails_ready_service_without_an_operation():
    """A dead ssh child must fail the service immediately, like a terminal."""
    from sshpilot.api.events import EventType

    runtime, runner = _make_runtime()
    events = []
    runtime.subscribe_events(events.append)
    owner, summary, _client = _ready_service(runtime, runner)
    assert runner.on_exit is not None

    runner.on_exit(255)

    assert runtime.get_service(summary.id).state is SftpServiceState.FAILED
    failed = [event for event in events if event.type is EventType.SFTP_FAILED]
    assert len(failed) == 1
    assert failed[0].payload.failure is not None
    assert failed[0].payload.failure.code == ErrorCode.SFTP_PROTOCOL_LOST.value


def test_process_exit_during_close_does_not_fail_the_service():
    runtime, runner = _make_runtime()
    owner, summary, _client = _ready_service(runtime, runner)
    close_request = CloseSftpRequest(service_id=summary.id)
    assert runtime.prepare_close_service(close_request, client_id=owner)
    runner.on_exit(0)
    runtime.finish_close_service(summary.id)
    assert runtime.get_service(summary.id).state is SftpServiceState.CLOSED


def test_subprocess_handle_notifies_once_when_process_exits():
    import subprocess
    import sys
    from types import SimpleNamespace

    from sshpilot.daemon.sftp_runtime import _SubprocessSftpHandle

    process = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    seen = []
    handle = _SubprocessSftpHandle(
        process,
        SimpleNamespace(close=lambda: None),
        seen.append,
        lambda _handle: None,
    )
    assert process.wait(timeout=5) == 0
    assert handle.poll_and_notify() is True
    assert seen == [0]
    assert handle.poll_and_notify() is True
    assert seen == [0]


def _list_tmp(runtime, summary, owner):
    return runtime.list_directory(
        ListDirectoryRequest(
            connection_id=ConnectionId("demo"),
            service_id=summary.id,
            path="/tmp",
        ),
        client_id=owner,
    )


def test_connection_lost_status_fails_service_with_specific_message():
    runtime, _runner = _make_runtime()
    owner, summary, client = _ready_service(runtime, _runner)

    def _lost(_path):
        raise sftp_proto.SFTPError(sftp_proto.FX_CONNECTION_LOST, "Connection lost")

    client.listdir_attr = _lost
    with pytest.raises(SshPilotError) as raised:
        _list_tmp(runtime, summary, owner)

    assert raised.value.code is ErrorCode.SFTP_PROTOCOL_LOST
    assert raised.value.message == "The SFTP connection was lost"
    failed = runtime.get_service(summary.id)
    assert failed.state is SftpServiceState.FAILED
    assert failed.failure.message == "The SFTP connection was lost"


def test_permission_denied_status_keeps_service_ready():
    runtime, _runner = _make_runtime()
    owner, summary, client = _ready_service(runtime, _runner)

    def _denied(_path):
        raise sftp_proto.SFTPError(sftp_proto.FX_PERMISSION_DENIED, "Permission denied")

    client.listdir_attr = _denied
    with pytest.raises(SshPilotError) as raised:
        _list_tmp(runtime, summary, owner)

    assert raised.value.code is ErrorCode.REMOTE_PERMISSION_DENIED
    assert raised.value.message == "Permission denied"
    assert runtime.get_service(summary.id).state is SftpServiceState.READY


def test_fx_failure_keeps_generic_command_message_and_ready_service():
    runtime, _runner = _make_runtime()
    owner, summary, client = _ready_service(runtime, _runner)

    def _fail(_path):
        raise sftp_proto.SFTPError(sftp_proto.FX_FAILURE, "Failure")

    client.listdir_attr = _fail
    with pytest.raises(SshPilotError) as raised:
        _list_tmp(runtime, summary, owner)

    assert raised.value.code is ErrorCode.SFTP_COMMAND_FAILED
    assert raised.value.message == "The SFTP command failed"
    assert runtime.get_service(summary.id).state is SftpServiceState.READY


def test_fx_failure_with_server_text_is_surfaced():
    runtime, _runner = _make_runtime()
    owner, summary, client = _ready_service(runtime, _runner)

    def _fail(_path):
        raise sftp_proto.SFTPError(sftp_proto.FX_FAILURE, "Directory not empty")

    client.listdir_attr = _fail
    with pytest.raises(SshPilotError) as raised:
        _list_tmp(runtime, summary, owner)

    assert raised.value.code is ErrorCode.SFTP_COMMAND_FAILED
    assert raised.value.message == "Directory not empty"
    assert runtime.get_service(summary.id).state is SftpServiceState.READY

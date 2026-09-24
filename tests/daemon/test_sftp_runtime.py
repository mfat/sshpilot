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
    SftpFailureCode,
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

    @property
    def handle(self):
        return self.path

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

    def remove_many(self, paths, *, continue_on_error=False):
        failures = []
        for path in paths:
            try:
                self.remove(path)
            except Exception as exc:
                failures.append((path, exc))
                if not continue_on_error:
                    raise
        return failures

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


class _CopyDataSftpClient(_FakeSftpClient):
    """Offers the ``copy-data`` extension; ``copy_error`` makes it fail."""

    def __init__(self, copy_error=None):
        super().__init__()
        self.copy_error = copy_error
        self.copy_data_calls = []

    def supports_copy_data(self):
        return True

    def copy_data(self, source_handle, destination_handle):
        self.copy_data_calls.append((source_handle, destination_handle))
        if self.copy_error is not None:
            raise self.copy_error
        self.files[destination_handle] = self.files[source_handle]


@pytest.mark.parametrize("copy_error", [None, sftp_proto.SFTPError(sftp_proto.FX_OP_UNSUPPORTED)])
def test_remote_copy_uses_server_side_copy_and_falls_back(copy_error):
    runtime, runner = _make_runtime()
    owner = ClientId("client:owner")
    summary = runtime.prepare_open_service(_open_request(), client_id=owner)
    runtime.start_service(summary.id)
    client = _CopyDataSftpClient(copy_error)
    runner.handles[0].client = client

    runtime.copy(
        SftpCopyRequest(
            service_id=summary.id,
            source_path="/source.txt",
            destination_path="/copy.txt",
        ),
        client_id=owner,
    )

    assert client.copy_data_calls == [("/source.txt", "/copy.txt")]
    assert client.files["/copy.txt"] == b"payload"


def test_remote_copy_does_not_stream_after_losing_the_connection():
    runtime, runner = _make_runtime()
    owner = ClientId("client:owner")
    summary = runtime.prepare_open_service(_open_request(), client_id=owner)
    runtime.start_service(summary.id)
    client = _CopyDataSftpClient(sftp_proto.SFTPError(sftp_proto.FX_CONNECTION_LOST))
    runner.handles[0].client = client

    with pytest.raises(SshPilotError):
        runtime.copy(
            SftpCopyRequest(
                service_id=summary.id,
                source_path="/source.txt",
                destination_path="/copy.txt",
            ),
            client_id=owner,
        )
    assert "/copy.txt" not in client.files


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


def test_remove_multi_path_pipelines_files_and_reports_failures():
    from sshpilot.api.models.operations import SftpRemoveResult

    runtime, runner = _make_runtime()
    owner = ClientId("client:owner")
    summary = runtime.prepare_open_service(_open_request(), client_id=owner)
    runtime.start_service(summary.id)
    client = runner.handles[0].client
    client.files.update({"/a.txt": b"a", "/b.txt": b"b"})

    result = runtime.remove(
        SftpPathRequest(
            service_id=summary.id,
            path="/a.txt",
            paths=("/missing.txt", "/b.txt"),
        ),
        client_id=owner,
    )

    assert isinstance(result, SftpRemoveResult)
    assert result.failures == ()
    assert set(client.remove_calls) == {"/a.txt", "/missing.txt", "/b.txt"}
    assert "/a.txt" not in client.files
    assert "/b.txt" not in client.files


def test_remove_recursive_batches_sibling_files_via_remove_many():
    runtime, runner = _make_runtime()
    owner = ClientId("client:owner")
    summary = runtime.prepare_open_service(_open_request(), client_id=owner)
    runtime.start_service(summary.id)
    client = runner.handles[0].client
    client.files.update({"/tree/a.txt": b"a", "/tree/b.txt": b"b"})
    client.directories.add("/tree")
    batches = []
    original = client.remove_many

    def _track(paths, *, continue_on_error=False):
        batches.append(list(paths))
        return original(paths, continue_on_error=continue_on_error)

    client.remove_many = _track

    runtime.remove(
        SftpPathRequest(service_id=summary.id, path="/tree", recursive=True),
        client_id=owner,
    )

    assert batches == [["/tree/a.txt", "/tree/b.txt"]]
    assert "/tree" not in client.directories


@pytest.mark.parametrize(
    "method",
    ["stat_path", "realpath", "readlink", "filesystem_usage", "mkdir", "rmdir"],
)
def test_path_methods_reject_extra_paths(method):
    runtime, runner = _make_runtime()
    owner = ClientId("client:owner")
    summary = runtime.prepare_open_service(_open_request(), client_id=owner)
    runtime.start_service(summary.id)
    req = SftpPathRequest(service_id=summary.id, path="/a", paths=("/b",))
    with pytest.raises(SshPilotError) as exc_info:
        getattr(runtime, method)(req, client_id=owner)
    assert exc_info.value.code == ErrorCode.INVALID_REQUEST
    assert "Extra SFTP paths are only valid for sftp.remove" in str(exc_info.value)


def test_remove_recursive_chunks_large_file_lists():
    runtime, runner = _make_runtime()
    owner = ClientId("client:owner")
    summary = runtime.prepare_open_service(_open_request(), client_id=owner)
    runtime.start_service(summary.id)
    client = runner.handles[0].client
    for i in range(300):
        client.files[f"/tree/file_{i}.txt"] = b"x"
    client.directories.add("/tree")
    batches = []
    original = client.remove_many

    def _track(paths, *, continue_on_error=False):
        batches.append(list(paths))
        return original(paths, continue_on_error=continue_on_error)

    client.remove_many = _track

    runtime.remove(
        SftpPathRequest(service_id=summary.id, path="/tree", recursive=True),
        client_id=owner,
    )

    assert len(batches) == 2
    assert len(batches[0]) == 256
    assert len(batches[1]) == 44
    assert "/tree" not in client.directories


def test_remove_multi_path_cancel_stops_after_first_chunk():
    runtime, runner = _make_runtime()
    owner = ClientId("client:owner")
    summary = runtime.prepare_open_service(_open_request(), client_id=owner)
    runtime.start_service(summary.id)
    client = runner.handles[0].client
    paths = [f"/file_{i}.txt" for i in range(300)]
    for path in paths:
        client.files[path] = b"x"
    batches = []
    original = client.remove_many

    def _track(chunk, *, continue_on_error=False):
        batches.append(list(chunk))
        return original(chunk, continue_on_error=continue_on_error)

    client.remove_many = _track
    calls = {"cancel": 0}

    def _cancel():
        calls["cancel"] += 1
        return calls["cancel"] > 1

    result = runtime.remove(
        SftpPathRequest(
            service_id=summary.id,
            path=paths[0],
            paths=tuple(paths[1:]),
        ),
        client_id=owner,
        cancel=_cancel,
    )

    assert result is not None
    assert result.failures == ()
    assert len(batches) == 1
    assert len(batches[0]) == 256
    assert calls["cancel"] == 2
    # First chunk is gone; remaining paths were not attempted.
    assert all(path not in client.files for path in paths[:256])
    assert all(path in client.files for path in paths[256:])


def test_remove_multi_path_reports_progress_per_chunk():
    runtime, runner = _make_runtime()
    owner = ClientId("client:owner")
    summary = runtime.prepare_open_service(_open_request(), client_id=owner)
    runtime.start_service(summary.id)
    client = runner.handles[0].client
    paths = [f"/file_{i}.txt" for i in range(300)]
    for path in paths:
        client.files[path] = b"x"
    seen = []

    result = runtime.remove(
        SftpPathRequest(
            service_id=summary.id,
            path=paths[0],
            paths=tuple(paths[1:]),
        ),
        client_id=owner,
        progress=seen.append,
    )

    assert result.failures == ()
    assert len(seen) == 2
    # Same coarse convention as the recursive walk: strictly increasing,
    # bounded below 1.0 mid-walk (terminal 1.0 comes from the caller).
    assert 0.0 < seen[0] < seen[1] < 1.0
    assert all(path not in client.files for path in paths)


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
    assert failed[0].payload.failure.code is SftpFailureCode.CONNECTION_LOST
    assert failed[0].payload.failure.error_code is ErrorCode.SFTP_PROTOCOL_LOST


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
    assert raised.value.message == ErrorCode.SFTP_PROTOCOL_LOST.value
    assert raised.value.details == {
        "service_id": summary.id,
        "sftp_status": sftp_proto.FX_CONNECTION_LOST,
        "server_message": "Connection lost",
    }
    failed = runtime.get_service(summary.id)
    assert failed.state is SftpServiceState.FAILED
    assert failed.failure.code is SftpFailureCode.CONNECTION_LOST
    assert failed.failure.error_code is ErrorCode.SFTP_PROTOCOL_LOST
    # This stock protocol phrase is not a server-specific diagnostic.
    assert failed.failure.diagnostic == ""


def test_permission_denied_status_keeps_service_ready():
    runtime, _runner = _make_runtime()
    owner, summary, client = _ready_service(runtime, _runner)

    def _denied(_path):
        raise sftp_proto.SFTPError(sftp_proto.FX_PERMISSION_DENIED, "Permission denied")

    client.listdir_attr = _denied
    with pytest.raises(SshPilotError) as raised:
        _list_tmp(runtime, summary, owner)

    assert raised.value.code is ErrorCode.REMOTE_PERMISSION_DENIED
    assert raised.value.message == ErrorCode.REMOTE_PERMISSION_DENIED.value
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
    assert raised.value.message == ErrorCode.SFTP_COMMAND_FAILED.value
    assert "server_message_is_specific" not in raised.value.details
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
    assert raised.value.message == ErrorCode.SFTP_COMMAND_FAILED.value
    assert raised.value.details["server_message"] == "Directory not empty"
    assert raised.value.details["server_message_is_specific"] is True
    assert runtime.get_service(summary.id).state is SftpServiceState.READY


@pytest.mark.parametrize(
    ("status", "server_message", "expected_code"),
    (
        (
            sftp_proto.FX_NO_SUCH_FILE,
            "No such file",
            ErrorCode.REMOTE_PATH_NOT_FOUND,
        ),
        (
            sftp_proto.FX_OP_UNSUPPORTED,
            "Operation unsupported",
            ErrorCode.REMOTE_UNSUPPORTED_OPERATION,
        ),
    ),
)
def test_direct_status_errors_use_stable_codes_not_rendered_messages(
    status, server_message, expected_code
):
    runtime, _runner = _make_runtime()
    owner, summary, client = _ready_service(runtime, _runner)

    def _fail(_path):
        raise sftp_proto.SFTPError(status, server_message)

    client.listdir_attr = _fail
    with pytest.raises(SshPilotError) as raised:
        _list_tmp(runtime, summary, owner)

    assert raised.value.code is expected_code
    assert raised.value.message == expected_code.value
    assert raised.value.details["server_message"] == server_message
    assert "server_message_is_specific" not in raised.value.details


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    (
        (OSError("system connection detail"), ErrorCode.SFTP_PROTOCOL_LOST),
        (RuntimeError("library protocol detail"), ErrorCode.SFTP_PROTOCOL_ERROR),
    ),
)
def test_external_exception_text_is_not_transported_as_direct_message(
    failure, expected_code
):
    runtime, _runner = _make_runtime()
    owner, summary, client = _ready_service(runtime, _runner)

    def _fail(_path):
        raise failure

    client.listdir_attr = _fail
    with pytest.raises(SshPilotError) as raised:
        _list_tmp(runtime, summary, owner)

    assert raised.value.code is expected_code
    assert raised.value.message == expected_code.value
    assert str(failure) not in str(raised.value.to_dict())


def test_reattaching_an_orphaned_service_reclaims_ownership():
    """After the app's daemon transport is replaced, its new client re-attaches
    the surviving service and must be able to save through it again."""
    runtime, _runner = _make_runtime()
    old_client = ClientId("client:old")
    new_client = ClientId("client:new")
    summary = runtime.prepare_open_service(_open_request(), client_id=old_client)
    runtime.start_service(summary.id)

    runtime.detach_client(old_client)
    runtime.attach_service(AttachSftpRequest(service_id=summary.id), client_id=new_client)

    runtime.mkdir(
        SftpPathRequest(service_id=summary.id, path="/tmp/demo"),
        client_id=new_client,
    )

"""Copying between two SFTP services (#1356: a server in each file-manager pane).

``SftpCopyRequest.destination_service_id`` names the service that receives
the copy. Another server receives the bytes through its own session; two
sessions on one server copy through the source session alone, so the server
can still copy for us. A failure on the receiving side is that service's
failure, never the source's.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ConnectionId
from sshpilot.api.models.operations import (
    OpenSftpRequest,
    OperationState,
    SftpCopyRequest,
    SftpFailureCode,
    SftpServiceState,
)
from sshpilot.api.transport.codec import (
    sftp_copy_request_from_wire,
    sftp_copy_request_to_wire,
)
from sshpilot.daemon.operation_runtime import OperationRuntime
from sshpilot.daemon.sftp_runtime import SftpServiceRuntime
from sshpilot.sftp import protocol as sftp_proto

from tests.daemon.test_sftp_operations import (
    OWNER,
    _FsClient,
    _await_terminal,
    _tree_client,
)


class _Connections:
    def get_connection(self, connection_id):
        return SimpleNamespace(
            id=str(connection_id), protocol="ssh", hostname=str(connection_id),
            username="alice", port=22,
        )


class _QueueRunner:
    """Hands each started service the next filesystem double."""

    def __init__(self, clients):
        self._clients = list(clients)

    def start(self, _spec, _on_exit=None):
        return SimpleNamespace(client=self._clients.pop(0))

    def close(self):
        pass


def _two_services(source_fs, destination_fs, *, same_connection=False):
    ops = OperationRuntime()
    runtime = SftpServiceRuntime(
        _Connections(),
        runner=_QueueRunner([source_fs, destination_fs]),
        operation_lifecycle=ops,
    )
    services = []
    for name in ("conn-a", "conn-a" if same_connection else "conn-b"):
        summary = runtime.prepare_open_service(
            OpenSftpRequest(ConnectionId(name)), client_id=OWNER
        )
        runtime.start_service(summary.id)
        services.append(summary.id)
    return runtime, ops, services[0], services[1]


def _run(runtime, ops, request, timeout=5.0):
    started = runtime.start_copy(request, client_id=OWNER)
    return _await_terminal(ops, started.operation_id, timeout=timeout)


class _Untouchable(_FsClient):
    """A destination session that must never be used."""

    def open(self, path, mode):  # pragma: no cover - asserted not called
        raise AssertionError(f"destination session opened {path}")

    def mkdir(self, path):  # pragma: no cover - asserted not called
        raise AssertionError(f"destination session made {path}")

    def stat(self, path):  # pragma: no cover - asserted not called
        raise AssertionError(f"destination session stat {path}")

    def lstat(self, path):  # pragma: no cover - asserted not called
        raise AssertionError(f"destination session lstat {path}")


def test_tree_copies_to_another_server():
    source, destination = _tree_client(), _FsClient()
    destination.add_dir("/srv", set())
    runtime, ops, a, b = _two_services(source, destination)

    done = _run(runtime, ops, SftpCopyRequest(
        a, "/tree", "/srv/tree", recursive=True, destination_service_id=b,
    ))

    assert done.state is OperationState.SUCCEEDED, done.failure
    assert destination.files["/srv/tree/a.txt"] == b"hello"
    assert destination.files["/srv/tree/sub/b.txt"] == b"world!"
    assert "/srv/tree" not in source.dirs
    assert source.files["/tree/a.txt"] == b"hello"


def test_move_to_another_server_removes_the_source():
    source, destination = _tree_client(), _FsClient()
    destination.add_dir("/srv", set())
    runtime, ops, a, b = _two_services(source, destination)

    done = _run(runtime, ops, SftpCopyRequest(
        a, "/tree", "/srv/tree", recursive=True, move=True, destination_service_id=b,
    ))

    assert done.state is OperationState.SUCCEEDED, done.failure
    assert destination.files["/srv/tree/a.txt"] == b"hello"
    assert "/tree/a.txt" not in source.files
    assert "/tree" not in source.dirs


def test_single_file_between_servers_always_runs_as_an_operation():
    request = SftpCopyRequest("svc-a", "/f", "/f", destination_service_id="svc-b")
    assert request.runs_as_operation
    source, destination = _FsClient(), _FsClient()
    source.add_dir("/", {"f"})
    source.add_file("/f", b"payload")
    destination.add_dir("/", set())
    runtime, ops, a, b = _two_services(source, destination)

    done = _run(runtime, ops, SftpCopyRequest(a, "/f", "/f", destination_service_id=b))

    assert done.state is OperationState.SUCCEEDED, done.failure
    assert destination.files["/f"] == b"payload"


def test_existing_destination_on_the_other_server_is_refused():
    source, destination = _tree_client(), _FsClient()
    destination.add_dir("/srv", {"tree"})
    destination.add_dir("/srv/tree", set())
    runtime, ops, a, b = _two_services(source, destination)

    done = _run(runtime, ops, SftpCopyRequest(
        a, "/tree", "/srv/tree", recursive=True, destination_service_id=b,
    ))

    assert done.state is OperationState.FAILED
    assert done.failure.code is SftpFailureCode.DESTINATION_ALREADY_EXISTS
    assert destination.dirs["/srv/tree"] == set()


def test_a_folder_may_land_under_its_own_path_on_another_server():
    source, destination = _tree_client(), _FsClient()
    destination.add_dir("/tree", set())
    destination.add_dir("/tree/sub", set())
    runtime, ops, a, b = _two_services(source, destination)

    done = _run(runtime, ops, SftpCopyRequest(
        a, "/tree", "/tree/sub/copy", recursive=True, destination_service_id=b,
    ))

    assert done.state is OperationState.SUCCEEDED, done.failure
    assert destination.files["/tree/sub/copy/a.txt"] == b"hello"


def test_lost_destination_fails_that_service_and_leaves_no_partial_file():
    source, destination = _FsClient(), _FsClient()
    source.add_dir("/", {"f"})
    source.add_file("/f", b"x" * (3 * 1024 * 1024))
    destination.add_dir("/", set())
    original_open = destination.open

    def _open(path, mode):
        handle = original_open(path, mode)
        if mode != "rb":
            def _write(_data):
                raise EOFError("connection closed")
            handle.write = _write
        return handle

    destination.open = _open
    runtime, ops, a, b = _two_services(source, destination)

    done = _run(runtime, ops, SftpCopyRequest(a, "/f", "/f", destination_service_id=b))

    assert done.state is OperationState.FAILED
    assert done.failure.error_code is ErrorCode.SFTP_PROTOCOL_LOST
    assert runtime.get_service(b).state is SftpServiceState.FAILED
    assert runtime.get_service(a).state is SftpServiceState.READY
    assert "/f" not in destination.files
    assert destination.staging_files() == []


def test_two_sessions_on_one_server_copy_through_the_source_session():
    fs = _tree_client()
    runtime, ops, a, b = _two_services(fs, _Untouchable(), same_connection=True)

    done = _run(runtime, ops, SftpCopyRequest(
        a, "/tree", "/copy", recursive=True, destination_service_id=b,
    ))

    assert done.state is OperationState.SUCCEEDED, done.failure
    assert fs.files["/copy/sub/b.txt"] == b"world!"


def test_one_server_still_refuses_a_folder_into_itself():
    runtime, _ops, a, b = _two_services(
        _tree_client(), _Untouchable(), same_connection=True
    )
    with pytest.raises(SshPilotError) as raised:
        runtime.start_copy(
            SftpCopyRequest(a, "/tree", "/tree/sub/copy", recursive=True,
                            destination_service_id=b),
            client_id=OWNER,
        )
    assert raised.value.details["sftp_failure_code"] == (
        "directory_cannot_be_copied_into_itself"
    )


def test_copies_in_both_directions_at_once_both_finish():
    left, right = _tree_client(), _tree_client()
    left.add_dir("/in", set())
    right.add_dir("/in", set())
    runtime, ops, a, b = _two_services(left, right)
    results = {}

    def _copy(name, request):
        results[name] = _run(runtime, ops, request)

    threads = [
        threading.Thread(target=_copy, args=("ab", SftpCopyRequest(
            a, "/tree", "/in/tree", recursive=True, destination_service_id=b))),
        threading.Thread(target=_copy, args=("ba", SftpCopyRequest(
            b, "/tree", "/in/tree", recursive=True, destination_service_id=a))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert results["ab"].state is OperationState.SUCCEEDED
    assert results["ba"].state is OperationState.SUCCEEDED
    assert right.files["/in/tree/a.txt"] == b"hello"
    assert left.files["/in/tree/a.txt"] == b"hello"


def test_unknown_destination_service_is_refused_before_starting():
    runtime, _ops, a, _b = _two_services(_tree_client(), _FsClient())
    with pytest.raises(SshPilotError) as raised:
        runtime.start_copy(
            SftpCopyRequest(a, "/tree", "/x", recursive=True,
                            destination_service_id="sftp-missing"),
            client_id=OWNER,
        )
    assert raised.value.code is ErrorCode.SFTP_SERVICE_NOT_FOUND


def test_destination_service_round_trips_and_is_omitted_when_unset():
    plain = SftpCopyRequest("svc-a", "/a", "/b")
    assert "destination_service_id" not in sftp_copy_request_to_wire(plain)
    cross = SftpCopyRequest("svc-a", "/a", "/a", destination_service_id="svc-b")
    wire = sftp_copy_request_to_wire(cross)
    assert wire["destination_service_id"] == "svc-b"
    assert sftp_copy_request_from_wire(wire) == cross


def test_same_path_on_the_same_service_is_still_rejected():
    with pytest.raises(ValueError):
        SftpCopyRequest("svc-a", "/a", "/a")
    with pytest.raises(ValueError):
        SftpCopyRequest("svc-a", "/a", "/a", destination_service_id="svc-a")


def test_two_connections_to_one_machine_refuse_a_folder_into_itself():
    """Two saved connections can reach one machine. Their ids differ, so
    only the files can tell: the copy must stop instead of copying the new
    folder into itself without end, and leave nothing behind."""
    fs = _tree_client()
    runtime, ops, a, b = _two_services(fs, fs)
    before_dirs, before_files = dict(fs.dirs), dict(fs.files)

    done = _run(runtime, ops, SftpCopyRequest(
        a, "/tree", "/tree/sub/copy", recursive=True, destination_service_id=b,
    ))

    assert done.state is OperationState.FAILED
    assert done.failure.code is SftpFailureCode.DIRECTORY_CANNOT_BE_COPIED_INTO_ITSELF
    assert "/tree/sub/copy" not in fs.dirs
    assert set(fs.dirs) == set(before_dirs)
    assert fs.files == before_files


def _aliased(fs):
    """``/alias`` is a link to ``/tree``: the same folder under another name."""
    fs.add_dir("/alias", {"sub"})
    fs.add_dir("/alias/sub", set())
    fs.realpath = lambda path: (
        "/tree" + path[len("/alias"):] if path.startswith("/alias") else path
    )
    return fs


def test_one_server_refuses_a_folder_into_itself_through_a_link():
    """As text ``/alias/sub/copy`` is not inside ``/tree``; resolved, it is."""
    fs = _aliased(_tree_client())
    runtime, ops, a, b = _two_services(fs, _Untouchable(), same_connection=True)

    done = _run(runtime, ops, SftpCopyRequest(
        a, "/tree", "/alias/sub/copy", recursive=True, destination_service_id=b,
    ))

    assert done.state is OperationState.FAILED
    assert done.failure.code is SftpFailureCode.DIRECTORY_CANNOT_BE_COPIED_INTO_ITSELF
    assert "/alias/sub/copy" not in fs.dirs


def test_two_connections_refuse_a_folder_into_itself_through_a_link():
    fs = _aliased(_tree_client())
    runtime, ops, a, b = _two_services(fs, fs)

    done = _run(runtime, ops, SftpCopyRequest(
        a, "/tree", "/alias/sub/copy", recursive=True, destination_service_id=b,
    ))

    assert done.state is OperationState.FAILED
    assert done.failure.code is SftpFailureCode.DIRECTORY_CANNOT_BE_COPIED_INTO_ITSELF
    assert "/alias/sub/copy" not in fs.dirs


def test_two_connections_to_one_machine_still_copy_elsewhere():
    """The same machine through two connections is fine as long as the copy
    lands outside the folder being copied."""
    fs = _tree_client()
    fs.add_dir("/srv", set())
    runtime, ops, a, b = _two_services(fs, fs)

    done = _run(runtime, ops, SftpCopyRequest(
        a, "/tree", "/srv/tree", recursive=True, destination_service_id=b,
    ))

    assert done.state is OperationState.SUCCEEDED, done.failure
    assert fs.files["/srv/tree/sub/b.txt"] == b"world!"


def _unresolvable(fs):
    """A server that will not say where a path really is."""
    def _refuse(_path):
        raise OSError("Permission denied")

    fs.realpath = _refuse
    return fs


def test_two_connections_refuse_a_folder_copy_they_cannot_place():
    """The new folder shows up through the source, so both connections reach
    one filesystem; where it really lies cannot be resolved. As text it is
    outside the source, which a link would hide: refuse rather than guess."""
    fs = _unresolvable(_aliased(_tree_client()))
    runtime, ops, a, b = _two_services(fs, fs)
    before_dirs, before_files = dict(fs.dirs), dict(fs.files)

    done = _run(runtime, ops, SftpCopyRequest(
        a, "/tree", "/alias/sub/copy", recursive=True, destination_service_id=b,
    ))

    assert done.state is OperationState.FAILED
    assert done.failure.code is SftpFailureCode.DIRECTORY_CANNOT_BE_COPIED_INTO_ITSELF
    assert set(fs.dirs) == set(before_dirs)
    assert fs.files == before_files


def test_one_server_refuses_a_folder_copy_it_cannot_place():
    fs = _unresolvable(_aliased(_tree_client()))
    runtime, ops, a, b = _two_services(fs, _Untouchable(), same_connection=True)
    before_dirs = dict(fs.dirs)

    done = _run(runtime, ops, SftpCopyRequest(
        a, "/tree", "/alias/sub/copy", recursive=True, destination_service_id=b,
    ))

    assert done.state is OperationState.FAILED
    assert done.failure.code is SftpFailureCode.DIRECTORY_CANNOT_BE_COPIED_INTO_ITSELF
    assert set(fs.dirs) == set(before_dirs)


def test_one_server_refuses_unresolvable_destination_parent():
    """Missing canonical paths cannot establish safe containment."""
    fs = _tree_client()
    original_realpath = fs.realpath

    def _realpath(path):
        if path.startswith("/nowhere"):
            raise sftp_proto.SFTPError(sftp_proto.FX_NO_SUCH_FILE)
        return original_realpath(path)

    fs.realpath = _realpath
    runtime, ops, a, b = _two_services(fs, _Untouchable(), same_connection=True)

    done = _run(runtime, ops, SftpCopyRequest(
        a, "/tree", "/nowhere/copy", recursive=True, destination_service_id=b,
    ))

    assert done.state is OperationState.FAILED
    assert done.failure.code is SftpFailureCode.DIRECTORY_CANNOT_BE_COPIED_INTO_ITSELF


def test_move_to_another_server_never_renames():
    """A rename only works inside one filesystem: a move between servers
    copies through both sessions and removes the source."""
    source, destination = _tree_client(), _FsClient()
    destination.add_dir("/srv", set())
    runtime, ops, a, b = _two_services(source, destination)

    done = _run(runtime, ops, SftpCopyRequest(
        a, "/tree", "/srv/tree", recursive=True, move=True, destination_service_id=b,
    ))

    assert done.state is OperationState.SUCCEEDED, done.failure
    assert source.move_renames == [] and destination.move_renames == []
    assert destination.files["/srv/tree/a.txt"] == b"hello"


def test_move_between_two_panes_on_one_server_is_a_rename():
    """#1356: both panes on one server, moving 1 GB files took minutes
    because the bytes were copied and the originals removed."""
    source = _tree_client()
    source.add_dir("/srv", set())
    runtime, ops, a, b = _two_services(source, _Untouchable(), same_connection=True)

    done = _run(runtime, ops, SftpCopyRequest(
        a, "/tree", "/srv/tree", recursive=True, move=True, destination_service_id=b,
    ))

    assert done.state is OperationState.SUCCEEDED, done.failure
    assert source.move_renames == [("/tree", "/srv/tree")]
    assert source.files["/srv/tree/sub/b.txt"] == b"world!"
    assert "/tree" not in source.dirs



# --- Speed over a slow link, against the real sftp-server (#1431) ----------

from tests.test_sftp_client_large_files import (  # noqa: E402
    _SFTP_SERVER,
    _remote,
    _start_client,
    _stop_client,
)

_needs_sftp_server = pytest.mark.skipif(
    _SFTP_SERVER is None, reason="OpenSSH sftp-server not installed"
)


def _timed_real_copy(tmp_path, monkeypatch, *, same_connection):
    """Copy 16 MiB through the runtime between two real sftp-server sessions
    whose replies arrive one round trip late; returns the round trips taken."""
    import os
    import time

    # Windows' local work is worth a few 50 ms round trips on its own.
    delay = 0.25 if os.name == "nt" else 0.05
    if same_connection:
        # Two sessions on one server stream only when it cannot copy for us.
        from sshpilot.sftp.client import OpenSSHSFTPClient
        monkeypatch.setattr(OpenSSHSFTPClient, "supports_copy_data", lambda self: False)
    content = os.urandom(16 * 1024 * 1024)
    source = tmp_path / "source.bin"
    source.write_bytes(content)
    destination = tmp_path / "copy.bin"
    sessions = [_start_client(delay), _start_client(delay)]
    try:
        runtime, ops, a, b = _two_services(
            sessions[0][0], sessions[1][0], same_connection=same_connection
        )
        started = time.monotonic()
        # Wait well past the slowest passing copy (24 round trips): a
        # timeout here would hide the round-trip count the test is about.
        done = _run(runtime, ops, SftpCopyRequest(
            a, _remote(source), _remote(destination), as_operation=True,
            destination_service_id=b,
        ), timeout=60 * delay)
        round_trips = (time.monotonic() - started) / delay
    finally:
        for session in sessions:
            _stop_client(*session)
    assert done.state is OperationState.SUCCEEDED, done.failure
    assert destination.read_bytes() == content
    return round_trips


@_needs_sftp_server
def test_copy_to_another_server_streams_over_a_slow_link(tmp_path, monkeypatch):
    """#1431: reading and writing 1 MiB at a time waited out both request
    windows per block: ~47 round trips here, against ~13 streamed."""
    round_trips = _timed_real_copy(tmp_path, monkeypatch, same_connection=False)
    assert round_trips < 24, f"copy took {round_trips:.1f} round trips"


@_needs_sftp_server
def test_copy_on_one_server_without_copy_data_streams_too(tmp_path, monkeypatch):
    round_trips = _timed_real_copy(tmp_path, monkeypatch, same_connection=True)
    assert round_trips < 24, f"copy took {round_trips:.1f} round trips"


def test_a_failed_streamed_write_is_the_destinations_failure():
    """The stream writer writes to the destination: its failures must carry
    the destination's record, as the files it opens do."""
    from sshpilot.daemon.sftp_runtime import _DestinationSide, _DestinationSideError

    class _Writer:
        def write(self, _data):
            raise sftp_proto.SFTPError(sftp_proto.FX_FAILURE, "disk full")

    class _File:
        def stream_writer(self):
            return _Writer()

    record = object()
    writer = _DestinationSide(_File(), record).stream_writer()
    with pytest.raises(_DestinationSideError) as raised:
        writer.write(b"x")
    assert raised.value.record is record

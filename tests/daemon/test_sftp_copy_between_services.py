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


def _run(runtime, ops, request):
    started = runtime.start_copy(request, client_id=OWNER)
    return _await_terminal(ops, started.operation_id)


class _Untouchable(_FsClient):
    """A destination session that must never be used."""

    def open(self, path, mode):  # pragma: no cover - asserted not called
        raise AssertionError(f"destination session opened {path}")

    def mkdir(self, path):  # pragma: no cover - asserted not called
        raise AssertionError(f"destination session made {path}")

    def stat(self, path):  # pragma: no cover - asserted not called
        raise AssertionError(f"destination session stat {path}")


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
        if mode == "wb":
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

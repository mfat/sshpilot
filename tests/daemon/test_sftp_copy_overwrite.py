"""Copy to Other Pane over names that already exist (#1431).

``SftpCopyRequest.overwrite`` replaces a file whole, by one rename once its
copy is complete, and merges a folder. It never replaces a file with
itself (in a move that would lose it) or a folder with a file or back.

Runs against the real sftp-server. Two of its sessions on this machine are
also two saved connections reaching one machine: only the files can tell
they are the same, which is where replacing gets dangerous.
"""

from __future__ import annotations

import os

import pytest

from sshpilot.api.errors import SshPilotError
from sshpilot.api.models.operations import OperationState, SftpCopyRequest, SftpFailureCode

from tests.daemon.test_sftp_copy_between_services import _run, _two_services
from tests.test_sftp_client_large_files import _SFTP_SERVER, _start_client, _stop_client

pytestmark = pytest.mark.skipif(_SFTP_SERVER is None, reason="OpenSSH sftp-server not installed")


@pytest.fixture
def services():
    """Start two runtime services over real sftp-server sessions."""
    sessions = []

    def start(*, same_connection):
        sessions.extend([_start_client(), _start_client()])
        return _two_services(
            sessions[-2][0], sessions[-1][0], same_connection=same_connection
        )

    yield start
    for session in sessions:
        _stop_client(*session)


def _copy(runtime, ops, a, b, source, destination, **flags):
    return _run(runtime, ops, SftpCopyRequest(
        a, str(source), str(destination), as_operation=True,
        destination_service_id=b, **flags,
    ))


def _failure(done):
    assert done.state is OperationState.FAILED, done
    return done.failure.code


@pytest.mark.parametrize("same_connection", [False, True])
def test_replace_swaps_the_file_in_and_keeps_its_mode(tmp_path, services, same_connection):
    source = tmp_path / "new.sh"
    source.write_bytes(b"new contents\n")
    source.chmod(0o644)
    destination = tmp_path / "dest" / "new.sh"
    destination.parent.mkdir()
    destination.write_bytes(b"old\n")
    destination.chmod(0o755)
    runtime, ops, a, b = services(same_connection=same_connection)

    done = _copy(runtime, ops, a, b, source, destination, overwrite=True)

    assert done.state is OperationState.SUCCEEDED, done.failure
    assert destination.read_bytes() == b"new contents\n"
    # Replacing an executable script keeps it executable, as an upload does.
    assert destination.stat().st_mode & 0o777 == 0o755
    assert source.read_bytes() == b"new contents\n"
    assert sorted(p.name for p in destination.parent.iterdir()) == ["new.sh"]


def test_without_overwrite_an_existing_file_is_still_refused(tmp_path, services):
    source = tmp_path / "a.txt"
    source.write_bytes(b"new")
    destination = tmp_path / "dest" / "a.txt"
    destination.parent.mkdir()
    destination.write_bytes(b"old")
    runtime, ops, a, b = services(same_connection=False)

    done = _copy(runtime, ops, a, b, source, destination)

    assert _failure(done) is SftpFailureCode.DESTINATION_ALREADY_EXISTS
    assert destination.read_bytes() == b"old"


@pytest.mark.parametrize("same_connection", [False, True])
def test_replace_merges_a_folder(tmp_path, services, same_connection):
    source = tmp_path / "src" / "docs"
    (source / "sub").mkdir(parents=True)
    (source / "shared.txt").write_bytes(b"new shared")
    (source / "sub" / "added.txt").write_bytes(b"added")
    destination = tmp_path / "dest" / "docs"
    destination.mkdir(parents=True)
    (destination / "shared.txt").write_bytes(b"old shared")
    (destination / "theirs.txt").write_bytes(b"kept")
    runtime, ops, a, b = services(same_connection=same_connection)

    done = _copy(runtime, ops, a, b, source, destination, recursive=True, overwrite=True)

    assert done.state is OperationState.SUCCEEDED, done.failure
    assert (destination / "shared.txt").read_bytes() == b"new shared"
    assert (destination / "theirs.txt").read_bytes() == b"kept"
    assert (destination / "sub" / "added.txt").read_bytes() == b"added"
    assert (source / "shared.txt").read_bytes() == b"new shared"


@pytest.mark.parametrize("same_connection", [False, True])
def test_a_move_never_replaces_a_file_with_itself(tmp_path, services, same_connection):
    """Both panes show the same folder. Replacing the file with itself and
    then removing the source would delete it."""
    path = tmp_path / "report.txt"
    path.write_bytes(b"precious")
    runtime, ops, a, b = services(same_connection=same_connection)

    done = _copy(runtime, ops, a, b, path, path, move=True, overwrite=True)

    assert _failure(done) is SftpFailureCode.DESTINATION_ALREADY_EXISTS
    assert path.read_bytes() == b"precious"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["report.txt"]


def test_a_move_never_replaces_a_file_with_itself_through_a_link(tmp_path, services):
    """Two connections to one machine, the folder reached by two names."""
    (tmp_path / "real").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "real")
    path = tmp_path / "real" / "report.txt"
    path.write_bytes(b"precious")
    runtime, ops, a, b = services(same_connection=False)

    done = _copy(
        runtime, ops, a, b, path, tmp_path / "alias" / "report.txt",
        move=True, overwrite=True,
    )

    assert _failure(done) is SftpFailureCode.DESTINATION_ALREADY_EXISTS
    assert path.read_bytes() == b"precious"
    assert sorted(p.name for p in (tmp_path / "real").iterdir()) == ["report.txt"]


@pytest.mark.parametrize("same_connection", [False, True])
def test_a_merge_never_lands_inside_the_folder_it_copies(tmp_path, services, same_connection):
    source = tmp_path / "tree"
    (source / "sub" / "tree").mkdir(parents=True)
    (source / "a.txt").write_bytes(b"a")
    runtime, ops, a, b = services(same_connection=same_connection)

    if same_connection:
        # One server answers before the operation starts.
        with pytest.raises(SshPilotError) as raised:
            _copy(
                runtime, ops, a, b, source, source / "sub" / "tree",
                recursive=True, overwrite=True,
            )
        assert raised.value.details["sftp_failure_code"] == (
            SftpFailureCode.DIRECTORY_CANNOT_BE_COPIED_INTO_ITSELF.value
        )
    else:
        # Two connections: only the probe folder can tell it is one machine.
        done = _copy(
            runtime, ops, a, b, source, source / "sub" / "tree",
            recursive=True, overwrite=True,
        )
        assert _failure(done) is SftpFailureCode.DIRECTORY_CANNOT_BE_COPIED_INTO_ITSELF
    assert sorted(p.name for p in (source / "sub" / "tree").iterdir()) == []


@pytest.mark.parametrize("same_connection", [False, True])
def test_a_file_never_replaces_a_folder(tmp_path, services, same_connection):
    source = tmp_path / "name"
    source.write_bytes(b"file")
    destination = tmp_path / "dest" / "name"
    destination.mkdir(parents=True)
    (destination / "inside.txt").write_bytes(b"kept")
    runtime, ops, a, b = services(same_connection=same_connection)

    done = _copy(runtime, ops, a, b, source, destination, overwrite=True)

    assert _failure(done) is SftpFailureCode.DESTINATION_ALREADY_EXISTS
    assert (destination / "inside.txt").read_bytes() == b"kept"
    assert sorted(p.name for p in destination.parent.iterdir()) == ["name"]


@pytest.mark.parametrize("same_connection", [False, True])
def test_a_folder_never_replaces_a_file(tmp_path, services, same_connection):
    source = tmp_path / "name"
    source.mkdir()
    (source / "a.txt").write_bytes(b"a")
    destination = tmp_path / "dest" / "name"
    destination.parent.mkdir()
    destination.write_bytes(b"kept")
    runtime, ops, a, b = services(same_connection=same_connection)

    done = _copy(runtime, ops, a, b, source, destination, recursive=True, overwrite=True)

    assert _failure(done) is SftpFailureCode.DESTINATION_ALREADY_EXISTS
    assert destination.read_bytes() == b"kept"


def test_a_move_on_one_server_replaces_by_rename(tmp_path, services):
    source = tmp_path / "a.txt"
    source.write_bytes(b"new")
    destination = tmp_path / "dest" / "a.txt"
    destination.parent.mkdir()
    destination.write_bytes(b"old")
    runtime, ops, a, b = services(same_connection=True)

    done = _copy(runtime, ops, a, b, source, destination, move=True, overwrite=True)

    assert done.state is OperationState.SUCCEEDED, done.failure
    assert destination.read_bytes() == b"new"
    assert not source.exists()


@pytest.mark.parametrize("same_connection", [False, True])
def test_a_folder_move_merges_then_removes_the_source(tmp_path, services, same_connection):
    source = tmp_path / "src" / "docs"
    source.mkdir(parents=True)
    (source / "shared.txt").write_bytes(b"new")
    destination = tmp_path / "dest" / "docs"
    destination.mkdir(parents=True)
    (destination / "shared.txt").write_bytes(b"old")
    (destination / "theirs.txt").write_bytes(b"kept")
    runtime, ops, a, b = services(same_connection=same_connection)

    done = _copy(
        runtime, ops, a, b, source, destination,
        recursive=True, move=True, overwrite=True,
    )

    assert done.state is OperationState.SUCCEEDED, done.failure
    assert (destination / "shared.txt").read_bytes() == b"new"
    assert (destination / "theirs.txt").read_bytes() == b"kept"
    assert not source.exists()


def test_overwrite_round_trips_and_is_omitted_when_unset():
    from sshpilot.api.transport.codec import (
        sftp_copy_request_from_wire,
        sftp_copy_request_to_wire,
    )

    request = SftpCopyRequest("sftp-1", "/a", "/b", overwrite=True)
    wire = sftp_copy_request_to_wire(request)
    assert wire["overwrite"] is True
    assert sftp_copy_request_from_wire(wire) == request
    assert "overwrite" not in sftp_copy_request_to_wire(SftpCopyRequest("sftp-1", "/a", "/b"))
    with pytest.raises(TypeError):
        SftpCopyRequest("sftp-1", "/a", "/b", overwrite="yes")

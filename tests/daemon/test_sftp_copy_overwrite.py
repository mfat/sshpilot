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
import posixpath

import pytest

from sshpilot.api.errors import SshPilotError
from sshpilot.api.models.operations import OperationState, SftpCopyRequest, SftpFailureCode

from tests.daemon.test_sftp_copy_between_services import _run, _two_services
from tests.test_sftp_client_large_files import (
    _SFTP_SERVER,
    _local,
    _remote,
    _start_client,
    _stop_client,
)

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
        a, _remote(source), _remote(destination), as_operation=True,
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
    # NTFS has no permission bits to keep (every file reads back 0o666).
    if os.name != "nt":
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


# -- servers without posix-rename, differently rooted sessions, handles -------

from sshpilot.sftp import protocol as sftp_proto  # noqa: E402


def _without_posix_rename(sftp):
    """ProFTPD, AWS Transfer, Dropbear: plain RENAME only."""
    sftp.extensions.pop("posix-rename@openssh.com", None)
    return sftp


def _failing_publish(sftp, destination, code):
    """Putting a file in at *destination* once the old one is set aside
    fails with *code*; after a lost connection nothing gets through."""
    real_rename, real_remove = sftp.rename, sftp.remove
    dead = []

    def rename(old, new):
        if dead:
            raise sftp_proto.SFTPError(sftp_proto.FX_CONNECTION_LOST)
        if new == _remote(destination) and not old.endswith(".old") and not os.path.lexists(_local(new)):
            if code == sftp_proto.FX_CONNECTION_LOST:
                dead.append(True)
            raise sftp_proto.SFTPError(code)
        return real_rename(old, new)

    def remove(path):
        if dead:
            raise sftp_proto.SFTPError(sftp_proto.FX_CONNECTION_LOST)
        return real_remove(path)

    sftp.rename, sftp.remove = rename, remove
    return sftp


@pytest.fixture
def raw_sessions():
    sessions = []

    def start():
        sessions.append(_start_client())
        return sessions[-1][0]

    yield start
    for session in sessions:
        _stop_client(*session)


@pytest.mark.parametrize("same_connection", [False, True])
def test_replace_without_posix_rename_swaps_the_file_in(tmp_path, raw_sessions, same_connection):
    source = tmp_path / "a.txt"
    source.write_bytes(b"new")
    destination = tmp_path / "dest" / "a.txt"
    destination.parent.mkdir()
    destination.write_bytes(b"old")
    first = _without_posix_rename(raw_sessions())
    second = _without_posix_rename(raw_sessions())
    runtime, ops, a, b = _two_services(first, second, same_connection=same_connection)

    done = _copy(runtime, ops, a, b, source, destination, overwrite=True)

    assert done.state is OperationState.SUCCEEDED, done.failure
    assert destination.read_bytes() == b"new"
    assert sorted(p.name for p in destination.parent.iterdir()) == ["a.txt"]


@pytest.mark.parametrize("move", [False, True])
def test_a_failed_replace_without_posix_rename_keeps_the_destination(
    tmp_path, raw_sessions, move
):
    source = tmp_path / "a.txt"
    source.write_bytes(b"new")
    destination = tmp_path / "dest" / "a.txt"
    destination.parent.mkdir()
    destination.write_bytes(b"old")
    sftp = _failing_publish(
        _without_posix_rename(raw_sessions()), destination, sftp_proto.FX_PERMISSION_DENIED
    )
    runtime, ops, a, b = _two_services(sftp, _Unused(), same_connection=True)

    done = _copy(runtime, ops, a, b, source, destination, move=move, overwrite=True)

    assert done.state is OperationState.FAILED
    assert destination.read_bytes() == b"old"
    assert source.read_bytes() == b"new"
    assert sorted(p.name for p in destination.parent.iterdir()) == ["a.txt"]


def test_a_connection_lost_mid_replace_never_deletes_the_destination(tmp_path, raw_sessions):
    source = tmp_path / "a.txt"
    source.write_bytes(b"new")
    destination = tmp_path / "dest" / "a.txt"
    destination.parent.mkdir()
    destination.write_bytes(b"old")
    sftp = _failing_publish(
        _without_posix_rename(raw_sessions()), destination, sftp_proto.FX_CONNECTION_LOST
    )
    runtime, ops, a, b = _two_services(sftp, _Unused(), same_connection=True)

    done = _copy(runtime, ops, a, b, source, destination, overwrite=True)

    assert done.state is OperationState.FAILED
    # Set aside under a hidden name, not removed.
    kept = [p.read_bytes() for p in destination.parent.iterdir()]
    assert b"old" in kept


class _Unused:
    """The destination session of a same-connection copy: never used."""

    def __getattr__(self, name):
        raise AssertionError(f"destination session used: {name}")


class _Rooted:
    """A session whose ``/`` is *root* on the server, as a chrooted SFTP
    account sees it: ``/report`` here is ``<root>/report`` elsewhere."""

    def __init__(self, target, root):
        self._target = target
        self._root = _remote(root).rstrip("/")

    def _in(self, value):
        if isinstance(value, str) and value.startswith("/"):
            return self._root + value if value != "/" else self._root
        return value

    def realpath(self, path):
        real = self._target.realpath(self._in(path))
        if real == self._root:
            return "/"
        assert real.startswith(self._root + "/"), real
        return real[len(self._root):]

    def __getattr__(self, name):
        attr = getattr(self._target, name)
        if not callable(attr):
            return attr
        return lambda *a, **k: attr(*(self._in(v) for v in a), **k)


def test_a_move_never_replaces_a_file_with_itself_seen_from_another_root(
    tmp_path, raw_sessions
):
    """``<tmp>/share/report.txt`` in one session is ``/report.txt`` in a
    session rooted at ``<tmp>/share``: replacing it, then removing the
    source, would delete the only copy."""
    share = tmp_path / "share"
    share.mkdir()
    path = share / "report.txt"
    path.write_bytes(b"precious")
    rooted = _Rooted(raw_sessions(), share)
    runtime, ops, a, b = _two_services(raw_sessions(), rooted)

    done = _run(runtime, ops, SftpCopyRequest(
        a, _remote(path), "/report.txt", as_operation=True,
        destination_service_id=b, move=True, overwrite=True,
    ))

    assert _failure(done) is SftpFailureCode.DESTINATION_ALREADY_EXISTS
    assert path.read_bytes() == b"precious"
    assert sorted(p.name for p in share.iterdir()) == ["report.txt"]


def test_another_root_still_replaces_a_different_file(tmp_path, raw_sessions):
    share = tmp_path / "share"
    share.mkdir()
    (share / "report.txt").write_bytes(b"old")
    source = tmp_path / "report.txt"
    source.write_bytes(b"new")
    rooted = _Rooted(raw_sessions(), share)
    runtime, ops, a, b = _two_services(raw_sessions(), rooted)

    done = _run(runtime, ops, SftpCopyRequest(
        a, _remote(source), "/report.txt", as_operation=True,
        destination_service_id=b, move=True, overwrite=True,
    ))

    assert done.state is OperationState.SUCCEEDED, done.failure
    assert (share / "report.txt").read_bytes() == b"new"
    assert not source.exists()


class _CountingHandles:
    """Counts the files the destination opens and the ones it closes."""

    def __init__(self, target):
        self._target = target
        self.opened = 0
        self.closed = 0

    def open(self, *args, **kwargs):
        handle = self._target.open(*args, **kwargs)
        self.opened += 1
        counter = self
        real_exit = handle.__exit__
        real_close = handle.close

        class _Counted:
            def __getattr__(self, name):
                return getattr(handle, name)

            def __enter__(self):
                handle.__enter__()
                return self

            def __exit__(self, *exc):
                counter.closed += 1
                return real_exit(*exc)

            def close(self):
                counter.closed += 1
                return real_close()

        return _Counted()

    def __getattr__(self, name):
        return getattr(self._target, name)


@pytest.mark.parametrize("conflict", ["folder", "same_file"])
def test_a_refused_replace_closes_its_staging_file(tmp_path, raw_sessions, conflict):
    if conflict == "folder":
        source = tmp_path / "name"
        source.write_bytes(b"file")
        destination = tmp_path / "dest" / "name"
        destination.mkdir(parents=True)
    else:
        source = destination = tmp_path / "report.txt"
        source.write_bytes(b"precious")
    counting = _CountingHandles(raw_sessions())
    runtime, ops, a, b = _two_services(raw_sessions(), counting)

    for _ in range(3):
        done = _copy(runtime, ops, a, b, source, destination, move=True, overwrite=True)
        assert _failure(done) is SftpFailureCode.DESTINATION_ALREADY_EXISTS

    assert counting.opened == 3
    assert counting.closed == counting.opened


# -- servers whose REALPATH leaves links unresolved (Win32-OpenSSH) ----------


class _LinksUnresolved:
    """A session whose REALPATH only tidies the text, as Win32-OpenSSH's
    does: ``alias/report.txt`` stays ``alias/report.txt`` even when
    ``alias`` is a link to ``real``."""

    def __init__(self, target):
        self._target = target

    def realpath(self, path):
        return posixpath.normpath(path)

    def __getattr__(self, name):
        return getattr(self._target, name)


def _linked_folder(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "real")
    return tmp_path / "real", tmp_path / "alias"


def _copy_or_refusal(runtime, ops, a, b, source, destination, **flags):
    """The failure code, whether refused up front or by the operation."""
    try:
        done = _copy(runtime, ops, a, b, source, destination, **flags)
    except SshPilotError as exc:
        return SftpFailureCode(exc.details["sftp_failure_code"])
    return _failure(done)


@pytest.mark.parametrize("same_connection", [False, True])
def test_a_move_through_an_unresolved_link_never_replaces_the_file_with_itself(
    tmp_path, raw_sessions, same_connection
):
    """Found on a Windows server: the move succeeded and the file was gone."""
    real, alias = _linked_folder(tmp_path)
    path = real / "report.txt"
    path.write_bytes(b"precious")
    runtime, ops, a, b = _two_services(
        _LinksUnresolved(raw_sessions()), _LinksUnresolved(raw_sessions()),
        same_connection=same_connection,
    )

    done = _copy(runtime, ops, a, b, path, alias / "report.txt", move=True, overwrite=True)

    assert _failure(done) is SftpFailureCode.DESTINATION_ALREADY_EXISTS
    assert path.read_bytes() == b"precious"
    assert sorted(p.name for p in real.iterdir()) == ["report.txt"]


@pytest.mark.parametrize("merge", [False, True])
@pytest.mark.parametrize("same_connection", [False, True])
def test_a_folder_is_never_copied_into_itself_through_an_unresolved_link(
    tmp_path, raw_sessions, same_connection, merge
):
    """Found on a Windows server: the copy nested ``sub/real`` twenty
    levels deep before the path grew too long."""
    real, alias = _linked_folder(tmp_path)
    (real / "sub").mkdir()
    (real / "a.txt").write_bytes(b"a")
    if merge:
        (real / "sub" / "real").mkdir()
    runtime, ops, a, b = _two_services(
        _LinksUnresolved(raw_sessions()), _LinksUnresolved(raw_sessions()),
        same_connection=same_connection,
    )

    failure = _copy_or_refusal(
        runtime, ops, a, b, real, alias / "sub" / "real", recursive=True, overwrite=merge,
    )

    assert failure is SftpFailureCode.DIRECTORY_CANNOT_BE_COPIED_INTO_ITSELF
    left = sorted(p.name for p in (real / "sub").iterdir())
    assert left == (["real"] if merge else [])
    if merge:
        assert list((real / "sub" / "real").iterdir()) == []
    # The probe folder is gone again.
    assert sorted(p.name for p in real.iterdir()) == ["a.txt", "sub"]


@pytest.mark.parametrize("same_connection", [False, True])
def test_a_move_onto_the_same_name_in_another_case_is_refused(
    tmp_path, raw_sessions, same_connection
):
    """On NTFS (and APFS) ``Report.txt`` and ``report.txt`` are one file:
    found on a Windows server, the move replaced it with itself and removed
    it. A case-sensitive server cannot be told apart, so the replace is
    refused everywhere, which only costs one refused replace."""
    source = tmp_path / "Report.txt"
    source.write_bytes(b"precious")
    other = tmp_path / "report.txt"
    other.write_bytes(b"other")
    runtime, ops, a, b = _two_services(
        raw_sessions(), raw_sessions(), same_connection=same_connection
    )

    done = _copy(runtime, ops, a, b, source, other, move=True, overwrite=True)

    assert _failure(done) is SftpFailureCode.DESTINATION_ALREADY_EXISTS
    assert source.read_bytes() == b"precious"
    assert other.read_bytes() == b"other"

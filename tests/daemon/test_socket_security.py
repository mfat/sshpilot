import os
import socket
import stat

import pytest

from sshpilot.daemon import (
    DaemonAlreadyRunningError,
    SocketSecurityError,
)
from sshpilot.daemon.lifecycle import prepare_socket_path


pytestmark = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX") or not hasattr(os, "getuid"),
    reason="Unix socket security is only available on Unix",
)


def test_bound_socket_is_private_owned_and_a_unix_socket(daemon_factory):
    server, _manager = daemon_factory()
    directory = server.socket_path.parent.stat()
    endpoint = server.socket_path.lstat()

    assert stat.S_IMODE(directory.st_mode) == 0o700
    assert stat.S_IMODE(endpoint.st_mode) == 0o600
    assert endpoint.st_uid == os.getuid()
    assert stat.S_ISSOCK(endpoint.st_mode)
    assert server._listener.family == socket.AF_UNIX


def test_stale_socket_is_recovered_and_cleaned_after_shutdown(tmp_path, daemon_factory):
    directory = tmp_path / "stale"
    directory.mkdir(mode=0o700)
    path = directory / "sshpilotd.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()

    server, _manager = daemon_factory(socket_path=path)
    server.shutdown()

    assert server.wait_stopped()
    assert not path.exists()


def test_active_daemon_is_not_replaced(daemon_factory):
    first, manager = daemon_factory()
    second, _manager = daemon_factory(
        manager=manager,
        socket_path=first.socket_path,
        start=False,
    )

    with pytest.raises(DaemonAlreadyRunningError):
        second.start_in_thread()

    assert first.ready


def test_insecure_directory_is_refused(tmp_path, daemon_factory):
    directory = tmp_path / "insecure"
    directory.mkdir(mode=0o755)
    path = directory / "sshpilotd.sock"
    server, _manager = daemon_factory(socket_path=path, start=False)

    with pytest.raises(SocketSecurityError):
        server.start_in_thread()

    assert not path.exists()


def test_non_socket_path_is_never_unlinked(tmp_path):
    directory = tmp_path / "file-target"
    directory.mkdir(mode=0o700)
    path = directory / "sshpilotd.sock"
    path.write_text("keep", encoding="utf-8")

    with pytest.raises(SocketSecurityError):
        prepare_socket_path(path)

    assert path.read_text(encoding="utf-8") == "keep"


def test_symlink_path_is_never_unlinked(tmp_path):
    directory = tmp_path / "symlink-target"
    directory.mkdir(mode=0o700)
    target = directory / "target"
    target.write_text("keep", encoding="utf-8")
    path = directory / "sshpilotd.sock"
    path.symlink_to(target)

    with pytest.raises(SocketSecurityError):
        prepare_socket_path(path)

    assert path.is_symlink()
    assert target.read_text(encoding="utf-8") == "keep"


def test_symlink_directory_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(SocketSecurityError):
        prepare_socket_path(linked / "sshpilotd.sock")

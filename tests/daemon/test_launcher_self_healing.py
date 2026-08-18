"""Startup must survive whatever is squatting on the daemon socket.

A desktop application cannot be left unusable because a previous build's
daemon is still resident, because one wedged before it could answer, or
because a stale socket file outlived its owner. Every one of those states is
recovered here rather than reported, so ``connect_or_start`` either returns a
working client or fails for a reason that is genuinely not about the peer.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sshpilot.daemon.launcher import (
    DaemonLaunchError,
    DaemonLauncher,
    DaemonStartupFailure,
)
from sshpilot.daemon.lifecycle import (
    evict_socket_owner,
    peer_process_id,
    probe_socket_owner,
    remove_dead_socket,
)


# A process that owns the socket but can never serve it: it answers the
# connection and hangs up, so the handshake fails rather than hanging. This is
# the shape of both a wedged daemon and one whose protocol we cannot speak.
_SQUATTER = """
import os, signal, socket, sys, time

path, mode = sys.argv[1], sys.argv[2]
if mode == "ignore-term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
listener.bind(path)
os.chmod(path, 0o600)
listener.listen(8)
sys.stdout.write("ready\\n")
sys.stdout.flush()
while True:
    try:
        peer, _address = listener.accept()
        peer.close()
    except OSError:
        time.sleep(0.05)
"""


def _start_squatter(socket_path: Path, *, mode: str = "term") -> subprocess.Popen:
    socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    process = subprocess.Popen(
        [sys.executable, "-c", _SQUATTER, str(socket_path), mode],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert process.stdout is not None
    if process.stdout.readline().strip() != "ready":
        process.kill()
        pytest.fail("squatter process did not bind the socket")
    return process


def _reap(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _isolated_environment(tmp_path: Path) -> dict:
    environment = dict(os.environ)
    environment.update(
        {
            "HOME": str(tmp_path / "home"),
            "XDG_RUNTIME_DIR": str(tmp_path / "xdg-runtime"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
        }
    )
    for key in (
        "HOME",
        "XDG_RUNTIME_DIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
    ):
        Path(environment[key]).mkdir(parents=True, exist_ok=True)
    return environment


def _require_daemon_subprocess() -> None:
    probe = subprocess.run(
        [sys.executable, "-c", "import gi"], capture_output=True, check=False
    )
    if probe.returncode:
        pytest.skip("production daemon dependencies unavailable to subprocess")


def _stop_launched(result, socket_path: Path) -> None:
    result.client.close()
    handle = result.process
    if handle is not None and handle.process.poll() is None:
        handle.process.terminate()
        try:
            handle.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            handle.process.kill()
            handle.process.wait(timeout=5)
    deadline = time.monotonic() + 5
    while socket_path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# Eviction primitives
# ---------------------------------------------------------------------------


def test_peer_process_id_identifies_the_process_holding_the_socket(tmp_path):
    socket_path = tmp_path / "runtime" / "sshpilotd.sock"
    squatter = _start_squatter(socket_path)
    try:
        accepting, pid = probe_socket_owner(socket_path)
        assert accepting is True
        if pid is None:
            pytest.skip("peer credentials unavailable on this platform")
        assert pid == squatter.pid
    finally:
        _reap(squatter)


def test_peer_process_id_returns_none_without_credentials_support(tmp_path):
    """A platform that reports no peer identity must not guess one."""
    socket_path = tmp_path / "runtime" / "pair.sock"
    socket_path.parent.mkdir(mode=0o700, parents=True)
    left, right = socket.socketpair()
    try:
        pid = peer_process_id(left)
        # Both ends are this process, so any reported pid must be our own.
        assert pid in (None, os.getpid())
    finally:
        left.close()
        right.close()


def test_evict_socket_owner_terminates_the_holder_and_frees_the_path(tmp_path):
    socket_path = tmp_path / "runtime" / "sshpilotd.sock"
    squatter = _start_squatter(socket_path)
    try:
        assert evict_socket_owner(socket_path) is True
        assert squatter.wait(timeout=5) is not None
        assert not socket_path.exists()
        assert probe_socket_owner(socket_path) == (False, None)
    finally:
        _reap(squatter)


def test_evict_socket_owner_escalates_to_sigkill(tmp_path):
    """A daemon that ignores SIGTERM is still not allowed to hold the socket."""
    socket_path = tmp_path / "runtime" / "sshpilotd.sock"
    squatter = _start_squatter(socket_path, mode="ignore-term")
    try:
        assert evict_socket_owner(socket_path, term_timeout=0.3) is True
        assert squatter.wait(timeout=5) == -signal.SIGKILL
        assert not socket_path.exists()
    finally:
        _reap(squatter)


def test_remove_dead_socket_clears_an_orphan_but_spares_a_live_one(tmp_path):
    orphan_dir = tmp_path / "orphan"
    orphan_dir.mkdir(mode=0o700)
    orphan = orphan_dir / "sshpilotd.sock"
    holder = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    holder.bind(str(orphan))
    holder.close()  # Socket file survives its owner: nothing accepts on it.
    assert orphan.exists()
    assert remove_dead_socket(orphan) is True
    assert not orphan.exists()

    live_path = tmp_path / "live" / "sshpilotd.sock"
    squatter = _start_squatter(live_path)
    try:
        assert remove_dead_socket(live_path) is False
        assert live_path.exists()
    finally:
        _reap(squatter)


# ---------------------------------------------------------------------------
# Launcher recovery
# ---------------------------------------------------------------------------


def test_unusable_peer_is_evicted_and_startup_still_succeeds(tmp_path):
    """The headline guarantee: a squatter cannot stop the app from starting."""
    _require_daemon_subprocess()
    socket_path = tmp_path / "runtime" / "sshpilotd.sock"
    squatter = _start_squatter(socket_path)
    launcher = DaemonLauncher(
        socket_path=socket_path,
        startup_timeout=15,
        environment=_isolated_environment(tmp_path),
    )
    try:
        result = launcher.connect_or_start()
    except BaseException:
        _reap(squatter)
        raise
    try:
        assert result.process is not None, "a fresh daemon must have been launched"
        assert result.client.list_connections() == []
        assert squatter.poll() is not None, "the squatter must have been evicted"
    finally:
        _stop_launched(result, socket_path)
        _reap(squatter)


def test_orphaned_socket_file_does_not_block_startup(tmp_path):
    _require_daemon_subprocess()
    socket_path = tmp_path / "runtime" / "sshpilotd.sock"
    socket_path.parent.mkdir(mode=0o700, parents=True)
    holder = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    holder.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    holder.close()

    launcher = DaemonLauncher(
        socket_path=socket_path,
        startup_timeout=15,
        environment=_isolated_environment(tmp_path),
    )
    result = launcher.connect_or_start()
    try:
        assert result.process is not None
        assert result.client.list_connections() == []
    finally:
        _stop_launched(result, socket_path)


def test_stray_endpoint_file_is_repaired_instead_of_failing_startup(tmp_path):
    """A non-socket at the endpoint is our own litter, inside our own 0700 dir.

    It used to fail startup permanently with ``unsafe_socket``; nothing about
    a file only this user could have created justifies that.
    """
    directory = tmp_path / "runtime"
    directory.mkdir(mode=0o700)
    socket_path = directory / "sshpilotd.sock"
    socket_path.write_text("not a socket", encoding="utf-8")

    launches = []

    def _record(command, **_kwargs):
        launches.append(command)
        raise OSError("launch suppressed for this test")

    launcher = DaemonLauncher(socket_path=socket_path, popen=_record)
    with pytest.raises(DaemonLaunchError) as caught:
        launcher.connect_or_start()

    # It got past the socket check and tried to start a daemon.
    assert caught.value.reason is DaemonStartupFailure.PROCESS_EXITED
    assert launches, "startup must proceed to launching a daemon"
    assert not socket_path.exists()


def test_loose_runtime_directory_mode_is_tightened(tmp_path):
    """A 0755 runtime dir (lax umask) is repaired, not treated as fatal."""
    directory = tmp_path / "runtime"
    directory.mkdir(mode=0o755)
    socket_path = directory / "sshpilotd.sock"

    launches = []

    def _record(command, **_kwargs):
        launches.append(command)
        raise OSError("launch suppressed for this test")

    launcher = DaemonLauncher(socket_path=socket_path, popen=_record)
    with pytest.raises(DaemonLaunchError) as caught:
        launcher.connect_or_start()

    assert caught.value.reason is DaemonStartupFailure.PROCESS_EXITED
    assert launches, "startup must proceed once the directory is repaired"
    assert directory.stat().st_mode & 0o777 == 0o700


def test_symlinked_runtime_directory_still_fails_closed(tmp_path):
    """Repair never extends to a path that is not really ours."""
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "runtime"
    link.symlink_to(real, target_is_directory=True)
    socket_path = link / "sshpilotd.sock"

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("an unsafe socket location must not launch a daemon")

    launcher = DaemonLauncher(socket_path=socket_path, popen=_forbidden)
    with pytest.raises(DaemonLaunchError) as caught:
        launcher.connect_or_start()
    assert caught.value.reason is DaemonStartupFailure.UNSAFE_SOCKET


def test_incompatible_daemon_is_asked_to_stop_before_it_is_signalled(
    tmp_path, daemon_factory, monkeypatch
):
    """Eviction prefers the graceful path so the outgoing daemon cleans up.

    The peer here is a real, healthy daemon that this build simply considers
    incompatible — exactly the DMG-upgrade case. It must be stopped by RPC,
    not killed, so its sessions and ControlMasters are torn down properly.
    """
    server, _manager = daemon_factory()
    monkeypatch.setattr(
        "sshpilot.api.version.API_IMPLEMENTATION_VERSION", "0.0-old"
    )

    launcher = DaemonLauncher(socket_path=server.socket_path)
    killed = []
    monkeypatch.setattr(
        "sshpilot.daemon.launcher.evict_socket_owner",
        lambda path, **kwargs: killed.append(path) or True,
    )

    assert launcher._evict_unusable_peer() is True
    assert server.wait_stopped(timeout=5), "the graceful stop must have landed"


def test_frozen_builds_get_a_longer_startup_budget(monkeypatch):
    """A packaged app is a bundle launch, not an interpreter launch.

    Three seconds is the source-tree budget; a signed .app doing first-launch
    validation routinely needs more, and timing out there reports a broken
    daemon when it was only still starting.
    """
    from sshpilot.daemon.launcher import (
        DEFAULT_STARTUP_TIMEOUT,
        FROZEN_STARTUP_TIMEOUT,
    )

    socket_path = Path("/nonexistent/runtime/sshpilotd.sock")
    assert DaemonLauncher(socket_path=socket_path).startup_timeout == (
        DEFAULT_STARTUP_TIMEOUT
    )

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert DaemonLauncher(socket_path=socket_path).startup_timeout == (
        FROZEN_STARTUP_TIMEOUT
    )
    assert FROZEN_STARTUP_TIMEOUT > DEFAULT_STARTUP_TIMEOUT

    # An explicit value always wins over the environment-derived default.
    assert (
        DaemonLauncher(socket_path=socket_path, startup_timeout=1.5).startup_timeout
        == 1.5
    )


def test_frozen_children_are_told_they_are_packaged():
    """The packaged idle-shutdown default only applies if the child knows."""
    from sshpilot.daemon.launcher import _child_environment

    assert "SSHPILOT_PACKAGED" not in _child_environment({})


def test_frozen_child_environment_marks_packaged(monkeypatch):
    from sshpilot.daemon.launcher import _child_environment

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert _child_environment({})["SSHPILOT_PACKAGED"] == "1"

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


def test_eviction_never_signals_a_pid_that_no_longer_owns_the_socket(tmp_path):
    """The PID is re-confirmed before every signal, not captured once.

    We are, by definition, evicting a process that may be exiting right now.
    If it goes and its PID is recycled, a stale number would name an
    unrelated process — and this code's whole job is to send SIGKILL to it.
    """
    import sshpilot.daemon.lifecycle as lifecycle

    socket_path = tmp_path / "runtime" / "sshpilotd.sock"
    squatter = _start_squatter(socket_path)
    try:
        real_probe = lifecycle.probe_socket_owner
        probes = []

        def _owner_changed(path, **kwargs):
            accepting, pid = real_probe(path, **kwargs)
            probes.append(pid)
            # First look reports the truth; from then on the socket appears
            # to belong to somebody else, exactly as a PID recycle would.
            if len(probes) == 1:
                return accepting, pid
            return accepting, (pid or 0) + 100000

        signalled = []
        monkey_kill = lambda pid, sig: signalled.append((pid, sig))
        lifecycle.probe_socket_owner = _owner_changed
        original_kill = lifecycle.os.kill
        lifecycle.os.kill = monkey_kill
        try:
            assert lifecycle.evict_socket_owner(socket_path) is False
        finally:
            lifecycle.probe_socket_owner = real_probe
            lifecycle.os.kill = original_kill

        assert signalled == [], "no signal may be sent to an unconfirmed PID"
        assert squatter.poll() is None, "the squatter must be left alone"
        assert socket_path.exists()
    finally:
        _reap(squatter)


def test_eviction_reconfirms_ownership_before_escalating_to_sigkill(tmp_path):
    """The SIGKILL step re-probes too, not just the SIGTERM step."""
    import sshpilot.daemon.lifecycle as lifecycle

    socket_path = tmp_path / "runtime" / "sshpilotd.sock"
    squatter = _start_squatter(socket_path, mode="ignore-term")
    try:
        real_probe = lifecycle.probe_socket_owner
        calls = {"n": 0}

        def _changes_after_sigterm(path, **kwargs):
            accepting, pid = real_probe(path, **kwargs)
            calls["n"] += 1
            # Confirm for SIGTERM, then look like a different owner so the
            # SIGKILL escalation has to stand down.
            if calls["n"] <= 1:
                return accepting, pid
            return accepting, (pid or 0) + 100000

        signalled = []
        original_kill = lifecycle.os.kill

        def _record_kill(pid, sig):
            signalled.append(sig)
            original_kill(pid, sig)

        lifecycle.probe_socket_owner = _changes_after_sigterm
        lifecycle.os.kill = _record_kill
        try:
            lifecycle.evict_socket_owner(socket_path, term_timeout=0.2)
        finally:
            lifecycle.probe_socket_owner = real_probe
            lifecycle.os.kill = original_kill

        assert signal.SIGKILL not in signalled, (
            "SIGKILL must not be sent once ownership can no longer be confirmed"
        )
        assert squatter.poll() is None
    finally:
        _reap(squatter)


def test_an_accepted_stop_is_given_time_to_drain_before_any_signal(tmp_path):
    """A daemon that accepted the stop is leaving; do not shoot it mid-drain.

    ``daemon.stop`` replies on acceptance while the daemon then drains (5s of
    its own budget), and a killed daemon cannot reap the ssh children the
    drain exists to clean up.
    """
    socket_path = tmp_path / "runtime" / "sshpilotd.sock"
    squatter = _start_squatter(socket_path)
    launcher = DaemonLauncher(socket_path=socket_path)

    evictions = []

    def _stopped_on_request() -> bool:
        # Stand in for a daemon that accepts the stop and then takes a moment
        # to unlink its socket, the way a real drain does.
        _reap(squatter)
        socket_path.unlink(missing_ok=True)
        return True

    launcher._request_peer_stop = _stopped_on_request
    import sshpilot.daemon.launcher as launcher_mod

    original_evict = launcher_mod.evict_socket_owner
    launcher_mod.evict_socket_owner = lambda *a, **k: evictions.append(a) or True
    try:
        assert launcher._evict_unusable_peer() is True
    finally:
        launcher_mod.evict_socket_owner = original_evict
        _reap(squatter)

    assert evictions == [], "a daemon that left on request must never be signalled"


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


def test_a_socket_replaced_after_the_probe_is_never_unlinked(tmp_path):
    """Required: the inode check must survive a socket changing hands.

    Between deciding a socket is dead and removing it, a new daemon may claim
    the path. Unlinking then would strand a healthy daemon holding a socket
    nobody can reach.
    """
    import sshpilot.daemon.lifecycle as lifecycle

    socket_path = tmp_path / "runtime" / "sshpilotd.sock"
    socket_path.parent.mkdir(mode=0o700, parents=True)

    # A dead socket file: bound, then its owner went away.
    orphan = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    orphan.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    orphan.close()
    original_inode = socket_path.stat().st_ino

    real_probe = lifecycle.probe_socket_owner

    def _replace_socket_mid_flight(path, **kwargs):
        result = real_probe(path, **kwargs)
        # Simulate a new daemon claiming the path right after the probe said
        # nothing was listening.
        if socket_path.stat().st_ino == original_inode:
            socket_path.unlink()
            replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            replacement.bind(str(socket_path))
            os.chmod(socket_path, 0o600)
            replacement.listen(4)
            _replace_socket_mid_flight.holder = replacement
        return result

    lifecycle.probe_socket_owner = _replace_socket_mid_flight
    try:
        assert lifecycle.remove_dead_socket(socket_path) is False
    finally:
        lifecycle.probe_socket_owner = real_probe
        holder = getattr(_replace_socket_mid_flight, "holder", None)
        if holder is not None:
            holder.close()

    assert socket_path.exists(), "the replacement socket must survive"
    assert socket_path.stat().st_ino != original_inode


def test_a_still_starting_daemon_is_not_killed_for_being_slow(tmp_path):
    """Required by G: an ambiguous handshake gets a grace window, not a signal.

    A daemon binds its socket before it can serve, so "connected but no
    handshake" is what a healthy daemon looks like mid-startup. Killing on the
    first such failure would shoot a daemon that was about to work.
    """
    from sshpilot.daemon.launcher import DaemonStartupFailure

    socket_path = tmp_path / "runtime" / "sshpilotd.sock"
    socket_path.parent.mkdir(mode=0o700, parents=True)
    launcher = DaemonLauncher(socket_path=socket_path)

    probes = []
    sentinel = object()

    def _slow_start():
        probes.append(1)
        # Unusable twice, then ready — a daemon finishing its startup.
        if len(probes) < 3:
            return DaemonStartupFailure.HANDSHAKE_FAILED
        return sentinel

    launcher._probe_peer_once = _slow_start

    def _must_not_evict():
        raise AssertionError("a daemon that became ready must never be evicted")

    launcher._evict_unusable_peer = _must_not_evict

    assert launcher._connect_to_usable_peer() is sentinel
    assert len(probes) >= 3


def test_a_proven_incompatible_daemon_is_not_granted_the_grace_window(tmp_path):
    """The grace window must not slow down (or soften) a real mismatch."""
    from sshpilot.daemon.launcher import DaemonStartupFailure

    socket_path = tmp_path / "runtime" / "sshpilotd.sock"
    socket_path.parent.mkdir(mode=0o700, parents=True)
    launcher = DaemonLauncher(socket_path=socket_path)

    probes = []
    launcher._probe_peer_once = lambda: (
        probes.append(1) or DaemonStartupFailure.API_VERSION_MISMATCH
    )
    evictions = []
    launcher._evict_unusable_peer = lambda: evictions.append(1) or False

    with pytest.raises(DaemonLaunchError) as caught:
        launcher._connect_to_usable_peer()

    assert caught.value.reason is DaemonStartupFailure.API_VERSION_MISMATCH
    assert evictions, "a proven mismatch is evicted immediately"
    assert len(probes) == 2, "no grace retries for a proven mismatch"


def test_an_unidentifiable_live_peer_is_neither_signalled_nor_unlinked(tmp_path):
    """Required by F: no PID, no graceful stop — fail safe, not destructive."""
    import sshpilot.daemon.launcher as launcher_mod

    socket_path = tmp_path / "runtime" / "sshpilotd.sock"
    squatter = _start_squatter(socket_path)
    launcher = DaemonLauncher(socket_path=socket_path)
    launcher._request_peer_stop = lambda: False

    real_probe = launcher_mod.probe_socket_owner
    launcher_mod.probe_socket_owner = lambda path, **kw: (True, None)
    evictions = []
    real_evict = launcher_mod.evict_socket_owner
    launcher_mod.evict_socket_owner = lambda *a, **k: evictions.append(a) or True
    try:
        assert launcher._evict_unusable_peer() is False
    finally:
        launcher_mod.probe_socket_owner = real_probe
        launcher_mod.evict_socket_owner = real_evict

    assert evictions == [], "eviction must not run without an identified peer"
    assert squatter.poll() is None, "the peer must be left alone"
    assert socket_path.exists(), "a live socket must never be unlinked"
    _reap(squatter)

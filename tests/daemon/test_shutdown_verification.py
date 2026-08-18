"""Quit is gated on proof that sshPilot's runtime is gone.

The contract these tests defend: after the user confirms Quit, the application
either verifies that everything it started is gone and exits, or keeps the
window open and says what survived. "The socket disappeared" is not proof —
a killed daemon leaves children and ControlMasters that no socket check sees.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sshpilot.daemon.control_masters import owns_default_control_master_namespace
from sshpilot.daemon.process_registry import (
    KIND_FORWARD,
    KIND_SESSION,
    OwnedProcessRegistry,
    identify_process,
)
from sshpilot.daemon.runtime_verification import (
    SURVIVOR_CHILD,
    SURVIVOR_CONTROL_MASTER,
    SURVIVOR_DAEMON,
    SURVIVOR_SOCKET,
    reap_orphaned_children,
    verify_sshpilot_runtime_terminated,
)


def _sleeper(*, ignore_term: bool = False) -> subprocess.Popen:
    """A real child process, ready before it is returned.

    Readiness is explicit because a child that has not yet installed its
    SIGTERM handler would be killed by the first signal, quietly turning a
    SIGKILL-escalation test into a SIGTERM test that proves nothing.
    """
    body = (
        "import signal, sys, time\n"
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "")
        + "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
        + "time.sleep(60)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", body], stdout=subprocess.PIPE, text=True
    )
    assert process.stdout is not None
    if process.stdout.readline().strip() != "ready":
        process.kill()
        raise AssertionError("helper process did not start")
    return process


def _reap(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _wait_gone(process: subprocess.Popen, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def test_nothing_owned_verifies_as_terminated(tmp_path):
    result = verify_sshpilot_runtime_terminated(
        socket_path=tmp_path / "absent.sock",
        registry_path=tmp_path / "processes.json",
        check_control_masters=False,
    )
    assert result.terminated is True
    assert result.messages() == ()


def test_a_surviving_tracked_child_blocks_termination(tmp_path):
    """Required: a live owned SSH child must prevent a 'successful' Quit."""
    registry_path = tmp_path / "processes.json"
    registry = OwnedProcessRegistry(registry_path)
    child = _sleeper()
    try:
        registry.register(child.pid, kind=KIND_SESSION, label="demo-host")

        result = verify_sshpilot_runtime_terminated(
            socket_path=tmp_path / "absent.sock",
            registry_path=registry_path,
            check_control_masters=False,
        )

        assert result.terminated is False
        assert [item.kind for item in result.survivors] == [SURVIVOR_CHILD]
        assert f"pid={child.pid}" in result.describe()
        assert "demo-host" in result.describe()
    finally:
        _reap(child)


def test_a_dead_tracked_child_does_not_block_termination(tmp_path):
    """Required: all tracked resources dead allows exit."""
    registry_path = tmp_path / "processes.json"
    registry = OwnedProcessRegistry(registry_path)
    child = _sleeper()
    registry.register(child.pid, kind=KIND_FORWARD)
    _reap(child)

    result = verify_sshpilot_runtime_terminated(
        socket_path=tmp_path / "absent.sock",
        registry_path=registry_path,
        check_control_masters=False,
    )
    assert result.terminated is True


def test_a_live_daemon_socket_blocks_termination(tmp_path):
    socket_path = tmp_path / "runtime" / "sshpilotd.sock"
    socket_path.parent.mkdir(mode=0o700, parents=True)
    squatter = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os,socket,sys,time\n"
            "l = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            "l.bind(sys.argv[1]); os.chmod(sys.argv[1], 0o600); l.listen(4)\n"
            "sys.stdout.write('ready\\n'); sys.stdout.flush(); time.sleep(60)\n",
            str(socket_path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert squatter.stdout is not None
        assert squatter.stdout.readline().strip() == "ready"
        result = verify_sshpilot_runtime_terminated(
            socket_path=socket_path,
            registry_path=tmp_path / "processes.json",
            check_control_masters=False,
        )
        assert result.terminated is False
        assert result.survivors[0].kind == SURVIVOR_SOCKET
    finally:
        _reap(squatter)


def test_a_live_daemon_process_blocks_termination(tmp_path):
    daemon = _sleeper()
    try:
        identity = identify_process(daemon.pid)
        assert identity is not None
        result = verify_sshpilot_runtime_terminated(
            socket_path=tmp_path / "absent.sock",
            daemon_identity=identity,
            registry_path=tmp_path / "processes.json",
            check_control_masters=False,
        )
        assert result.terminated is False
        assert result.survivors[0].kind == SURVIVOR_DAEMON
    finally:
        _reap(daemon)


def test_a_recycled_daemon_pid_does_not_read_as_alive(tmp_path):
    from sshpilot.daemon.process_registry import ProcessIdentity

    daemon = _sleeper()
    try:
        stale = ProcessIdentity(pid=daemon.pid, create_time=1.0)
        result = verify_sshpilot_runtime_terminated(
            socket_path=tmp_path / "absent.sock",
            daemon_identity=stale,
            registry_path=tmp_path / "processes.json",
            check_control_masters=False,
        )
        assert result.terminated is True, (
            "a PID whose fingerprint no longer matches is not our daemon"
        )
    finally:
        _reap(daemon)


def test_a_surviving_control_master_blocks_termination(tmp_path, monkeypatch):
    """Required: ControlMaster termination failure prevents a successful Quit."""
    import sshpilot.daemon.runtime_verification as verification
    from sshpilot.daemon.control_masters import OwnedControlMaster

    monkeypatch.setattr(
        verification,
        "list_owned_control_masters",
        lambda **_kwargs: (
            OwnedControlMaster(path=Path("/tmp/cm/abc"), pid=4242),
        ),
    )

    result = verify_sshpilot_runtime_terminated(
        socket_path=tmp_path / "absent.sock",
        registry_path=tmp_path / "processes.json",
        check_control_masters=True,
    )
    assert result.terminated is False
    assert result.survivors[0].kind == SURVIVOR_CONTROL_MASTER
    assert "pid=4242" in result.describe()


# ---------------------------------------------------------------------------
# Reaping orphans a killed daemon left behind
# ---------------------------------------------------------------------------


def test_orphaned_children_are_reaped_and_then_verify_clean(tmp_path):
    registry_path = tmp_path / "processes.json"
    registry = OwnedProcessRegistry(registry_path)
    child = _sleeper()
    try:
        registry.register(child.pid, kind=KIND_SESSION)
        remaining = reap_orphaned_children(registry_path=registry_path)
        assert remaining == ()
        assert _wait_gone(child), "the orphan must actually be dead"

        result = verify_sshpilot_runtime_terminated(
            socket_path=tmp_path / "absent.sock",
            registry_path=registry_path,
            check_control_masters=False,
        )
        assert result.terminated is True
    finally:
        _reap(child)


def test_an_orphan_ignoring_sigterm_is_killed(tmp_path):
    registry_path = tmp_path / "processes.json"
    registry = OwnedProcessRegistry(registry_path)
    child = _sleeper(ignore_term=True)
    try:
        registry.register(child.pid, kind=KIND_SESSION)
        remaining = reap_orphaned_children(
            registry_path=registry_path, term_timeout=0.3
        )
        assert remaining == ()
        assert _wait_gone(child)
        assert child.returncode == -signal.SIGKILL
    finally:
        _reap(child)


def test_reaping_never_signals_a_recycled_pid(tmp_path):
    """Required: an unrelated process holding a recycled PID is untouched."""
    registry_path = tmp_path / "processes.json"
    bystander = _sleeper()
    try:
        # A registry entry naming the bystander's PID but a creation time
        # from long before it started: what a recycled PID looks like.
        import json

        registry_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "entries": [
                        {
                            "pid": bystander.pid,
                            "create_time": 1.0,
                            "kind": "session",
                            "label": "",
                            "process_group": False,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        reap_orphaned_children(registry_path=registry_path, term_timeout=0.2)

        time.sleep(0.3)
        assert bystander.poll() is None, (
            "a process that merely reuses a recorded PID must never be signalled"
        )
    finally:
        _reap(bystander)


def test_an_unrelated_ssh_process_is_never_touched(tmp_path):
    """Required: nothing outside the registry is ever signalled.

    Stands in for the user's own ``ssh`` session: it is not in the registry,
    so no amount of teardown may reach it. Ownership is the registry, never a
    process name.
    """
    registry_path = tmp_path / "processes.json"
    registry = OwnedProcessRegistry(registry_path)
    ours = _sleeper()
    theirs = _sleeper()
    try:
        registry.register(ours.pid, kind=KIND_SESSION)
        reap_orphaned_children(registry_path=registry_path, term_timeout=0.3)
        assert _wait_gone(ours)
        time.sleep(0.2)
        assert theirs.poll() is None, "an unowned process must survive teardown"
    finally:
        _reap(ours)
        _reap(theirs)


# ---------------------------------------------------------------------------
# ControlMaster ownership
# ---------------------------------------------------------------------------


def test_only_the_default_socket_owns_the_shared_master_namespace(tmp_path):
    """Required: an explicit --socket instance may not sweep shared masters."""
    from sshpilot.daemon.lifecycle import resolve_socket_path

    assert owns_default_control_master_namespace(resolve_socket_path()) is True
    assert owns_default_control_master_namespace(tmp_path / "other.sock") is False
    assert owns_default_control_master_namespace(None) is False


def test_verification_skips_masters_it_does_not_own(tmp_path, monkeypatch):
    """An explicit-socket caller must not even look at shared masters."""
    import sshpilot.daemon.runtime_verification as verification

    def _must_not_be_called(**_kwargs):
        raise AssertionError("masters were inspected without owning them")

    monkeypatch.setattr(
        verification, "list_owned_control_masters", _must_not_be_called
    )

    result = verify_sshpilot_runtime_terminated(
        socket_path=tmp_path / "explicit.sock",
        registry_path=tmp_path / "processes.json",
    )
    assert result.terminated is True

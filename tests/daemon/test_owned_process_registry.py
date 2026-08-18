"""The durable record of processes sshPilot owns.

A PID alone is not ownership. These tests pin the two properties the rest of
the shutdown machinery relies on: an entry only counts as ours while the
*same* process is behind the PID, and a registry that cannot be trusted
reports nothing rather than something.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sshpilot.daemon.process_registry import (
    KIND_SESSION,
    OwnedProcessRegistry,
    ProcessIdentity,
    RegistryUnreadable,
    identify_process,
    registry_path_for,
)


def _sleeper() -> subprocess.Popen:
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time\n"
            "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
            "time.sleep(60)\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "ready"
    return process


def _reap(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def test_registry_lives_beside_the_daemon_socket_and_is_per_socket():
    """Two daemons in one directory must not share one registry.

    The directory is per-user; daemons are per-socket. An explicit
    ``--socket`` instance sharing the default daemon's registry would read
    its children as its own and reap them.
    """
    default = registry_path_for("/run/user/1000/sshpilot/sshpilotd.sock")
    explicit = registry_path_for("/run/user/1000/sshpilot/dev.sock")

    assert default.parent == Path("/run/user/1000/sshpilot")
    assert default.name.startswith("sshpilotd-processes")
    assert default.name.endswith(".json")
    assert default != explicit, "each daemon endpoint needs its own registry"
    assert default.parent == explicit.parent
    # Deterministic: the same socket always resolves to the same registry.
    assert registry_path_for("/run/user/1000/sshpilot/sshpilotd.sock") == default


def test_two_registries_in_one_directory_stay_independent(tmp_path):
    """Required: teardown of one daemon must not touch the other's child."""
    first_socket = tmp_path / "sshpilotd.sock"
    second_socket = tmp_path / "dev.sock"
    first = OwnedProcessRegistry(registry_path_for(first_socket))
    second = OwnedProcessRegistry(registry_path_for(second_socket))

    mine, theirs = _sleeper(), _sleeper()
    try:
        first.register(mine.pid, kind=KIND_SESSION, label="mine")
        second.register(theirs.pid, kind=KIND_SESSION, label="theirs")

        assert [item.pid for item in first.entries()] == [mine.pid]
        assert [item.pid for item in second.entries()] == [theirs.pid]

        from sshpilot.daemon.runtime_verification import reap_orphaned_children

        reap_orphaned_children(socket_path=first_socket, term_timeout=0.3)

        deadline = time.time() + 5
        while mine.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        assert mine.poll() is not None, "the first daemon's child is reaped"
        assert theirs.poll() is None, "the second daemon's child is untouched"
        assert [item.pid for item in second.entries()] == [theirs.pid]
    finally:
        _reap(mine)
        _reap(theirs)


def test_identify_process_refuses_pid_0_and_1():
    """Neither can be a process sshPilot created; both are ruinous to signal."""
    assert identify_process(0) is None
    assert identify_process(1) is None
    assert identify_process(-5) is None


def test_a_registered_process_is_live_until_it_exits(tmp_path):
    registry = OwnedProcessRegistry(tmp_path / "processes.json")
    child = _sleeper()
    try:
        entry = registry.register(child.pid, kind=KIND_SESSION, label="demo")
        assert entry is not None
        assert entry.is_alive() is True
        assert [item.pid for item in registry.live_entries()] == [child.pid]
    finally:
        _reap(child)

    assert registry.live_entries() == ()
    assert registry.prune() == ()
    assert registry.entries() == ()


def test_a_recycled_pid_is_never_mistaken_for_the_registered_process(tmp_path):
    """The whole point of storing a creation time.

    A registry entry whose PID now belongs to a different process must read as
    gone. If it read as alive, shutdown would signal a stranger.
    """
    registry = OwnedProcessRegistry(tmp_path / "processes.json")
    child = _sleeper()
    try:
        entry = registry.register(child.pid, kind=KIND_SESSION)
        assert entry is not None
        # Same PID, but a creation time from before this process existed:
        # exactly what a recycled PID looks like.
        impostor = ProcessIdentity(pid=child.pid, create_time=1.0)
        assert impostor.matches_live_process() is False
        assert entry.is_alive() is True
    finally:
        _reap(child)


def test_registration_of_an_already_dead_process_is_refused(tmp_path):
    registry = OwnedProcessRegistry(tmp_path / "processes.json")
    child = _sleeper()
    pid = child.pid
    _reap(child)

    assert registry.register(pid, kind=KIND_SESSION) is None
    assert registry.entries() == ()


def test_unregister_drops_only_the_named_process(tmp_path):
    registry = OwnedProcessRegistry(tmp_path / "processes.json")
    first, second = _sleeper(), _sleeper()
    try:
        registry.register(first.pid, kind=KIND_SESSION)
        registry.register(second.pid, kind=KIND_SESSION)
        registry.unregister(first.pid)
        assert [item.pid for item in registry.entries()] == [second.pid]
    finally:
        _reap(first)
        _reap(second)


def test_an_unreadable_registry_refuses_to_answer(tmp_path):
    """Required: corrupt / unsupported / unusable must not read as empty.

    "Nothing is recorded" and "the record cannot be read" are opposites for
    shutdown: the first proves there is nothing to clean up, the second
    proves nothing at all. Returning ``()`` for both is how a quit falsely
    succeeds.
    """
    path = tmp_path / "processes.json"

    path.write_text("{ not json at all", encoding="utf-8")
    with pytest.raises(RegistryUnreadable):
        OwnedProcessRegistry(path).entries()

    path.write_text(json.dumps({"version": 999, "entries": []}), encoding="utf-8")
    with pytest.raises(RegistryUnreadable):
        OwnedProcessRegistry(path).entries()

    path.write_text(json.dumps({"version": 1, "entries": "nope"}), encoding="utf-8")
    with pytest.raises(RegistryUnreadable):
        OwnedProcessRegistry(path).entries()

    path.write_text(
        json.dumps({"version": 1, "entries": [{"pid": 1, "create_time": 1.0,
                                              "kind": "session"}]}),
        encoding="utf-8",
    )
    with pytest.raises(RegistryUnreadable):
        OwnedProcessRegistry(path).entries()


def test_an_unreadable_registry_file_raises_rather_than_reporting_empty(tmp_path):
    """Required: a permission failure is unknown state, not empty state."""
    path = tmp_path / "processes.json"
    path.write_text(json.dumps({"version": 1, "entries": []}), encoding="utf-8")
    path.chmod(0o000)
    if os.getuid() == 0:
        pytest.skip("root can read a 0000 file")
    try:
        with pytest.raises(RegistryUnreadable):
            OwnedProcessRegistry(path).entries()
    finally:
        path.chmod(0o600)


def test_a_missing_registry_is_provably_empty(tmp_path):
    """The one absence this design can prove: no file, no tracked child."""
    assert OwnedProcessRegistry(tmp_path / "absent.json").entries() == ()


def test_a_child_that_cannot_be_recorded_is_not_left_running(tmp_path):
    """Required: registry write failure during launch must not orphan a child.

    An untrackable child is exactly the orphan the registry exists to
    prevent, so the launch fails and the child is killed rather than
    silently downgrading the ownership guarantee.
    """
    from sshpilot.daemon.process_registry import (
        OwnershipNotRecorded,
        record_owned_process_or_abandon,
        set_active_registry,
    )

    unwritable = tmp_path / "readonly"
    unwritable.mkdir(mode=0o500)
    registry = OwnedProcessRegistry(unwritable / "processes.json")
    set_active_registry(registry)
    child = _sleeper()
    try:
        if os.getuid() == 0:
            pytest.skip("root can write to a read-only directory")
        with pytest.raises(OwnershipNotRecorded):
            record_owned_process_or_abandon(child, kind=KIND_SESSION)

        deadline = time.time() + 5
        while child.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        assert child.poll() is not None, "an unrecordable child must be killed"
    finally:
        set_active_registry(None)
        unwritable.chmod(0o700)
        _reap(child)


def test_the_registry_file_is_private(tmp_path):
    registry = OwnedProcessRegistry(tmp_path / "processes.json")
    child = _sleeper()
    try:
        registry.register(child.pid, kind=KIND_SESSION)
        assert registry.path.stat().st_mode & 0o077 == 0
    finally:
        _reap(child)


def test_registry_survives_the_process_that_wrote_it(tmp_path):
    """The point of writing it down: a killed daemon leaves it behind."""
    path = tmp_path / "processes.json"
    child = _sleeper()
    try:
        OwnedProcessRegistry(path).register(child.pid, kind=KIND_SESSION)
        # A brand-new reader, as the GUI would be after the daemon died.
        reopened = OwnedProcessRegistry(path)
        assert [item.pid for item in reopened.live_entries()] == [child.pid]
    finally:
        _reap(child)

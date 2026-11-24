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


def test_a_registry_lost_to_power_failure_reads_as_corrupt(tmp_path):
    """The bytes found in the field: a write the disk never received.

    A VM powered off after the rename but before the data was flushed left
    the registry as 235 zero bytes. That must still refuse to read as empty.
    """
    from sshpilot.daemon.process_registry import RegistryCorrupt

    path = tmp_path / "processes.json"
    path.write_bytes(b"\x00" * 235)
    with pytest.raises(RegistryCorrupt):
        OwnedProcessRegistry(path).entries()


def test_only_an_undecodable_registry_is_quarantined(tmp_path):
    """Another build's valid-looking record is kept; zeros are set aside."""
    path = tmp_path / "processes.json"
    path.write_text(json.dumps({"version": 999, "entries": []}), encoding="utf-8")
    assert OwnedProcessRegistry(path).quarantine_if_corrupt() is None
    assert path.exists()

    path.write_bytes(b"\x00" * 235)
    moved = OwnedProcessRegistry(path).quarantine_if_corrupt()
    assert moved == path.with_name(path.name + ".corrupt")
    assert moved.read_bytes() == b"\x00" * 235
    assert not path.exists()
    assert OwnedProcessRegistry(path).entries() == ()


def test_a_starting_daemon_recovers_from_a_corrupt_registry(tmp_path):
    """Without this, every quit was refused for good after one power cut."""
    from sshpilot.daemon import process_registry
    from sshpilot.daemon.server import DaemonServer

    socket_path = tmp_path / "sshpilotd.sock"
    path = registry_path_for(socket_path)
    path.write_bytes(b"\x00" * 235)

    class _Stub:
        pass

    stub = _Stub()
    stub.socket_path = socket_path
    try:
        DaemonServer._install_process_registry(stub)
        assert stub._process_registry is not None
        assert process_registry.active_registry() is stub._process_registry
        assert stub._process_registry.entries() == ()
        assert path.with_name(path.name + ".corrupt").exists()
    finally:
        process_registry.set_active_registry(None)


def test_registry_writes_reach_the_disk_before_the_rename(tmp_path, monkeypatch):
    """The rename is journaled, the data is not: fsync must come first."""
    calls = []
    real_fsync = os.fsync
    real_replace = os.replace

    def fsync(fd):
        calls.append("fsync")
        return real_fsync(fd)

    def replace(src, dst):
        calls.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    process = _sleeper()
    try:
        OwnedProcessRegistry(tmp_path / "processes.json").register(
            process.pid, kind=KIND_SESSION
        )
    finally:
        _reap(process)
    assert calls.index("fsync") < calls.index("replace")


# -- ending the owned child a helper descends from ----------------------------

_PARENT_OF_A_SLEEPER = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "sys.stdout.write(str(child.pid) + '\\n'); sys.stdout.flush()\n"
    "time.sleep(60)\n"
)


TOKEN = "prompt-token-1"


@pytest.fixture
def family(tmp_path):
    """An active registry, a parent process and the PID of its child."""
    from sshpilot.daemon import process_registry

    registry = OwnedProcessRegistry(tmp_path / "processes.json")
    previous = process_registry.active_registry()
    process_registry.set_active_registry(registry)
    parent = subprocess.Popen(
        [sys.executable, "-c", _PARENT_OF_A_SLEEPER], stdout=subprocess.PIPE, text=True,
        env={**os.environ, "SSHPILOT_DAEMON_ASKPASS_TOKEN": TOKEN},
    )
    child_pid = int(parent.stdout.readline())
    try:
        yield registry, parent, child_pid
    finally:
        process_registry.set_active_registry(previous)
        for pid in (child_pid, parent.pid):
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        parent.wait(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process signals")
def test_the_owned_parent_of_a_helper_is_ended(family):
    from sshpilot.daemon.process_registry import terminate_owned_ancestor

    registry, parent, child_pid = family
    registry.register(parent.pid, kind=KIND_SESSION)
    assert terminate_owned_ancestor(child_pid, token=TOKEN) == parent.pid
    assert parent.wait(timeout=5) != 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process signals")
def test_nothing_is_ended_when_no_ancestor_is_owned(family):
    from sshpilot.daemon.process_registry import terminate_owned_ancestor

    registry, parent, child_pid = family
    # This test process is an ancestor too, and is not in the registry.
    assert terminate_owned_ancestor(child_pid, token=TOKEN) is None
    assert terminate_owned_ancestor(1, token=TOKEN) is None
    assert terminate_owned_ancestor(0, token=TOKEN) is None
    assert parent.poll() is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process signals")
def test_a_recycled_pid_in_the_registry_is_not_ended(family):
    """The entry names the parent's PID but another process's start time."""
    from sshpilot.daemon.process_registry import terminate_owned_ancestor

    registry, parent, child_pid = family
    registry.register(parent.pid, kind=KIND_SESSION)
    entries = json.loads(registry.path.read_text())
    text = registry.path.read_text()
    real = identify_process(parent.pid).create_time
    assert repr(real) in text or str(real) in text, entries
    registry.path.write_text(text.replace(str(real), str(real - 1000.0)))
    assert terminate_owned_ancestor(child_pid, token=TOKEN) is None
    assert parent.poll() is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process signals")
def test_an_owned_ancestor_launched_for_another_prompt_is_not_ended(family):
    """Review P1: owning the process is not enough; it must carry the
    cancelled prompt's token."""
    from sshpilot.daemon.process_registry import terminate_owned_ancestor

    registry, parent, child_pid = family
    registry.register(parent.pid, kind=KIND_SESSION)
    assert terminate_owned_ancestor(child_pid, token="another-prompt") is None
    assert terminate_owned_ancestor(child_pid, token="") is None
    assert parent.poll() is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process signals")
def test_an_owned_ancestor_whose_token_cannot_be_read_is_not_ended(
    family, monkeypatch, caplog
):
    from sshpilot.daemon import process_registry
    from sshpilot.daemon.process_registry import terminate_owned_ancestor

    registry, parent, child_pid = family
    registry.register(parent.pid, kind=KIND_SESSION)
    monkeypatch.setattr(process_registry, "_environment_value", lambda _pid, _name: None)
    with caplog.at_level("WARNING", logger=process_registry.logger.name):
        assert terminate_owned_ancestor(child_pid, token=TOKEN) is None
    assert parent.poll() is None
    # The reason is on record: a Cancel that ends nothing is otherwise silent.
    assert "Cannot read the askpass token" in caplog.text

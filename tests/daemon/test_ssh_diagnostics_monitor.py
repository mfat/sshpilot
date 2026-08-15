"""Tests for the inotify-backed SSH diagnostic file monitor."""

from __future__ import annotations

import os
import time

import pytest

from sshpilot.daemon.ssh_diagnostics_monitor import (
    SshDiagnosticsMonitor,
    inotify_available,
)

pytestmark = pytest.mark.skipif(
    not inotify_available(),
    reason="inotify is unavailable on this platform",
)


@pytest.fixture()
def monitor():
    instance = SshDiagnosticsMonitor()
    yield instance
    instance.close()


@pytest.fixture()
def diagnostic_file(tmp_path):
    path = tmp_path / "session.log"
    path.write_bytes(b"")
    return str(path)


def _await(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_pre_existing_content_is_delivered_at_register(monitor, diagnostic_file):
    with open(diagnostic_file, "ab") as handle:
        handle.write(b'debug1: Authenticated to host ([::1]:22) using "publickey".\n')
    received = []
    monitor.register("session-a", diagnostic_file, received.append)
    assert _await(lambda: received != [])
    assert b"Authenticated to host" in received[0]


def test_appended_writes_are_delivered_incrementally(monitor, diagnostic_file):
    received = []
    monitor.register("session-a", diagnostic_file, received.append)
    time.sleep(0.1)
    with open(diagnostic_file, "ab") as handle:
        handle.write(b"debug1: Connecting to host port 22.\n")
    assert _await(lambda: len(received) == 1)
    with open(diagnostic_file, "ab") as handle:
        handle.write(b"debug1: Next authentication method: password\n")
    assert _await(lambda: len(received) == 2)
    assert received[1].startswith(b"debug1: Next authentication")


def test_coalesced_burst_writes_are_not_lost(monitor, diagnostic_file):
    received = []
    monitor.register("session-a", diagnostic_file, received.append)
    time.sleep(0.1)
    payload = b"".join(
        b"debug1: line %d\n" % index for index in range(200)
    )
    with open(diagnostic_file, "ab") as handle:
        handle.write(payload)
    assert _await(lambda: b"".join(received) == payload)


def test_unregister_drains_pending_writes_to_eof(monitor, diagnostic_file):
    received = []
    monitor.register("session-a", diagnostic_file, received.append)
    time.sleep(0.1)
    with open(diagnostic_file, "ab") as handle:
        handle.write(b"ssh: connect to host x port 22: Connection refused\n")
    # Race the final write against unregister: the drain must deliver it.
    monitor.unregister("session-a")
    assert _await(lambda: b"".join(received).endswith(b"Connection refused\n"))


def test_unregister_unlinks_verified_regular_file(monitor, diagnostic_file):
    monitor.register("session-a", diagnostic_file, lambda _data: None)
    time.sleep(0.1)
    monitor.unregister("session-a", unlink_path=diagnostic_file)
    assert _await(lambda: not os.path.exists(diagnostic_file))


def test_unregister_refuses_to_unlink_a_swapped_symlink(
    monitor, diagnostic_file, tmp_path
):
    monitor.register("session-a", diagnostic_file, lambda _data: None)
    time.sleep(0.1)
    os.unlink(diagnostic_file)
    target = tmp_path / "target.log"
    target.write_text("precious data")
    os.symlink(str(target), diagnostic_file)
    monitor.unregister("session-a", unlink_path=diagnostic_file)
    assert _await(lambda: os.path.islink(diagnostic_file))
    assert target.read_text() == "precious data"


def test_multiple_sessions_are_independent(monitor, diagnostic_file, tmp_path):
    second = tmp_path / "second.log"
    second.write_bytes(b"")
    first_chunks = []
    second_chunks = []
    monitor.register("session-a", diagnostic_file, first_chunks.append)
    monitor.register("session-b", str(second), second_chunks.append)
    time.sleep(0.1)
    with open(diagnostic_file, "ab") as handle:
        handle.write(b"A\n")
    with open(second, "ab") as handle:
        handle.write(b"B\n")
    assert _await(lambda: first_chunks == [b"A\n"] and second_chunks == [b"B\n"])
    monitor.unregister("session-a")
    with open(second, "ab") as handle:
        handle.write(b"B2\n")
    assert _await(lambda: second_chunks == [b"B\n", b"B2\n"])
    monitor.unregister("session-b")


def test_file_removed_externally_is_tolerated(monitor, diagnostic_file):
    received = []
    monitor.register("session-a", diagnostic_file, received.append)
    time.sleep(0.1)
    os.unlink(diagnostic_file)
    # Removing the file unregisters the watch; a later explicit unregister is
    # a safe no-op and must not raise.
    monitor.unregister("session-a", unlink_path=diagnostic_file)
    time.sleep(0.2)
    assert not os.path.exists(diagnostic_file)


def test_register_reports_armed_with_bool(monitor, diagnostic_file):
    assert monitor.register("session-ok", diagnostic_file, lambda _data: None) is True
    monitor.unregister("session-ok")


def test_register_reports_failure_when_watch_cannot_be_added(
    monitor, diagnostic_file, monkeypatch
):
    class _BrokenLibc:
        def inotify_add_watch(self, *args):
            return -1

    monkeypatch.setattr(
        "sshpilot.daemon.ssh_diagnostics_monitor._INOTIFY_LIBC",
        _BrokenLibc(),
    )
    assert monitor.register("session-bad", diagnostic_file, lambda _data: None) is False


def test_register_reports_failure_when_command_submission_fails(
    monitor, diagnostic_file, monkeypatch
):
    def _boom(*args, **kwargs):
        raise RuntimeError("diagnostics monitor is closed")

    monkeypatch.setattr(monitor, "_submit", _boom)
    assert monitor.register("session-bad", diagnostic_file, lambda _data: None) is False

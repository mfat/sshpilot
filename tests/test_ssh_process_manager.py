"""Characterization tests for SSHProcessManager.

The manager is GTK-free SSH-process lifecycle logic. These pin its current
behavior (singleton, terminal registration, process-group termination, orphan
skipping, cleanup) before it is relocated out of terminal.py. Imported from
sshpilot.terminal, which is its current (and, after the move, re-exporting) home.
"""

import os
import signal
import types
from datetime import datetime, timedelta

import pytest

terminal = pytest.importorskip("sshpilot.terminal")
SSHProcessManager = terminal.SSHProcessManager


class FakeTerminal:
    """Weakref-able terminal stand-in (SimpleNamespace can't be weak-referenced)."""

    def __init__(self, *, process_pid=None, is_connected=False):
        self.process_pid = process_pid
        self.is_connected = is_connected
        self._is_quitting = False
        self.disconnected = False

    def disconnect(self):
        self.disconnected = True


@pytest.fixture
def pm():
    """The singleton, with its mutable state reset for isolation."""
    m = SSHProcessManager()
    with m.lock:
        m.processes.clear()
    m.terminals.clear()
    return m


def test_singleton_identity(pm):
    assert SSHProcessManager() is pm
    assert terminal.process_manager is pm


def test_register_terminal_tracks_it(pm):
    t = FakeTerminal()
    pm.register_terminal(t)
    assert t in pm.terminals


def test_terminate_by_pid_signals_process_group(pm, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "getpgid", lambda pid: 4242)
    def fake_killpg(pgid, sig):
        calls.append((pgid, sig))
        if sig == 0:
            raise ProcessLookupError  # process gone after SIGTERM
    monkeypatch.setattr(os, "killpg", fake_killpg)
    assert pm._terminate_process_by_pid(1234) is True
    assert (4242, signal.SIGTERM) in calls
    # process reported gone, so no SIGKILL escalation
    assert (4242, signal.SIGKILL) not in calls


def test_terminate_by_pid_escalates_to_sigkill(pm, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "getpgid", lambda pid: 7)
    monkeypatch.setattr(terminal.time, "sleep", lambda *_: None)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))
    assert pm._terminate_process_by_pid(1234) is True
    # stayed alive through the poll loop -> force kill
    assert (7, signal.SIGKILL) in calls


def test_terminate_by_pid_returns_false_on_error(pm, monkeypatch):
    monkeypatch.setattr(os, "getpgid", lambda pid: (_ for _ in ()).throw(OSError()))
    assert pm._terminate_process_by_pid(999) is False


def test_cleanup_orphaned_skips_recent_processes(pm, monkeypatch):
    killed = []
    monkeypatch.setattr(pm, "_terminate_process_by_pid", lambda pid: killed.append(pid))
    # A recent, untracked process (no terminal owns it) must NOT be killed.
    with pm.lock:
        pm.processes[555] = {"start_time": datetime.now(), "command": "x"}
    pm._cleanup_orphaned_processes()
    assert killed == []
    assert 555 in pm.processes  # left tracked


def test_cleanup_orphaned_removes_dead_old_process(pm, monkeypatch):
    killed = []
    monkeypatch.setattr(pm, "_terminate_process_by_pid", lambda pid: killed.append(pid))
    monkeypatch.setattr(os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    with pm.lock:
        pm.processes[556] = {"start_time": datetime.now() - timedelta(hours=1)}
    pm._cleanup_orphaned_processes()
    # old + already dead -> dropped from tracking, not "killed" again
    assert 556 not in pm.processes
    assert killed == []


def test_cleanup_all_clears_state(pm, monkeypatch):
    monkeypatch.setattr(signal, "signal", lambda *a, **k: None)
    monkeypatch.setattr(signal, "alarm", lambda *a, **k: 0)
    term = FakeTerminal()
    pm.register_terminal(term)
    with pm.lock:
        pm.processes[1] = {"command": "c"}
    monkeypatch.setattr(pm, "_terminate_process_by_pid", lambda pid: None)
    pm.cleanup_all()
    assert term._is_quitting is True
    assert dict(pm.processes) == {}
    assert len(pm.terminals) == 0

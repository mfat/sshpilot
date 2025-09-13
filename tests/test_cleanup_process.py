import sys
import types
import signal

import pytest


def test_cleanup_process_sends_sigterm_to_pgid(monkeypatch):
    sys.modules.pop("sshpilot.terminal", None)

    gi = types.ModuleType("gi")
    gi.require_version = lambda *a, **k: None
    repo = types.SimpleNamespace()
    repo.Gtk = types.SimpleNamespace(Box=type("Box", (), {}))
    repo.GObject = types.SimpleNamespace(
        SignalFlags=types.SimpleNamespace(RUN_FIRST=0)
    )
    repo.GLib = types.SimpleNamespace()
    repo.Vte = types.SimpleNamespace()
    repo.Pango = types.SimpleNamespace()
    repo.Gdk = types.SimpleNamespace()
    repo.Gio = types.SimpleNamespace()
    repo.Adw = types.SimpleNamespace()
    gi.repository = repo
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repo)
    for name in [
        "Gtk",
        "GObject",
        "GLib",
        "Vte",
        "Pango",
        "Gdk",
        "Gio",
        "Adw",
    ]:
        monkeypatch.setitem(sys.modules, f"gi.repository.{name}", getattr(repo, name))

    from sshpilot import terminal as terminal_mod

    pid = 1234
    pgid = 5678
    terminal_mod.process_manager.processes[pid] = {"pgid": pgid}

    calls = []

    def fake_kill(target_pid, sig):
        calls.append(("kill", target_pid, sig))
        if sig == 0:
            raise ProcessLookupError

    def fake_killpg(target_pgid, sig):
        calls.append(("killpg", target_pgid, sig))

    monkeypatch.setattr(terminal_mod.os, "kill", fake_kill)
    monkeypatch.setattr(terminal_mod.os, "killpg", fake_killpg)
    monkeypatch.setattr(terminal_mod.time, "sleep", lambda x: None)

    terminal_mod.TerminalWidget._cleanup_process(None, pid)

    assert ("killpg", pgid, signal.SIGTERM) in calls
    assert ("kill", pid, signal.SIGTERM) in calls

    terminal_mod.process_manager.processes.pop(pid, None)

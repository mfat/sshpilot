"""Real GTK: the built-in backend runs a local shell on its own PTY."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

from tests._gui_harness import requires_gui

Gtk, _Adw, _Gio, GLib = requires_gui()

from sshpilot.terminal_backend_pyte import PyteTerminalBackend  # noqa: E402

pytestmark = pytest.mark.gui


def _pump_until(predicate, seconds=20):
    context = GLib.MainContext.default()
    deadline = GLib.get_monotonic_time() + int(seconds * 1_000_000)
    while not predicate() and GLib.get_monotonic_time() < deadline:
        context.iteration(False)
    return predicate()


def _shell():
    if sys.platform == "win32":
        return [os.environ.get("ComSpec") or "cmd.exe", "/d", "/q", "/k"], b"echo typed-%OS%\r", "typed-Windows_NT"
    return ["/bin/sh"], b"echo typed-$((40+2))\n", "typed-42"


@pytest.mark.parametrize("model", ["pyte", "vterm"])
def test_local_shell_round_trip(model, monkeypatch):
    monkeypatch.setenv("SSHPILOT_TERMINAL_MODEL", model)
    backend = PyteTerminalBackend(SimpleNamespace(config=None))
    view = backend.view
    started, exited = [], []
    backend.connect_child_exited(lambda _view, status: exited.append(status))
    argv, line, expected = _shell()

    backend.spawn_async(
        argv=argv,
        env=dict(os.environ),
        cwd=os.path.expanduser("~"),
        callback=lambda _terminal, pid, error, _data: started.append((pid, error)),
    )
    assert _pump_until(lambda: started)
    pid, error = started[0]
    assert error is None and pid > 0
    assert backend.get_child_pid() == pid and backend.local_shell_running()

    # Typed input goes to the shell, its output to the view.
    view.emit("commit", line, len(line))
    assert _pump_until(lambda: expected in view.model.all_text()), view.model.all_text()

    exit_line = b"exit\r" if sys.platform == "win32" else b"exit\n"
    view.emit("commit", exit_line, len(exit_line))
    assert _pump_until(lambda: exited)
    assert not backend.local_shell_running()
    backend.destroy()


def test_spawn_failure_reaches_the_callback(monkeypatch):
    backend = PyteTerminalBackend(SimpleNamespace(config=None))
    results = []
    backend.spawn_async(
        argv=["sshpilot-no-such-shell-xyz"],
        env=dict(os.environ),
        callback=lambda _terminal, pid, error, _data: results.append((pid, error)),
    )
    assert _pump_until(lambda: results, seconds=10)
    pid, error = results[0]
    # POSIX forks first and the exec fails in the child (exit 127 follows);
    # Windows fails CreateProcess and reports the error directly.
    assert error is not None or pid > 0
    backend.destroy()

"""VTE backend owns PTY creation, sizing, attachment, and spawning."""

import types

import pytest

pytest.importorskip("gi")

from sshpilot import terminal_backends
from sshpilot.terminal_backends import VTETerminalBackend


class DummyPty:
    def __init__(self, calls):
        self.calls = calls
        self.size = None

    def set_size(self, rows, columns):
        self.size = (rows, columns)
        self.calls.append(("size", rows, columns))


class DummyTerminal:
    def __init__(self, rows, columns, calls):
        self.rows, self.columns, self.calls = rows, columns, calls
        self.pty = None

    def get_pty(self):
        return self.pty

    def get_row_count(self):
        return self.rows

    def get_column_count(self):
        return self.columns

    def set_pty(self, pty):
        self.pty = pty
        self.calls.append(("attach", pty))

    def spawn_async(self, *args):
        self.calls.append(("spawn", args))


def _backend(monkeypatch, rows=30, columns=100):
    calls = []
    terminal = DummyTerminal(rows, columns, calls)
    ptys = []

    def new_sync(_flags):
        pty = DummyPty(calls)
        ptys.append(pty)
        calls.append(("create", pty))
        return pty

    fake_vte = types.SimpleNamespace(
        Pty=types.SimpleNamespace(new_sync=new_sync),
        PtyFlags=lambda value: value,
    )
    fake_vte.PtyFlags.DEFAULT = 0
    monkeypatch.setattr(terminal_backends, "Vte", fake_vte)
    backend = object.__new__(VTETerminalBackend)
    backend.vte = terminal
    return backend, terminal, ptys, calls


def test_backend_sets_pty_size_before_spawn(monkeypatch):
    backend, terminal, ptys, calls = _backend(monkeypatch)

    backend.spawn_async(["ssh", "host"])

    assert ptys[0].size == (30, 100)
    assert terminal.pty is ptys[0]
    assert [call[0] for call in calls] == ["create", "size", "attach", "spawn"]


def test_backend_does_not_resize_default_dimensions(monkeypatch):
    backend, terminal, ptys, calls = _backend(monkeypatch, 24, 80)

    backend.spawn_async(["/bin/sh"])

    assert ptys[0].size is None
    assert [call[0] for call in calls] == ["create", "attach", "spawn"]


def test_backend_reuses_attached_pty(monkeypatch):
    backend, terminal, ptys, calls = _backend(monkeypatch)
    existing = DummyPty(calls)
    terminal.pty = existing

    backend.spawn_async(["/bin/sh"])

    assert ptys == []
    assert [call[0] for call in calls] == ["spawn"]

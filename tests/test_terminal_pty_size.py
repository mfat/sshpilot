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

    def spawn_async(self, *args, **kwargs):
        self.calls.append(("spawn", args, kwargs))


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


def test_backend_delegates_pty_creation_and_sizing_to_vte(monkeypatch):
    backend, terminal, ptys, calls = _backend(monkeypatch)

    backend.spawn_async(["ssh", "host"])

    assert ptys == []
    assert calls[0][0] == "spawn"
    assert calls[0][2]["argv"] == ["ssh", "host"]


def test_backend_delegates_default_dimensions_to_vte(monkeypatch):
    backend, terminal, ptys, calls = _backend(monkeypatch, 24, 80)

    backend.spawn_async(["/bin/sh"])

    assert ptys == []
    assert calls[0][0] == "spawn"
    assert calls[0][2]["argv"] == ["/bin/sh"]


def test_backend_reuses_attached_pty(monkeypatch):
    backend, terminal, ptys, calls = _backend(monkeypatch)
    existing = DummyPty(calls)
    terminal.pty = existing

    backend.spawn_async(["/bin/sh"])

    assert ptys == []
    assert [call[0] for call in calls] == ["spawn"]


class DummySignalEmitter:
    """Minimal GObject.connect()/disconnect() stand-in that also lets a test
    fire a named signal to every connected handler."""

    def __init__(self):
        self.handlers = {}
        self._next_id = 1

    def connect(self, signal, callback):
        handler_id = self._next_id
        self._next_id += 1
        self.handlers[handler_id] = (signal, callback)
        return handler_id

    def disconnect(self, handler_id):
        self.handlers.pop(handler_id, None)

    def emit_signal(self, signal, *args):
        for sig, callback in list(self.handlers.values()):
            if sig == signal:
                callback(self, *args)


def test_connect_size_changed_uses_column_row_count_not_char_size():
    """Regression (GH #1164): VTE's "char-size-changed" only fires on
    font-metric changes (zoom) — never on a widget resize. Wiring daemon
    terminal resize to it left the remote PTY (and anything reading it,
    e.g. tmux) stuck at whatever size the session opened with, no matter
    how big the window/pane grew afterwards. Must connect to
    notify::column-count / notify::row-count, which VTE actually
    recomputes when the widget is resized."""
    backend = object.__new__(VTETerminalBackend)
    backend.vte = DummySignalEmitter()

    calls = []
    handler = backend.connect_size_changed(lambda *args: calls.append(args))

    connected_signals = {signal for signal, _cb in backend.vte.handlers.values()}
    assert connected_signals == {"notify::column-count", "notify::row-count"}
    assert "char-size-changed" not in connected_signals

    backend.vte.emit_signal("notify::column-count", object())
    backend.vte.emit_signal("notify::row-count", object())
    assert len(calls) == 2

    backend.disconnect(handler)
    assert backend.vte.handlers == {}

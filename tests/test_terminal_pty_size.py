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


class DummyGridEmitter:
    """Minimal GridTrackingVteTerminal signal stand-in."""

    def __init__(self):
        self.rows = 24
        self.columns = 80
        self._callbacks = {}
        self._next_id = 1
        self.invalidations = 0

    def connect(self, signal_name, callback):
        assert signal_name == "grid-size-changed"
        handler_id = self._next_id
        self._next_id += 1
        self._callbacks[handler_id] = callback
        return handler_id

    def disconnect(self, handler_id):
        self._callbacks.pop(handler_id, None)

    def get_row_count(self):
        return self.rows

    def get_column_count(self):
        return self.columns

    def emit_grid_changed(self):
        for callback in tuple(self._callbacks.values()):
            callback(self, self.columns, self.rows)

    def invalidate_grid_size(self):
        self.invalidations += 1


def test_connect_size_changed_uses_grid_tracking_signal():
    backend = object.__new__(VTETerminalBackend)
    backend.vte = DummyGridEmitter()

    calls = []
    handler = backend.connect_size_changed(lambda *args: calls.append(args))
    assert backend.vte.invalidations == 1

    backend.vte.emit_grid_changed()
    assert len(calls) == 1
    backend.vte.rows, backend.vte.columns = 50, 200
    backend.vte.emit_grid_changed()
    assert len(calls) == 2

    backend.disconnect(handler)
    assert backend.vte._callbacks == {}


def test_invalidate_size_tracking_queues_grid_redelivery():
    backend = object.__new__(VTETerminalBackend)
    backend.vte = DummyGridEmitter()

    backend.invalidate_size_tracking()
    assert backend.vte.invalidations == 1

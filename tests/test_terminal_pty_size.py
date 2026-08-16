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


class DummyTickEmitter:
    """Minimal Gtk.Widget.add_tick_callback()/remove_tick_callback() stand-in.

    ``fire_tick()`` drives the registered callback the way GTK would once
    per rendered frame; the test controls row/column counts directly.
    """

    def __init__(self):
        self.rows = 24
        self.columns = 80
        self._callback = None
        self._next_id = 1
        self._id = None

    def add_tick_callback(self, callback):
        self._callback = callback
        self._id = self._next_id
        self._next_id += 1
        return self._id

    def remove_tick_callback(self, tick_id):
        if tick_id == self._id:
            self._callback = None
            self._id = None

    def get_row_count(self):
        return self.rows

    def get_column_count(self):
        return self.columns

    def fire_tick(self):
        if self._callback is not None:
            self._callback(self, None)


def test_connect_size_changed_polls_via_tick_callback_not_char_size():
    """Regression (GH #1164): VTE's "char-size-changed" only fires on
    font-metric changes (zoom) — never on a widget resize (confirmed by
    VTE's own docs). A first fix tried notify::column-count/row-count
    instead, but those aren't real GObject properties on Vte.Terminal
    (confirmed via GObject.list_properties) — that notify never fires
    either. GTK4 also removed the public size-allocate signal entirely.
    Polling once per rendered frame via add_tick_callback is the only
    mechanism GTK4 actually offers for observing VTE's real grid size, and
    is what's left to notice the remote PTY (and anything reading it, e.g.
    tmux) needs a fresh size — the callback must fire only on an actual
    change, not every tick."""
    backend = object.__new__(VTETerminalBackend)
    backend.vte = DummyTickEmitter()

    calls = []
    handler = backend.connect_size_changed(lambda *args: calls.append(args))

    assert backend.vte._callback is not None

    # No change yet: first tick only establishes the baseline, no callback.
    backend.vte.fire_tick()
    assert calls == []

    # Same size again: still no spurious callback.
    backend.vte.fire_tick()
    assert calls == []

    # Widget actually resized: exactly one callback fires.
    backend.vte.rows, backend.vte.columns = 50, 200
    backend.vte.fire_tick()
    assert len(calls) == 1

    # Settled at the new size: no further callback until it changes again.
    backend.vte.fire_tick()
    assert len(calls) == 1

    backend.disconnect(handler)
    assert backend.vte._callback is None

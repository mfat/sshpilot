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
    mechanism GTK4 actually offers for observing VTE's real grid size."""
    backend = object.__new__(VTETerminalBackend)
    backend.vte = DummyTickEmitter()

    calls = []
    handler = backend.connect_size_changed(lambda *args: calls.append(args))

    assert backend.vte._callback is not None

    # The first observed size fires too (see the dedicated test below for
    # why) — establish it, then confirm a repeat tick at the same size is
    # not a spurious duplicate.
    backend.vte.fire_tick()
    assert len(calls) == 1
    backend.vte.fire_tick()
    assert len(calls) == 1

    # Widget actually resized: exactly one more callback fires.
    backend.vte.rows, backend.vte.columns = 50, 200
    backend.vte.fire_tick()
    assert len(calls) == 2

    # Settled at the new size: no further callback until it changes again.
    backend.vte.fire_tick()
    assert len(calls) == 2

    backend.disconnect(handler)
    assert backend.vte._callback is None


def test_connect_size_changed_fires_on_the_first_tick_too():
    """Regression: an earlier version of this fix treated tick #1 as "just
    establishing a baseline" and never fired on it — implicitly assuming
    whatever size was read synchronously at session-open time (before
    layout/allocation may have settled on a fresh tab) still matched
    reality by the time polling started. When layout was still one or more
    frames behind session-open, the daemon was told a size the widget had
    already outgrown, nothing ever detected a "change" from that wrong
    baseline (the widget's size doesn't drift on its own), and a fullscreen
    program (top, tmux) rendered into the stale corner until the user
    manually resized the window — the only thing that produced a real
    before/after delta for the old logic to notice. The very first tick
    must report whatever size it observes, unconditionally."""
    backend = object.__new__(VTETerminalBackend)
    backend.vte = DummyTickEmitter()
    backend.vte.rows, backend.vte.columns = 36, 99  # already its "final" size

    calls = []
    backend.connect_size_changed(lambda *args: calls.append(args))

    backend.vte.fire_tick()

    assert len(calls) == 1


def test_invalidate_size_tracking_forces_redelivery_on_the_next_tick():
    """Regression: a caller (terminal.py) may observe a resize through this
    poll and have to drop it for an unrelated reason (daemon input
    ownership not granted yet). If the grid doesn't change again
    afterwards, plain change-detection never re-reports it — nothing but a
    genuine further resize produces a new delta, which on GH #1164 meant
    tmux/top stuck at the stale size until the user manually resized the
    window. invalidate_size_tracking() lets the caller force one more
    unconditional report on the very next tick once it's ready to act on
    it (e.g. right when ownership is granted), regardless of whether the
    grid itself moves again."""
    backend = object.__new__(VTETerminalBackend)
    backend.vte = DummyTickEmitter()
    backend.vte.rows, backend.vte.columns = 50, 200

    calls = []
    backend.connect_size_changed(lambda *args: calls.append(args))
    backend.vte.fire_tick()
    assert len(calls) == 1

    # Grid stays put (already settled) — no further change to detect.
    backend.vte.fire_tick()
    assert len(calls) == 1

    backend.invalidate_size_tracking()
    backend.vte.fire_tick()
    assert len(calls) == 2

    # Settles again afterwards: normal change-detection still applies.
    backend.vte.fire_tick()
    assert len(calls) == 2

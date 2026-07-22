"""Header double-click peek: a temporary full<->minimal flip that reverts.

Exercises the state machine in isolation — set_sidebar_minimal and GLib timers
are stubbed so no GTK window/main loop is needed.
"""
from types import SimpleNamespace, MethodType

import sshpilot.window as window
from sshpilot.window import MainWindow


def _fake_window(monkeypatch):
    timers = {"next": 1, "live": {}}

    def timeout_add(_ms, cb):
        tid = timers["next"]
        timers["next"] += 1
        timers["live"][tid] = cb
        return tid

    def source_remove(tid):
        timers["live"].pop(tid, None)

    monkeypatch.setattr(window.GLib, "timeout_add", timeout_add)
    monkeypatch.setattr(window.GLib, "source_remove", source_remove)

    w = SimpleNamespace(_sidebar_minimal=False)

    def set_sidebar_minimal(self, minimal, animate=True):
        minimal = bool(minimal)
        if minimal == self._sidebar_minimal:
            return
        self._sidebar_minimal = minimal
        if not getattr(self, "_applying_sidebar_peek", False):
            self._cancel_sidebar_peek()

    for name in ("_toggle_sidebar_minimal_peek", "_on_sidebar_peek_timeout",
                 "_cancel_sidebar_peek"):
        setattr(w, name, MethodType(getattr(MainWindow, name), w))
    w.set_sidebar_minimal = MethodType(set_sidebar_minimal, w)
    return w, timers


def test_peek_from_minimal_then_reverts(monkeypatch):
    w, timers = _fake_window(monkeypatch)
    w._sidebar_minimal = True                   # start on the minimal strip
    w._toggle_sidebar_minimal_peek()
    assert w._sidebar_minimal is False          # peeked to full
    assert w._sidebar_peek_source in timers["live"]

    timers["live"][w._sidebar_peek_source]()   # fire the timeout
    assert w._sidebar_minimal is True           # reverted to minimal
    assert w._sidebar_peek_source is None


def test_no_peek_when_full(monkeypatch):
    w, _ = _fake_window(monkeypatch)             # starts full
    w._toggle_sidebar_minimal_peek()
    assert w._sidebar_minimal is False           # unchanged
    assert getattr(w, '_sidebar_peek_source', None) is None


def test_external_mode_change_cancels_pending_revert(monkeypatch):
    w, _ = _fake_window(monkeypatch)
    w._sidebar_minimal = True
    w._toggle_sidebar_minimal_peek()            # minimal -> full, revert armed
    assert w._sidebar_peek_source is not None
    w.set_sidebar_minimal(True)                 # user picks minimal elsewhere
    assert w._sidebar_peek_source is None        # revert-to-minimal can't clobber

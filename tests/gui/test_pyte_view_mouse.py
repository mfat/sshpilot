"""Real GTK coverage for the built-in terminal view's mouse reports."""

from __future__ import annotations

import pytest

from tests._gui_harness import requires_gui

Gtk, _Adw, _Gio, _GLib = requires_gui()

from gi.repository import Gdk  # noqa: E402

from sshpilot.terminal_backend_pyte import _PAD_X, _PAD_Y, PyteTerminalView  # noqa: E402

pytestmark = pytest.mark.gui


class _Click:
    """Stand-in for the click gesture: claiming it cancels the drag gesture."""

    def __init__(self, on_claim) -> None:
        self._on_claim = on_claim

    def get_current_button(self):
        return Gdk.BUTTON_PRIMARY

    def get_current_event_state(self):
        return Gdk.ModifierType(0)

    def set_state(self, state):
        if state == Gtk.EventSequenceState.CLAIMED:
            self._on_claim()


class _CancelledDrag:
    """A drag gesture GTK cancelled: it has no start point any more."""

    def get_start_point(self):
        return False, 0.0, 0.0

    def get_current_event_state(self):
        return Gdk.ModifierType(0)


@pytest.mark.parametrize("model", ["pyte", "vterm"])
def test_left_click_reports_press_and_release(model, monkeypatch):
    # GTK runs the drag gesture's "drag-end" while the click gesture claims
    # the press, before the button is up; that must not swallow the release
    # (htop acts on button-1 release).
    monkeypatch.setenv("SSHPILOT_TERMINAL_MODEL", model)
    view = PyteTerminalView()
    view.feed(b"\x1b[?1000h\x1b[?1006h")
    sent = []
    monkeypatch.setattr(view, "_send", sent.append)
    click = _Click(lambda: view._on_drag_end(_CancelledDrag(), 0.0, 0.0))
    # Widget coordinates: the text grid starts inside the view's padding.
    x, y = _PAD_X + view._cell_w * 4.5, _PAD_Y + view._cell_h * 2.5

    view._on_click_pressed(click, 1, x, y)
    view._on_click_released(click, 1, x, y)

    assert sent == [b"\x1b[<0;5;3M", b"\x1b[<0;5;3m"]

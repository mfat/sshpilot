"""Real GTK coverage for the built-in terminal view's scrollback direction."""

from __future__ import annotations

import pytest

from tests._gui_harness import requires_gui

Gtk, _Adw, _Gio, GLib = requires_gui()

from gi.repository import Gdk  # noqa: E402

from sshpilot.terminal_backend_pyte import PyteTerminalView  # noqa: E402

pytestmark = pytest.mark.gui


class _Event:
    """Stand-in for the scroll controller: only the modifier state is read."""

    def get_current_event_state(self):
        return Gdk.ModifierType(0)


@pytest.mark.parametrize("model", ["pyte", "vterm"])
def test_wheel_and_shift_page_keys_scroll_the_right_way(model, monkeypatch):
    monkeypatch.setenv("SSHPILOT_TERMINAL_MODEL", model)
    view = PyteTerminalView()
    view.model.resize(40, 10)
    view.feed(b"".join(b"%d\r\n" % n for n in range(1, 101)))
    assert view.model.history_length > 20

    view._on_scroll(_Event(), 0, -1)  # wheel up: back into the scrollback
    up = view.scroll_offset
    assert up > 0
    view._on_scroll(_Event(), 0, 1)  # wheel down: forward again
    assert view.scroll_offset < up

    view.scroll_offset = 0
    page_up = Gdk.keyval_from_name("Page_Up")
    page_down = Gdk.keyval_from_name("Page_Down")
    view._on_key_pressed(None, page_up, 0, Gdk.ModifierType.SHIFT_MASK)
    assert view.scroll_offset == view.model.lines - 1
    view._on_key_pressed(None, page_down, 0, Gdk.ModifierType.SHIFT_MASK)
    assert view.scroll_offset == 0


@pytest.mark.parametrize("platform, pulls", [("win32", False), ("linux", True), ("darwin", True)])
@pytest.mark.parametrize("model", ["pyte", "vterm"])
def test_daemon_sessions_pull_history_on_grow_only_without_conpty(model, platform, pulls, monkeypatch):
    from sshpilot import terminal_backend_pyte

    monkeypatch.setenv("SSHPILOT_TERMINAL_MODEL", model)
    backend = terminal_backend_pyte.PyteTerminalBackend(object())
    assert backend.view.model.pull_history_on_grow
    monkeypatch.setattr(terminal_backend_pyte.sys, "platform", platform)
    backend.prepare_pty_less_emulation()
    assert backend.view.model.pull_history_on_grow is pulls

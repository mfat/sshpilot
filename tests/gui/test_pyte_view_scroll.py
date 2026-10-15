"""Real GTK coverage for the built-in terminal view's scrollback direction."""

from __future__ import annotations

import pytest

from tests._gui_harness import requires_gui

Gtk, _Adw, _Gio, GLib = requires_gui()

from gi.repository import Gdk  # noqa: E402

from sshpilot.terminal_backend_pyte import _PAD_X, _PAD_Y, PyteTerminalView  # noqa: E402

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


@pytest.mark.parametrize("model", ["pyte", "vterm"])
def test_synchronized_update_holds_the_repaint(model, monkeypatch):
    monkeypatch.setenv("SSHPILOT_TERMINAL_MODEL", model)
    view = PyteTerminalView()
    draws = []
    monkeypatch.setattr(view, "queue_draw", lambda: draws.append(1))
    sent = []
    monkeypatch.setattr(view, "_send", sent.append)

    view.feed(b"\x1b[?2026h" + b"half-drawn")
    assert draws == [] and view._sync_timer
    view.feed(b"\x1b[?2026$p")
    assert sent == [b"\x1b[?2026;1$y"]
    view.feed(b" done\x1b[?2026l")
    assert draws == [1] and not view._sync_timer
    assert "half-drawn done" in view.model.row_text(view.model.history_length)

    # An update that never ends is painted after the timeout.
    view.feed(b"\x1b[?2026hstuck")
    context = GLib.MainContext.default()
    deadline = GLib.get_monotonic_time() + 2_000_000
    while len(draws) < 2 and GLib.get_monotonic_time() < deadline:
        context.iteration(False)
    assert len(draws) == 2


@pytest.mark.parametrize("model", ["pyte", "vterm"])
def test_console_host_title_is_not_shown(model, monkeypatch):
    monkeypatch.setenv("SSHPILOT_TERMINAL_MODEL", model)
    view = PyteTerminalView()
    titles = []
    view.connect("window-title-changed", lambda v: titles.append(v.get_window_title()))
    view.feed(b"\x1b]0;" + rb"C:\Windows\System32\OpenSSH\ssh.exe" + b"\x07")
    assert titles == [] and view.get_window_title() == ""
    view.feed(b"\x1b]0;root@router: ~\x07")
    assert titles == ["root@router: ~"]


@pytest.mark.parametrize("model", ["pyte", "vterm"])
def test_plain_text_link_under_the_pointer(model, monkeypatch):
    from types import SimpleNamespace

    from sshpilot.terminal_backend_pyte import PyteTerminalBackend

    monkeypatch.setenv("SSHPILOT_TERMINAL_MODEL", model)
    backend = PyteTerminalBackend(SimpleNamespace(config=None))
    view = backend.view
    view.model.resize(80, 5)
    view.feed(b"docs at https://example.org/guide now\r\n")
    assert backend.supports_feature("hyperlinks")
    row = view.model.history_length  # first screen row
    column = len("docs at ") + 4
    # Pointer coordinates include the view's padding around the text grid.
    x = _PAD_X + (column + 0.5) * view._cell_w
    y = _PAD_Y + 0.5 * view._cell_h
    assert view._cell_at(x, y) == (row, column)
    assert backend.hyperlink_at(x, y) == "https://example.org/guide"
    assert backend.hyperlink_at(_PAD_X + 0.5 * view._cell_w, y) is None
    backend.destroy()

"""Opening omni-search must not destroy the terminal's selection (GH #1178).

A focused ``GtkText`` publishes its selection on PRIMARY, and VTE deselects
when it loses PRIMARY ownership (``Terminal::widget_clipboard_data_clear``).
So focusing the omni-search entry over a live terminal selection silently
wipes it, and every copy path afterwards reports an empty selection.

``grab_focus()`` alone is enough to trigger it: ``GtkText`` selects all of its
content on focus-in. These tests drive the real widgets rather than a stub,
because the whole bug lives in GTK's focus/PRIMARY behavior.
"""

from __future__ import annotations

import pytest

from tests._gui_harness import requires_gui

Gtk, _Adw, _Gio, GLib = requires_gui()

import gi

gi.require_version("Vte", "3.91")
from gi.repository import Vte  # noqa: E402

from sshpilot.omni_search import focus_search_entry  # noqa: E402

pytestmark = pytest.mark.gui


def _pump(milliseconds=250):
    context = GLib.MainContext.default()
    done = {"value": False}
    GLib.timeout_add(
        milliseconds,
        lambda: done.__setitem__("value", True) or GLib.SOURCE_REMOVE,
    )
    while not done["value"]:
        context.iteration(True)


@pytest.fixture
def selected_terminal():
    """A presented window with a VTE terminal holding a real selection."""
    window = Gtk.Window()
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    terminal = Vte.Terminal()
    terminal.set_size_request(600, 200)
    entry = Gtk.SearchEntry()
    box.append(terminal)
    box.append(entry)
    window.set_child(box)
    window.present()
    _pump(400)

    terminal.feed(b"root@host:~# cat /etc/hostname\r\nprod-web-01\r\n")
    _pump(200)
    terminal.grab_focus()
    terminal.select_all()
    _pump(300)
    assert terminal.get_has_selection(), "fixture failed to make a selection"

    try:
        yield terminal, entry
    finally:
        window.destroy()
        _pump(100)


def _delegate(entry):
    return entry.get_delegate() or entry


def test_focusing_entry_with_stale_text_keeps_terminal_selection(selected_terminal):
    """The double-Shift / shortcut path: open with a previous query present."""
    terminal, entry = selected_terminal
    entry.set_text("previous query")

    focus_search_entry(entry, _delegate(entry), select_all=True)
    _pump(400)

    assert terminal.get_has_selection(), (
        "opening omni-search destroyed the terminal selection; copy would "
        "fail with an empty selection (GH #1178)"
    )
    assert _delegate(entry).has_focus(), "omni-search entry must take focus"


def test_focused_entry_is_ready_to_type_over_the_stale_query(selected_terminal):
    """select_all exists so typing replaces the old query -- keep that."""
    _terminal, entry = selected_terminal
    entry.set_text("previous query")

    focus_search_entry(entry, _delegate(entry), select_all=True)
    _pump(300)

    delegate = _delegate(entry)
    assert delegate.has_focus()
    bounds = delegate.get_selection_bounds()
    assert not (len(bounds) == 2 and bounds[0] != bounds[1]), (
        "a focused entry with a selection owns PRIMARY, which is what "
        "clears the terminal selection"
    )
    # Typing must still replace the stale query rather than append to it.
    assert entry.get_text() == ""


def test_click_path_preserves_the_typed_query(selected_terminal):
    """select_all=False (the docked entry click path) keeps what was typed."""
    terminal, entry = selected_terminal
    entry.set_text("half-typed quer")

    focus_search_entry(entry, _delegate(entry), select_all=False)
    _pump(400)

    assert entry.get_text() == "half-typed quer"
    assert terminal.get_has_selection(), (
        "the click path must not destroy the terminal selection either"
    )


def test_empty_entry_is_harmless(selected_terminal):
    """An empty entry never owned PRIMARY -- this is why a fresh start works."""
    terminal, entry = selected_terminal
    entry.set_text("")

    focus_search_entry(entry, _delegate(entry), select_all=True)
    _pump(300)

    assert terminal.get_has_selection()

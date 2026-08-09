"""Regression: the VTE keyboard-search controller must install on normal init.

Ctrl+F / Ctrl+G / Esc terminal search is wired by ``_install_shortcuts()``,
which ``setup_terminal()`` calls from inside ``TerminalWidget.__init__``. That
requires ``self._search`` to already exist when ``setup_terminal()`` runs — i.e.
``self._search = TerminalSearch(self)`` must be created *before* the
``setup_terminal()`` call. The composed-object extraction briefly got this order
wrong (search object built after ``setup_terminal``), and the AttributeError was
swallowed, so keyboard search silently never installed.

The unit characterization tests build widgets via ``__new__`` and so cannot see
this wiring; only a real ``TerminalWidget`` construction does. Opt-in GUI test,
skipped in CI / under the stubbed gi.
"""
import pytest

from tests._gui_harness import requires_gui

requires_gui()

from gi.repository import Gtk

pytestmark = pytest.mark.gui


def test_search_key_controller_installed_on_local_terminal(gui):
    gui.reset()
    gui.open_local_tabs(1)

    terminals = [t for terms in gui.window.connection_to_terminals.values() for t in terms]
    assert terminals, "no local terminal was created"

    for t in terminals:
        assert getattr(t, "_search", None) is not None, "TerminalSearch not attached"
        assert t._search._search_key_controller is not None, (
            "VTE search key controller was not installed during init "
            "(self._search must be created before setup_terminal())"
        )


def test_search_layout_uses_flow_for_active_terminal_backend(gui):
    """Search reduces the real viewport for the active terminal widget."""
    gui.reset()
    gui.open_local_tabs(1)

    terminals = [t for terms in gui.window.connection_to_terminals.values() for t in terms]
    assert terminals, "no local terminal was created"
    terminal = terminals[0]

    stack = terminal.terminal_stack
    assert terminal.search_revealer.get_parent() is stack
    assert terminal.search_revealer.get_next_sibling() is terminal.overlay
    assert terminal.overlay.get_parent() is stack
    assert terminal.disconnected_revealer.get_parent() is stack
    assert terminal.save_connection_revealer.get_parent() is stack

    # Only the connecting UI remains a true overlay child.
    assert terminal.connecting_bg.get_parent() is terminal.overlay
    assert terminal.connecting_box.get_parent() is terminal.overlay
    assert terminal.search_revealer.get_transition_type() == Gtk.RevealerTransitionType.NONE

    assert terminal.search_revealer.get_visible() is False
    terminal._show_search_overlay(select_all=True)
    assert terminal.search_revealer.get_visible() is True
    assert terminal.search_revealer.get_reveal_child() is True
    terminal._hide_search_overlay()
    assert terminal.search_revealer.get_visible() is False

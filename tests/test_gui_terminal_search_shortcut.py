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

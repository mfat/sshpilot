"""The sidebar header must not carry window controls.

They live in the content title bar, and a copy in the sidebar header asks for
~126px — twice the width of the minimal icon strip, which held the whole sidebar
that wide and left the strip showing empty space instead of its icons once the
split view became a resizable ``Gtk.Paned``.
"""

import importlib
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def sidebar_mod():
    return importlib.import_module('sshpilot.sidebar')


def _assemble(sidebar_mod, monkeypatch):
    header = MagicMock(name='sidebar_header_bar')
    toolbar_view = MagicMock(name='sidebar_toolbar_view')
    adw = MagicMock(name='Adw')
    adw.HeaderBar.return_value = header
    adw.ToolbarView.return_value = toolbar_view
    monkeypatch.setattr(sidebar_mod, 'Adw', adw)
    # The headless stubs hand back bare objects for Gtk widgets; the shell only
    # builds a title label here, so a mock module is enough.
    monkeypatch.setattr(sidebar_mod, 'Gtk', MagicMock(name='Gtk'))

    window = MagicMock(name='window')
    sidebar_mod._assemble_sidebar_shell(window, MagicMock(name='sidebar_box'))
    return window, header


def test_sidebar_header_shows_no_window_controls(sidebar_mod, monkeypatch):
    _window, header = _assemble(sidebar_mod, monkeypatch)
    header.set_show_start_title_buttons.assert_called_once_with(False)
    header.set_show_end_title_buttons.assert_called_once_with(False)


def test_sidebar_shell_is_attached_to_the_split_view(sidebar_mod, monkeypatch):
    window, _header = _assemble(sidebar_mod, monkeypatch)
    window._set_sidebar_widget.assert_called_once_with(window._sidebar_toolbar_view)

"""What the sidebar header does and does not carry.

No window controls: they live in the content title bar, and a copy in the
sidebar header would floor the header (and the sidebar) much wider than the
connection list needs.
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


def test_sidebar_header_title_is_the_title_label(sidebar_mod, monkeypatch):
    window, header = _assemble(sidebar_mod, monkeypatch)
    header.set_title_widget.assert_called_once_with(window._sidebar_title_label)

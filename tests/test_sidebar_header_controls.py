"""What the sidebar header does and does not carry.

No window controls: they live in the content title bar, and a copy in the
sidebar header asks for ~126px — twice the width of the minimal icon strip,
which held the whole sidebar that wide and left the strip showing empty space
instead of its icons once the split view became a resizable ``Gtk.Paned``.

The app icon, though, is built here and starts hidden: it stands in for the
title in the minimal strip, where the title label is hidden and the title moves
to the content header (``window._apply_sidebar_minimal_chrome``).
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
    # The headless stubs hand back bare objects for Gtk widgets, so both the
    # widget module and the icon helper (which the shell uses for the app icon)
    # have to be mocks here.
    monkeypatch.setattr(sidebar_mod, 'Gtk', MagicMock(name='Gtk'))
    icon_utils = importlib.import_module('sshpilot.icon_utils')
    monkeypatch.setattr(
        icon_utils, 'new_image_from_icon_name',
        MagicMock(name='new_image_from_icon_name'))

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


def test_the_app_icon_is_built_hidden_for_the_strip(sidebar_mod, monkeypatch):
    """It is the strip's stand-in for the title, so the full sidebar — which
    shows the title label — must not show it too."""
    window, _header = _assemble(sidebar_mod, monkeypatch)
    icon_utils = importlib.import_module('sshpilot.icon_utils')
    icon_utils.new_image_from_icon_name.assert_called_once_with(
        'io.github.mfat.sshpilot')
    window._sidebar_app_icon.set_visible.assert_called_once_with(False)

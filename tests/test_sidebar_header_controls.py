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


def test_header_compact_tightens_horizontal_margins_only():
    """Strip mode narrows side padding; vertical margins stay put so the
    New Connection toolbar does not jump when rows compact."""
    win_mod = importlib.import_module('sshpilot.window')
    win = win_mod.MainWindow.__new__(win_mod.MainWindow)

    handle = MagicMock(name='handle')
    header = MagicMock(name='header_toolbar')
    win._sidebar_header_stack = None
    win._sidebar_header_handle = handle
    win._sidebar_header_toolbar = header
    win._hide_hosts_button = None

    win_mod.MainWindow._apply_sidebar_header_compact(win, True)
    handle.set_visible.assert_called_with(True)
    header.set_margin_start.assert_called_with(6)
    header.set_margin_end.assert_called_with(6)
    header.set_margin_top.assert_called_with(12)
    header.set_margin_bottom.assert_called_with(6)

    win_mod.MainWindow._apply_sidebar_header_compact(win, False)
    header.set_margin_start.assert_called_with(12)
    header.set_margin_end.assert_called_with(12)
    header.set_margin_top.assert_called_with(12)
    header.set_margin_bottom.assert_called_with(6)


def test_header_compact_hides_hostname_toggle(monkeypatch):
    """Hide-hostnames is irrelevant in the strip (no host labels shown)."""
    win_mod = importlib.import_module('sshpilot.window')
    overflow = importlib.import_module('sshpilot.overflow_toolbar')
    win = win_mod.MainWindow.__new__(win_mod.MainWindow)

    hide_btn = MagicMock(name='hide_hosts')
    header = MagicMock(name='header_toolbar')
    win._sidebar_header_handle = MagicMock()
    win._sidebar_header_toolbar = header
    win._hide_hosts_button = hide_btn

    marked = []
    monkeypatch.setattr(
        overflow, 'mark_force_hidden',
        lambda widget, hidden=True: marked.append((widget, hidden)))

    win_mod.MainWindow._apply_sidebar_header_compact(win, True)
    assert marked == [(hide_btn, True)]

    marked.clear()
    win_mod.MainWindow._apply_sidebar_header_compact(win, False)
    assert marked == [(hide_btn, False)]

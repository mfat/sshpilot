"""Reparenting logic for the detachable SearchPopup (search_popup.py).

show()/hide() move the live sidebar_box between the split view's ToolbarView and
the floating overlay panel. We exercise the class via __new__ with mock widgets —
no GTK realization needed.
"""
import types
from unittest.mock import MagicMock

from sshpilot.search_popup import SearchPopup


def _popup():
    p = SearchPopup.__new__(SearchPopup)
    p._visible = False
    p._transparent = False
    p._scrim = MagicMock()
    p._panel = MagicMock()

    win = types.SimpleNamespace()
    win.config = MagicMock()
    win.config.get_setting.return_value = 280
    win._sidebar_box = MagicMock()
    win._sidebar_toolbar_view = MagicMock()
    win._sidebar_minimal = False
    win._set_sidebar_clipping = MagicMock()
    win._apply_sidebar_minimal_chrome = MagicMock()
    win._apply_sidebar_minimal_rows = MagicMock()
    win.split_view = MagicMock()
    win.split_view.get_width.return_value = 1200
    win.split_view.get_sidebar_width_fraction.return_value = 0.25
    win.search_container = None
    p._window = win
    return p, win


def test_show_reparents_into_panel_with_full_content():
    p, win = _popup()
    p.show()

    assert p.visible is True
    win._sidebar_toolbar_view.set_content.assert_called_once_with(None)
    p._panel.append.assert_called_once_with(win._sidebar_box)
    p._panel.set_visible.assert_called_with(True)
    p._scrim.set_visible.assert_called_with(True)
    # Popup always shows the full sidebar.
    win._apply_sidebar_minimal_rows.assert_called_once_with(False)
    win._apply_sidebar_minimal_chrome.assert_called_once_with(False)
    # Sized to the fraction-based width (0.25*1200=300) clamped to max 280.
    p._panel.set_size_request.assert_called_once_with(280, -1)


def test_hide_reattaches_to_toolbar_view():
    p, win = _popup()
    p._visible = True
    win._sidebar_box.get_parent.return_value = p._panel

    p.hide()

    assert p.visible is False
    p._panel.remove.assert_called_once_with(win._sidebar_box)
    win._sidebar_toolbar_view.set_content.assert_called_once_with(win._sidebar_box)
    p._panel.set_visible.assert_called_with(False)


def test_hide_recollapses_when_minimal():
    p, win = _popup()
    p._visible = True
    win._sidebar_minimal = True
    win._sidebar_box.get_parent.return_value = p._panel

    p.hide()

    win._apply_sidebar_minimal_rows.assert_called_once_with(True)
    win._apply_sidebar_minimal_chrome.assert_called_once_with(True)


def test_show_is_idempotent():
    p, win = _popup()
    p._visible = True
    p.show()
    win._sidebar_toolbar_view.set_content.assert_not_called()


def test_hide_is_noop_when_not_shown():
    p, win = _popup()
    p.hide()
    win._sidebar_toolbar_view.set_content.assert_not_called()


def test_transparency_toggle_adds_and_removes_class():
    p, _win = _popup()
    p.set_transparent(True)
    assert p._transparent is True
    p._panel.add_css_class.assert_called_once_with('sidebar-popup-transparent')

    p.set_transparent(False)
    assert p._transparent is False
    p._panel.remove_css_class.assert_called_once_with('sidebar-popup-transparent')


def test_dismiss_routes_to_search_teardown_when_searching():
    p, win = _popup()
    win.search_container = MagicMock()
    win.search_container.get_visible.return_value = True
    win._close_search_if_open = MagicMock()

    p.dismiss()

    win._close_search_if_open.assert_called_once()

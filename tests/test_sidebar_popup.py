"""Reparenting logic for the detachable sidebar popup (window.py).

show_sidebar_popup/hide_sidebar_popup move the live sidebar_box between the split
view's ToolbarView and the floating overlay panel. We exercise the methods on a
bare stand-in with mock widgets — no GTK realization needed.
"""
import types
from unittest.mock import MagicMock

from sshpilot.window import MainWindow


def _win():
    w = types.SimpleNamespace()
    w.config = MagicMock()
    w.config.get_setting.return_value = 280
    w._sidebar_box = MagicMock()
    w._sidebar_toolbar_view = MagicMock()
    w._sidebar_popup = MagicMock()
    w._sidebar_popup_scrim = MagicMock()
    w._sidebar_popup_visible = False
    w._sidebar_minimal = False
    w._set_sidebar_clipping = MagicMock()
    w._apply_sidebar_minimal_chrome = MagicMock()
    w._apply_sidebar_minimal_rows = MagicMock()
    # Real state helper, bound to the stand-in.
    w.sidebar_popup_visible = types.MethodType(MainWindow.sidebar_popup_visible, w)
    return w


def test_show_reparents_into_panel_with_full_content():
    w = _win()
    MainWindow.show_sidebar_popup(w)

    assert w._sidebar_popup_visible is True
    w._sidebar_toolbar_view.set_content.assert_called_once_with(None)
    w._sidebar_popup.append.assert_called_once_with(w._sidebar_box)
    w._sidebar_popup.set_visible.assert_called_with(True)
    w._sidebar_popup_scrim.set_visible.assert_called_with(True)
    # Popup always shows the full sidebar.
    w._apply_sidebar_minimal_rows.assert_called_once_with(False)
    w._apply_sidebar_minimal_chrome.assert_called_once_with(False)


def test_hide_reattaches_to_toolbar_view():
    w = _win()
    w._sidebar_popup_visible = True
    w._sidebar_box.get_parent.return_value = w._sidebar_popup

    MainWindow.hide_sidebar_popup(w)

    assert w._sidebar_popup_visible is False
    w._sidebar_popup.remove.assert_called_once_with(w._sidebar_box)
    w._sidebar_toolbar_view.set_content.assert_called_once_with(w._sidebar_box)
    w._sidebar_popup.set_visible.assert_called_with(False)


def test_hide_recollapses_when_minimal():
    w = _win()
    w._sidebar_popup_visible = True
    w._sidebar_minimal = True
    w._sidebar_box.get_parent.return_value = w._sidebar_popup

    MainWindow.hide_sidebar_popup(w)

    w._apply_sidebar_minimal_rows.assert_called_once_with(True)
    w._apply_sidebar_minimal_chrome.assert_called_once_with(True)


def test_show_is_idempotent():
    w = _win()
    w._sidebar_popup_visible = True
    MainWindow.show_sidebar_popup(w)
    w._sidebar_toolbar_view.set_content.assert_not_called()


def test_hide_is_noop_when_not_shown():
    w = _win()
    MainWindow.hide_sidebar_popup(w)
    w._sidebar_toolbar_view.set_content.assert_not_called()


def test_transparency_toggle_adds_and_removes_class():
    w = _win()
    MainWindow.set_sidebar_popup_transparent(w, True)
    assert w._sidebar_popup_transparent is True
    w._sidebar_popup.add_css_class.assert_called_once_with('sidebar-popup-transparent')

    MainWindow.set_sidebar_popup_transparent(w, False)
    assert w._sidebar_popup_transparent is False
    w._sidebar_popup.remove_css_class.assert_called_once_with('sidebar-popup-transparent')

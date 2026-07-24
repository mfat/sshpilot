from unittest.mock import MagicMock

from sshpilot.window import MainWindow


class _Window:
    _sidebar_minimal = False
    _expand_sidebar_for_search = MainWindow._expand_sidebar_for_search
    activate_search_entry = MainWindow.activate_search_entry


def test_ctrl_f_uses_popup_when_f9_sidebar_is_hidden():
    window = _Window()
    window._search_popup = MagicMock()
    window.sidebar_toggle_button = MagicMock()
    window.sidebar_toggle_button.get_active.return_value = True
    window.search_container = MagicMock()
    # Exercise the important case where the docked search was already visible:
    # it is still inaccessible because its sidebar is hidden.
    window.search_container.get_visible.return_value = True
    window.search_entry = MagicMock()
    window.search_entry.get_text.return_value = ""

    window.activate_search_entry()

    window._search_popup.show.assert_called_once_with()
    window.sidebar_toggle_button.set_active.assert_not_called()
    window.search_entry.grab_focus.assert_called_once_with()
    assert window._search_expanded_sidebar is True


def test_ctrl_f_does_not_use_popup_for_visible_full_sidebar():
    window = _Window()
    window._search_popup = MagicMock()
    window.sidebar_toggle_button = MagicMock()
    window.sidebar_toggle_button.get_active.return_value = False
    window.search_container = MagicMock()
    window.search_container.get_visible.return_value = False
    window.search_entry = MagicMock()
    window.search_entry.get_text.return_value = ""
    window.toast_overlay = MagicMock()

    window.activate_search_entry()

    window._search_popup.show.assert_not_called()
    window.search_container.set_visible.assert_called_once_with(True)
    assert window._search_expanded_sidebar is False

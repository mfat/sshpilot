from unittest.mock import MagicMock

from sshpilot.window import MainWindow


class _Window:
    # _expand_sidebar_for_search dismisses the command popup before detaching
    # the sidebar; MainWindow.__init__ leaves it None until one is built.
    _command_popup = None
    _expand_sidebar_for_search = MainWindow._expand_sidebar_for_search
    _ensure_search_popup = MainWindow._ensure_search_popup
    activate_search_entry = MainWindow.activate_search_entry
    _sidebar_hidden_by_toggle = MainWindow._sidebar_hidden_by_toggle


def test_ctrl_f_uses_popup_when_f9_sidebar_is_hidden():
    window = _Window()
    window._search_popup = MagicMock()
    window._sidebar_visible = False
    window.search_container = MagicMock()
    # Exercise the important case where the docked search was already visible:
    # it is still inaccessible because its sidebar is hidden.
    window.search_container.get_visible.return_value = True
    window.search_entry = MagicMock()
    window.search_entry.get_text.return_value = ""

    window.activate_search_entry()

    window._search_popup.show.assert_called_once_with()
    window.search_entry.grab_focus.assert_called_once_with()
    assert window._search_expanded_sidebar is True


def test_ctrl_f_does_not_use_popup_for_visible_full_sidebar():
    window = _Window()
    window._search_popup = MagicMock()
    window._sidebar_visible = True
    window.search_container = MagicMock()
    window.search_container.get_visible.return_value = False
    window.search_entry = MagicMock()
    window.search_entry.get_text.return_value = ""
    window.toast_overlay = MagicMock()

    window.activate_search_entry()

    window._search_popup.show.assert_not_called()
    window.search_container.set_visible.assert_called_once_with(True)
    assert window._search_expanded_sidebar is False

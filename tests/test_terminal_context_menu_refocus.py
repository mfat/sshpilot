"""Closing the app-owned terminal context menu must not strand keyboard focus (#1378).

GTK moves focus from a closing popover to an ancestor container, so the next
Enter reached the sidebar instead of the pasted command. Verified end to end
against the pyte backend under Xvfb; these tests pin the focus decision.
"""

import sshpilot.terminal as terminal_mod


class _Widget:
    def __init__(self, name, parent=None):
        self.name = name
        self.parent = parent

    def is_ancestor(self, other):
        node = self.parent
        while node is not None:
            if node is other:
                return True
            node = node.parent
        return False


def _tree():
    window = _Widget("window")
    paned = _Widget("paned", window)
    sidebar_row = _Widget("row", paned)
    terminal = _Widget("terminal-box", paned)
    view = _Widget("view", terminal)
    popover = _Widget("popover", terminal)
    menu_item = _Widget("menu-item", popover)
    search_entry = _Widget("search-entry", paned)
    return locals()


def _decide(focus_name):
    t = _tree()
    focus = t[focus_name] if focus_name else None
    return terminal_mod._menu_close_should_refocus_terminal(
        focus, t["terminal"], t["popover"])


def test_focus_left_on_an_ancestor_container_returns_to_terminal():
    assert _decide("paned") is True
    assert _decide("window") is True


def test_focus_nowhere_or_inside_closed_menu_returns_to_terminal():
    assert _decide(None) is True
    assert _decide("menu_item") is True


def test_focus_already_in_terminal_is_left_alone():
    assert _decide("view") is False
    assert _decide("terminal") is False


def test_focus_an_action_moved_elsewhere_is_kept():
    assert _decide("search_entry") is False
    assert _decide("sidebar_row") is False

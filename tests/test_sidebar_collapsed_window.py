"""The window's side of a collapsed sidebar (#1423).

When the window is too narrow, SidebarPaned collapses the sidebar and the
window floats it in the search popup instead. These drive the real
MainWindow methods on a stub window, so they run under the suite's stubbed
``gi`` without building any widgets.
"""
from unittest.mock import MagicMock

from sshpilot.window import MainWindow


class _Popup:
    def __init__(self):
        self.visible = False
        self.hides = []

    def show(self):
        self.visible = True

    def hide(self, animate=True):
        self.hides.append(animate)
        self.visible = False


class _Hover:
    def __init__(self, revealed=False):
        self.revealed = revealed
        self.claims = 0

    def claim(self):
        self.claims += 1
        self.revealed = False


class _Window:
    _toggle_sidebar_overlay = MainWindow._toggle_sidebar_overlay
    _sidebar_overlay_shown = MainWindow._sidebar_overlay_shown
    _sidebar_shown = MainWindow._sidebar_shown
    _sidebar_collapsed = MainWindow._sidebar_collapsed
    _sidebar_hidden_by_toggle = MainWindow._sidebar_hidden_by_toggle
    _dismiss_search_popup = MainWindow._dismiss_search_popup
    _reveal_sidebar = MainWindow._reveal_sidebar
    _on_sidebar_collapsed_changed = MainWindow._on_sidebar_collapsed_changed
    _apply_sidebar_visible = MainWindow._apply_sidebar_visible
    _toggle_sidebar_visibility = MainWindow._toggle_sidebar_visibility
    _cancel_pending_sidebar_hide = MainWindow._cancel_pending_sidebar_hide

    def __init__(self, *, collapsed=True, visible=True, hover=None):
        self._split_variant = 'paned'
        self.split_view = MagicMock()
        self.split_view.get_collapsed.side_effect = lambda: self.collapsed
        self.collapsed = collapsed
        self._sidebar_visible = visible
        self._search_popup = _Popup()
        self._search_expanded_sidebar = False
        self._sidebar_hide_timer_id = None
        self._sidebar_hover_reveal = hover
        self.search_container = MagicMock()
        self.search_container.get_visible.return_value = False
        self.button_states = []

    def _ensure_search_popup(self):
        return self._search_popup

    def _sync_sidebar_toggle_button(self, visible):
        self.button_states.append(visible)


def test_toggle_floats_and_puts_away_a_collapsed_sidebar():
    window = _Window(hover=_Hover())
    window._toggle_sidebar_overlay()
    assert window._search_popup.visible
    assert window.button_states[-1] is True
    window._toggle_sidebar_overlay()
    assert not window._search_popup.visible
    assert window.button_states[-1] is False


def test_clicking_the_toggle_after_a_hover_reveal_keeps_the_sidebar_open():
    # Resting on the toggle before clicking it hover-reveals the popup; the
    # click means "show", not "close what just appeared".
    hover = _Hover(revealed=True)
    window = _Window(hover=hover)
    window._search_popup.visible = True
    window._toggle_sidebar_overlay()
    assert window._search_popup.visible
    assert window._search_popup.hides == []
    assert hover.claims == 1  # the hover no longer hides it on leave
    assert window.button_states[-1] is True


def test_closing_from_the_toggle_disarms_the_hover_reveal():
    hover = _Hover()
    window = _Window(hover=hover)
    window._search_popup.visible = True
    window._toggle_sidebar_overlay()
    assert not window._search_popup.visible
    assert hover.claims == 1


def test_show_when_no_tabs_while_collapsed_keeps_a_floating_sidebar():
    window = _Window(visible=False)
    window._search_popup.visible = True
    window._apply_sidebar_visible(True)
    assert window._sidebar_visible is True  # docks once the window widens
    assert window._search_popup.visible
    assert window.button_states[-1] is True


def test_show_when_no_tabs_while_collapsed_does_not_claim_it_is_shown():
    window = _Window(visible=False)
    window._apply_sidebar_visible(True)
    assert window._sidebar_visible is True
    assert window.button_states[-1] is False  # nothing is on screen


def test_widening_docks_a_floating_sidebar():
    window = _Window()
    window._search_popup.visible = True
    window.collapsed = False
    window._on_sidebar_collapsed_changed(False)
    assert window._search_popup.hides == [False]
    assert window.button_states[-1] is True


def test_widening_puts_away_a_floating_sidebar_the_user_had_hidden():
    window = _Window(visible=False)
    window._search_popup.visible = True
    window.collapsed = False
    window._on_sidebar_collapsed_changed(False)
    assert not window._search_popup.visible
    assert window.button_states[-1] is False


def test_widening_keeps_a_search_popup_for_a_user_hidden_sidebar():
    window = _Window(visible=False)
    window._search_popup.visible = True
    window._search_expanded_sidebar = True
    window.collapsed = False
    window._on_sidebar_collapsed_changed(False)
    assert window._search_popup.visible
    assert window.button_states[-1] is False


def test_reveal_floats_a_collapsed_sidebar():
    window = _Window()
    window._reveal_sidebar()
    assert window._search_popup.visible

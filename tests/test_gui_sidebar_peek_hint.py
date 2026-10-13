"""Starts with a hidden sidebar explain the hover reveal until dismissed.

Opt-in only: SSHPILOT_GUI_TESTS=1 pytest -m gui
"""

import pytest

from tests._gui_harness import requires_gui  # the `gui` fixture comes from conftest

requires_gui()

pytestmark = pytest.mark.gui


def _settle(gui, condition, ms=3000):
    # Fixed short pumps lose to slow low-priority idles elsewhere in the app.
    for _i in range(ms // 50):
        if condition():
            return True
        gui.pump(50)
    return condition()


def test_hint_shows_each_start_until_dont_show_again(gui):
    from sshpilot import sidebar_peek_hint

    window, config = gui.window, gui.window.config
    key = sidebar_peek_hint.HINT_DISMISSED_KEY
    old_dismissed = config.get_setting(key, False)
    old_button = config.get_setting('ui.headerbar_show_sidebar_toggle', True)
    try:
        config.set_setting(key, False)
        window._apply_sidebar_visible(True)
        assert sidebar_peek_hint.show_if_needed(window) is None, 'sidebar is visible'

        window._apply_sidebar_visible(False)
        config.set_setting('ui.headerbar_show_sidebar_toggle', False)
        window.update_headerbar_buttons()
        assert _settle(gui, lambda: not window.sidebar_toggle_button.get_mapped())
        assert sidebar_peek_hint.show_if_needed(window) is None, 'no button to hover'

        config.set_setting('ui.headerbar_show_sidebar_toggle', True)
        window.update_headerbar_buttons()
        assert _settle(gui, lambda: window.sidebar_toggle_button.get_mapped())
        popover = sidebar_peek_hint.show_if_needed(window)
        assert popover is not None
        assert _settle(gui, popover.get_visible)
        assert popover.get_parent() is window.sidebar_toggle_button

        popover.popdown()  # Got It / click away: comes back next start
        assert _settle(gui, lambda: popover.get_parent() is None), 'popover not cleaned up'
        assert not config.get_setting(key)

        popover = sidebar_peek_hint.show_if_needed(window)
        assert popover is not None, 'shown again on the next start'
        assert _settle(gui, popover.get_visible)
        popover.dont_show_button.emit('clicked')
        assert _settle(gui, lambda: popover.get_parent() is None)
        assert config.get_setting(key) is True
        assert sidebar_peek_hint.show_if_needed(window) is None, 'dismissed for good'
    finally:
        config.set_setting(key, old_dismissed)
        config.set_setting('ui.headerbar_show_sidebar_toggle', old_button)
        window.update_headerbar_buttons()
        window._apply_sidebar_visible(True)

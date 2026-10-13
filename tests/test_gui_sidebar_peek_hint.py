"""The first start with a hidden sidebar explains the hover reveal, once.

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


def test_hint_shows_once_on_the_sidebar_button_while_hidden(gui):
    from sshpilot import sidebar_peek_hint

    window, config = gui.window, gui.window.config
    old_shown = config.get_setting(sidebar_peek_hint.HINT_SHOWN_KEY, False)
    old_button = config.get_setting('ui.headerbar_show_sidebar_toggle', True)
    try:
        config.set_setting(sidebar_peek_hint.HINT_SHOWN_KEY, False)
        window._apply_sidebar_visible(True)
        assert sidebar_peek_hint.show_if_needed(window) is None, 'sidebar is visible'

        window._apply_sidebar_visible(False)
        config.set_setting('ui.headerbar_show_sidebar_toggle', False)
        window.update_headerbar_buttons()
        assert _settle(gui, lambda: not window.sidebar_toggle_button.get_mapped())
        assert sidebar_peek_hint.show_if_needed(window) is None, 'no button to hover'
        assert not config.get_setting(sidebar_peek_hint.HINT_SHOWN_KEY), 'still pending'

        config.set_setting('ui.headerbar_show_sidebar_toggle', True)
        window.update_headerbar_buttons()
        assert _settle(gui, lambda: window.sidebar_toggle_button.get_mapped())
        popover = sidebar_peek_hint.show_if_needed(window)
        assert popover is not None
        assert _settle(gui, popover.get_visible)
        assert popover.get_parent() is window.sidebar_toggle_button
        assert config.get_setting(sidebar_peek_hint.HINT_SHOWN_KEY) is True

        popover.popdown()
        assert _settle(gui, lambda: popover.get_parent() is None), 'popover not cleaned up'
        assert sidebar_peek_hint.show_if_needed(window) is None, 'shown twice'
    finally:
        config.set_setting(sidebar_peek_hint.HINT_SHOWN_KEY, old_shown)
        config.set_setting('ui.headerbar_show_sidebar_toggle', old_button)
        window.update_headerbar_buttons()
        window._apply_sidebar_visible(True)

"""Users on any other style are offered Colored Grips, pointing at a coloured grip.

Opt-in only: SSHPILOT_GUI_TESTS=1 pytest -m gui
"""

import pytest

from tests._gui_harness import requires_gui  # the `gui` fixture comes from conftest

requires_gui()

pytestmark = pytest.mark.gui

GROUP_NAME = "Grip Hint Test Group"


def _settle(gui, condition, ms=3000):
    for _i in range(ms // 50):
        if condition():
            return True
        gui.pump(50)
    return condition()


def _group_id(window):
    for gid, info in window.group_manager.groups.items():
        if info.get('name') == GROUP_NAME:
            return gid
    return None


def test_hint_points_at_a_coloured_grip_and_switches(gui):
    from sshpilot import sidebar_grip_hint as hint

    window, config = gui.window, gui.window.config
    saved = {k: config.get_setting(k, d) for k, d in (
        (hint.HINT_DISMISSED_KEY, False),
        ('ui.group_color_display', 'dot'),
        ('ui.sidebar_show_drag_handles', True),
    )}
    try:
        config.set_setting(hint.HINT_DISMISSED_KEY, False)
        config.set_setting('ui.group_color_display', 'dot')
        config.set_setting('ui.sidebar_show_drag_handles', True)
        window._apply_sidebar_visible(True)
        window.group_manager.create_group(GROUP_NAME, None, '#e01b24')
        window.rebuild_connection_list()
        assert _settle(gui, lambda: hint.find_anchor(window) is not None), \
            'no coloured grip on screen'

        config.set_setting('ui.group_color_display', 'grip')
        window.rebuild_connection_list()
        gui.pump(200)
        assert hint.show_if_needed(window) is None, 'already on Colored Grips'

        # Any other style is offered it, Fill as much as Dots.
        config.set_setting('ui.group_color_display', 'fill')
        window.rebuild_connection_list()
        assert _settle(gui, lambda: hint.find_anchor(window) is not None)

        popover = hint.show_if_needed(window)
        assert popover is not None
        assert _settle(gui, popover.get_visible)
        assert popover.get_parent() is window.connection_list

        popover.popdown()  # click away: comes back next start
        assert _settle(gui, lambda: popover.get_parent() is None)
        assert not config.get_setting(hint.HINT_DISMISSED_KEY)

        popover = hint.show_if_needed(window)
        assert popover is not None
        assert _settle(gui, popover.get_visible)
        popover.switch_button.emit('clicked')
        assert _settle(gui, lambda: popover.get_parent() is None)
        assert config.get_setting('ui.group_color_display') == 'grip'
        assert config.get_setting(hint.HINT_DISMISSED_KEY) is True
        config.set_setting('ui.group_color_display', 'fill')
        assert hint.show_if_needed(window) is None, 'answered for good'
    finally:
        gid = _group_id(window)
        if gid is not None:
            window.group_manager.delete_group(gid)
        for key, value in saved.items():
            config.set_setting(key, value)
        window.rebuild_connection_list()

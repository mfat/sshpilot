"""Choosing Colored Grips turns Display Drag Handles back on."""

from types import SimpleNamespace

from sshpilot.core.settings.defaults import GROUP_COLOR_DISPLAY_MODES
from sshpilot.preferences import PreferencesWindow


class _Config:
    def __init__(self, **settings):
        self.settings = dict(settings)

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


class _Switch:
    def __init__(self, active, on_change):
        self.active = active
        self.on_change = on_change

    def set_active(self, active):
        if active != self.active:
            self.active = active
            self.on_change(self)

    def get_active(self):
        return self.active


def _prefs(config):
    prefs = PreferencesWindow.__new__(PreferencesWindow)
    prefs.config = config
    prefs.parent_window = None
    prefs._group_color_display_values = list(GROUP_COLOR_DISPLAY_MODES)
    prefs._config_signal_id = 1          # no sidebar refresh in the test
    return prefs


def _choose(prefs, mode):
    combo = SimpleNamespace(get_selected=lambda: GROUP_COLOR_DISPLAY_MODES.index(mode))
    PreferencesWindow.on_group_color_display_changed(prefs, combo, None)


def test_choosing_colored_grips_turns_the_handles_switch_on():
    config = _Config(**{'ui.group_color_display': 'dot', 'ui.sidebar_show_drag_handles': False})
    prefs = _prefs(config)
    prefs.show_drag_handles_switch = _Switch(
        False, lambda sw: PreferencesWindow.on_sidebar_show_drag_handles_changed(prefs, sw))
    _choose(prefs, 'grip')
    assert prefs.show_drag_handles_switch.active is True
    assert config.settings['ui.sidebar_show_drag_handles'] is True


def test_without_the_switch_built_the_setting_is_turned_on():
    config = _Config(**{'ui.group_color_display': 'dot', 'ui.sidebar_show_drag_handles': False})
    _choose(_prefs(config), 'grip')
    assert config.settings['ui.sidebar_show_drag_handles'] is True


def test_other_styles_leave_the_handles_alone():
    config = _Config(**{'ui.group_color_display': 'grip', 'ui.sidebar_show_drag_handles': False})
    _choose(_prefs(config), 'dot')
    assert config.settings['ui.sidebar_show_drag_handles'] is False

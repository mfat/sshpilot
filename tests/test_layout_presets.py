"""Layout presets are shortcuts over the existing startup settings."""

import pytest

from sshpilot.layout_presets import (
    HIDE_SIDEBAR_KEY,
    LAYOUT_PRESETS,
    STARTUP_BEHAVIOR_KEY,
    apply_preset,
    current_preset,
)


class _Config:
    def __init__(self, **settings):
        self.settings = settings
        self.writes = []

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value
        self.writes.append(key)


def test_defaults_match_the_sidebar_preset():
    assert current_preset(_Config()) == 'sidebar'


@pytest.mark.parametrize('preset_id', list(LAYOUT_PRESETS))
def test_apply_then_detect_round_trips(preset_id):
    config = _Config(**{STARTUP_BEHAVIOR_KEY: 'saved-session'})
    apply_preset(config, preset_id)
    assert current_preset(config) == preset_id


def test_settings_matching_no_preset_select_none():
    assert current_preset(_Config(**{STARTUP_BEHAVIOR_KEY: 'previous-session'})) is None
    assert current_preset(_Config(**{STARTUP_BEHAVIOR_KEY: 'terminal',
                                     HIDE_SIDEBAR_KEY: False})) is None


def test_apply_writes_only_what_changes():
    config = _Config(**{STARTUP_BEHAVIOR_KEY: 'welcome', HIDE_SIDEBAR_KEY: False})
    apply_preset(config, 'startpage')
    assert config.writes == [HIDE_SIDEBAR_KEY]

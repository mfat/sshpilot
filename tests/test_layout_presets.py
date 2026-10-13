"""Layout presets are shortcuts over the existing startup settings."""

import pytest

from sshpilot.layout_presets import (
    HIDE_SIDEBAR_KEY,
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


def _config(behavior, hide):
    return _Config(**{STARTUP_BEHAVIOR_KEY: behavior, HIDE_SIDEBAR_KEY: hide})


def test_defaults_match_the_sidebar_preset():
    assert current_preset(_Config()) == 'sidebar'


@pytest.mark.parametrize('behavior', ['welcome', 'terminal', 'previous-session'])
def test_sidebar_preset_goes_with_any_startup_view(behavior):
    config = _config(behavior, True)
    apply_preset(config, 'sidebar')
    assert config.writes == [HIDE_SIDEBAR_KEY], 'App Startup left alone'
    assert current_preset(config) == 'sidebar'


@pytest.mark.parametrize('preset_id, behavior', [('startpage', 'welcome'),
                                                 ('terminal', 'terminal')])
def test_only_presets_hide_the_sidebar(preset_id, behavior):
    config = _config('previous-session', False)
    apply_preset(config, preset_id)
    assert config.settings == {STARTUP_BEHAVIOR_KEY: behavior, HIDE_SIDEBAR_KEY: True}
    assert current_preset(config) == preset_id


def test_hidden_sidebar_with_a_restored_session_matches_no_preset():
    assert current_preset(_config('previous-session', True)) is None

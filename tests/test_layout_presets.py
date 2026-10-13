"""Layout presets are shortcuts over the existing startup settings."""

from sshpilot.layout_presets import (
    HIDE_SIDEBAR_KEY,
    STARTUP_BEHAVIOR_KEY,
    apply_view,
    current_view,
    set_sidebar_shown,
    sidebar_shown,
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


def test_defaults_are_start_page_with_sidebar():
    config = _Config()
    assert current_view(config) == 'startpage'
    assert sidebar_shown(config)


def test_view_and_sidebar_are_independent():
    config = _Config()
    apply_view(config, 'terminal')
    set_sidebar_shown(config, True)
    assert (current_view(config), sidebar_shown(config)) == ('terminal', True)
    set_sidebar_shown(config, False)
    assert current_view(config) == 'terminal'
    assert config.settings[HIDE_SIDEBAR_KEY] is True
    apply_view(config, 'startpage')
    assert (current_view(config), sidebar_shown(config)) == ('startpage', False)


def test_restored_session_selects_no_view():
    assert current_view(_Config(**{STARTUP_BEHAVIOR_KEY: 'previous-session'})) is None


def test_writes_only_what_changes():
    config = _Config(**{STARTUP_BEHAVIOR_KEY: 'terminal'})
    apply_view(config, 'terminal')
    set_sidebar_shown(config, True)
    assert config.writes == []

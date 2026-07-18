"""Plugin enable/disable must warn that a restart is required."""

from types import SimpleNamespace

from sshpilot.preferences import PreferencesWindow


class _FakeConfig:
    def __init__(self):
        self.settings = {
            'plugins.disabled': [],
            'plugins.enabled': [],
        }

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


def _prefs():
    prefs = PreferencesWindow.__new__(PreferencesWindow)
    prefs.config = _FakeConfig()
    prefs._prompts = []
    prefs._prompt_restart_required = (
        lambda body: prefs._prompts.append(body))
    return prefs


def test_builtin_plugin_toggle_prompts_restart():
    prefs = _prefs()
    row = SimpleNamespace(subtitle=None)
    row.set_subtitle = lambda text: setattr(row, 'subtitle', text)

    prefs._on_builtin_plugin_toggled('docker-manager', True, row)

    assert 'docker-manager' not in prefs.config.settings['plugins.disabled']
    assert row.subtitle == 'Restart SSH Pilot to apply'
    assert prefs._prompts
    assert 'restart' in prefs._prompts[0].lower()


def test_builtin_plugin_disable_prompts_restart():
    prefs = _prefs()
    prefs._on_builtin_plugin_toggled('docker-manager', False, row=None)
    assert prefs.config.settings['plugins.disabled'] == ['docker-manager']
    assert prefs._prompts


def test_user_plugin_disable_prompts_restart():
    prefs = _prefs()
    prefs.config.settings['plugins.enabled'] = ['example']
    prefs._on_user_plugin_toggled('example', False, row=None)
    assert prefs.config.settings['plugins.enabled'] == []
    assert prefs._prompts

"""The Docker Console built-in plugin must be off by default on a fresh install
(opt-in via Preferences ▸ Plugins), while still loadable when not disabled."""

from sshpilot.config import Config
from sshpilot.plugins.loader import load_plugins


class FakeConfig:
    def __init__(self, settings=None):
        self._settings = dict(settings or {})

    def get_setting(self, key, default=None):
        return self._settings.get(key, default)


def test_docker_manager_disabled_by_default():
    # get_default_config() returns a literal dict and does not touch instance
    # state, so a bare instance is enough to read the defaults.
    defaults = Config.get_default_config(Config.__new__(Config))
    assert defaults['plugins']['disabled'] == ['docker-manager']


def test_docker_manager_not_loaded_when_disabled():
    cfg = FakeConfig({'plugins.disabled': ['docker-manager']})
    loaded = load_plugins(app_config=cfg, connection_manager=None)
    assert not any(getattr(p, 'plugin_id', None) == 'docker-manager' for p in loaded)
    # The default disable must not take SSH (or any required plugin) down with it.
    assert any(getattr(p, 'plugin_id', None) == 'ssh' for p in loaded)

"""A stale io.github.mfat.sshpilot schema must not split settings from config.json.

Older builds stored some keys in dconf whenever that schema was installed, while
the daemon and core read config.json. Config now keeps JSON as its only store and
moves any user-set dconf values into it once.
"""

import json
from types import SimpleNamespace

import sshpilot.config as config_module
from sshpilot.config import Config


class _Variant:
    def __init__(self, value):
        self._value = value

    def unpack(self):
        return self._value


class _FakeSettings:
    def __init__(self, user_values):
        self.user_values = dict(user_values)
        self.reset_keys = []

    def get_user_value(self, key):
        if key in self.user_values:
            return _Variant(self.user_values[key])
        return None

    def reset(self, key):
        self.reset_keys.append(key)
        self.user_values.pop(key, None)


def _install_fake_gio(monkeypatch, settings, schema_keys):
    schema = SimpleNamespace(list_keys=lambda: list(schema_keys))
    source = SimpleNamespace(lookup=lambda schema_id, recursive: schema)
    fake_gio = SimpleNamespace(
        SettingsSchemaSource=SimpleNamespace(get_default=lambda: source),
        Settings=SimpleNamespace(
            new_full=lambda schema, backend, path: settings,
            sync=lambda: None,
        ),
    )
    monkeypatch.setattr(config_module, 'Gio', fake_gio)


def _make_config(tmp_path, data):
    config_file = tmp_path / 'config.json'
    config_file.write_text(json.dumps(data))
    cfg = Config.__new__(Config)
    cfg.config_file = str(config_file)
    cfg.config_data = json.loads(config_file.read_text())
    cfg._config_snapshot = json.loads(config_file.read_text())
    return cfg, config_file


def test_user_set_dconf_values_move_into_json_once(tmp_path, monkeypatch):
    settings = _FakeSettings({
        'terminal-font': 'JetBrains Mono 14',
        'ssh-compression': True,
        # Never shadowed JSON: the JSON key is ui.window_width.
        'ui-window-width': 999,
    })
    _install_fake_gio(
        monkeypatch,
        settings,
        ['terminal-font', 'terminal-theme', 'ssh-compression', 'ui-window-width'],
    )
    cfg, config_file = _make_config(tmp_path, {
        'terminal': {'font': 'Monospace 12', 'theme': 'default'},
        'ssh': {'compression': False},
        'ui': {'window_width': 1200},
    })

    cfg._import_legacy_gsettings()

    saved = json.loads(config_file.read_text())
    assert saved['terminal'] == {'font': 'JetBrains Mono 14', 'theme': 'default'}
    assert saved['ssh']['compression'] is True
    assert saved['ui']['window_width'] == 1200
    assert sorted(settings.reset_keys) == ['ssh-compression', 'terminal-font']

    # A later run finds nothing left to import and keeps the user's JSON edits.
    cfg.config_data['terminal']['font'] = 'Fira Code 11'
    cfg._import_legacy_gsettings()
    assert cfg.config_data['terminal']['font'] == 'Fira Code 11'


def test_json_is_the_only_store_even_with_schema_installed(tmp_path, monkeypatch):
    _install_fake_gio(monkeypatch, _FakeSettings({}), ['terminal-font'])
    cfg, _ = _make_config(tmp_path, {'terminal': {'font': 'Monospace 12'}})

    assert not hasattr(cfg, 'use_gsettings')
    assert cfg.get_setting('terminal.font') == 'Monospace 12'

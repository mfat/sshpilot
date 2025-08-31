import json
import os
import sys
import types


# Stub minimal gi modules required for Config
gi_repo = sys.modules.get('gi.repository', types.ModuleType('gi.repository'))

gi_repo.GObject = types.SimpleNamespace(
    SignalFlags=types.SimpleNamespace(RUN_FIRST=0),
    Object=type('GObject', (object,), {}),
)


class DummySettingsSchemaSource:
    @staticmethod
    def get_default():
        return None


gi_repo.Gio = types.SimpleNamespace(
    SettingsSchemaSource=DummySettingsSchemaSource
)
gi_repo.GLib = types.SimpleNamespace()

gi_mod = sys.modules.get('gi', types.ModuleType('gi'))
gi_mod.repository = gi_repo
gi_mod.require_version = lambda *args, **kwargs: None

sys.modules['gi'] = gi_mod
sys.modules['gi.repository'] = gi_repo
sys.modules['gi.repository.GObject'] = gi_repo.GObject
sys.modules['gi.repository.Gio'] = gi_repo.Gio
sys.modules['gi.repository.GLib'] = gi_repo.GLib


# Ensure project root is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.config import Config, CONFIG_VERSION


def test_old_config_is_replaced(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))

    config_dir = tmp_path / '.config' / 'sshpilot'
    config_dir.mkdir(parents=True)
    config_file = config_dir / 'config.json'

    # Write old configuration without config_version
    config_file.write_text(json.dumps({'terminal': {'theme': 'old'}}))

    cfg = Config.__new__(Config)
    cfg.config_file = str(config_file)
    cfg.get_default_config = Config.get_default_config.__get__(cfg, Config)
    cfg.save_json_config = Config.save_json_config.__get__(cfg, Config)

    new_config = Config.load_json_config(cfg)

    backup_file = config_dir / 'config.json.bak'

    assert backup_file.exists()
    assert new_config['config_version'] == CONFIG_VERSION
    assert json.loads(config_file.read_text())['config_version'] == CONFIG_VERSION


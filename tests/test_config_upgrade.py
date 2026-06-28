import json
import os
import sys


# conftest.py already installs ``gi.repository`` with auto-creating
# ``_DummyGIModule`` stubs for Gtk/Gio/GLib/etc. We only need to add the
# specific behaviours this test relies on, without replacing the whole module
# (which would clobber attributes other tests depend on, see #985).
from gi.repository import Gio, GLib, GObject


class DummySettingsSchemaSource:
    @staticmethod
    def get_default():
        return None


# Config() must produce a real instance; the default _DummyGIModule stub
# returns ``object()`` for any class call, so override Object with a real
# subclassable type just for this test.
GObject.Object = type('GObject', (object,), {})
Gio.SettingsSchemaSource = DummySettingsSchemaSource
GLib.get_user_config_dir = lambda: os.path.join(os.environ.get("HOME", ""), ".config")


# Ensure project root is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.config import Config, CONFIG_VERSION
from sshpilot import platform_utils


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
    saved_config = json.loads(config_file.read_text())
    assert saved_config['config_version'] == CONFIG_VERSION
    assert saved_config['terminal']['encoding'] == 'UTF-8'
    assert new_config['terminal']['encoding'] == 'UTF-8'


def test_config_path_from_glib(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr(
        platform_utils.GLib,
        'get_user_config_dir',
        lambda: str(tmp_path / '.config'),
        raising=False,
    )
    cfg = Config()
    expected = tmp_path / '.config' / 'sshpilot' / 'config.json'
    assert cfg.config_file == str(expected)


def test_get_ssh_config_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr(
        platform_utils.GLib,
        'get_user_config_dir',
        lambda: str(tmp_path / '.config'),
        raising=False,
    )
    cfg = Config()

    ssh_cfg = cfg.get_ssh_config()

    assert ssh_cfg['strict_host_key_checking'] == 'accept-new'
    assert ssh_cfg['batch_mode'] is False


def test_get_ssh_config_respects_saved_values(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr(
        platform_utils.GLib,
        'get_user_config_dir',
        lambda: str(tmp_path / '.config'),
        raising=False,
    )
    cfg = Config()

    cfg.set_setting('ssh.strict_host_key_checking', 'no')
    cfg.set_setting('ssh.connection_timeout', '45')
    cfg.set_setting('ssh.connection_attempts', '3')
    cfg.set_setting('ssh.batch_mode', 'false')

    ssh_cfg = cfg.get_ssh_config()

    assert ssh_cfg['strict_host_key_checking'] == 'no'
    assert ssh_cfg['connection_timeout'] == 45
    assert ssh_cfg['connection_attempts'] == 3
    assert ssh_cfg['batch_mode'] is False

    cfg.set_setting('ssh.strict_host_key_checking', 'maybe')
    ssh_cfg_invalid = cfg.get_ssh_config()
    assert ssh_cfg_invalid['strict_host_key_checking'] == 'accept-new'


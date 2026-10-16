import json
import os
import sys

import pytest


# conftest.py installs gi.repository stubs and makes GObject.Object a real,
# subclassable base, so Config() yields a real instance without any per-module
# stub surgery. (This file used to rebind GObject.Object / Gio.SettingsSchemaSource
# / GLib.get_user_config_dir at import time, which leaked across the shared stub
# and made the suite order-dependent — see #985.) The one test that needs a
# specific GLib.get_user_config_dir patches it function-scoped via monkeypatch.

# Ensure project root is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.config import Config, CONFIG_VERSION
from sshpilot import platform_utils


def test_old_config_is_upgraded_keeping_values(tmp_path, monkeypatch):
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

    original = config_file.read_bytes()
    new_config = Config.load_json_config(cfg)

    # Upgraded in memory with the user's values...
    assert new_config['config_version'] == CONFIG_VERSION
    assert new_config['terminal']['theme'] == 'old'
    assert new_config['terminal']['encoding'] == 'UTF-8'
    # ...while the file is left for the daemon to back up and upgrade.
    assert config_file.read_bytes() == original
    assert not list(config_dir.glob('config.json*.bak'))


def test_config_path_from_glib(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    # Wins over GLib and %APPDATA% alike, so Config() never reaches the
    # developer's real config.json (the GLib patch alone misses Windows).
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
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
    # Wins over GLib and %APPDATA% alike, so Config() never reaches the
    # developer's real config.json (the GLib patch alone misses Windows).
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
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
    # Wins over GLib and %APPDATA% alike, so Config() never reaches the
    # developer's real config.json (the GLib patch alone misses Windows).
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
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



def _config_in(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
    monkeypatch.setattr(
        platform_utils.GLib,
        'get_user_config_dir',
        lambda: str(tmp_path / '.config'),
        raising=False,
    )
    config_dir = tmp_path / '.config' / 'sshpilot'
    config_dir.mkdir(parents=True)
    return config_dir / 'config.json'



@pytest.mark.parametrize('content', [
    json.dumps({'config_version': CONFIG_VERSION + 1, 'ui': {'language': 'de'}}),
    '{"config_version": 3, "ui": ',
])
def test_unusable_config_is_never_written_over(tmp_path, monkeypatch, content):
    """A newer or malformed config.json survives a session that changes settings."""
    config_file = _config_in(tmp_path, monkeypatch)
    config_file.write_text(content, encoding='utf-8')
    original = config_file.read_bytes()

    cfg = Config()
    assert cfg.config_data['config_version'] == CONFIG_VERSION
    cfg.set_setting('ui.language', 'fr')
    assert cfg.save_json_config() is False

    assert config_file.read_bytes() == original
    assert not list(config_file.parent.glob('config.json*.bak'))


def test_settings_saved_after_daemon_upgrade_keep_old_values(tmp_path, monkeypatch):
    from sshpilot.core.settings import migrate_settings_file

    config_file = _config_in(tmp_path, monkeypatch)
    config_file.write_text(
        json.dumps({'config_version': 2, 'ui': {'language': 'de'}, 'terminal': {'theme': 'old'}}),
        encoding='utf-8',
    )
    original = config_file.read_bytes()

    cfg = Config()
    assert cfg.config_data['terminal']['theme'] == 'old'
    # Until the daemon has upgraded the file the frontend does not write it.
    assert cfg.save_json_config() is False
    assert config_file.read_bytes() == original

    migrate_settings_file(config_file)  # what the daemon does at startup
    cfg.set_setting('ui.language', 'fr')

    saved = json.loads(config_file.read_text(encoding='utf-8'))
    assert saved['config_version'] == CONFIG_VERSION
    assert saved['ui']['language'] == 'fr'
    assert saved['terminal']['theme'] == 'old'
    backups = list(config_file.parent.glob('config.json.v2-*.bak'))
    assert [b.read_bytes() for b in backups] == [original]

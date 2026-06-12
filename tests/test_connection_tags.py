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
gi_repo.GLib = types.SimpleNamespace(
    get_user_config_dir=lambda: os.path.join(os.environ.get("HOME", ""), ".config")
)

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

import sshpilot.config
from sshpilot.config import Config


def make_config(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    # The conftest gi stub makes GLib-based dir lookup return a dummy object,
    # so point the config dir at tmp_path directly.
    monkeypatch.setattr(sshpilot.config, 'get_config_dir', lambda: str(tmp_path))
    return Config()


def test_tags_round_trip_strips_whitespace_and_empties(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, monkeypatch)
    cfg.set_connection_tags('srv', [' a ', 'b', '', '  '])
    assert cfg.get_connection_tags('srv') == ['a', 'b']


def test_clearing_tags_removes_key_but_preserves_other_meta(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, monkeypatch)
    cfg.pin_connection('srv')
    cfg.set_connection_tags('srv', ['prod'])
    cfg.set_connection_tags('srv', [])
    assert cfg.get_connection_tags('srv') == []
    assert 'tags' not in cfg.get_connection_meta('srv')
    assert cfg.is_pinned('srv') is True


def test_unknown_nickname_returns_empty(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, monkeypatch)
    assert cfg.get_connection_tags('nope') == []


def test_corrupt_meta_returns_empty(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, monkeypatch)
    cfg.set_connection_meta('srv', {'tags': 'not-a-list'})
    assert cfg.get_connection_tags('srv') == []

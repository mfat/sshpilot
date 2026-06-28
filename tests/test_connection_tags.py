import os
import sys


# conftest.py provides gi.repository stubs. Override only the specific
# attributes this test needs without clobbering the shared module (see #985).
from gi.repository import Gio, GLib, GObject


class DummySettingsSchemaSource:
    @staticmethod
    def get_default():
        return None


GObject.Object = type('GObject', (object,), {})
Gio.SettingsSchemaSource = DummySettingsSchemaSource
GLib.get_user_config_dir = lambda: os.path.join(os.environ.get("HOME", ""), ".config")


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


def test_rename_tag_across_connections(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, monkeypatch)
    cfg.set_connection_tags('a', ['staging', 'web'])
    cfg.set_connection_tags('b', ['Staging'])      # case-insensitive match
    cfg.set_connection_tags('c', ['db'])           # untouched
    count = cfg.rename_tag('staging', 'prod')
    assert count == 2
    assert cfg.get_connection_tags('a') == ['prod', 'web']
    assert cfg.get_connection_tags('b') == ['prod']
    assert cfg.get_connection_tags('c') == ['db']


def test_rename_tag_merges_and_dedups(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, monkeypatch)
    cfg.set_connection_tags('a', ['staging', 'prod'])
    assert cfg.rename_tag('staging', 'prod') == 1
    assert cfg.get_connection_tags('a') == ['prod']


def test_rename_tag_preserves_other_meta(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, monkeypatch)
    cfg.pin_connection('a')
    cfg.set_connection_tags('a', ['staging'])
    cfg.rename_tag('staging', 'prod')
    assert cfg.is_pinned('a') is True
    assert cfg.get_connection_tags('a') == ['prod']


def test_rename_tag_case_only(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, monkeypatch)
    cfg.set_connection_tags('a', ['prod'])
    assert cfg.rename_tag('prod', 'Prod') == 1
    assert cfg.get_connection_tags('a') == ['Prod']


def test_get_all_tags_distinct_sorted_with_counts(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, monkeypatch)
    cfg.set_connection_tags('a', ['web', 'Prod'])
    cfg.set_connection_tags('b', ['prod', 'db'])
    assert cfg.get_all_tags() == [('db', 1), ('Prod', 2), ('web', 1)]


def test_get_all_tags_empty(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, monkeypatch)
    assert cfg.get_all_tags() == []


def test_rename_tag_rejects_empty_new_name(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, monkeypatch)
    cfg.set_connection_tags('a', ['staging'])
    assert cfg.rename_tag('staging', '   ') == 0
    assert cfg.get_connection_tags('a') == ['staging']

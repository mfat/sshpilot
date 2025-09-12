import sys
import types

# Ensure Gio stub for Config import
if 'gi.repository' in sys.modules:
    repo = sys.modules['gi.repository']
    if not hasattr(repo, 'Gio'):
        class DummySettingsSchemaSource:
            @staticmethod
            def get_default():
                return None
        repo.Gio = types.SimpleNamespace(SettingsSchemaSource=DummySettingsSchemaSource)
        sys.modules['gi.repository.Gio'] = repo.Gio

from sshpilot.groups import GroupManager


class DummyConfig:
    def __init__(self):
        self.settings = {}

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


def test_rename_connection_preserves_group():
    cfg = DummyConfig()
    gm = GroupManager(cfg)
    gid = gm.create_group("Test")
    gm.move_connection("old", gid)
    assert gm.get_connection_group("old") == gid

    gm.rename_connection("old", "new")

    assert gm.get_connection_group("new") == gid
    assert "new" in gm.groups[gid]['connections']
    assert "old" not in gm.groups[gid]['connections']

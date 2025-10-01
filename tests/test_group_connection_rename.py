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


def test_rename_connection_cleans_stale_root_entries():
    cfg = DummyConfig()
    gm = GroupManager(cfg)
    gid = gm.create_group("Test")
    gm.move_connection("old", gid)

    # Simulate a leftover root entry for the connection
    gm.root_connections.append("old")

    gm.rename_connection("old", "new")

    # Simulate application restart by reloading from saved config
    cfg2 = DummyConfig()
    cfg2.settings = cfg.settings.copy()
    gm2 = GroupManager(cfg2)

    assert gm2.root_connections == []
    assert gm2.groups[gid]['connections'] == ["new"]


def test_group_color_defaults_and_updates():
    cfg = DummyConfig()
    gm = GroupManager(cfg)

    gid = gm.create_group("Colorful", color="#ff0000ff")
    assert gm.groups[gid]['color'] == "#ff0000ff"

    gm.set_group_color(gid, None)
    assert gm.groups[gid]['color'] is None

    gm.set_group_color(gid, "#123456ff")
    hierarchy = gm.get_group_hierarchy()
    assert hierarchy[0]['color'] == "#123456ff"

    all_groups = gm.get_all_groups()
    assert any(group['color'] == "#123456ff" for group in all_groups)


def test_load_groups_backfills_missing_color():
    legacy_group_id = "legacy"
    cfg = DummyConfig()
    cfg.set_setting('connection_groups', {
        'groups': {
            legacy_group_id: {
                'id': legacy_group_id,
                'name': 'Legacy',
                'parent_id': None,
                'children': [],
                'connections': [],
                'expanded': True,
                'order': 0,
            }
        },
        'connections': {},
        'root_connections': [],
    })

    gm = GroupManager(cfg)
    assert gm.groups[legacy_group_id]['color'] is None


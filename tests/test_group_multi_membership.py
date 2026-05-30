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


def test_copy_connection_adds_to_multiple_groups():
    gm = GroupManager(DummyConfig())
    a = gm.create_group("A")
    b = gm.create_group("B")

    gm.move_connection("srv", a)
    gm.copy_connection_to_group("srv", b)

    assert set(gm.get_connection_groups("srv")) == {a, b}
    assert "srv" in gm.groups[a]['connections']
    assert "srv" in gm.groups[b]['connections']
    # Primary group stays the original one
    assert gm.get_connection_group("srv") == a
    assert "srv" not in gm.root_connections


def test_resolve_display_group_id_uses_context_after_copy():
    gm = GroupManager(DummyConfig())
    a = gm.create_group("A")
    b = gm.create_group("B")

    gm.move_connection("srv", a)
    gm.copy_connection_to_group("srv", b)

    assert gm.get_connection_group("srv") == a
    assert gm.resolve_display_group_id("srv", a) == a
    assert gm.resolve_display_group_id("srv", b) == b
    assert gm.resolve_display_group_id("srv", None) == a


def test_copy_ungrouped_connection_sets_primary():
    gm = GroupManager(DummyConfig())
    a = gm.create_group("A")

    gm.move_connection("srv", None)
    assert "srv" in gm.root_connections

    gm.copy_connection_to_group("srv", a)

    assert gm.get_connection_groups("srv") == [a]
    assert gm.get_connection_group("srv") == a
    assert "srv" not in gm.root_connections


def test_copy_is_idempotent():
    gm = GroupManager(DummyConfig())
    a = gm.create_group("A")
    gm.copy_connection_to_group("srv", a)
    gm.copy_connection_to_group("srv", a)
    assert gm.groups[a]['connections'].count("srv") == 1


def test_remove_from_one_group_keeps_others():
    gm = GroupManager(DummyConfig())
    a = gm.create_group("A")
    b = gm.create_group("B")
    gm.move_connection("srv", a)
    gm.copy_connection_to_group("srv", b)

    gm.remove_connection_from_group("srv", a)

    assert gm.get_connection_groups("srv") == [b]
    # Primary repointed to remaining group
    assert gm.get_connection_group("srv") == b
    assert "srv" not in gm.root_connections


def test_remove_from_last_group_returns_to_root():
    gm = GroupManager(DummyConfig())
    a = gm.create_group("A")
    gm.move_connection("srv", a)

    gm.remove_connection_from_group("srv", a)

    assert gm.get_connection_groups("srv") == []
    assert gm.get_connection_group("srv") is None
    assert "srv" in gm.root_connections


def test_rename_preserves_multiple_memberships():
    gm = GroupManager(DummyConfig())
    a = gm.create_group("A")
    b = gm.create_group("B")
    gm.move_connection("old", a)
    gm.copy_connection_to_group("old", b)

    gm.rename_connection("old", "new")

    assert set(gm.get_connection_groups("new")) == {a, b}
    assert gm.get_connection_groups("old") == []


def test_delete_group_keeps_connection_in_other_group():
    gm = GroupManager(DummyConfig())
    a = gm.create_group("A")
    b = gm.create_group("B")
    gm.move_connection("srv", a)
    gm.copy_connection_to_group("srv", b)

    gm.delete_group(a)

    assert gm.get_connection_groups("srv") == [b]
    assert "srv" not in gm.root_connections


def test_delete_only_group_moves_connection_to_root():
    gm = GroupManager(DummyConfig())
    a = gm.create_group("A")
    gm.move_connection("srv", a)

    gm.delete_group(a)

    assert gm.get_connection_groups("srv") == []
    assert "srv" in gm.root_connections


def test_multi_membership_survives_reload():
    cfg = DummyConfig()
    gm = GroupManager(cfg)
    a = gm.create_group("A")
    b = gm.create_group("B")
    gm.move_connection("srv", a)
    gm.copy_connection_to_group("srv", b)

    cfg2 = DummyConfig()
    cfg2.settings = cfg.settings.copy()
    gm2 = GroupManager(cfg2)

    assert set(gm2.get_connection_groups("srv")) == {a, b}
    assert "srv" not in gm2.root_connections

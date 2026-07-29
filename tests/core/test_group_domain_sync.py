"""GroupManager ↔ ConnectionService sync tests."""
from __future__ import annotations

from sshpilot.core.connections import ConnectionService
from sshpilot.groups import GroupManager


class _Cfg:
    def __init__(self):
        self._data = {}

    def get_setting(self, key, default=None):
        return self._data.get(key, default)

    def set_setting(self, key, value):
        self._data[key] = value


class _Conn:
    def __init__(self, uuid, nickname):
        self.uuid = uuid
        self.nickname = nickname
        self.hostname = "h.example"
        self.username = "u"
        self.port = 22
        self.protocol = "ssh"
        self.data = {
            "uuid": uuid,
            "nickname": nickname,
            "hostname": "h.example",
            "username": "u",
            "port": 22,
        }


class _CM:
    def __init__(self, domain, connections):
        self.domain = domain
        self.connections = connections

    def get_connections(self):
        return self.connections

    def find_connection_by_nickname(self, nick):
        for c in self.connections:
            if c.nickname == nick:
                return c
        return None


def test_move_connection_updates_domain_group_id():
    domain = ConnectionService(autosave=False)
    conn = _Conn("11111111-1111-4111-8111-111111111111", "Demo")
    domain.create(
        {
            "uuid": conn.uuid,
            "nickname": conn.nickname,
            "hostname": "h.example",
            "username": "u",
            "port": 22,
        }
    )
    cm = _CM(domain, [conn])
    gm = GroupManager(_Cfg(), connection_manager=cm)
    gid = gm.create_group("Prod")
    assert domain.get(gid) is None or domain.list_groups()
    assert any(g.id == gid for g in domain.list_groups())
    gm.move_connection(conn.nickname, gid)
    record = domain.get(conn.uuid)
    assert record is not None
    assert record.group_id == gid
    gm.move_connection(conn.nickname, None)
    assert domain.get(conn.uuid).group_id is None


def test_sync_domain_from_live_includes_groups():
    from sshpilot.connection_manager import ConnectionManager

    # Exercise the payload builder without full SSH init by calling the method
    # on a minimal stand-in that shares the implementation via type.
    domain = ConnectionService(autosave=False)
    conn = _Conn("22222222-2222-4222-8222-222222222222", "SyncMe")
    cm = ConnectionManager.__new__(ConnectionManager)
    cm._domain = domain
    cm.connections = [conn]
    gm = GroupManager(_Cfg(), connection_manager=_CM(domain, [conn]))
    gid = gm.create_group("Team")
    gm.move_connection(conn.uuid, gid)
    cm.group_manager = gm
    cm.sync_domain_from_live_connections(gm)
    assert domain.get(conn.uuid).group_id == gid
    assert any(g.id == gid for g in domain.list_groups())

"""Tests for the plugin group API: ctx.create_group (find-or-create),
add_connection_to_group, and the add_connection_group convenience."""

import os
import sys


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.groups import GroupManager  # noqa: E402
from sshpilot.plugins import registry as registry_mod  # noqa: E402
from sshpilot.plugins.api import PluginContext  # noqa: E402
from sshpilot.plugins.host import ConnectionInfo, PluginHost  # noqa: E402


class FakeConfig:
    def __init__(self):
        self.settings = {}

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


class FakeConnection:
    def __init__(self, nickname):
        self.nickname = nickname
        self.hostname = nickname
        self.host = nickname
        self.username = ""
        self.protocol = "easyenv"
        self.port = 22
        self.data = {"nickname": nickname, "protocol": "easyenv"}


class FakeCM:
    def __init__(self):
        self.connections = []

    def connect_after(self, *a, **k):
        return 0

    def add_connection_from_data(self, data):
        nick = data.get("nickname") or data.get("host")
        if any(c.nickname == nick for c in self.connections):
            raise ValueError(f"A connection named {nick!r} already exists.")
        c = FakeConnection(nick)
        c.data = dict(data)
        self.connections.append(c)
        return c

    def find_connection_by_nickname(self, nickname):
        return next((c for c in self.connections if c.nickname == nickname), None)


class FakeWindow:
    def __init__(self, cm):
        self.connection_manager = cm
        self.group_manager = GroupManager(FakeConfig())
        self.rebuilds = 0

    def rebuild_connection_list(self):
        self.rebuilds += 1


def _ctx(host, cm):
    return PluginContext(plugin_id="easyenv-workspaces", app_config=FakeConfig(),
                         connection_manager=cm,
                         protocol_registry=registry_mod.ProtocolRegistry(),
                         host=host)


def _bound():
    cm = FakeCM()
    host = PluginHost(connection_manager=cm)
    window = FakeWindow(cm)
    host.bind_window(window)
    return _ctx(host, cm), cm, host, window


def test_create_group_is_find_or_create():
    ctx, _cm, _host, window = _bound()
    gid1 = ctx.create_group("EasyEnv: cluster")
    gid2 = ctx.create_group("easyenv: CLUSTER")  # case-insensitive match
    assert gid1 is not None and gid1 == gid2
    assert len(window.group_manager.get_all_groups()) == 1


def test_add_connection_to_group_moves_and_rebuilds():
    ctx, cm, _host, window = _bound()
    gid = ctx.create_group("g1")
    cm.add_connection_from_data({"nickname": "n1", "protocol": "easyenv"})
    before = window.rebuilds
    assert ctx.add_connection_to_group("n1", gid) is True
    assert "n1" in window.group_manager.groups[gid]["connections"]
    assert window.rebuilds > before


def test_add_connection_to_unknown_group_returns_false():
    ctx, _cm, _host, _window = _bound()
    assert ctx.add_connection_to_group("n1", "nonexistent-group-id") is False


def test_add_connection_group_builds_group_of_n():
    ctx, _cm, _host, window = _bound()
    nodes = [{"nickname": f"cluster/node-{i}", "protocol": "easyenv",
              "workspace_id": "ws-1", "machine_id": f"m-{i}"} for i in range(4)]
    gid, infos = ctx.add_connection_group("EasyEnv: cluster", nodes)
    assert gid is not None
    assert len(infos) == 4 and all(isinstance(i, ConnectionInfo) for i in infos)
    assert len(window.group_manager.groups[gid]["connections"]) == 4


def test_add_connection_group_idempotent_on_reprovision():
    ctx, _cm, _host, window = _bound()
    nodes = [{"nickname": "cluster/a", "protocol": "easyenv", "machine_id": "m-a"},
             {"nickname": "cluster/b", "protocol": "easyenv", "machine_id": "m-b"}]
    gid1, infos1 = ctx.add_connection_group("EasyEnv: cluster", nodes)
    gid2, infos2 = ctx.add_connection_group("EasyEnv: cluster", nodes)
    assert gid1 == gid2  # same group reused
    assert len(window.group_manager.get_all_groups()) == 1
    assert len(window.group_manager.groups[gid1]["connections"]) == 2  # no dupes
    assert len(infos2) == 2  # existing connections reused, returned


def test_group_api_safe_without_window():
    cm = FakeCM()
    host = PluginHost(connection_manager=cm)  # not bound
    ctx = _ctx(host, cm)
    assert ctx.create_group("g") is None
    assert ctx.add_connection_to_group("n", "g") is False
    gid, infos = ctx.add_connection_group("g", [{"nickname": "n", "protocol": "easyenv"}])
    assert gid is None
    # the connection is still created even if it can't be grouped
    assert len(infos) == 1

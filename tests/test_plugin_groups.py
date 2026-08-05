"""Tests for the plugin group API: ``ctx.create_group`` (find-or-create),
``add_connection_to_group``, and the ``add_connection_group`` convenience.

Groups are daemon-owned. ``GroupManager`` is only a snapshot-backed
presentation adapter, so the tests drive a narrowly shaped daemon-client fake
that owns the group state and a snapshot projection the adapter renders from.
No local synchronous group persistence is expected.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.connection_model import Connection  # noqa: E402
from sshpilot.groups import GroupManager  # noqa: E402
from sshpilot.plugins import registry as registry_mod  # noqa: E402
from sshpilot.plugins.api import PluginContext  # noqa: E402
from sshpilot.plugins.host import ConnectionInfo, PluginHost  # noqa: E402
from sshpilot.api.models.connection_store import (  # noqa: E402
    ConnectionStoreSnapshot,
    GroupSummary,
)
from sshpilot.api.models.connections import (  # noqa: E402
    ConnectionId,
    ConnectionSummary,
)


class FakeConfig:
    def __init__(self):
        self.settings = {}

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


class FakeCM:
    """Plugin connection surface (daemon-backed contract)."""

    def __init__(self):
        self.connections = []

    def connect_after(self, *a, **k):
        return 0

    def add_connection_from_data(self, data):
        values = dict(data)
        nick = str(values.get("nickname") or values.get("host") or "").strip()
        if any(c.nickname == nick for c in self.connections):
            raise ValueError(f"A connection named {nick!r} already exists.")
        conn = Connection(values)
        self.connections.append(conn)
        return conn

    def find_connection_by_nickname(self, nickname):
        return next((c for c in self.connections if c.nickname == nickname), None)


class SnapshotProjection:
    """Read-only snapshot holder standing in for ConnectionPresentationStore."""

    def __init__(self, client, cm):
        self._client = client
        self._cm = cm
        self._snapshot = None

    def snapshot(self):
        if self._snapshot is None:
            self._rebuild()
        return self._snapshot

    def connect_after(self, *a, **k):
        return 0

    def invalidate(self):
        self._snapshot = None

    def _rebuild(self):
        connections = tuple(
            ConnectionSummary(
                id=ConnectionId(c.nickname),
                nickname=c.nickname,
                host=getattr(c, "host", "") or c.nickname,
                hostname=getattr(c, "hostname", "") or "",
                username=getattr(c, "username", "") or "",
                port=int(getattr(c, "port", 22) or 22),
                protocol=getattr(c, "protocol", "ssh") or "ssh",
            )
            for c in self._cm.connections
        )
        known = {c.id for c in connections}
        groups = tuple(
            GroupSummary(
                id=gid,
                name=g["name"],
                color=g.get("color") or "",
                connection_ids=tuple(
                    cid for cid in g["connections"] if cid in known
                ),
            )
            for gid, g in sorted(self._client.groups.items())
        )
        grouped = {cid for g in groups for cid in g.connection_ids}
        root = tuple(
            c.id for c in connections
            if c.id not in grouped
        )
        self._snapshot = ConnectionStoreSnapshot(
            generation=0,
            connections=connections,
            groups=groups,
            root_connection_ids=root,
            metadata=(),
        )


class FakeGroupClient:
    """Narrow daemon-client fake for the group RPC surface GroupManager uses."""

    def __init__(self, cm):
        self.cm = cm
        self.groups = {}
        self._seq = 0
        self._projection = None

    def attach_projection(self, projection):
        self._projection = projection

    def _publish(self):
        if self._projection is not None:
            self._projection.invalidate()

    def create_group(self, name, parent_id="", color=""):
        gid = f"g{self._seq}"
        self._seq += 1
        self.groups[gid] = {
            "id": gid, "name": str(name), "parent_id": parent_id or None,
            "color": color or "", "connections": [],
        }
        self._publish()
        return gid

    def assign_connection_to_group(self, connection_id, group_id):
        for group in self.groups.values():
            if connection_id in group["connections"]:
                group["connections"].remove(connection_id)
        if group_id in self.groups and connection_id not in self.groups[group_id]["connections"]:
            self.groups[group_id]["connections"].append(connection_id)
        self._publish()
        return self.cm.find_connection_by_nickname(connection_id)

    def copy_connection_to_group(self, request):
        group = self.groups.get(request.group_id)
        if group is not None and request.connection_id not in group["connections"]:
            group["connections"].append(request.connection_id)
        self._publish()
        return True


class FakeWindow:
    def __init__(self, cm, gm):
        self.connection_manager = cm
        self.group_manager = gm
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
    client = FakeGroupClient(cm)
    projection = SnapshotProjection(client, cm)
    client.attach_projection(projection)
    gm = GroupManager(
        config=FakeConfig(),
        connection_manager=projection,
        client=client,
    )
    host = PluginHost(connection_manager=cm)
    window = FakeWindow(cm, gm)
    host.bind_window(window)
    return _ctx(host, cm), cm, host, window, gm


def test_create_group_is_find_or_create():
    ctx, _cm, _host, _window, gm = _bound()
    gid1 = ctx.create_group("EasyEnv: cluster")
    gid2 = ctx.create_group("easyenv: CLUSTER")  # case-insensitive match
    assert gid1 is not None and gid1 == gid2
    assert len(gm.get_all_groups()) == 1


def test_add_connection_to_group_moves_connection():
    ctx, cm, _host, _window, gm = _bound()
    gid = ctx.create_group("g1")
    cm.add_connection_from_data({"nickname": "n1", "protocol": "easyenv"})
    assert ctx.add_connection_to_group("n1", gid) is True
    assert "n1" in gm.groups[gid]["connections"]


def test_add_connection_to_unknown_group_returns_false():
    ctx, _cm, _host, _window, _gm = _bound()
    assert ctx.add_connection_to_group("n1", "nonexistent-group-id") is False


def test_add_connection_group_builds_group_of_n():
    ctx, _cm, _host, _window, gm = _bound()
    nodes = [{"nickname": f"cluster/node-{i}", "protocol": "easyenv",
              "workspace_id": "ws-1", "machine_id": f"m-{i}"} for i in range(4)]
    gid, infos = ctx.add_connection_group("EasyEnv: cluster", nodes)
    assert gid is not None
    assert len(infos) == 4 and all(isinstance(i, ConnectionInfo) for i in infos)
    assert len(gm.groups[gid]["connections"]) == 4


def test_add_connection_group_idempotent_on_reprovision():
    ctx, _cm, _host, _window, gm = _bound()
    nodes = [{"nickname": "cluster/a", "protocol": "easyenv", "machine_id": "m-a"},
             {"nickname": "cluster/b", "protocol": "easyenv", "machine_id": "m-b"}]
    gid1, infos1 = ctx.add_connection_group("EasyEnv: cluster", nodes)
    gid2, infos2 = ctx.add_connection_group("EasyEnv: cluster", nodes)
    assert gid1 == gid2  # same group reused
    assert len(gm.get_all_groups()) == 1
    assert len(gm.groups[gid1]["connections"]) == 2  # no dupes
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


def test_resolve_display_group_id_uses_context_after_copy():
    """GroupManager (snapshot-backed adapter) resolves the display group from
    the sidebar's context when the connection belongs to several groups."""
    ctx, cm, _host, _window, gm = _bound()
    gid_a = ctx.create_group("A")
    gid_b = ctx.create_group("B")
    cm.add_connection_from_data({"nickname": "srv", "protocol": "easyenv"})
    assert ctx.add_connection_to_group("srv", gid_a) is True
    gm.copy_connection_to_group("srv", gid_b)

    assert gm.resolve_display_group_id("srv", gid_a) == gid_a
    assert gm.resolve_display_group_id("srv", gid_b) == gid_b
    assert gm.resolve_display_group_id("srv", None) == gid_a

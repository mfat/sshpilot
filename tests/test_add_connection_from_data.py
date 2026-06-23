"""ConnectionManager.add_connection_from_data — the programmatic creation
path exposed to plugins via PluginContext.add_connection."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.connection_manager import Connection, ConnectionManager
from sshpilot.plugins import registry as registry_mod
from sshpilot.plugins.api import PluginContext


class FakeConfig:
    def __init__(self):
        self.settings = {}

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch):
    monkeypatch.setattr(registry_mod, "_registry", None)
    from sshpilot.plugins.builtin.ssh_protocol import Plugin
    ctx = PluginContext(plugin_id="ssh", app_config=None, connection_manager=None,
                        protocol_registry=registry_mod.protocol_registry())
    Plugin().activate(ctx)


@pytest.fixture
def manager(tmp_path):
    cm = ConnectionManager.__new__(ConnectionManager)
    cm.config = FakeConfig()
    cm.connections = []
    cm.rules = []
    cm.ssh_config = {}
    cm.isolated_mode = False
    cm.ssh_config_path = str(tmp_path / 'ssh_config')
    cm.known_hosts_path = str(tmp_path / 'known_hosts')
    with open(cm.ssh_config_path, 'w', encoding='utf-8') as fh:
        fh.write("# empty\n")
    cm.emitted = []
    cm.emit = lambda *args: cm.emitted.append(args)
    cm.store_password = lambda *a, **k: True
    cm.delete_password = lambda *a, **k: True
    return cm


def test_creates_and_persists_ssh_connection(manager):
    conn = manager.add_connection_from_data({
        'nickname': 'newhost',
        'hostname': 'new.example.com',
        'username': 'root',
        'port': 2222,
    })
    assert isinstance(conn, Connection)
    assert conn in manager.connections
    assert conn.protocol == 'ssh'

    content = open(manager.ssh_config_path, encoding='utf-8').read()
    assert 'Host newhost' in content
    assert 'new.example.com' in content
    assert ('connection-updated', conn) in manager.emitted


def test_password_routed_to_keyring(manager):
    stored = []
    manager.store_password = lambda host, user, pw: stored.append((host, user, pw)) or True
    manager.add_connection_from_data({
        'nickname': 'pwhost',
        'hostname': 'pw.example.com',
        'username': 'admin',
        'password': 'hunter2',
    })
    assert stored == [('pw.example.com', 'admin', 'hunter2')]


def test_duplicate_nickname_rejected_and_list_unpolluted(manager):
    manager.add_connection_from_data({'nickname': 'dup', 'hostname': 'a.example.com'})
    before = list(manager.connections)
    with pytest.raises(ValueError, match='already exists'):
        manager.add_connection_from_data({'nickname': 'dup', 'hostname': 'b.example.com'})
    assert manager.connections == before


def test_missing_everything_rejected(manager):
    with pytest.raises(ValueError):
        manager.add_connection_from_data({})
    assert manager.connections == []


def test_unknown_protocol_rejected(manager):
    with pytest.raises(ValueError, match='Unknown protocol'):
        manager.add_connection_from_data({'nickname': 'x', 'protocol': 'gopher'})


def test_plugin_context_add_connection_delegates(manager):
    ctx = PluginContext(plugin_id="mock-vps", app_config=manager.config,
                        connection_manager=manager,
                        protocol_registry=registry_mod.protocol_registry())
    info = ctx.add_connection({'nickname': 'viactx', 'hostname': 'ctx.example.com'})
    # add_connection returns a stable ConnectionInfo, not the internal object.
    assert info.nickname == 'viactx'
    assert any(c.nickname == 'viactx' for c in manager.connections)

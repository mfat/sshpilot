"""Plugin-protocol connections persist as JSON in the app config
('connections.non_ssh'), never in ~/.ssh/config."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.connection_manager import Connection, ConnectionManager


class FakeConfig:
    def __init__(self):
        self.settings = {}

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


SSH_CONFIG = """Host alpha
    HostName alpha.example.com
    User root
"""


@pytest.fixture
def manager(tmp_path):
    # ConnectionManager subclasses the stubbed GObject.Object, whose dummy
    # metaclass hijacks normal construction — build the instance by hand.
    cm = ConnectionManager.__new__(ConnectionManager)
    cm.config = FakeConfig()
    cm.connections = []
    cm.rules = []
    cm.ssh_config = {}
    cm.isolated_mode = False
    cm.ssh_config_path = str(tmp_path / 'ssh_config')
    cm.known_hosts_path = str(tmp_path / 'known_hosts')
    with open(cm.ssh_config_path, 'w', encoding='utf-8') as fh:
        fh.write(SSH_CONFIG)
    cm.emitted = []
    cm.emit = lambda *args: cm.emitted.append(args)
    cm.store_password = lambda *a, **k: True
    cm.delete_password = lambda *a, **k: True
    return cm


def _telnet_data(nickname='lab-switch'):
    return {'nickname': nickname, 'protocol': 'telnet',
            'host': '10.0.0.5', 'port': 2323}


def test_update_creates_json_entry_and_leaves_ssh_config_alone(manager):
    before = open(manager.ssh_config_path, encoding='utf-8').read()

    conn = Connection(_telnet_data())
    assert manager.update_connection(conn, _telnet_data()) is True

    stored = manager.config.settings['connections.non_ssh']
    assert len(stored) == 1
    assert stored[0]['nickname'] == 'lab-switch'
    assert stored[0]['protocol'] == 'telnet'
    assert conn in manager.connections
    assert ('connection-updated', conn) in manager.emitted

    after = open(manager.ssh_config_path, encoding='utf-8').read()
    assert before == after


def test_password_goes_to_keyring_not_json(manager):
    stored_pw = []
    manager.store_password = lambda host, user, pw: stored_pw.append((host, user, pw)) or True

    data = _telnet_data()
    data['password'] = 'hunter2'
    conn = Connection(dict(data))
    assert manager.update_connection(conn, data) is True

    entry = manager.config.settings['connections.non_ssh'][0]
    assert 'password' not in entry
    assert stored_pw == [('10.0.0.5', '', 'hunter2')]


def test_reload_preserves_object_identity(manager):
    conn = Connection(_telnet_data())
    manager.update_connection(conn, _telnet_data())

    manager.load_ssh_config()
    by_nickname = {c.nickname: c for c in manager.connections}
    assert 'alpha' in by_nickname  # the ssh_config host still loads
    assert by_nickname.get('lab-switch') is conn  # identity preserved

    # And once more, with a fresh manager list (simulating app restart).
    manager.connections = []
    manager.load_ssh_config()
    reloaded = next(c for c in manager.connections if c.nickname == 'lab-switch')
    assert reloaded is not conn
    assert reloaded.protocol == 'telnet'
    assert reloaded.data['host'] == '10.0.0.5'


def test_rename_updates_json_entry(manager):
    conn = Connection(_telnet_data())
    manager.update_connection(conn, _telnet_data())

    new_data = _telnet_data(nickname='renamed-switch')
    assert manager.update_connection(conn, new_data) is True

    stored = manager.config.settings['connections.non_ssh']
    assert [e['nickname'] for e in stored] == ['renamed-switch']
    assert conn.nickname == 'renamed-switch'


def test_remove_connection_cleans_json(manager):
    conn = Connection(_telnet_data())
    manager.update_connection(conn, _telnet_data())
    assert manager.config.settings['connections.non_ssh']

    assert manager.remove_connection(conn) is True
    assert manager.config.settings['connections.non_ssh'] == []
    assert all(c.nickname != 'lab-switch' for c in manager.connections)


def test_ssh_connections_never_enter_json(manager):
    manager.load_ssh_config()
    manager._persist_non_ssh_connections()
    assert manager.config.settings.get('connections.non_ssh', []) == []

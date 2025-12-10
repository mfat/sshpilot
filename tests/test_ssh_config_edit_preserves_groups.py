"""Test that editing Host entries in SSH config preserves group membership."""

import sys
import types
import asyncio

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

# Ensure an event loop for Connection objects
asyncio.set_event_loop(asyncio.new_event_loop())

from sshpilot.connection_manager import ConnectionManager, Connection
from sshpilot.groups import GroupManager


class DummyConfig:
    def __init__(self):
        self.settings = {}

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


def test_edit_host_preserves_group_membership(tmp_path):
    """Test that editing a Host entry in SSH config preserves group membership."""
    # Create SSH config file with a connection
    config_path = tmp_path / "config"
    config_path.write_text("""Host old-server
    HostName example.com
    User alice
    Port 22
""")

    # Set up connection manager
    cm = ConnectionManager.__new__(ConnectionManager)
    cm.connections = []
    cm.ssh_config_path = str(config_path)
    cm.known_hosts_path = str(tmp_path / "known_hosts")
    cm.emit = lambda *args, **kwargs: None
    cm.store_password = lambda *args, **kwargs: True
    cm.delete_password = lambda *args, **kwargs: True
    
    # Load initial config
    cm.load_ssh_config()
    
    # Verify connection exists
    assert len(cm.connections) == 1
    old_conn = cm.connections[0]
    assert old_conn.nickname == "old-server"
    assert old_conn.hostname == "example.com"
    assert old_conn.username == "alice"
    assert old_conn.port == 22
    
    # Set up group manager and add connection to a group
    cfg = DummyConfig()
    gm = GroupManager(cfg)
    group_id = gm.create_group("Production Servers")
    gm.move_connection("old-server", group_id)
    
    # Verify connection is in the group
    assert gm.get_connection_group("old-server") == group_id
    assert "old-server" in gm.groups[group_id]['connections']
    
    # Simulate editing the SSH config file: change Host from "old-server" to "new-server"
    config_path.write_text("""Host new-server
    HostName example.com
    User alice
    Port 22
""")
    
    # Simulate the _reload_ssh_config() logic
    # Capture current connections and their group memberships before reload
    old_connections = {conn.nickname: conn for conn in cm.get_connections()}
    old_group_memberships = {}
    for nickname in old_connections.keys():
        group_id_for_nickname = gm.get_connection_group(nickname)
        if group_id_for_nickname:
            old_group_memberships[nickname] = group_id_for_nickname
    
    # Reload SSH config (this creates new Connection objects)
    cm.load_ssh_config()
    new_connections = {conn.nickname: conn for conn in cm.get_connections()}
    
    # Verify new connection exists with new nickname
    assert len(new_connections) == 1
    assert "new-server" in new_connections
    new_conn = new_connections["new-server"]
    assert new_conn.hostname == "example.com"
    assert new_conn.username == "alice"
    assert new_conn.port == 22
    
    # Detect nickname changes by matching connections on hostname/username/port
    # This handles the case where Host value changes but connection is otherwise the same
    for old_nickname, old_conn in old_connections.items():
        if old_nickname in old_group_memberships:
            # This connection was in a group, try to find its new nickname
            group_id = old_group_memberships[old_nickname]
            
            # Try to find matching connection by hostname/username/port
            matching_new_nickname = None
            for new_nickname, new_conn in new_connections.items():
                if (new_conn.hostname == old_conn.hostname and
                    new_conn.username == old_conn.username and
                    new_conn.port == old_conn.port):
                    matching_new_nickname = new_nickname
                    break
            
            # If we found a match and nickname changed, update group membership
            if matching_new_nickname and matching_new_nickname != old_nickname:
                gm.rename_connection(old_nickname, matching_new_nickname)
    
    # Verify group membership was preserved
    assert gm.get_connection_group("new-server") == group_id
    assert "new-server" in gm.groups[group_id]['connections']
    assert "old-server" not in gm.groups[group_id]['connections']
    assert gm.get_connection_group("old-server") is None


def test_edit_host_preserves_group_multiple_connections(tmp_path):
    """Test that editing one Host entry doesn't affect other connections in the same group."""
    # Create SSH config file with multiple connections
    config_path = tmp_path / "config"
    config_path.write_text("""Host server1
    HostName server1.example.com
    User alice
    Port 22

Host server2
    HostName server2.example.com
    User bob
    Port 22
""")

    # Set up connection manager
    cm = ConnectionManager.__new__(ConnectionManager)
    cm.connections = []
    cm.ssh_config_path = str(config_path)
    cm.known_hosts_path = str(tmp_path / "known_hosts")
    cm.emit = lambda *args, **kwargs: None
    cm.store_password = lambda *args, **kwargs: True
    cm.delete_password = lambda *args, **kwargs: True
    
    # Load initial config
    cm.load_ssh_config()
    
    # Verify both connections exist
    assert len(cm.connections) == 2
    
    # Set up group manager and add both connections to a group
    cfg = DummyConfig()
    gm = GroupManager(cfg)
    group_id = gm.create_group("Web Servers")
    gm.move_connection("server1", group_id)
    gm.move_connection("server2", group_id)
    
    # Verify both connections are in the group
    assert gm.get_connection_group("server1") == group_id
    assert gm.get_connection_group("server2") == group_id
    assert len(gm.groups[group_id]['connections']) == 2
    
    # Edit only server1's Host name
    config_path.write_text("""Host server1-renamed
    HostName server1.example.com
    User alice
    Port 22

Host server2
    HostName server2.example.com
    User bob
    Port 22
""")
    
    # Simulate the _reload_ssh_config() logic
    old_connections = {conn.nickname: conn for conn in cm.get_connections()}
    old_group_memberships = {}
    for nickname in old_connections.keys():
        group_id_for_nickname = gm.get_connection_group(nickname)
        if group_id_for_nickname:
            old_group_memberships[nickname] = group_id_for_nickname
    
    # Reload SSH config
    cm.load_ssh_config()
    new_connections = {conn.nickname: conn for conn in cm.get_connections()}
    
    # Detect and handle nickname changes
    for old_nickname, old_conn in old_connections.items():
        if old_nickname in old_group_memberships:
            group_id = old_group_memberships[old_nickname]
            
            matching_new_nickname = None
            for new_nickname, new_conn in new_connections.items():
                if (new_conn.hostname == old_conn.hostname and
                    new_conn.username == old_conn.username and
                    new_conn.port == old_conn.port):
                    matching_new_nickname = new_nickname
                    break
            
            if matching_new_nickname and matching_new_nickname != old_nickname:
                gm.rename_connection(old_nickname, matching_new_nickname)
    
    # Verify server1-renamed is in the group and server2 is still in the group
    assert gm.get_connection_group("server1-renamed") == group_id
    assert gm.get_connection_group("server2") == group_id
    assert "server1-renamed" in gm.groups[group_id]['connections']
    assert "server2" in gm.groups[group_id]['connections']
    assert "server1" not in gm.groups[group_id]['connections']
    assert len(gm.groups[group_id]['connections']) == 2



from pathlib import Path

from sshpilot.connection_manager import Connection, ConnectionManager


def test_update_connection_creates_ssh_directory(tmp_path):
    """Saving a new connection should create the SSH directory when missing."""
    ssh_dir = tmp_path / "custom_ssh"
    config_path = ssh_dir / "config"
    known_hosts_path = ssh_dir / "known_hosts"

    manager = ConnectionManager.__new__(ConnectionManager)
    manager.connections = []
    manager.rules = []
    manager.isolated_mode = False
    manager.ssh_config_path = str(config_path)
    manager.known_hosts_path = str(known_hosts_path)
    manager.emit = lambda *args, **kwargs: None
    manager.store_password = lambda *args, **kwargs: True
    manager.delete_password = lambda *args, **kwargs: True

    connection_data = {
        "nickname": "demo",
        "hostname": "example.com",
        "username": "alice",
        "port": 22,
        "auth_method": 0,
        "keyfile": "",
        "password": "",
        "forwarding_rules": [],
    }

    connection = Connection(connection_data)
    manager.connections.append(connection)

    assert not ssh_dir.exists()

    manager.update_connection(connection, dict(connection_data))

    assert ssh_dir.exists()
    assert config_path.exists()
    contents = Path(config_path).read_text()
    assert "Host demo" in contents


def test_update_connection_recovers_missing_config_path(monkeypatch, tmp_path):
    """Updating a connection rebuilds the default SSH config path when missing."""
    ssh_dir = tmp_path / ".ssh"
    default_config = ssh_dir / "config"
    monkeypatch.setenv("SSHPILOT_SSH_DIR", str(ssh_dir))

    manager = ConnectionManager.__new__(ConnectionManager)
    manager.connections = []
    manager.rules = []
    manager.isolated_mode = False
    manager.ssh_config_path = ""
    manager.known_hosts_path = ""
    manager.emit = lambda *args, **kwargs: None
    manager.store_password = lambda *args, **kwargs: True
    manager.delete_password = lambda *args, **kwargs: True

    connection_data = {
        "nickname": "demo",
        "hostname": "example.com",
        "username": "alice",
        "port": 22,
        "auth_method": 0,
        "keyfile": "",
        "password": "",
        "forwarding_rules": [],
    }

    connection = Connection(connection_data)
    manager.connections.append(connection)

    assert not ssh_dir.exists()

    manager.update_connection(connection, dict(connection_data))

    assert ssh_dir.exists()
    assert default_config.exists()
    assert Path(manager.ssh_config_path) == default_config
    assert connection.source == manager.ssh_config_path
    contents = default_config.read_text()
    assert "Host demo" in contents

from pathlib import Path

from sshpilot.core.connections.ssh_config_store import SshConfigStore


def test_update_connection_creates_ssh_directory(tmp_path):
    """Saving a new connection should create the SSH directory when missing."""
    ssh_dir = tmp_path / "custom_ssh"
    config_path = ssh_dir / "config"

    store = SshConfigStore(config_path)

    assert not ssh_dir.exists()

    result = store.create({
        "nickname": "demo",
        "hostname": "example.com",
        "username": "alice",
        "port": 22,
        "auth_method": 0,
        "keyfile": "",
        "forwarding_rules": [],
        "protocol": "ssh",
    })

    assert result.connection_id == "demo"
    assert ssh_dir.exists()
    assert config_path.exists()
    contents = Path(config_path).read_text()
    assert "Host demo" in contents

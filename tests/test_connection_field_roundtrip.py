"""Every supported connection field must survive format -> write -> reload."""


import pytest

from sshpilot.core.connections.ssh_config_loader import load_ssh_configuration
from sshpilot.ssh_config_formatter import format_ssh_config_entry


def _roundtrip(tmp_path, payload):
    entry = format_ssh_config_entry(dict(payload))
    path = tmp_path / "config"
    path.write_text("# SSH configuration file\n\n" + entry + "\n", encoding="utf-8")
    return load_ssh_configuration(path, isolated=False)


def _get(record, key):
    return record.data.get(key)


# name: (payload overrides, expected field values after reload)
CASES = {
    "basic": (
        {"port": 2222},
        {"hostname": "example.com", "username": "alice", "port": 2222},
    ),
    "default_port_omitted": (
        {"port": 22},
        {"port": 22},
    ),
    "alias_only_no_hostname": (
        {"hostname": ""},
        {"hostname": "", "host": "web"},
    ),
    "dedicated_key_identities_only": (
        {"keyfile": "/keys/id_ed25519", "key_select_mode": 1, "auth_method": 0},
        {"keyfile": "/keys/id_ed25519", "key_select_mode": 1, "auth_method": 0},
    ),
    "dedicated_key_try_agent_too": (
        {"keyfile": "/keys/id_ed25519", "key_select_mode": 2, "auth_method": 0},
        {"keyfile": "/keys/id_ed25519", "key_select_mode": 2},
    ),
    "multiple_identity_files": (
        {"keyfile": "/keys/k1", "identity_files": ["/keys/k1", "/keys/k2"],
         "key_select_mode": 1},
        {"identity_files": ["/keys/k1", "/keys/k2"]},
    ),
    "certificate": (
        {"keyfile": "/keys/k1", "key_select_mode": 1,
         "certificate": "/keys/cert.pub", "certificate_files": ["/keys/cert.pub"]},
        {"certificate": "/keys/cert.pub"},
    ),
    "identity_agent": (
        {"identity_agent": "/run/agent.sock"},
        {"identity_agent": "/run/agent.sock"},
    ),
    "add_keys_to_agent": (
        {"add_keys_to_agent": "confirm"},
        {"add_keys_to_agent": "confirm"},
    ),
    "proxy_jump": (
        {"proxy_jump": ["jump1", "jump2"]},
        {"proxy_jump": ["jump1", "jump2"]},
    ),
    "proxy_command": (
        {"proxy_command": "ssh -W %h:%p bastion"},
        {"proxy_command": "ssh -W %h:%p bastion"},
    ),
    "forward_agent": (
        {"forward_agent": True},
        {"forward_agent": True},
    ),
    "forward_agent_target": (
        {"forward_agent": True, "forward_agent_target": "$SSH_AUTH_SOCK"},
        {"forward_agent": True, "forward_agent_target": "$SSH_AUTH_SOCK"},
    ),
    "x11_forwarding": (
        {"x11_forwarding": True},
        {"x11_forwarding": True},
    ),
    "forwarding_rules": (
        {"forwarding_rules": [
            {"type": "local", "listen_addr": "localhost", "listen_port": 8080,
             "remote_host": "db.internal", "remote_port": 5432, "enabled": True},
            {"type": "remote", "listen_addr": "", "listen_port": 9000,
             "local_host": "localhost", "local_port": 3000, "enabled": True},
            {"type": "dynamic", "listen_addr": "localhost", "listen_port": 1080,
             "enabled": True},
        ]},
        {"forwarding_rules": [
            {"type": "local", "listen_addr": "localhost", "listen_port": 8080,
             "remote_host": "db.internal", "remote_port": 5432, "enabled": True},
            {"type": "remote", "listen_addr": "", "listen_port": 9000,
             "local_host": "localhost", "local_port": 3000, "enabled": True},
            {"type": "dynamic", "listen_addr": "localhost", "listen_port": 1080,
             "enabled": True},
        ]},
    ),
    "pre_command": (
        {"pre_command": "echo hi"},
        {"pre_command": "echo hi"},
    ),
    "local_command": (
        {"local_command": "notify-send connected"},
        {"local_command": "notify-send connected"},
    ),
    "remote_command_keeps_shell": (
        {"remote_command": "uptime"},
        {"remote_command": "uptime"},
    ),
    "request_tty_force_preserved": (
        {"request_tty": "force"},
        {"request_tty": "force"},
    ),
    "request_tty_yes": (
        {"request_tty": "yes"},
        {"request_tty": "yes"},
    ),
    "request_tty_no_preserved": (
        {"request_tty": "no"},
        {"request_tty": "no"},
    ),
    "request_tty_legacy_bool": (
        {"request_tty": True},
        {"request_tty": "yes"},
    ),
    "remote_command_keeps_authored_request_tty": (
        {"remote_command": "uptime", "request_tty": "force"},
        {"remote_command": "uptime", "request_tty": "force"},
    ),
    "extra_ssh_config": (
        {"extra_ssh_config": "Compression yes"},
        {"extra_ssh_config": "compression yes"},
    ),
    "password_auth": (
        {"auth_method": 1},
        {"auth_method": 1},
    ),
    "password_auth_no_pubkey": (
        {"auth_method": 1, "pubkey_auth_no": True},
        {"auth_method": 1, "pubkey_auth_no": True},
    ),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_field_roundtrip(tmp_path, name):
    overrides, expected = CASES[name]
    payload = {"nickname": "web", "hostname": "example.com", "username": "alice",
               **overrides}

    result = _roundtrip(tmp_path, payload)
    record = next(c for c in result.connections if c.id == "web")

    for key, want in expected.items():
        # Every supported model field must be a real attribute or parsed data
        # key, not just a key that happens to survive in record.data.
        got = _get(record, key)
        assert got == want, f"{name}.{key}: {got!r} != {want!r}"


def test_remote_command_does_not_invent_request_tty(tmp_path):
    entry = format_ssh_config_entry({
        "nickname": "batch",
        "remote_command": "printf ready",
    })

    assert "    RemoteCommand printf ready" in entry
    assert "RequestTTY" not in entry

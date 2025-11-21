from types import SimpleNamespace

from sshpilot.tui.command_builder import build_ssh_command


class DummyConfig:
    """Minimal config stub for command_builder tests."""

    def get_ssh_config(self):
        return {
            "connection_timeout": 5,
            "auto_add_host_keys": True,
            "batch_mode": False,
            "compression": False,
        }


def _make_connection(**overrides):
    defaults = {
        "nickname": "web",
        "hostname": "web.internal",
        "host": "web",
        "username": "deploy",
        "port": 2222,
        "keyfile": "",
        "certificate": "",
        "forwarding_rules": [],
        "data": {},
        "quick_connect_command": "",
        "remote_command": "",
        "local_command": "",
        "x11_forwarding": False,
        "auth_method": 0,
        "key_select_mode": 0,
        "password": "",
        "extra_ssh_config": "",
    }
    defaults.update(overrides)
    conn = SimpleNamespace(**defaults)

    def _resolver():
        return getattr(conn, "hostname", "") or getattr(conn, "host", "") or getattr(conn, "nickname", "")

    conn.resolve_host_identifier = _resolver
    return conn


def test_build_basic_command_uses_username_and_port():
    conn = _make_connection()
    cmd = build_ssh_command(conn, DummyConfig())

    assert cmd[0] == "ssh"
    assert cmd[-1] == "deploy@web.internal"
    assert "-p" in cmd
    port_index = cmd.index("-p")
    assert cmd[port_index + 1] == "2222"


def test_quick_connect_command_is_respected():
    conn = _make_connection(quick_connect_command="ssh -p 44 other-host")
    cmd = build_ssh_command(conn, DummyConfig())
    assert cmd == ["ssh", "-p", "44", "other-host"]


def test_remote_command_requests_tty():
    conn = _make_connection(remote_command="uptime")
    cmd = build_ssh_command(conn, DummyConfig())
    host_index = cmd.index("deploy@web.internal")
    assert cmd[host_index - 2 : host_index] == ["-t", "-t"]
    assert cmd[-1].startswith("uptime")


def test_forwarding_rules_are_translated():
    forwarding_rules = [
        {
            "type": "local",
            "listen_addr": "127.0.0.1",
            "listen_port": 8080,
            "remote_host": "db.local",
            "remote_port": 5432,
            "enabled": True,
        },
        {
            "type": "dynamic",
            "listen_addr": "localhost",
            "listen_port": 1080,
            "enabled": True,
        },
        {
            "type": "remote",
            "listen_addr": "0.0.0.0",
            "listen_port": 2222,
            "local_host": "localhost",
            "local_port": 22,
            "enabled": True,
        },
    ]
    conn = _make_connection(forwarding_rules=forwarding_rules)
    cmd = build_ssh_command(conn, DummyConfig())

    assert any(arg == "127.0.0.1:8080:db.local:5432" for arg in cmd)
    assert any(arg == "localhost:1080" for arg in cmd)
    assert any(arg == "0.0.0.0:2222:localhost:22" for arg in cmd)

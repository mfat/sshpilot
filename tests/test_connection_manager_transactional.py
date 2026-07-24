"""Persistence must be transactional: a failed config write may not mutate
in-memory state, emit success signals, or (on reload) wipe the last good list."""

import asyncio
import types

from sshpilot.connection_manager import Connection, ConnectionManager

asyncio.set_event_loop(asyncio.new_event_loop())

CONFIG = "Host web\n    HostName example.com\n    User alice\n"


def make_cm(tmp_path):
    cm = ConnectionManager.__new__(ConnectionManager)
    cm.config = types.SimpleNamespace(get_setting=lambda *a, **k: [])
    cm.connections = []
    cm.rules = []
    cm.ssh_config = {}
    cm.isolated_mode = False
    cm.ssh_config_path = str(tmp_path / "config")
    cm.known_hosts_path = str(tmp_path / "known_hosts")
    cm.emitted = []
    cm.emit = lambda *args: cm.emitted.append(args)
    return cm


def _boom(*args, **kwargs):
    raise OSError("disk full")


def test_empty_username_omits_user_line(tmp_path):
    cm = make_cm(tmp_path)
    entry = cm.format_ssh_config_entry(
        {"nickname": "x", "hostname": "example.com", "username": ""}
    )
    assert "User" not in entry
    entry = cm.format_ssh_config_entry(
        {"nickname": "x", "hostname": "example.com", "username": "alice"}
    )
    assert "    User alice" in entry


def test_failed_write_leaves_connection_unchanged(tmp_path, monkeypatch):
    cm = make_cm(tmp_path)
    (tmp_path / "config").write_text(CONFIG)
    cm.load_ssh_config()
    conn = cm.find_connection_by_nickname("web")
    assert conn is not None

    monkeypatch.setattr(cm, "_safe_write_config", _boom)
    cm.emitted.clear()
    ok = cm.update_connection(
        conn, {"nickname": "web2", "hostname": "example.org", "username": "bob"}
    )
    assert ok is False
    assert conn.nickname == "web"
    assert conn.hostname == "example.com"
    assert conn.username == "alice"
    assert not any(sig[0] == "connection-updated" for sig in cm.emitted)


def test_failed_reload_preserves_connections(tmp_path, monkeypatch):
    cm = make_cm(tmp_path)
    (tmp_path / "config").write_text(CONFIG)
    cm.load_ssh_config()
    assert len(cm.connections) == 1

    import sshpilot.connection_manager as cm_mod

    monkeypatch.setattr(
        cm_mod, "resolve_ssh_config_files", _boom
    )
    cm.load_ssh_config()
    assert cm.find_connection_by_nickname("web") is not None


def test_failed_remove_keeps_connection(tmp_path, monkeypatch):
    cm = make_cm(tmp_path)
    (tmp_path / "config").write_text(CONFIG)
    cm.load_ssh_config()
    conn = cm.find_connection_by_nickname("web")

    monkeypatch.setattr(cm, "_safe_write_config", _boom)
    cm.emitted.clear()
    ok = cm.remove_connection(conn, reload_config=False)
    assert ok is False
    assert conn in cm.connections
    assert not any(sig[0] == "connection-removed" for sig in cm.emitted)
    assert "Host web" in (tmp_path / "config").read_text()


def test_remove_not_on_disk_still_removes_from_memory(tmp_path, monkeypatch):
    cm = make_cm(tmp_path)
    (tmp_path / "config").write_text("")
    conn = Connection({"nickname": "ghost", "hostname": "x", "username": "u"})
    cm.connections.append(conn)
    cm.delete_connection_passwords = lambda *a, **k: False

    import sshpilot.config as config_mod

    monkeypatch.setattr(
        config_mod,
        "Config",
        lambda: types.SimpleNamespace(
            get_setting=lambda *a, **k: {}, set_setting=lambda *a, **k: None
        ),
    )
    ok = cm.remove_connection(conn, reload_config=False)
    assert ok is True
    assert conn not in cm.connections


def test_edit_preserves_unsurfaced_directives(tmp_path):
    """A dialog-style save payload omits ProxyCommand / standalone RequestTTY /
    ForwardAgent targets entirely; editing must not delete them from disk."""
    cm = make_cm(tmp_path)
    (tmp_path / "config").write_text(
        "Host bast\n"
        "    HostName example.com\n"
        "    User alice\n"
        "    ProxyCommand ssh -W %h:%p jumphost\n"
        "    RequestTTY yes\n"
        "    ForwardAgent $SSH_AUTH_SOCK\n"
    )
    cm.load_ssh_config()
    conn = cm.find_connection_by_nickname("bast")
    assert conn is not None

    ok = cm.update_connection(
        conn,
        {
            "nickname": "bast",
            "hostname": "example.com",
            "username": "bob",
            "forward_agent": True,
        },
    )
    assert ok is True
    text = (tmp_path / "config").read_text()
    assert "ProxyCommand ssh -W %h:%p jumphost" in text
    assert "RequestTTY yes" in text
    assert "ForwardAgent $SSH_AUTH_SOCK" in text
    assert "User bob" in text


def test_failed_create_leaves_no_phantom(tmp_path, monkeypatch):
    cm = make_cm(tmp_path)
    monkeypatch.setattr(cm, "update_connection", lambda *a, **k: False)
    result = cm.create_connection(
        {"nickname": "new", "hostname": "example.net", "username": "u"}
    )
    assert result is None
    assert cm.connections == []

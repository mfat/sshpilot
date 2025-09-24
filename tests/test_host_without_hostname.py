import os
import sys
import types
import asyncio
import subprocess

# Stub external dependencies required by connection_manager


gi_repo = types.ModuleType('gi.repository')
gi_repo.GObject = types.SimpleNamespace(
    SignalFlags=types.SimpleNamespace(RUN_FIRST=0),
    Object=type('GObject', (object,), {})
)
gi_repo.GLib = types.SimpleNamespace(idle_add=lambda *args, **kwargs: None)
gi_repo.Gtk = types.SimpleNamespace()
gi_repo.Secret = types.SimpleNamespace(
    Schema=types.SimpleNamespace(new=lambda *a, **k: object()),
    SchemaFlags=types.SimpleNamespace(NONE=0),
    SchemaAttributeType=types.SimpleNamespace(STRING=0),
    password_store_sync=lambda *a, **k: True,
    password_lookup_sync=lambda *a, **k: None,
    password_clear_sync=lambda *a, **k: None,
    COLLECTION_DEFAULT=None,
)

gi_mod = types.ModuleType('gi')
gi_mod.repository = gi_repo
gi_mod.require_version = lambda *args, **kwargs: None
sys.modules['gi'] = gi_mod
sys.modules['gi.repository'] = gi_repo
sys.modules['gi.repository.Secret'] = gi_repo.Secret

# Ensure the project package is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.connection_manager import Connection, ConnectionManager


def test_host_token_used_when_hostname_missing(tmp_path):
    """Entries without HostName should fall back to the Host token for the hostname."""
    asyncio.set_event_loop(asyncio.new_event_loop())

    manager = ConnectionManager.__new__(ConnectionManager)
    manager.connections = []

    config_path = tmp_path / 'config'
    config_path.write_text('Host example.com\n    User testuser\n')
    manager.ssh_config_path = str(config_path)

    manager.load_ssh_config()

    assert len(manager.connections) == 1
    conn = manager.connections[0]
    assert conn.nickname == 'example.com'
    assert conn.data['hostname'] == ''
    assert conn.data['host'] == 'example.com'
    assert conn.hostname == ''
    assert conn.host == 'example.com'


def test_multiple_labels_without_hostname_have_no_aliases(tmp_path):
    """Multiple labels without HostName produce separate entries without aliases."""
    asyncio.set_event_loop(asyncio.new_event_loop())

    manager = ConnectionManager.__new__(ConnectionManager)
    manager.connections = []

    cfg = """Host primary alias1 alias2
    User testuser
"""
    config_path = tmp_path / 'config'
    config_path.write_text(cfg)
    manager.ssh_config_path = str(config_path)

    manager.load_ssh_config()

    assert sorted(c.nickname for c in manager.connections) == ['alias1', 'alias2', 'primary']
    for c in manager.connections:
        assert c.data['hostname'] == ''
        assert c.hostname == ''
        assert c.host == c.nickname
        assert not hasattr(c, 'aliases')

    primary = next(c for c in manager.connections if c.nickname == 'primary')

    entry = manager.format_ssh_config_entry(primary.data)
    assert 'HostName' not in entry
    assert entry.splitlines()[0] == 'Host primary'
    assert 'alias1' not in entry and 'alias2' not in entry


def test_alias_labels_with_hostname(tmp_path):
    """Alias groups with HostName create independent entries for each label."""
    asyncio.set_event_loop(asyncio.new_event_loop())

    manager = ConnectionManager.__new__(ConnectionManager)
    manager.connections = []

    cfg = """Host app1 app2
    HostName 192.168.1.50
    User testuser
"""
    config_path = tmp_path / 'config'
    config_path.write_text(cfg)
    manager.ssh_config_path = str(config_path)

    manager.load_ssh_config()

    assert sorted(c.nickname for c in manager.connections) == ['app1', 'app2']
    for c in manager.connections:
        assert c.data['hostname'] == '192.168.1.50'
        assert c.hostname == '192.168.1.50'
        assert c.username == 'testuser'
        assert c.aliases == []


def test_connect_command_preserves_empty_hostname(tmp_path):
    """Connecting should use the alias for SSH while keeping hostname empty."""
    asyncio.set_event_loop(asyncio.new_event_loop())

    manager = ConnectionManager.__new__(ConnectionManager)
    manager.connections = []

    cfg = """Host example.com\n    User testuser\n"""
    config_path = tmp_path / 'config'
    config_path.write_text(cfg)
    manager.ssh_config_path = str(config_path)

    manager.load_ssh_config()

    assert len(manager.connections) == 1
    conn = manager.connections[0]
    assert conn.hostname == ''
    assert conn.host == 'example.com'

    asyncio.get_event_loop().run_until_complete(conn.connect())

    # Hostname remains empty but ssh command targets the alias
    assert conn.hostname == ''
    assert any(part.endswith('example.com') for part in conn.ssh_cmd)


def test_isolated_config_used_for_effective_resolution(tmp_path, monkeypatch):
    """Isolated configs should be passed to ssh -G via -F while preserving hostname."""
    asyncio.set_event_loop(asyncio.new_event_loop())

    config_dir = tmp_path / 'isolated'
    config_dir.mkdir()
    config_path = config_dir / 'ssh_config'
    config_path.write_text(
        "Host alias\n    HostName 10.0.0.5\n    User tester\n"
    )

    connection = Connection(
        {
            'nickname': 'alias',
            'host': 'alias',
            'hostname': '10.0.0.5',
            'username': 'tester',
            'source': str(config_path),
        }
    )
    connection.isolated_mode = True

    calls = []

    class DummyResult:
        def __init__(self, stdout: str):
            self.stdout = stdout
            self.stderr = ''

    def fake_run(cmd, capture_output, text, check):
        calls.append(cmd)
        return DummyResult('hostname 10.0.0.5\nuser tester\n')

    monkeypatch.setattr(subprocess, 'run', fake_run)

    loop = asyncio.get_event_loop()
    assert loop.run_until_complete(connection.connect())

    expected_cmd = [
        'ssh',
        '-F',
        os.path.abspath(str(config_path)),
        '-G',
        'alias',
    ]
    assert calls == [expected_cmd]
    assert connection.hostname == '10.0.0.5'


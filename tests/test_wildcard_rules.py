import os
import sys
import types
import asyncio

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

from sshpilot.connection_manager import ConnectionManager


def test_wildcard_and_negated_hosts_are_stored_as_rules(tmp_path):
    """Host blocks with wildcard or negated tokens should be stored in rules and not connections."""
    asyncio.set_event_loop(asyncio.new_event_loop())

    cfg_path = tmp_path / 'config'
    cfg_path.write_text(
        '\n'.join([
            'Host *.example.com alias?',
            '    User user1',
            '',
            'Host normal',
            '    HostName normal.example.com',
            '    User user2',
            '',
            'Host !blocked',
            '    User user3',
        ])
    )

    cm = ConnectionManager.__new__(ConnectionManager)
    cm.connections = []
    cm.ssh_config_path = str(cfg_path)

    cm.load_ssh_config()

    # Only the normal host should appear in connections
    assert len(cm.connections) == 1
    assert cm.connections[0].nickname == 'normal'

    # Wildcard and negated hosts are stored as rules
    assert len(cm.rules) == 2
    first, second = cm.rules
    assert first['host'] == '*.example.com'
    assert 'aliases' not in first
    assert second['host'] == '!blocked'


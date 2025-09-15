import os
import sys
import types

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

from sshpilot.connection_manager import ConnectionManager, Connection


def test_certificatefile_persistence():
    manager = ConnectionManager.__new__(ConnectionManager)
    config = {
        'host': 'example',
        'hostname': 'example.com',
        'certificatefile': '~/cert.pub',
    }

    parsed = manager.parse_host_config(config)
    expected = os.path.expanduser('~/cert.pub')
    assert parsed['certificate'] == expected

    conn = Connection(parsed)
    assert conn.certificate == expected

    # simulate reloading the SSH config
    parsed_reload = manager.parse_host_config(config)
    conn.update_data(parsed_reload)
    assert conn.certificate == expected

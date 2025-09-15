import os
import sys
import types
import asyncio

# Stub external dependencies required by connection_dialog/manager


gi_repo = types.ModuleType('gi.repository')
gi_repo.GObject = types.SimpleNamespace(
    SignalFlags=types.SimpleNamespace(RUN_FIRST=0),
    Object=type('GObject', (object,), {})
)
gi_repo.GLib = types.SimpleNamespace(idle_add=lambda func, *args, **kwargs: func())
gi_repo.Gtk = types.SimpleNamespace(
    Box=type('Box', (object,), {}),
    Orientation=types.SimpleNamespace(VERTICAL=0, HORIZONTAL=1),
    Widget=type('Widget', (object,), {}),
)
gi_repo.Adw = types.SimpleNamespace(Window=type('Window', (object,), {}))
gi_repo.Gio = types.SimpleNamespace()
gi_repo.Gdk = types.SimpleNamespace()
gi_repo.Pango = types.SimpleNamespace()
gi_repo.PangoFT2 = types.SimpleNamespace()
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

from sshpilot.connection_dialog import SSHConfigAdvancedTab
from sshpilot.connection_manager import ConnectionManager


def test_host_aliases_added_via_advanced_tab():
    """Host entries in advanced tab populate alias list without stray directives."""

    asyncio.set_event_loop(asyncio.new_event_loop())

    class DummyEntryRow:
        def __init__(self, text=""):
            self._text = text

        def get_text(self):
            return self._text

        def set_text(self, text):
            self._text = text

    alias_row = DummyEntryRow()

    connection = types.SimpleNamespace(
        nickname='primary',
        host='primary',
        aliases=[],
        data={'nickname': 'primary', 'host': 'primary', 'aliases': [], 'username': '', 'port': 22, 'extra_ssh_config': ''},
        extra_ssh_config=''
    )

    parent_dialog = types.SimpleNamespace(connection=connection, aliases_row=alias_row)

    row_grid = types.SimpleNamespace()
    row_grid.key_dropdown = 'Host'
    row_grid.value_entry = DummyEntryRow('foo bar')

    tab = SSHConfigAdvancedTab.__new__(SSHConfigAdvancedTab)
    tab.config_entries = [row_grid]
    tab._get_dropdown_selected_text = lambda dropdown: dropdown
    tab.on_remove_option = lambda _b, r: tab.config_entries.remove(r)
    tab.update_config_preview = lambda: None
    tab.get_ancestor = lambda _cls: parent_dialog

    extra = tab.get_extra_ssh_config()
    assert extra == ''
    assert alias_row.get_text() == 'foo bar'
    assert connection.aliases == ['foo', 'bar']
    assert connection.data['aliases'] == ['foo', 'bar']
    assert tab.config_entries == []

    connection.data['extra_ssh_config'] = extra
    cm = ConnectionManager.__new__(ConnectionManager)
    entry = cm.format_ssh_config_entry(connection.data)

    lines = entry.splitlines()
    assert lines[0] == 'Host primary foo bar'
    assert 'HostName' not in entry
    assert sum(1 for line in lines if line.startswith('Host ')) == 1


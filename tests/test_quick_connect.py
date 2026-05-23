"""Regression tests for quick connect commands."""

import asyncio
import shlex
import sys
import types


def _ensure_gi_stubs():
    if 'gi' not in sys.modules:
        gi = types.ModuleType('gi')
        gi.require_version = lambda *args, **kwargs: None
        repository = types.ModuleType('gi.repository')
        gi.repository = repository
        sys.modules['gi'] = gi
        sys.modules['gi.repository'] = repository
    else:
        gi = sys.modules['gi']
        repository = getattr(gi, 'repository', None)
        if repository is None:
            repository = types.ModuleType('gi.repository')
            gi.repository = repository
            sys.modules['gi.repository'] = repository

    for name, attr_map in (
        ('Gtk', {'Overlay': type('Overlay', (object,), {}),
                 'Box': type('Box', (object,), {'__init__': lambda s, **kw: None}),
                 'Label': type('Label', (object,), {}),
                 'Entry': type('Entry', (object,), {}),
                 'Button': type('Button', (object,), {}),
                 'FileDialog': type('FileDialog', (object,), {}),
                 'Align': type('Align', (object,), {'START': 0, 'CENTER': 1}),
                 'Orientation': type('Orientation', (object,), {'VERTICAL': 0, 'HORIZONTAL': 1})}),
        ('Adw', {'MessageDialog': type('MessageDialog', (object,), {}),
                 'PreferencesGroup': type('PreferencesGroup', (object,), {}),
                 'PasswordEntryRow': type('PasswordEntryRow', (object,), {}),
                 'ActionRow': type('ActionRow', (object,), {}),
                 'ResponseAppearance': type('ResponseAppearance', (object,), {'SUGGESTED': 0})}),
        ('Gdk', {}),
        ('GObject', {'Object': type('Object', (object,), {}),
                     'SignalFlags': type('SignalFlags', (object,), {'RUN_FIRST': 0})}),
        ('Gio', {'File': type('File', (object,), {'new_for_path': staticmethod(lambda p: None)})}),
        ('GLib', {
            'get_user_config_dir': lambda: '/tmp',
            'get_user_data_dir': lambda: '/tmp',
            'get_home_dir': lambda: '/tmp',
            'idle_add': lambda *a, **kw: None,
        }),
    ):
        module_name = f'gi.repository.{name}'
        module = sys.modules.get(module_name)
        if module is None:
            module = types.ModuleType(module_name)
            sys.modules[module_name] = module
        setattr(sys.modules['gi.repository'], name, module)
        for attr, value in attr_map.items():
            if not hasattr(module, attr):
                setattr(module, attr, value)


_ensure_gi_stubs()

from sshpilot.connection_manager import Connection
from sshpilot.welcome_page import QuickConnectDialog


def _parse_quick_command(command: str):
    dialog = QuickConnectDialog.__new__(QuickConnectDialog)
    return QuickConnectDialog._parse_ssh_command(dialog, command)


def test_quick_connect_preserves_original_command():
    command = "ssh -J bastion -o Foo=bar user@host"

    parsed = _parse_quick_command(command)

    assert parsed["quick_connect_command"] == command
    assert parsed["host"] == "host"
    assert parsed["username"] == "user"
    # -J now maps to proxy_jump and -o Foo=bar goes to extra_ssh_config
    assert parsed["proxy_jump"] == ["bastion"]
    assert "Foo bar" in parsed["extra_ssh_config"]
    assert parsed["unparsed_args"] == []

    new_loop = asyncio.new_event_loop()
    previous_loop = None
    try:
        try:
            previous_loop = asyncio.get_event_loop()
        except RuntimeError:
            previous_loop = None
        asyncio.set_event_loop(new_loop)

        connection = Connection(parsed)
        assert connection.quick_connect_command == command

        connected = new_loop.run_until_complete(connection.connect())
    finally:
        asyncio.set_event_loop(previous_loop)
        new_loop.close()

    assert connected is True
    assert connection.ssh_cmd == shlex.split(command)


def test_quick_connect_custom_port_with_native_mode():
    """Regression test for issue #899: -p flag must survive when native mode is active.

    native_connect() (called by default since ssh.native_connect=True) passes
    both native_mode=True and quick_connect_mode=True to build_ssh_connection().
    The quick_connect_mode branch must take priority so the full user-supplied
    command, including -p, is used verbatim.
    """
    command = "ssh -p 2222 root@192.168.8.1"

    parsed = _parse_quick_command(command)

    assert parsed["quick_connect_command"] == command
    assert parsed["host"] == "192.168.8.1"
    assert parsed["username"] == "root"
    assert parsed["port"] == 2222

    new_loop = asyncio.new_event_loop()
    previous_loop = None
    try:
        try:
            previous_loop = asyncio.get_event_loop()
        except RuntimeError:
            previous_loop = None
        asyncio.set_event_loop(new_loop)

        connection = Connection(parsed)
        assert connection.quick_connect_command == command

        # native_connect() is the default code path (ssh.native_connect=True).
        # It sets native_mode=True alongside quick_connect_mode=True; the
        # quick_connect branch must win and preserve -p 2222.
        connected = new_loop.run_until_complete(connection.native_connect())
    finally:
        asyncio.set_event_loop(previous_loop)
        new_loop.close()

    assert connected is True
    assert connection.ssh_cmd == shlex.split(command)
    assert '-p' in connection.ssh_cmd
    assert '2222' in connection.ssh_cmd

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
        ('Gtk', {'Overlay': type('Overlay', (object,), {})}),
        ('Adw', {'MessageDialog': type('MessageDialog', (object,), {})}),
        ('Gdk', {}),
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
    assert parsed["unparsed_args"] == ['-J', 'bastion']

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

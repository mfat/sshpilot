import asyncio
import types


from sshpilot.connection_manager import Connection
from sshpilot import terminal as terminal_mod


def _stub_adw(monkeypatch):
    app_stub = types.SimpleNamespace(native_connect_enabled=False)
    monkeypatch.setattr(
        terminal_mod.Adw,
        'Application',
        types.SimpleNamespace(get_default=lambda: app_stub),
        raising=False,
    )


def test_refresh_connection_command_rebuilds_ssh_cmd(monkeypatch):
    """Refreshing the connection should rebuild the SSH command from preferences."""

    _stub_adw(monkeypatch)

    terminal_cls = terminal_mod.TerminalWidget
    widget = terminal_cls.__new__(terminal_cls)

    class FakeConnection:
        def __init__(self):
            self.ssh_cmd = ['ssh', '-o', 'BatchMode=yes']
            self.connect_calls = 0

        async def connect(self):
            self.connect_calls += 1
            self.ssh_cmd = ['ssh', 'example.com']
            return True

    connection = FakeConnection()
    widget.connection = connection
    widget.connection_manager = types.SimpleNamespace(native_connect_enabled=False)

    loop = asyncio.new_event_loop()
    monkeypatch.setattr(asyncio, 'get_event_loop', lambda: loop)

    try:
        assert widget._refresh_connection_command() is True
    finally:
        loop.close()

    assert connection.connect_calls == 1
    assert connection.ssh_cmd == ['ssh', 'example.com']


# Removed: test_setup_terminal_drops_stale_batchmode — BatchMode handling moved out of
# TerminalWidget into build_ssh_connection (ssh_connection_builder); covered there.

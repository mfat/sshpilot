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


def test_setup_terminal_drops_stale_batchmode(monkeypatch):
    """Stale BatchMode options from previous sessions should be removed before spawning."""

    _stub_adw(monkeypatch)

    conn = Connection(
        {
            'host': 'example.com',
            'username': 'alice',
            'nickname': 'Example',
        }
    )
    conn.ssh_cmd = [
        'ssh',
        '-o',
        'BatchMode=yes',
        '-o',
        'NumberOfPasswordPrompts=1',
        'alice@example.com',
    ]
    conn.auth_method = 1  # password auth selected
    conn.password = ''
    conn.forwarding_rules = []

    terminal_cls = terminal_mod.TerminalWidget
    widget = terminal_cls.__new__(terminal_cls)
    widget.connection = conn
    widget.config = types.SimpleNamespace(
        get_ssh_config=lambda: {
            'batch_mode': False,
            'connection_timeout': None,
            'connection_attempts': None,
            'keepalive_interval': None,
            'keepalive_count_max': None,
            'strict_host_key_checking': '',
            'auto_add_host_keys': False,
            'compression': False,
            'verbosity': 0,
            'debug_enabled': False,
        }
    )
    widget.connection_manager = types.SimpleNamespace(
        native_connect_enabled=False,
        get_password=lambda *a, **k: None,
        prepare_key_for_connection=lambda *a, **k: True,
        known_hosts_path='',
    )
    widget._enable_askpass_log_forwarding = lambda *a, **k: None
    widget._fallback_to_askpass = lambda *a, **k: None
    widget._on_spawn_complete = lambda *a, **k: None
    widget._on_connection_failed = lambda *a, **k: (_ for _ in ()).throw(AssertionError('unexpected failure'))
    widget.apply_theme = lambda *a, **k: None
    widget._set_connecting_overlay_visible = lambda *a, **k: None
    widget._set_disconnected_banner_visible = lambda *a, **k: None
    widget._fallback_hide_spinner = lambda *a, **k: False
    widget._fallback_timer_id = None
    widget._is_quitting = False
    widget.connecting_bg = types.SimpleNamespace(set_visible=lambda *a, **k: None)
    widget.connecting_box = types.SimpleNamespace(set_visible=lambda *a, **k: None)
    widget.scrolled_window = types.SimpleNamespace()
    widget.terminal_stack = types.SimpleNamespace()

    class DummyVte:
        def __init__(self):
            self.last_cmd = None
            self.spawn_env = None

        def spawn_async(self, *args):
            self.last_cmd = list(args[2])
            self.spawn_env = list(args[3])

        def grab_focus(self):
            pass

    widget.vte = DummyVte()

    monkeypatch.setattr(terminal_mod, 'is_flatpak', lambda: False, raising=False)
    monkeypatch.setattr(terminal_mod, 'is_macos', lambda: False, raising=False)
    monkeypatch.setattr(
        terminal_mod,
        'get_port_checker',
        lambda: types.SimpleNamespace(get_port_conflicts=lambda ports, addr: []),
    )
    monkeypatch.setattr(
        terminal_mod.Vte,
        'Pty',
        types.SimpleNamespace(new_sync=lambda *a, **k: object()),
        raising=False,
    )
    monkeypatch.setattr(
        terminal_mod.Vte,
        'PtyFlags',
        types.SimpleNamespace(DEFAULT=0),
        raising=False,
    )
    monkeypatch.setattr(
        terminal_mod.GLib,
        'SpawnFlags',
        types.SimpleNamespace(DEFAULT=0),
        raising=False,
    )
    monkeypatch.setattr(terminal_mod.GLib, 'timeout_add_seconds', lambda *a, **k: 0, raising=False)
    if not hasattr(terminal_mod.GLib, 'source_remove'):
        monkeypatch.setattr(terminal_mod.GLib, 'source_remove', lambda *a, **k: None, raising=False)

    widget._setup_ssh_terminal()

    assert widget.vte.last_cmd is not None
    assert 'BatchMode=yes' not in widget.vte.last_cmd
    assert widget.vte.last_cmd.count('-t') >= 1

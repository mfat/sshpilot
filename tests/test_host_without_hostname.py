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


class _DummyWidget:
    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, name):
        def _noop(*_args, **_kwargs):
            return None

        return _noop


class _DummyGLibError(Exception):
    pass


gi_repo.GLib = types.SimpleNamespace(
    idle_add=lambda *args, **kwargs: None,
    timeout_add_seconds=lambda *args, **kwargs: 1,
    SpawnFlags=types.SimpleNamespace(DEFAULT=0),
    Error=_DummyGLibError,
)
gi_repo.Gtk = types.SimpleNamespace(
    Box=_DummyWidget,
    Orientation=types.SimpleNamespace(VERTICAL=0, HORIZONTAL=1),
    Align=types.SimpleNamespace(CENTER=0, FILL=1, END=2, START=3),
    PolicyType=types.SimpleNamespace(AUTOMATIC=0),
    STYLE_PROVIDER_PRIORITY_APPLICATION=0,
    ScrolledWindow=_DummyWidget,
    Overlay=_DummyWidget,
    Spinner=_DummyWidget,
    Label=_DummyWidget,
    CssProvider=_DummyWidget,
    StyleContext=types.SimpleNamespace(add_provider_for_display=lambda *a, **k: None),
    Image=types.SimpleNamespace(new_from_icon_name=lambda *a, **k: _DummyWidget()),
    Button=types.SimpleNamespace(new_with_label=lambda *a, **k: _DummyWidget()),
)
gi_repo.Secret = types.SimpleNamespace(
    Schema=types.SimpleNamespace(new=lambda *a, **k: object()),
    SchemaFlags=types.SimpleNamespace(NONE=0),
    SchemaAttributeType=types.SimpleNamespace(STRING=0),
    password_store_sync=lambda *a, **k: True,
    password_lookup_sync=lambda *a, **k: None,
    password_clear_sync=lambda *a, **k: None,
    COLLECTION_DEFAULT=None,
)
gi_repo.Vte = types.SimpleNamespace(
    Terminal=_DummyWidget,
    PtyFlags=types.SimpleNamespace(DEFAULT=0),
    Pty=types.SimpleNamespace(new_sync=lambda *args, **kwargs: object()),
)
gi_repo.Pango = types.SimpleNamespace(Weight=types.SimpleNamespace(BOLD=700))


class _DummyRGBA:
    def __init__(self, *args, **kwargs):
        self.red = 0.0
        self.green = 0.0
        self.blue = 0.0
        self.alpha = 1.0


class _DummyRectangle:
    def __init__(self, *args, **kwargs):
        self.x = 0
        self.y = 0
        self.width = 0
        self.height = 0


gi_repo.Gdk = types.SimpleNamespace(
    Display=types.SimpleNamespace(get_default=lambda: None),
    RGBA=_DummyRGBA,
    Rectangle=_DummyRectangle,
    BUTTON_SECONDARY=3,
    ModifierType=types.SimpleNamespace(META_MASK=0, CONTROL_MASK=0),
)
gi_repo.Gio = types.SimpleNamespace()
gi_repo.Adw = types.SimpleNamespace(
    Application=type('DummyApp', (), {'get_default': staticmethod(lambda: None)})
)

gi_mod = types.ModuleType('gi')
gi_mod.repository = gi_repo
gi_mod.require_version = lambda *args, **kwargs: None
sys.modules['gi'] = gi_mod
sys.modules['gi.repository'] = gi_repo
sys.modules['gi.repository.Secret'] = gi_repo.Secret
sys.modules['gi.repository.GLib'] = gi_repo.GLib
sys.modules['gi.repository.Gtk'] = gi_repo.Gtk
sys.modules['gi.repository.GObject'] = gi_repo.GObject
sys.modules['gi.repository.Vte'] = gi_repo.Vte
sys.modules['gi.repository.Pango'] = gi_repo.Pango
sys.modules['gi.repository.Gdk'] = gi_repo.Gdk
sys.modules['gi.repository.Gio'] = gi_repo.Gio
sys.modules['gi.repository.Adw'] = gi_repo.Adw

# Ensure the project package is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.connection_manager import Connection, ConnectionManager
import sshpilot.connection_manager as connection_manager_module
import sshpilot.terminal as terminal_module
from sshpilot.terminal import TerminalWidget

setattr(connection_manager_module, 'Config', None)


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


def test_effective_config_overrides_default_options(monkeypatch):
    """Effective SSH config should replace defaults for matching -o options."""
    asyncio.set_event_loop(asyncio.new_event_loop())

    connection = Connection(
        {
            'nickname': 'example',
            'host': 'example',
            'hostname': '',
            'username': 'tester',
        }
    )

    class DummyConfig:
        def get_ssh_config(self):
            return {
                'apply_advanced': True,
                'strict_host_key_checking': 'accept-new',
                'verbosity': 1,
                'batch_mode': False,
                'compression': False,
                'debug_enabled': False,
                'auto_add_host_keys': True,
            }

    monkeypatch.setattr('sshpilot.connection_manager.Config', DummyConfig)
    monkeypatch.setattr('sshpilot.config.Config', DummyConfig)

    def fake_effective(_alias, config_file=None):
        return {
            'stricthostkeychecking': 'no',
            'loglevel': 'QUIET',
            'user': 'tester',
            'hostname': 'example',
        }

    monkeypatch.setattr(
        'sshpilot.connection_manager.get_effective_ssh_config', fake_effective
    )

    loop = asyncio.get_event_loop()
    assert loop.run_until_complete(connection.connect())

    strict_options = [part for part in connection.ssh_cmd if 'StrictHostKeyChecking' in part]
    loglevel_options = [part for part in connection.ssh_cmd if 'LogLevel' in part]

    assert 'StrictHostKeyChecking=no' in strict_options
    assert 'StrictHostKeyChecking=accept-new' not in strict_options
    assert 'LogLevel=QUIET' in loglevel_options
    assert 'LogLevel=VERBOSE' not in loglevel_options


def test_terminal_command_respects_effective_options(monkeypatch):
    """Terminal command should apply final SSH config without conflicting duplicates."""
    asyncio.set_event_loop(asyncio.new_event_loop())

    connection = Connection(
        {
            'nickname': 'example',
            'host': 'example',
            'hostname': '',
            'username': 'tester',
        }
    )
    connection.ssh_cmd = [
        'ssh',
        '-o', 'StrictHostKeyChecking=accept-new',
        '-o', 'LogLevel=QUIET',
        'tester@example.com',
    ]
    connection.forwarding_rules = []
    connection.extra_ssh_config = ''

    captured = {}

    class DummyTerminal:
        def spawn_async(self, *_args, **_kwargs):
            captured['cmd'] = list(_args[2])
            captured['env'] = list(_args[3])

        def grab_focus(self):
            return None

    monkeypatch.setattr(
        terminal_module,
        'get_port_checker',
        lambda: types.SimpleNamespace(get_port_conflicts=lambda ports, addr: []),
    )
    monkeypatch.setattr(
        terminal_module.Vte.Pty,
        'new_sync',
        lambda *args, **kwargs: object(),
    )

    terminal = TerminalWidget.__new__(TerminalWidget)
    terminal.connection = connection
    terminal.connection_manager = types.SimpleNamespace(
        native_connect_enabled=False,
        get_password=lambda *args, **kwargs: None,
        known_hosts_path='',
        prepare_key_for_connection=lambda *args, **kwargs: True,
    )
    terminal.config = types.SimpleNamespace(
        get_ssh_config=lambda: {
            'apply_advanced': True,
            'strict_host_key_checking': 'no',
            'connection_timeout': 10,
            'connection_attempts': 1,
            'keepalive_interval': 30,
            'keepalive_count_max': 3,
            'auto_add_host_keys': True,
            'batch_mode': False,
            'compression': False,
            'verbosity': 0,
            'debug_enabled': False,
        }
    )
    terminal.vte = DummyTerminal()
    terminal.apply_theme = lambda: None
    terminal._on_connection_failed = lambda *args, **kwargs: None
    terminal._fallback_to_askpass = lambda *args, **kwargs: None
    terminal._fallback_hide_spinner = lambda: False
    terminal._on_spawn_complete = lambda *args, **kwargs: None
    terminal._show_forwarding_error_dialog = lambda *args, **kwargs: None

    terminal._setup_ssh_terminal()

    ssh_cmd = captured['cmd']
    strict_entries = [token for token in ssh_cmd if 'StrictHostKeyChecking' in token]
    log_entries = [token for token in ssh_cmd if 'LogLevel' in token]

    assert strict_entries == ['StrictHostKeyChecking=no']
    assert log_entries == ['LogLevel=QUIET']

"""
Unit tests for PTY size setting before spawn.

Tests verify that:
1. PTY size is set correctly before spawning SSH connections
2. PTY size is set correctly before spawning local terminals
3. PTY size matches terminal widget dimensions
4. PTY is associated with Terminal before spawn_async is called
"""

import importlib
import logging
import types
from unittest.mock import Mock, MagicMock, patch

import pytest

# Add project root to path
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


class DummyPty:
    """Mock Vte.Pty object that tracks size setting"""
    def __init__(self):
        self._rows = None
        self._cols = None
        self.set_size_called = False
        self.fd = 42  # Mock file descriptor
    
    def set_size(self, rows, cols):
        """Set PTY size and track that it was called"""
        self._rows = rows
        self._cols = cols
        self.set_size_called = True
        return True
    
    def get_size(self):
        """Get current PTY size"""
        return (True, self._rows, self._cols) if self._rows and self._cols else (False, 0, 0)
    
    def get_fd(self):
        """Get PTY file descriptor"""
        return self.fd


class DummyVte:
    """Mock Vte.Terminal that tracks PTY operations"""
    def __init__(self, rows=80, cols=120):
        self._rows = rows
        self._cols = cols
        self._pty = None
        self.spawn_calls = []
        self.set_pty_calls = []
    
    def get_row_count(self):
        """Get terminal row count"""
        return self._rows
    
    def get_column_count(self):
        """Get terminal column count"""
        return self._cols
    
    def set_pty(self, pty):
        """Associate PTY with terminal"""
        self._pty = pty
        self.set_pty_calls.append(pty)
    
    def get_pty(self):
        """Get associated PTY"""
        return self._pty
    
    def spawn_async(self, *args, **kwargs):
        """Track spawn calls"""
        self.spawn_calls.append((args, kwargs))
    
    def grab_focus(self):
        pass
    
    def connect(self, *args, **kwargs):
        return 12345


class DummyBackend:
    """Mock terminal backend"""
    def __init__(self, vte=None):
        self.vte = vte
        self.widget = Mock()
        self.spawn_calls = []
    
    def get_pty(self):
        """Return None to force PTY creation in terminal"""
        return None
    
    def spawn_async(self, *args, **kwargs):
        """Track spawn calls"""
        self.spawn_calls.append((args, kwargs))
        # Call vte.spawn_async if available
        if self.vte:
            self.vte.spawn_async(*args, **kwargs)
    
    def grab_focus(self):
        pass


class _DummyGLib:
    """Mock GLib"""
    class Error(Exception):
        pass
    
    SpawnFlags = types.SimpleNamespace(DEFAULT=0)
    
    @staticmethod
    def timeout_add_seconds(*args, **kwargs):
        return 0
    
    @staticmethod
    def idle_add(*args, **kwargs):
        return None


def test_ssh_terminal_sets_pty_size_before_spawn(monkeypatch, caplog):
    """Test that PTY size is set correctly before spawning SSH connection"""
    terminal_mod = importlib.import_module("sshpilot.terminal")
    
    # Create mock PTY factory
    pty_instances = []
    def create_pty(*args, **kwargs):
        pty = DummyPty()
        pty_instances.append(pty)
        return pty
    
    # Mock VTE
    monkeypatch.setattr(
        terminal_mod,
        "Vte",
        types.SimpleNamespace(
            Pty=types.SimpleNamespace(new_sync=create_pty),
            PtyFlags=types.SimpleNamespace(DEFAULT=0),
        ),
        raising=False,
    )
    monkeypatch.setattr(terminal_mod, "GLib", _DummyGLib, raising=False)
    monkeypatch.setattr(
        terminal_mod.Adw,
        "Application",
        types.SimpleNamespace(get_default=lambda: None),
        raising=False,
    )
    
    # Create terminal with mock VTE
    terminal_cls = terminal_mod.TerminalWidget
    terminal = terminal_cls.__new__(terminal_cls)
    
    # Setup terminal with realistic size (not default 80x24)
    vte = DummyVte(rows=30, cols=100)
    backend = DummyBackend(vte=vte)
    terminal.vte = vte
    terminal.backend = backend
    
    # Setup connection
    terminal.connection = types.SimpleNamespace(
        ssh_cmd=['ssh', 'user@host'],
        auth_method=0,
        password=None,
        key_passphrase=None,
        keyfile=None,
        key_select_mode=0,
        identity_agent_disabled=False,
        quick_connect_command="",
        data={},
        forwarding_rules=[],
        hostname="test.example.com",
        username="user",
        port=22,
        pubkey_auth_no=False,
        remote_command="",
        local_command="",
        extra_ssh_config="",
    )
    
    terminal.connection_manager = types.SimpleNamespace(
        native_connect_enabled=False,
        get_password=lambda *a, **k: None,
        known_hosts_path="",
        prepare_key_for_connection=lambda *a, **k: True,
        get_key_passphrase=lambda *a, **k: None,
        update_connection_status=lambda *a, **k: None,
    )
    
    terminal.config = types.SimpleNamespace(
        get_ssh_config=lambda: {},
        get_setting=lambda *a, **k: None,
    )
    
    terminal._enable_askpass_log_forwarding = lambda *a, **k: None
    terminal.apply_theme = lambda *a, **k: None
    terminal._set_connecting_overlay_visible = lambda *a, **k: None
    terminal._set_disconnected_banner_visible = lambda *a, **k: None
    terminal.emit = lambda *a, **k: None
    terminal.session_id = "test-session"
    terminal.is_connected = False
    terminal._is_quitting = False
    
    caplog.set_level(logging.DEBUG)
    
    # Trigger SSH terminal setup
    terminal._setup_ssh_terminal()
    
    # Verify PTY was created
    assert len(pty_instances) > 0, "PTY should be created"
    pty = pty_instances[0]
    
    # Verify PTY size was set
    assert pty.set_size_called, "PTY set_size() should be called"
    assert pty._rows == 30, f"PTY rows should be 30, got {pty._rows}"
    assert pty._cols == 100, f"PTY cols should be 100, got {pty._cols}"
    
    # Verify PTY was associated with Terminal
    assert len(vte.set_pty_calls) > 0, "set_pty() should be called on Terminal"
    assert vte.set_pty_calls[0] == pty, "PTY should be associated with Terminal"
    
    # Verify spawn was called
    assert len(backend.spawn_calls) > 0, "spawn_async() should be called"
    
    # Verify log message
    assert "Set PTY size to 30x100" in caplog.text


def test_local_terminal_sets_pty_size_before_spawn(monkeypatch, caplog):
    """Test that PTY size is set correctly before spawning local terminal"""
    terminal_mod = importlib.import_module("sshpilot.terminal")
    
    # Create mock PTY factory
    pty_instances = []
    def create_pty(*args, **kwargs):
        pty = DummyPty()
        pty_instances.append(pty)
        return pty
    
    # Mock VTE
    monkeypatch.setattr(
        terminal_mod,
        "Vte",
        types.SimpleNamespace(
            Pty=types.SimpleNamespace(new_sync=create_pty),
            PtyFlags=types.SimpleNamespace(DEFAULT=0),
        ),
        raising=False,
    )
    monkeypatch.setattr(terminal_mod, "GLib", _DummyGLib, raising=False)
    monkeypatch.setattr(terminal_mod, "is_flatpak", lambda: False, raising=False)
    monkeypatch.setattr(terminal_mod, "pwd", types.SimpleNamespace(
        getpwuid=lambda uid: types.SimpleNamespace(
            pw_name="testuser",
            pw_dir="/home/testuser",
        )
    ), raising=False)
    
    # Create terminal with mock VTE
    terminal_cls = terminal_mod.TerminalWidget
    terminal = terminal_cls.__new__(terminal_cls)
    
    # Setup terminal with realistic size (not default 80x24)
    vte = DummyVte(rows=40, cols=120)
    backend = DummyBackend(vte=vte)
    terminal.vte = vte
    terminal.backend = backend
    
    terminal.connection = None  # Local terminal has no connection
    terminal.connection_manager = types.SimpleNamespace()
    terminal.config = types.SimpleNamespace(
        get_setting=lambda key, default=None: {
            'terminal.shell': '/bin/bash',
        }.get(key, default),
    )
    
    terminal._is_local_shell = True
    terminal.apply_theme = lambda *a, **k: None
    terminal._set_connecting_overlay_visible = lambda *a, **k: None
    terminal._set_disconnected_banner_visible = lambda *a, **k: None
    terminal.emit = lambda *a, **k: None
    terminal.session_id = "test-session"
    terminal.is_connected = False
    terminal._is_quitting = False
    terminal._on_spawn_complete = lambda *a, **k: None
    
    caplog.set_level(logging.DEBUG)
    
    # Trigger local terminal setup
    terminal._setup_local_shell_direct()
    
    # Verify PTY was created
    assert len(pty_instances) > 0, "PTY should be created for local terminal"
    pty = pty_instances[0]
    
    # Verify PTY size was set
    assert pty.set_size_called, "PTY set_size() should be called for local terminal"
    assert pty._rows == 40, f"PTY rows should be 40, got {pty._rows}"
    assert pty._cols == 120, f"PTY cols should be 120, got {pty._cols}"
    
    # Verify PTY was associated with Terminal
    assert len(vte.set_pty_calls) > 0, "set_pty() should be called on Terminal"
    assert vte.set_pty_calls[0] == pty, "PTY should be associated with Terminal"
    
    # Verify spawn was called
    assert len(backend.spawn_calls) > 0, "spawn_async() should be called"
    
    # Verify log message
    assert "Set PTY size to 40x120" in caplog.text


def test_pty_size_not_set_for_default_dimensions(monkeypatch, caplog):
    """Test that PTY size is not set when terminal has default 80x24 dimensions"""
    terminal_mod = importlib.import_module("sshpilot.terminal")
    
    # Create mock PTY factory
    pty_instances = []
    def create_pty(*args, **kwargs):
        pty = DummyPty()
        pty_instances.append(pty)
        return pty
    
    # Mock VTE
    monkeypatch.setattr(
        terminal_mod,
        "Vte",
        types.SimpleNamespace(
            Pty=types.SimpleNamespace(new_sync=create_pty),
            PtyFlags=types.SimpleNamespace(DEFAULT=0),
        ),
        raising=False,
    )
    monkeypatch.setattr(terminal_mod, "GLib", _DummyGLib, raising=False)
    monkeypatch.setattr(
        terminal_mod.Adw,
        "Application",
        types.SimpleNamespace(get_default=lambda: None),
        raising=False,
    )
    
    # Create terminal with default size (80x24)
    terminal_cls = terminal_mod.TerminalWidget
    terminal = terminal_cls.__new__(terminal_cls)
    
    vte = DummyVte(rows=24, cols=80)  # Default size
    backend = DummyBackend(vte=vte)
    terminal.vte = vte
    terminal.backend = backend
    
    terminal.connection = types.SimpleNamespace(
        ssh_cmd=['ssh', 'user@host'],
        auth_method=0,
        password=None,
        key_passphrase=None,
        keyfile=None,
        key_select_mode=0,
        identity_agent_disabled=False,
        quick_connect_command="",
        data={},
        forwarding_rules=[],
        hostname="test.example.com",
        username="user",
        port=22,
        pubkey_auth_no=False,
        remote_command="",
        local_command="",
        extra_ssh_config="",
    )
    
    terminal.connection_manager = types.SimpleNamespace(
        native_connect_enabled=False,
        get_password=lambda *a, **k: None,
        known_hosts_path="",
        prepare_key_for_connection=lambda *a, **k: True,
        get_key_passphrase=lambda *a, **k: None,
        update_connection_status=lambda *a, **k: None,
    )
    
    terminal.config = types.SimpleNamespace(
        get_ssh_config=lambda: {},
        get_setting=lambda *a, **k: None,
    )
    
    terminal._enable_askpass_log_forwarding = lambda *a, **k: None
    terminal.apply_theme = lambda *a, **k: None
    terminal._set_connecting_overlay_visible = lambda *a, **k: None
    terminal._set_disconnected_banner_visible = lambda *a, **k: None
    terminal.emit = lambda *a, **k: None
    terminal.session_id = "test-session"
    terminal.is_connected = False
    terminal._is_quitting = False
    
    caplog.set_level(logging.DEBUG)
    
    # Trigger SSH terminal setup
    terminal._setup_ssh_terminal()
    
    # Verify PTY was created
    assert len(pty_instances) > 0, "PTY should be created"
    pty = pty_instances[0]
    
    # Verify PTY size was NOT set (because dimensions are default 80x24)
    assert not pty.set_size_called, "PTY set_size() should NOT be called for default dimensions"
    
    # But PTY should still be associated with Terminal
    assert len(vte.set_pty_calls) > 0, "set_pty() should still be called"


def test_pty_size_set_before_spawn_order(monkeypatch):
    """Test that PTY size is set and associated before spawn_async is called"""
    terminal_mod = importlib.import_module("sshpilot.terminal")
    
    # Track call order
    call_order = []
    
    # Create mock PTY factory
    pty_instances = []
    def create_pty(*args, **kwargs):
        pty = DummyPty()
        pty_instances.append(pty)
        call_order.append(('create_pty', pty))
        return pty
    
    # Mock VTE with call tracking
    vte = DummyVte(rows=50, cols=150)
    original_set_pty = vte.set_pty
    def tracked_set_pty(pty):
        call_order.append(('set_pty', pty))
        return original_set_pty(pty)
    vte.set_pty = tracked_set_pty
    
    original_spawn = vte.spawn_async
    def tracked_spawn(*args, **kwargs):
        call_order.append(('spawn_async', args))
        return original_spawn(*args, **kwargs)
    vte.spawn_async = tracked_spawn
    
    # Mock VTE module
    monkeypatch.setattr(
        terminal_mod,
        "Vte",
        types.SimpleNamespace(
            Pty=types.SimpleNamespace(new_sync=create_pty),
            PtyFlags=types.SimpleNamespace(DEFAULT=0),
        ),
        raising=False,
    )
    monkeypatch.setattr(terminal_mod, "GLib", _DummyGLib, raising=False)
    monkeypatch.setattr(
        terminal_mod.Adw,
        "Application",
        types.SimpleNamespace(get_default=lambda: None),
        raising=False,
    )
    
    # Create terminal
    terminal_cls = terminal_mod.TerminalWidget
    terminal = terminal_cls.__new__(terminal_cls)
    
    backend = DummyBackend(vte=vte)
    terminal.vte = vte
    terminal.backend = backend
    
    terminal.connection = types.SimpleNamespace(
        ssh_cmd=['ssh', 'user@host'],
        auth_method=0,
        password=None,
        key_passphrase=None,
        keyfile=None,
        key_select_mode=0,
        identity_agent_disabled=False,
        quick_connect_command="",
        data={},
        forwarding_rules=[],
        hostname="test.example.com",
        username="user",
        port=22,
        pubkey_auth_no=False,
        remote_command="",
        local_command="",
        extra_ssh_config="",
    )
    
    terminal.connection_manager = types.SimpleNamespace(
        native_connect_enabled=False,
        get_password=lambda *a, **k: None,
        known_hosts_path="",
        prepare_key_for_connection=lambda *a, **k: True,
        get_key_passphrase=lambda *a, **k: None,
        update_connection_status=lambda *a, **k: None,
    )
    
    terminal.config = types.SimpleNamespace(
        get_ssh_config=lambda: {},
        get_setting=lambda *a, **k: None,
    )
    
    terminal._enable_askpass_log_forwarding = lambda *a, **k: None
    terminal.apply_theme = lambda *a, **k: None
    terminal._set_connecting_overlay_visible = lambda *a, **k: None
    terminal._set_disconnected_banner_visible = lambda *a, **k: None
    terminal.emit = lambda *a, **k: None
    terminal.session_id = "test-session"
    terminal.is_connected = False
    terminal._is_quitting = False
    
    # Trigger SSH terminal setup
    terminal._setup_ssh_terminal()
    
    # Verify call order: create_pty -> set_pty -> spawn_async
    call_names = [name for name, _ in call_order]
    
    # PTY should be created first
    assert 'create_pty' in call_names, "PTY should be created"
    create_idx = call_names.index('create_pty')
    
    # set_pty should come after create_pty
    assert 'set_pty' in call_names, "set_pty should be called"
    set_pty_idx = call_names.index('set_pty')
    assert set_pty_idx > create_idx, "set_pty should be called after PTY creation"
    
    # spawn_async should come after set_pty
    assert 'spawn_async' in call_names, "spawn_async should be called"
    spawn_idx = call_names.index('spawn_async')
    assert spawn_idx > set_pty_idx, "spawn_async should be called after set_pty"

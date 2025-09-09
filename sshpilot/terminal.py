"""
Terminal Widget for sshPilot
Integrated VTE terminal with SSH connection handling using system SSH client
"""

import os
import sys
import logging
import signal
import time
import json
import re
import gi
from gettext import gettext as _
import asyncio
import threading
import weakref
import subprocess
import pwd
from datetime import datetime
from .askpass_utils import ensure_askpass_script, get_ssh_env_with_askpass_for_password
from .port_utils import get_port_checker

gi.require_version('Gtk', '4.0')
gi.require_version('Vte', '3.91')

gi.require_version('Adw', '1')
from gi.repository import Gtk, GObject, GLib, Vte, Pango, Gdk, Gio, Adw

logger = logging.getLogger(__name__)

class SSHProcessManager:
    """Manages SSH processes and ensures proper cleanup"""
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.processes = {}
            cls._instance.terminals = weakref.WeakSet()
            cls._instance.lock = threading.Lock()
            cls._instance.cleanup_thread = None
            cls._instance._start_cleanup_thread()
        return cls._instance
    
    def _start_cleanup_thread(self):
        """Start background cleanup thread"""
        # Disable automatic cleanup thread to prevent race conditions
        # Manual cleanup will happen on app shutdown via cleanup_all()
        logger.debug("Automatic SSH cleanup thread disabled to prevent race conditions")
    
    def _cleanup_loop(self):
        """Background cleanup loop"""
        while True:
            try:
                time.sleep(60)  # Increased from 30s to 60s to reduce interference
                self._cleanup_orphaned_processes()
            except Exception as e:
                logger.error(f"Error in cleanup loop: {e}")
    
    def _cleanup_orphaned_processes(self):
        """Clean up processes not tracked by active terminals"""
        with self.lock:
            active_pids = set()
            for terminal in list(self.terminals):
                try:
                    # Use stored PID instead of calling _get_terminal_pid() which can hang
                    pid = getattr(terminal, 'process_pid', None)
                    if pid:
                        active_pids.add(pid)
                        logger.debug(f"Terminal {id(terminal)} has active PID {pid}")
                except Exception as e:
                    logger.debug(f"Error getting stored PID from terminal: {e}")
            
            # Only clean up processes that are definitely orphaned AND old enough
            import time
            current_time = time.time()
            orphaned_pids = []
            
            for pid in list(self.processes.keys()):
                if pid not in active_pids:
                    # Check if process is old enough to be considered orphaned (10+ minutes)
                    process_info = self.processes.get(pid, {})
                    start_time = process_info.get('start_time')
                    if start_time and hasattr(start_time, 'timestamp'):
                        process_age = current_time - start_time.timestamp()
                        if process_age < 600:  # Less than 10 minutes old - be very conservative
                            logger.debug(f"Process {pid} is only {process_age:.1f}s old, skipping cleanup")
                            continue
                    else:
                        # If we don't have start time info, assume it's recent and skip cleanup
                        logger.debug(f"Process {pid} has no start_time info, skipping cleanup")
                        continue
                    
                    # Double-check: make sure the process actually exists before trying to kill it
                    try:
                        os.kill(pid, 0)  # Test if process exists
                        orphaned_pids.append(pid)
                        logger.debug(f"Found orphaned process {pid} (age: {process_age:.1f}s)")
                    except ProcessLookupError:
                        # Process already gone, just remove from tracking
                        logger.debug(f"Process {pid} already gone, removing from tracking")
                        if pid in self.processes:
                            del self.processes[pid]
                    except Exception as e:
                        logger.debug(f"Error checking process {pid}: {e}")
            
            # Clean up confirmed orphaned processes
            for pid in orphaned_pids:
                logger.info(f"Cleaning up orphaned process {pid}")
                self._terminate_process_by_pid(pid)
    
    def _terminate_process_by_pid(self, pid):
        """Terminate a process by PID"""
        try:
            # Always try process group first
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
            
            # Wait with timeout
            for _ in range(10):  # 1 second max
                try:
                    os.killpg(pgid, 0)
                    time.sleep(0.1)
                except ProcessLookupError:
                    return True
                    
            # Force kill if still alive
            os.killpg(pgid, signal.SIGKILL)
            return True
        except Exception:
            return False
    
    def register_terminal(self, terminal):
        """Register a terminal for tracking"""
        self.terminals.add(terminal)
        logger.debug(f"Registered terminal {id(terminal)}")
    
    def cleanup_all(self):
        """Clean up all managed processes"""
        import signal
        
        def timeout_handler(signum, frame):
            logger.warning("Cleanup timeout - forcing exit")
            os._exit(1)
        
        # Set 5-second timeout for entire cleanup
        signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(5)
        
        try:
            logger.info("Cleaning up all SSH processes...")
            
            # First, mark all terminals as quitting to suppress signal handlers
            for terminal in list(self.terminals):
                terminal._is_quitting = True
            
            with self.lock:
                # Atomically extract and clear all processes
                processes_to_clean = dict(self.processes)
                self.processes.clear()
            
            # Clean up processes without holding the lock
            for pid, info in processes_to_clean.items():
                logger.debug(f"Cleaning up process {pid} (command: {info.get('command', 'unknown')})")
                self._terminate_process_by_pid(pid)
            
            # Clean up terminals separately
            for terminal in list(self.terminals):
                try:
                    if hasattr(terminal, 'disconnect') and hasattr(terminal, 'is_connected') and terminal.is_connected:
                        logger.debug(f"Disconnecting terminal {id(terminal)}")
                        terminal.disconnect()
                except Exception as e:
                    logger.error(f"Error cleaning up terminal {id(terminal)}: {e}")
            
            # Clear terminal references
            self.terminals.clear()
                
            logger.info("SSH process cleanup completed")
        finally:
            signal.alarm(0)  # Cancel timeout

# Global process manager instance
process_manager = SSHProcessManager()

class TerminalWidget(Gtk.Box):
    """A terminal widget that uses VTE for display and system SSH client for connections"""
    __gtype_name__ = 'TerminalWidget'
    
    # Signals
    __gsignals__ = {
        'connection-established': (GObject.SignalFlags.RUN_FIRST, None, ()),
        'connection-failed': (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'connection-lost': (GObject.SignalFlags.RUN_FIRST, None, ()),
        'title-changed': (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }
    
    def __init__(self, connection, config, connection_manager):
        # Initialize as a vertical Gtk.Box
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        
        # Store references
        self.connection = connection
        self.config = config
        self.connection_manager = connection_manager
        
        # Process tracking
        self.process = None
        self.process_pid = None
        self.process_pgid = None
        self.is_connected = False
        self.watch_id = 0
        self.ssh_client = None
        self.session_id = str(id(self))  # Unique ID for this session
        self._is_quitting = False  # Flag to suppress signal handlers during quit
        self.last_error_message = None  # Store last SSH error for reporting
        self._fallback_timer_id = None  # GLib timeout ID for spawn fallback
        
        # Register with process manager
        process_manager.register_terminal(self)
        
        # Connect to signals
        self.connect('destroy', self._on_destroy)
        
        # Connect to connection manager signals using GObject.GObject.connect directly
        self._connection_updated_handler = GObject.GObject.connect(connection_manager, 'connection-updated', self._on_connection_updated_signal)
        logger.debug("Connected to connection-updated signal")
        
        # Create scrolled window for terminal
        self.scrolled_window = Gtk.ScrolledWindow()
        self.scrolled_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        
        # Set up the terminal
        self.vte = Vte.Terminal()
        
        # Initialize terminal with basic settings and apply configured theme early
        self.setup_terminal()
        try:
            self.apply_theme()
        except Exception:
            pass
        
        # Add terminal to scrolled window and to the box via an overlay with a connecting view
        self.scrolled_window.set_child(self.vte)
        self.overlay = Gtk.Overlay()
        self.overlay.set_child(self.scrolled_window)

        # Connecting overlay elements
        self.connecting_bg = Gtk.Box()
        self.connecting_bg.set_hexpand(True)
        self.connecting_bg.set_vexpand(True)
        try:
            provider = Gtk.CssProvider()
            provider.load_from_data(b".connecting-bg { background-color: #000000; }")
            display = Gdk.Display.get_default()
            if display:
                Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            if hasattr(self.connecting_bg, 'add_css_class'):
                self.connecting_bg.add_css_class('connecting-bg')
        except Exception:
            pass

        self.connecting_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.connecting_box.set_halign(Gtk.Align.CENTER)
        self.connecting_box.set_valign(Gtk.Align.CENTER)
        spinner = Gtk.Spinner()
        spinner.start()
        label = Gtk.Label()
        label.set_markup('<span color="#FFFFFF">Connecting</span>')
        self.connecting_box.append(spinner)
        self.connecting_box.append(label)

        self.overlay.add_overlay(self.connecting_bg)
        self.overlay.add_overlay(self.connecting_box)

        # Disconnected banner with reconnect button at the bottom (separate panel below terminal)
        # Install CSS for a solid red background banner once
        try:
            display = Gdk.Display.get_default()
            if display and not getattr(display, '_sshpilot_banner_css_installed', False):
                css_provider = Gtk.CssProvider()
                css_provider.load_from_data(b"""
                    .error-toolbar.toolbar {
                        background-color: #cc0000;
                        color: #ffffff;
                        border-radius: 0;
                        padding-top: 10px;
                        padding-bottom: 10px;
                    }
                    .error-toolbar.toolbar label { color: #ffffff; }
                    .reconnect-button { background: #4a4a4a; color: #ffffff; border-radius: 4px; padding: 6px 10px; }
                    .reconnect-button:hover { background: #3f3f3f; }
                    .reconnect-button:active { background: #353535; }
                """)
                Gtk.StyleContext.add_provider_for_display(
                    display, css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                )
                setattr(display, '_sshpilot_banner_css_installed', True)
        except Exception:
            pass

        # Create error toolbar with same structure as sidebar toolbar
        self.disconnected_banner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.disconnected_banner.set_halign(Gtk.Align.FILL)
        self.disconnected_banner.set_valign(Gtk.Align.END)
        self.disconnected_banner.set_margin_start(0)
        self.disconnected_banner.set_margin_end(0)
        self.disconnected_banner.set_margin_top(0)
        self.disconnected_banner.set_margin_bottom(0)
        try:
            self.disconnected_banner.add_css_class('toolbar')
            self.disconnected_banner.add_css_class('error-toolbar')
            # Add a unique class per instance so we can set a per-widget min-height via CSS
            self._banner_unique_class = f"banner-{id(self)}"
            self.disconnected_banner.add_css_class(self._banner_unique_class)
        except Exception:
            pass
        # Banner content: icon + label + spacer + reconnect + dismiss, matching toolbar layout
        icon = Gtk.Image.new_from_icon_name('dialog-error-symbolic')
        icon.set_valign(Gtk.Align.CENTER)
        self.disconnected_banner.append(icon)
        self.disconnected_banner_label = Gtk.Label()
        self.disconnected_banner_label.set_halign(Gtk.Align.START)
        self.disconnected_banner_label.set_valign(Gtk.Align.CENTER)
        self.disconnected_banner_label.set_hexpand(True)
        self.disconnected_banner_label.set_text(_('Session ended.'))
        self.disconnected_banner.append(self.disconnected_banner_label)
        self.reconnect_button = Gtk.Button.new_with_label(_('Reconnect'))
        try:
            self.reconnect_button.add_css_class('reconnect-button')
        except Exception:
            pass
        self.reconnect_button.connect('clicked', self._on_reconnect_clicked)
        self.disconnected_banner.append(self.reconnect_button)

        # Dismiss button to hide the banner manually
        self.dismiss_button = Gtk.Button.new_with_label(_('Dismiss'))
        try:
            self.dismiss_button.add_css_class('flat')
            self.dismiss_button.add_css_class('reconnect-button')
        except Exception:
            pass
        self.dismiss_button.connect('clicked', lambda *_: self._set_disconnected_banner_visible(False))
        self.disconnected_banner.append(self.dismiss_button)
        self.disconnected_banner.set_visible(False)

        # Allow window to force an exact height match to the sidebar toolbar using per-widget CSS min-height
        self._banner_css_provider = None
        def _apply_external_height(new_h: int):
            try:
                h = max(0, int(new_h))
                display = Gdk.Display.get_default()
                if not display:
                    return
                css = f".{self._banner_unique_class} {{ min-height: {h}px; }}"
                provider = Gtk.CssProvider()
                provider.load_from_data(css.encode('utf-8'))
                Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
                # Keep a reference to prevent GC; latest provider wins at same priority
                self._banner_css_provider = provider
            except Exception:
                pass
        self.set_banner_height = _apply_external_height

        # Container to stack terminal (overlay) above the banner panel
        self.container_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.container_box.set_hexpand(True)
        self.container_box.set_vexpand(True)
        self.container_box.append(self.overlay)
        self.container_box.append(self.disconnected_banner)

        self.append(self.container_box)
        
        # Set expansion properties
        self.scrolled_window.set_hexpand(True)
        self.scrolled_window.set_vexpand(True)
        self.vte.set_hexpand(True)
        self.vte.set_vexpand(True)
        
        # Connect terminal signals and store handler IDs for cleanup
        self._child_exited_handler = self.vte.connect('child-exited', self.on_child_exited)
        self._title_changed_handler = self.vte.connect('window-title-changed', self.on_title_changed)
        
        # Apply theme
        self.force_style_refresh()
        
        # Set visibility of child widgets (GTK4 style)
        self.scrolled_window.set_visible(True)
        self.vte.set_visible(True)
        
        # Show overlay initially
        self._set_connecting_overlay_visible(True)
        logger.debug("Terminal widget initialized")

    def _set_disconnected_banner_visible(self, visible: bool, message: str = None):
        try:
            # Allow callers (e.g., ssh-copy-id dialog) to suppress the red banner entirely
            if getattr(self, '_suppress_disconnect_banner', False):
                return
            if message:
                self.disconnected_banner_label.set_text(message)
            if hasattr(self.disconnected_banner, 'set_visible'):
                self.disconnected_banner.set_visible(visible)
        except Exception:
            pass

    def _on_reconnect_clicked(self, *args):
        """User clicked reconnect on the banner"""
        try:
            # Immediately hide banner and show connecting overlay
            self._set_disconnected_banner_visible(False)
            self._set_connecting_overlay_visible(True)
            # Reuse existing connect method
            if not self._connect_ssh():
                # Show banner again if failed to start reconnect
                self._set_connecting_overlay_visible(False)
                self._set_disconnected_banner_visible(True, _('Reconnect failed to start'))
        except Exception:
            self._set_connecting_overlay_visible(False)
            self._set_disconnected_banner_visible(True, _('Reconnect failed'))

    def _set_connecting_overlay_visible(self, visible: bool):
        try:
            if hasattr(self.connecting_bg, 'set_visible'):
                self.connecting_bg.set_visible(visible)
            if hasattr(self.connecting_box, 'set_visible'):
                self.connecting_box.set_visible(visible)
        except Exception:
            pass
    
    def _connect_ssh(self):
        """Connect to SSH host"""
        if not self.connection:
            logger.error("No connection configured")
            return False
            
        # Ensure VTE terminal is properly initialized
        if not hasattr(self, 'vte') or self.vte is None:
            logger.error("VTE terminal not initialized")
            return False
        
        try:
            # Connect in a separate thread to avoid blocking UI
            thread = threading.Thread(target=self._connect_ssh_thread)
            thread.daemon = True
            thread.start()
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to start SSH connection: {e}")
            GLib.idle_add(self._on_connection_failed, str(e))
            return False
    
    def _connect_ssh_thread(self):
        """SSH connection thread: directly spawn SSH and rely on its output for errors."""
        try:
            GLib.idle_add(self._setup_ssh_terminal)
        except Exception as e:
            logger.error(f"SSH connection failed: {e}")
            GLib.idle_add(self._on_connection_failed, str(e))
    
    def _setup_ssh_terminal(self):
        """Set up terminal with direct SSH command (called from main thread)"""
        try:
            ssh_cmd = ['ssh']

            # Read SSH behavior from config with sane defaults
            try:
                ssh_cfg = self.config.get_ssh_config() if hasattr(self.config, 'get_ssh_config') else {}
            except Exception:
                ssh_cfg = {}
            apply_adv = bool(ssh_cfg.get('apply_advanced', False))
            connect_timeout = int(ssh_cfg.get('connection_timeout', 10)) if apply_adv else None
            connection_attempts = int(ssh_cfg.get('connection_attempts', 1)) if apply_adv else None
            keepalive_interval = int(ssh_cfg.get('keepalive_interval', 30)) if apply_adv else None
            keepalive_count = int(ssh_cfg.get('keepalive_count_max', 3)) if apply_adv else None
            strict_host = str(ssh_cfg.get('strict_host_key_checking', '')) if apply_adv else ''
            auto_add_host_keys = bool(ssh_cfg.get('auto_add_host_keys', True))
            batch_mode = bool(ssh_cfg.get('batch_mode', False)) if apply_adv else False
            compression = bool(ssh_cfg.get('compression', True)) if apply_adv else False

            # Determine auth method from connection and retrieve any saved password
            password_auth_selected = False
            has_saved_password = False
            password_value = None
            auth_method = 0
            try:
                # In our UI: 0 = key-based, 1 = password
                auth_method = getattr(self.connection, 'auth_method', 0)
                password_auth_selected = (auth_method == 1)
                # Try to fetch stored password regardless of auth method
                password_value = getattr(self.connection, 'password', None)
                if (not password_value) and hasattr(self, 'connection_manager') and self.connection_manager:
                    password_value = self.connection_manager.get_password(self.connection.host, self.connection.username)
                has_saved_password = bool(password_value)
            except Exception:
                auth_method = 0
                password_auth_selected = False
                has_saved_password = False

            using_password = password_auth_selected or has_saved_password

            # Apply advanced args only when user explicitly enabled them
            if apply_adv:
                # Only enable BatchMode when NOT doing password auth (BatchMode disables prompts)
                if batch_mode and not using_password:
                    ssh_cmd.extend(['-o', 'BatchMode=yes'])
                if connect_timeout is not None:
                    ssh_cmd.extend(['-o', f'ConnectTimeout={connect_timeout}'])
                if connection_attempts is not None:
                    ssh_cmd.extend(['-o', f'ConnectionAttempts={connection_attempts}'])
                if keepalive_interval is not None:
                    ssh_cmd.extend(['-o', f'ServerAliveInterval={keepalive_interval}'])
                if keepalive_count is not None:
                    ssh_cmd.extend(['-o', f'ServerAliveCountMax={keepalive_count}'])
                if strict_host:
                    ssh_cmd.extend(['-o', f'StrictHostKeyChecking={strict_host}'])
                if compression:
                    ssh_cmd.append('-C')

            # Default to accepting new host keys non-interactively on fresh installs
            try:
                if (not strict_host) and auto_add_host_keys:
                    ssh_cmd.extend(['-o', 'StrictHostKeyChecking=accept-new'])
            except Exception:
                pass

            # Ensure SSH exits immediately on failure rather than waiting in background
            ssh_cmd.extend(['-o', 'ExitOnForwardFailure=yes'])

            # Only add verbose flag if explicitly enabled in config
            try:
                ssh_cfg = self.config.get_ssh_config() if hasattr(self.config, 'get_ssh_config') else {}
                verbosity = int(ssh_cfg.get('verbosity', 0))
                debug_enabled = bool(ssh_cfg.get('debug_enabled', False))
                v = max(0, min(3, verbosity))
                for _ in range(v):
                    ssh_cmd.append('-v')
                # Map verbosity to LogLevel to ensure messages are not suppressed by defaults
                if v == 1:
                    ssh_cmd.extend(['-o', 'LogLevel=VERBOSE'])
                elif v == 2:
                    ssh_cmd.extend(['-o', 'LogLevel=DEBUG2'])
                elif v >= 3:
                    ssh_cmd.extend(['-o', 'LogLevel=DEBUG3'])
                elif debug_enabled:
                    ssh_cmd.extend(['-o', 'LogLevel=DEBUG'])
                if v > 0 or debug_enabled:
                    logger.debug("SSH verbosity configured: -v x %d, LogLevel set", v)
            except Exception as e:
                logger.warning(f"Could not check SSH verbosity/debug settings: {e}")
                # Default to non-verbose on error

            # Add key file/options only for key-based auth
            if not password_auth_selected:
                # Get key selection mode
                key_select_mode = 0
                try:
                    key_select_mode = int(getattr(self.connection, 'key_select_mode', 0) or 0)
                except Exception:
                    pass

                # Only add specific key if key_select_mode == 1 (specific key)
                if key_select_mode == 1 and hasattr(self.connection, 'keyfile') and self.connection.keyfile and \
                   os.path.isfile(self.connection.keyfile) and \
                   not self.connection.keyfile.startswith('Select key file'):

                    # Prepare key for connection (add to ssh-agent if needed)
                    if hasattr(self, 'connection_manager') and self.connection_manager:
                        try:
                            if hasattr(self.connection_manager, 'prepare_key_for_connection'):
                                key_prepared = self.connection_manager.prepare_key_for_connection(self.connection.keyfile)
                                if key_prepared:
                                    logger.debug(f"Key prepared for connection: {self.connection.keyfile}")
                                else:
                                    logger.warning(f"Failed to prepare key for connection: {self.connection.keyfile}")
                        except Exception as e:
                            logger.warning(f"Error preparing key for connection: {e}")

                    ssh_cmd.extend(['-i', self.connection.keyfile])
                    logger.debug(f"Using SSH key: {self.connection.keyfile}")
                    ssh_cmd.extend(['-o', 'IdentitiesOnly=yes'])

                    # Add certificate if specified
                    if hasattr(self.connection, 'certificate') and self.connection.certificate and \
                       os.path.isfile(self.connection.certificate):
                        ssh_cmd.extend(['-o', f'CertificateFile={self.connection.certificate}'])
                        logger.debug(f"Using SSH certificate: {self.connection.certificate}")
                else:
                    logger.debug("Using default SSH key selection (key_select_mode=0 or no valid key specified)")

                # If a password exists, allow all standard authentication methods
                if has_saved_password:
                    ssh_cmd.extend([
                        '-o',
                        'PreferredAuthentications=gssapi-with-mic,hostbased,publickey,keyboard-interactive,password'
                    ])
            else:
                # Force password authentication when user chose password auth
                ssh_cmd.extend(['-o', 'PreferredAuthentications=password'])
                if getattr(self.connection, 'pubkey_auth_no', False):
                    ssh_cmd.extend(['-o', 'PubkeyAuthentication=no'])

            # Add X11 forwarding if enabled
            if hasattr(self.connection, 'x11_forwarding') and self.connection.x11_forwarding:
                ssh_cmd.append('-X')

            # Prepare command-related options (must appear before host)
            remote_cmd = ''
            local_cmd = ''
            try:
                if hasattr(self.connection, 'remote_command'):
                    remote_cmd = (self.connection.remote_command or '').strip()
                if not remote_cmd and hasattr(self.connection, 'data'):
                    remote_cmd = (self.connection.data.get('remote_command') or '').strip()
            except Exception:
                remote_cmd = ''
            try:
                if hasattr(self.connection, 'local_command'):
                    local_cmd = (self.connection.local_command or '').strip()
                if not local_cmd and hasattr(self.connection, 'data'):
                    local_cmd = (self.connection.data.get('local_command') or '').strip()
            except Exception:
                local_cmd = ''

            # If remote command is specified, request a TTY (twice for force allocation)
            if remote_cmd:
                ssh_cmd.extend(['-t', '-t'])

            # If local command specified, allow and set it via options
            if local_cmd:
                ssh_cmd.extend(['-o', 'PermitLocalCommand=yes'])
                # Pass exactly as user provided, letting ssh parse quoting
                ssh_cmd.extend(['-o', f'LocalCommand={local_cmd}'])

            # Add port forwarding rules with conflict checking
            if hasattr(self.connection, 'forwarding_rules'):
                port_conflicts = []
                port_checker = get_port_checker()

                # Check for port conflicts before adding rules
                for rule in self.connection.forwarding_rules:
                    if not rule.get('enabled', True):
                        continue

                    rule_type = rule.get('type')
                    listen_addr = rule.get('listen_addr', '127.0.0.1')
                    listen_port = rule.get('listen_port')
                    
                    # Check for local port conflicts (for local and dynamic forwarding)
                    if rule_type in ['local', 'dynamic'] and listen_port:
                        try:
                            conflicts = port_checker.get_port_conflicts([listen_port], listen_addr)
                            if conflicts:
                                port, port_info = conflicts[0]
                                conflict_msg = f"Port {port} is already in use"
                                if port_info.process_name:
                                    conflict_msg += f" by {port_info.process_name} (PID: {port_info.pid})"
                                port_conflicts.append(conflict_msg)
                                continue  # Skip this rule
                        except Exception as e:
                            logger.debug(f"Could not check port conflict for {listen_port}: {e}")
                    
                    # Add the forwarding rule if no conflicts
                    if rule_type == 'dynamic' and listen_port:
                        try:
                            ssh_cmd.extend(['-D', f"{listen_addr}:{listen_port}"])
                            logger.debug(f"Added dynamic port forwarding: {listen_addr}:{listen_port}")
                        except Exception as e:
                            logger.error(f"Failed to set up dynamic forwarding: {e}")
                            
                    elif rule_type == 'local' and listen_port and 'remote_host' in rule and 'remote_port' in rule:
                        try:
                            remote_host = rule.get('remote_host', 'localhost')
                            remote_port = rule.get('remote_port')
                            ssh_cmd.extend(['-L', f"{listen_addr}:{listen_port}:{remote_host}:{remote_port}"])
                            logger.debug(f"Added local port forwarding: {listen_addr}:{listen_port} -> {remote_host}:{remote_port}")
                        except Exception as e:
                            logger.error(f"Failed to set up local forwarding: {e}")
                            
                    # Handle remote port forwarding (remote bind -> local destination)
                    elif rule_type == 'remote' and listen_port:
                        try:
                            local_host = rule.get('local_host') or rule.get('remote_host', 'localhost')
                            local_port = rule.get('local_port') or rule.get('remote_port')
                            if local_port:
                                ssh_cmd.extend(['-R', f"{listen_addr}:{listen_port}:{local_host}:{local_port}"])
                                logger.debug(f"Added remote port forwarding: {listen_addr}:{listen_port} -> {local_host}:{local_port}")
                        except Exception as e:
                            logger.error(f"Failed to set up remote forwarding: {e}")
                
                # Show port conflict warnings if any
                if port_conflicts:
                    conflict_message = "Port forwarding conflicts detected:\n" + "\n".join([f"• {msg}" for msg in port_conflicts])
                    logger.warning(conflict_message)
                    GLib.idle_add(self._show_forwarding_error_dialog, conflict_message)
                
                # Add extra SSH config options from advanced tab
                extra_ssh_config = getattr(self.connection, 'extra_ssh_config', '').strip()
                if extra_ssh_config:
                    logger.debug(f"Adding extra SSH config options: {extra_ssh_config}")
                    # Parse and add each extra SSH config option
                    for line in extra_ssh_config.split('\n'):
                        line = line.strip()
                        if line and not line.startswith('#'):  # Skip empty lines and comments
                            # Split on first space to separate option and value
                            parts = line.split(' ', 1)
                            if len(parts) == 2:
                                option, value = parts
                                ssh_cmd.extend(['-o', f"{option}={value}"])
                                logger.debug(f"Added SSH option: {option}={value}")
                            elif len(parts) == 1:
                                # Option without value (e.g., "Compression yes" becomes "Compression=yes")
                                option = parts[0]
                                ssh_cmd.extend(['-o', f"{option}=yes"])
                                logger.debug(f"Added SSH option: {option}=yes")
                
                # Add NumberOfPasswordPrompts option before hostname and command
                ssh_cmd.extend(['-o', 'NumberOfPasswordPrompts=1'])
                
                # Add port if not default (must be before host)
                if hasattr(self.connection, 'port') and self.connection.port != 22:
                    ssh_cmd.extend(['-p', str(self.connection.port)])
                
                # Add host and user
                ssh_cmd.append(f"{self.connection.username}@{self.connection.host}" if hasattr(self.connection, 'username') and self.connection.username else self.connection.host)

                # Append remote command last so ssh treats it as the command to run, ensure shell remains active
                if remote_cmd:
                    final_remote_cmd = remote_cmd if 'exec $SHELL' in remote_cmd else f"{remote_cmd} ; exec $SHELL -l"
                    # Append as single argument; let shell on remote parse quotes. Keep as-is to allow user quoting.
                    ssh_cmd.append(final_remote_cmd)
                
                # Make sure ssh will prompt in our VTE if no saved password:
                if (password_auth_selected or auth_method == 0) and not has_saved_password:
                    if '-t' not in ssh_cmd and '-tt' not in ssh_cmd:
                        ssh_cmd.append('-t')  # force a TTY for interactive password
            
                            # Log the SSH command
                try:
                    logger.debug(f"SSH command: {' '.join(ssh_cmd)}")
                except Exception:
                    logger.debug("Prepared SSH command")
                
                # End of SSH command building

            # Start the SSH process using VTE's spawn_async with our PTY
            logger.debug(f"Flatpak debug: About to spawn SSH with command: {ssh_cmd}")

            # Handle password authentication with sshpass if available
            env = os.environ.copy()
            logger.debug(f"Initial environment SSH_ASKPASS: {env.get('SSH_ASKPASS', 'NOT_SET')}, SSH_ASKPASS_REQUIRE: {env.get('SSH_ASKPASS_REQUIRE', 'NOT_SET')}")

            if has_saved_password and password_value:
                # Use sshpass for password authentication
                import shutil
                sshpass_path = None
                
                # Check if sshpass is available and executable
                if os.path.exists('/app/bin/sshpass') and os.access('/app/bin/sshpass', os.X_OK):
                    sshpass_path = '/app/bin/sshpass'
                    logger.debug("Found sshpass at /app/bin/sshpass")
                elif shutil.which('sshpass'):
                    sshpass_path = shutil.which('sshpass')
                    logger.debug(f"Found sshpass in PATH: {sshpass_path}")
                else:
                    logger.debug("sshpass not found or not executable")
                
                if sshpass_path:
                    # Use the same approach as ssh_password_exec.py for consistency
                    from .ssh_password_exec import _mk_priv_dir, _write_once_fifo
                    import threading
                    
                    # Create private temp directory and FIFO
                    tmpdir = _mk_priv_dir()
                    fifo = os.path.join(tmpdir, "pw.fifo")
                    os.mkfifo(fifo, 0o600)
                    
                    # Start writer thread that writes the password exactly once
                    t = threading.Thread(target=_write_once_fifo, args=(fifo, password_value), daemon=True)
                    t.start()
                    
                    # Use sshpass with FIFO
                    ssh_cmd = [sshpass_path, "-f", fifo] + ssh_cmd
                    
                    # Important: strip askpass vars so OpenSSH won't try the askpass helper for passwords
                    env.pop("SSH_ASKPASS", None)
                    env.pop("SSH_ASKPASS_REQUIRE", None)
                    
                    logger.debug("Using sshpass with FIFO for password authentication")
                    logger.debug(f"Environment after removing SSH_ASKPASS: SSH_ASKPASS={env.get('SSH_ASKPASS', 'NOT_SET')}, SSH_ASKPASS_REQUIRE={env.get('SSH_ASKPASS_REQUIRE', 'NOT_SET')}")
                    
                    # Store tmpdir for cleanup
                    self._sshpass_tmpdir = tmpdir
                else:
                    # sshpass not available, fallback to askpass for password prompts
                    ensure_askpass_script()
                    askpass_env = get_ssh_env_with_askpass_for_password(self.connection.host, self.connection.username)
                    env.update(askpass_env)
            elif (password_auth_selected or auth_method == 0) and not has_saved_password:
                # Password may be required but none saved - allow interactive prompt
                logger.debug("No saved password - using interactive prompt if required")
            else:
                # Use askpass for passphrase prompts (key-based auth)
                from .askpass_utils import get_ssh_env_with_askpass
                askpass_env = get_ssh_env_with_askpass()
                env.update(askpass_env)
            env['TERM'] = env.get('TERM', 'xterm-256color')
            env['SHELL'] = env.get('SHELL', '/bin/bash')
            env['SSHPILOT_FLATPAK'] = '1'
            # Add /app/bin to PATH for Flatpak compatibility
            if os.path.exists('/app/bin'):
                current_path = env.get('PATH', '')
                if '/app/bin' not in current_path:
                    env['PATH'] = f"/app/bin:{current_path}"
            
            # Convert environment dict to list format expected by VTE
            env_list = []
            for key, value in env.items():
                env_list.append(f"{key}={value}")
            
            # Log the command being executed for debugging
            logger.debug(f"Spawning SSH command: {ssh_cmd}")
            logger.debug(f"Environment PATH: {env.get('PATH', 'NOT_SET')}")
            
            # Create a new PTY for the terminal
            pty = Vte.Pty.new_sync(Vte.PtyFlags.DEFAULT)
            
            try:
                self.vte.spawn_async(
                    Vte.PtyFlags.DEFAULT,
                    os.path.expanduser('~') or '/',
                    ssh_cmd,
                    env_list,  # Use environment list for Flatpak
                    GLib.SpawnFlags.DEFAULT,
                    None,  # Child setup function
                    None,  # Child setup data
                    -1,    # Timeout (-1 = default)
                    None,  # Cancellable
                    self._on_spawn_complete,
                    ()     # User data - empty tuple for Flatpak VTE compatibility
                )
            except GLib.Error as e:
                logger.error(f"VTE spawn failed with GLib error: {e}")
                # Check if it's a "No such file or directory" error for sshpass
                if "sshpass" in str(e) and "No such file or directory" in str(e):
                    logger.error("sshpass binary not found, falling back to askpass")
                    # Fall back to askpass method
                    self._fallback_to_askpass(ssh_cmd, env_list)
                else:
                    self._on_connection_failed(str(e))
                return
            except Exception as e:
                logger.error(f"VTE spawn failed with exception: {e}")
                self._on_connection_failed(str(e))
                return
            
            # Store the PTY for later cleanup
            self.pty = pty
            try:
                import time
                self._spawn_start_time = time.time()
            except Exception:
                self._spawn_start_time = None
            
            # Defer marking as connected until spawn completes
            try:
                self.apply_theme()
            except Exception:
                pass
            
            # Apply theme after connection is established
            self.apply_theme()
            
            # Focus the terminal
            self.vte.grab_focus()

            # Add fallback timer to hide spinner if spawn completion doesn't fire
            self._fallback_timer_id = GLib.timeout_add_seconds(5, self._fallback_hide_spinner)

            logger.info(f"SSH terminal connected to {self.connection}")
            
        except Exception as e:
            logger.error(f"Failed to setup SSH terminal: {e}")
            self._on_connection_failed(str(e))
    
    def _fallback_to_askpass(self, ssh_cmd, env_list):
        """Fallback method when sshpass fails - use askpass instead"""
        try:
            logger.info("Falling back to askpass method for password authentication")
            
            # Remove sshpass from the command
            if ssh_cmd[0] == 'sshpass':
                ssh_cmd = ssh_cmd[3:]  # Remove sshpass, -f, and fifo_path
            
            # Set up askpass environment
            from .askpass_utils import get_ssh_env_with_askpass_for_password
            askpass_env = get_ssh_env_with_askpass_for_password(self.connection.host, self.connection.username)
            
            # Update environment list
            for key, value in askpass_env.items():
                env_list.append(f"{key}={value}")
            
            logger.debug(f"Fallback SSH command: {ssh_cmd}")
            
            # Try spawning again with askpass
            self.vte.spawn_async(
                Vte.PtyFlags.DEFAULT,
                os.path.expanduser('~') or '/',
                ssh_cmd,
                env_list,
                GLib.SpawnFlags.DEFAULT,
                None,
                None,
                -1,
                None,
                self._on_spawn_complete,
                ()
            )
            
        except Exception as e:
            logger.error(f"Fallback to askpass also failed: {e}")
            self._on_connection_failed(str(e))
    
    def _on_spawn_complete(self, terminal, pid, error, user_data=None):
        """Called when terminal spawn is complete"""
        # Skip if terminal is quitting
        if getattr(self, '_is_quitting', False):
            logger.debug("Terminal is quitting, skipping spawn complete handler")
            return

        # Cancel fallback timer if it's still pending
        if getattr(self, '_fallback_timer_id', None):
            try:
                GLib.source_remove(self._fallback_timer_id)
            except Exception:
                pass
            self._fallback_timer_id = None

        logger.debug(f"Flatpak debug: _on_spawn_complete called with pid={pid}, error={error}, user_data={user_data}")
        
        if error:
            logger.error(f"Terminal spawn failed: {error}")
            # Ensure theme is applied before showing error so bg doesn't flash white
            try:
                self.apply_theme()
            except Exception:
                pass
            self._on_connection_failed(str(error))
            return

        logger.debug(f"Terminal spawned with PID: {pid}")
        self.process_pid = pid
        
        try:
            # Get and store process group ID
            self.process_pgid = os.getpgid(pid)
            logger.debug(f"Process group ID: {self.process_pgid}")
            
            # Store process info for cleanup
            with process_manager.lock:
                # Determine command type based on connection type
                command_type = 'bash' if hasattr(self.connection, 'host') and self.connection.host == 'localhost' else 'ssh'
                process_manager.processes[pid] = {
                    'terminal': weakref.ref(self),
                    'start_time': datetime.now(),
                    'command': command_type,
                    'pgid': self.process_pgid
                }
            
            # Grab focus and apply theme
            self.vte.grab_focus()
            self.apply_theme()

            # Spawn succeeded; mark as connected and hide overlay
            self.is_connected = True
            
            # Update connection status in the connection manager (only for SSH connections)
            if hasattr(self, 'connection') and self.connection and hasattr(self.connection, 'host') and self.connection.host != 'localhost':
                if hasattr(self, 'connection_manager') and self.connection_manager:
                    self.connection_manager.update_connection_status(self.connection, True)
                    logger.debug(f"Terminal {self.session_id} updated connection status to connected")
                else:
                    logger.warning(f"Terminal {self.session_id} has no connection manager")
            elif hasattr(self, 'connection') and self.connection and hasattr(self.connection, 'host') and self.connection.host == 'localhost':
                logger.debug(f"Local terminal {self.session_id} spawned successfully")
            else:
                logger.warning(f"Terminal {self.session_id} has no connection object to update")
            
            self.emit('connection-established')
            self._set_connecting_overlay_visible(False)
            # Ensure any reconnect/disconnected banner is hidden upon successful spawn
            try:
                self._set_disconnected_banner_visible(False)
            except Exception:
                pass
            
        except Exception as e:
            logger.error(f"Error in spawn complete: {e}")
            self._on_connection_failed(str(e))
    
    def _fallback_hide_spinner(self):
        """Fallback method to hide spinner if spawn completion doesn't fire"""
        # Clear stored timer ID
        self._fallback_timer_id = None

        # Skip if terminal is quitting
        if getattr(self, '_is_quitting', False):
            logger.debug("Terminal is quitting, skipping fallback hide spinner")
            return False

        logger.debug("Flatpak debug: Fallback hide spinner called")

        # If a connection error was recorded, skip forcing a connected state
        if self.last_error_message:
            logger.debug("Fallback timer triggered after connection failure; ignoring")
            return False

        if not self.is_connected:
            logger.warning("Flatpak: Spawn completion callback didn't fire, forcing connection state")
            self.is_connected = True
            
            # Update connection status in the connection manager (only for SSH connections)
            if hasattr(self, 'connection') and self.connection and hasattr(self.connection, 'host') and self.connection.host != 'localhost':
                if hasattr(self, 'connection_manager') and self.connection_manager:
                    self.connection_manager.update_connection_status(self.connection, True)
                    logger.debug(f"Terminal {self.session_id} updated connection status to connected (fallback)")
                else:
                    logger.warning(f"Terminal {self.session_id} has no connection manager (fallback)")
            elif hasattr(self, 'connection') and self.connection and hasattr(self.connection, 'host') and self.connection.host == 'localhost':
                logger.debug(f"Local terminal {self.session_id} spawned successfully (fallback)")
            else:
                logger.warning(f"Terminal {self.session_id} has no connection object to update (fallback)")
            
            self.emit('connection-established')
            self._set_connecting_overlay_visible(False)
            try:
                self._set_disconnected_banner_visible(False)
            except Exception:
                pass
        return False  # Don't repeat the timer
        

    def _show_forwarding_error_dialog(self, message):
        try:
            dialog = Adw.MessageDialog(
                transient_for=self.get_root() if hasattr(self, 'get_root') else None,
                modal=True,
                heading="Port Forwarding Error",
                body=str(message)
            )
            dialog.add_response('ok', 'OK')
            dialog.set_default_response('ok')
            dialog.present()
        except Exception as e:
            logger.debug(f"Failed to present forwarding error dialog: {e}")
        return False
        
    def apply_theme(self, theme_name=None):
        """Apply terminal theme and font settings
        
        Args:
            theme_name (str, optional): Name of the theme to apply. If None, uses the saved theme.
        """
        try:
            if theme_name is None and self.config:
                # Get the saved theme from config
                theme_name = self.config.get_setting('terminal.theme', 'default')
                
            # Get the theme profile from config
            if self.config:
                profile = self.config.get_terminal_profile(theme_name)
            else:
                # Fallback default theme
                profile = {
                    'foreground': '#000000',  # Black text
                    'background': '#FFFFFF',  # White background
                    'font': 'Monospace 12',
                    'cursor_color': '#000000',
                    'highlight_background': '#4A90E2',
                    'highlight_foreground': '#FFFFFF',
                }
            
            # Set colors
            fg_color = Gdk.RGBA()
            fg_color.parse(profile['foreground'])
            
            bg_color = Gdk.RGBA()
            bg_color.parse(profile['background'])
            
            cursor_color = Gdk.RGBA()
            cursor_color.parse(profile.get('cursor_color', profile['foreground']))
            
            highlight_bg = Gdk.RGBA()
            highlight_bg.parse(profile.get('highlight_background', '#4A90E2'))
            
            highlight_fg = Gdk.RGBA()
            highlight_fg.parse(profile.get('highlight_foreground', profile['foreground']))
            
            # Apply colors to terminal
            self.vte.set_colors(fg_color, bg_color, None)
            self.vte.set_color_cursor(cursor_color)
            self.vte.set_color_highlight(highlight_bg)
            self.vte.set_color_highlight_foreground(highlight_fg)

            # Also color the container background to prevent white flash before VTE paints
            try:
                rgba = bg_color
                # For Gtk4, setting the widget style via CSS provider
                provider = Gtk.CssProvider()
                css = f".terminal-bg {{ background-color: rgba({int(rgba.red*255)}, {int(rgba.green*255)}, {int(rgba.blue*255)}, {rgba.alpha}); }}"
                provider.load_from_data(css.encode('utf-8'))
                display = Gdk.Display.get_default()
                if display:
                    Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
                if hasattr(self, 'add_css_class'):
                    self.add_css_class('terminal-bg')
                if hasattr(self.scrolled_window, 'add_css_class'):
                    self.scrolled_window.add_css_class('terminal-bg')
                if hasattr(self.vte, 'add_css_class'):
                    self.vte.add_css_class('terminal-bg')
            except Exception as e:
                logger.debug(f"Failed to set container background: {e}")
            
            # Set font
            font_desc = Pango.FontDescription.from_string(profile['font'])
            self.vte.set_font(font_desc)
            
            # Force a redraw
            self.vte.queue_draw()
            
            logger.debug(f"Applied terminal theme: {theme_name or 'default'}")
            
        except Exception as e:
            logger.error(f"Failed to apply terminal theme: {e}")
            
    def force_style_refresh(self):
        """Force a style refresh of the terminal widget."""
        self.apply_theme()
    
    def setup_terminal(self):
        """Initialize the VTE terminal with appropriate settings."""
        logger.info("Setting up terminal...")
        
        try:
            # Set terminal font
            font_desc = Pango.FontDescription()
            font_desc.set_family("Monospace")
            font_desc.set_size(12 * Pango.SCALE)  # Slightly larger default font
            self.vte.set_font(font_desc)
            
            # Do not force a light default; theme will define colors
            self.apply_theme()
            
            # Set cursor properties
            self.vte.set_cursor_blink_mode(Vte.CursorBlinkMode.ON)
            self.vte.set_cursor_shape(Vte.CursorShape.BLOCK)
            
            # Set scrollback lines
            self.vte.set_scrollback_lines(10000)
            
            # Set word char exceptions (for double-click selection)
            try:
                # Try the newer API first (VTE 0.60+)
                if hasattr(self.vte, 'set_word_char_exceptions'):
                    self.vte.set_word_char_exceptions("@-./_~")
                    logger.debug("Set word char exceptions using VTE 0.60+ API")
                # Fall back to the older API if needed
                elif hasattr(self.vte, 'set_word_char_options'):
                    self.vte.set_word_char_options("@-./_~")
                    logger.debug("Set word char exceptions using older VTE API")
            except Exception as e:
                logger.warning(f"Could not set word char options: {e}")
            
            # Set cursor and selection colors
            try:
                cursor_color = Gdk.RGBA()
                cursor_color.parse('black')  # Black cursor
                
                if hasattr(self.vte, 'set_color_cursor'):
                    self.vte.set_color_cursor(cursor_color)
                    logger.debug("Set cursor color")
                
                # Set selection colors
                if hasattr(self.vte, 'set_color_highlight'):
                    highlight_bg = Gdk.RGBA()
                    highlight_bg.parse('#4A90E2')  # Light blue highlight
                    self.vte.set_color_highlight(highlight_bg)
                    logger.debug("Set highlight color")
                    
                    highlight_fg = Gdk.RGBA()
                    highlight_fg.parse('white')
                    if hasattr(self.vte, 'set_color_highlight_foreground'):
                        self.vte.set_color_highlight_foreground(highlight_fg)
                        logger.debug("Set highlight foreground color")
                        
            except Exception as e:
                logger.warning(f"Could not set terminal colors: {e}")
            
            # Enable mouse reporting if available
            if hasattr(self.vte, 'set_mouse_autohide'):
                self.vte.set_mouse_autohide(True)
                logger.debug("Enabled mouse autohide")
                
            # Set terminal encoding
            try:
                self.vte.set_encoding('UTF-8')
                logger.debug("Set terminal encoding to UTF-8")
            except Exception as e:
                logger.warning(f"Could not set terminal encoding: {e}")
                
            # Enable bold text
            try:
                if hasattr(self.vte, 'set_allow_bold'):
                    self.vte.set_allow_bold(True)
                    logger.debug("Enabled bold text")
            except Exception as e:
                logger.warning(f"Could not enable bold text: {e}")
                
            logger.info("Terminal setup complete")
            
            # Enable bold text
            self.vte.set_allow_bold(True)
            
            # Show the terminal
            self.vte.show()
            logger.info("Terminal setup complete")
            
        except Exception as e:
            logger.error(f"Error in setup_terminal: {e}", exc_info=True)
            raise
        
        # Install terminal shortcuts and custom context menu
        self._install_shortcuts()
        self._setup_context_menu()

    def setup_local_shell(self):
        """Set up the terminal for local shell (not SSH)"""
        logger.info("Setting up local shell terminal")
        try:
            # Hide connecting overlay immediately for local shell
            self._set_connecting_overlay_visible(False)
            
            # Set up the terminal for local shell
            self.setup_terminal()
            
            # Start a simple local shell - just like GNOME Terminal
            env = os.environ.copy()

            # Determine the user's preferred shell
            shell = env.get('SHELL') or pwd.getpwuid(os.getuid()).pw_shell or '/bin/bash'

            # Ensure we have a proper environment
            env['SHELL'] = shell
            if 'TERM' not in env:
                env['TERM'] = 'xterm-256color'

            # Set initial title for local terminal
            self.emit('title-changed', 'Local Terminal')

            # Convert environment dict to list for VTE compatibility
            env_list = []
            for key, value in env.items():
                env_list.append(f"{key}={value}")

            # Start the user's shell as a login shell
            self.vte.spawn_async(
                Vte.PtyFlags.DEFAULT,
                os.path.expanduser('~') or '/',
                [shell, '-l'],
                env_list,
                GLib.SpawnFlags.DEFAULT,
                None,
                None,
                -1,
                None,
                self._on_spawn_complete,
                ()
            )

            # Add fallback timer to hide spinner if spawn completion doesn't fire
            self._fallback_timer_id = GLib.timeout_add_seconds(5, self._fallback_hide_spinner)

            logger.info("Local shell terminal setup initiated")
            
        except Exception as e:
            logger.error(f"Failed to setup local shell: {e}")
            self.emit('connection-failed', str(e))

    def _setup_context_menu(self):
        """Set up a robust per-terminal context menu and actions."""
        try:
            logger.debug("Setting up terminal context menu...")
            # Per-widget action group
            self._menu_actions = Gio.SimpleActionGroup()
            act_copy = Gio.SimpleAction.new("copy", None)
            act_copy.connect("activate", lambda a, p: self.copy_text())
            self._menu_actions.add_action(act_copy)
            act_paste = Gio.SimpleAction.new("paste", None)
            act_paste.connect("activate", lambda a, p: self.paste_text())
            self._menu_actions.add_action(act_paste)
            act_selall = Gio.SimpleAction.new("select_all", None)
            act_selall.connect("activate", lambda a, p: self.select_all())
            self._menu_actions.add_action(act_selall)
            
            # Add zoom actions
            act_zoom_in = Gio.SimpleAction.new("zoom_in", None)
            act_zoom_in.connect("activate", lambda a, p: self.zoom_in())
            self._menu_actions.add_action(act_zoom_in)
            
            act_zoom_out = Gio.SimpleAction.new("zoom_out", None)
            act_zoom_out.connect("activate", lambda a, p: self.zoom_out())
            self._menu_actions.add_action(act_zoom_out)
            
            act_reset_zoom = Gio.SimpleAction.new("reset_zoom", None)
            act_reset_zoom.connect("activate", lambda a, p: self.reset_zoom())
            self._menu_actions.add_action(act_reset_zoom)
            
            self.insert_action_group('term', self._menu_actions)

            # Menu model with keyboard shortcuts
            self._menu_model = Gio.Menu()
            import platform
            is_macos = platform.system() == 'Darwin'
            
            if is_macos:
                self._menu_model.append(_("Copy\t⌘C"), "term.copy")
                self._menu_model.append(_("Paste\t⌘V"), "term.paste")
                self._menu_model.append(_("Select All\t⌘A"), "term.select_all")
                # Create a separator section for zoom options
                zoom_section = Gio.Menu()
                zoom_section.append(_("Zoom In\t⌘="), "term.zoom_in")
                zoom_section.append(_("Zoom Out\t⌘-"), "term.zoom_out")
                zoom_section.append(_("Reset Zoom\t⌘0"), "term.reset_zoom")
                self._menu_model.append_section(None, zoom_section)
            else:
                self._menu_model.append(_("Copy\tCtrl+Shift+C"), "term.copy")
                self._menu_model.append(_("Paste\tCtrl+Shift+V"), "term.paste")
                self._menu_model.append(_("Select All\tCtrl+Shift+A"), "term.select_all")
                # Create a separator section for zoom options
                zoom_section = Gio.Menu()
                zoom_section.append(_("Zoom In\tCtrl++"), "term.zoom_in")
                zoom_section.append(_("Zoom Out\tCtrl+-"), "term.zoom_out")
                zoom_section.append(_("Reset Zoom\tCtrl+0"), "term.reset_zoom")
                self._menu_model.append_section(None, zoom_section)

            # Popover - set parent to the terminal widget
            self._menu_popover = Gtk.PopoverMenu.new_from_model(self._menu_model)
            self._menu_popover.set_has_arrow(True)
            # Set parent to the terminal widget
            self._menu_popover.set_parent(self.vte)

            # Right-click gesture to open popover
            gesture = Gtk.GestureClick()
            gesture.set_button(0)
            def _on_pressed(gest, n_press, x, y):
                try:
                    btn = 0
                    try:
                        btn = gest.get_current_button()
                    except Exception:
                        pass
                    logger.debug(f"Context menu gesture: button={btn}, x={x}, y={y}")
                    if btn not in (Gdk.BUTTON_SECONDARY, 3):
                        logger.debug(f"Not a right-click button: {btn}")
                        return
                    # Focus terminal first for reliable copy/paste
                    try:
                        self.vte.grab_focus()
                    except Exception:
                        pass
                    # Position popover near click
                    try:
                        rect = Gdk.Rectangle()
                        rect.x = int(x)
                        rect.y = int(y)
                        rect.width = 1
                        rect.height = 1
                        self._menu_popover.set_pointing_to(rect)
                        logger.debug("Context menu positioned, showing popup")
                    except Exception as e:
                        logger.error(f"Failed to position context menu: {e}")
                    self._menu_popover.popup()
                except Exception as e:
                    logger.error(f"Context menu popup failed: {e}")
            gesture.connect('pressed', _on_pressed)
            self.vte.add_controller(gesture)
            logger.debug("Terminal context menu setup completed successfully")
        except Exception as e:
            logger.error(f"Context menu setup failed: {e}")

    def _install_shortcuts(self):
        """Install local shortcuts on the VTE widget for copy/paste/select-all"""
        try:
            controller = Gtk.ShortcutController()
            controller.set_scope(Gtk.ShortcutScope.LOCAL)
            
            def _cb_copy(widget, *args):
                try:
                    self.copy_text()
                except Exception:
                    pass
                return True
            def _cb_paste(widget, *args):
                try:
                    self.paste_text()
                except Exception:
                    pass
                return True
            def _cb_select_all(widget, *args):
                try:
                    self.select_all()
                except Exception:
                    pass
                return True
            
            import platform
            is_macos = platform.system() == 'Darwin'
            if is_macos:
                # macOS: Use standard Cmd+C/V for copy/paste, Cmd+Shift+C/V for terminal-specific operations
                copy_trigger = "<Meta>c"
                paste_trigger = "<Meta>v"
                select_trigger = "<Meta>a"
            else:
                # Linux/Windows: Use Ctrl+Shift+C/V for terminal copy/paste (standard for terminals)
                copy_trigger = "<Primary><Shift>c"
                paste_trigger = "<Primary><Shift>v"
                select_trigger = "<Primary><Shift>a"
            
            controller.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string(copy_trigger),
                Gtk.CallbackAction.new(_cb_copy)
            ))
            controller.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string(paste_trigger),
                Gtk.CallbackAction.new(_cb_paste)
            ))
            controller.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string(select_trigger),
                Gtk.CallbackAction.new(_cb_select_all)
            ))
            
            # Add zoom shortcuts
            if is_macos:
                # macOS: Use Cmd+= (equals key), Cmd+-, and Cmd+0 for zoom
                # Note: On macOS, Cmd+Shift+= is the same as Cmd+=
                zoom_in_trigger = "<Meta>equal"
                zoom_out_trigger = "<Meta>minus"
                zoom_reset_trigger = "<Meta>0"
            else:
                # Linux/Windows: Use Ctrl++, Ctrl+-, and Ctrl+0 for zoom
                zoom_in_trigger = "<Primary>plus"
                zoom_out_trigger = "<Primary>minus"
                zoom_reset_trigger = "<Primary>0"
            
            def _cb_zoom_in(widget, *args):
                try:
                    self.zoom_in()
                except Exception:
                    pass
                return True
            
            def _cb_zoom_out(widget, *args):
                try:
                    self.zoom_out()
                except Exception:
                    pass
                return True
            
            def _cb_reset_zoom(widget, *args):
                try:
                    self.reset_zoom()
                except Exception:
                    pass
                return True
            
            controller.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string(zoom_in_trigger),
                Gtk.CallbackAction.new(_cb_zoom_in)
            ))
            
            controller.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string(zoom_out_trigger),
                Gtk.CallbackAction.new(_cb_zoom_out)
            ))
            
            controller.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string(zoom_reset_trigger),
                Gtk.CallbackAction.new(_cb_reset_zoom)
            ))
            
            self.vte.add_controller(controller)
            
            # Add mouse wheel zoom functionality
            self._setup_mouse_wheel_zoom()
            
        except Exception as e:
            logger.debug(f"Failed to install shortcuts: {e}")
    
    def _setup_mouse_wheel_zoom(self):
        """Set up mouse wheel zoom functionality with Cmd+MouseWheel"""
        try:
            import platform
            is_macos = platform.system() == 'Darwin'
            
            scroll_controller = Gtk.EventControllerScroll()
            scroll_controller.set_flags(Gtk.EventControllerScrollFlags.VERTICAL)
            
            def _on_scroll(controller, dx, dy):
                try:
                    # Check if Command key (macOS) or Ctrl key (Linux/Windows) is pressed
                    modifiers = controller.get_current_event_state()
                    if is_macos:
                        # Check for Command key (Meta modifier)
                        if modifiers & Gdk.ModifierType.META_MASK:
                            if dy > 0:
                                self.zoom_out()
                            elif dy < 0:
                                self.zoom_in()
                            return True  # Consume the event
                    else:
                        # Check for Ctrl key
                        if modifiers & Gdk.ModifierType.CONTROL_MASK:
                            if dy > 0:
                                self.zoom_out()
                            elif dy < 0:
                                self.zoom_in()
                            return True  # Consume the event
                except Exception as e:
                    logger.debug(f"Error in mouse wheel zoom: {e}")
                return False  # Don't consume the event if modifier not pressed
            
            scroll_controller.connect('scroll', _on_scroll)
            self.vte.add_controller(scroll_controller)
            logger.debug("Mouse wheel zoom functionality installed")
            
        except Exception as e:
            logger.debug(f"Failed to setup mouse wheel zoom: {e}")
            
    # PTY forwarding is now handled automatically by VTE
    # No need for manual PTY management in this implementation
    
    def reconnect(self):
        """Reconnect the terminal with updated connection settings"""
        logger.info("Reconnecting terminal with updated settings...")
        was_connected = self.is_connected
        
        # Disconnect if currently connected
        if was_connected:
            self.disconnect()
        
        # Reconnect after a short delay to allow disconnection to complete
        def _reconnect():
            if self._connect_ssh():
                logger.info("Terminal reconnected with updated settings")
                # Ensure theme is applied after reconnection
                self.apply_theme()
                return True
            else:
                logger.error("Failed to reconnect terminal with updated settings")
                return False
        
        GLib.timeout_add(500, _reconnect)  # 500ms delay before reconnecting
    
    def _on_connection_updated_signal(self, sender, connection):
        """Signal handler for connection-updated signal"""
        self._on_connection_updated(connection)
        
    def _on_connection_updated(self, connection):
        """Called when connection settings are updated
        
        Note: We don't automatically reconnect here to prevent infinite loops.
        The main window will handle the reconnection flow after user confirmation.
        """
        if connection == self.connection:
            logger.info("Connection settings updated, waiting for user confirmation to reconnect...")
            # Just update our connection reference, don't reconnect automatically
            self.connection = connection
    
    def _on_connection_established(self):
        """Handle successful SSH connection"""
        logger.info(f"SSH connection to {self.connection.host} established")
        self.is_connected = True
        
        # Update connection status in the connection manager
        self.connection.is_connected = True
        self.connection_manager.emit('connection-status-changed', self.connection, True)
        
        self.emit('connection-established')

        # Apply theme after connection is established
        self.apply_theme()
        # Hide any reconnect banner on success
        self._set_disconnected_banner_visible(False)
        self.last_error_message = None
        
    def _on_connection_lost(self, message: str = None):
        """Handle SSH connection loss"""
        if self.is_connected:
            logger.info(f"SSH connection to {self.connection.host} lost")
            self.is_connected = False

            # Update connection status in the connection manager
            if hasattr(self, 'connection') and self.connection:
                self.connection.is_connected = False
                self.connection_manager.emit('connection-status-changed', self.connection, False)

            self.emit('connection-lost')
            # Show reconnect UI
            self._set_connecting_overlay_visible(False)
            banner_text = message or self.last_error_message or _('Connection lost.')
            self._set_disconnected_banner_visible(True, banner_text)
    
    def _on_terminal_input(self, widget, text, size):
        """Handle input from the terminal (handled automatically by VTE)"""
        pass
            
    def _on_terminal_resize(self, widget, width, height):
        """Handle terminal resize events"""
        # Update the SSH session if it exists
        if self.ssh_session and hasattr(self.ssh_session, 'change_terminal_size'):
            asyncio.create_task(
                self.ssh_session.change_terminal_size(
                    height, width, 0, 0
                )
            )
    
    def _on_ssh_disconnected(self, exc):
        """Called when SSH connection is lost"""
        if self.is_connected:
            self.is_connected = False
            if exc:
                logger.error(f"SSH connection lost: {exc}")
            GLib.idle_add(lambda: self.emit('connection-lost'))
    
    def _setup_process_group(self, spawn_data):
        """Setup function called after fork but before exec"""
        # Create new process group for the child process
        os.setpgrp()
        
    def _get_terminal_pid(self):
        """Get the PID of the terminal's child process"""
        # First try the stored PID
        if self.process_pid:
            try:
                # Verify the process still exists
                os.kill(self.process_pid, 0)
                return self.process_pid
            except (ProcessLookupError, OSError):
                pass
        
        # Fall back to getting from PTY or VTE helpers
        try:
            # Prefer PID recorded at spawn complete
            if getattr(self, 'process_pid', None):
                return self.process_pid
            pty = self.vte.get_pty()
            if pty and hasattr(pty, 'get_pid'):
                pid = pty.get_pid()
                if pid:
                    self.process_pid = pid
                    return pid
        except Exception as e:
            logger.error(f"Error getting terminal PID: {e}")
        
        return None
        
    def _on_destroy(self, widget):
        """Handle widget destruction"""
        logger.debug(f"Terminal widget {self.session_id} being destroyed")
        
        # Disconnect VTE signal handlers first to prevent callbacks on destroyed objects
        if hasattr(self, 'vte') and self.vte:
            try:
                if hasattr(self, '_child_exited_handler'):
                    self.vte.disconnect(self._child_exited_handler)
                    logger.debug("Disconnected child-exited signal handler")
                if hasattr(self, '_title_changed_handler'):
                    self.vte.disconnect(self._title_changed_handler)
                    logger.debug("Disconnected title-changed signal handler")
            except Exception as e:
                logger.error(f"Error disconnecting VTE signals: {e}")
        
        # Disconnect from connection manager signals
        if hasattr(self, '_connection_updated_handler') and hasattr(self.connection_manager, 'disconnect'):
            try:
                self.connection_manager.disconnect(self._connection_updated_handler)
                logger.debug("Disconnected from connection manager signals")
            except Exception as e:
                logger.error(f"Error disconnecting from connection manager: {e}")
        
        # Disconnect the terminal
        self.disconnect()
        
        # Remove from process manager terminals set (only if not already quitting)
        if not getattr(self, '_is_quitting', False):
            try:
                if self in process_manager.terminals:
                    process_manager.terminals.remove(self)
                    logger.debug(f"Removed terminal {self.session_id} from process manager terminals set")
            except Exception as e:
                logger.debug(f"Error removing terminal from process manager: {e}")

    def _terminate_process_tree(self, pid):
        """Terminate a process and all its children"""
        try:
            # First try to get the process group
            try:
                pgid = os.getpgid(pid)
                logger.debug(f"Terminating process group {pgid}")
                os.killpg(pgid, signal.SIGTERM)
                
                # Give processes a moment to shut down
                time.sleep(0.5)
                
                # Check if any processes are still running
                try:
                    os.killpg(pgid, 0)  # Check if process group exists
                    logger.debug(f"Process group {pgid} still running, sending SIGKILL")
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass  # Process group already gone
                    
            except ProcessLookupError:
                logger.debug(f"Process {pid} already terminated")
                return
                
            # Wait for process to terminate
            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass
                
        except Exception as e:
            logger.error(f"Error terminating process {pid}: {e}")
    
    def _cleanup_process(self, pid):
        """Clean up a process by PID"""
        if not pid:
            return False
            
        try:
            # Try to get process info from manager first
            pgid = None
            with process_manager.lock:
                if pid in process_manager.processes:
                    pgid = process_manager.processes[pid].get('pgid')
            
            # Fall back to getting PGID from system
            if not pgid:
                try:
                    pgid = os.getpgid(pid)
                except ProcessLookupError:
                    logger.debug(f"Process {pid} already terminated")
                    return True
            
            # First try a clean termination
            try:
                os.kill(pid, signal.SIGTERM)
                logger.debug(f"Sent SIGTERM to process {pid} (PGID: {pgid})")
                
                # Wait for clean termination
                for _ in range(5):  # Wait up to 0.5 seconds
                    try:
                        os.kill(pid, 0)
                        time.sleep(0.1)
                    except ProcessLookupError:
                        logger.debug(f"Process {pid} terminated cleanly")
                        return True
                
                # If still running, force kill
                try:
                    os.kill(pid, 0)  # Check if still exists
                    logger.debug(f"Process {pid} still running, sending SIGKILL")
                    if pgid:
                        try:
                            os.killpg(pgid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    os.kill(pid, signal.SIGKILL)
                    return True
                except ProcessLookupError:
                    return True
                    
            except ProcessLookupError:
                return True
                
        except Exception as e:
            logger.error(f"Error terminating process {pid}: {e}")
            return False
    
    def disconnect(self):
        """Close the SSH connection and clean up resources"""
        if not self.is_connected:
            return
            
        logger.debug(f"Disconnecting SSH session {self.session_id}...")
        was_connected = self.is_connected
        self.is_connected = False
        
        # Guard UI emissions when the root window is quitting
        root = self.get_root() if hasattr(self, 'get_root') else None
        is_quitting = bool(getattr(root, '_is_quitting', False))
        
        # Only update manager / UI if not quitting
        if was_connected and hasattr(self, 'connection') and self.connection and not is_quitting:
            self.connection.is_connected = False
            if hasattr(self, 'connection_manager') and self.connection_manager:
                GLib.idle_add(self.connection_manager.emit, 'connection-status-changed', self.connection, False)
        
        try:
            # Try to get the terminal's child PID (with timeout protection)
            pid = None
            try:
                pid = self._get_terminal_pid()
            except Exception as e:
                logger.debug(f"Error getting terminal PID during disconnect: {e}")
            
            # Collect all PIDs that need to be cleaned up
            pids_to_clean = set()
            
            # Add the main process PID if available
            if pid:
                pids_to_clean.add(pid)
            
            # Add the process group ID if available
            if hasattr(self, 'process_pgid') and self.process_pgid:
                pids_to_clean.add(self.process_pgid)
            
            # Add any PIDs from the process manager (with lock timeout)
            try:
                with process_manager.lock:
                    for proc_pid, proc_info in list(process_manager.processes.items()):
                        if proc_info.get('terminal')() is self:
                            pids_to_clean.add(proc_pid)
                            if 'pgid' in proc_info:
                                pids_to_clean.add(proc_info['pgid'])
            except Exception as e:
                logger.debug(f"Error accessing process manager during disconnect: {e}")
            
            # Clean up all collected PIDs (with error handling for each)
            for cleanup_pid in pids_to_clean:
                if cleanup_pid:
                    try:
                        self._cleanup_process(cleanup_pid)
                    except Exception as e:
                        logger.debug(f"Error cleaning up PID {cleanup_pid}: {e}")
            
            # Clean up PTY if it exists
            if hasattr(self, 'pty') and self.pty:
                try:
                    self.pty.close()
                except Exception as e:
                    logger.error(f"Error closing PTY: {e}")
                finally:
                    self.pty = None
            
            # Clean up sshpass temporary directory if it exists
            if hasattr(self, '_sshpass_tmpdir') and self._sshpass_tmpdir:
                try:
                    import shutil
                    shutil.rmtree(self._sshpass_tmpdir, ignore_errors=True)
                    logger.debug(f"Cleaned up sshpass tmpdir: {self._sshpass_tmpdir}")
                except Exception as e:
                    logger.debug(f"Error cleaning up sshpass tmpdir: {e}")
                finally:
                    self._sshpass_tmpdir = None
            
            # Clean up from process manager (only if not quitting)
            if not getattr(self, '_is_quitting', False):
                try:
                    with process_manager.lock:
                        for proc_pid in list(process_manager.processes.keys()):
                            proc_info = process_manager.processes[proc_pid]
                            if proc_info.get('terminal')() is self:
                                logger.debug(f"Removing process {proc_pid} from process manager for terminal {self.session_id}")
                                del process_manager.processes[proc_pid]
                except Exception as e:
                    logger.debug(f"Error cleaning up from process manager: {e}")
            
            # Do not hard-reset here; keep current theme/colors
            
            logger.debug(f"Cleaned up {len(pids_to_clean)} processes for session {self.session_id}")
            
        except Exception as e:
            logger.error(f"Error during disconnect: {e}")
        finally:
            # Clean up references
            self.process_pid = None
            self.process_pgid = None
            
            # Only emit connection-lost signal if not quitting
            if not is_quitting:
                self.emit('connection-lost')
            logger.debug(f"SSH session {self.session_id} disconnected")
    
    def _on_connection_failed(self, error_message):
        """Handle connection failure (called from main thread)"""
        logger.error(f"Connection failed: {error_message}")

        # Cancel any pending fallback timer so we don't mark connection as successful
        if getattr(self, '_fallback_timer_id', None):
            try:
                GLib.source_remove(self._fallback_timer_id)
            except Exception:
                pass
            self._fallback_timer_id = None

        try:
            # Show raw error in terminal
            error_msg = f"\r\n\x1b[31m{error_message}\x1b[0m\r\n"
            self.vte.feed(error_msg.encode('utf-8'))

            self.is_connected = False

            # Clean up PTY if it exists
            if hasattr(self, 'pty') and self.pty:
                self.pty.close()
                del self.pty

            # Remember last error for later reporting
            self.last_error_message = error_message

            # Notify UI
            self.emit('connection-failed', error_message)

            # Show reconnect banner with the raw SSH error
            self._set_connecting_overlay_visible(False)
            self._set_disconnected_banner_visible(True, error_message)

        except Exception as e:
            logger.error(f"Error in _on_connection_failed: {e}")

    def on_child_exited(self, terminal, status):
        """Handle terminal child process exit"""
        # Skip if terminal is quitting
        if getattr(self, '_is_quitting', False):
            logger.debug("Terminal is quitting, skipping child exit handler")
            return
            
        logger.debug(f"Terminal child exited with status: {status}")
        
        # Defer the heavy work to avoid blocking the signal handler
        # This prevents potential deadlocks with the UI thread
        def _handle_exit_cleanup():
            try:
                self._handle_child_exit_cleanup(status)
            except Exception as e:
                logger.error(f"Error in exit cleanup: {e}")
            return False  # Don't repeat
        
        # Schedule cleanup on the main thread
        GLib.idle_add(_handle_exit_cleanup)
    
    def _handle_child_exit_cleanup(self, status):
        """Handle the actual cleanup work for child process exit (called from main thread)"""
        logger.debug(f"Starting exit cleanup for status {status}")
        
        # Clean up process tracking immediately since the process has already exited
        try:
            # Skip getting PID since process is already dead - just clear our tracking
            logger.debug("Clearing process tracking for dead process")
            
            # Clear our stored PID first to prevent any attempts to interact with dead process
            old_pid = getattr(self, 'process_pid', None)
            self.process_pid = None
            
            # Clean up process manager tracking
            with process_manager.lock:
                if old_pid and old_pid in process_manager.processes:
                    logger.debug(f"Removing dead process {old_pid} from tracking")
                    del process_manager.processes[old_pid]
                
                # Remove this terminal from tracking
                if self in process_manager.terminals:
                    logger.debug(f"Removing terminal {id(self)} from tracking")
                    process_manager.terminals.remove(self)
            
            logger.debug("Process tracking cleanup completed")
        except Exception as e:
            logger.error(f"Error cleaning up exited process tracking: {e}")

        # Normalize exit status: GLib may pass waitpid-style status
        exit_code = None
        try:
            if os.WIFEXITED(status):
                exit_code = os.WEXITSTATUS(status)
            else:
                # If not a normal exit or os.WIF* not applicable, best-effort mapping
                exit_code = status if 0 <= int(status) < 256 else ((int(status) >> 8) & 0xFF)
        except Exception:
            try:
                exit_code = int(status)
            except Exception:
                exit_code = status

        # If user explicitly typed 'exit' (clean status 0), update status and close tab immediately
        try:
            if exit_code == 0 and hasattr(self, 'get_root'):
                # Update connection status BEFORE closing the tab
                logger.debug("Clean exit detected, updating connection status before closing tab")
                if self.connection:
                    self.connection.is_connected = False
                self.is_connected = False
                
                # Emit connection status change signal
                if hasattr(self, 'connection_manager') and self.connection_manager and self.connection:
                    GLib.idle_add(self.connection_manager.emit, 'connection-status-changed', self.connection, False)
                
                root = self.get_root()
                if root and hasattr(root, 'tab_view'):
                    page = root.tab_view.get_page(self)
                    if page:
                        try:
                            setattr(root, '_suppress_close_confirmation', True)
                            root.tab_view.close_page(page)
                        finally:
                            try:
                                setattr(root, '_suppress_close_confirmation', False)
                            except Exception:
                                pass
                        return
        except Exception:
            pass

        # Check if this is a controlled reconnect to avoid interfering with the reconnection process
        try:
            if hasattr(self, 'get_root') and self.get_root():
                root = self.get_root()
                if hasattr(root, '_is_controlled_reconnect') and root._is_controlled_reconnect:
                    logger.debug("Controlled reconnect in progress, skipping connection status update")
                    return
        except Exception:
            pass
        
        # Non-zero or unknown exit: treat as connection lost and show banner
        logger.debug("Updating connection status after process exit")
        if self.connection:
            self.connection.is_connected = False

        # Don't call disconnect() here since the process has already exited
        # Just update the connection status and emit signals
        self.is_connected = False
        
        # Update connection manager status
        logger.debug("Scheduling connection manager status update")
        if hasattr(self, 'connection_manager') and self.connection_manager and self.connection:
            GLib.idle_add(self.connection_manager.emit, 'connection-status-changed', self.connection, False)
        
        # Defer all signal emissions and UI updates to prevent deadlocks
        def _finalize_exit_cleanup():
            try:
                logger.debug("Emitting connection-lost signal")
                self.emit('connection-lost')

                # Show reconnect UI with detailed error if available
                logger.debug("Updating UI elements")
                self._set_connecting_overlay_visible(False)
                banner_text = self.last_error_message
                if not banner_text:
                    if exit_code and exit_code != 0:
                        banner_text = _('SSH exited with status {code}').format(code=exit_code)
                    else:
                        banner_text = _('Session ended.')
                self._set_disconnected_banner_visible(True, banner_text)

                logger.debug("Exit cleanup completed successfully")
            except Exception as e:
                logger.error(f"Error in final exit cleanup: {e}")
            return False
        
        # Schedule final cleanup on next idle cycle
        GLib.idle_add(_finalize_exit_cleanup)

    def on_title_changed(self, terminal):
        """Handle terminal title change"""
        title = terminal.get_window_title()
        if title:
            self.emit('title-changed', title)
        # If terminal is connected and a title update occurs (often when prompt is ready),
        # ensure the reconnect banner is hidden
        try:
            if getattr(self, 'is_connected', False):
                self._set_disconnected_banner_visible(False)
        except Exception:
            pass

    def on_bell(self, terminal):
        """Handle terminal bell"""
        # Could implement visual bell or notification here
        pass

    def copy_text(self):
        """Copy selected text to clipboard"""
        if self.vte.get_has_selection():
            self.vte.copy_clipboard_format(Vte.Format.TEXT)

    def paste_text(self):
        """Paste text from clipboard"""
        self.vte.paste_clipboard()

    def select_all(self):
        """Select all text in terminal"""
        self.vte.select_all()

    def zoom_in(self):
        """Zoom in the terminal font"""
        try:
            current_scale = self.vte.get_font_scale()
            new_scale = min(current_scale + 0.1, 5.0)  # Max zoom 5x
            self.vte.set_font_scale(new_scale)
            logger.debug(f"Terminal zoomed in to {new_scale:.1f}x")
        except Exception as e:
            logger.error(f"Failed to zoom in terminal: {e}")

    def zoom_out(self):
        """Zoom out the terminal font"""
        try:
            current_scale = self.vte.get_font_scale()
            new_scale = max(current_scale - 0.1, 0.5)  # Min zoom 0.5x
            self.vte.set_font_scale(new_scale)
            logger.debug(f"Terminal zoomed out to {new_scale:.1f}x")
        except Exception as e:
            logger.error(f"Failed to zoom out terminal: {e}")

    def reset_zoom(self):
        """Reset terminal zoom to default (1.0x)"""
        try:
            self.vte.set_font_scale(1.0)
            logger.debug("Terminal zoom reset to 1.0x")
        except Exception as e:
            logger.error(f"Failed to reset terminal zoom: {e}")

    def reset_terminal(self):
        """Reset terminal"""
        self.vte.reset(True, True)

    def reset_and_clear(self):
        """Reset and clear terminal"""
        self.vte.reset(True, False)

    def search_text(self, text, case_sensitive=False, regex=False):
        """Search for text in terminal"""
        try:
            # Create search regex
            if regex:
                search_regex = GLib.Regex.new(text, 0 if case_sensitive else GLib.RegexCompileFlags.CASELESS, 0)
            else:
                escaped_text = GLib.regex_escape_string(text)
                search_regex = GLib.Regex.new(escaped_text, 0 if case_sensitive else GLib.RegexCompileFlags.CASELESS, 0)
            
            # Set search regex
            self.vte.search_set_regex(search_regex, 0)
            
            # Find next match
            return self.vte.search_find_next()
            
        except Exception as e:
            logger.error(f"Search failed: {e}")
            return False

    def get_connection_info(self):
        """Get connection information"""
        if self.connection:
            return {
                'nickname': self.connection.nickname,
                'host': self.connection.host,
                'username': self.connection.username,
                'connected': self.is_connected
            }
        return None
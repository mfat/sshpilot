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
import asyncio
import errno
import threading
import weakref
import subprocess
from datetime import datetime

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
        if self.cleanup_thread is None or not self.cleanup_thread.is_alive():
            self.cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
            self.cleanup_thread.start()
            logger.debug("Started SSH cleanup thread")
    
    def _cleanup_loop(self):
        """Background cleanup loop"""
        while True:
            try:
                time.sleep(30)
                self._cleanup_orphaned_processes()
            except Exception as e:
                logger.error(f"Error in cleanup loop: {e}")
    
    def _cleanup_orphaned_processes(self):
        """Clean up processes not tracked by active terminals"""
        with self.lock:
            active_pids = set()
            for terminal in list(self.terminals):
                try:
                    pid = terminal._get_terminal_pid()
                    if pid:
                        active_pids.add(pid)
                except Exception as e:
                    logger.error(f"Error getting PID from terminal: {e}")
            
            for pid in list(self.processes.keys()):
                if pid not in active_pids:
                    self._terminate_process_by_pid(pid)
    
    def _terminate_process_by_pid(self, pid):
        """Terminate a process by PID"""
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
            time.sleep(1)
            try:
                os.kill(pid, 0)
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        except (ProcessLookupError, OSError) as e:
            logger.debug(f"Process {pid} cleanup: {e}")
        finally:
            with self.lock:
                if pid in self.processes:
                    del self.processes[pid]
    
    def register_terminal(self, terminal):
        """Register a terminal for tracking"""
        self.terminals.add(terminal)
        logger.debug(f"Registered terminal {id(terminal)}")
    
    def cleanup_all(self):
        """Clean up all managed processes"""
        logger.info("Cleaning up all SSH processes...")
        with self.lock:
            # Make a copy of PIDs to avoid modifying the dict during iteration
            pids = list(self.processes.keys())
            for pid in pids:
                self._terminate_process_by_pid(pid)
            
            # Clear all tracked processes
            self.processes.clear()
            
            # Clean up any remaining terminals
            for terminal in list(self.terminals):
                try:
                    if hasattr(terminal, 'disconnect'):
                        terminal.disconnect()
                except Exception as e:
                    logger.error(f"Error cleaning up terminal {id(terminal)}: {e}")
            
            # Clear terminal references
            self.terminals.clear()
            
        logger.info("SSH process cleanup completed")

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
        self._preflight_ok = False
        self._preflight_error = None
        
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

        self.append(self.overlay)
        
        # Set expansion properties
        self.scrolled_window.set_hexpand(True)
        self.scrolled_window.set_vexpand(True)
        self.vte.set_hexpand(True)
        self.vte.set_vexpand(True)
        
        # Connect terminal signals
        self.vte.connect('child-exited', self.on_child_exited)
        self.vte.connect('window-title-changed', self.on_title_changed)
        
        # Apply theme
        self.force_style_refresh()
        
        # Set visibility of child widgets (GTK4 style)
        self.scrolled_window.set_visible(True)
        self.vte.set_visible(True)
        
        # Show overlay initially
        self._set_connecting_overlay_visible(True)
        logger.debug("Terminal widget initialized")

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
        """SSH connection thread with TCP preflight and clear errors"""
        try:
            import socket
            host = getattr(self.connection, 'host', None)
            port = int(getattr(self.connection, 'port', 22) or 22)
            if not host:
                # Even if host is missing, set up terminal to render theme; ssh will error
                GLib.idle_add(self._setup_ssh_terminal)
                return

            # Quick TCP preflight to avoid hanging ssh on blackhole hosts
            try:
                with socket.create_connection((host, port), timeout=5) as sock:
                    # Try to read SSH banner to ensure it's an SSH server
                    sock.settimeout(2)
                    try:
                        banner = sock.recv(255)
                        if banner and banner.startswith(b'SSH-'):
                            self._preflight_ok = True
                        else:
                            self._preflight_ok = False
                            self._preflight_error = "Invalid SSH banner from server"
                    except Exception as read_err:
                        self._preflight_ok = False
                        self._preflight_error = f"Failed to read SSH banner: {read_err}"
            except Exception as sock_err:
                # Do not bail out early; still spawn ssh so terminal paints with theme
                self._preflight_ok = False
                self._preflight_error = f"TCP connection failed: {sock_err}"
                logger.debug(f"TCP preflight failed: {sock_err}; proceeding to spawn ssh with timeout options")

            # Local port-forward preflight: check local bind ports for dynamic/local forwards
            try:
                forwarding_rules = getattr(self.connection, 'forwarding_rules', []) or []
                for rule in forwarding_rules:
                    rtype = rule.get('type')
                    listen_addr = rule.get('listen_addr', '127.0.0.1')
                    listen_port = int(rule.get('listen_port') or 0)
                    if listen_port <= 0:
                        continue
                    if rtype in ('dynamic', 'local'):
                        try:
                            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                                s.bind((listen_addr, listen_port))
                        except OSError as e:
                            if e.errno == errno.EADDRINUSE:
                                msg = f"bind [{listen_addr}]:{listen_port}: Address already in use"
                                logger.debug(f"Local port preflight failed: {msg}")
                                GLib.idle_add(self._show_preflight_error, msg)
                                GLib.idle_add(self._show_forwarding_error_dialog, msg)
                                return
                            else:
                                # Other bind errors
                                msg = f"bind [{listen_addr}]:{listen_port}: {e}"
                                logger.debug(f"Local port preflight failed: {msg}")
                                GLib.idle_add(self._show_preflight_error, msg)
                                GLib.idle_add(self._show_forwarding_error_dialog, msg)
                                return
            except Exception as e:
                logger.debug(f"Port-forward preflight check skipped/failed: {e}")

            # If preflight failed based on TCP or banner, show error without spawning ssh
            if not self._preflight_ok:
                GLib.idle_add(self._show_preflight_error, self._preflight_error or "Connection failed")
                return

            # Proceed to set up the SSH terminal for valid SSH servers
            GLib.idle_add(self._setup_ssh_terminal)

        except Exception as e:
            logger.error(f"SSH connection failed: {e}")
            GLib.idle_add(self._on_connection_failed, str(e))
    
    def _setup_ssh_terminal(self):
        """Set up terminal with direct SSH command (called from main thread)"""
        try:
            # Build SSH command
            ssh_cmd = ['ssh']

            # Read SSH behavior from config with sane defaults
            try:
                ssh_cfg = self.config.get_ssh_config() if hasattr(self.config, 'get_ssh_config') else {}
            except Exception:
                ssh_cfg = {}
            connect_timeout = int(ssh_cfg.get('connection_timeout', 10))
            connection_attempts = int(ssh_cfg.get('connection_attempts', 1))
            keepalive_interval = int(ssh_cfg.get('keepalive_interval', 30))
            keepalive_count = int(ssh_cfg.get('keepalive_count_max', 3))
            strict_host = str(ssh_cfg.get('strict_host_key_checking', 'accept-new'))
            batch_mode = bool(ssh_cfg.get('batch_mode', True))
            compression = bool(ssh_cfg.get('compression', True))

            # Robust non-interactive options to prevent hangs
            if batch_mode:
                ssh_cmd.extend(['-o', 'BatchMode=yes'])
            ssh_cmd.extend(['-o', f'ConnectTimeout={connect_timeout}'])
            ssh_cmd.extend(['-o', f'ConnectionAttempts={connection_attempts}'])
            ssh_cmd.extend(['-o', f'ServerAliveInterval={keepalive_interval}'])
            ssh_cmd.extend(['-o', f'ServerAliveCountMax={keepalive_count}'])
            if strict_host:
                ssh_cmd.extend(['-o', f'StrictHostKeyChecking={strict_host}'])
            if compression:
                ssh_cmd.append('-C')

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
            
            # Add key file if specified and valid
            if hasattr(self.connection, 'keyfile') and self.connection.keyfile and \
               os.path.isfile(self.connection.keyfile) and \
               not self.connection.keyfile.startswith('Select key file'):
                ssh_cmd.extend(['-i', self.connection.keyfile])
                logger.debug(f"Using SSH key: {self.connection.keyfile}")
            else:
                logger.debug("No valid SSH key specified, using default")
            
            # Add X11 forwarding if enabled
            if hasattr(self.connection, 'x11_forwarding') and self.connection.x11_forwarding:
                ssh_cmd.append('-X')
            
            # Add port forwarding rules
            if hasattr(self.connection, 'forwarding_rules'):
                for rule in self.connection.forwarding_rules:
                    if not rule.get('enabled', True):
                        continue
                        
                    rule_type = rule.get('type')
                    listen_addr = rule.get('listen_addr', '127.0.0.1')
                    listen_port = rule.get('listen_port')
                    
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
                            
                    # Handle remote port forwarding
                    elif rule_type == 'remote' and listen_port and 'remote_host' in rule and 'remote_port' in rule:
                        try:
                            remote_host = rule.get('remote_host', 'localhost')
                            remote_port = rule.get('remote_port')
                            ssh_cmd.extend(['-R', f"{listen_addr}:{listen_port}:{remote_host}:{remote_port}"])
                            logger.debug(f"Added remote port forwarding: {listen_addr}:{listen_port} -> {remote_host}:{remote_port}")
                        except Exception as e:
                            logger.error(f"Failed to set up remote forwarding: {e}")
            
            # Add host and user
            ssh_cmd.append(f"{self.connection.username}@{self.connection.host}" if hasattr(self.connection, 'username') and self.connection.username else self.connection.host)
            
            # Add port if not default
            if hasattr(self.connection, 'port') and self.connection.port != 22:
                ssh_cmd.extend(['-p', str(self.connection.port)])
            
            logger.debug(f"SSH command: {' '.join(ssh_cmd)}")
            
            # Create a new PTY for the terminal
            pty = Vte.Pty.new_sync(Vte.PtyFlags.DEFAULT)
            
            # Start the SSH process using VTE's spawn_async with our PTY
            self.vte.spawn_async(
                Vte.PtyFlags.DEFAULT,
                os.environ.get('HOME', '/'),
                ssh_cmd,
                None,  # Environment (use default)
                GLib.SpawnFlags.DEFAULT,
                None,  # Child setup function
                None,  # Child setup data
                -1,    # Timeout (-1 = default)
                None,  # Cancellable
                self._on_spawn_complete,
                None   # User data
            )
            
            # Store the PTY for later cleanup
            self.pty = pty
            try:
                import time
                self._spawn_start_time = time.time()
            except Exception:
                self._spawn_start_time = None
            
            # Only mark as connected if preflight succeeded (host reachable)
            if getattr(self, '_preflight_ok', False):
                # Re-apply theme immediately after spawning to avoid default flash
                try:
                    self.apply_theme()
                except Exception:
                    pass
                self.is_connected = True
                self.emit('connection-established')
            # Hide connecting overlay regardless of outcome at this point
            self._set_connecting_overlay_visible(False)
            
            # Apply theme after connection is established
            self.apply_theme()
            
            # Focus the terminal
            self.vte.grab_focus()
            
            logger.info(f"SSH terminal connected to {self.connection}")
            
        except Exception as e:
            logger.error(f"Failed to setup SSH terminal: {e}")
            self._on_connection_failed(str(e))
    
    def _on_spawn_complete(self, terminal, pid, error, user_data):
        """Called when terminal spawn is complete"""
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
                process_manager.processes[pid] = {
                    'terminal': weakref.ref(self),
                    'start_time': datetime.now(),
                    'command': 'ssh',
                    'pgid': self.process_pgid
                }
            
            # Connection is already marked as connected in _setup_ssh_terminal when preflight OK
            # Just grab focus here
            self.vte.grab_focus()
            
            # Apply theme to ensure it's set correctly after spawn
            self.apply_theme()

            # If preflight indicated failure, show error now (after VTE is ready) and do not mark connected
            if not getattr(self, '_preflight_ok', False):
                self._on_connection_failed(self._preflight_error or "Connection failed")
                return
            
        except Exception as e:
            logger.error(f"Error in spawn complete: {e}")
            self._on_connection_failed(str(e))
    
    def _on_connection_failed(self, error_message):
        """Handle connection failure (called from main thread)"""
        logger.error(f"Connection failed: {error_message}")
        
        # Ensure theme is applied so background remains consistent
        try:
            self.apply_theme()
        except Exception:
            pass
        
        # Show error in terminal
        try:
            self.vte.feed(f"\r\n\x1b[31mConnection failed: {error_message}\x1b[0m\r\n".encode('utf-8'))
        except Exception as e:
            logger.error(f"Error displaying connection error: {e}")
        
        self.is_connected = False
        self.emit('connection-failed', error_message)

    def _show_preflight_error(self, message):
        """Show preflight error in the terminal area without spawning SSH"""
        try:
            # Ensure theme
            self.apply_theme()
            # Render error
            self.vte.feed(("\r\n\x1b[31m" + str(message) + "\x1b[0m\r\n").encode('utf-8'))
        except Exception as e:
            logger.error(f"Error showing preflight error: {e}")
        finally:
            self.is_connected = False
            self.emit('connection-failed', str(message))
            # Hide connecting overlay now that we have an error to show
            self._set_connecting_overlay_visible(False)
        return False

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
        
        # Enable right-click context menu
        self.vte.set_context_menu_model(self.create_context_menu())
        
        # Install standard Linux terminal shortcuts (Ctrl+Shift+C/V/A)
        self._install_shortcuts()

    def create_context_menu(self):
        """Create right-click context menu"""
        menu = Gio.Menu()
        # Add actions
        menu.append("Copy", "app.terminal-copy")
        menu.append("Paste", "app.terminal-paste")
        menu.append("Select All", "app.terminal-select-all")
        
        # Install actions on the widget's application
        app = Gtk.Application.get_default()
        if app:
            # Copy
            if app.lookup_action("terminal-copy") is None:
                copy_action = Gio.SimpleAction.new("terminal-copy", None)
                copy_action.connect("activate", lambda a, p: self.copy_text())
                app.add_action(copy_action)
            # Paste
            if app.lookup_action("terminal-paste") is None:
                paste_action = Gio.SimpleAction.new("terminal-paste", None)
                paste_action.connect("activate", lambda a, p: self.paste_text())
                app.add_action(paste_action)
            # Select All
            if app.lookup_action("terminal-select-all") is None:
                sel_action = Gio.SimpleAction.new("terminal-select-all", None)
                sel_action.connect("activate", lambda a, p: self.select_all())
                app.add_action(sel_action)
            # Standard accelerators
            try:
                app.set_accels_for_action("app.terminal-copy", ["<Primary><Shift>c"]) 
                app.set_accels_for_action("app.terminal-paste", ["<Primary><Shift>v"]) 
                app.set_accels_for_action("app.terminal-select-all", ["<Primary><Shift>a"]) 
            except Exception:
                pass
        
        return menu

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
            
            controller.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string("<Primary><Shift>c"),
                Gtk.CallbackAction.new(_cb_copy)
            ))
            controller.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string("<Primary><Shift>v"),
                Gtk.CallbackAction.new(_cb_paste)
            ))
            controller.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string("<Primary><Shift>a"),
                Gtk.CallbackAction.new(_cb_select_all)
            ))
            
            self.vte.add_controller(controller)
        except Exception as e:
            logger.debug(f"Failed to install shortcuts: {e}")
            
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
    
    def _on_connection_updated_signal(self, _, connection):
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
        
    def _on_connection_lost(self):
        """Handle SSH connection loss"""
        if self.is_connected:
            logger.info(f"SSH connection to {self.connection.host} lost")
            self.is_connected = False
            
            # Update connection status in the connection manager
            if hasattr(self, 'connection') and self.connection:
                self.connection.is_connected = False
                self.connection_manager.emit('connection-status-changed', self.connection, False)
            
            self.emit('connection-lost')
    
    def on_child_exited(self, widget, status):
        """Called when the child process exits"""
        if self.is_connected:
            self.is_connected = False
            logger.info(f"SSH session ended with status {status}")
            self.emit('connection-lost')
        else:
            # Early exit without connection; if forwarding was requested, show dialog
            try:
                if getattr(self.connection, 'forwarding_rules', None):
                    import time
                    fast_fail = (getattr(self, '_spawn_start_time', None) and (time.time() - self._spawn_start_time) < 5)
                    if fast_fail:
                        self._show_forwarding_error_dialog("SSH failed to start with requested port forwarding. Check if ports are available.")
                        # Also print red hint
                        try:
                            self.vte.feed(b"\r\n\x1b[31mPort forwarding failed.\x1b[0m\r\n")
                        except Exception:
                            pass
            except Exception as e:
                logger.debug(f"Error handling early child exit: {e}")
    
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
        
        # Fall back to getting from PTY
        try:
            pty = self.vte.get_pty()
            if pty:
                pid = pty.get_child_pid()
                if pid:
                    self.process_pid = pid
                    return pid
        except Exception as e:
            logger.error(f"Error getting terminal PID: {e}")
        
        return None
        
    def _on_destroy(self, widget):
        """Handle widget destruction"""
        logger.debug(f"Terminal widget {self.session_id} being destroyed")
        
        # Disconnect from connection manager signals
        if hasattr(self, '_connection_updated_handler') and hasattr(self.connection_manager, 'disconnect'):
            try:
                self.connection_manager.disconnect(self._connection_updated_handler)
                logger.debug("Disconnected from connection manager signals")
            except Exception as e:
                logger.error(f"Error disconnecting from connection manager: {e}")
        
        # Disconnect the terminal
        self.disconnect()

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
        
        # Update connection status in the connection manager if we were connected
        if was_connected and hasattr(self, 'connection') and self.connection:
            self.connection.is_connected = False
            if hasattr(self, 'connection_manager') and self.connection_manager:
                GLib.idle_add(self.connection_manager.emit, 'connection-status-changed', self.connection, False)
        
        try:
            # Try to get the terminal's child PID
            pid = self._get_terminal_pid()
            
            # Collect all PIDs that need to be cleaned up
            pids_to_clean = set()
            
            # Add the main process PID if available
            if pid:
                pids_to_clean.add(pid)
            
            # Add the process group ID if available
            if hasattr(self, 'process_pgid') and self.process_pgid:
                pids_to_clean.add(self.process_pgid)
            
            # Add any PIDs from the process manager
            with process_manager.lock:
                for proc_pid, proc_info in list(process_manager.processes.items()):
                    if proc_info.get('terminal')() is self:
                        pids_to_clean.add(proc_pid)
                        if 'pgid' in proc_info:
                            pids_to_clean.add(proc_info['pgid'])
            
            # Clean up all collected PIDs
            for cleanup_pid in pids_to_clean:
                if cleanup_pid:
                    self._cleanup_process(cleanup_pid)
            
            # Clean up PTY if it exists
            if hasattr(self, 'pty') and self.pty:
                try:
                    self.pty.close()
                except Exception as e:
                    logger.error(f"Error closing PTY: {e}")
                finally:
                    self.pty = None
            
            # Clean up from process manager
            with process_manager.lock:
                for proc_pid in list(process_manager.processes.keys()):
                    proc_info = process_manager.processes[proc_pid]
                    if proc_info.get('terminal')() is self:
                        del process_manager.processes[proc_pid]
            
            # Do not hard-reset here; keep current theme/colors
            
            logger.debug(f"Cleaned up {len(pids_to_clean)} processes for session {self.session_id}")
            
        except Exception as e:
            logger.error(f"Error during disconnect: {e}")
        finally:
            # Clean up references
            self.process_pid = None
            self.process_pgid = None
            
            # Ensure we always emit the connection-lost signal
            self.emit('connection-lost')
            logger.debug(f"SSH session {self.session_id} disconnected")
    
    def _on_connection_failed(self, error_message):
        """Handle connection failure (called from main thread)"""
        logger.error(f"Connection failed: {error_message}")
        
        try:
            # Show error in terminal
            error_msg = f"\r\n\x1b[31mConnection failed: {error_message}\x1b[0m\r\n"
            self.vte.feed(error_msg.encode('utf-8'))
            
            self.is_connected = False
            
            # Clean up PTY if it exists
            if hasattr(self, 'pty') and self.pty:
                self.pty.close()
                del self.pty
            
            # Do not reset here to avoid losing theme; leave buffer with error text
            
            # Notify UI
            self.emit('connection-failed', error_message)
            
        except Exception as e:
            logger.error(f"Error in _on_connection_failed: {e}")

    def on_child_exited(self, terminal, status):
        """Handle terminal child process exit"""
        logger.debug(f"Terminal child exited with status: {status}")
        
        if self.connection:
            self.connection.is_connected = False
        
        self.disconnect()
        self.emit('connection-lost')

    def on_title_changed(self, terminal):
        """Handle terminal title change"""
        title = terminal.get_window_title()
        if title:
            self.emit('title-changed', title)

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
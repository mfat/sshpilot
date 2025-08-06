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
import threading
import weakref
import subprocess
from datetime import datetime

gi.require_version('Gtk', '4.0')
gi.require_version('Vte', '3.91')

from gi.repository import Gtk, GObject, GLib, Vte, Pango, Gdk, Gio

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
        
        # Register with process manager
        process_manager.register_terminal(self)
        
        # Connect to destroy signal for cleanup
        self.connect('destroy', self._on_destroy)
        
        # Create scrolled window for terminal
        self.scrolled_window = Gtk.ScrolledWindow()
        self.scrolled_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        
        # Set up the terminal
        self.vte = Vte.Terminal()
        
        # Initialize terminal with basic settings
        self.setup_terminal()
        
        # Set initial colors
        fg_color = Gdk.RGBA()
        fg_color.parse('black')
        bg_color = Gdk.RGBA()
        bg_color.parse('white')
        
        self.vte.set_color_foreground(fg_color)
        self.vte.set_color_background(bg_color)
        
        # Add terminal to scrolled window and to the box
        self.scrolled_window.set_child(self.vte)
        self.append(self.scrolled_window)
        
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
        
        logger.debug("Terminal widget initialized")
    
    def _connect_ssh(self):
        """Connect to SSH host"""
        if not self.connection:
            logger.error("No connection configured")
            return False
        
        try:
            # Connect in a separate thread to avoid blocking UI
            thread = threading.Thread(target=self._connect_ssh_thread)
            thread.daemon = True
            thread.start()
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to start SSH connection: {e}")
            return False
    
    def _connect_ssh_thread(self):
        """SSH connection thread - simplified approach"""
        try:
            # Test SSH connection first
            self.ssh_client = self.connection_manager.connect(self.connection)
            
            if not self.ssh_client:
                GLib.idle_add(self._on_connection_failed, "Failed to establish SSH connection")
                return
            
            # Connection successful - disconnect the test connection
            self.ssh_client.close()
            self.ssh_client = None
            
            # Set up terminal with direct SSH command
            GLib.idle_add(self._setup_ssh_terminal)
            
        except Exception as e:
            logger.error(f"SSH connection failed: {e}")
            GLib.idle_add(self._on_connection_failed, str(e))
    
    def _setup_ssh_terminal(self):
        """Set up terminal with direct SSH command (called from main thread)"""
        try:
            # Build SSH command
            ssh_cmd = ['ssh', '-v']  # Add verbose flag for debugging
            
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
            
            # Mark as connected
            self.is_connected = True
            self.emit('connection-established')
            
            # Focus the terminal
            self.vte.grab_focus()
            
            self.is_connected = True
            self.emit('connection-established')
            
            # Apply theme after connection is established
            self.apply_theme()
            
            # Focus terminal
            self.vte.grab_focus()
            
            logger.info(f"SSH terminal connected to {self.connection}")
            
        except Exception as e:
            logger.error(f"Failed to setup SSH terminal: {e}")
            self._on_connection_failed(str(e))
    
    def _on_spawn_complete(self, terminal, pid, error, user_data):
        """Called when terminal spawn is complete"""
        if error:
            logger.error(f"Terminal spawn failed: {error}")
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
            
            self.is_connected = True
            self.emit('connection-established')
            self.vte.grab_focus()
            
        except Exception as e:
            logger.error(f"Error in spawn complete: {e}")
            self._on_connection_failed(str(e))
    
    def _on_connection_failed(self, error_message):
        """Handle connection failure (called from main thread)"""
        logger.error(f"Connection failed: {error_message}")
        
        # Show error in terminal
        try:
            self.vte.feed(f"Connection failed: {error_message}\r\n".encode())
            self.vte.feed("Press Ctrl+Shift+N to create a new connection.\r\n".encode())
        except Exception as e:
            logger.error(f"Error displaying connection error: {e}")
        
        self.is_connected = False
        self.emit('connection-failed', error_message)
        
    def apply_theme(self, theme_name=None):
        """Apply a color theme to the terminal.
        
        Args:
            theme_name (str, optional): Name of the theme to apply. If None, uses the default theme.
        """
        if theme_name is None:
            theme_name = self.config.get_setting('terminal-color-scheme', 'default')
            
        # Define color schemes
        schemes = {
            'default': {
                'foreground': Gdk.RGBA(0, 0, 0, 1),  # Black text
                'background': Gdk.RGBA(1, 1, 1, 1),  # White background
                'palette': [
                    Gdk.RGBA(0, 0, 0, 1),        # Black
                    Gdk.RGBA(1, 0, 0, 1),        # Red
                    Gdk.RGBA(0, 1, 0, 1),        # Green
                    Gdk.RGBA(1, 1, 0, 1),        # Yellow
                    Gdk.RGBA(0, 0, 1, 1),        # Blue
                    Gdk.RGBA(1, 0, 1, 1),        # Magenta
                    Gdk.RGBA(0, 1, 1, 1),        # Cyan
                    Gdk.RGBA(1, 1, 1, 1),        # White
                    Gdk.RGBA(0.5, 0.5, 0.5, 1),  # Bright Black (Gray)
                    Gdk.RGBA(1, 0.5, 0.5, 1),    # Bright Red
                    Gdk.RGBA(0.5, 1, 0.5, 1),    # Bright Green
                    Gdk.RGBA(1, 1, 0.5, 1),      # Bright Yellow
                    Gdk.RGBA(0.5, 0.5, 1, 1),    # Bright Blue
                    Gdk.RGBA(1, 0.5, 1, 1),      # Bright Magenta
                    Gdk.RGBA(0.5, 1, 1, 1),      # Bright Cyan
                    Gdk.RGBA(1, 1, 1, 1)         # Bright White
                ]
            },
            'solarized-dark': {
                'foreground': Gdk.RGBA(0.8314, 0.9137, 0.9098, 1),  # Base0
                'background': Gdk.RGBA(0.0, 0.1686, 0.2118, 1),     # Base03
                'palette': [
                    Gdk.RGBA(0.0, 0.1686, 0.2118, 1),     # Base03
                    Gdk.RGBA(0.8627, 0.1961, 0.1843, 1),  # Red
                    Gdk.RGBA(0.5216, 0.6, 0.0, 1),        # Green
                    Gdk.RGBA(0.7098, 0.5373, 0.0, 1),     # Yellow
                    Gdk.RGBA(0.1490, 0.5451, 0.8235, 1),  # Blue
                    Gdk.RGBA(0.8275, 0.2118, 0.5098, 1),  # Magenta
                    Gdk.RGBA(0.1647, 0.6314, 0.5961, 1),  # Cyan
                    Gdk.RGBA(0.5137, 0.5804, 0.5882, 1),  # Base01
                    Gdk.RGBA(0.3451, 0.4314, 0.4588, 1),  # Base02
                    Gdk.RGBA(0.7961, 0.2941, 0.0863, 1),  # Orange
                    Gdk.RGBA(0.0, 0.6, 0.5216, 1),        # Base01 Green
                    Gdk.RGBA(0.7098, 0.5373, 0.0, 1),     # Base01 Yellow
                    Gdk.RGBA(0.1490, 0.5451, 0.8235, 1),  # Base01 Blue
                    Gdk.RGBA(0.8275, 0.2118, 0.5098, 1),  # Base01 Magenta
                    Gdk.RGBA(0.1647, 0.6314, 0.5961, 1),  # Base01 Cyan
                    Gdk.RGBA(0.9333, 0.9098, 0.8353, 1)   # Base2
                ]
            },
            'solarized-light': {
                'foreground': Gdk.RGBA(0.0, 0.1686, 0.2118, 1),     # Base03
                'background': Gdk.RGBA(0.9882, 0.9647, 0.8902, 1),  # Base3
                'palette': [
                    Gdk.RGBA(0.0, 0.1686, 0.2118, 1),     # Base03
                    Gdk.RGBA(0.8627, 0.1961, 0.1843, 1),  # Red
                    Gdk.RGBA(0.5216, 0.6, 0.0, 1),        # Green
                    Gdk.RGBA(0.7098, 0.5373, 0.0, 1),     # Yellow
                    Gdk.RGBA(0.1490, 0.5451, 0.8235, 1),  # Blue
                    Gdk.RGBA(0.8275, 0.2118, 0.5098, 1),  # Magenta
                    Gdk.RGBA(0.1647, 0.6314, 0.5961, 1),  # Cyan
                    Gdk.RGBA(0.5137, 0.5804, 0.5882, 1),  # Base01
                    Gdk.RGBA(0.0, 0.1686, 0.2118, 1),     # Base03 (for bright black)
                    Gdk.RGBA(0.7961, 0.2941, 0.0863, 1),  # Orange
                    Gdk.RGBA(0.0, 0.6, 0.5216, 1),        # Base01 Green
                    Gdk.RGBA(0.7098, 0.5373, 0.0, 1),     # Base01 Yellow
                    Gdk.RGBA(0.1490, 0.5451, 0.8235, 1),  # Base01 Blue
                    Gdk.RGBA(0.8275, 0.2118, 0.5098, 1),  # Base01 Magenta
                    Gdk.RGBA(0.1647, 0.6314, 0.5961, 1),  # Base01 Cyan
                    Gdk.RGBA(0.0, 0.0, 0.0, 1)            # Black
                ]
            }
            # Add more themes as needed
        }
        
        # Get the theme or fall back to default
        theme = schemes.get(theme_name.lower().replace(' ', '-'), schemes['default'])
        
        # Apply the theme
        try:
            self.vte.set_color_foreground(theme['foreground'])
            self.vte.set_color_background(theme['background'])
            self.vte.set_colors(
                theme['foreground'],
                theme['background'],
                theme['palette']
            )
            logger.info(f"Applied theme: {theme_name}")
        except Exception as e:
            logger.error(f"Failed to apply theme {theme_name}: {e}")
            # Fall back to default theme
            if theme_name != 'default':
                self.apply_theme('default')
    
    
    def apply_theme(self, theme_name=None):
        """Apply terminal theme and font settings"""
        try:
            if self.config:
                # If no theme specified, get the saved color scheme
                if theme_name is None:
                    theme_name = self.config.get_setting('terminal-color-scheme', 'default')
                profile = self.config.get_terminal_profile(theme_name)
            else:
                # Default theme
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
            
            # Set default colors (light theme by default)
            fg_color = Gdk.RGBA()
            fg_color.parse('black')
            bg_color = Gdk.RGBA()
            bg_color.parse('white')
            
            self.vte.set_color_foreground(fg_color)
            self.vte.set_color_background(bg_color)
            
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

    def create_context_menu(self):
        """Create right-click context menu"""
        menu = Gio.Menu()
        menu.append("Copy", "terminal.copy")
        menu.append("Paste", "terminal.paste")
        menu.append("Select All", "terminal.select-all")
        menu.append("Reset", "terminal.reset")
        menu.append("Reset and Clear", "terminal.reset-clear")
        
        return menu
            
    # PTY forwarding is now handled automatically by VTE
    # No need for manual PTY management in this implementation
    
    def _on_connection_established(self):
        """Called when SSH connection is established"""
        self.is_connected = True
        self.emit('connection-established')
        
    def on_child_exited(self, widget, status):
        """Called when the child process exits"""
        if self.is_connected:
            self.is_connected = False
            logger.info(f"SSH session ended with status {status}")
            self.emit('connection-lost')
    
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
        self.is_connected = False
        
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
            
            # Reset the terminal
            try:
                self.vte.reset(True, True)
            except Exception as e:
                logger.error(f"Error resetting terminal: {e}")
            
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
            
            # Reset terminal
            self.vte.reset(True, True)
            
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
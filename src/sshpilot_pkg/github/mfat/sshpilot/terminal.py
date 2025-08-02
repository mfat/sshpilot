"""
Terminal Widget for sshPilot
Integrated VTE terminal with SSH connection handling
"""

import os
import logging
import threading
from typing import Optional

import gi
gi.require_version('Vte', '3.91')
gi.require_version('Pango', '1.0')

from gi.repository import Gtk, Vte, Pango, GLib, GObject, Gdk, Gio
import paramiko

logger = logging.getLogger(__name__)

class TerminalWidget(Gtk.Box):
    """VTE terminal widget with SSH integration"""
    
    __gsignals__ = {
        'connection-established': (GObject.SignalFlags.RUN_FIRST, None, ()),
        'connection-lost': (GObject.SignalFlags.RUN_FIRST, None, ()),
        'title-changed': (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }
    
    def __init__(self, connection=None, config=None, connection_manager=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        
        self.connection = connection
        self.config = config
        self.connection_manager = connection_manager
        self.ssh_client = None
        self.ssh_channel = None
        self.is_connected = False
        
        # Create VTE terminal
        self.vte = Vte.Terminal()
        self.setup_terminal()
        
        # Create scrolled window for terminal
        self.scrolled_window = Gtk.ScrolledWindow()
        self.scrolled_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.scrolled_window.set_child(self.vte)
        
        # Set expansion properties for proper sizing
        self.scrolled_window.set_hexpand(True)
        self.scrolled_window.set_vexpand(True)
        self.vte.set_hexpand(True)
        self.vte.set_vexpand(True)
        
        # Set expansion for the container widget itself
        self.set_hexpand(True)
        self.set_vexpand(True)
        
        # Add terminal to box
        self.append(self.scrolled_window)
        
        # Connect signals
        self.vte.connect('child-exited', self.on_child_exited)
        self.vte.connect('window-title-changed', self.on_title_changed)
        self.vte.connect('bell', self.on_bell)
        
        # Apply theme and settings
        self.apply_theme()
        
        logger.debug("Terminal widget initialized")

    def setup_terminal(self):
        """Configure terminal settings"""
        # Set terminal properties
        self.vte.set_scrollback_lines(10000)
        self.vte.set_scroll_on_output(True)
        self.vte.set_scroll_on_keystroke(True)
        self.vte.set_mouse_autohide(True)
        self.vte.set_allow_hyperlink(True)
        
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
                    'foreground': '#FFFFFF',
                    'background': '#000000',
                    'font': 'Monospace 12',
                    'cursor_color': '#FFFFFF',
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
            
            logger.debug(f"Applied terminal theme: {theme_name or 'default'}")
            
        except Exception as e:
            logger.error(f"Failed to apply terminal theme: {e}")

    def connect_ssh(self):
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
            self.ssh_client = self.connection_manager.connect_ssh(self.connection)
            
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
            ssh_cmd = ['ssh']
            
            # Add key file if specified
            if hasattr(self.connection, 'keyfile') and self.connection.keyfile:
                ssh_cmd.extend(['-i', self.connection.keyfile])
            
            # Add X11 forwarding if enabled
            if hasattr(self.connection, 'x11_forwarding') and self.connection.x11_forwarding:
                ssh_cmd.append('-X')
            
            # Add host and user
            ssh_cmd.append(f"{self.connection.username}@{self.connection.host}")
            
            # Add port if not default
            if self.connection.port != 22:
                ssh_cmd.extend(['-p', str(self.connection.port)])
            
            logger.debug(f"SSH command: {' '.join(ssh_cmd)}")
            
            # Spawn SSH process in terminal
            self.vte.spawn_async(
                Vte.PtyFlags.DEFAULT,
                os.environ.get('HOME', '/'),
                ssh_cmd,
                None,
                GLib.SpawnFlags.DEFAULT,
                None, None,
                -1,  # Use default PTY
                None,
                self._on_spawn_complete,
                None
            )
            
            self.is_connected = True
            self.emit('connection-established')
            
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
        else:
            logger.debug(f"Terminal spawned with PID: {pid}")

    def _on_connection_failed(self, error_message):
        """Handle connection failure (called from main thread)"""
        logger.error(f"Connection failed: {error_message}")
        
        # Show error in terminal
        self.vte.feed(f"Connection failed: {error_message}\r\n".encode())
        self.vte.feed("Press Ctrl+Shift+N to create a new connection.\r\n".encode())
        
        self.is_connected = False

    def _on_connection_lost(self):
        """Handle connection loss (called from main thread)"""
        logger.info("SSH connection lost")
        
        self.is_connected = False
        self.emit('connection-lost')
        
        # Show message in terminal
        self.vte.feed("\r\n[Connection lost]\r\n".encode())

    def spawn_local_shell(self):
        """Spawn a local shell instead of SSH"""
        try:
            self.vte.spawn_async(
                Vte.PtyFlags.DEFAULT,
                os.environ.get('HOME', '/'),
                [os.environ.get('SHELL', '/bin/bash')],
                None,
                GLib.SpawnFlags.DEFAULT,
                None, None,
                -1,
                None,
                self._on_local_spawn_complete,
                None
            )
            
            # Focus terminal
            self.vte.grab_focus()
            
            logger.debug("Local shell spawned")
            
        except Exception as e:
            logger.error(f"Failed to spawn local shell: {e}")

    def _on_local_spawn_complete(self, terminal, pid, error, user_data):
        """Called when local shell spawn is complete"""
        if error:
            logger.error(f"Local shell spawn failed: {error}")
        else:
            logger.debug(f"Local shell spawned with PID: {pid}")

    def disconnect(self):
        """Disconnect from SSH"""
        try:
            self.is_connected = False
            
            if hasattr(self, 'ssh_client') and self.ssh_client:
                self.ssh_client.close()
                self.ssh_client = None
            
            if self.connection:
                self.connection_manager.disconnect(self.connection)
            
            logger.info("Terminal disconnected")
            
        except Exception as e:
            logger.error(f"Failed to disconnect terminal: {e}")

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
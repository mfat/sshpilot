"""
Terminal Widget for sshPilot
Integrated VTE terminal with SSH connection handling using system SSH client
"""

import os
import sys
import logging
import subprocess
from gi.repository import Gtk, GObject, GLib, Vte, Pango, Gdk, Gio

logger = logging.getLogger(__name__)

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
        
        # Initialize process
        self.process = None
        self.is_connected = False
        self.watch_id = 0
        
        # Create scrolled window for terminal
        self.scrolled_window = Gtk.ScrolledWindow()
        self.scrolled_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        
        # Set up the terminal
        self.vte = Vte.Terminal()
        self.setup_terminal()
        
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
        
        # Connection will be established by the window when added to the UI
        
        logger.debug("Terminal widget initialized")
    
    def _connect_ssh(self):
        """Establish SSH connection using system SSH client"""
        try:
            # Build SSH command
            ssh_cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
            
            # Add connection parameters
            if hasattr(self.connection, 'port') and self.connection.port:
                ssh_cmd.extend(["-p", str(self.connection.port)])
                
            if hasattr(self.connection, 'username') and self.connection.username:
                ssh_cmd.extend(["-l", self.connection.username])
                
            # Add host
            ssh_cmd.append(self.connection.host)
            
            # Set up environment variables - keep as a list of "KEY=VALUE" strings
            env = [
                f"TERM={os.environ.get('TERM', 'xterm-256color')}",
                f"COLORTERM={os.environ.get('COLORTERM', 'truecolor')}"
            ]
            
            # Spawn the SSH process in the VTE terminal
            # Note: env parameter expects a list of "KEY=VALUE" strings, not a dictionary
            # spawn_sync returns (success, pid) tuple
            success, pid = self.vte.spawn_sync(
                Vte.PtyFlags.DEFAULT,  # Use default PTY flags
                None,  # Working directory
                ssh_cmd,
                env,  # Environment variables as list of "KEY=VALUE" strings
                GLib.SpawnFlags.DEFAULT,  # Spawn flags
                None,  # Child setup function
                None,  # User data
                None   # Cancellable
            )
            
            if success and pid > 0:
                self.is_connected = True
                self.emit('connection-established')
                logger.info(f"SSH connection established to {self.connection.host}")
                return True
            else:
                raise Exception("Failed to spawn SSH process")
                
        except Exception as e:
            error_msg = f"Failed to establish SSH connection: {str(e)}"
            logger.error(error_msg, exc_info=True)
            self.emit('connection-failed', error_msg)
            return False
        
    def setup_terminal(self):
        """Initialize the VTE terminal with appropriate settings."""
        logger.info("Setting up terminal...")
        
        try:
            # Set terminal font
            font_desc = Pango.FontDescription()
            font_desc.set_family("Monospace")
            font_desc.set_size(10 * Pango.SCALE)
            self.vte.set_font(font_desc)
            
            # Set terminal colors (solarized dark theme)
            self.vte.set_color_foreground(Gdk.RGBA(0.8314, 0.9569, 0.4078, 1.0))  # Light green text
            self.vte.set_color_background(Gdk.RGBA(0.0275, 0.2118, 0.2588, 1.0))  # Dark blue background
            
            # Set cursor shape and blink mode
            self.vte.set_cursor_blink_mode(Vte.CursorBlinkMode.ON)
            self.vte.set_cursor_shape(Vte.CursorShape.BLOCK)
            
            # Set scrollback lines
            self.vte.set_scrollback_lines(10000)
            
            # Set word char exceptions (for double-click selection)
            try:
                # Try the newer API first (VTE 0.60+)
                self.vte.set_word_char_exceptions("@-./_~")
            except AttributeError:
                try:
                    # Fall back to the older API
                    self.vte.set_word_char_options("@-./_~")
                except Exception as e:
                    logger.debug(f"Could not set word char options: {e}")
            
            # Set cursor and selection colors (using compatible methods)
            try:
                # Try to set cursor color if available
                if hasattr(self.vte, 'set_cursor_foreground_color'):
                    self.vte.set_cursor_foreground_color(Gdk.RGBA(1, 1, 1, 1))  # White cursor
                
                # Try to set selection color if available
                if hasattr(self.vte, 'set_color_highlight'):
                    self.vte.set_color_highlight(Gdk.RGBA(0.3, 0.3, 0.3, 1))  # Dark gray highlight
            except Exception as e:
                logger.debug(f"Could not set terminal colors: {e}")
            
            # Enable mouse reporting if available
            if hasattr(self.vte, 'set_mouse_autohide'):
                self.vte.set_mouse_autohide(True)
            
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
            
    def _cleanup_pty(self):
        """Clean up PTY file descriptors"""
        if self._pty_master is not None:
            os.close(self._pty_master)
            self._pty_master = None
        if self._pty_slave is not None:
            os.close(self._pty_slave)
            self._pty_slave = None
    
    async def _forward_stdin(self):
        """Forward data from the PTY master to the SSH session"""
        while self.is_connected and hasattr(self, '_pty_master') and self._pty_master is not None:
            try:
                data = os.read(self._pty_master, 65536)
                if not data:
                    break
                    
                if self.ssh_session and hasattr(self.ssh_session, 'stdin') and self.ssh_session.stdin:
                    self.ssh_session.stdin.write(data)
                    await self.ssh_session.stdin.drain()
                    
            except (ConnectionError, OSError) as e:
                if self.is_connected:  # Only log if we didn't expect this
                    logger.error(f"Error forwarding stdin: {e}")
                break
            except Exception as e:
                if self.is_connected:  # Only log if we didn't expect this
                    logger.error(f"Unexpected error in stdin forwarding: {e}")
                break
    
    async def _forward_stdout(self):
        """Forward data from the SSH session to the PTY master"""
        while self.is_connected and hasattr(self, '_pty_master') and self._pty_master is not None:
            try:
                if not self.ssh_session or not hasattr(self.ssh_session, 'stdout'):
                    break
                    
                data = await self.ssh_session.stdout.read(65536)
                if not data:
                    break
                    
                os.write(self._pty_master, data)
                
            except (ConnectionError, OSError) as e:
                if self.is_connected:  # Only log if we didn't expect this
                    logger.error(f"Error forwarding stdout: {e}")
                break
            except Exception as e:
                if self.is_connected:  # Only log if we didn't expect this
                    logger.error(f"Unexpected error in stdout forwarding: {e}")
                break
    
    def _on_terminal_resize(self, widget, width, height):
        """Handle terminal resize events"""
        # VTE handles PTY resizing automatically when using spawn_sync
        pass
    
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
    
    def close_connection(self):
        """Close the SSH connection"""
        if self.is_connected:
            self.is_connected = False
            # VTE will handle the process termination when the widget is destroyed
            self.emit('connection-lost')
    
    def _on_connection_failed(self, error_message):
        """Handle connection failure (called from main thread)"""
        logger.error(f"Connection failed: {error_message}")
        
        # Show error in terminal
        error_msg = f"\r\n\x1b[31mConnection failed: {error_message}\x1b[0m\r\n"
        self.vte.feed(error_msg.encode('utf-8'))
        
        self.is_connected = False
        
        # Clean up PTY
        self._cleanup_pty()
        
        # Notify UI
        self.emit('connection-failed', error_message)

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
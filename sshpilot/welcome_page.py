"""Welcome page widget for sshPilot."""

import gi
import shlex
import re

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Gtk, Gdk, Adw
from gettext import gettext as _


from .connection_manager import Connection
from .platform_utils import is_macos


class WelcomePage(Gtk.Overlay):
    """Welcome page shown when no tabs are open."""

    BG_OPTIONS = [
        (_("Default"), None),
        (_("Blue"), "#4A90E2"),
        (_("Green"), "#27AE60"),
        (_("Red"), "#FF4757"),
        (_("Warm"), "linear-gradient(to bottom right, #ff7e5f, #feb47b)"),
        (_("Cool"), "linear-gradient(to bottom right, #6a11cb, #2575fc)"),
        (_("Sunset"), "linear-gradient(to bottom right, #ff6b6b, #feca57)"),
        (_("Ocean"), "linear-gradient(to bottom right, #74b9ff, #0984e3)"),
        (_("Forest"), "linear-gradient(to bottom right, #00b894, #00cec9)"),
        (_("Purple"), "linear-gradient(to bottom right, #a29bfe, #6c5ce7)"),
        (_("Pink"), "linear-gradient(to bottom right, #fd79a8, #e84393)"),
        (_("Orange"), "linear-gradient(to bottom right, #fdcb6e, #e17055)"),
        (_("Mint"), "linear-gradient(to bottom right, #00cec9, #55a3ff)"),
        (_("Lavender"), "linear-gradient(to bottom right, #a29bfe, #fd79a8)"),
        (_("Emerald"), "linear-gradient(to bottom right, #00b894, #00cec9)"),
        (_("Coral"), "linear-gradient(to bottom right, #ff7675, #fd79a8)"),
        (_("Sky"), "linear-gradient(to bottom right, #74b9ff, #a29bfe)"),
        (_("Fire"), "linear-gradient(to bottom right, #ff7675, #fdcb6e)"),
        (_("Ice"), "linear-gradient(to bottom right, #74b9ff, #00cec9)"),
        (_("Rainbow"), "linear-gradient(to bottom right, #ff6b6b, #4ecdc4, #45b7d1, #96ceb4, #feca57)"),
        # Dark gradients
        (_("Dark Blue"), "linear-gradient(to bottom right, #1a237e, #0d47a1)"),
        (_("Dark Green"), "linear-gradient(to bottom right, #1b5e20, #2e7d32)"),
        (_("Dark Red"), "linear-gradient(to bottom right, #b71c1c, #d32f2f)"),
        (_("Dark Purple"), "linear-gradient(to bottom right, #4a148c, #6a1b9a)"),
        (_("Dark Orange"), "linear-gradient(to bottom right, #e65100, #f57c00)"),
        (_("Dark Teal"), "linear-gradient(to bottom right, #004d40, #00695c)"),
        (_("Dark Indigo"), "linear-gradient(to bottom right, #1a237e, #283593)"),
        (_("Dark Brown"), "linear-gradient(to bottom right, #3e2723, #5d4037)"),
        (_("Dark Gray"), "linear-gradient(to bottom right, #212121, #424242)"),
        (_("Dark Navy"), "linear-gradient(to bottom right, #0d47a1, #1565c0)"),
        (_("Dark Forest"), "linear-gradient(to bottom right, #1b5e20, #388e3c)"),
        (_("Dark Crimson"), "linear-gradient(to bottom right, #b71c1c, #c62828)"),
        (_("Dark Violet"), "linear-gradient(to bottom right, #4a148c, #7b1fa2)"),
        (_("Dark Amber"), "linear-gradient(to bottom right, #e65100, #ff8f00)"),
        (_("Dark Cyan"), "linear-gradient(to bottom right, #004d40, #00796b)"),
    ]

    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        self.connection_manager = window.connection_manager
        self.set_hexpand(True)
        self.set_vexpand(True)

        # Main container replicating previous layout
        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        container.set_valign(Gtk.Align.CENTER)
        container.set_halign(Gtk.Align.FILL)
        container.set_hexpand(True)
        container.set_margin_start(24)
        container.set_margin_end(24)
        container.set_margin_top(24)
        container.set_margin_bottom(24)
        container.set_can_focus(False)
        container.set_focus_on_click(False)
        self.set_child(container)

        # CSS provider for dynamic background
        self._bg_provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            self._bg_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        # Create grid layout for large tiles
        grid = Gtk.Grid()
        grid.set_column_spacing(12)
        grid.set_row_spacing(12)
        grid.set_halign(Gtk.Align.CENTER)
        grid.set_hexpand(False)
        grid.set_vexpand(False)
        container.append(grid)

        # Background selection button (top-right overlay)
        self.bg_button = Gtk.Button.new_from_icon_name("preferences-color-symbolic")
        self.bg_button.add_css_class("flat")
        self.bg_button.set_tooltip_text(_("Change background"))
        self.bg_button.set_halign(Gtk.Align.END)
        self.bg_button.set_valign(Gtk.Align.START)
        self.bg_button.set_margin_top(6)
        self.bg_button.set_margin_end(6)
        self.bg_button.set_can_focus(False)
        self.bg_button.connect("clicked", self._show_bg_menu)
        self.add_overlay(self.bg_button)

        # Popover with color tiles
        self.bg_popover = Gtk.Popover.new()
        self.bg_popover.set_has_arrow(True)
        self.bg_popover.set_parent(self.bg_button)
        menu_grid = Gtk.Grid(margin_top=6, margin_bottom=6,
                              margin_start=6, margin_end=6,
                              column_spacing=6, row_spacing=6)
        self.bg_popover.set_child(menu_grid)
        for idx, (name, css) in enumerate(self.BG_OPTIONS):
            btn = Gtk.Button()
            btn.set_size_request(32, 32)
            btn.add_css_class("flat")
            btn.set_tooltip_text(name)
            btn.set_can_focus(False)
            if css:
                provider = Gtk.CssProvider()
                provider.load_from_data(
                    f"* {{ background: {css}; border-radius:4px; }}".encode()
                )
                btn.get_style_context().add_provider(
                    provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                )
            else:
                img = Gtk.Image.new_from_icon_name("window-close-symbolic")
                btn.set_child(img)
            btn.connect("clicked", self._on_bg_selected, css)
            menu_grid.attach(btn, idx % 4, idx // 4, 1, 1)
        
        # Create large tile buttons
        def create_tile(title, tooltip_text, icon_name, callback):
            """Create a large tile button with icon and title"""
            tile = Gtk.Button()
            tile.set_css_classes(['card', 'flat'])
            tile.set_size_request(180, 140)
            tile.set_hexpand(False)
            tile.set_vexpand(False)
            tile.set_tooltip_text(tooltip_text)
            
            # Main container
            container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            container.set_margin_top(24)
            container.set_margin_bottom(24)
            container.set_margin_start(16)
            container.set_margin_end(16)
            container.set_halign(Gtk.Align.CENTER)
            container.set_valign(Gtk.Align.CENTER)
            
            # Icon
            icon = Gtk.Image()
            icon.set_from_icon_name(icon_name)
            icon.set_icon_size(Gtk.IconSize.LARGE)
            icon.set_pixel_size(48)
            container.append(icon)
            
            # Title
            title_label = Gtk.Label()
            title_label.set_text(title)
            title_label.set_halign(Gtk.Align.CENTER)
            title_label.set_wrap(True)
            title_label.set_max_width_chars(15)
            container.append(title_label)
            
            tile.set_child(container)
            tile.connect('clicked', callback)
            tile.set_can_focus(False)
            
            return tile
        
        # Create tiles
        quick_connect_tile = create_tile(
            _('Quick Connect'),
            _('Connect to a server using SSH command'),
            'network-server-symbolic',
            self.on_quick_connect_clicked
        )
        
        local_terminal_tile = create_tile(
            _('Local Terminal'),
            _('Open a local terminal session'),
            'utilities-terminal-symbolic',
            lambda *_: window.terminal_manager.show_local_terminal()
        )
        
        
        shortcuts_tile = create_tile(
            _('Shortcuts'),
            _('View and learn keyboard shortcuts'),
            'preferences-desktop-keyboard-symbolic',
            lambda *_: window.show_shortcuts_window()
        )
        
        help_tile = create_tile(
            _('Online Help'),
            _('View online documentation and help'),
            'help-contents-symbolic',
            lambda *_: self.open_online_help()
        )
        
        # Add tiles to grid (2 columns, 2 rows)
        grid.attach(quick_connect_tile, 0, 0, 1, 1)
        grid.attach(local_terminal_tile, 1, 0, 1, 1)
        grid.attach(shortcuts_tile, 0, 1, 1, 1)
        grid.attach(help_tile, 1, 1, 1, 1)
        
        # Load saved background setting
        self._load_saved_background()

    def _show_bg_menu(self, button):
        """Display background selection popover."""
        self.bg_popover.popup()

    def _on_bg_selected(self, button, css):
        """Handle selection of background option."""
        self.bg_popover.popdown()
        self._apply_background(css)

    def _apply_background(self, css):
        """Apply CSS background or reset to default."""
        # Save the background setting to config
        self.window.config.set_setting('welcome.background', css)
        self._set_background_css(css)

    def _set_background_css(self, css):
        """Apply CSS background without saving to config."""
        if not css:
            if self.has_css_class("welcome-bg"):
                self.remove_css_class("welcome-bg")
            self._bg_provider.load_from_data(b"")
            return
        if "gradient" in css:
            style = f"background-image: {css};"
        else:
            style = f"background: {css};"
        self._bg_provider.load_from_data(
            f".welcome-bg {{ {style} }}".encode()
        )
        if not self.has_css_class("welcome-bg"):
            self.add_css_class("welcome-bg")

    def _load_saved_background(self):
        """Load and apply saved background setting from config."""
        saved_bg = self.window.config.get_setting('welcome.background', None)
        if saved_bg:
            self._set_background_css(saved_bg)

    # Quick connect handlers
    def on_quick_connect_clicked(self, button):
        """Open quick connect dialog"""
        dialog = QuickConnectDialog(self.window)
        dialog.present()
    
    def open_online_help(self):
        """Open online help documentation"""
        import webbrowser
        try:
            webbrowser.open('https://github.com/mfat/sshpilot/wiki')
        except Exception as e:
            # Fallback: show a dialog with the URL
            dialog = Adw.MessageDialog.new(
                self.window,
                "Online Help",
                "Visit the sshPilot documentation at:\nhttps://github.com/mfat/sshpilot/wiki"
            )
            dialog.add_response("ok", "OK")
            dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_modal(True)
            dialog.set_transient_for(self.window)
            dialog.present()
    
    def _parse_ssh_command(self, command_text):
        """Parse SSH command text and extract connection parameters"""
        try:
            # Handle simple user@host format (backward compatibility)
            if not command_text.startswith('ssh') and '@' in command_text and ' ' not in command_text:
                username, host = command_text.split('@', 1)
                return {
                    "nickname": host,
                    "host": host,
                    "username": username,
                    "port": 22,
                    "auth_method": 0,  # Default to key-based auth
                    "key_select_mode": 0  # Try all keys
                }
            
            # Parse full SSH command
            # Remove 'ssh' prefix if present
            if command_text.startswith('ssh '):
                command_text = command_text[4:]
            elif command_text.startswith('ssh'):
                command_text = command_text[3:]
            
            # Use shlex to properly parse the command with quoted arguments
            try:
                args = shlex.split(command_text)
            except ValueError:
                # If shlex fails, fall back to simple split
                args = command_text.split()
            
            # Initialize connection data with defaults
            connection_data = {
                "nickname": "",
                "host": "",
                "username": "",
                "port": 22,
                "auth_method": 0,  # Key-based auth
                "key_select_mode": 0,  # Try all keys
                "keyfile": "",
                "certificate": "",
                "x11_forwarding": False,
                "local_port_forwards": [],
                "remote_port_forwards": [],
                "dynamic_forwards": []
            }
            
            i = 0
            while i < len(args):
                arg = args[i]
                
                # Handle options with values
                if arg == '-p' and i + 1 < len(args):
                    try:
                        connection_data["port"] = int(args[i + 1])
                        i += 2
                        continue
                    except ValueError:
                        pass
                elif arg == '-i' and i + 1 < len(args):
                    connection_data["keyfile"] = args[i + 1]
                    connection_data["key_select_mode"] = 1  # Use specific key
                    i += 2
                    continue
                elif arg == '-o' and i + 1 < len(args):
                    # Handle SSH options like -o "UserKnownHostsFile=/dev/null"
                    option = args[i + 1]
                    if '=' in option:
                        key, value = option.split('=', 1)
                        if key == 'User':
                            connection_data["username"] = value
                        elif key == 'Port':
                            try:
                                connection_data["port"] = int(value)
                            except ValueError:
                                pass
                        elif key == 'IdentityFile':
                            connection_data["keyfile"] = value
                            connection_data["key_select_mode"] = 1
                    i += 2
                    continue
                elif arg == '-X':
                    connection_data["x11_forwarding"] = True
                    i += 1
                    continue
                elif arg == '-L' and i + 1 < len(args):
                    # Local port forwarding: -L [bind_address:]port:host:hostport
                    forward_spec = args[i + 1]
                    connection_data["local_port_forwards"].append(forward_spec)
                    i += 2
                    continue
                elif arg == '-R' and i + 1 < len(args):
                    # Remote port forwarding: -R [bind_address:]port:host:hostport
                    forward_spec = args[i + 1]
                    connection_data["remote_port_forwards"].append(forward_spec)
                    i += 2
                    continue
                elif arg == '-D' and i + 1 < len(args):
                    # Dynamic port forwarding: -D [bind_address:]port
                    forward_spec = args[i + 1]
                    connection_data["dynamic_forwards"].append(forward_spec)
                    i += 2
                    continue
                elif arg.startswith('-p'):
                    # Handle -p2222 format (no space)
                    try:
                        connection_data["port"] = int(arg[2:])
                        i += 1
                        continue
                    except ValueError:
                        pass
                elif arg.startswith('-i'):
                    # Handle -i/path/to/key format (no space)
                    connection_data["keyfile"] = arg[2:]
                    connection_data["key_select_mode"] = 1
                    i += 1
                    continue
                elif not arg.startswith('-'):
                    # This should be the host specification (user@host)
                    if '@' in arg:
                        username, host = arg.split('@', 1)
                        connection_data["username"] = username
                        connection_data["host"] = host
                        connection_data["nickname"] = host
                    else:
                        # Just hostname, no username
                        connection_data["host"] = arg
                        connection_data["nickname"] = arg
                    i += 1
                else:
                    # Unknown option, skip it
                    i += 1
            
            # Validate that we have at least a host
            if not connection_data["host"]:
                return None
            
            return connection_data
            
        except Exception as e:
            # If parsing fails, try simple fallback
            if '@' in command_text:
                try:
                    username, host = command_text.split('@', 1)
                    return {
                        "nickname": host,
                        "host": host,
                        "username": username,
                        "port": 22,
                        "auth_method": 0,
                        "key_select_mode": 0
                    }
                except:
                    pass
            return None


class QuickConnectDialog(Adw.MessageDialog):
    """Modal dialog for quick SSH connection"""
    
    def __init__(self, parent_window):
        super().__init__()
        
        self.parent_window = parent_window
        
        # Set dialog properties
        self.set_modal(True)
        self.set_transient_for(parent_window)
        self.set_title("Quick Connect")
        
        # Create content area
        content_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content_area.set_margin_top(12)
        content_area.set_margin_bottom(12)
        content_area.set_margin_start(12)
        content_area.set_margin_end(12)
        
        # Add description
        description = Gtk.Label()
        description.set_text("Enter SSH command or connection details:")
        description.set_halign(Gtk.Align.START)
        content_area.append(description)
        
        # Create entry field
        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("ssh -p 2222 user@host")
        self.entry.set_hexpand(True)
        self.entry.connect('activate', self.on_connect)
        content_area.append(self.entry)
        
        # Add content to dialog
        self.set_extra_child(content_area)
        
        # Add response buttons
        self.add_response("cancel", "Cancel")
        self.add_response("connect", "Connect")
        self.set_response_appearance("connect", Adw.ResponseAppearance.SUGGESTED)
        
        # Connect response signal
        self.connect('response', self.on_response)
        
        # Focus the entry when dialog is shown
        self.entry.grab_focus()
    
    def on_response(self, dialog, response):
        """Handle dialog response"""
        if response == "connect":
            self.on_connect()
        self.destroy()
    
    def on_connect(self, *args):
        """Handle connect button or Enter key"""
        text = self.entry.get_text().strip()
        if not text:
            return
        
        # Parse the SSH command
        connection_data = self._parse_ssh_command(text)
        if connection_data:
            connection = Connection(connection_data)
            self.parent_window.terminal_manager.connect_to_host(connection, force_new=False)
            self.destroy()
    
    def _parse_ssh_command(self, command_text):
        """Parse SSH command text and extract connection parameters"""
        try:
            # Handle simple user@host format (backward compatibility)
            if not command_text.startswith('ssh') and '@' in command_text and ' ' not in command_text:
                username, host = command_text.split('@', 1)
                return {
                    "nickname": host,
                    "host": host,
                    "username": username,
                    "port": 22,
                    "auth_method": 0,  # Default to key-based auth
                    "key_select_mode": 0  # Try all keys
                }
            
            # Parse full SSH command
            # Remove 'ssh' prefix if present
            if command_text.startswith('ssh '):
                command_text = command_text[4:]
            elif command_text.startswith('ssh'):
                command_text = command_text[3:]
            
            # Use shlex to properly parse the command with quoted arguments
            try:
                args = shlex.split(command_text)
            except ValueError:
                # If shlex fails, fall back to simple split
                args = command_text.split()
            
            # Initialize connection data with defaults
            connection_data = {
                "nickname": "",
                "host": "",
                "username": "",
                "port": 22,
                "auth_method": 0,  # Key-based auth
                "key_select_mode": 0,  # Try all keys
                "keyfile": "",
                "certificate": "",
                "x11_forwarding": False,
                "local_port_forwards": [],
                "remote_port_forwards": [],
                "dynamic_forwards": []
            }
            
            i = 0
            while i < len(args):
                arg = args[i]
                
                # Handle options with values
                if arg == '-p' and i + 1 < len(args):
                    try:
                        connection_data["port"] = int(args[i + 1])
                        i += 2
                        continue
                    except ValueError:
                        pass
                elif arg == '-i' and i + 1 < len(args):
                    connection_data["keyfile"] = args[i + 1]
                    connection_data["key_select_mode"] = 1  # Use specific key
                    i += 2
                    continue
                elif arg == '-o' and i + 1 < len(args):
                    # Handle SSH options like -o "UserKnownHostsFile=/dev/null"
                    option = args[i + 1]
                    if '=' in option:
                        key, value = option.split('=', 1)
                        if key == 'User':
                            connection_data["username"] = value
                        elif key == 'Port':
                            try:
                                connection_data["port"] = int(value)
                            except ValueError:
                                pass
                        elif key == 'IdentityFile':
                            connection_data["keyfile"] = value
                            connection_data["key_select_mode"] = 1
                    i += 2
                    continue
                elif arg == '-X':
                    connection_data["x11_forwarding"] = True
                    i += 1
                    continue
                elif arg == '-L' and i + 1 < len(args):
                    # Local port forwarding: -L [bind_address:]port:host:hostport
                    forward_spec = args[i + 1]
                    connection_data["local_port_forwards"].append(forward_spec)
                    i += 2
                    continue
                elif arg == '-R' and i + 1 < len(args):
                    # Remote port forwarding: -R [bind_address:]port:host:hostport
                    forward_spec = args[i + 1]
                    connection_data["remote_port_forwards"].append(forward_spec)
                    i += 2
                    continue
                elif arg == '-D' and i + 1 < len(args):
                    # Dynamic port forwarding: -D [bind_address:]port
                    forward_spec = args[i + 1]
                    connection_data["dynamic_forwards"].append(forward_spec)
                    i += 2
                    continue
                elif arg.startswith('-p'):
                    # Handle -p2222 format (no space)
                    try:
                        connection_data["port"] = int(arg[2:])
                        i += 1
                        continue
                    except ValueError:
                        pass
                elif arg.startswith('-i'):
                    # Handle -i/path/to/key format (no space)
                    connection_data["keyfile"] = arg[2:]
                    connection_data["key_select_mode"] = 1
                    i += 1
                    continue
                elif not arg.startswith('-'):
                    # This should be the host specification (user@host)
                    if '@' in arg:
                        username, host = arg.split('@', 1)
                        connection_data["username"] = username
                        connection_data["host"] = host
                        connection_data["nickname"] = host
                    else:
                        # Just hostname, no username
                        connection_data["host"] = arg
                        connection_data["nickname"] = arg
                    i += 1
                else:
                    # Unknown option, skip it
                    i += 1
            
            # Validate that we have at least a host
            if not connection_data["host"]:
                return None
            
            return connection_data
            
        except Exception as e:
            # If parsing fails, try simple fallback
            if '@' in command_text:
                try:
                    username, host = command_text.split('@', 1)
                    return {
                        "nickname": host,
                        "host": host,
                        "username": username,
                        "port": 22,
                        "auth_method": 0,
                        "key_select_mode": 0
                    }
                except:
                    pass
            return None

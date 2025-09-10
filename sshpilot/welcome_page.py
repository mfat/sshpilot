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


class WelcomePage(Gtk.Box):
    """Welcome page shown when no tabs are open."""

    def __init__(self, window) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        self.window = window
        self.connection_manager = window.connection_manager
        self.set_valign(Gtk.Align.CENTER)
        self.set_halign(Gtk.Align.FILL)
        self.set_hexpand(True)
        self.set_margin_start(24)
        self.set_margin_end(24)
        self.set_margin_top(24)
        self.set_margin_bottom(24)
        # Prevent the welcome page container from grabbing keyboard focus
        self.set_can_focus(False)
        self.set_focus_on_click(False)


        # Create grid layout for large tiles
        grid = Gtk.Grid()
        grid.set_column_spacing(12)
        grid.set_row_spacing(12)
        grid.set_halign(Gtk.Align.CENTER)
        grid.set_hexpand(False)
        grid.set_vexpand(False)
        
        # Create large tile buttons
        def create_tile(title, tooltip_text, icon_name, callback, css_class=None):
            """Create a large tile button with icon and title"""
            tile = Gtk.Button()
            tile.set_css_classes(['card', 'flat'])
            if css_class:
                tile.add_css_class(css_class)
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
        
        # Create tiles with different colors
        quick_connect_tile = create_tile(
            _('Quick Connect'),
            _('Connect to a server using SSH command'),
            'network-server-symbolic',
            self.on_quick_connect_clicked,
            'tile-blue'
        )
        
        local_terminal_tile = create_tile(
            _('Local Terminal'),
            _('Open a local terminal session'),
            'utilities-terminal-symbolic',
            lambda *_: window.terminal_manager.show_local_terminal(),
            'tile-green'
        )
        
        known_hosts_tile = create_tile(
            _('Known Hosts'),
            _('Manage trusted SSH host keys'),
            'view-list-symbolic',
            lambda *_: window.show_known_hosts_editor(),
            'tile-orange'
        )
        
        preferences_tile = create_tile(
            _('Preferences'),
            _('Configure application settings'),
            'preferences-system-symbolic',
            lambda *_: window.show_preferences(),
            'tile-purple'
        )
        
        shortcuts_tile = create_tile(
            _('Shortcuts'),
            _('View and learn keyboard shortcuts'),
            'preferences-desktop-keyboard-symbolic',
            lambda *_: window.show_shortcuts_window(),
            'tile-red'
        )
        
        help_tile = create_tile(
            _('Online Help'),
            _('View online documentation and help'),
            'help-contents-symbolic',
            lambda *_: self.open_online_help(),
            'tile-teal'
        )
        
        # Add tiles to grid (3 columns, 2 rows)
        grid.attach(quick_connect_tile, 0, 0, 1, 1)
        grid.attach(local_terminal_tile, 1, 0, 1, 1)
        grid.attach(known_hosts_tile, 2, 0, 1, 1)
        grid.attach(preferences_tile, 0, 1, 1, 1)
        grid.attach(shortcuts_tile, 1, 1, 1, 1)
        grid.attach(help_tile, 2, 1, 1, 1)
        
        self.append(grid)
        
        # Add CSS styling for colored tiles
        self._add_tile_colors()

    def _add_tile_colors(self):
        """Add CSS styling for different colored tiles"""
        css = b"""
        /* Blue tile */
        .tile-blue {
            background: linear-gradient(135deg, #3584e4 0%, #1c71d8 100%);
            border: 1px solid #1c71d8;
        }
        .tile-blue:hover {
            background: linear-gradient(135deg, #1c71d8 0%, #1a5fb4 100%);
            transform: translateY(-2px);
            box-shadow: 0 8px 25px rgba(28, 113, 216, 0.3);
        }
        
        /* Green tile */
        .tile-green {
            background: linear-gradient(135deg, #2ec27e 0%, #26a269 100%);
            border: 1px solid #26a269;
        }
        .tile-green:hover {
            background: linear-gradient(135deg, #26a269 0%, #2d8659 100%);
            transform: translateY(-2px);
            box-shadow: 0 8px 25px rgba(38, 162, 105, 0.3);
        }
        
        /* Orange tile */
        .tile-orange {
            background: linear-gradient(135deg, #ff7800 0%, #e66100 100%);
            border: 1px solid #e66100;
        }
        .tile-orange:hover {
            background: linear-gradient(135deg, #e66100 0%, #c64600 100%);
            transform: translateY(-2px);
            box-shadow: 0 8px 25px rgba(230, 97, 0, 0.3);
        }
        
        /* Purple tile */
        .tile-purple {
            background: linear-gradient(135deg, #9141ac 0%, #813d9c 100%);
            border: 1px solid #813d9c;
        }
        .tile-purple:hover {
            background: linear-gradient(135deg, #813d9c 0%, #6d2c85 100%);
            transform: translateY(-2px);
            box-shadow: 0 8px 25px rgba(129, 61, 156, 0.3);
        }
        
        /* Red tile */
        .tile-red {
            background: linear-gradient(135deg, #e01b24 0%, #c01c28 100%);
            border: 1px solid #c01c28;
        }
        .tile-red:hover {
            background: linear-gradient(135deg, #c01c28 0%, #a51d2d 100%);
            transform: translateY(-2px);
            box-shadow: 0 8px 25px rgba(192, 28, 40, 0.3);
        }
        
        /* Teal tile */
        .tile-teal {
            background: linear-gradient(135deg, #1e9a96 0%, #1a7f7b 100%);
            border: 1px solid #1a7f7b;
        }
        .tile-teal:hover {
            background: linear-gradient(135deg, #1a7f7b 0%, #166461 100%);
            transform: translateY(-2px);
            box-shadow: 0 8px 25px rgba(26, 127, 123, 0.3);
        }
        
        /* Make text white on colored tiles */
        .tile-blue label,
        .tile-green label,
        .tile-orange label,
        .tile-purple label,
        .tile-red label,
        .tile-teal label {
            color: white;
            font-weight: 600;
        }
        
        /* Make icons white on colored tiles */
        .tile-blue image,
        .tile-green image,
        .tile-orange image,
        .tile-purple image,
        .tile-red image,
        .tile-teal image {
            color: white;
            -gtk-icon-shadow: 0 1px 2px rgba(0, 0, 0, 0.3);
        }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

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

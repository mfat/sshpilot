"""Welcome page widget for sshPilot."""

import gi
import shlex
import logging

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Gtk, Adw, Gdk

from gettext import gettext as _

from .connection_manager import Connection
from .platform_utils import is_macos


class WelcomePage(Gtk.Overlay):
    """Welcome page shown when no tabs are open."""



    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        self.connection_manager = window.connection_manager
        self.config = window.config
        self.set_hexpand(True)
        self.set_vexpand(True)
        clamp = Adw.Clamp()
        clamp.set_halign(Gtk.Align.CENTER)
        clamp.set_valign(Gtk.Align.CENTER)
        grid = Gtk.Grid(column_spacing=24, row_spacing=24)
        grid.set_column_homogeneous(True)
        grid.set_row_homogeneous(True)
        grid.set_halign(Gtk.Align.CENTER)
        grid.set_valign(Gtk.Align.CENTER)
        clamp.set_child(grid)
        self.set_child(clamp)

        
        # Create welcome page cards with keyboard shortcuts in tooltips
        mac = is_macos()
        primary = '⌘' if mac else 'Ctrl'
        alt = '⌥' if mac else 'Alt'
        shift = '⇧' if mac else 'Shift'
        
        quick_connect_card = self.create_card(
            _('Quick Connect'),
            _('Connect to a server using SSH command\n({})').format(f'{primary}+{alt}+C'),
            'network-server-symbolic',
            self.on_quick_connect_clicked
        )

        local_terminal_card = self.create_card(
            _('Local Terminal'),
            _('Open a local terminal session\n({})').format(f'{primary}+{shift}+T'),
            'utilities-terminal-symbolic',
            lambda *_: window.terminal_manager.show_local_terminal()
        )

        shortcuts_card = self.create_card(
            _('Shortcuts'),
            _('View and learn keyboard shortcuts\n({})').format(f'{primary}+{shift}+/'),
            'preferences-desktop-keyboard-symbolic',
            lambda *_: window.show_shortcuts_window()
        )

        help_card = self.create_card(
            _('Help'),
            _('View online documentation\n(F1)'),
            'help-browser-symbolic',
            lambda *_: self.open_online_help()
        )

        # Add cards to grid (2 columns, 2 rows)
        grid.attach(quick_connect_card, 0, 0, 1, 1)
        grid.attach(local_terminal_card, 1, 0, 1, 1)
        grid.attach(shortcuts_card, 0, 1, 1, 1)
        grid.attach(help_card, 1, 1, 1, 1)


    def create_card(self, title, tooltip_text, icon_name, callback):
        """Create an activatable card with icon and title"""
        # Create a vertical box for icon and text
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content.set_halign(Gtk.Align.CENTER)
        content.set_valign(Gtk.Align.CENTER)
        content.set_margin_top(24)
        content.set_margin_bottom(24)
        content.set_margin_start(24)
        content.set_margin_end(24)
        
        # Create larger icon
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_icon_size(Gtk.IconSize.LARGE)  # Use large icon size
        icon.set_pixel_size(64)  # Set specific pixel size for even larger icons
        content.append(icon)
        
        # Create title label
        title_label = Gtk.Label(label=title)
        title_label.set_halign(Gtk.Align.CENTER)
        title_label.set_css_classes(['title-5'])  # Use larger text style
        content.append(title_label)

        card = Adw.Bin()
        card.add_css_class("card")
        card.add_css_class("activatable")
        card.add_css_class("welcome-card")
        card.set_child(content)
        card.set_tooltip_text(tooltip_text)
        card.set_focusable(True)
        card.set_accessible_role(Gtk.AccessibleRole.BUTTON)
        card.set_hexpand(True)
        card.set_vexpand(True)
        card.set_valign(Gtk.Align.FILL)

        click = Gtk.GestureClick()
        click.connect("released", lambda *_: callback(card))
        card.add_controller(click)

        key = Gtk.EventControllerKey()
        key.connect(
            "key-released",
            lambda _c, keyval, *_: (
                callback(card)
                if keyval in (Gdk.KEY_Return, Gdk.KEY_space)
                else False
            ),
        )
        card.add_controller(key)

        return card


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
                    connection_data["key_select_mode"] = 2  # Use specific key without forcing IdentitiesOnly by default
                    i += 2
                    continue
                elif arg == '-o' and i + 1 < len(args):
                    # Handle SSH options like -o "UserKnownHostsFile=/dev/null"
                    option = args[i + 1]
                    parsed = option.split('=', 1)
                    if len(parsed) == 2:
                        key, value = parsed
                        key_lower = key.lower()
                        value = value.strip()
                        if key_lower == 'user':
                            connection_data["username"] = value
                        elif key_lower == 'port':
                            try:
                                connection_data["port"] = int(value)
                            except ValueError:
                                pass
                        elif key_lower == 'identityfile':
                            connection_data["keyfile"] = value
                            connection_data["key_select_mode"] = 2
                        elif key_lower == 'identitiesonly':
                            if value.lower() in ('yes', 'true', '1', 'on'):
                                connection_data["key_select_mode"] = 1
                            elif value.lower() in ('no', 'false', '0', 'off') and connection_data.get("keyfile"):
                                connection_data["key_select_mode"] = 2
                    i += 2
                    continue
                elif arg.startswith('-o') and '=' in arg[2:]:
                    key, value = arg[2:].split('=', 1)
                    key_lower = key.lower()
                    value = value.strip()
                    if key_lower == 'identityfile':
                        connection_data["keyfile"] = value
                        connection_data["key_select_mode"] = 2
                    elif key_lower == 'identitiesonly':
                        if value.lower() in ('yes', 'true', '1', 'on'):
                            connection_data["key_select_mode"] = 1
                        elif value.lower() in ('no', 'false', '0', 'off') and connection_data.get("keyfile"):
                            connection_data["key_select_mode"] = 2
                    elif key_lower == 'user':
                        connection_data["username"] = value
                    elif key_lower == 'port':
                        try:
                            connection_data["port"] = int(value)
                        except ValueError:
                            pass
                    i += 1
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
                    connection_data["key_select_mode"] = 2
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

            if connection_data.get("keyfile") and connection_data.get("key_select_mode", 0) == 0:
                connection_data["key_select_mode"] = 2

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
                    connection_data["key_select_mode"] = 2  # Use specific key without forcing IdentitiesOnly by default
                    i += 2
                    continue
                elif arg == '-o' and i + 1 < len(args):
                    # Handle SSH options like -o "UserKnownHostsFile=/dev/null"
                    option = args[i + 1]
                    parsed = option.split('=', 1)
                    if len(parsed) == 2:
                        key, value = parsed
                        key_lower = key.lower()
                        value = value.strip()
                        if key_lower == 'user':
                            connection_data["username"] = value
                        elif key_lower == 'port':
                            try:
                                connection_data["port"] = int(value)
                            except ValueError:
                                pass
                        elif key_lower == 'identityfile':
                            connection_data["keyfile"] = value
                            connection_data["key_select_mode"] = 2
                        elif key_lower == 'identitiesonly':
                            if value.lower() in ('yes', 'true', '1', 'on'):
                                connection_data["key_select_mode"] = 1
                            elif value.lower() in ('no', 'false', '0', 'off') and connection_data.get("keyfile"):
                                connection_data["key_select_mode"] = 2
                    i += 2
                    continue
                elif arg.startswith('-o') and '=' in arg[2:]:
                    key, value = arg[2:].split('=', 1)
                    key_lower = key.lower()
                    value = value.strip()
                    if key_lower == 'identityfile':
                        connection_data["keyfile"] = value
                        connection_data["key_select_mode"] = 2
                    elif key_lower == 'identitiesonly':
                        if value.lower() in ('yes', 'true', '1', 'on'):
                            connection_data["key_select_mode"] = 1
                        elif value.lower() in ('no', 'false', '0', 'off') and connection_data.get("keyfile"):
                            connection_data["key_select_mode"] = 2
                    elif key_lower == 'user':
                        connection_data["username"] = value
                    elif key_lower == 'port':
                        try:
                            connection_data["port"] = int(value)
                        except ValueError:
                            pass
                    i += 1
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
                    connection_data["key_select_mode"] = 2
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

            if connection_data.get("keyfile") and connection_data.get("key_select_mode", 0) == 0:
                connection_data["key_select_mode"] = 2

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

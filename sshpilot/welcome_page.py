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
from .search_utils import connection_matches


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



       

        # Quick connect box
        self.quick_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.quick_box.set_halign(Gtk.Align.CENTER)
        self.quick_box.set_hexpand(False)
        self.quick_entry = Gtk.Entry()
        self.quick_entry.set_hexpand(False)
        self.quick_entry.set_size_request(200, -1)

        self.quick_entry.set_placeholder_text(_('ssh -p 2222 user@host'))
        self.quick_entry.connect('activate', self.on_quick_connect)
        self.quick_entry.connect('changed', self.on_quick_entry_changed)

        # Key controller for navigation to search results
        entry_key = Gtk.EventControllerKey()
        entry_key.connect('key-pressed', self.on_quick_entry_key_pressed)
        self.quick_entry.add_controller(entry_key)
        
        # Add focus controller for auto-expansion
        focus_controller = Gtk.EventControllerFocus()
        focus_controller.connect('enter', self.on_quick_entry_focus_in)
        focus_controller.connect('leave', self.on_quick_entry_focus_out)
        self.quick_entry.add_controller(focus_controller)
        connect_button = Gtk.Button(label=_('Connect'))
        connect_button.connect('clicked', self.on_quick_connect)
        self.quick_box.append(self.quick_entry)
        self.quick_box.append(connect_button)
        self.append(self.quick_box)

        # Search results list (omni box)
        self.search_list = Gtk.ListBox()
        self.search_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.search_list.set_visible(False)
        self.search_list.connect('row-activated', self.on_search_row_activated)
        list_key = Gtk.EventControllerKey()
        list_key.connect('key-pressed', self.on_search_results_key_pressed)
        self.search_list.add_controller(list_key)
        self.append(self.search_list)


        # Action buttons
        buttons_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        buttons_box.set_halign(Gtk.Align.CENTER)
        buttons_box.set_hexpand(False)

        # Search button
        search_button = Gtk.Button()
        search_button.set_icon_name('system-search-symbolic')
        # Platform-aware shortcut in tooltip
        shortcut = 'Cmd+F' if is_macos() else 'Ctrl+F'
        search_button.set_tooltip_text(_('Search Connections') + f' ({shortcut})')
        search_button.connect('clicked', self.on_search_clicked)
        
        # Local terminal button
        local_button = Gtk.Button()
        local_button.set_icon_name('utilities-terminal-symbolic')
        # Platform-aware shortcut in tooltip
        shortcut = 'Cmd+Shift+T' if is_macos() else 'Ctrl+Shift+T'
        local_button.set_tooltip_text(_('Local Terminal') + f' ({shortcut})')
        local_button.connect('clicked', lambda *_: window.terminal_manager.show_local_terminal())

        # Known hosts editor button
        known_hosts_button = Gtk.Button()
        known_hosts_button.set_icon_name('view-list-symbolic')
        known_hosts_button.set_tooltip_text(_('Known Hosts Editor'))
        known_hosts_button.connect('clicked', lambda *_: window.show_known_hosts_editor())

        # Preferences button
        prefs_button = Gtk.Button()
        prefs_button.set_icon_name('preferences-system-symbolic')
        # Platform-aware shortcut in tooltip
        shortcut = 'Cmd+,' if is_macos() else 'Ctrl+,'
        prefs_button.set_tooltip_text(_('Preferences') + f' ({shortcut})')
        prefs_button.connect('clicked', lambda *_: window.show_preferences())

        # Keyboard shortcuts button
        shortcuts_button = Gtk.Button()
        shortcuts_button.set_icon_name('preferences-desktop-keyboard-symbolic')
        # Platform-aware shortcut in tooltip
        shortcut = 'Cmd+Shift+/' if is_macos() else 'Ctrl+Shift+/'
        shortcuts_button.set_tooltip_text(_('Keyboard Shortcuts') + f' ({shortcut})')
        shortcuts_button.connect('clicked', lambda *_: window.show_shortcuts_window())

        buttons_box.append(search_button)
        buttons_box.append(local_button)
        buttons_box.append(known_hosts_button)
        buttons_box.append(prefs_button)
        buttons_box.append(shortcuts_button)
        self.append(buttons_box)



    # Quick connect handlers
    def on_quick_connect(self, *_args):
        text = self.quick_entry.get_text().strip()
        if not text:
            return
        
        # Parse the SSH command
        connection_data = self._parse_ssh_command(text)
        if connection_data:
            connection = Connection(connection_data)
            self.window.terminal_manager.connect_to_host(connection, force_new=False)

    def on_quick_entry_changed(self, entry):
        """Update search results as text changes."""
        text = entry.get_text().strip().lower()
        # Clear previous results
        for child in list(self.search_list.get_children()):
            self.search_list.remove(child)
        if not text:
            self.search_list.set_visible(False)
            return
        matches = [c for c in self.connection_manager.connections if connection_matches(c, text)]
        if not matches:
            self.search_list.set_visible(False)
            return
        for conn in matches:
            row = Adw.ActionRow()
            row.set_title(conn.nickname)
            subtitle = f"{conn.username+'@' if conn.username else ''}{conn.host}"
            row.set_subtitle(subtitle)
            row.connection = conn
            self.search_list.append(row)
        self.search_list.set_visible(True)

    def on_search_row_activated(self, listbox, row):
        """Connect to the selected connection from search results."""
        connection = getattr(row, 'connection', None)
        if connection:
            self.window.terminal_manager.connect_to_host(connection, force_new=False)

    def on_quick_entry_key_pressed(self, controller, keyval, keycode, state):
        """Handle navigation from entry to results with arrow keys."""
        if keyval == Gdk.KEY_Down and self.search_list.get_visible():
            first = self.search_list.get_row_at_index(0)
            if first:
                self.search_list.select_row(first)
                self.search_list.grab_focus()
                return True
        return False

    def on_search_results_key_pressed(self, controller, keyval, keycode, state):
        """Allow returning to entry when navigating results."""
        if keyval == Gdk.KEY_Up:
            selected = self.search_list.get_selected_row()
            if selected and self.search_list.get_row_at_index(0) == selected:
                self.quick_entry.grab_focus()
                return True
        return False
    
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

    # Search button handler
    def on_search_clicked(self, button):
        """Handle search button click - activate sidebar search"""
        self.window.focus_search_entry()
    
    # Quick connect box focus handlers
    def on_quick_entry_focus_in(self, controller):
        """Handle focus in event - expand the quick connect box"""
        # Expand the entry field width when focused
        self.quick_entry.set_size_request(300, -1)  # Expand from 200 to 300 pixels
    
    def on_quick_entry_focus_out(self, controller):
        """Handle focus out event - contract the quick connect box"""
        # Contract the entry field width when focus is lost
        self.quick_entry.set_size_request(200, -1)  # Contract back to 200 pixels
    

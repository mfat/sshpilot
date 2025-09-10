"""Welcome page widget for sshPilot."""

import gi
import shlex
import re

gi.require_version('Gtk', '4.0')

from gi.repository import Gtk, Gdk
from gettext import gettext as _


from .connection_manager import Connection
from .search_utils import connection_matches
from .sidebar import ConnectionRow


class WelcomePage(Gtk.Box):
    """Welcome page shown when no tabs are open."""

    def __init__(self, window) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        self.window = window
        self.connection_manager = window.connection_manager
        self.set_valign(Gtk.Align.START)
        self.set_halign(Gtk.Align.FILL)
        self.set_hexpand(True)
        self.set_margin_start(24)
        self.set_margin_end(24)
        self.set_margin_top(24)
        self.set_margin_bottom(24)


        # Welcome icon
        try:
            texture = Gdk.Texture.new_from_resource('/io/github/mfat/sshpilot/sshpilot.svg')
            icon = Gtk.Image.new_from_paintable(texture)
            icon.set_pixel_size(64)
        except Exception:
            icon = Gtk.Image.new_from_icon_name('network-workgroup-symbolic')
            icon.set_icon_size(Gtk.IconSize.LARGE)
            icon.set_pixel_size(128)
        icon.set_halign(Gtk.Align.CENTER)
        self.append(icon)

        # Add some vertical spacing after the icon
        spacer = Gtk.Box()
        spacer.set_size_request(-1, 16)  # 16px vertical spacing
        self.append(spacer)

        # Quick connect box
        quick_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        quick_box.set_halign(Gtk.Align.CENTER)
        quick_box.set_hexpand(False)
        self.quick_entry = Gtk.Entry()
        self.quick_entry.set_hexpand(False)
        self.quick_entry.set_size_request(200, -1)

        self.quick_entry.set_placeholder_text(_('ssh user@host or ssh -p 2222 user@host'))
        self.quick_entry.connect('activate', self.on_quick_connect)
        connect_button = Gtk.Button(label=_('Connect'))
        connect_button.connect('clicked', self.on_quick_connect)
        quick_box.append(self.quick_entry)
        quick_box.append(connect_button)
        self.append(quick_box)

        # Search box and results
        search_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        search_container.set_halign(Gtk.Align.CENTER)
        search_container.set_hexpand(False)
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_hexpand(False)
        self.search_entry.set_size_request(300, -1)

        self.search_entry.set_placeholder_text(_('Search connections'))
        self.search_entry.connect('activate', self.on_search_activate)
        self.search_entry.connect('search-changed', self.on_search_changed)
        
        # Add keyboard navigation support
        key_controller = Gtk.EventControllerKey()
        key_controller.connect('key-pressed', self.on_search_key_pressed)
        self.search_entry.add_controller(key_controller)
        
        search_container.append(self.search_entry)

        self.results_list = Gtk.ListBox()
        self.results_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.results_list.set_hexpand(True)
        self.results_list.add_css_class('boxed-list')

        self.results_list.connect('row-activated', self.on_result_activated)
        
        # Add keyboard navigation to results list
        results_key_controller = Gtk.EventControllerKey()
        results_key_controller.connect('key-pressed', self.on_results_key_pressed)
        self.results_list.add_controller(results_key_controller)
        
        # Wrap the list in a scrolled window for better UX
        self.results_scrolled = Gtk.ScrolledWindow()
        self.results_scrolled.set_child(self.results_list)
        self.results_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.results_scrolled.set_min_content_height(100)
        self.results_scrolled.set_max_content_height(300)
        self.results_scrolled.set_hexpand(True)
        self.results_scrolled.set_vexpand(False)
        self.results_scrolled.set_visible(False)  # Hidden by default
        
        search_container.append(self.results_scrolled)
        self.append(search_container)

        # Action buttons
        buttons_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        buttons_box.set_halign(Gtk.Align.CENTER)
        buttons_box.set_hexpand(False)

        local_button = Gtk.Button()
        local_button.set_icon_name('utilities-terminal-symbolic')
        local_button.set_tooltip_text(_('Local Terminal'))
        local_button.connect('clicked', lambda *_: window.terminal_manager.show_local_terminal())
        
        prefs_button = Gtk.Button()
        prefs_button.set_icon_name('preferences-system-symbolic')
        prefs_button.set_tooltip_text(_('Preferences'))
        prefs_button.connect('clicked', lambda *_: window.show_preferences())
        buttons_box.append(local_button)
        buttons_box.append(prefs_button)
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

    # Search handlers
    def _search_results(self, query: str):
        connections = self.connection_manager.get_connections()
        matches = [c for c in connections if connection_matches(c, query)]
        return matches

    def on_search_changed(self, entry):
        query = entry.get_text().strip().lower()
        for child in list(self.results_list):
            self.results_list.remove(child)
        if not query:
            self.results_scrolled.set_visible(False)
            return
        for conn in self._search_results(query):
            row = ConnectionRow(conn)
            self.results_list.append(row)
        self.results_scrolled.set_visible(True)

    def on_search_activate(self, entry):
        query = entry.get_text().strip().lower()
        matches = self._search_results(query)
        if matches:
            self.window.terminal_manager.connect_to_host(matches[0], force_new=False)

    def on_result_activated(self, listbox, row):
        if hasattr(row, 'connection'):
            self.window.terminal_manager.connect_to_host(row.connection, force_new=False)
    
    def on_search_key_pressed(self, controller, keyval, keycode, state):
        """Handle key presses in search entry for navigation"""
        # Arrow down - move to first result or next result
        if keyval == Gdk.KEY_Down:
            if self.results_scrolled.get_visible():
                # Check if there are any results by trying to get the first row
                first_row = self.results_list.get_row_at_index(0)
                if first_row:
                    # Focus the results list
                    self.results_list.grab_focus()
                    # Select first row if none selected
                    selected_row = self.results_list.get_selected_row()
                    if not selected_row:
                        self.results_list.select_row(first_row)
                    return True
        
        # Arrow up - move to search entry (when in results)
        elif keyval == Gdk.KEY_Up:
            # This will be handled by the results list when it has focus
            return False
        
        # Enter - activate selected result or first result
        elif keyval == Gdk.KEY_Return or keyval == Gdk.KEY_KP_Enter:
            if self.results_scrolled.get_visible():
                # Check if there are any results by trying to get the first row
                first_row = self.results_list.get_row_at_index(0)
                if first_row:
                    selected_row = self.results_list.get_selected_row()
                    if not selected_row:
                        # No selection, activate first row
                        self.on_result_activated(self.results_list, first_row)
                    else:
                        # Activate selected row
                        self.on_result_activated(self.results_list, selected_row)
                    return True
        
        return False
    
    def on_results_key_pressed(self, controller, keyval, keycode, state):
        """Handle key presses in results list for navigation"""
        # Arrow up on first row - move focus back to search entry
        if keyval == Gdk.KEY_Up:
            selected_row = self.results_list.get_selected_row()
            if selected_row:
                row_index = selected_row.get_index()
                if row_index == 0:
                    # On first row, move focus back to search entry
                    self.search_entry.grab_focus()
                    return True
        
        # Enter - activate selected result
        elif keyval == Gdk.KEY_Return or keyval == Gdk.KEY_KP_Enter:
            selected_row = self.results_list.get_selected_row()
            if selected_row:
                self.on_result_activated(self.results_list, selected_row)
                return True
        
        return False
    

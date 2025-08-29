"""
Main Window for sshPilot
Primary UI with connection list, tabs, and terminal management
"""

import os
import logging
import math
from typing import Optional, Dict, Any, List, Tuple, Callable

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
try:
    gi.require_version('Vte', '3.91')
    from gi.repository import Vte
    _HAS_VTE = True
except Exception:
    _HAS_VTE = False

from gi.repository import Gtk, Adw, Gio, GLib, GObject, Gdk, Pango, PangoFT2
import subprocess
import threading

# Feature detection for libadwaita versions across distros
HAS_OVERLAY_SPLIT = hasattr(Adw, 'OverlaySplitView')
HAS_TIMED_ANIMATION = hasattr(Adw, 'TimedAnimation')

from gettext import gettext as _

from .connection_manager import ConnectionManager, Connection
from .terminal import TerminalWidget
from .config import Config
from .key_manager import KeyManager, SSHKey
# Port forwarding UI is now integrated into connection_dialog.py
from .connection_dialog import ConnectionDialog
from .askpass_utils import ensure_askpass_script
from .preferences import PreferencesWindow, is_running_in_flatpak
from .sshcopyid_window import SshCopyIdWindow
from .sftp_utils import open_remote_in_file_manager

logger = logging.getLogger(__name__)


# ============================================================================
# Advanced Grouping System
# ============================================================================

class GroupManager:
    """Manages hierarchical groups for connections"""
    
    def __init__(self, config: Config):
        self.config = config
        self.groups = {}  # group_id -> GroupInfo
        self.connections = {}  # connection_nickname -> group_id
        self._load_groups()
    
    def _load_groups(self):
        """Load groups from configuration"""
        try:
            groups_data = self.config.get_setting('connection_groups', {})
            self.groups = groups_data.get('groups', {})
            self.connections = groups_data.get('connections', {})
        except Exception as e:
            logger.error(f"Failed to load groups: {e}")
            self.groups = {}
            self.connections = {}
    
    def _save_groups(self):
        """Save groups to configuration"""
        try:
            groups_data = {
                'groups': self.groups,
                'connections': self.connections
            }
            self.config.set_setting('connection_groups', groups_data)
        except Exception as e:
            logger.error(f"Failed to save groups: {e}")
    
    def create_group(self, name: str, parent_id: str = None) -> str:
        """Create a new group and return its ID"""
        import uuid
        group_id = str(uuid.uuid4())
        
        self.groups[group_id] = {
            'id': group_id,
            'name': name,
            'parent_id': parent_id,
            'children': [],
            'connections': [],
            'expanded': True,
            'order': len(self.groups)
        }
        
        if parent_id and parent_id in self.groups:
            self.groups[parent_id]['children'].append(group_id)
        
        self._save_groups()
        return group_id
    
    def delete_group(self, group_id: str):
        """Delete a group and move its contents to parent or root"""
        if group_id not in self.groups:
            return
        
        group = self.groups[group_id]
        parent_id = group.get('parent_id')
        
        # Move connections to parent or root
        for conn_nickname in group['connections']:
            self.connections[conn_nickname] = parent_id
        
        # Move child groups to parent
        for child_id in group['children']:
            if child_id in self.groups:
                self.groups[child_id]['parent_id'] = parent_id
                if parent_id and parent_id in self.groups:
                    self.groups[parent_id]['children'].append(child_id)
        
        # Remove from parent's children
        if parent_id and parent_id in self.groups:
            if group_id in self.groups[parent_id]['children']:
                self.groups[parent_id]['children'].remove(group_id)
        
        # Delete the group
        del self.groups[group_id]
        self._save_groups()
    
    def move_connection(self, connection_nickname: str, target_group_id: str = None):
        """Move a connection to a different group"""
        self.connections[connection_nickname] = target_group_id
        
        # Remove from old group
        for group in self.groups.values():
            if connection_nickname in group['connections']:
                group['connections'].remove(connection_nickname)
        
        # Add to new group
        if target_group_id and target_group_id in self.groups:
            if connection_nickname not in self.groups[target_group_id]['connections']:
                self.groups[target_group_id]['connections'].append(connection_nickname)
        
        self._save_groups()
    
    def get_group_hierarchy(self) -> List[Dict]:
        """Get the complete group hierarchy"""
        def build_tree(parent_id=None):
            result = []
            for group_id, group in self.groups.items():
                if group.get('parent_id') == parent_id:
                    group_copy = group.copy()
                    group_copy['children'] = build_tree(group_id)
                    result.append(group_copy)
            return sorted(result, key=lambda x: x.get('order', 0))
        
        return build_tree()
    
    def get_connection_group(self, connection_nickname: str) -> str:
        """Get the group ID for a connection"""
        return self.connections.get(connection_nickname)
    
    def set_group_expanded(self, group_id: str, expanded: bool):
        """Set whether a group is expanded"""
        if group_id in self.groups:
            self.groups[group_id]['expanded'] = expanded
            self._save_groups()
    
    def reorder_connection_in_group(self, connection_nickname: str, target_connection_nickname: str, position: str):
        """Reorder a connection within the same group relative to another connection"""
        # Get the group for both connections
        source_group_id = self.connections.get(connection_nickname)
        target_group_id = self.connections.get(target_connection_nickname)
        
        # Both connections must be in the same group
        if source_group_id != target_group_id or not source_group_id:
            return
        
        group = self.groups.get(source_group_id)
        if not group:
            return
        
        connections = group['connections']
        
        # Remove the source connection from its current position
        if connection_nickname in connections:
            connections.remove(connection_nickname)
        
        # Find the target connection's position
        try:
            target_index = connections.index(target_connection_nickname)
        except ValueError:
            # Target not found, append to end
            connections.append(connection_nickname)
            self._save_groups()
            return
        
        # Insert at the appropriate position
        if position == 'above':
            connections.insert(target_index, connection_nickname)
        else:  # 'below'
            connections.insert(target_index + 1, connection_nickname)
        
        self._save_groups()


class GroupRow(Gtk.ListBoxRow):
    """Row widget for group headers"""
    
    __gsignals__ = {
        'group-toggled': (GObject.SignalFlags.RUN_FIRST, None, (str, bool))
    }
    
    def __init__(self, group_info: Dict, group_manager: GroupManager, connections_dict: Dict = None):
        super().__init__()
        self.group_info = group_info
        self.group_manager = group_manager
        self.group_id = group_info['id']
        self.connections_dict = connections_dict or {}
        
        # Create main content box
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.set_margin_top(8)
        content.set_margin_bottom(8)
        
        # Group icon
        icon = Gtk.Image.new_from_icon_name('folder-symbolic')
        icon.set_icon_size(Gtk.IconSize.NORMAL)
        content.append(icon)
        
        # Group info
        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info_box.set_hexpand(True)
        
        # Group name label
        self.name_label = Gtk.Label()
        self.name_label.set_markup(f"<b>{group_info['name']}</b>")
        self.name_label.set_halign(Gtk.Align.START)
        info_box.append(self.name_label)
        
        # Connection count label
        self.count_label = Gtk.Label()
        self.count_label.add_css_class('dim-label')
        self.count_label.set_halign(Gtk.Align.START)
        info_box.append(self.count_label)
        
        content.append(info_box)
        
        # Expand/collapse button
        self.expand_button = Gtk.Button()
        self.expand_button.set_icon_name('pan-end-symbolic')
        self.expand_button.add_css_class('flat')
        self.expand_button.add_css_class('group-expand-button')
        self.expand_button.set_can_focus(False)
        self.expand_button.connect('clicked', self._on_expand_clicked)
        content.append(self.expand_button)
        
        self.set_child(content)
        self.set_selectable(True)
        self.set_can_focus(True)
        
        # Update display
        self._update_display()
        
        # Setup drag source
        self._setup_drag_source()
        
        # Setup double-click gesture for expand/collapse
        self._setup_double_click_gesture()
    
    def _update_display(self):
        """Update the display based on current group state"""
        # Update expand/collapse icon
        if self.group_info.get('expanded', True):
            self.expand_button.set_icon_name('pan-down-symbolic')
        else:
            self.expand_button.set_icon_name('pan-end-symbolic')
        
        # Update connection count - only count connections that actually exist
        actual_connections = []
        for conn_nickname in self.group_info.get('connections', []):
            if conn_nickname in self.connections_dict:
                actual_connections.append(conn_nickname)
        
        count = len(actual_connections)
        if count == 1:
            self.count_label.set_text("1")
        else:
            self.count_label.set_text(f"{count}")
    
    def _on_expand_clicked(self, button):
        """Handle expand/collapse button click"""
        self._toggle_expand()
    
    def _setup_drag_source(self):
        """Setup drag source for group reordering"""
        drag_source = Gtk.DragSource()
        drag_source.set_actions(Gdk.DragAction.MOVE)
        drag_source.connect('prepare', self._on_drag_prepare)
        self.add_controller(drag_source)
    
    def _on_drag_prepare(self, source, x, y):
        """Prepare drag data"""
        data = {
            'type': 'group',
            'group_id': self.group_id
        }
        return Gdk.ContentProvider.new_for_value(GObject.Value(GObject.TYPE_PYOBJECT, data))
    
    def _setup_double_click_gesture(self):
        """Setup double-click gesture for expand/collapse"""
        gesture = Gtk.GestureClick()
        gesture.set_button(1)  # Left mouse button
        gesture.connect('pressed', self._on_double_click)
        self.add_controller(gesture)
    
    def _on_double_click(self, gesture, n_press, x, y):
        """Handle double-click to expand/collapse group"""
        if n_press == 2:  # Double-click
            self._toggle_expand()
    
    def _toggle_expand(self):
        """Toggle the expanded state of the group"""
        expanded = not self.group_info.get('expanded', True)
        self.group_info['expanded'] = expanded
        self.group_manager.set_group_expanded(self.group_id, expanded)
        self._update_display()
        
        # Emit signal to parent to rebuild the list
        self.emit('group-toggled', self.group_id, expanded)


class ConnectionRow(Gtk.ListBoxRow):
    """Row widget for connection list"""
    
    def __init__(self, connection: Connection):
        super().__init__()
        self.connection = connection
        
        # Create overlay for pulse effect
        overlay = Gtk.Overlay()
        self.set_child(overlay)
        
        # Create main content box
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.set_margin_top(6)
        content.set_margin_bottom(6)
        
        # Connection icon
        icon = Gtk.Image.new_from_icon_name('computer-symbolic')
        icon.set_icon_size(Gtk.IconSize.NORMAL)
        content.append(icon)
        
        # Connection info
        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info_box.set_hexpand(True)
        
        # Nickname label
        self.nickname_label = Gtk.Label()
        self.nickname_label.set_markup(f"<b>{connection.nickname}</b>")
        self.nickname_label.set_halign(Gtk.Align.START)
        info_box.append(self.nickname_label)
        
        # Host info label (may be hidden based on user setting)
        self.host_label = Gtk.Label()
        self.host_label.set_halign(Gtk.Align.START)
        self.host_label.add_css_class('dim-label')
        self._apply_host_label_text()
        info_box.append(self.host_label)
        
        content.append(info_box)
        
        # Port forwarding indicators (L/R/D)
        self.indicator_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.indicator_box.set_halign(Gtk.Align.CENTER)
        self.indicator_box.set_valign(Gtk.Align.CENTER)
        content.append(self.indicator_box)

        # Connection status indicator
        self.status_icon = Gtk.Image.new_from_icon_name('network-offline-symbolic')
        self.status_icon.set_pixel_size(16)  # GTK4 uses pixel size instead of IconSize
        content.append(self.status_icon)
        
        # Set content as the main child of overlay
        overlay.set_child(content)
        
        # Create pulse layer
        self._pulse = Gtk.Box()
        self._pulse.add_css_class("pulse-highlight")
        self._pulse.set_can_target(False)  # Don't intercept mouse events
        self._pulse.set_hexpand(True)
        self._pulse.set_vexpand(True)
        overlay.add_overlay(self._pulse)
        
        self.set_selectable(True)  # Make the row selectable for keyboard navigation
        
        # Update status
        self.update_status()
        # Update forwarding indicators
        self._update_forwarding_indicators()
        
        # Setup drag source for connection reordering
        self._setup_drag_source()
    
    def _setup_drag_source(self):
        """Setup drag source for connection reordering"""
        drag_source = Gtk.DragSource()
        drag_source.set_actions(Gdk.DragAction.MOVE)
        drag_source.connect('prepare', self._on_drag_prepare)
        drag_source.connect('drag-begin', self._on_drag_begin)
        drag_source.connect('drag-end', self._on_drag_end)
        self.add_controller(drag_source)
    
    def _on_drag_prepare(self, source, x, y):
        """Prepare drag data"""
        data = {
            'type': 'connection',
            'connection_nickname': self.connection.nickname
        }
        return Gdk.ContentProvider.new_for_value(GObject.Value(GObject.TYPE_PYOBJECT, data))
    
    def _on_drag_begin(self, source, drag):
        """Handle drag begin - show ungrouped area"""
        try:
            # Get the main window to show ungrouped area
            window = self.get_root()
            if hasattr(window, '_show_ungrouped_area'):
                window._show_ungrouped_area()
        except Exception as e:
            logger.error(f"Error in drag begin: {e}")
    
    def _on_drag_end(self, source, drag, delete_data):
        """Handle drag end - hide ungrouped area"""
        try:
            # Get the main window to hide ungrouped area
            window = self.get_root()
            if hasattr(window, '_hide_ungrouped_area'):
                window._hide_ungrouped_area()
        except Exception as e:
            logger.error(f"Error in drag end: {e}")

    @staticmethod
    def _install_pf_css():
        try:
            # Install CSS for port forwarding indicator badges once per display
            display = Gdk.Display.get_default()
            if not display:
                return
            # Use an attribute on the display to avoid re-adding provider
            if getattr(display, '_pf_css_installed', False):
                return
            provider = Gtk.CssProvider()
            css = """
            .pf-indicator { /* kept for legacy, not used by circled glyphs */ }
            .pf-local { color: #E01B24; }
            .pf-remote { color: #2EC27E; }
            .pf-dynamic { color: #3584E4; }
            """
            provider.load_from_data(css.encode('utf-8'))
            Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            setattr(display, '_pf_css_installed', True)
        except Exception:
            pass



    def _update_forwarding_indicators(self):
        # Ensure CSS exists
        self._install_pf_css()
        # Clear previous indicators
        try:
            while self.indicator_box.get_first_child():
                self.indicator_box.remove(self.indicator_box.get_first_child())
        except Exception:
            return

        rules = getattr(self.connection, 'forwarding_rules', []) or []
        has_local = any(r.get('enabled', True) and r.get('type') == 'local' for r in rules)
        has_remote = any(r.get('enabled', True) and r.get('type') == 'remote' for r in rules)
        has_dynamic = any(r.get('enabled', True) and r.get('type') == 'dynamic' for r in rules)

        def make_badge(letter: str, cls: str):
            # Use Unicode precomposed circled letters for perfect centering
            circled_map = {
                'L': '\u24C1',  # Ⓛ
                'R': '\u24C7',  # Ⓡ
                'D': '\u24B9',  # Ⓓ
            }
            glyph = circled_map.get(letter, letter)
            lbl = Gtk.Label(label=glyph)
            lbl.add_css_class(cls)
            lbl.set_halign(Gtk.Align.CENTER)
            lbl.set_valign(Gtk.Align.CENTER)
            try:
                lbl.set_xalign(0.5)
                lbl.set_yalign(0.5)
            except Exception:
                pass
            return lbl

        if has_local:
            self.indicator_box.append(make_badge('L', 'pf-local'))
        if has_remote:
            self.indicator_box.append(make_badge('R', 'pf-remote'))
        if has_dynamic:
            self.indicator_box.append(make_badge('D', 'pf-dynamic'))

    def _apply_host_label_text(self):
        try:
            window = self.get_root()
            hide = bool(getattr(window, '_hide_hosts', False)) if window else False
        except Exception:
            hide = False
        if hide:
            self.host_label.set_text('••••••••••')
        else:
            self.host_label.set_text(f"{self.connection.username}@{self.connection.host}")

    def apply_hide_hosts(self, hide: bool):
        """Called by window when hide/show toggles."""
        self._apply_host_label_text()

    def update_status(self):
        """Update connection status display"""
        try:
            # Check if there's any active terminal for this connection
            window = self.get_root()
            has_active_terminal = False

            # Prefer multi-tab map if present; fallback to most-recent mapping
            if hasattr(window, 'connection_to_terminals') and self.connection in getattr(window, 'connection_to_terminals', {}):
                for t in window.connection_to_terminals.get(self.connection, []) or []:
                    if getattr(t, 'is_connected', False):
                        has_active_terminal = True
                        break
            elif hasattr(window, 'active_terminals') and self.connection in window.active_terminals:
                terminal = window.active_terminals[self.connection]
                # Check if the terminal is still valid and connected
                if terminal and hasattr(terminal, 'is_connected'):
                    has_active_terminal = terminal.is_connected
            
            # Update the connection's is_connected status
            self.connection.is_connected = has_active_terminal
            
            # Log the status update for debugging
            logger.debug(f"Updating status for {self.connection.nickname}: is_connected={has_active_terminal}")
            
            # Update the UI based on the connection status
            if has_active_terminal:
                self.status_icon.set_from_icon_name('network-idle-symbolic')
                self.status_icon.set_tooltip_text(f'Connected to {getattr(self.connection, "hname", "") or self.connection.host}')
                logger.debug(f"Set status icon to connected for {self.connection.nickname}")
            else:
                self.status_icon.set_from_icon_name('network-offline-symbolic')
                self.status_icon.set_tooltip_text('Disconnected')
                logger.debug(f"Set status icon to disconnected for {getattr(self.connection, 'nickname', 'connection')}")
                
            # Force a redraw to ensure the icon updates
            self.status_icon.queue_draw()
            
        except Exception as e:
            logger.error(f"Error updating status for {getattr(self.connection, 'nickname', 'connection')}: {e}")
    
    def update_display(self):
        """Update the display with current connection data"""
        # Update the labels with current connection data
        if hasattr(self.connection, 'nickname') and hasattr(self, 'nickname_label'):
            self.nickname_label.set_markup(f"<b>{self.connection.nickname}</b>")
        
        if hasattr(self.connection, 'username') and hasattr(self.connection, 'host') and hasattr(self, 'host_label'):
            port_text = f":{self.connection.port}" if hasattr(self.connection, 'port') and self.connection.port != 22 else ""
            self.host_label.set_text(f"{self.connection.username}@{self.connection.host}{port_text}")
        # Refresh forwarding indicators if rules changed
        self._update_forwarding_indicators()
        
        self.update_status()

    def show_error(self, message):
        """Show error message"""
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading='Error',
            body=message,
        )
        dialog.add_response('ok', 'OK')
        dialog.set_default_response('ok')
        dialog.present()

class WelcomePage(Gtk.Box):
    """Welcome page shown when no tabs are open"""
    
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        self.set_valign(Gtk.Align.CENTER)
        self.set_halign(Gtk.Align.CENTER)
        self.set_margin_start(48)
        self.set_margin_end(48)
        self.set_margin_top(48)
        self.set_margin_bottom(48)
        
        # Welcome icon
        try:
            texture = Gdk.Texture.new_from_resource('/io/github/mfat/sshpilot/sshpilot.svg')
            icon = Gtk.Image.new_from_paintable(texture)
            icon.set_pixel_size(128)
        except Exception:
            icon = Gtk.Image.new_from_icon_name('network-workgroup-symbolic')
            icon.set_icon_size(Gtk.IconSize.LARGE)
            icon.set_pixel_size(128)
        self.append(icon)
        
        # Welcome message
        message = Gtk.Label()
        message.set_text('Select a host from the list, double-click or press Enter to connect')
        message.set_halign(Gtk.Align.CENTER)
        message.add_css_class('dim-label')
        self.append(message)
        
        # Shortcuts box
        shortcuts_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        shortcuts_box.set_halign(Gtk.Align.CENTER)
        
        shortcuts_title = Gtk.Label()
        shortcuts_title.set_markup('<b>Keyboard Shortcuts</b>')
        shortcuts_box.append(shortcuts_title)
        
        shortcuts = [
            ('Ctrl+N', 'New Connection'),
            ('Ctrl+Alt+N', 'Open  Selected Host in a New Tab'),
            ('F9', 'Toggle Sidebar'),
            ('Ctrl+L', 'Focus connection list to select server'),
            ('Ctrl+Shift+K', 'Copy SSH Key to Server'),
            ('Alt+Right', 'Next Tab'),
            ('Alt+Left', 'Previous Tab'),
            ('Ctrl+F4', 'Close Tab'),
            ('Ctrl+Shift+T', 'New Local Terminal'),
            ('Ctrl+,', 'Preferences'),
        ]
        
        for shortcut, description in shortcuts:
            shortcut_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            
            key_label = Gtk.Label()
            key_label.set_markup(f'<tt>{shortcut}</tt>')
            key_label.set_width_chars(15)
            key_label.set_halign(Gtk.Align.START)
            shortcut_box.append(key_label)
            
            desc_label = Gtk.Label()
            desc_label.set_text(description)
            desc_label.set_halign(Gtk.Align.START)
            shortcut_box.append(desc_label)
            
            shortcuts_box.append(shortcut_box)
        
        self.append(shortcuts_box)

class MainWindow(Adw.ApplicationWindow):
    """Main application window"""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.active_terminals = {}
        self.connections = []
        self._is_quitting = False  # Flag to prevent multiple quit attempts
        self._is_controlled_reconnect = False  # Flag to track controlled reconnection
        
        # Initialize managers
        self.connection_manager = ConnectionManager()
        self.config = Config()
        self.key_manager = KeyManager()
        self.group_manager = GroupManager(self.config)
        
        # UI state
        self.active_terminals: Dict[Connection, TerminalWidget] = {}  # most recent terminal per connection
        self.connection_to_terminals: Dict[Connection, List[TerminalWidget]] = {}
        self.terminal_to_connection: Dict[TerminalWidget, Connection] = {}
        self.connection_rows = {}   # connection -> row_widget
        # Hide hosts toggle state
        try:
            self._hide_hosts = bool(self.config.get_setting('ui.hide_hosts', False))
        except Exception:
            self._hide_hosts = False
        
        # Set up window
        self.setup_window()
        self.setup_ui()
        self.setup_connections()
        self.setup_signals()
        
        # Add action for activating connections
        self.activate_action = Gio.SimpleAction.new('activate-connection', None)
        self.activate_action.connect('activate', self.on_activate_connection)
        self.add_action(self.activate_action)
        # Context menu action to force opening a new connection tab
        self.open_new_connection_action = Gio.SimpleAction.new('open-new-connection', None)
        self.open_new_connection_action.connect('activate', self.on_open_new_connection_action)
        self.add_action(self.open_new_connection_action)
        
        # Global action for opening new connection tab (Ctrl+Alt+N)
        self.open_new_connection_tab_action = Gio.SimpleAction.new('open-new-connection-tab', None)
        self.open_new_connection_tab_action.connect('activate', self.on_open_new_connection_tab_action)
        self.add_action(self.open_new_connection_tab_action)
        
        # Action for managing files on remote server
        self.manage_files_action = Gio.SimpleAction.new('manage-files', None)
        self.manage_files_action.connect('activate', self.on_manage_files_action)
        self.add_action(self.manage_files_action)
        
        # Action for editing connections via context menu
        self.edit_connection_action = Gio.SimpleAction.new('edit-connection', None)
        self.edit_connection_action.connect('activate', self.on_edit_connection_action)
        self.add_action(self.edit_connection_action)
        
        # Action for deleting connections via context menu
        self.delete_connection_action = Gio.SimpleAction.new('delete-connection', None)
        self.delete_connection_action.connect('activate', self.on_delete_connection_action)
        self.add_action(self.delete_connection_action)
        
        # Action for opening connections in system terminal (only when not in Flatpak)
        if not is_running_in_flatpak():
            self.open_in_system_terminal_action = Gio.SimpleAction.new('open-in-system-terminal', None)
            self.open_in_system_terminal_action.connect('activate', self.on_open_in_system_terminal_action)
            self.add_action(self.open_in_system_terminal_action)
            
        # Action for broadcasting commands to all SSH terminals
        self.broadcast_command_action = Gio.SimpleAction.new('broadcast-command', None)
        self.broadcast_command_action.connect('activate', self.on_broadcast_command_action)
        self.add_action(self.broadcast_command_action)
        
        # Group management actions
        self.create_group_action = Gio.SimpleAction.new('create-group', None)
        self.create_group_action.connect('activate', self.on_create_group_action)
        self.add_action(self.create_group_action)
        
        self.edit_group_action = Gio.SimpleAction.new('edit-group', None)
        self.edit_group_action.connect('activate', self.on_edit_group_action)
        self.add_action(self.edit_group_action)
        logger.debug("Edit group action registered")
        
        self.delete_group_action = Gio.SimpleAction.new('delete-group', None)
        self.delete_group_action.connect('activate', self.on_delete_group_action)
        self.add_action(self.delete_group_action)
        
        # Add move to ungrouped action
        self.move_to_ungrouped_action = Gio.SimpleAction.new('move-to-ungrouped', None)
        self.move_to_ungrouped_action.connect('activate', self.on_move_to_ungrouped_action)
        self.add_action(self.move_to_ungrouped_action)
        
        # Add move to group action
        self.move_to_group_action = Gio.SimpleAction.new('move-to-group', None)
        self.move_to_group_action.connect('activate', self.on_move_to_group_action)
        self.add_action(self.move_to_group_action)
        # (Toasts disabled) Remove any toast-related actions if previously defined
        try:
            if hasattr(self, '_toast_reconnect_action'):
                self.remove_action('toast-reconnect')
        except Exception:
            pass
        
        # Connect to close request signal
        self.connect('close-request', self.on_close_request)
        
        # Start with welcome view (tab view setup already shows welcome initially)
        
        logger.info("Main window initialized")

        # Install sidebar CSS
        try:
            self._install_sidebar_css()
        except Exception as e:
            logger.error(f"Failed to install sidebar CSS: {e}")

        # On startup, focus the first item in the connection list (not the toolbar buttons)
        try:
            GLib.idle_add(self._focus_connection_list_first_row)
        except Exception:
            pass
        
        # Check startup behavior setting and show appropriate view
        try:
            startup_behavior = self.config.get_setting('app-startup-behavior', 'terminal')
            if startup_behavior == 'terminal':
                # Show local terminal on startup
                GLib.idle_add(self.show_local_terminal)
            # If startup_behavior == 'welcome', the welcome view is already shown by default
        except Exception as e:
            logger.error(f"Error handling startup behavior: {e}")

    def _install_sidebar_css(self):
        """Install sidebar focus CSS"""
        try:
            # Install CSS for sidebar focus highlighting once per display
            display = Gdk.Display.get_default()
            if not display:
                logger.warning("No display available for CSS installation")
                return
            # Use an attribute on the display to avoid re-adding provider
            if getattr(display, '_sidebar_css_installed', False):
                return
            provider = Gtk.CssProvider()
            css = """
            /* Pulse highlight for selected rows */
            .pulse-highlight {
              background: alpha(@accent_bg_color, 0.5);
              border-radius: 8px;
              box-shadow: 0 0 0 0.5px alpha(@accent_bg_color, 0.28) inset;
              opacity: 0;
              transition: opacity 0.3s ease-in-out;
            }
            .pulse-highlight.on {
              opacity: 1;
            }

            /* optional: a subtle focus ring while the list is focused */
            row:selected:focus-within {
            #   box-shadow: 0 0 8px 2px @accent_bg_color inset;
            #border: 2px solid @accent_bg_color;  /* Adds a solid border of 2px thickness */
              border-radius: 8px;
            }
            
            /* Group styling */
            .group-expand-button {
              min-width: 16px;
              min-height: 16px;
              padding: 2px;
              border-radius: 4px;
            }
            
            .group-expand-button:hover {
              background: alpha(@accent_bg_color, 0.1);
            }
            

            
            /* Drag and drop visual feedback */
            row.drag-highlight {
              background: alpha(@accent_bg_color, 0.2);
              border: 2px dashed @accent_bg_color;
              border-radius: 8px;
            }
            
            /* Drop indicators */
            row.drop-above {
              border-top: 3px solid @accent_bg_color;
              background: alpha(@accent_bg_color, 0.1);
            }
            
            row.drop-below {
              border-bottom: 3px solid @accent_bg_color;
              background: alpha(@accent_bg_color, 0.1);
            }
            
            /* Ungrouped area indicator */
            .ungrouped-area {
              background: alpha(@accent_bg_color, 0.05);
              border: 2px dashed alpha(@accent_bg_color, 0.3);
              border-radius: 8px;
              margin: 8px;
              padding: 12px;
            }
            
            .ungrouped-area.drag-over {
              background: alpha(@accent_bg_color, 0.1);
              border-color: @accent_bg_color;
            }
            """
            provider.load_from_data(css.encode('utf-8'))
            Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            setattr(display, '_sidebar_css_installed', True)
            logger.debug("Sidebar CSS installed successfully")
        except Exception as e:
            logger.error(f"Failed to install sidebar CSS: {e}")
            import traceback
            logger.debug(f"CSS installation traceback: {traceback.format_exc()}")

    def _toggle_class(self, widget, name, on):
        """Helper to toggle CSS class on a widget"""
        if on: 
            widget.add_css_class(name)
        else:  
            widget.remove_css_class(name)



    def pulse_selected_row(self, list_box: Gtk.ListBox, repeats=3, duration_ms=280):
        """Pulse the selected row with highlight effect"""
        row = list_box.get_selected_row() or (list_box.get_selected_rows()[0] if list_box.get_selected_rows() else None)
        if not row:
            return
        if not hasattr(row, "_pulse"):
            return
        # Ensure it's realized so opacity changes render
        if not row.get_mapped():
            row.realize()
        
        # Use CSS-based pulse for now
        pulse = row._pulse
        cycle_duration = max(300, duration_ms // repeats)  # Minimum 300ms per cycle for faster pulses
        
        def do_cycle(count):
            if count == 0:
                return False
            pulse.add_css_class("on")
            # Keep the pulse visible for a shorter time for snappier effect
            GLib.timeout_add(cycle_duration // 2, lambda: (
                pulse.remove_css_class("on"),
                # Add a shorter delay before the next pulse
                GLib.timeout_add(cycle_duration // 2, lambda: do_cycle(count - 1)) or True
            ) and False)
            return False

        GLib.idle_add(lambda: do_cycle(repeats))

    def _test_css_pulse(self, action, param):
        """Simple test to manually toggle CSS class"""
        row = self.connection_list.get_selected_row()
        if row and hasattr(row, "_pulse"):
            pulse = row._pulse
            pulse.add_css_class("on")
            GLib.timeout_add(1000, lambda: (
                pulse.remove_css_class("on")
            ) or False)

    def _setup_interaction_stop_pulse(self):
        """Set up event controllers to stop pulse effect on user interaction"""
        # Mouse click controller
        click_ctl = Gtk.GestureClick()
        click_ctl.connect("pressed", self._stop_pulse_on_interaction)
        self.connection_list.add_controller(click_ctl)
        
        # Key controller
        key_ctl = Gtk.EventControllerKey()
        key_ctl.connect("key-pressed", self._on_connection_list_key_pressed)
        self.connection_list.add_controller(key_ctl)
        
        # Scroll controller
        scroll_ctl = Gtk.EventControllerScroll()
        scroll_ctl.connect("scroll", self._stop_pulse_on_interaction)
        self.connection_list.add_controller(scroll_ctl)

    def _on_connection_list_key_pressed(self, controller, keyval, keycode, state):
        """Handle key presses in the connection list"""
        # Stop pulse effect on any key press
        self._stop_pulse_on_interaction(controller)
        
        # Handle Enter key specifically
        if keyval == Gdk.KEY_Return or keyval == Gdk.KEY_KP_Enter:
            selected_row = self.connection_list.get_selected_row()
            if selected_row and hasattr(selected_row, 'connection'):
                connection = selected_row.connection
                self._focus_most_recent_tab_or_open_new(connection)
                return True  # Consume the event to prevent row-activated
            return False  # Allow group rows to be handled by row-activated
        return False

    def _stop_pulse_on_interaction(self, controller, *args):
        """Stop any ongoing pulse effect when user interacts"""
        # Stop pulse on any row that has the 'on' class
        for row in self.connection_list:
            if hasattr(row, "_pulse"):
                pulse = row._pulse
                if "on" in pulse.get_css_classes():
                    pulse.remove_css_class("on")

    def _wire_pulses(self):
        """Wire pulse effects to trigger on focus-in only"""
        # Track if this is the initial startup focus
        self._is_initial_focus = True
        
        # When list gains keyboard focus (e.g., after Ctrl+L)
        focus_ctl = Gtk.EventControllerFocus()
        def on_focus_enter(*args):
            # Don't pulse on initial startup focus
            if self._is_initial_focus:
                self._is_initial_focus = False
                return
            self.pulse_selected_row(self.connection_list, repeats=1, duration_ms=600)
        focus_ctl.connect("enter", on_focus_enter)
        self.connection_list.add_controller(focus_ctl)
        
        # Stop pulse effect when user interacts with the list
        self._setup_interaction_stop_pulse()
        
        # Add sidebar toggle action and accelerators
        try:
            # Add window-scoped action for sidebar toggle
            sidebar_action = Gio.SimpleAction.new("toggle_sidebar", None)
            sidebar_action.connect("activate", self.on_toggle_sidebar_action)
            self.add_action(sidebar_action)
            

            
            # Bind accelerators (F9 primary, Ctrl+B alternate)
            app = self.get_application()
            if app:
                app.set_accels_for_action("win.toggle_sidebar", ["F9", "<Control>b"])
        except Exception as e:
            logger.warning(f"Failed to add sidebar toggle action: {e}")

    def setup_window(self):
        """Configure main window properties"""
        self.set_title('sshPilot')
        self.set_icon_name('io.github.mfat.sshpilot')
        
        # Load window geometry
        geometry = self.config.get_window_geometry()
        self.set_default_size(geometry['width'], geometry['height'])
        
        # Connect window state signals
        self.connect('notify::default-width', self.on_window_size_changed)
        self.connect('notify::default-height', self.on_window_size_changed)
        # Ensure initial focus after the window is mapped
        try:
            self.connect('map', lambda *a: GLib.timeout_add(50, self._focus_connection_list_first_row))
        except Exception:
            pass

        # Global shortcuts for tab navigation: Alt+Right / Alt+Left
        try:
            nav = Gtk.ShortcutController()
            nav.set_scope(Gtk.ShortcutScope.GLOBAL)
            if hasattr(nav, 'set_propagation_phase'):
                nav.set_propagation_phase(Gtk.PropagationPhase.BUBBLE)

            def _cb_next(widget, *args):
                try:
                    self._select_tab_relative(1)
                except Exception:
                    pass
                return True

            def _cb_prev(widget, *args):
                try:
                    self._select_tab_relative(-1)
                except Exception:
                    pass
                return True

            nav.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string('<Alt>Right'),
                Gtk.CallbackAction.new(_cb_next)
            ))
            nav.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string('<Alt>Left'),
                Gtk.CallbackAction.new(_cb_prev)
            ))
            
            self.add_controller(nav)
        except Exception:
            pass
        
    def on_window_size_changed(self, window, param):
        """Handle window size changes and save the new dimensions"""
        width = self.get_default_width()
        height = self.get_default_height()
        logger.debug(f"Window size changed to: {width}x{height}")
        
        # Save the new window geometry
        self.config.set_window_geometry(width, height)

    def setup_ui(self):
        """Set up the user interface"""
        # Create main container
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        
        # Create header bar
        self.header_bar = Adw.HeaderBar()
        self.header_bar.set_title_widget(Gtk.Label(label="sshPilot"))
        
        # Add window controls (minimize, maximize, close)
        self.header_bar.set_show_start_title_buttons(True)
        self.header_bar.set_show_end_title_buttons(True)
        
        # Add sidebar toggle button to the left side of header bar
        self.sidebar_toggle_button = Gtk.ToggleButton()
        self.sidebar_toggle_button.set_can_focus(False)  # Remove focus from sidebar toggle
        
        # Sidebar always starts visible
        sidebar_visible = True
        
        self.sidebar_toggle_button.set_icon_name('sidebar-show-symbolic')
        self.sidebar_toggle_button.set_tooltip_text('Hide Sidebar (F9, Ctrl+B)')
        self.sidebar_toggle_button.set_active(sidebar_visible)
        self.sidebar_toggle_button.connect('toggled', self.on_sidebar_toggle)
        self.header_bar.pack_start(self.sidebar_toggle_button)
        
        # Add header bar to main container
        main_box.append(self.header_bar)
        
        # Create main layout (fallback if OverlaySplitView is unavailable)
        if HAS_OVERLAY_SPLIT:
            self.split_view = Adw.OverlaySplitView()
            try:
                self.split_view.set_sidebar_width_fraction(0.25)
                self.split_view.set_min_sidebar_width(200)
                self.split_view.set_max_sidebar_width(400)
            except Exception:
                pass
            self.split_view.set_vexpand(True)
            self._split_variant = 'overlay'
        else:
            self.split_view = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
            self.split_view.set_wide_handle(True)
            self.split_view.set_vexpand(True)
            self._split_variant = 'paned'
        
        # Sidebar always starts visible
        sidebar_visible = True
        
        # Create sidebar
        self.setup_sidebar()
        
        # Create main content area
        self.setup_content_area()
        
        # Add split view to main container
        main_box.append(self.split_view)
        
        # Sidebar is always visible on startup

        # Create toast overlay and set main content
        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(main_box)
        self.set_content(self.toast_overlay)

    def _set_sidebar_widget(self, widget: Gtk.Widget) -> None:
        if HAS_OVERLAY_SPLIT:
            try:
                self.split_view.set_sidebar(widget)
                return
            except Exception:
                pass
        # Fallback for Gtk.Paned
        try:
            self.split_view.set_start_child(widget)
        except Exception:
            pass

    def _set_content_widget(self, widget: Gtk.Widget) -> None:
        if HAS_OVERLAY_SPLIT:
            try:
                self.split_view.set_content(widget)
                return
            except Exception:
                pass
        # Fallback for Gtk.Paned
        try:
            self.split_view.set_end_child(widget)
        except Exception:
            pass

    def _get_sidebar_width(self) -> int:
        try:
            if HAS_OVERLAY_SPLIT and hasattr(self.split_view, 'get_max_sidebar_width'):
                return int(self.split_view.get_max_sidebar_width())
        except Exception:
            pass
        # Fallback: attempt to read allocation of the first child when using Paned
        try:
            sidebar = self.split_view.get_start_child()
            if sidebar is not None:
                alloc = sidebar.get_allocation()
                return int(alloc.width)
        except Exception:
            pass
        return 0

    def setup_sidebar(self):
        """Set up the sidebar with connection list"""
        sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar_box.add_css_class('sidebar')
        
        # Sidebar header
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.set_margin_start(12)
        header.set_margin_end(12)
        header.set_margin_top(12)
        header.set_margin_bottom(6)
        
        # Title
        title_label = Gtk.Label()
        title_label.set_markup('<b>Connections</b>')
        title_label.set_halign(Gtk.Align.START)
        title_label.set_hexpand(True)
        header.append(title_label)
        
        # Add connection button
        add_button = Gtk.Button.new_from_icon_name('list-add-symbolic')
        add_button.set_tooltip_text('Add Connection (Ctrl+N)')
        add_button.connect('clicked', self.on_add_connection_clicked)
        try:
            add_button.set_can_focus(False)
        except Exception:
            pass
        header.append(add_button)

        # Menu button
        menu_button = Gtk.MenuButton()
        menu_button.set_can_focus(False)
        menu_button.set_icon_name('open-menu-symbolic')
        menu_button.set_tooltip_text('Menu')
        menu_button.set_menu_model(self.create_menu())
        header.append(menu_button)

        # Hide/Show hostnames button (eye icon)
        def _update_eye_icon(btn):
            try:
                icon = 'view-conceal-symbolic' if self._hide_hosts else 'view-reveal-symbolic'
                btn.set_icon_name(icon)
                btn.set_tooltip_text('Show hostnames' if self._hide_hosts else 'Hide hostnames')
            except Exception:
                pass

        hide_button = Gtk.Button.new_from_icon_name('view-reveal-symbolic')
        _update_eye_icon(hide_button)
        def _on_toggle_hide(btn):
            try:
                self._hide_hosts = not self._hide_hosts
                # Persist setting
                try:
                    self.config.set_setting('ui.hide_hosts', self._hide_hosts)
                except Exception:
                    pass
                # Update all rows
                for row in self.connection_rows.values():
                    if hasattr(row, 'apply_hide_hosts'):
                        row.apply_hide_hosts(self._hide_hosts)
                # Update icon/tooltip
                _update_eye_icon(btn)
            except Exception:
                pass
        hide_button.connect('clicked', _on_toggle_hide)
        try:
            hide_button.set_can_focus(False)
        except Exception:
            pass
        header.append(hide_button)
        
        sidebar_box.append(header)
        
        # Connection list
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        
        self.connection_list = Gtk.ListBox()
        self.connection_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        try:
            self.connection_list.set_can_focus(True)
        except Exception:
            pass
        
        # Wire pulse effects
        self._wire_pulses()
        
        # Connect signals
        self.connection_list.connect('row-selected', self.on_connection_selected)  # For button sensitivity
        self.connection_list.connect('row-activated', self.on_connection_activated)  # For Enter key/double-click
        
        # Make sure the connection list is focusable and can receive key events
        self.connection_list.set_focusable(True)
        self.connection_list.set_can_focus(True)
        self.connection_list.set_focus_on_click(True)
        self.connection_list.set_activate_on_single_click(False)  # Require double-click to activate
        
        # Set up drag and drop for reordering
        self.setup_connection_list_dnd()

        # Right-click context menu to open multiple connections
        try:
            context_click = Gtk.GestureClick()
            context_click.set_button(0)  # handle any button; filter inside
            def _on_list_pressed(gesture, n_press, x, y):
                try:
                    btn = 0
                    try:
                        btn = gesture.get_current_button()
                    except Exception:
                        pass
                    if btn not in (Gdk.BUTTON_SECONDARY, 3):
                        return
                    row = self.connection_list.get_row_at_y(int(y))
                    if not row:
                        return
                    self.connection_list.select_row(row)
                    self._context_menu_connection = getattr(row, 'connection', None)
                    self._context_menu_group_row = row if hasattr(row, 'group_id') else None
                    menu = Gio.Menu()
                    
                    # Add menu items based on row type
                    if hasattr(row, 'group_id'):
                        # Group row context menu
                        logger.debug(f"Creating context menu for group row: {row.group_id}")
                        menu.append(_('✏ Edit Group'), 'win.edit-group')
                        menu.append(_('🗑 Delete Group'), 'win.delete-group')
                    else:
                        # Connection row context menu
                        logger.debug(f"Creating context menu for connection row: {getattr(row, 'connection', None)}")
                        menu.append(_('+ Open New Connection'), 'win.open-new-connection')
                        menu.append(_('✏ Edit Connection'), 'win.edit-connection')
                        menu.append(_('🗄 Manage Files'), 'win.manage-files')
                        # Only show system terminal option when not in Flatpak
                        if not is_running_in_flatpak():
                            menu.append(_('💻 Open in System Terminal'), 'win.open-in-system-terminal')
                        menu.append(_('🗑 Delete Connection'), 'win.delete-connection')
                        
                        # Add grouping options
                        current_group_id = self.group_manager.get_connection_group(row.connection.nickname)
                        if current_group_id:
                            menu.append(_('📁 Move to Ungrouped'), 'win.move-to-ungrouped')
                        else:
                            # Show available groups to move to
                            available_groups = self.get_available_groups()
                            if available_groups:
                                menu.append(_('📁 Move to Group'), 'win.move-to-group')
                    pop = Gtk.PopoverMenu.new_from_model(menu)
                    pop.set_parent(self.connection_list)
                    try:
                        rect = Gdk.Rectangle()
                        rect.x = int(x)
                        rect.y = int(y)
                        rect.width = 1
                        rect.height = 1
                        pop.set_pointing_to(rect)
                    except Exception:
                        pass
                    pop.popup()
                except Exception:
                    pass
            context_click.connect('pressed', _on_list_pressed)
            self.connection_list.add_controller(context_click)
        except Exception:
            pass
        
        # Add keyboard controller for Ctrl+Enter to open new connection
        try:
            key_controller = Gtk.ShortcutController()
            key_controller.set_scope(Gtk.ShortcutScope.LOCAL)
            
            def _on_ctrl_enter(widget, *args):
                try:
                    selected_row = self.connection_list.get_selected_row()
                    if selected_row and hasattr(selected_row, 'connection'):
                        connection = selected_row.connection
                        self.connect_to_host(connection, force_new=True)
                except Exception as e:
                    logger.error(f"Failed to open new connection with Ctrl+Enter: {e}")
                return True
            
            key_controller.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string('<Control>Return'),
                Gtk.CallbackAction.new(_on_ctrl_enter)
            ))
            
            self.connection_list.add_controller(key_controller)
        except Exception as e:
            logger.debug(f"Failed to add Ctrl+Enter shortcut: {e}")
        
        scrolled.set_child(self.connection_list)
        sidebar_box.append(scrolled)
        
        # Sidebar toolbar
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar.set_margin_start(6)
        toolbar.set_margin_end(6)
        toolbar.set_margin_top(6)
        toolbar.set_margin_bottom(6)
        toolbar.add_css_class('toolbar')
        try:
            # Expose the computed visual height so terminal banners can match
            min_h, nat_h, min_baseline, nat_baseline = toolbar.measure(Gtk.Orientation.VERTICAL, -1)
            self._toolbar_row_height = max(min_h, nat_h)
            # Also track the real allocated height dynamically
            def _on_toolbar_alloc(widget, allocation):
                try:
                    self._toolbar_row_height = allocation.height
                except Exception:
                    pass
            toolbar.connect('size-allocate', _on_toolbar_alloc)
        except Exception:
            self._toolbar_row_height = 36
        
        # Edit button
        self.edit_button = Gtk.Button.new_from_icon_name('document-edit-symbolic')
        self.edit_button.set_tooltip_text('Edit Connection')
        self.edit_button.set_sensitive(False)
        self.edit_button.connect('clicked', self.on_edit_connection_clicked)
        toolbar.append(self.edit_button)

        # Copy key to server button (ssh-copy-id)
        self.copy_key_button = Gtk.Button.new_from_icon_name('dialog-password-symbolic')
        self.copy_key_button.set_tooltip_text('Copy public key to server for passwordless login')
        self.copy_key_button.set_sensitive(False)
        self.copy_key_button.connect('clicked', self.on_copy_key_to_server_clicked)
        toolbar.append(self.copy_key_button)

        # Upload (scp) button
        self.upload_button = Gtk.Button.new_from_icon_name('document-send-symbolic')
        self.upload_button.set_tooltip_text('Upload file(s) to server (scp)')
        self.upload_button.set_sensitive(False)
        self.upload_button.connect('clicked', self.on_upload_file_clicked)
        toolbar.append(self.upload_button)

        # Manage files button
        self.manage_files_button = Gtk.Button.new_from_icon_name('folder-symbolic')
        self.manage_files_button.set_tooltip_text('Open file manager for remote server')
        self.manage_files_button.set_sensitive(False)
        self.manage_files_button.connect('clicked', self.on_manage_files_button_clicked)
        toolbar.append(self.manage_files_button)
        
        # System terminal button (only when not in Flatpak)
        if not is_running_in_flatpak():
            self.system_terminal_button = Gtk.Button.new_from_icon_name('utilities-terminal-symbolic')
            self.system_terminal_button.set_tooltip_text('Open connection in system terminal')
            self.system_terminal_button.set_sensitive(False)
            self.system_terminal_button.connect('clicked', self.on_system_terminal_button_clicked)
            toolbar.append(self.system_terminal_button)
        
        # Delete button
        self.delete_button = Gtk.Button.new_from_icon_name('user-trash-symbolic')
        self.delete_button.set_tooltip_text('Delete Connection')
        self.delete_button.set_sensitive(False)
        self.delete_button.connect('clicked', self.on_delete_connection_clicked)
        toolbar.append(self.delete_button)
        
        # Spacer
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        toolbar.append(spacer)
        
        sidebar_box.append(toolbar)
        
        self._set_sidebar_widget(sidebar_box)

    def setup_content_area(self):
        """Set up the main content area with stack for tabs and welcome view"""
        # Create stack to switch between welcome view and tab view
        self.content_stack = Gtk.Stack()
        self.content_stack.set_hexpand(True)
        self.content_stack.set_vexpand(True)
        
        # Create welcome/help view
        self.welcome_view = WelcomePage()
        self.content_stack.add_named(self.welcome_view, "welcome")
        
        # Create tab view
        self.tab_view = Adw.TabView()
        self.tab_view.set_hexpand(True)
        self.tab_view.set_vexpand(True)
        
        # Connect tab signals
        self.tab_view.connect('close-page', self.on_tab_close)
        self.tab_view.connect('page-attached', self.on_tab_attached)
        self.tab_view.connect('page-detached', self.on_tab_detached)

        # Whenever the window layout changes, propagate toolbar height to
        # any TerminalWidget so the reconnect banner exactly matches.
        try:
            # Capture the toolbar variable from this scope for measurement
            local_toolbar = locals().get('toolbar', None)
            def _sync_banner_heights(*args):
                try:
                    # Re-measure toolbar height in case style/theme changed
                    try:
                        if local_toolbar is not None:
                            min_h, nat_h, min_baseline, nat_baseline = local_toolbar.measure(Gtk.Orientation.VERTICAL, -1)
                            self._toolbar_row_height = max(min_h, nat_h)
                    except Exception:
                        pass
                    # Push exact allocated height to all terminal widgets (+5px)
                    for terms in self.connection_to_terminals.values():
                        for term in terms:
                            if hasattr(term, 'set_banner_height'):
                                term.set_banner_height(getattr(self, '_toolbar_row_height', 37) + 55)
                except Exception:
                    pass
            # Call once after UI is built and again after a short delay
            def _push_now():
                try:
                    height = getattr(self, '_toolbar_row_height', 36)
                    for terms in self.connection_to_terminals.values():
                        for term in terms:
                            if hasattr(term, 'set_banner_height'):
                                term.set_banner_height(height + 55)
                except Exception:
                    pass
                return False
            GLib.idle_add(_sync_banner_heights)
            GLib.timeout_add(200, _sync_banner_heights)
            GLib.idle_add(_push_now)
        except Exception:
            pass
        
        # Create tab bar
        self.tab_bar = Adw.TabBar()
        self.tab_bar.set_view(self.tab_view)
        self.tab_bar.set_autohide(False)
        
        # Create tab content box
        tab_content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        tab_content_box.append(self.tab_bar)
        tab_content_box.append(self.tab_view)
        # Ensure background matches terminal theme to avoid white flash
        if hasattr(tab_content_box, 'add_css_class'):
            tab_content_box.add_css_class('terminal-bg')
        
        self.content_stack.add_named(tab_content_box, "tabs")
        # Also color the stack background
        if hasattr(self.content_stack, 'add_css_class'):
            self.content_stack.add_css_class('terminal-bg')
        
        # Start with welcome view visible
        self.content_stack.set_visible_child_name("welcome")
        
        self._set_content_widget(self.content_stack)

    def setup_connection_list_dnd(self):
        """Set up drag and drop for connection list reordering"""
        # Add drop target to the connection list
        drop_target = Gtk.DropTarget.new(type=GObject.TYPE_PYOBJECT, actions=Gdk.DragAction.MOVE)
        drop_target.connect('drop', self._on_connection_list_drop)
        drop_target.connect('motion', self._on_connection_list_motion)
        drop_target.connect('leave', self._on_connection_list_leave)
        self.connection_list.add_controller(drop_target)
        
        # Store drop indicator state
        self._drop_indicator_row = None
        self._drop_indicator_position = None
        
        # Store ungrouped area state
        self._ungrouped_area_row = None
        self._ungrouped_area_visible = False
    
    def _on_connection_list_motion(self, target, x, y):
        """Handle motion over the connection list for visual feedback"""
        try:
            # Clear previous indicator
            self._clear_drop_indicator()
            
            # Show ungrouped area when dragging
            self._show_ungrouped_area()
            
            # Find the row at the current position
            row = self.connection_list.get_row_at_y(int(y))
            if not row:
                return Gdk.DragAction.MOVE
            
            # Check if this is the ungrouped area
            if hasattr(row, 'ungrouped_area') and row.ungrouped_area:
                # Show ungrouped area highlight
                row.add_css_class('drag-over')
                self._drop_indicator_row = row
                self._drop_indicator_position = 'ungrouped'
                return Gdk.DragAction.MOVE
            
            # Determine drop position (above/below the row)
            row_y = row.get_allocation().y
            row_height = row.get_allocation().height
            relative_y = y - row_y
            
            if relative_y < row_height / 2:
                position = 'above'
            else:
                position = 'below'
            
            # Show drop indicator
            self._show_drop_indicator(row, position)
            
            return Gdk.DragAction.MOVE
        except Exception as e:
            logger.error(f"Error handling motion: {e}")
            return Gdk.DragAction.MOVE
    
    def _on_connection_list_leave(self, target):
        """Handle leaving the drop target area"""
        self._clear_drop_indicator()
        self._hide_ungrouped_area()
        return True
    
    def _show_drop_indicator(self, row, position):
        """Show visual drop indicator"""
        try:
            # Add CSS class to the row for visual feedback
            if position == 'above':
                row.add_css_class('drop-above')
            else:
                row.add_css_class('drop-below')
            
            self._drop_indicator_row = row
            self._drop_indicator_position = position
        except Exception as e:
            logger.error(f"Error showing drop indicator: {e}")
    
    def _create_ungrouped_area(self):
        """Create the ungrouped area row"""
        if self._ungrouped_area_row:
            return self._ungrouped_area_row
            
        ungrouped_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        ungrouped_area.add_css_class('ungrouped-area')
        
        # Add icon and label
        icon = Gtk.Image.new_from_icon_name('folder-open-symbolic')
        icon.set_pixel_size(24)
        icon.add_css_class('dim-label')
        
        label = Gtk.Label(label=_("Drop connections here to ungroup them"))
        label.add_css_class('dim-label')
        label.add_css_class('caption')
        
        ungrouped_area.append(icon)
        ungrouped_area.append(label)
        
        # Make it a list box row
        ungrouped_row = Gtk.ListBoxRow()
        ungrouped_row.set_child(ungrouped_area)
        ungrouped_row.set_selectable(False)
        ungrouped_row.set_activatable(False)
        ungrouped_row.ungrouped_area = True
        
        self._ungrouped_area_row = ungrouped_row
        return ungrouped_row
    
    def _show_ungrouped_area(self):
        """Show the ungrouped area at the bottom of the list"""
        try:
            if self._ungrouped_area_visible:
                return
                
            # Only show if there are groups
            hierarchy = self.group_manager.get_group_hierarchy()
            if not hierarchy:
                return
                
            ungrouped_row = self._create_ungrouped_area()
            self.connection_list.append(ungrouped_row)
            self._ungrouped_area_visible = True
            
        except Exception as e:
            logger.error(f"Error showing ungrouped area: {e}")
    
    def _hide_ungrouped_area(self):
        """Hide the ungrouped area"""
        try:
            if not self._ungrouped_area_visible or not self._ungrouped_area_row:
                return
                
            self.connection_list.remove(self._ungrouped_area_row)
            self._ungrouped_area_visible = False
            
        except Exception as e:
            logger.error(f"Error hiding ungrouped area: {e}")
    
    def _clear_drop_indicator(self):
        """Clear visual drop indicator"""
        try:
            if self._drop_indicator_row:
                self._drop_indicator_row.remove_css_class('drop-above')
                self._drop_indicator_row.remove_css_class('drop-below')
                self._drop_indicator_row.remove_css_class('drag-over')
                self._drop_indicator_row = None
                self._drop_indicator_position = None
        except Exception as e:
            logger.error(f"Error clearing drop indicator: {e}")
    
    def _on_connection_list_drop(self, target, value, x, y):
        """Handle drops on the connection list"""
        try:
            # Clear drop indicator and hide ungrouped area
            self._clear_drop_indicator()
            self._hide_ungrouped_area()
            
            if not isinstance(value, dict):
                return False
            
            drop_type = value.get('type')
            if drop_type == 'connection':
                connection_nickname = value.get('connection_nickname')
                if connection_nickname:
                    # Get current group of the connection
                    current_group_id = self.group_manager.get_connection_group(connection_nickname)
                    
                    # Find the target row and position
                    target_row = self.connection_list.get_row_at_y(int(y))
                    if not target_row:
                        # Dropping in empty space - move to ungrouped
                        self.group_manager.move_connection(connection_nickname, None)
                        self.rebuild_connection_list()
                        return True
                    
                    # Check if dropping on ungrouped area
                    if hasattr(target_row, 'ungrouped_area') and target_row.ungrouped_area:
                        # Move to ungrouped
                        self.group_manager.move_connection(connection_nickname, None)
                        self.rebuild_connection_list()
                        return True
                    
                    # Get drop position (above/below)
                    row_y = target_row.get_allocation().y
                    row_height = target_row.get_allocation().height
                    relative_y = y - row_y
                    position = 'above' if relative_y < row_height / 2 else 'below'
                    
                    # Determine if we're dropping on a group or connection
                    if hasattr(target_row, 'group_id'):
                        # Dropping on a group
                        target_group_id = target_row.group_id
                        if target_group_id != current_group_id:
                            # Move to different group
                            self.group_manager.move_connection(connection_nickname, target_group_id)
                            self.rebuild_connection_list()
                    else:
                        # Dropping on a connection
                        target_connection = getattr(target_row, 'connection', None)
                        if target_connection:
                            target_group_id = self.group_manager.get_connection_group(target_connection.nickname)
                            if target_group_id != current_group_id:
                                # Move to different group
                                self.group_manager.move_connection(connection_nickname, target_group_id)
                                self.rebuild_connection_list()
                            else:
                                # Reorder within the same group
                                self.group_manager.reorder_connection_in_group(
                                    connection_nickname, 
                                    target_connection.nickname, 
                                    position
                                )
                                self.rebuild_connection_list()
                    
                    return True
            
            elif drop_type == 'group':
                group_id = value.get('group_id')
                if group_id:
                    # Handle group reordering
                    target_row = self.connection_list.get_row_at_y(int(y))
                    if target_row and hasattr(target_row, 'group_id'):
                        target_group_id = target_row.group_id
                        if target_group_id != group_id:
                            self._move_group(group_id, target_group_id)
                            self.rebuild_connection_list()
                            return True
            
            return False
        except Exception as e:
            logger.error(f"Error handling drop: {e}")
            return False
    
    def _get_target_group_at_position(self, x, y):
        """Get the target group ID at the given position"""
        try:
            row = self.connection_list.get_row_at_y(int(y))
            if row and hasattr(row, 'group_id'):
                return row.group_id
            elif row and hasattr(row, 'connection'):
                # If dropping on a connection, get its group
                connection = row.connection
                return self.group_manager.get_connection_group(connection.nickname)
            return None
        except Exception:
            return None
    
    def _move_group(self, group_id, target_parent_id):
        """Move a group to a new parent"""
        try:
            if group_id not in self.group_manager.groups:
                return
            
            group = self.group_manager.groups[group_id]
            old_parent_id = group.get('parent_id')
            
            # Remove from old parent
            if old_parent_id and old_parent_id in self.group_manager.groups:
                if group_id in self.group_manager.groups[old_parent_id]['children']:
                    self.group_manager.groups[old_parent_id]['children'].remove(group_id)
            
            # Add to new parent
            group['parent_id'] = target_parent_id
            if target_parent_id and target_parent_id in self.group_manager.groups:
                if group_id not in self.group_manager.groups[target_parent_id]['children']:
                    self.group_manager.groups[target_parent_id]['children'].append(group_id)
            
            self.group_manager._save_groups()
        except Exception as e:
            logger.error(f"Error moving group: {e}")

    def create_menu(self):
        """Create application menu"""
        menu = Gio.Menu()
        
        # Add all menu items directly to the main menu
        menu.append('New Connection', 'app.new-connection')
        menu.append('Create Group', 'win.create-group')
        menu.append('Local Terminal', 'app.local-terminal')
        menu.append('Generate SSH Key', 'app.new-key')
        menu.append('Broadcast Command', 'app.broadcast-command')
        menu.append('Preferences', 'app.preferences')
        menu.append('Help', 'app.help')
        menu.append('About', 'app.about')
        menu.append('Quit', 'app.quit')
        
        return menu

    def setup_connections(self):
        """Load and display existing connections with grouping"""
        self.rebuild_connection_list()
        
        # Select first connection if available
        connections = self.connection_manager.get_connections()
        if connections:
            first_row = self.connection_list.get_row_at_index(0)
            if first_row:
                self.connection_list.select_row(first_row)
                # Defer focus to the list to ensure keyboard navigation works immediately
                GLib.idle_add(self._focus_connection_list_first_row)
    
    def rebuild_connection_list(self):
        """Rebuild the connection list with groups"""
        # Clear existing rows
        child = self.connection_list.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.connection_list.remove(child)
            child = next_child
        self.connection_rows.clear()
        
        # Get all connections
        connections = self.connection_manager.get_connections()
        connections_dict = {conn.nickname: conn for conn in connections}
        
        # Get group hierarchy
        hierarchy = self.group_manager.get_group_hierarchy()
        
        # Build the list with groups
        self._build_grouped_list(hierarchy, connections_dict, 0)
        
        # Add ungrouped connections at the end
        ungrouped_connections = []
        for conn in connections:
            if not self.group_manager.get_connection_group(conn.nickname):
                ungrouped_connections.append(conn)
        
        if ungrouped_connections:
            # No separator - just add ungrouped connections directly
            pass
            
            # Add ungrouped connections
            for conn in sorted(ungrouped_connections, key=lambda c: c.nickname.lower()):
                self.add_connection_row(conn)
        
        # Store reference to ungrouped area (hidden by default)
        self._ungrouped_area_row = None
    
    def _build_grouped_list(self, hierarchy, connections_dict, level):
        """Recursively build the grouped connection list"""
        for group_info in hierarchy:
            # Add group row
            group_row = GroupRow(group_info, self.group_manager, connections_dict)
            group_row.connect('group-toggled', self._on_group_toggled)
            self.connection_list.append(group_row)
            
            # Add connections in this group if expanded
            if group_info.get('expanded', True):
                group_connections = []
                for conn_nickname in group_info.get('connections', []):
                    if conn_nickname in connections_dict:
                        group_connections.append(connections_dict[conn_nickname])
                
                # Use the order from the group's connections list (preserves custom ordering)
                for conn_nickname in group_info.get('connections', []):
                    if conn_nickname in connections_dict:
                        conn = connections_dict[conn_nickname]
                        self.add_connection_row(conn, level + 1)
            
            # Recursively add child groups
            if group_info.get('children'):
                self._build_grouped_list(group_info['children'], connections_dict, level + 1)
    
    def _on_group_toggled(self, group_row, group_id, expanded):
        """Handle group expand/collapse"""
        self.rebuild_connection_list()

        # Reselect the toggled group so focus doesn't jump to another row
        for row in self.connection_list:
            if hasattr(row, "group_id") and row.group_id == group_id:
                self.connection_list.select_row(row)
                break
    
    def add_connection_row(self, connection: Connection, indent_level: int = 0):
        """Add a connection row to the list with optional indentation"""
        row = ConnectionRow(connection)
        
        # Apply indentation for grouped connections
        if indent_level > 0:
            content = row.get_child()
            if hasattr(content, 'get_child'):  # Handle overlay
                content = content.get_child()
            content.set_margin_start(12 + (indent_level * 20))
        
        self.connection_list.append(row)
        self.connection_rows[connection] = row
        
        # Apply current hide-hosts setting to new row
        if hasattr(row, 'apply_hide_hosts'):
            row.apply_hide_hosts(getattr(self, '_hide_hosts', False))

    def setup_signals(self):
        """Connect to manager signals"""
        # Connection manager signals - use connect_after to avoid conflict with GObject.connect
        self.connection_manager.connect_after('connection-added', self.on_connection_added)
        self.connection_manager.connect_after('connection-removed', self.on_connection_removed)
        self.connection_manager.connect_after('connection-status-changed', self.on_connection_status_changed)
        
        # Config signals
        self.config.connect('setting-changed', self.on_setting_changed)



    def show_welcome_view(self):
        """Show the welcome/help view when no connections are active"""
        # Remove terminal background styling so welcome uses app theme colors
        if hasattr(self.content_stack, 'remove_css_class'):
            try:
                self.content_stack.remove_css_class('terminal-bg')
            except Exception:
                pass
        # Ensure welcome fills the pane
        if hasattr(self, 'welcome_view'):
            try:
                self.welcome_view.set_hexpand(True)
                self.welcome_view.set_vexpand(True)
            except Exception:
                pass
        self.content_stack.set_visible_child_name("welcome")
        logger.info("Showing welcome view")

    def _focus_connection_list_first_row(self):
        """Focus the connection list and ensure the first row is selected."""
        try:
            if not hasattr(self, 'connection_list') or self.connection_list is None:
                return False
            # If the list has no selection, select the first row
            selected = self.connection_list.get_selected_row() if hasattr(self.connection_list, 'get_selected_row') else None
            first_row = self.connection_list.get_row_at_index(0)
            if not selected and first_row:
                self.connection_list.select_row(first_row)
            # If no widget currently has focus in the window, give it to the list
            focus_widget = self.get_focus() if hasattr(self, 'get_focus') else None
            if focus_widget is None and first_row:
                self.connection_list.grab_focus()
        except Exception:
            pass
        return False

    def focus_connection_list(self):
        """Focus the connection list and show a toast notification."""
        try:
            if hasattr(self, 'connection_list') and self.connection_list:
                # If sidebar is hidden, show it first
                if hasattr(self, 'sidebar_toggle_button') and self.sidebar_toggle_button:
                    if not self.sidebar_toggle_button.get_active():
                        self.sidebar_toggle_button.set_active(True)
                
                # Ensure a row is selected before focusing
                selected = self.connection_list.get_selected_row()
                logger.debug(f"Focus connection list - current selection: {selected}")
                if not selected:
                    # Select the first row regardless of type
                    first_row = self.connection_list.get_row_at_index(0)
                    logger.debug(f"Focus connection list - first row: {first_row}")
                    if first_row:
                        self.connection_list.select_row(first_row)
                        logger.debug(f"Focus connection list - selected first row: {first_row}")
                
                self.connection_list.grab_focus()
                
                # Pulse the selected row
                self.pulse_selected_row(self.connection_list, repeats=1, duration_ms=600)
                
                # Show toast notification
                toast = Adw.Toast.new(
                    "Switched to connection list — ↑/↓ navigate, Enter open, Ctrl+Enter new tab"
                )
                toast.set_timeout(3)  # seconds
                self.toast_overlay.add_toast(toast)
        except Exception as e:
            logger.error(f"Error focusing connection list: {e}")
    
    def show_tab_view(self):
        """Show the tab view when connections are active"""
        # Re-apply terminal background when switching back to tabs
        if hasattr(self.content_stack, 'add_css_class'):
            try:
                self.content_stack.add_css_class('terminal-bg')
            except Exception:
                pass
        self.content_stack.set_visible_child_name("tabs")
        logger.info("Showing tab view")

    def show_connection_dialog(self, connection: Connection = None):
        """Show connection dialog for adding/editing connections"""
        logger.info(f"Show connection dialog for: {connection}")
        
        # Create connection dialog
        dialog = ConnectionDialog(self, connection, self.connection_manager)
        dialog.connect('connection-saved', self.on_connection_saved)
        dialog.present()

    # --- Helpers (use your existing ones if already present) ---------------------

    def _error_dialog(self, heading: str, body: str, detail: str = ""):
        try:
            msg = Adw.MessageDialog(transient_for=self, modal=True,
                                    heading=heading, body=(body + (f"\n\n{detail}" if detail else "")))
            msg.add_response("ok", _("OK"))
            msg.set_default_response("ok")
            msg.set_close_response("ok")
            msg.present()
        except Exception:
            pass

    def _info_dialog(self, heading: str, body: str):
        try:
            msg = Adw.MessageDialog(transient_for=self, modal=True,
                                    heading=heading, body=body)
            msg.add_response("ok", _("OK"))
            msg.set_default_response("ok")
            msg.set_close_response("ok")
            msg.present()
        except Exception:
            pass


    # --- Single, simplified key generator (no copy-to-server inside) ------------

    def show_key_dialog(self, on_success=None):
        """
        Single key generation dialog (Adw). Optional passphrase.
        No copy-to-server in this dialog. If provided, `on_success(key)` is called.
        """
        try:
            dlg = Adw.Dialog.new()
            dlg.set_title(_("Generate SSH Key"))

            tv = Adw.ToolbarView()
            hb = Adw.HeaderBar()
            hb.set_title_widget(Gtk.Label(label=_("New SSH Key")))
            tv.add_top_bar(hb)

            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            content.set_margin_top(18); content.set_margin_bottom(18)
            content.set_margin_start(18); content.set_margin_end(18)
            content.set_size_request(500, -1)

            form = Adw.PreferencesGroup()

            name_row = Adw.EntryRow()
            name_row.set_title(_("Key file name"))
            name_row.set_text("id_ed25519")
            
            # Add real-time validation
            def on_name_changed(entry):
                key_name = (entry.get_text() or "").strip()
                if key_name and not key_name.startswith(".") and "/" not in key_name:
                    key_path = self.key_manager.ssh_dir / key_name
                    if key_path.exists():
                        entry.add_css_class("error")
                        entry.set_title(_("Key file name (already exists)"))
                    else:
                        entry.remove_css_class("error")
                        entry.set_title(_("Key file name"))
                else:
                    entry.remove_css_class("error")
                    entry.set_title(_("Key file name"))
            
            name_row.connect("changed", on_name_changed)
            form.add(name_row)

            type_row = Adw.ComboRow()
            type_row.set_title(_("Key type"))
            types = Gtk.StringList.new(["ed25519", "rsa"])
            type_row.set_model(types)
            type_row.set_selected(0)
            form.add(type_row)

            pass_switch = Adw.SwitchRow()
            pass_switch.set_title(_("Encrypt with passphrase"))
            pass_switch.set_active(False)
            form.add(pass_switch)

            pass_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            pass1 = Gtk.PasswordEntry()
            pass1.set_property("placeholder-text", _("Passphrase"))
            pass2 = Gtk.PasswordEntry()
            pass2.set_property("placeholder-text", _("Confirm passphrase"))
            pass_box.append(pass1); pass_box.append(pass2)
            pass_box.set_visible(False)



            def on_pass_toggle(*_):
                pass_box.set_visible(pass_switch.get_active())
            pass_switch.connect("notify::active", on_pass_toggle)

            # Buttons
            btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            btn_box.set_halign(Gtk.Align.END)
            btn_cancel = Gtk.Button.new_with_label(_("Cancel"))
            btn_primary = Gtk.Button.new_with_label(_("Generate"))
            try:
                btn_primary.add_css_class("suggested-action")
            except Exception:
                pass
            btn_box.append(btn_cancel); btn_box.append(btn_primary)

            # Compose
            content.append(form)
            content.append(pass_box)
            content.append(btn_box)
            tv.set_content(content)
            dlg.set_child(tv)

            def close_dialog(*args):
                try:
                    dlg.force_close()
                except Exception:
                    pass

            btn_cancel.connect("clicked", close_dialog)

            def do_generate(*args):
                try:
                    key_name = (name_row.get_text() or "").strip()
                    if not key_name:
                        raise ValueError(_("Enter a key file name (e.g. id_ed25519)"))
                    if "/" in key_name or key_name.startswith("."):
                        raise ValueError(_("Key file name must not contain '/' or start with '.'"))

                    # Check if key already exists before attempting generation
                    key_path = self.key_manager.ssh_dir / key_name
                    if key_path.exists():
                        # Suggest alternative names
                        base_name = key_name
                        counter = 1
                        while (self.key_manager.ssh_dir / f"{base_name}_{counter}").exists():
                            counter += 1
                        suggestion = f"{base_name}_{counter}"
                        
                        raise ValueError(_("A key named '{}' already exists. Try '{}' instead.").format(key_name, suggestion))

                    kt = "ed25519" if type_row.get_selected() == 0 else "rsa"

                    passphrase = None
                    if pass_switch.get_active():
                        p1 = pass1.get_text() or ""
                        p2 = pass2.get_text() or ""
                        logger.debug(f"SshCopyIdWindow: Passphrase lengths - p1: {len(p1)}, p2: {len(p2)}")
                        if p1 != p2:
                            logger.debug("SshCopyIdWindow: Passphrases do not match")
                            raise ValueError("Passphrases do not match")
                        passphrase = p1
                        logger.info("SshCopyIdWindow: Passphrase enabled")
                        logger.debug("SshCopyIdWindow: Passphrase validation successful")

                    logger.info(f"SshCopyIdWindow: Calling key_manager.generate_key with name='{key_name}', type='{kt}'")
                    logger.debug(f"SshCopyIdWindow: Key generation parameters - name='{key_name}', type='{kt}', "
                               f"size={3072 if kt == 'rsa' else 0}, passphrase={'<set>' if passphrase else 'None'}")
                    
                    new_key = self._km.generate_key(
                        key_name=key_name,
                        key_type=kt,
                        key_size=3072 if kt == "rsa" else 0,
                        comment=None,
                        passphrase=passphrase,
                    )
                    
                    if not new_key:
                        logger.debug("SshCopyIdWindow: Key generation returned None")
                        raise RuntimeError("Key generation failed. See logs for details.")

                    logger.info(f"SshCopyIdWindow: Key generated successfully: {new_key.private_path}")
                    logger.debug(f"SshCopyIdWindow: Generated key details - private_path='{new_key.private_path}', "
                               f"public_path='{new_key.public_path}'")
                    
                    # Ensure the key files are properly written and accessible
                    import time
                    logger.debug("SshCopyIdWindow: Waiting 0.5s for files to be written")
                    time.sleep(0.5)  # Small delay to ensure files are written
                    
                    # Verify the key files exist and are accessible
                    private_exists = os.path.exists(new_key.private_path)
                    public_exists = os.path.exists(new_key.public_path)
                    logger.debug(f"SshCopyIdWindow: File existence check - private: {private_exists}, public: {public_exists}")
                    
                    if not private_exists:
                        logger.debug(f"SshCopyIdWindow: Private key file missing: {new_key.private_path}")
                        raise RuntimeError(f"Private key file not found: {new_key.private_path}")
                    if not public_exists:
                        logger.debug(f"SshCopyIdWindow: Public key file missing: {new_key.public_path}")
                        raise RuntimeError(f"Public key file not found: {new_key.public_path}")
                    
                    logger.info(f"SshCopyIdWindow: Key files verified, starting ssh-copy-id")
                    logger.debug("SshCopyIdWindow: All key files verified successfully")
                    
                    # Run your terminal ssh-copy-id flow
                    logger.debug("SshCopyIdWindow: Calling _show_ssh_copy_id_terminal_using_main_widget()")
                    self._parent._show_ssh_copy_id_terminal_using_main_widget(self._conn, new_key)
                    logger.debug("SshCopyIdWindow: Terminal window launched, closing dialog")
                    self.close()

                except Exception as e:
                    logger.error(f"SshCopyIdWindow: Generate and copy failed: {e}")
                    logger.debug(f"SshCopyIdWindow: Exception details: {type(e).__name__}: {str(e)}")
                    self._error("Generate & Copy failed",
                                "Could not generate a new key and copy it to the server.",
                                str(e))

            btn_primary.connect("clicked", do_generate)
            dlg.present()
            return dlg
        except Exception as e:
            logger.error("Failed to present key generator: %s", e)


    # --- Integrate generator into ssh-copy-id chooser ---------------------------

    def on_copy_key_to_server_clicked(self, _button):
        logger.info("Main window: ssh-copy-id button clicked")
        logger.debug("Main window: Starting ssh-copy-id process")
        
        selected_row = self.connection_list.get_selected_row()
        if not selected_row or not getattr(selected_row, "connection", None):
            logger.warning("Main window: No connection selected for ssh-copy-id")
            return
        connection = selected_row.connection
        logger.info(f"Main window: Selected connection: {getattr(connection, 'nickname', 'unknown')}")
        logger.debug(f"Main window: Connection details - host: {getattr(connection, 'host', 'unknown')}, "
                    f"username: {getattr(connection, 'username', 'unknown')}, "
                    f"port: {getattr(connection, 'port', 22)}")

        try:
            logger.info("Main window: Creating SshCopyIdWindow")
            logger.debug("Main window: Initializing SshCopyIdWindow with key_manager and connection_manager")
            win = SshCopyIdWindow(self, connection, self.key_manager, self.connection_manager)
            logger.info("Main window: SshCopyIdWindow created successfully, presenting")
            win.present()
        except Exception as e:
            logger.error(f"Main window: ssh-copy-id window failed: {e}")
            logger.debug(f"Main window: Exception details: {type(e).__name__}: {str(e)}")
            # Fallback error if window cannot be created
            try:
                md = Adw.MessageDialog(transient_for=self, modal=True,
                                       heading="Error",
                                       body=f"Could not open the Copy Key window.\n\n{e}")
                md.add_response("ok", "OK")
                md.present()
            except Exception:
                pass

    def show_preferences(self):
        """Show preferences dialog"""
        logger.info("Show preferences dialog")
        try:
            preferences_window = PreferencesWindow(self, self.config)
            preferences_window.present()
        except Exception as e:
            logger.error(f"Failed to show preferences dialog: {e}")

    def show_local_terminal(self):
        """Show a local terminal tab"""
        logger.info("Show local terminal tab")
        try:
            # Create a local terminal widget
            from .terminal import TerminalWidget
            
            # Create a dummy connection object for local terminal
            class LocalConnection:
                def __init__(self):
                    self.nickname = "Local Terminal"
                    self.host = "localhost"
                    self.username = os.getenv('USER', 'user')
                    self.port = 22
            
            local_connection = LocalConnection()
            
            # Create terminal widget for local shell
            logger.info("Creating TerminalWidget...")
            terminal_widget = TerminalWidget(local_connection, self.config, self.connection_manager)
            logger.info("TerminalWidget created successfully")
            
            # Set up the terminal for local shell (not SSH)
            logger.info("Setting up local shell...")
            terminal_widget.setup_local_shell()
            logger.info("Local shell setup completed")
            
            # Add to tab view
            logger.info("Adding terminal to tab view...")
            self._add_terminal_tab(terminal_widget, "Local Terminal")
            
            # Ensure the terminal widget is properly shown
            GLib.idle_add(terminal_widget.show)
            GLib.idle_add(terminal_widget.vte.show)
            
            logger.info("Local terminal tab created successfully")
            
        except Exception as e:
            logger.error(f"Failed to show local terminal: {e}")
            # Show error dialog
            try:
                dialog = Adw.MessageDialog(
                    transient_for=self,
                    modal=True,
                    heading="Error",
                    body=f"Could not open local terminal.\n\n{e}"
                )
                dialog.add_response("ok", "OK")
                dialog.present()
            except Exception:
                pass

    def _add_terminal_tab(self, terminal_widget, title):
        """Add a terminal widget to the tab view"""
        try:
            # Add to tab view
            page = self.tab_view.append(terminal_widget)
            page.set_title(title)
            page.set_icon(Gio.ThemedIcon.new('utilities-terminal-symbolic'))
            
            # Switch to tab view
            self.show_tab_view()
            
            # Activate the new tab
            self.tab_view.set_selected_page(page)
            
            logger.info(f"Added terminal tab: {title}")
            
        except Exception as e:
            logger.error(f"Failed to add terminal tab: {e}")

    def broadcast_command(self, command: str):
        """Send a command to all open SSH terminal tabs (excluding local terminal tabs)"""
        cmd = (command + "\n").encode("utf-8")
        sent_count = 0
        failed_count = 0
        
        # Get all open terminals from the tab view
        for i in range(self.tab_view.get_n_pages()):
            page = self.tab_view.get_nth_page(i)
            if page is None:
                continue
                
            # Get the terminal widget from the page
            terminal_widget = page.get_child()
            if terminal_widget is None or not hasattr(terminal_widget, 'vte'):
                continue
                
            # Check if this is a local terminal by looking at the connection
            if hasattr(terminal_widget, 'connection'):
                # Skip local terminals
                if (hasattr(terminal_widget.connection, 'nickname') and 
                    terminal_widget.connection.nickname == "Local Terminal"):
                    continue
                    
                # Skip terminals that don't have a connection (shouldn't happen for SSH terminals)
                if not hasattr(terminal_widget.connection, 'host'):
                    continue
                    
                # This is an SSH terminal, send the command
                try:
                    terminal_widget.vte.feed_child(cmd)
                    sent_count += 1
                    logger.debug(f"Sent command to SSH terminal: {terminal_widget.connection.nickname}")
                except Exception as e:
                    failed_count += 1
                    logger.error(f"Failed to send command to terminal {terminal_widget.connection.nickname}: {e}")
        
        logger.info(f"Broadcast command completed: {sent_count} terminals received command, {failed_count} failed")
        return sent_count, failed_count

    def show_about_dialog(self):
        """Show about dialog"""
        # Use Gtk.AboutDialog so we can force a logo even without icon theme entries
        about = Gtk.AboutDialog()
        about.set_transient_for(self)
        about.set_modal(True)
        about.set_program_name('sshPilot')
        try:
            from . import __version__ as APP_VERSION
        except Exception:
            APP_VERSION = "0.0.0"
        about.set_version(APP_VERSION)
        about.set_comments('SSH connection manager with integrated terminal')
        about.set_website('https://github.com/mfat/sshpilot')
        # Gtk.AboutDialog in GTK4 has no set_issue_url; include issue link in website label
        about.set_website_label('Project homepage')
        about.set_license_type(Gtk.License.GPL_3_0)
        about.set_authors(['mFat <newmfat@gmail.com>'])
        
        # Attempt to load logo from GResource; fall back to local files
        logo_texture = None
        # 1) From GResource bundle
        for resource_path in (
            '/io/github/mfat/sshpilot/sshpilot.svg',
        ):
            try:
                logo_texture = Gdk.Texture.new_from_resource(resource_path)
                if logo_texture:
                    break
            except Exception:
                logo_texture = None
        # 2) From project-local files
        if logo_texture is None:
            candidate_files = []
            # repo root (user added io.github.mfat.sshpilot.png)
            try:
                path = os.path.abspath(os.path.dirname(__file__))
                repo_root = path
                while True:
                    if os.path.exists(os.path.join(repo_root, '.git')):
                        break
                    parent = os.path.dirname(repo_root)
                    if parent == repo_root:
                        break
                    repo_root = parent
                candidate_files.extend([
                    os.path.join(repo_root, 'io.github.mfat.sshpilot.svg'),
                    os.path.join(repo_root, 'sshpilot.svg'),
                ])
                # package resources folder (when running from source)
                candidate_files.append(os.path.join(os.path.dirname(__file__), 'resources', 'sshpilot.svg'))
            except Exception:
                pass
            for png_path in candidate_files:
                try:
                    if os.path.exists(png_path):
                        logo_texture = Gdk.Texture.new_from_filename(png_path)
                        if logo_texture:
                            break
                except Exception:
                    logo_texture = None
        # Apply if loaded
        if logo_texture is not None:
            try:
                about.set_logo(logo_texture)
            except Exception:
                pass
        
        about.present()

    def open_help_url(self):
        """Open the SSH Pilot wiki in the default browser"""
        try:
            import subprocess
            import webbrowser
            
            # Try to open the URL using the default browser
            url = "https://github.com/mfat/sshpilot/wiki"
            
            # Use webbrowser module which handles platform differences
            webbrowser.open(url)
            
            logger.info(f"Opened help URL: {url}")
        except Exception as e:
            logger.error(f"Failed to open help URL: {e}")
            # Fallback: show an error dialog
            try:
                dialog = Gtk.MessageDialog(
                    transient_for=self,
                    modal=True,
                    message_type=Gtk.MessageType.ERROR,
                    buttons=Gtk.ButtonsType.OK,
                    text="Failed to open help",
                    secondary_text=f"Could not open the help URL. Please visit:\n{url}"
                )
                dialog.present()
            except Exception:
                pass

    def toggle_list_focus(self):
        """Toggle focus between connection list and terminal"""
        if self.connection_list.has_focus():
            # Focus current terminal
            current_page = self.tab_view.get_selected_page()
            if current_page:
                child = current_page.get_child()
                if hasattr(child, 'vte'):
                    child.vte.grab_focus()
        else:
            # Focus connection list with toast notification
            self.focus_connection_list()

    def _select_tab_relative(self, delta: int):
        """Select tab relative to current index, wrapping around."""
        try:
            n = self.tab_view.get_n_pages()
            if n <= 0:
                return
            current = self.tab_view.get_selected_page()
            # If no current selection, pick first
            if not current:
                page = self.tab_view.get_nth_page(0)
                if page:
                    self.tab_view.set_selected_page(page)
                return
            # Find current index
            idx = 0
            for i in range(n):
                if self.tab_view.get_nth_page(i) == current:
                    idx = i
                    break
            new_index = (idx + delta) % n
            page = self.tab_view.get_nth_page(new_index)
            if page:
                self.tab_view.set_selected_page(page)
        except Exception:
            pass

    def connect_to_host(self, connection: Connection, force_new: bool = False):
        """Connect to SSH host and create terminal tab.
        If force_new is False and a tab exists for this server, select the most recent tab.
        If force_new is True, always open a new tab.
        """
        if not force_new:
            # If a tab exists for this connection, activate the most recent one
            if connection in self.active_terminals:
                terminal = self.active_terminals[connection]
                page = self.tab_view.get_page(terminal)
                if page is not None:
                    self.tab_view.set_selected_page(page)
                    return
                else:
                    # Terminal exists but not in tab view, remove from active terminals
                    logger.warning(f"Terminal for {connection.nickname} not found in tab view, removing from active terminals")
                    del self.active_terminals[connection]
            # Fallback: look up any existing terminals for this connection
            existing_terms = self.connection_to_terminals.get(connection) or []
            for t in reversed(existing_terms):  # most recent last
                page = self.tab_view.get_page(t)
                if page is not None:
                    self.active_terminals[connection] = t
                    self.tab_view.set_selected_page(page)
                    return
        
        # Check preferred terminal setting
        use_external = self.config.get_setting('use-external-terminal', False)
        
        if use_external and not is_running_in_flatpak():
            # Use external terminal
            self._open_connection_in_external_terminal(connection)
            return
        else:
            # Use built-in terminal
            # Create new terminal
            terminal = TerminalWidget(connection, self.config, self.connection_manager)
            
            # Connect signals
            terminal.connect('connection-established', self.on_terminal_connected)
            terminal.connect('connection-failed', lambda w, e: logger.error(f"Connection failed: {e}"))
            terminal.connect('connection-lost', self.on_terminal_disconnected)
            terminal.connect('title-changed', self.on_terminal_title_changed)
            
            # Add to tab view
            page = self.tab_view.append(terminal)
            page.set_title(connection.nickname)
            page.set_icon(Gio.ThemedIcon.new('utilities-terminal-symbolic'))
            
            # Store references for multi-tab tracking
            self.connection_to_terminals.setdefault(connection, []).append(terminal)
            self.terminal_to_connection[terminal] = connection
            self.active_terminals[connection] = terminal
            
            # Switch to tab view when first connection is made
            self.show_tab_view()
            
            # Activate the new tab
            self.tab_view.set_selected_page(page)
        
        # Force set colors after the terminal is added to the UI
        def _set_terminal_colors():
            try:
                # Set colors using RGBA
                fg = Gdk.RGBA()
                fg.parse('rgb(0,0,0)')  # Black
                
                bg = Gdk.RGBA()
                bg.parse('rgb(255,255,255)')  # White
                
                # Set colors using both methods for maximum compatibility
                terminal.vte.set_color_foreground(fg)
                terminal.vte.set_color_background(bg)
                terminal.vte.set_colors(fg, bg, None)
                
                # Force a redraw
                terminal.vte.queue_draw()
                
                # Connect to the SSH server after setting colors
                if not terminal._connect_ssh():
                    logger.error("Failed to establish SSH connection")
                    self.tab_view.close_page(page)
                    # Cleanup on failure
                    try:
                        if connection in self.active_terminals and self.active_terminals[connection] is terminal:
                            del self.active_terminals[connection]
                        if terminal in self.terminal_to_connection:
                            del self.terminal_to_connection[terminal]
                        if connection in self.connection_to_terminals and terminal in self.connection_to_terminals[connection]:
                            self.connection_to_terminals[connection].remove(terminal)
                            if not self.connection_to_terminals[connection]:
                                del self.connection_to_terminals[connection]
                    except Exception:
                        pass
                        
            except Exception as e:
                logger.error(f"Error setting terminal colors: {e}")
                # Still try to connect even if color setting fails
                if not terminal._connect_ssh():
                    logger.error("Failed to establish SSH connection")
                    self.tab_view.close_page(page)
                    # Cleanup on failure
                    try:
                        if connection in self.active_terminals and self.active_terminals[connection] is terminal:
                            del self.active_terminals[connection]
                        if terminal in self.terminal_to_connection:
                            del self.terminal_to_connection[terminal]
                        if connection in self.connection_to_terminals and terminal in self.connection_to_terminals[connection]:
                            self.connection_to_terminals[connection].remove(terminal)
                            if not self.connection_to_terminals[connection]:
                                del self.connection_to_terminals[connection]
                    except Exception:
                        pass
        
        # Schedule the color setting to run after the terminal is fully initialized
        GLib.idle_add(_set_terminal_colors)

    def _on_disconnect_confirmed(self, dialog, response_id, connection):
        """Handle response from disconnect confirmation dialog"""
        dialog.destroy()
        if response_id == 'disconnect' and connection in self.active_terminals:
            terminal = self.active_terminals[connection]
            terminal.disconnect()
            # If part of a delete flow, remove the connection now
            if getattr(self, '_pending_delete_connection', None) is connection:
                try:
                    self.connection_manager.remove_connection(connection)
                finally:
                    self._pending_delete_connection = None
    
    def disconnect_from_host(self, connection: Connection):
        """Disconnect from SSH host"""
        if connection not in self.active_terminals:
            return
            
        # Check if confirmation is required
        confirm_disconnect = self.config.get_setting('confirm-disconnect', True)
        
        if confirm_disconnect:
            # Show confirmation dialog
            dialog = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_("Disconnect from {}").format(connection.nickname or connection.host),
                body=_("Are you sure you want to disconnect from this host?")
            )
            dialog.add_response('cancel', _("Cancel"))
            dialog.add_response('disconnect', _("Disconnect"))
            dialog.set_response_appearance('disconnect', Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response('close')
            dialog.set_close_response('cancel')
            
            dialog.connect('response', self._on_disconnect_confirmed, connection)
            dialog.present()
        else:
            # Disconnect immediately without confirmation
            terminal = self.active_terminals[connection]
            terminal.disconnect()

    # Signal handlers
    def on_connection_click(self, gesture, n_press, x, y):
        """Handle clicks on the connection list"""
        # Get the row that was clicked
        row = self.connection_list.get_row_at_y(int(y))
        if row is None:
            return
        
        if n_press == 1:  # Single click - just select
            self.connection_list.select_row(row)
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        elif n_press == 2:  # Double click - connect
            if hasattr(row, 'connection'):
                self._cycle_connection_tabs_or_open(row.connection)
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def on_connection_activated(self, list_box, row):
        """Handle connection activation (Enter key)"""
        logger.debug(f"Connection activated - row: {row}, has connection: {hasattr(row, 'connection') if row else False}")
        if row and hasattr(row, 'connection'):
            self._cycle_connection_tabs_or_open(row.connection)
        elif row and hasattr(row, 'group_id'):
            # Handle group row activation - toggle expand/collapse
            logger.debug(f"Group row activated - toggling expand/collapse for group: {row.group_id}")
            row._toggle_expand()
            

        
    def on_connection_activate(self, list_box, row):
        """Handle connection activation (Enter key or double-click)"""
        if row and hasattr(row, 'connection'):
            self._cycle_connection_tabs_or_open(row.connection)
            return True  # Stop event propagation
        return False
        
    def on_activate_connection(self, action, param):
        """Handle the activate-connection action"""
        row = self.connection_list.get_selected_row()
        if row and hasattr(row, 'connection'):
            self._cycle_connection_tabs_or_open(row.connection)
            


    def _focus_most_recent_tab_or_open_new(self, connection: Connection):
        """If there are open tabs for this server, focus the most recent one.
        Otherwise open a new tab for the server.
        """
        try:
            # Check if there are open tabs for this connection
            terms_for_conn = []
            try:
                n = self.tab_view.get_n_pages()
            except Exception:
                n = 0
            for i in range(n):
                page = self.tab_view.get_nth_page(i)
                child = page.get_child() if hasattr(page, 'get_child') else None
                if child is not None and self.terminal_to_connection.get(child) == connection:
                    terms_for_conn.append(child)

            if terms_for_conn:
                # Focus the most recent tab for this connection
                most_recent_term = self.active_terminals.get(connection)
                if most_recent_term and most_recent_term in terms_for_conn:
                    # Use the most recent terminal
                    target_term = most_recent_term
                else:
                    # Fallback to the first tab for this connection
                    target_term = terms_for_conn[0]
                
                page = self.tab_view.get_page(target_term)
                if page is not None:
                    self.tab_view.set_selected_page(page)
                    # Update most-recent mapping
                    self.active_terminals[connection] = target_term
                    # Give focus to the VTE terminal so user can start typing immediately
                    target_term.vte.grab_focus()
                    return

            # No existing tabs for this connection -> open a new one
            self.connect_to_host(connection, force_new=False)
        except Exception as e:
            logger.error(f"Failed to focus most recent tab or open new for {getattr(connection, 'nickname', '')}: {e}")

    def _cycle_connection_tabs_or_open(self, connection: Connection):
        """If there are open tabs for this server, cycle to the next one (wrap).
        Otherwise open a new tab for the server.
        """
        try:
            # Collect current pages in visual/tab order
            terms_for_conn = []
            try:
                n = self.tab_view.get_n_pages()
            except Exception:
                n = 0
            for i in range(n):
                page = self.tab_view.get_nth_page(i)
                child = page.get_child() if hasattr(page, 'get_child') else None
                if child is not None and self.terminal_to_connection.get(child) == connection:
                    terms_for_conn.append(child)

            if terms_for_conn:
                # Determine current index among this connection's tabs
                selected = self.tab_view.get_selected_page()
                current_idx = -1
                if selected is not None:
                    current_child = selected.get_child()
                    for i, t in enumerate(terms_for_conn):
                        if t == current_child:
                            current_idx = i
                            break
                # Compute next index (wrap)
                next_idx = (current_idx + 1) % len(terms_for_conn) if current_idx >= 0 else 0
                next_term = terms_for_conn[next_idx]
                page = self.tab_view.get_page(next_term)
                if page is not None:
                    self.tab_view.set_selected_page(page)
                    # Update most-recent mapping
                    self.active_terminals[connection] = next_term
                    return

            # No existing tabs for this connection -> open a new one
            self.connect_to_host(connection, force_new=False)
        except Exception as e:
            logger.error(f"Failed to cycle or open for {getattr(connection, 'nickname', '')}: {e}")

    def on_connection_selected(self, list_box, row):
        """Handle connection list selection change"""
        has_selection = row is not None
        self.edit_button.set_sensitive(has_selection)
        if hasattr(self, 'copy_key_button'):
            self.copy_key_button.set_sensitive(has_selection)
        if hasattr(self, 'upload_button'):
            self.upload_button.set_sensitive(has_selection)
        if hasattr(self, 'manage_files_button'):
            self.manage_files_button.set_sensitive(has_selection)
        if hasattr(self, 'system_terminal_button') and self.system_terminal_button:
            self.system_terminal_button.set_sensitive(has_selection)
        self.delete_button.set_sensitive(has_selection)

    def on_add_connection_clicked(self, button):
        """Handle add connection button click"""
        self.show_connection_dialog()

    def on_edit_connection_clicked(self, button):
        """Handle edit connection button click"""
        selected_row = self.connection_list.get_selected_row()
        if selected_row:
            if hasattr(selected_row, 'connection'):
                self.show_connection_dialog(selected_row.connection)
            else:
                logger.debug("Cannot edit group row")

    def on_sidebar_toggle(self, button):
        """Handle sidebar toggle button click"""
        try:
            is_visible = button.get_active()
            self._toggle_sidebar_visibility(is_visible)
            
            # Update button icon and tooltip
            if is_visible:
                button.set_icon_name('sidebar-show-symbolic')
                button.set_tooltip_text('Hide Sidebar (F9, Ctrl+B)')
            else:
                button.set_icon_name('sidebar-show-symbolic')
                button.set_tooltip_text('Show Sidebar (F9, Ctrl+B)')
            
            # No need to save state - sidebar always starts visible
                
        except Exception as e:
            logger.error(f"Failed to toggle sidebar: {e}")

    def on_toggle_sidebar_action(self, action, param):
        """Handle sidebar toggle action (for keyboard shortcuts)"""
        try:
            # Get current sidebar visibility
            if HAS_OVERLAY_SPLIT:
                current_visible = self.split_view.get_show_sidebar()
            else:
                sidebar_widget = self.split_view.get_start_child()
                current_visible = sidebar_widget.get_visible() if sidebar_widget else True
            
            # Toggle to opposite state
            new_visible = not current_visible
            
            # Update sidebar visibility
            self._toggle_sidebar_visibility(new_visible)
            
            # Update button state if it exists
            if hasattr(self, 'sidebar_toggle_button'):
                self.sidebar_toggle_button.set_active(new_visible)
            
            # No need to save state - sidebar always starts visible
                
        except Exception as e:
            logger.error(f"Failed to toggle sidebar via action: {e}")

    def _toggle_sidebar_visibility(self, is_visible):
        """Helper method to toggle sidebar visibility"""
        try:
            if HAS_OVERLAY_SPLIT:
                # For Adw.OverlaySplitView
                self.split_view.set_show_sidebar(is_visible)
            else:
                # For Gtk.Paned fallback
                sidebar_widget = self.split_view.get_start_child()
                if sidebar_widget:
                    sidebar_widget.set_visible(is_visible)
        except Exception as e:
            logger.error(f"Failed to toggle sidebar visibility: {e}")



    def on_upload_file_clicked(self, button):
        """Show SCP intro dialog and start upload to selected server."""
        try:
            selected_row = self.connection_list.get_selected_row()
            if not selected_row:
                return
            connection = getattr(selected_row, 'connection', None)
            if not connection:
                return

            intro = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_('Upload files to server'),
                body=_('We will use scp to upload file(s) to the selected server. You will be prompted to choose files and a destination path on the server.')
            )
            intro.add_response('cancel', _('Cancel'))
            intro.add_response('choose', _('Choose files…'))
            intro.set_default_response('choose')
            intro.set_close_response('cancel')

            def _on_intro(dlg, response):
                dlg.close()
                if response != 'choose':
                    return
                # Choose local files
                file_chooser = Gtk.FileChooserDialog(
                    title=_('Select files to upload'),
                    action=Gtk.FileChooserAction.OPEN,
                )
                file_chooser.set_transient_for(self)
                file_chooser.set_modal(True)
                file_chooser.add_button(_('Cancel'), Gtk.ResponseType.CANCEL)
                file_chooser.add_button(_('Open'), Gtk.ResponseType.ACCEPT)
                file_chooser.set_select_multiple(True)
                file_chooser.connect('response', lambda fc, resp: self._on_files_chosen(fc, resp, connection))
                file_chooser.show()

            intro.connect('response', _on_intro)
            intro.present()
        except Exception as e:
            logger.error(f'Upload dialog failed: {e}')

    def on_manage_files_button_clicked(self, button):
        """Handle manage files button click from toolbar"""
        try:
            selected_row = self.connection_list.get_selected_row()
            if not selected_row:
                return
            connection = getattr(selected_row, 'connection', None)
            if not connection:
                return
            
            # Use the same logic as the context menu action
            try:
                # Define error callback for async operation
                def error_callback(error_msg):
                    logger.error(f"Failed to open file manager for {connection.nickname}: {error_msg}")
                    # Show error dialog to user
                    self._show_manage_files_error(connection.nickname, error_msg or "Failed to open file manager")
                
                success, error_msg = open_remote_in_file_manager(
                    user=connection.username,
                    host=connection.host,
                    port=connection.port if connection.port != 22 else None,
                    error_callback=error_callback,
                    parent_window=self
                )
                if success:
                    logger.info(f"Started file manager process for {connection.nickname}")
                else:
                    logger.error(f"Failed to start file manager process for {connection.nickname}: {error_msg}")
                    # Show error dialog to user
                    self._show_manage_files_error(connection.nickname, error_msg or "Failed to start file manager process")
            except Exception as e:
                logger.error(f"Error opening file manager: {e}")
                # Show error dialog to user
                self._show_manage_files_error(connection.nickname, str(e))
        except Exception as e:
            logger.error(f"Manage files button click failed: {e}")

    def on_system_terminal_button_clicked(self, button):
        """Handle system terminal button click from toolbar"""
        try:
            selected_row = self.connection_list.get_selected_row()
            if not selected_row:
                return
            connection = getattr(selected_row, 'connection', None)
            if not connection:
                return
            
            # Use the same logic as the context menu action
            self.open_in_system_terminal(connection)
        except Exception as e:
            logger.error(f"System terminal button click failed: {e}")

    def _show_ssh_copy_id_terminal_using_main_widget(self, connection, ssh_key, force=False):
        """Show a window with header bar and embedded terminal running ssh-copy-id.

        Requirements:
        - Terminal expands horizontally, no borders around it
        - Header bar contains Cancel and Close buttons
        """
        logger.info("Main window: Starting ssh-copy-id terminal window creation")
        logger.debug(f"Main window: Connection details - host: {getattr(connection, 'host', 'unknown')}, "
                    f"username: {getattr(connection, 'username', 'unknown')}, "
                    f"port: {getattr(connection, 'port', 22)}")
        logger.debug(f"Main window: SSH key details - private_path: {getattr(ssh_key, 'private_path', 'unknown')}, "
                    f"public_path: {getattr(ssh_key, 'public_path', 'unknown')}")
        
        try:
            target = f"{connection.username}@{connection.host}" if getattr(connection, 'username', '') else str(connection.host)
            pub_name = os.path.basename(getattr(ssh_key, 'public_path', '') or '')
            body_text = _('This will add your public key to the server\'s ~/.ssh/authorized_keys so future logins can use SSH keys.')
            logger.debug(f"Main window: Target: {target}, public key name: {pub_name}")
            
            dlg = Adw.Window()
            dlg.set_transient_for(self)
            dlg.set_modal(True)
            logger.debug("Main window: Created modal window")
            try:
                dlg.set_title(_('ssh-copy-id'))
            except Exception:
                pass
            try:
                dlg.set_default_size(920, 520)
            except Exception:
                pass

            # Header bar with Cancel
            header = Adw.HeaderBar()
            title_widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            title_label = Gtk.Label(label=_('ssh-copy-id'))
            title_label.set_halign(Gtk.Align.START)
            subtitle_label = Gtk.Label(label=_('Copying {key} to {target}').format(key=pub_name or _('selected key'), target=target))
            subtitle_label.set_halign(Gtk.Align.START)
            try:
                title_label.add_css_class('title-2')
                subtitle_label.add_css_class('dim-label')
            except Exception:
                pass
            title_widget.append(title_label)
            title_widget.append(subtitle_label)
            header.set_title_widget(title_widget)

            # Close button is omitted; window has native close (X)

            # Content: TerminalWidget without connecting spinner/banner
            content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            content_box.set_hexpand(True)
            content_box.set_vexpand(True)
            try:
                content_box.set_margin_top(12)
                content_box.set_margin_bottom(12)
                content_box.set_margin_start(6)
                content_box.set_margin_end(6)
            except Exception:
                pass
            # Optional info text under header bar
            info_lbl = Gtk.Label(label=body_text)
            info_lbl.set_halign(Gtk.Align.START)
            try:
                info_lbl.add_css_class('dim-label')
                info_lbl.set_wrap(True)
            except Exception:
                pass
            content_box.append(info_lbl)

            term_widget = TerminalWidget(connection, self.config, self.connection_manager)
            # Hide connecting overlay and suppress disconnect banner for this non-SSH task
            try:
                term_widget._set_connecting_overlay_visible(False)
                setattr(term_widget, '_suppress_disconnect_banner', True)
                term_widget._set_disconnected_banner_visible(False)
            except Exception:
                pass
            term_widget.set_hexpand(True)
            term_widget.set_vexpand(True)
            # No frame: avoid borders around the terminal
            content_box.append(term_widget)

            # Bottom button area with Close button
            button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            button_box.set_halign(Gtk.Align.END)
            button_box.set_margin_top(12)
            
            cancel_btn = Gtk.Button(label=_('Close'))
            try:
                cancel_btn.add_css_class('suggested-action')
            except Exception:
                pass
            button_box.append(cancel_btn)
            
            content_box.append(button_box)

            # Root container combines header and content
            root_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            root_box.append(header)
            root_box.append(content_box)
            dlg.set_content(root_box)

            def _on_cancel(btn):
                try:
                    if hasattr(term_widget, 'disconnect'):
                        term_widget.disconnect()
                except Exception:
                    pass
                dlg.close()
            cancel_btn.connect('clicked', _on_cancel)
            # No explicit close button; use window close (X)

            # Build ssh-copy-id command with options derived from connection settings
            logger.debug("Main window: Building ssh-copy-id command arguments")
            argv = self._build_ssh_copy_id_argv(connection, ssh_key, force)
            cmdline = ' '.join([GLib.shell_quote(a) for a in argv])
            logger.info("Starting ssh-copy-id: %s", ' '.join(argv))
            logger.info("Full command line: %s", cmdline)
            logger.debug(f"Main window: Command argv: {argv}")
            logger.debug(f"Main window: Shell-quoted command: {cmdline}")

            # Helper to write colored lines into the terminal
            def _feed_colored_line(text: str, color: str):
                colors = {
                    'red': '\x1b[31m',
                    'green': '\x1b[32m',
                    'yellow': '\x1b[33m',
                    'blue': '\x1b[34m',
                }
                prefix = colors.get(color, '')
                try:
                    term_widget.vte.feed(("\r\n" + prefix + text + "\x1b[0m\r\n").encode('utf-8'))
                except Exception:
                    pass

            # Initial info line
            _feed_colored_line(_('Running ssh-copy-id…'), 'yellow')

            # Handle password authentication consistently with terminal connections
            logger.debug("Main window: Setting up authentication environment")
            env = os.environ.copy()
            logger.debug(f"Main window: Environment variables count: {len(env)}")
            
            # Determine auth method and check for saved password
            prefer_password = False
            logger.debug("Main window: Determining authentication preferences")
            try:
                cfg = Config()
                meta = cfg.get_connection_meta(connection.nickname) if hasattr(cfg, 'get_connection_meta') else {}
                logger.debug(f"Main window: Connection metadata: {meta}")
                if isinstance(meta, dict) and 'auth_method' in meta:
                    prefer_password = int(meta.get('auth_method', 0) or 0) == 1
                    logger.debug(f"Main window: Auth method from metadata: {meta.get('auth_method')} -> prefer_password={prefer_password}")
            except Exception as e:
                logger.debug(f"Main window: Failed to get auth method from metadata: {e}")
                try:
                    prefer_password = int(getattr(connection, 'auth_method', 0) or 0) == 1
                    logger.debug(f"Main window: Auth method from connection object: {getattr(connection, 'auth_method', 0)} -> prefer_password={prefer_password}")
                except Exception as e2:
                    logger.debug(f"Main window: Failed to get auth method from connection object: {e2}")
                    prefer_password = False
            
            has_saved_password = bool(self.connection_manager.get_password(connection.host, connection.username))
            logger.debug(f"Main window: Has saved password: {has_saved_password}")
            logger.debug(f"Main window: Authentication setup - prefer_password={prefer_password}, has_saved_password={has_saved_password}")
            
            if prefer_password and has_saved_password:
                # Use sshpass for password authentication
                logger.debug("Main window: Using sshpass for password authentication")
                import shutil
                sshpass_path = None
                
                # Check if sshpass is available and executable
                logger.debug("Main window: Checking for sshpass availability")
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
                    logger.debug("Main window: Setting up sshpass with FIFO")
                    from .ssh_password_exec import _mk_priv_dir, _write_once_fifo
                    import threading
                    
                    # Create private temp directory and FIFO
                    logger.debug("Main window: Creating private temp directory")
                    tmpdir = _mk_priv_dir()
                    fifo = os.path.join(tmpdir, "pw.fifo")
                    logger.debug(f"Main window: FIFO path: {fifo}")
                    os.mkfifo(fifo, 0o600)
                    logger.debug("Main window: FIFO created with permissions 0o600")
                    
                    # Start writer thread that writes the password exactly once
                    saved_password = self.connection_manager.get_password(connection.host, connection.username)
                    logger.debug(f"Main window: Retrieved saved password, length: {len(saved_password) if saved_password else 0}")
                    t = threading.Thread(target=_write_once_fifo, args=(fifo, saved_password), daemon=True)
                    t.start()
                    logger.debug("Main window: Password writer thread started")
                    
                    # Use sshpass with FIFO
                    original_argv = argv.copy()
                    argv = [sshpass_path, "-f", fifo] + argv
                    logger.debug(f"Main window: Modified argv - added sshpass: {argv}")
                    
                    # Important: strip askpass vars so OpenSSH won't try the askpass helper for passwords
                    env.pop("SSH_ASKPASS", None)
                    env.pop("SSH_ASKPASS_REQUIRE", None)
                    logger.debug("Main window: Removed SSH_ASKPASS environment variables")
                    
                    logger.debug("Using sshpass with FIFO for ssh-copy-id password authentication")
                    
                    # Store tmpdir for cleanup (will be cleaned up when process exits)
                    def cleanup_tmpdir():
                        try:
                            import shutil
                            shutil.rmtree(tmpdir, ignore_errors=True)
                        except Exception:
                            pass
                    import atexit
                    atexit.register(cleanup_tmpdir)
                else:
                    # sshpass not available, fallback to askpass
                    logger.debug("Main window: sshpass not available, falling back to askpass")
                    from .askpass_utils import get_ssh_env_with_askpass
                    askpass_env = get_ssh_env_with_askpass()
                    logger.debug(f"Main window: Askpass environment variables: {list(askpass_env.keys())}")
                    env.update(askpass_env)
            elif prefer_password and not has_saved_password:
                # Password auth selected but no saved password - let SSH prompt interactively
                # Don't set any askpass environment variables
                logger.debug("Main window: Password auth selected but no saved password - using interactive prompt")
            else:
                # Use askpass for passphrase prompts (key-based auth)
                logger.debug("Main window: Using askpass for key-based authentication")
                from .askpass_utils import get_ssh_env_with_askpass
                askpass_env = get_ssh_env_with_askpass()
                logger.debug(f"Main window: Askpass environment variables: {list(askpass_env.keys())}")
                env.update(askpass_env)

            # Ensure /app/bin is first in PATH for Flatpak compatibility
            logger.debug("Main window: Setting up PATH for Flatpak compatibility")
            if os.path.exists('/app/bin'):
                current_path = env.get('PATH', '')
                logger.debug(f"Main window: Current PATH: {current_path}")
                if '/app/bin' not in current_path:
                    env['PATH'] = f"/app/bin:{current_path}"
                    logger.debug(f"Main window: Updated PATH: {env['PATH']}")
                else:
                    logger.debug("Main window: /app/bin already in PATH")
            else:
                logger.debug("Main window: /app/bin does not exist, skipping PATH modification")
            
            cmdline = ' '.join([GLib.shell_quote(a) for a in argv])
            logger.info("Starting ssh-copy-id: %s", ' '.join(argv))
            logger.debug(f"Main window: Final command line: {cmdline}")
            envv = [f"{k}={v}" for k, v in env.items()]
            logger.debug(f"Main window: Environment variables count: {len(envv)}")

            try:
                logger.debug("Main window: Spawning ssh-copy-id process in VTE terminal")
                logger.debug(f"Main window: Working directory: {os.path.expanduser('~') or '/'}")
                logger.debug(f"Main window: Command: ['bash', '-lc', '{cmdline}']")
                
                term_widget.vte.spawn_async(
                    Vte.PtyFlags.DEFAULT,
                    os.path.expanduser('~') or '/',
                    ['bash', '-lc', cmdline],
                    envv,  # <— use merged env
                    GLib.SpawnFlags.DEFAULT,
                    None,
                    None,
                    -1,
                    None,
                    None
                )
                logger.debug("Main window: ssh-copy-id process spawned successfully")

                # Show result modal when the command finishes
                def _on_copyid_exited(vte, status):
                    logger.debug(f"Main window: ssh-copy-id process exited with raw status: {status}")
                    # Normalize exit code
                    exit_code = None
                    try:
                        if os.WIFEXITED(status):
                            exit_code = os.WEXITSTATUS(status)
                            logger.debug(f"Main window: Process exited normally, exit code: {exit_code}")
                        else:
                            exit_code = status if 0 <= int(status) < 256 else ((int(status) >> 8) & 0xFF)
                            logger.debug(f"Main window: Process did not exit normally, normalized exit code: {exit_code}")
                    except Exception as e:
                        logger.debug(f"Main window: Error normalizing exit status: {e}")
                        try:
                            exit_code = int(status)
                            logger.debug(f"Main window: Converted status to int: {exit_code}")
                        except Exception as e2:
                            logger.debug(f"Main window: Failed to convert status to int: {e2}")
                            exit_code = status

                    logger.info(f"ssh-copy-id exited with status: {status}, normalized exit_code: {exit_code}")
                    
                    # Simple verification: just check exit code like default ssh-copy-id
                    ok = (exit_code == 0)
                    
                    # Get error details from output if failed
                    error_details = None
                    if not ok:
                        try:
                            content = term_widget.vte.get_text_range(0, 0, -1, -1, None)
                            if content:
                                # Look for common error patterns in the output
                                content_lower = content.lower()
                                if 'permission denied' in content_lower:
                                    error_details = 'Permission denied - check user credentials and server permissions'
                                elif 'connection refused' in content_lower:
                                    error_details = 'Connection refused - check server address and SSH service'
                                elif 'authentication failed' in content_lower:
                                    error_details = 'Authentication failed - check username and password/key'
                                elif 'no such file or directory' in content_lower:
                                    error_details = 'File not found - check if SSH directory exists on server'
                                elif 'operation not permitted' in content_lower:
                                    error_details = 'Operation not permitted - check server permissions'
                                else:
                                    # Extract the last few lines of output for context
                                    lines = content.strip().split('\n')
                                    if lines:
                                        error_details = f"Error details: {lines[-1]}"
                        except Exception as e:
                            logger.debug(f"Main window: Error extracting error details: {e}")
                    
                    if ok:
                        logger.info("ssh-copy-id completed successfully")
                        logger.debug("Main window: ssh-copy-id succeeded, showing success message")
                        _feed_colored_line(_('Public key was installed successfully.'), 'green')
                    else:
                        logger.error(f"ssh-copy-id failed with exit code: {exit_code}")
                        logger.debug(f"Main window: ssh-copy-id failed with exit code {exit_code}")
                        _feed_colored_line(_('Failed to install the public key.'), 'red')
                        if error_details:
                            _feed_colored_line(error_details, 'red')

                    def _present_result_dialog():
                        logger.debug(f"Main window: Presenting result dialog - success: {ok}")
                        msg = Adw.MessageDialog(
                            transient_for=dlg,
                            modal=True,
                            heading=_('Success') if ok else _('Error'),
                            body=(_('Public key copied to {}@{}').format(connection.username, connection.host)
                                  if ok else _('Failed to copy the public key. Check logs for details.')),
                        )
                        msg.add_response('ok', _('OK'))
                        msg.set_default_response('ok')
                        msg.set_close_response('ok')
                        msg.present()
                        logger.debug("Main window: Result dialog presented")
                        return False

                    GLib.idle_add(_present_result_dialog)

                try:
                    term_widget.vte.connect('child-exited', _on_copyid_exited)
                except Exception:
                    pass
            except Exception as e:
                logger.error(f'Failed to spawn ssh-copy-id in TerminalWidget: {e}')
                logger.debug(f'Main window: Exception details: {type(e).__name__}: {str(e)}')
                dlg.close()
                # No fallback method available
                logger.error(f'Terminal ssh-copy-id failed: {e}')
                self._error_dialog(_("SSH Key Copy Error"),
                                  _("Failed to copy SSH key to server."), 
                                  f"Terminal error: {str(e)}\n\nPlease check:\n• Network connectivity\n• SSH server configuration\n• User permissions")
                return

            dlg.present()
            logger.debug("Main window: ssh-copy-id terminal window presented successfully")
        except Exception as e:
            logger.error(f'VTE ssh-copy-id window failed: {e}')
            logger.debug(f'Main window: Exception details: {type(e).__name__}: {str(e)}')
            self._error_dialog(_("SSH Key Copy Error"),
                              _("Failed to create ssh-copy-id terminal window."), 
                              f"Error: {str(e)}\n\nThis could be due to:\n• Missing VTE terminal widget\n• Display/GTK issues\n• System resource limitations")



    def _build_ssh_copy_id_argv(self, connection, ssh_key, force=False):
        """Construct argv for ssh-copy-id honoring saved UI auth preferences."""
        logger.info(f"Building ssh-copy-id argv for key: {getattr(ssh_key, 'public_path', 'unknown')}")
        logger.debug(f"Main window: Building ssh-copy-id command arguments")
        logger.debug(f"Main window: Connection object: {type(connection)}")
        logger.debug(f"Main window: SSH key object: {type(ssh_key)}")
        logger.debug(f"Main window: Force option: {force}")
        logger.info(f"Key object attributes: private_path={getattr(ssh_key, 'private_path', 'unknown')}, public_path={getattr(ssh_key, 'public_path', 'unknown')}")
        
        # Verify the public key file exists
        logger.debug(f"Main window: Checking if public key file exists: {ssh_key.public_path}")
        if not os.path.exists(ssh_key.public_path):
            logger.error(f"Public key file does not exist: {ssh_key.public_path}")
            logger.debug(f"Main window: Public key file missing: {ssh_key.public_path}")
            raise RuntimeError(f"Public key file not found: {ssh_key.public_path}")
        
        logger.debug(f"Main window: Public key file verified: {ssh_key.public_path}")
        argv = ['ssh-copy-id']
        
        # Add force option if enabled
        if force:
            argv.append('-f')
            logger.debug("Main window: Added force option (-f) to ssh-copy-id")
        
        argv.extend(['-i', ssh_key.public_path])
        logger.debug(f"Main window: Base command: {argv}")
        try:
            port = getattr(connection, 'port', 22)
            logger.debug(f"Main window: Connection port: {port}")
            if port and port != 22:
                argv += ['-p', str(connection.port)]
                logger.debug(f"Main window: Added port option: -p {connection.port}")
        except Exception as e:
            logger.debug(f"Main window: Error getting port: {e}")
            pass
        # Honor app SSH settings: strict host key checking / auto-add
        logger.debug("Main window: Loading SSH configuration")
        try:
            cfg = Config()
            ssh_cfg = cfg.get_ssh_config() if hasattr(cfg, 'get_ssh_config') else {}
            logger.debug(f"Main window: SSH config: {ssh_cfg}")
            strict_val = str(ssh_cfg.get('strict_host_key_checking', '') or '').strip()
            auto_add = bool(ssh_cfg.get('auto_add_host_keys', True))
            logger.debug(f"Main window: SSH settings - strict_val='{strict_val}', auto_add={auto_add}")
            if strict_val:
                argv += ['-o', f'StrictHostKeyChecking={strict_val}']
                logger.debug(f"Main window: Added strict host key checking: {strict_val}")
            elif auto_add:
                argv += ['-o', 'StrictHostKeyChecking=accept-new']
                logger.debug("Main window: Added auto-accept new host keys")
        except Exception as e:
            logger.debug(f"Main window: Error loading SSH config: {e}")
            argv += ['-o', 'StrictHostKeyChecking=accept-new']
            logger.debug("Main window: Using default strict host key checking: accept-new")
        # Derive auth prefs from saved config and connection
        logger.debug("Main window: Determining authentication preferences")
        prefer_password = False
        key_mode = 0
        keyfile = getattr(connection, 'keyfile', '') or ''
        logger.debug(f"Main window: Connection keyfile: '{keyfile}'")
        
        try:
            cfg = Config()
            meta = cfg.get_connection_meta(connection.nickname) if hasattr(cfg, 'get_connection_meta') else {}
            logger.debug(f"Main window: Connection metadata: {meta}")
            if isinstance(meta, dict) and 'auth_method' in meta:
                prefer_password = int(meta.get('auth_method', 0) or 0) == 1
                logger.debug(f"Main window: Auth method from metadata: {meta.get('auth_method')} -> prefer_password={prefer_password}")
        except Exception as e:
            logger.debug(f"Main window: Error getting auth method from metadata: {e}")
            try:
                prefer_password = int(getattr(connection, 'auth_method', 0) or 0) == 1
                logger.debug(f"Main window: Auth method from connection object: {getattr(connection, 'auth_method', 0)} -> prefer_password={prefer_password}")
            except Exception as e2:
                logger.debug(f"Main window: Error getting auth method from connection object: {e2}")
                prefer_password = False
        
        try:
            # key_select_mode is saved in ssh config, our connection object should have it post-load
            key_mode = int(getattr(connection, 'key_select_mode', 0) or 0)
            logger.debug(f"Main window: Key select mode: {key_mode}")
        except Exception as e:
            logger.debug(f"Main window: Error getting key select mode: {e}")
            key_mode = 0
        
        # Validate keyfile path
        try:
            keyfile_ok = bool(keyfile) and os.path.isfile(keyfile)
            logger.debug(f"Main window: Keyfile validation - keyfile='{keyfile}', exists={keyfile_ok}")
        except Exception as e:
            logger.debug(f"Main window: Error validating keyfile: {e}")
            keyfile_ok = False

        # Priority: if UI selected a specific key and it exists, use it; otherwise fall back to password prefs/try-all
        logger.debug(f"Main window: Applying authentication options - key_mode={key_mode}, keyfile_ok={keyfile_ok}, prefer_password={prefer_password}")
        
        # For ssh-copy-id, we should NOT add IdentityFile options because:
        # 1. ssh-copy-id should use the same key for authentication that it's copying
        # 2. The -i parameter already specifies which key to copy
        # 3. Adding IdentityFile would cause ssh-copy-id to use a different key for auth
        
        if key_mode == 1 and keyfile_ok:
            # Don't add IdentityFile for ssh-copy-id - it should use the key being copied
            logger.debug(f"Main window: Skipping IdentityFile for ssh-copy-id - using key being copied for authentication")
        else:
            # Only force password when user selected password auth
            if prefer_password:
                argv += ['-o', 'PubkeyAuthentication=no', '-o', 'PreferredAuthentications=password']
                logger.debug("Main window: Added password authentication options - PubkeyAuthentication=no, PreferredAuthentications=password")
        
        # Target
        target = f"{connection.username}@{connection.host}" if getattr(connection, 'username', '') else str(connection.host)
        argv.append(target)
        logger.debug(f"Main window: Added target: {target}")
        logger.debug(f"Main window: Final argv: {argv}")
        return argv

    def _on_files_chosen(self, chooser, response, connection):
        try:
            if response != Gtk.ResponseType.ACCEPT:
                chooser.destroy()
                return
            files = chooser.get_files()
            chooser.destroy()
            if not files:
                return
            # Ask remote destination path
            prompt = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_('Remote destination'),
                body=_('Enter a remote directory (e.g., ~/ or /var/tmp). Files will be uploaded using scp.')
            )
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            dest_row = Adw.EntryRow(title=_('Remote directory'))
            dest_row.set_text('~')
            box.append(dest_row)
            prompt.set_extra_child(box)
            prompt.add_response('cancel', _('Cancel'))
            prompt.add_response('upload', _('Upload'))
            prompt.set_default_response('upload')
            prompt.set_close_response('cancel')

            def _go(d, resp):
                d.close()
                if resp != 'upload':
                    return
                remote_dir = dest_row.get_text().strip() or '~'
                self._start_scp_upload(connection, [f.get_path() for f in files], remote_dir)

            prompt.connect('response', _go)
            prompt.present()
        except Exception as e:
            logger.error(f'File selection failed: {e}')

    def _start_scp_upload(self, connection, local_paths, remote_dir):
        """Run scp using the same terminal window layout as ssh-copy-id."""
        try:
            self._show_scp_upload_terminal_window(connection, local_paths, remote_dir)
        except Exception as e:
            logger.error(f'scp upload failed to start: {e}')

    def _show_scp_upload_terminal_window(self, connection, local_paths, remote_dir):
        try:
            target = f"{connection.username}@{connection.host}"
            info_text = _('We will use scp to upload file(s) to the selected server.')

            dlg = Adw.Window()
            dlg.set_transient_for(self)
            dlg.set_modal(True)
            try:
                dlg.set_title(_('Upload files (scp)'))
            except Exception:
                pass
            try:
                dlg.set_default_size(920, 520)
            except Exception:
                pass

            # Header bar with Cancel
            header = Adw.HeaderBar()
            title_widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            title_label = Gtk.Label(label=_('Upload files (scp)'))
            title_label.set_halign(Gtk.Align.START)
            subtitle_label = Gtk.Label(label=_('Uploading to {target}:{dir}').format(target=target, dir=remote_dir))
            subtitle_label.set_halign(Gtk.Align.START)
            try:
                title_label.add_css_class('title-2')
                subtitle_label.add_css_class('dim-label')
            except Exception:
                pass
            title_widget.append(title_label)
            title_widget.append(subtitle_label)
            header.set_title_widget(title_widget)

            cancel_btn = Gtk.Button(label=_('Cancel'))
            try:
                cancel_btn.add_css_class('flat')
            except Exception:
                pass
            header.pack_start(cancel_btn)

            # Content area
            content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            content_box.set_hexpand(True)
            content_box.set_vexpand(True)
            try:
                content_box.set_margin_top(12)
                content_box.set_margin_bottom(12)
                content_box.set_margin_start(6)
                content_box.set_margin_end(6)
            except Exception:
                pass

            info_lbl = Gtk.Label(label=info_text)
            info_lbl.set_halign(Gtk.Align.START)
            try:
                info_lbl.add_css_class('dim-label')
                info_lbl.set_wrap(True)
            except Exception:
                pass
            content_box.append(info_lbl)

            term_widget = TerminalWidget(connection, self.config, self.connection_manager)
            try:
                term_widget._set_connecting_overlay_visible(False)
                setattr(term_widget, '_suppress_disconnect_banner', True)
                term_widget._set_disconnected_banner_visible(False)
            except Exception:
                pass
            term_widget.set_hexpand(True)
            term_widget.set_vexpand(True)
            content_box.append(term_widget)

            root_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            root_box.append(header)
            root_box.append(content_box)
            dlg.set_content(root_box)

            def _on_cancel(btn):
                # Clean up askpass helper scripts
                try:
                    if hasattr(self, '_scp_askpass_helpers'):
                        for helper_path in self._scp_askpass_helpers:
                            try:
                                os.unlink(helper_path)
                            except Exception:
                                pass
                        self._scp_askpass_helpers.clear()
                except Exception:
                    pass
                
                try:
                    if hasattr(term_widget, 'disconnect'):
                        term_widget.disconnect()
                except Exception:
                    pass
                dlg.close()
            cancel_btn.connect('clicked', _on_cancel)

            # Build and run scp command in the terminal
            argv = self._build_scp_argv(connection, local_paths, remote_dir)

            # Handle environment variables for authentication
            env = os.environ.copy()
            
            # Check if we have stored askpass environment from key passphrase handling
            if hasattr(self, '_scp_askpass_env') and self._scp_askpass_env:
                env.update(self._scp_askpass_env)
                logger.debug(f"SCP: Using askpass environment for key passphrase: {list(self._scp_askpass_env.keys())}")
                # Clear the stored environment after use
                self._scp_askpass_env = {}
                
                # For key-based auth, ensure the key is loaded in SSH agent first
                try:
                    keyfile = getattr(connection, 'keyfile', '') or ''
                    if keyfile and os.path.isfile(keyfile):
                        # Prepare key for connection (add to ssh-agent if needed)
                        if hasattr(self, 'connection_manager') and self.connection_manager:
                            key_prepared = self.connection_manager.prepare_key_for_connection(keyfile)
                            if key_prepared:
                                logger.debug(f"SCP: Key prepared for connection: {keyfile}")
                            else:
                                logger.warning(f"SCP: Failed to prepare key for connection: {keyfile}")
                except Exception as e:
                    logger.warning(f"SCP: Error preparing key for connection: {e}")
            else:
                # Handle password authentication - sshpass is already handled in _build_scp_argv
                # No additional environment setup needed here
                logger.debug("SCP: Password authentication handled by sshpass in command line")

            # Ensure /app/bin is first in PATH for Flatpak compatibility
            if os.path.exists('/app/bin'):
                current_path = env.get('PATH', '')
                if '/app/bin' not in current_path:
                    env['PATH'] = f"/app/bin:{current_path}"
            
            cmdline = ' '.join([GLib.shell_quote(a) for a in argv])
            envv = [f"{k}={v}" for k, v in env.items()]
            logger.debug(f"SCP: Final environment variables: SSH_ASKPASS={env.get('SSH_ASKPASS', 'NOT_SET')}, SSH_ASKPASS_REQUIRE={env.get('SSH_ASKPASS_REQUIRE', 'NOT_SET')}")
            logger.debug(f"SCP: Command line: {cmdline}")

            # Helper to write colored lines
            def _feed_colored_line(text: str, color: str):
                colors = {
                    'red': '\x1b[31m',
                    'green': '\x1b[32m',
                    'yellow': '\x1b[33m',
                    'blue': '\x1b[34m',
                }
                prefix = colors.get(color, '')
                try:
                    term_widget.vte.feed(("\r\n" + prefix + text + "\x1b[0m\r\n").encode('utf-8'))
                except Exception:
                    pass

            _feed_colored_line(_('Starting upload…'), 'yellow')

            try:
                term_widget.vte.spawn_async(
                    Vte.PtyFlags.DEFAULT,
                    os.path.expanduser('~') or '/',
                    ['bash', '-lc', cmdline],
                    envv,  # <— use merged env (ASKPASS + DISPLAY + SSHPILOT_* )
                    GLib.SpawnFlags.DEFAULT,
                    None,
                    None,
                    -1,
                    None,
                    None
                )

                def _on_scp_exited(vte, status):
                    # Normalize exit code
                    exit_code = None
                    try:
                        if os.WIFEXITED(status):
                            exit_code = os.WEXITSTATUS(status)
                        else:
                            exit_code = status if 0 <= int(status) < 256 else ((int(status) >> 8) & 0xFF)
                    except Exception:
                        try:
                            exit_code = int(status)
                        except Exception:
                            exit_code = status
                    ok = (exit_code == 0)
                    if ok:
                        _feed_colored_line(_('Upload finished successfully.'), 'green')
                    else:
                        _feed_colored_line(_('Upload failed. See output above.'), 'red')

                    def _present_result_dialog():
                        # Clean up askpass helper scripts
                        try:
                            if hasattr(self, '_scp_askpass_helpers'):
                                for helper_path in self._scp_askpass_helpers:
                                    try:
                                        os.unlink(helper_path)
                                    except Exception:
                                        pass
                                self._scp_askpass_helpers.clear()
                        except Exception:
                            pass
                        
                        msg = Adw.MessageDialog(
                            transient_for=dlg,
                            modal=True,
                            heading=_('Upload complete') if ok else _('Upload failed'),
                            body=(_('Files uploaded to {target}:{dir}').format(target=target, dir=remote_dir)
                                  if ok else _('scp exited with an error. Please review the log output.')),
                        )
                        msg.add_response('ok', _('OK'))
                        msg.set_default_response('ok')
                        msg.set_close_response('ok')
                        msg.present()
                        return False

                    GLib.idle_add(_present_result_dialog)

                try:
                    term_widget.vte.connect('child-exited', _on_scp_exited)
                except Exception:
                    pass
            except Exception as e:
                logger.error(f'Failed to spawn scp in TerminalWidget: {e}')
                dlg.close()
                # Fallback could be implemented here if needed
                return

            dlg.present()
        except Exception as e:
            logger.error(f'Failed to open scp terminal window: {e}')

    def _build_scp_argv(self, connection, local_paths, remote_dir):
        argv = ['scp', '-v']
        # Port
        try:
            if getattr(connection, 'port', 22) and connection.port != 22:
                argv += ['-P', str(connection.port)]
        except Exception:
            pass
        # Auth/SSH options similar to ssh-copy-id
        try:
            cfg = Config()
            ssh_cfg = cfg.get_ssh_config() if hasattr(cfg, 'get_ssh_config') else {}
            strict_val = str(ssh_cfg.get('strict_host_key_checking', '') or '').strip()
            auto_add = bool(ssh_cfg.get('auto_add_host_keys', True))
            if strict_val:
                argv += ['-o', f'StrictHostKeyChecking={strict_val}']
            elif auto_add:
                argv += ['-o', 'StrictHostKeyChecking=accept-new']
        except Exception:
            argv += ['-o', 'StrictHostKeyChecking=accept-new']
        # Prefer password if selected
        prefer_password = False
        key_mode = 0
        keyfile = getattr(connection, 'keyfile', '') or ''
        try:
            cfg = Config()
            meta = cfg.get_connection_meta(connection.nickname) if hasattr(cfg, 'get_connection_meta') else {}
            if isinstance(meta, dict) and 'auth_method' in meta:
                prefer_password = int(meta.get('auth_method', 0) or 0) == 1
        except Exception:
            try:
                prefer_password = int(getattr(connection, 'auth_method', 0) or 0) == 1
            except Exception:
                prefer_password = False
        try:
            key_mode = int(getattr(connection, 'key_select_mode', 0) or 0)
        except Exception:
            key_mode = 0
        try:
            keyfile_ok = bool(keyfile) and os.path.isfile(keyfile)
        except Exception:
            keyfile_ok = False
        # Handle authentication with saved credentials
        if key_mode == 1 and keyfile_ok:
            argv += ['-i', keyfile, '-o', 'IdentitiesOnly=yes']
            
            # Try to get saved passphrase for the key
            try:
                if hasattr(self, 'connection_manager') and self.connection_manager:
                    saved_passphrase = self.connection_manager.get_key_passphrase(keyfile)
                    if saved_passphrase:
                        # Use the secure askpass script for passphrase authentication
                        # This avoids storing passphrases in plain text temporary files
                        from .askpass_utils import get_ssh_env_with_forced_askpass, get_scp_ssh_options
                        askpass_env = get_ssh_env_with_forced_askpass()
                        # Store for later use in the main execution
                        if not hasattr(self, '_scp_askpass_env'):
                            self._scp_askpass_env = {}
                        self._scp_askpass_env.update(askpass_env)
                        logger.debug(f"SCP: Stored askpass environment for key passphrase: {list(askpass_env.keys())}")
                        
                        # Add SSH options to force public key authentication and prevent password fallback
                        argv += get_scp_ssh_options()
            except Exception as e:
                logger.debug(f"Failed to get saved passphrase for SCP: {e}")
                
        elif prefer_password:
            argv += ['-o', 'PubkeyAuthentication=no', '-o', 'PreferredAuthentications=password']
            
            # Try to get saved password
            try:
                if hasattr(self, 'connection_manager') and self.connection_manager:
                    saved_password = self.connection_manager.get_password(connection.host, connection.username)
                    if saved_password:
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
                            t = threading.Thread(target=_write_once_fifo, args=(fifo, saved_password), daemon=True)
                            t.start()
                            
                            # Use sshpass with FIFO
                            argv = [sshpass_path, "-f", fifo] + argv
                            
                            logger.debug("Using sshpass with FIFO for SCP password authentication")
                            
                            # Store tmpdir for cleanup (will be cleaned up when process exits)
                            def cleanup_tmpdir():
                                try:
                                    import shutil
                                    shutil.rmtree(tmpdir, ignore_errors=True)
                                except Exception:
                                    pass
                            import atexit
                            atexit.register(cleanup_tmpdir)
                        else:
                            # sshpass not available → use askpass env (same pattern as in ssh-copy-id path)
                            from .askpass_utils import get_ssh_env_with_askpass
                            askpass_env = get_ssh_env_with_askpass("force")
                            # Store for later use in the main execution
                            if not hasattr(self, '_scp_askpass_env'):
                                self._scp_askpass_env = {}
                            self._scp_askpass_env.update(askpass_env)
                            logger.debug("SCP: sshpass unavailable, using SSH_ASKPASS fallback")
                    else:
                        # No saved password - will use interactive prompt
                        logger.debug("SCP: Password auth selected but no saved password - using interactive prompt")
            except Exception as e:
                logger.debug(f"Failed to get saved password for SCP: {e}")
        
        # Paths
        for p in local_paths:
            argv.append(p)
        target = f"{connection.username}@{connection.host}" if getattr(connection, 'username', '') else str(connection.host)
        argv.append(f"{target}:{remote_dir}")
        return argv

    def on_delete_connection_clicked(self, button):
        """Handle delete connection button click"""
        selected_row = self.connection_list.get_selected_row()
        if not selected_row:
            return
        
        if not hasattr(selected_row, 'connection'):
            logger.debug("Cannot delete group row")
            return
        
        connection = selected_row.connection
        
        # If host has active connections/tabs, warn about closing them first
        has_active_terms = bool(self.connection_to_terminals.get(connection, []))
        if getattr(connection, 'is_connected', False) or has_active_terms:
            dialog = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_('Remove host?'),
                body=_('Close connections and remove host?')
            )
            dialog.add_response('cancel', _('Cancel'))
            dialog.add_response('close_remove', _('Close and Remove'))
            dialog.set_response_appearance('close_remove', Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response('close')
            dialog.set_close_response('cancel')
        else:
            # Simple delete confirmation when not connected
            dialog = Adw.MessageDialog.new(self, _('Delete Connection?'),
                                         _('Are you sure you want to delete "{}"?').format(connection.nickname))
            dialog.add_response('cancel', _('Cancel'))
            dialog.add_response('delete', _('Delete'))
            dialog.set_response_appearance('delete', Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response('cancel')
            dialog.set_close_response('cancel')

        dialog.connect('response', self.on_delete_connection_response, connection)
        dialog.present()

    def on_delete_connection_response(self, dialog, response, connection):
        """Handle delete connection dialog response"""
        if response == 'delete':
            # Simple deletion when not connected
            self.connection_manager.remove_connection(connection)
        elif response == 'close_remove':
            # Close connections immediately (no extra confirmation), then remove
            try:
                # Disconnect all terminals for this connection
                for term in list(self.connection_to_terminals.get(connection, [])):
                    try:
                        if hasattr(term, 'disconnect'):
                            term.disconnect()
                    except Exception:
                        pass
                # Also disconnect the active terminal if tracked separately
                term = self.active_terminals.get(connection)
                if term and hasattr(term, 'disconnect'):
                    try:
                        term.disconnect()
                    except Exception:
                        pass
            finally:
                # Remove connection without further prompts
                self.connection_manager.remove_connection(connection)

    def _on_tab_close_confirmed(self, dialog, response_id, tab_view, page):
        """Handle response from tab close confirmation dialog"""
        dialog.destroy()
        if response_id == 'close':
            self._close_tab(tab_view, page)
        # If cancelled, do nothing - the tab remains open
    
    def _close_tab(self, tab_view, page):
        """Close the tab and clean up resources"""
        if hasattr(page, 'get_child'):
            child = page.get_child()
            if hasattr(child, 'disconnect'):
                # Get the connection associated with this terminal using reverse map
                connection = self.terminal_to_connection.get(child)
                # Disconnect the terminal
                child.disconnect()
                # Clean up multi-tab tracking maps
                try:
                    if connection is not None:
                        # Remove from list for this connection
                        if connection in self.connection_to_terminals and child in self.connection_to_terminals[connection]:
                            self.connection_to_terminals[connection].remove(child)
                            if not self.connection_to_terminals[connection]:
                                del self.connection_to_terminals[connection]
                        # Update most-recent mapping
                        if connection in self.active_terminals and self.active_terminals[connection] is child:
                            remaining = self.connection_to_terminals.get(connection)
                            if remaining:
                                self.active_terminals[connection] = remaining[-1]
                            else:
                                del self.active_terminals[connection]
                    if child in self.terminal_to_connection:
                        del self.terminal_to_connection[child]
                except Exception:
                    pass
        
        # Close the tab page
        tab_view.close_page(page)
        
        # Update the UI based on the number of remaining tabs
        GLib.idle_add(self._update_ui_after_tab_close)
    
    def on_tab_close(self, tab_view, page):
        """Handle tab close - THE KEY FIX: Never call close_page ourselves"""
        # If we are closing pages programmatically (e.g., after deleting a
        # connection), suppress the confirmation dialog and allow the default
        # close behavior to proceed.
        if getattr(self, '_suppress_close_confirmation', False):
            return False
        # Get the connection for this tab
        connection = None
        terminal = None
        if hasattr(page, 'get_child'):
            child = page.get_child()
            if hasattr(child, 'disconnect'):
                terminal = child
                connection = self.terminal_to_connection.get(child)
        
        if not connection:
            # For non-terminal tabs, allow immediate close
            return False  # Allow the default close behavior
        
        # Check if confirmation is required
        confirm_disconnect = self.config.get_setting('confirm-disconnect', True)
        
        if confirm_disconnect:
            # Store tab view and page as instance variables
            self._pending_close_tab_view = tab_view
            self._pending_close_page = page
            self._pending_close_connection = connection
            self._pending_close_terminal = terminal
            
            # Show confirmation dialog
            dialog = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_("Close connection to {}").format(connection.nickname or connection.host),
                body=_("Are you sure you want to close this connection?")
            )
            dialog.add_response('cancel', _("Cancel"))
            dialog.add_response('close', _("Close"))
            dialog.set_response_appearance('close', Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response('close')
            dialog.set_close_response('cancel')
            
            # Connect to response signal before showing the dialog
            dialog.connect('response', self._on_tab_close_response)
            dialog.present()
            
            # Prevent the default close behavior while we show confirmation
            return True
        else:
            # If no confirmation is needed, just allow the default close behavior.
            # The default handler will close the page, which in turn triggers the
            # terminal disconnection via the page's 'unmap' or 'destroy' signal.
            return False

    def _on_tab_close_response(self, dialog, response_id):
        """Handle the response from the close confirmation dialog."""
        # Retrieve the pending tab info
        tab_view = self._pending_close_tab_view
        page = self._pending_close_page
        terminal = self._pending_close_terminal

        if response_id == 'close':
            # User confirmed, disconnect the terminal. The tab will be removed
            # by the AdwTabView once we finish the close operation.
            if terminal and hasattr(terminal, 'disconnect'):
                terminal.disconnect()
            # Now, tell the tab view to finish closing the page.
            tab_view.close_page_finish(page, True)
            
            # Check if this was the last tab and show welcome screen if needed
            if tab_view.get_n_pages() == 0:
                self.show_welcome_view()
        else:
            # User cancelled, so we reject the close request.
            # This is the critical step that makes the close button work again.
            tab_view.close_page_finish(page, False)

        dialog.destroy()
        # Clear pending state to avoid memory leaks
        self._pending_close_tab_view = None
        self._pending_close_page = None
        self._pending_close_connection = None
        self._pending_close_terminal = None
    
    def on_tab_attached(self, tab_view, page, position):
        """Handle tab attached"""
        pass

    def on_tab_detached(self, tab_view, page, position):
        """Handle tab detached"""
        # Cleanup terminal-to-connection maps when a page is detached
        try:
            if hasattr(page, 'get_child'):
                child = page.get_child()
                if child in self.terminal_to_connection:
                    connection = self.terminal_to_connection.get(child)
                    # Remove reverse map
                    del self.terminal_to_connection[child]
                    # Remove from per-connection list
                    if connection in self.connection_to_terminals and child in self.connection_to_terminals[connection]:
                        self.connection_to_terminals[connection].remove(child)
                        if not self.connection_to_terminals[connection]:
                            del self.connection_to_terminals[connection]
                    # Update most recent mapping if needed
                    if connection in self.active_terminals and self.active_terminals[connection] is child:
                        remaining = self.connection_to_terminals.get(connection)
                        if remaining:
                            self.active_terminals[connection] = remaining[-1]
                        else:
                            del self.active_terminals[connection]
        except Exception:
            pass

        # Show welcome view if no more tabs are left
        if tab_view.get_n_pages() == 0:
            self.show_welcome_view()

    def on_terminal_connected(self, terminal):
        """Handle terminal connection established"""
        # Update the connection's is_connected status
        terminal.connection.is_connected = True
        
        # Update connection row status
        if terminal.connection in self.connection_rows:
            row = self.connection_rows[terminal.connection]
            row.update_status()
            row.queue_draw()  # Force redraw
        
        # Hide reconnecting feedback if visible and reset controlled flag
        GLib.idle_add(self._hide_reconnecting_message)
        self._is_controlled_reconnect = False

        # Log connection event
        if not getattr(self, '_is_controlled_reconnect', False):
            logger.info(f"Terminal connected: {terminal.connection.nickname} ({terminal.connection.username}@{terminal.connection.host})")
        else:
            logger.debug(f"Terminal reconnected after settings update: {terminal.connection.nickname}")

    def on_terminal_disconnected(self, terminal):
        """Handle terminal connection lost"""
        # Update the connection's is_connected status
        terminal.connection.is_connected = False
        
        # Update connection row status
        if terminal.connection in self.connection_rows:
            row = self.connection_rows[terminal.connection]
            row.update_status()
            row.queue_draw()  # Force redraw
            
        logger.info(f"Terminal disconnected: {terminal.connection.nickname} ({terminal.connection.username}@{terminal.connection.host})")
        
        # Do not reset controlled reconnect flag here; it is managed by the
        # reconnection flow (_on_reconnect_response/_reset_controlled_reconnect)

        # Toasts are disabled per user preference; no notification here.
        pass
            
    def on_connection_added(self, manager, connection):
        """Handle new connection added to the connection manager"""
        logger.info(f"New connection added: {connection.nickname}")
        self.rebuild_connection_list()
        
    def on_terminal_title_changed(self, terminal, title):
        """Handle terminal title change"""
        # Update the tab title with the new terminal title
        page = self.tab_view.get_page(terminal)
        if page:
            if title and title != terminal.connection.nickname:
                page.set_title(f"{terminal.connection.nickname} - {title}")
            else:
                page.set_title(terminal.connection.nickname)
                
    def on_connection_removed(self, manager, connection):
        """Handle connection removed from the connection manager"""
        logger.info(f"Connection removed: {connection.nickname}")

        # Remove from UI if it exists
        if connection in self.connection_rows:
            row = self.connection_rows[connection]
            self.connection_list.remove(row)
            del self.connection_rows[connection]
        
        # Remove from group manager
        self.group_manager.connections.pop(connection.nickname, None)
        self.group_manager._save_groups()

        # Close all terminals for this connection and clean up maps
        terminals = list(self.connection_to_terminals.get(connection, []))
        # Suppress confirmation while we programmatically close pages
        self._suppress_close_confirmation = True
        try:
            for term in terminals:
                try:
                    page = self.tab_view.get_page(term)
                    if page:
                        self.tab_view.close_page(page)
                except Exception:
                    pass
                try:
                    if hasattr(term, 'disconnect'):
                        term.disconnect()
                except Exception:
                    pass
                # Remove reverse map entry for each terminal
                try:
                    if term in self.terminal_to_connection:
                        del self.terminal_to_connection[term]
                except Exception:
                    pass
        finally:
            self._suppress_close_confirmation = False
        if connection in self.connection_to_terminals:
            del self.connection_to_terminals[connection]
        if connection in self.active_terminals:
            del self.active_terminals[connection]



    def on_connection_added(self, manager, connection):
        """Handle new connection added"""
        self.rebuild_connection_list()

    def on_connection_removed(self, manager, connection):
        """Handle connection removed (multi-tab aware)"""
        # Remove from UI
        if connection in self.connection_rows:
            row = self.connection_rows[connection]
            self.connection_list.remove(row)
            del self.connection_rows[connection]
        
        # Remove from group manager
        self.group_manager.connections.pop(connection.nickname, None)
        self.group_manager._save_groups()

        # Close all terminals for this connection and clean up maps
        terminals = list(self.connection_to_terminals.get(connection, []))
        # Suppress confirmation while we programmatically close pages
        self._suppress_close_confirmation = True
        try:
            for term in terminals:
                try:
                    page = self.tab_view.get_page(term)
                    if page:
                        self.tab_view.close_page(page)
                except Exception:
                    pass
                try:
                    if hasattr(term, 'disconnect'):
                        term.disconnect()
                except Exception:
                    pass
                # Remove reverse map entry for each terminal
                try:
                    if term in self.terminal_to_connection:
                        del self.terminal_to_connection[term]
                except Exception:
                    pass
        finally:
            self._suppress_close_confirmation = False
        if connection in self.connection_to_terminals:
            del self.connection_to_terminals[connection]
        if connection in self.active_terminals:
            del self.active_terminals[connection]

    def on_connection_status_changed(self, manager, connection, is_connected):
        """Handle connection status change"""
        logger.debug(f"Connection status changed: {connection.nickname} - {'Connected' if is_connected else 'Disconnected'}")
        if connection in self.connection_rows:
            row = self.connection_rows[connection]
            # Force update the connection's is_connected state
            connection.is_connected = is_connected
            # Update the row's status
            row.update_status()
            # Force a redraw of the row
            row.queue_draw()

        # If this was a controlled reconnect and we are now connected, hide feedback
        if is_connected and getattr(self, '_is_controlled_reconnect', False):
            GLib.idle_add(self._hide_reconnecting_message)
            self._is_controlled_reconnect = False

        # Use the same reliable status to control terminal banners
        try:
            for term in self.connection_to_terminals.get(connection, []) or []:
                if hasattr(term, '_set_disconnected_banner_visible'):
                    if is_connected:
                        term._set_disconnected_banner_visible(False)
                    else:
                        # Do not force-show here to avoid duplicate messages; terminals handle showing on failure/loss
                        pass
        except Exception:
            pass

    def on_setting_changed(self, config, key, value):
        """Handle configuration setting change"""
        logger.debug(f"Setting changed: {key} = {value}")
        
        # Apply relevant changes
        if key.startswith('terminal.'):
            # Update terminal themes/fonts
            for terms in self.connection_to_terminals.values():
                for terminal in terms:
                    terminal.apply_theme()

    def on_window_size_changed(self, window, param):
        """Handle window size change"""
        width = self.get_default_size()[0]
        height = self.get_default_size()[1]
        sidebar_width = self._get_sidebar_width()
        
        self.config.save_window_geometry(width, height, sidebar_width)

    def simple_close_handler(self, window):
        """Handle window close - distinguish between tab close and window close"""
        logger.info("")
        
        try:
            # Check if we have any tabs open
            n_pages = self.tab_view.get_n_pages()
            logger.info(f" Number of tabs: {n_pages}")
            
            # If we have tabs, close all tabs first and then quit
            if n_pages > 0:
                logger.info(" CLOSING ALL TABS FIRST")
                # Close all tabs
                while self.tab_view.get_n_pages() > 0:
                    page = self.tab_view.get_nth_page(0)
                    self.tab_view.close_page(page)
            
            # Now quit the application
            logger.info(" QUITTING APPLICATION")
            app = self.get_application()
            if app:
                app.quit()
                
        except Exception as e:
            logger.error(f" ERROR IN WINDOW CLOSE: {e}")
            # Force quit even if there's an error
            app = self.get_application()
            self.show_quit_confirmation_dialog()
            return False  # Don't quit yet, let dialog handle it
        
        # No active connections, safe to quit
        self._do_quit()
        return True  # Safe to quit

    def on_close_request(self, window):
        """Handle window close request - MAIN ENTRY POINT"""
        if self._is_quitting:
            return False  # Already quitting, allow close
            
        # Check for active connections across all tabs
        actually_connected = {}
        for conn, terms in self.connection_to_terminals.items():
            for term in terms:
                if getattr(term, 'is_connected', False):
                    actually_connected.setdefault(conn, []).append(term)
        if actually_connected:
            self.show_quit_confirmation_dialog()
            return True  # Prevent close, let dialog handle it
        
        # No active connections, safe to close
        return False  # Allow close

    def show_quit_confirmation_dialog(self):
        """Show confirmation dialog when quitting with active connections"""
        # Only count terminals that are actually connected across all tabs
        connected_items = []
        for conn, terms in self.connection_to_terminals.items():
            for term in terms:
                if getattr(term, 'is_connected', False):
                    connected_items.append((conn, term))
        active_count = len(connected_items)
        connection_names = [conn.nickname for conn, _ in connected_items]
        
        if active_count == 1:
            message = f"You have 1 active SSH connection to '{connection_names[0]}'."
            detail = "Closing the application will disconnect this connection."
        else:
            message = f"You have {active_count} active SSH connections."
            detail = f"Closing the application will disconnect all connections:\n• " + "\n• ".join(connection_names)
        
        dialog = Adw.AlertDialog()
        dialog.set_heading("Active SSH Connections")
        dialog.set_body(f"{message}\n\n{detail}")
        
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('quit', 'Quit Anyway')
        dialog.set_response_appearance('quit', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response('quit')
        dialog.set_close_response('cancel')
        
        dialog.connect('response', self.on_quit_confirmation_response)
        dialog.present(self)
    
    def on_quit_confirmation_response(self, dialog, response):
        """Handle quit confirmation dialog response"""
        dialog.close()
        
        if response == 'quit':
            # Start cleanup process
            self._cleanup_and_quit()

    def on_open_new_connection_action(self, action, param=None):
        """Open a new tab for the selected connection via context menu."""
        try:
            connection = getattr(self, '_context_menu_connection', None)
            if connection is None:
                # Fallback to selected row if any
                row = self.connection_list.get_selected_row()
                connection = getattr(row, 'connection', None) if row else None
            if connection is None:
                return
            self.connect_to_host(connection, force_new=True)
        except Exception as e:
            logger.error(f"Failed to open new connection tab: {e}")

    def on_open_new_connection_tab_action(self, action, param=None):
        """Open a new tab for the selected connection via global shortcut (Ctrl+Alt+N)."""
        try:
            # Get the currently selected connection
            row = self.connection_list.get_selected_row()
            if row and hasattr(row, 'connection'):
                connection = row.connection
                self.connect_to_host(connection, force_new=True)
            else:
                # If no connection is selected, show a message or fall back to new connection dialog
                logger.debug("No connection selected for Ctrl+Alt+N, opening new connection dialog")
                self.show_connection_dialog()
        except Exception as e:
            logger.error(f"Failed to open new connection tab with Ctrl+Alt+N: {e}")

    def on_manage_files_action(self, action, param=None):
        """Handle manage files action from context menu"""
        if hasattr(self, '_context_menu_connection') and self._context_menu_connection:
            connection = self._context_menu_connection
            try:
                # Define error callback for async operation
                def error_callback(error_msg):
                    logger.error(f"Failed to open file manager for {connection.nickname}: {error_msg}")
                    # Show error dialog to user
                    self._show_manage_files_error(connection.nickname, error_msg or "Failed to open file manager")
                
                success, error_msg = open_remote_in_file_manager(
                    user=connection.username,
                    host=connection.host,
                    port=connection.port if connection.port != 22 else None,
                    error_callback=error_callback,
                    parent_window=self
                )
                if success:
                    logger.info(f"Started file manager process for {connection.nickname}")
                else:
                    logger.error(f"Failed to start file manager process for {connection.nickname}: {error_msg}")
                    # Show error dialog to user
                    self._show_manage_files_error(connection.nickname, error_msg or "Failed to start file manager process")
            except Exception as e:
                logger.error(f"Error opening file manager: {e}")
                # Show error dialog to user
                self._show_manage_files_error(connection.nickname, str(e))

    def on_edit_connection_action(self, action, param=None):
        """Handle edit connection action from context menu"""
        try:
            connection = getattr(self, '_context_menu_connection', None)
            if connection is None:
                # Fallback to selected row if any
                row = self.connection_list.get_selected_row()
                connection = getattr(row, 'connection', None) if row else None
            if connection is None:
                return
            self.show_connection_dialog(connection)
        except Exception as e:
            logger.error(f"Failed to edit connection: {e}")

    def on_delete_connection_action(self, action, param=None):
        """Handle delete connection action from context menu"""
        try:
            connection = getattr(self, '_context_menu_connection', None)
            if connection is None:
                # Fallback to selected row if any
                row = self.connection_list.get_selected_row()
                connection = getattr(row, 'connection', None) if row else None
            if connection is None:
                return
            
            # Use the same logic as the button click handler
            # If host has active connections/tabs, warn about closing them first
            has_active_terms = bool(self.connection_to_terminals.get(connection, []))
            if getattr(connection, 'is_connected', False) or has_active_terms:
                dialog = Adw.MessageDialog(
                    transient_for=self,
                    modal=True,
                    heading=_('Remove host?'),
                    body=_('Close connections and remove host?')
                )
                dialog.add_response('cancel', _('Cancel'))
                dialog.add_response('close_remove', _('Close and Remove'))
                dialog.set_response_appearance('close_remove', Adw.ResponseAppearance.DESTRUCTIVE)
                dialog.set_default_response('close')
                dialog.set_close_response('cancel')
            else:
                # Simple delete confirmation when not connected
                dialog = Adw.MessageDialog.new(self, _('Delete Connection?'),
                                             _('Are you sure you want to delete "{}"?').format(connection.nickname))
                dialog.add_response('cancel', _('Cancel'))
                dialog.add_response('delete', _('Delete'))
                dialog.set_response_appearance('delete', Adw.ResponseAppearance.DESTRUCTIVE)
                dialog.set_default_response('cancel')
                dialog.set_close_response('cancel')

            dialog.connect('response', self.on_delete_connection_response, connection)
            dialog.present()
        except Exception as e:
            logger.error(f"Failed to delete connection: {e}")

    def on_open_in_system_terminal_action(self, action, param=None):
        """Handle open in system terminal action from context menu"""
        try:
            connection = getattr(self, '_context_menu_connection', None)
            if connection is None:
                # Fallback to selected row if any
                row = self.connection_list.get_selected_row()
                connection = getattr(row, 'connection', None) if row else None
            if connection is None:
                return
            
            self.open_in_system_terminal(connection)
        except Exception as e:
            logger.error(f"Failed to open in system terminal: {e}")

    def on_broadcast_command_action(self, action, param=None):
        """Handle broadcast command action - shows dialog to input command"""
        try:
            # Create a custom dialog window instead of using Adw.MessageDialog
            dialog = Gtk.Dialog(
                title=_("Broadcast Command"),
                transient_for=self,
                modal=True,
                destroy_with_parent=True
            )
            
            # Set dialog properties
            dialog.set_default_size(400, 150)
            dialog.set_resizable(False)
            
            # Get the content area
            content_area = dialog.get_content_area()
            content_area.set_margin_start(20)
            content_area.set_margin_end(20)
            content_area.set_margin_top(20)
            content_area.set_margin_bottom(20)
            content_area.set_spacing(12)
            
            # Add label
            label = Gtk.Label(label=_("Enter a command to send to all open SSH terminals:"))
            label.set_wrap(True)
            label.set_xalign(0)
            content_area.append(label)
            
            # Add text entry
            entry = Gtk.Entry()
            entry.set_placeholder_text(_("e.g., ls -la"))
            entry.set_activates_default(True)
            entry.set_hexpand(True)
            content_area.append(entry)
            
            # Add buttons
            dialog.add_button(_('Cancel'), Gtk.ResponseType.CANCEL)
            send_button = dialog.add_button(_('Send'), Gtk.ResponseType.OK)
            send_button.get_style_context().add_class('suggested-action')
            
            # Set default button
            dialog.set_default_response(Gtk.ResponseType.OK)
            
            # Connect to response signal
            def on_response(dialog, response):
                if response == Gtk.ResponseType.OK:
                    command = entry.get_text().strip()
                    if command:
                        sent_count, failed_count = self.broadcast_command(command)
                        
                        # Show result dialog
                        result_dialog = Adw.MessageDialog(
                            transient_for=self,
                            modal=True,
                            heading=_("Command Sent"),
                            body=_("Command sent to {} SSH terminals. {} failed.").format(sent_count, failed_count)
                        )
                        result_dialog.add_response('ok', _('OK'))
                        result_dialog.present()
                    else:
                        # Show error for empty command
                        error_dialog = Adw.MessageDialog(
                            transient_for=self,
                            modal=True,
                            heading=_("Error"),
                            body=_("Please enter a command to send.")
                        )
                        error_dialog.add_response('ok', _('OK'))
                        error_dialog.present()
                dialog.destroy()
            
            dialog.connect('response', on_response)
            
            # Show the dialog
            dialog.present()
            
            # Focus the entry after the dialog is shown
            def focus_entry():
                entry.grab_focus()
                return False
            
            GLib.idle_add(focus_entry)
            
        except Exception as e:
            logger.error(f"Failed to show broadcast command dialog: {e}")
            # Show error dialog
            try:
                error_dialog = Adw.MessageDialog(
                    transient_for=self,
                    modal=True,
                    heading=_("Error"),
                    body=_("Failed to open broadcast command dialog: {}").format(str(e))
                )
                error_dialog.add_response('ok', _('OK'))
                error_dialog.present()
            except Exception:
                pass
    
    def on_create_group_action(self, action, param=None):
        """Handle create group action"""
        try:
            # Create dialog for group name input
            dialog = Gtk.Dialog(
                title=_("Create New Group"),
                transient_for=self,
                modal=True,
                destroy_with_parent=True
            )
            
            dialog.set_default_size(400, 150)
            dialog.set_resizable(False)
            
            content_area = dialog.get_content_area()
            content_area.set_margin_start(20)
            content_area.set_margin_end(20)
            content_area.set_margin_top(20)
            content_area.set_margin_bottom(20)
            content_area.set_spacing(12)
            
            # Add label
            label = Gtk.Label(label=_("Enter a name for the new group:"))
            label.set_wrap(True)
            label.set_xalign(0)
            content_area.append(label)
            
            # Add text entry
            entry = Gtk.Entry()
            entry.set_placeholder_text(_("e.g., Production Servers"))
            entry.set_activates_default(True)
            entry.set_hexpand(True)
            content_area.append(entry)
            
            # Add buttons
            dialog.add_button(_('Cancel'), Gtk.ResponseType.CANCEL)
            create_button = dialog.add_button(_('Create'), Gtk.ResponseType.OK)
            create_button.get_style_context().add_class('suggested-action')
            
            dialog.set_default_response(Gtk.ResponseType.OK)
            
            def on_response(dialog, response):
                if response == Gtk.ResponseType.OK:
                    group_name = entry.get_text().strip()
                    if group_name:
                        self.group_manager.create_group(group_name)
                        self.rebuild_connection_list()
                    else:
                        # Show error for empty name
                        error_dialog = Adw.MessageDialog(
                            transient_for=self,
                            modal=True,
                            heading=_("Error"),
                            body=_("Please enter a group name.")
                        )
                        error_dialog.add_response('ok', _('OK'))
                        error_dialog.present()
                dialog.destroy()
            
            dialog.connect('response', on_response)
            dialog.present()
            
            def focus_entry():
                entry.grab_focus()
                return False
            
            GLib.idle_add(focus_entry)
            
        except Exception as e:
            logger.error(f"Failed to show create group dialog: {e}")
    
    def on_edit_group_action(self, action, param=None):
        """Handle edit group action"""
        try:
            logger.debug("Edit group action triggered")
            # Get the group row from context menu or selected row
            selected_row = getattr(self, '_context_menu_group_row', None)
            if not selected_row:
                selected_row = self.connection_list.get_selected_row()
            logger.debug(f"Selected row: {selected_row}")
            if not selected_row:
                logger.debug("No selected row")
                return
            if not hasattr(selected_row, 'group_id'):
                logger.debug("Selected row is not a group row")
                return
            
            group_id = selected_row.group_id
            logger.debug(f"Group ID: {group_id}")
            group_info = self.group_manager.groups.get(group_id)
            if not group_info:
                logger.debug(f"Group info not found for ID: {group_id}")
                return
            
            # Create dialog for group name editing
            dialog = Gtk.Dialog(
                title=_("Edit Group"),
                transient_for=self,
                modal=True,
                destroy_with_parent=True
            )
            
            dialog.set_default_size(400, 150)
            dialog.set_resizable(False)
            
            content_area = dialog.get_content_area()
            content_area.set_margin_start(20)
            content_area.set_margin_end(20)
            content_area.set_margin_top(20)
            content_area.set_margin_bottom(20)
            content_area.set_spacing(12)
            
            # Add label
            label = Gtk.Label(label=_("Enter a new name for the group:"))
            label.set_wrap(True)
            label.set_xalign(0)
            content_area.append(label)
            
            # Add text entry
            entry = Gtk.Entry()
            entry.set_text(group_info['name'])
            entry.set_activates_default(True)
            entry.set_hexpand(True)
            content_area.append(entry)
            
            # Add buttons
            dialog.add_button(_('Cancel'), Gtk.ResponseType.CANCEL)
            save_button = dialog.add_button(_('Save'), Gtk.ResponseType.OK)
            save_button.get_style_context().add_class('suggested-action')
            
            dialog.set_default_response(Gtk.ResponseType.OK)
            
            def on_response(dialog, response):
                if response == Gtk.ResponseType.OK:
                    new_name = entry.get_text().strip()
                    if new_name:
                        group_info['name'] = new_name
                        self.group_manager._save_groups()
                        self.rebuild_connection_list()
                    else:
                        # Show error for empty name
                        error_dialog = Adw.MessageDialog(
                            transient_for=self,
                            modal=True,
                            heading=_("Error"),
                            body=_("Please enter a group name.")
                        )
                        error_dialog.add_response('ok', _('OK'))
                        error_dialog.present()
                dialog.destroy()
            
            dialog.connect('response', on_response)
            dialog.present()
            
            def focus_entry():
                entry.grab_focus()
                entry.select_region(0, -1)
                return False
            
            GLib.idle_add(focus_entry)
            
        except Exception as e:
            logger.error(f"Failed to show edit group dialog: {e}")
    
    def on_delete_group_action(self, action, param=None):
        """Handle delete group action"""
        try:
            # Get the group row from context menu or selected row
            selected_row = getattr(self, '_context_menu_group_row', None)
            if not selected_row:
                selected_row = self.connection_list.get_selected_row()
            if not selected_row or not hasattr(selected_row, 'group_id'):
                return
            
            group_id = selected_row.group_id
            group_info = self.group_manager.groups.get(group_id)
            if not group_info:
                return
            
            # Show confirmation dialog
            dialog = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_("Delete Group"),
                body=_("Are you sure you want to delete the group '{}'?\n\nThis will move all connections in this group to the parent group or make them ungrouped.").format(group_info['name'])
            )
            
            dialog.add_response('cancel', _('Cancel'))
            dialog.add_response('delete', _('Delete'))
            dialog.set_response_appearance('delete', Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response('cancel')
            
            def on_response(dialog, response):
                if response == 'delete':
                    self.group_manager.delete_group(group_id)
                    self.rebuild_connection_list()
                dialog.destroy()
            
            dialog.connect('response', on_response)
            dialog.present()
            
        except Exception as e:
            logger.error(f"Failed to show delete group dialog: {e}")
    
    def on_move_to_ungrouped_action(self, action, param=None):
        """Handle move to ungrouped action"""
        try:
            # Get the connection from context menu or selected row
            selected_row = getattr(self, '_context_menu_connection', None)
            if not selected_row:
                selected_row = self.connection_list.get_selected_row()
                if selected_row and hasattr(selected_row, 'connection'):
                    selected_row = selected_row.connection
            
            if not selected_row:
                return
            
            connection_nickname = selected_row.nickname if hasattr(selected_row, 'nickname') else selected_row
            
            # Move to ungrouped (None group)
            self.group_manager.move_connection(connection_nickname, None)
            self.rebuild_connection_list()
            
        except Exception as e:
            logger.error(f"Failed to move connection to ungrouped: {e}")
    
    def on_move_to_group_action(self, action, param=None):
        """Handle move to group action"""
        try:
            # Get the connection from context menu or selected row
            selected_row = getattr(self, '_context_menu_connection', None)
            if not selected_row:
                selected_row = self.connection_list.get_selected_row()
                if selected_row and hasattr(selected_row, 'connection'):
                    selected_row = selected_row.connection
            
            if not selected_row:
                return
            
            connection_nickname = selected_row.nickname if hasattr(selected_row, 'nickname') else selected_row
            
            # Get available groups
            available_groups = self.get_available_groups()
            if not available_groups:
                return
            
            # Show group selection dialog
            dialog = Gtk.Dialog(
                title=_("Move to Group"),
                transient_for=self,
                modal=True,
                destroy_with_parent=True
            )
            
            dialog.set_default_size(400, 300)
            dialog.set_resizable(False)
            
            content_area = dialog.get_content_area()
            content_area.set_margin_start(20)
            content_area.set_margin_end(20)
            content_area.set_margin_top(20)
            content_area.set_margin_bottom(20)
            content_area.set_spacing(12)
            
            # Add label
            label = Gtk.Label(label=_("Select a group to move the connection to:"))
            label.set_wrap(True)
            label.set_xalign(0)
            content_area.append(label)
            
            # Add list box for groups
            listbox = Gtk.ListBox()
            listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
            listbox.set_vexpand(True)
            
            # Add groups to list
            selected_group_id = None
            for group in available_groups:
                row = Gtk.ListBoxRow()
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
                
                # Add group icon
                icon = Gtk.Image.new_from_icon_name('folder-symbolic')
                icon.set_pixel_size(16)
                box.append(icon)
                
                # Add group name
                label = Gtk.Label(label=group['name'])
                label.set_xalign(0)
                label.set_hexpand(True)
                box.append(label)
                
                row.set_child(box)
                row.group_id = group['id']
                listbox.append(row)
            
            content_area.append(listbox)
            
            # Add buttons
            dialog.add_button(_('Cancel'), Gtk.ResponseType.CANCEL)
            move_button = dialog.add_button(_('Move'), Gtk.ResponseType.OK)
            move_button.get_style_context().add_class('suggested-action')
            
            dialog.set_default_response(Gtk.ResponseType.OK)
            
            def on_response(dialog, response):
                if response == Gtk.ResponseType.OK:
                    selected_row = listbox.get_selected_row()
                    if selected_row:
                        target_group_id = selected_row.group_id
                        self.group_manager.move_connection(connection_nickname, target_group_id)
                        self.rebuild_connection_list()
                dialog.destroy()
            
            dialog.connect('response', on_response)
            dialog.present()
            
        except Exception as e:
            logger.error(f"Failed to show move to group dialog: {e}")
    
    def move_connection_to_group(self, connection_nickname: str, target_group_id: str = None):
        """Move a connection to a specific group"""
        try:
            self.group_manager.move_connection(connection_nickname, target_group_id)
            self.rebuild_connection_list()
        except Exception as e:
            logger.error(f"Failed to move connection {connection_nickname} to group: {e}")
    
    def get_available_groups(self) -> List[Dict]:
        """Get list of available groups for selection"""
        return self.group_manager.get_group_hierarchy()

    def open_in_system_terminal(self, connection):
        """Open the connection in the system's default terminal"""
        try:
            # Build the SSH command
            port_text = f" -p {connection.port}" if hasattr(connection, 'port') and connection.port != 22 else ""
            ssh_command = f"ssh{port_text} {connection.username}@{connection.host}"
            
            # Check if user prefers external terminal
            use_external = self.config.get_setting('use-external-terminal', False)
            
            if use_external:
                # Use user's preferred external terminal
                terminal_command = self._get_user_preferred_terminal()
            else:
                # Use built-in terminal (fallback to system default)
                terminal_command = self._get_default_terminal_command()
            
            if not terminal_command:
                # Fallback to common terminals
                common_terminals = [
                    'gnome-terminal', 'konsole', 'xterm', 'alacritty', 
                    'kitty', 'terminator', 'tilix', 'xfce4-terminal'
                ]
                
                for term in common_terminals:
                    try:
                        result = subprocess.run(['which', term], capture_output=True, text=True, timeout=2)
                        if result.returncode == 0:
                            terminal_command = term
                            break
                    except Exception:
                        continue
            
            if not terminal_command:
                # Last resort: try xdg-terminal
                try:
                    result = subprocess.run(['which', 'xdg-terminal'], capture_output=True, text=True, timeout=2)
                    if result.returncode == 0:
                        terminal_command = 'xdg-terminal'
                except Exception:
                    pass
            
            if not terminal_command:
                # Show error dialog
                self._show_terminal_error_dialog()
                return
            
            # Launch the terminal with SSH command
            if terminal_command in ['gnome-terminal', 'tilix', 'xfce4-terminal']:
                # These terminals use -- to separate options from command
                cmd = [terminal_command, '--', 'bash', '-c', f'{ssh_command}; exec bash']
            elif terminal_command in ['konsole', 'terminator']:
                # These terminals use -e for command execution
                cmd = [terminal_command, '-e', f'bash -c "{ssh_command}; exec bash"']
            elif terminal_command in ['alacritty', 'kitty']:
                # These terminals use -e for command execution
                cmd = [terminal_command, '-e', 'bash', '-c', f'{ssh_command}; exec bash']
            elif terminal_command == 'xterm':
                # xterm uses -e for command execution
                cmd = [terminal_command, '-e', f'bash -c "{ssh_command}; exec bash"']
            elif terminal_command == 'xdg-terminal':
                # xdg-terminal opens the default terminal
                cmd = [terminal_command, ssh_command]
            else:
                # Generic fallback
                cmd = [terminal_command, ssh_command]
            
            logger.info(f"Launching system terminal: {' '.join(cmd)}")
            subprocess.Popen(cmd, start_new_session=True)
            
        except Exception as e:
            logger.error(f"Failed to open system terminal: {e}")
            self._show_terminal_error_dialog()

    def _open_connection_in_external_terminal(self, connection):
        """Open the connection in the user's preferred external terminal"""
        try:
            # Build the SSH command
            port_text = f" -p {connection.port}" if hasattr(connection, 'port') and connection.port != 22 else ""
            ssh_command = f"ssh{port_text} {connection.username}@{connection.host}"
            
            # Get user's preferred terminal
            terminal_command = self._get_user_preferred_terminal()
            
            if not terminal_command:
                # Fallback to default terminal
                terminal_command = self._get_default_terminal_command()
            
            if not terminal_command:
                # Show error dialog
                self._show_terminal_error_dialog()
                return
            
            # Launch the terminal with SSH command
            if terminal_command in ['gnome-terminal', 'tilix', 'xfce4-terminal']:
                # These terminals use -- to separate options from command
                cmd = [terminal_command, '--', 'bash', '-c', f'{ssh_command}; exec bash']
            elif terminal_command in ['konsole', 'terminator']:
                # These terminals use -e for command execution
                cmd = [terminal_command, '-e', f'bash -c "{ssh_command}; exec bash"']
            elif terminal_command in ['alacritty', 'kitty']:
                # These terminals use -e for command execution
                cmd = [terminal_command, '-e', 'bash', '-c', f'{ssh_command}; exec bash']
            elif terminal_command == 'xterm':
                # xterm uses -e for command execution
                cmd = [terminal_command, '-e', f'bash -c "{ssh_command}; exec bash"']
            elif terminal_command == 'xdg-terminal':
                # xdg-terminal opens the default terminal
                cmd = [terminal_command, ssh_command]
            else:
                # Generic fallback
                cmd = [terminal_command, ssh_command]
            
            logger.info(f"Opening connection in external terminal: {' '.join(cmd)}")
            subprocess.Popen(cmd, start_new_session=True)
            
        except Exception as e:
            logger.error(f"Failed to open connection in external terminal: {e}")
            self._show_terminal_error_dialog()

    def _get_default_terminal_command(self):
        """Get the default terminal command from desktop environment"""
        try:
            # Check for desktop-specific terminals
            desktop = os.environ.get('XDG_CURRENT_DESKTOP', '').lower()
            
            if 'gnome' in desktop:
                return 'gnome-terminal'
            elif 'kde' in desktop or 'plasma' in desktop:
                return 'konsole'
            elif 'xfce' in desktop:
                return 'xfce4-terminal'
            elif 'cinnamon' in desktop:
                return 'gnome-terminal'  # Cinnamon uses gnome-terminal
            elif 'mate' in desktop:
                return 'mate-terminal'
            elif 'lxqt' in desktop:
                return 'qterminal'
            elif 'lxde' in desktop:
                return 'lxterminal'
            
            # Check for common terminals in PATH
            common_terminals = [
                'gnome-terminal', 'konsole', 'xfce4-terminal', 'alacritty', 
                'kitty', 'terminator', 'tilix'
            ]
            
            for term in common_terminals:
                try:
                    result = subprocess.run(['which', term], capture_output=True, text=True, timeout=2)
                    if result.returncode == 0:
                        return term
                except Exception:
                    continue
            
            return None
            
        except Exception as e:
            logger.error(f"Failed to get default terminal: {e}")
            return None
    
    def _get_user_preferred_terminal(self):
        """Get the user's preferred terminal from settings"""
        try:
            # Get the user's preferred terminal
            preferred_terminal = self.config.get_setting('external-terminal', 'gnome-terminal')
            
            if preferred_terminal == 'custom':
                # Use custom path
                custom_path = self.config.get_setting('custom-terminal-path', '')
                if custom_path and self._is_valid_unix_path(custom_path):
                    return custom_path
                else:
                    logger.warning("Custom terminal path is invalid or not set, falling back to built-in terminal")
                    return None
            
            # Check if the preferred terminal is available
            try:
                result = subprocess.run(['which', preferred_terminal], capture_output=True, text=True, timeout=2)
                if result.returncode == 0:
                    return preferred_terminal
                else:
                    logger.warning(f"Preferred terminal '{preferred_terminal}' not found, falling back to built-in terminal")
                    return None
            except Exception as e:
                logger.error(f"Failed to check preferred terminal '{preferred_terminal}': {e}")
                return None
                
        except Exception as e:
            logger.error(f"Failed to get user preferred terminal: {e}")
            return None

    def _show_terminal_error_dialog(self):
        """Show error dialog when no terminal is found"""
        try:
            dialog = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_("No Terminal Found"),
                body=_("Could not find a suitable terminal application. Please install a terminal like gnome-terminal, konsole, or xterm.")
            )
            
            dialog.add_response("ok", _("OK"))
            dialog.set_default_response("ok")
            dialog.set_close_response("ok")
            dialog.present()
            
        except Exception as e:
            logger.error(f"Failed to show terminal error dialog: {e}")

    def _show_manage_files_error(self, connection_name: str, error_message: str):
        """Show error dialog for manage files failure"""
        try:
            # Determine error type for appropriate messaging
            is_ssh_error = "ssh connection" in error_message.lower() or "connection failed" in error_message.lower()
            is_timeout_error = "timeout" in error_message.lower()
            
            if is_ssh_error or is_timeout_error:
                heading = _("SSH Connection Failed")
                body = _("Could not establish SSH connection to the server. Please check:")
                
                suggestions = [
                    _("• Server is running and accessible"),
                    _("• SSH service is enabled on the server"),
                    _("• Firewall allows SSH connections"),
                    _("• Your SSH keys or credentials are correct"),
                    _("• Network connectivity to the server")
                ]
            else:
                heading = _("File Manager Error")
                body = _("Failed to open file manager for remote server.")
                suggestions = [
                    _("• Try again in a moment"),
                    _("• Check if the server is accessible"),
                    _("• Ensure you have proper permissions")
                ]
            
            # Create suggestions box
            suggestions_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            suggestions_box.set_margin_top(12)
            
            for suggestion in suggestions:
                label = Gtk.Label(label=suggestion)
                label.set_halign(Gtk.Align.START)
                label.set_wrap(True)
                suggestions_box.append(label)
            
            msg = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=heading,
                body=body
            )
            msg.set_extra_child(suggestions_box)
            
            # Add technical details if available
            if error_message and error_message.strip():
                detail_label = Gtk.Label(label=error_message)
                detail_label.add_css_class("dim-label")
                detail_label.set_wrap(True)
                detail_label.set_margin_top(8)
                suggestions_box.append(detail_label)
            
            msg.add_response("ok", _("OK"))
            msg.set_default_response("ok")
            msg.set_close_response("ok")
            msg.present()
            
        except Exception as e:
            logger.error(f"Failed to show manage files error dialog: {e}")

    def _cleanup_and_quit(self):
        """Clean up all connections and quit - SIMPLIFIED VERSION"""
        if self._is_quitting:
            logger.debug("Already quitting, ignoring duplicate request")
            return
                
        logger.info("Starting cleanup before quit...")
        self._is_quitting = True
        
        # Get list of all terminals to disconnect
        connections_to_disconnect = []
        for conn, terms in self.connection_to_terminals.items():
            for term in terms:
                connections_to_disconnect.append((conn, term))
        
        if not connections_to_disconnect:
            # No connections to clean up, quit immediately
            self._do_quit()
            return
        
        # Show progress dialog and perform cleanup on idle so the dialog is visible immediately
        total = len(connections_to_disconnect)
        self._show_cleanup_progress(total)
        # Schedule cleanup to run after the dialog has a chance to render
        GLib.idle_add(self._perform_cleanup_and_quit, connections_to_disconnect, priority=GLib.PRIORITY_DEFAULT_IDLE)
        # Force-quit watchdog (last resort)
        try:
            GLib.timeout_add_seconds(5, self._do_quit)
        except Exception:
            pass

    def _perform_cleanup_and_quit(self, connections_to_disconnect):
        """Disconnect terminals with UI progress, then quit. Runs on idle."""
        try:
            total = len(connections_to_disconnect)
            for index, (connection, terminal) in enumerate(connections_to_disconnect, start=1):
                try:
                    logger.debug(f"Disconnecting {connection.nickname} ({index}/{total})")
                    # Always try to cancel any pending SSH spawn quickly first
                    if hasattr(terminal, 'process_pid') and terminal.process_pid:
                        try:
                            import os, signal
                            os.kill(terminal.process_pid, signal.SIGTERM)
                        except Exception:
                            pass
                    # Skip normal disconnect if terminal not connected to avoid hangs
                    if hasattr(terminal, 'is_connected') and not terminal.is_connected:
                        logger.debug("Terminal not connected; skipped disconnect")
                    else:
                        self._disconnect_terminal_safely(terminal)
                finally:
                    # Update progress even if a disconnect fails
                    self._update_cleanup_progress(index, total)
                    # Yield to main loop to keep UI responsive
                    GLib.MainContext.default().iteration(False)
        except Exception as e:
            logger.error(f"Cleanup during quit encountered an error: {e}")
        finally:
            # Final sweep of any lingering processes (but skip terminal cleanup since we already did that)
            try:
                from .terminal import SSHProcessManager
                # Only clean up processes, not terminals
                process_manager = SSHProcessManager()
                with process_manager.lock:
                    # Make a copy of PIDs to avoid modifying the dict during iteration
                    pids = list(process_manager.processes.keys())
                    for pid in pids:
                        process_manager._terminate_process_by_pid(pid)
                    # Clear all tracked processes
                    process_manager.processes.clear()
                    # Clear terminal references
                    process_manager.terminals.clear()
            except Exception as e:
                logger.debug(f"Final SSH cleanup failed: {e}")
            # Clear active terminals and hide progress
            self.active_terminals.clear()
            self._hide_cleanup_progress()
            # Quit on next idle to flush UI updates
            GLib.idle_add(self._do_quit)
        return False  # Do not repeat

    def _show_cleanup_progress(self, total_connections):
        """Show cleanup progress dialog"""
        self._progress_dialog = Gtk.Window()
        self._progress_dialog.set_title("Closing Connections")
        self._progress_dialog.set_transient_for(self)
        self._progress_dialog.set_modal(True)
        self._progress_dialog.set_default_size(350, 120)
        self._progress_dialog.set_resizable(False)
        
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(20)
        box.set_margin_bottom(20)
        box.set_margin_start(20)
        box.set_margin_end(20)
        
        # Progress bar
        self._progress_bar = Gtk.ProgressBar()
        self._progress_bar.set_fraction(0)
        box.append(self._progress_bar)
        
        # Status label
        self._progress_label = Gtk.Label()
        self._progress_label.set_text(f"Closing {total_connections} connection(s)...")
        box.append(self._progress_label)
        
        self._progress_dialog.set_child(box)
        self._progress_dialog.present()

    def _update_cleanup_progress(self, completed, total):
        """Update cleanup progress"""
        if hasattr(self, '_progress_bar') and self._progress_bar:
            fraction = completed / total if total > 0 else 1.0
            self._progress_bar.set_fraction(fraction)
            
        if hasattr(self, '_progress_label') and self._progress_label:
            self._progress_label.set_text(f"Closed {completed} of {total} connection(s)...")

    def _hide_cleanup_progress(self):
        """Hide cleanup progress dialog"""
        if hasattr(self, '_progress_dialog') and self._progress_dialog:
            try:
                self._progress_dialog.close()
                self._progress_dialog = None
                self._progress_bar = None
                self._progress_label = None
            except Exception as e:
                logger.debug(f"Error closing progress dialog: {e}")

    def _show_reconnecting_message(self, connection):
        """Show a small modal indicating reconnection is in progress"""
        try:
            # Avoid duplicate dialogs
            if hasattr(self, '_reconnect_dialog') and self._reconnect_dialog:
                return

            self._reconnect_dialog = Gtk.Window()
            self._reconnect_dialog.set_title(_("Reconnecting"))
            self._reconnect_dialog.set_transient_for(self)
            self._reconnect_dialog.set_modal(True)
            self._reconnect_dialog.set_default_size(320, 100)
            self._reconnect_dialog.set_resizable(False)

            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            box.set_margin_top(16)
            box.set_margin_bottom(16)
            box.set_margin_start(16)
            box.set_margin_end(16)

            spinner = Gtk.Spinner()
            spinner.set_hexpand(False)
            spinner.set_vexpand(False)
            spinner.start()
            box.append(spinner)

            label = Gtk.Label()
            label.set_text(_("Reconnecting to {}...").format(getattr(connection, "nickname", "")))
            label.set_halign(Gtk.Align.START)
            label.set_hexpand(True)
            box.append(label)

            self._reconnect_spinner = spinner
            self._reconnect_label = label
            self._reconnect_dialog.set_child(box)
            self._reconnect_dialog.present()
        except Exception as e:
            logger.debug(f"Failed to show reconnecting message: {e}")

    def _hide_reconnecting_message(self):
        """Hide the reconnection progress dialog if shown"""
        try:
            if hasattr(self, '_reconnect_dialog') and self._reconnect_dialog:
                self._reconnect_dialog.close()
            self._reconnect_dialog = None
            self._reconnect_spinner = None
            self._reconnect_label = None
        except Exception as e:
            logger.debug(f"Failed to hide reconnecting message: {e}")

    def _disconnect_terminal_safely(self, terminal):
        """Safely disconnect a terminal"""
        try:
            # Try multiple disconnect methods in order of preference
            if hasattr(terminal, 'disconnect'):
                terminal.disconnect()
            elif hasattr(terminal, 'close_connection'):
                terminal.close_connection()
            elif hasattr(terminal, 'close'):
                terminal.close()
                
            # Force any remaining processes to close
            if hasattr(terminal, 'force_close'):
                terminal.force_close()
                
        except Exception as e:
            logger.error(f"Error disconnecting terminal: {e}")

    def _do_quit(self):
        """Actually quit the application - FINAL STEP"""
        try:
            logger.info("Quitting application")
            
            # Save window geometry
            self._save_window_state()
            
            # Get the application and quit
            app = self.get_application()
            if app:
                app.quit()
            else:
                # Fallback: close the window directly
                self.close()
                
        except Exception as e:
            logger.error(f"Error during final quit: {e}")
            # Force exit as last resort
            import sys
            sys.exit(0)
        
        return False  # Don't repeat timeout

    def _save_window_state(self):
        """Save window state before quitting"""
        try:
            width, height = self.get_default_size()
            sidebar_width = getattr(self.split_view, 'get_sidebar_width', lambda: 250)()
            self.config.save_window_geometry(width, height, sidebar_width)
            logger.debug(f"Saved window geometry: {width}x{height}, sidebar: {sidebar_width}")
        except Exception as e:
            logger.error(f"Failed to save window state: {e}")
            self.welcome_view.set_visible(False)
            self.tab_view.set_visible(True)
            # Update tab titles in case they've changed
            self._update_tab_titles()
    
    def _update_tab_titles(self):
        """Update tab titles"""
        for page in self.tab_view.get_pages():
            child = page.get_child()
            if hasattr(child, 'connection'):
                page.set_title(child.connection.nickname)
    
    def on_connection_saved(self, dialog, connection_data):
        """Handle connection saved from dialog"""
        try:
            if dialog.is_editing:
                # Update existing connection
                old_connection = dialog.connection
                is_connected = old_connection in self.active_terminals
                
                # Store the current terminal instance if connected
                terminal = self.active_terminals.get(old_connection) if is_connected else None
                
                try:
                    logger.info(
                        "Window.on_connection_saved(edit): saving '%s' with %d forwarding rules",
                        old_connection.nickname, len(connection_data.get('forwarding_rules', []) or [])
                    )
                except Exception:
                    pass
                
                # Detect if anything actually changed; avoid unnecessary writes/prompts
                def _norm_str(v):
                    try:
                        s = ('' if v is None else str(v)).strip()
                        # Treat keyfile placeholders as empty
                        if s.lower().startswith('select key file') or 'select key file or leave empty' in s.lower():
                            return ''
                        return s
                    except Exception:
                        return ''
                def _norm_rules(rules):
                    try:
                        return list(rules or [])
                    except Exception:
                        return []
                existing = {
                    'nickname': _norm_str(getattr(old_connection, 'nickname', '')),
                    'host': _norm_str(getattr(old_connection, 'host', '')),
                    'username': _norm_str(getattr(old_connection, 'username', '')),
                    'port': int(getattr(old_connection, 'port', 22) or 22),
                    'auth_method': int(getattr(old_connection, 'auth_method', 0) or 0),
                    'keyfile': _norm_str(getattr(old_connection, 'keyfile', '')),
                    'certificate': _norm_str(getattr(old_connection, 'certificate', '')),
                    'use_raw_sshconfig': bool(getattr(old_connection, 'use_raw_sshconfig', False)),
                    'raw_ssh_config_block': _norm_str(getattr(old_connection, 'raw_ssh_config_block', '')),
                    'key_select_mode': int(getattr(old_connection, 'key_select_mode', 0) or 0),
                    'password': _norm_str(getattr(old_connection, 'password', '')),
                    'key_passphrase': _norm_str(getattr(old_connection, 'key_passphrase', '')),
                    'x11_forwarding': bool(getattr(old_connection, 'x11_forwarding', False)),
                    'forwarding_rules': _norm_rules(getattr(old_connection, 'forwarding_rules', [])),
                    'local_command': _norm_str(getattr(old_connection, 'local_command', '') or (getattr(old_connection, 'data', {}).get('local_command') if hasattr(old_connection, 'data') else '')),
                    'remote_command': _norm_str(getattr(old_connection, 'remote_command', '') or (getattr(old_connection, 'data', {}).get('remote_command') if hasattr(old_connection, 'data') else '')),
                }
                incoming = {
                    'nickname': _norm_str(connection_data.get('nickname')),
                    'host': _norm_str(connection_data.get('host')),
                    'username': _norm_str(connection_data.get('username')),
                    'port': int(connection_data.get('port') or 22),
                    'auth_method': int(connection_data.get('auth_method') or 0),
                    'keyfile': _norm_str(connection_data.get('keyfile')),
                    'certificate': _norm_str(connection_data.get('certificate')),
                    'use_raw_sshconfig': bool(connection_data.get('use_raw_sshconfig', False)),
                    'raw_ssh_config_block': _norm_str(connection_data.get('raw_ssh_config_block')),
                    'key_select_mode': int(connection_data.get('key_select_mode') or 0),
                    'password': _norm_str(connection_data.get('password')),
                    'key_passphrase': _norm_str(connection_data.get('key_passphrase')),
                    'x11_forwarding': bool(connection_data.get('x11_forwarding', False)),
                    'forwarding_rules': _norm_rules(connection_data.get('forwarding_rules')),
                    'local_command': _norm_str(connection_data.get('local_command')),
                    'remote_command': _norm_str(connection_data.get('remote_command')),
                }
                # Determine if anything meaningful changed by comparing canonical SSH config blocks
                try:
                    existing_block = self.connection_manager.format_ssh_config_entry(existing)
                    incoming_block = self.connection_manager.format_ssh_config_entry(incoming)
                    # Also include auth_method/password/key_select_mode delta in change detection
                    pw_changed_flag = bool(connection_data.get('password_changed', False))
                    ksm_changed = (existing.get('key_select_mode', 0) != incoming.get('key_select_mode', 0))
                    changed = (existing_block != incoming_block) or (existing['auth_method'] != incoming['auth_method']) or pw_changed_flag or ksm_changed or (existing['password'] != incoming['password'])
                except Exception:
                    # Fallback to dict comparison if formatter fails
                    changed = existing != incoming

                # Extra guard: if key_select_mode or auth_method differs from the object's current value, force changed
                try:
                    if int(connection_data.get('key_select_mode', -1)) != int(getattr(old_connection, 'key_select_mode', -1)):
                        changed = True
                    if int(connection_data.get('auth_method', -1)) != int(getattr(old_connection, 'auth_method', -1)):
                        changed = True
                except Exception:
                    pass

                # Always force update when editing connections - skip change detection entirely for forwarding rules
                logger.info("Editing connection '%s' - forcing update to ensure forwarding rules are synced", existing['nickname'])

                logger.debug(f"Updating connection '{old_connection.nickname}'")
                
                # Update connection in manager first
                if not self.connection_manager.update_connection(old_connection, connection_data):
                    logger.error("Failed to update connection in SSH config")
                    return
                
                # Update connection attributes in memory (ensure forwarding rules kept)
                old_connection.nickname = connection_data['nickname']
                old_connection.host = connection_data['host']
                old_connection.username = connection_data['username']
                old_connection.port = connection_data['port']
                old_connection.keyfile = connection_data['keyfile']
                old_connection.certificate = connection_data.get('certificate', '')
                old_connection.use_raw_sshconfig = connection_data.get('use_raw_sshconfig', False)
                old_connection.raw_ssh_config_block = connection_data.get('raw_ssh_config_block', '')
                old_connection.password = connection_data['password']
                old_connection.key_passphrase = connection_data['key_passphrase']
                old_connection.auth_method = connection_data['auth_method']
                # Persist key selection mode in-memory so the dialog reflects it without restart
                try:
                    old_connection.key_select_mode = int(connection_data.get('key_select_mode', getattr(old_connection, 'key_select_mode', 0)) or 0)
                except Exception:
                    pass
                old_connection.x11_forwarding = connection_data['x11_forwarding']
                old_connection.forwarding_rules = list(connection_data.get('forwarding_rules', []))
                # Update commands
                try:
                    old_connection.local_command = connection_data.get('local_command', '')
                    old_connection.remote_command = connection_data.get('remote_command', '')
                except Exception:
                    pass
                
                # The connection has already been updated in-place, so we don't need to reload from disk
                # The forwarding rules are already updated in the connection_data
                
                # Persist per-connection metadata not stored in SSH config (auth method, etc.)
                try:
                    meta_key = old_connection.nickname
                    self.config.set_connection_meta(meta_key, {
                        'auth_method': connection_data.get('auth_method', 0),
                        'use_raw_sshconfig': connection_data.get('use_raw_sshconfig', False),
                        'raw_ssh_config_block': connection_data.get('raw_ssh_config_block', '')
                    })
                except Exception:
                    pass

                # Update UI
                if old_connection in self.connection_rows:
                    # Get the row before potentially modifying the dictionary
                    row = self.connection_rows[old_connection]
                    # Remove the old connection from the dictionary
                    del self.connection_rows[old_connection]
                    # Add it back with the updated connection object
                    self.connection_rows[old_connection] = row
                    # Update the display
                    row.update_display()
                else:
                    # If the connection is not in the rows, rebuild the list
                    self.rebuild_connection_list()
                
                logger.info(f"Updated connection: {old_connection.nickname}")
                
                # If the connection is active, ask if user wants to reconnect
                if is_connected and terminal is not None:
                    # Store the terminal in the connection for later use
                    old_connection._terminal_instance = terminal
                    self._prompt_reconnect(old_connection)
                
            else:
                # Create new connection
                connection = Connection(connection_data)
                # Ensure the in-memory object has the chosen auth_method immediately
                try:
                    connection.auth_method = int(connection_data.get('auth_method', 0))
                except Exception:
                    connection.auth_method = 0
                # Ensure key selection mode is applied immediately
                try:
                    connection.key_select_mode = int(connection_data.get('key_select_mode', 0) or 0)
                except Exception:
                    connection.key_select_mode = 0
                # Ensure certificate is applied immediately
                try:
                    connection.certificate = connection_data.get('certificate', '')
                except Exception:
                    connection.certificate = ''
                # Ensure raw SSH config settings are applied immediately
                try:
                    connection.use_raw_sshconfig = connection_data.get('use_raw_sshconfig', False)
                    connection.raw_ssh_config_block = connection_data.get('raw_ssh_config_block', '')
                except Exception:
                    connection.use_raw_sshconfig = False
                    connection.raw_ssh_config_block = ''
                # Add the new connection to the manager's connections list
                self.connection_manager.connections.append(connection)
                

                
                # Save the connection to SSH config and emit the connection-added signal
                if self.connection_manager.update_connection(connection, connection_data):
                    # Reload from SSH config and rebuild list immediately
                    try:
                        self.connection_manager.load_ssh_config()
                        self.rebuild_connection_list()
                    except Exception:
                        pass
                    # Persist per-connection metadata then reload config
                    try:
                        self.config.set_connection_meta(connection.nickname, {
                            'auth_method': connection_data.get('auth_method', 0),
                            'use_raw_sshconfig': connection_data.get('use_raw_sshconfig', False),
                            'raw_ssh_config_block': connection_data.get('raw_ssh_config_block', '')
                        })
                        try:
                            self.connection_manager.load_ssh_config()
                            self.rebuild_connection_list()
                        except Exception:
                            pass
                    except Exception:
                        pass
                    # Sync forwarding rules from a fresh reload to ensure UI matches disk
                    try:
                        reloaded_new = self.connection_manager.find_connection_by_nickname(connection.nickname)
                        if reloaded_new:
                            connection.forwarding_rules = list(reloaded_new.forwarding_rules or [])
                            logger.info("New connection '%s' has %d rules after write", connection.nickname, len(connection.forwarding_rules))
                    except Exception:
                        pass
                    # Manually add the connection to the UI since we're not using the signal
                    # Row list was rebuilt from config; no manual add required
                    logger.info(f"Created new connection: {connection_data['nickname']}")
                else:
                    logger.error("Failed to save connection to SSH config")
                
        except Exception as e:
            logger.error(f"Failed to save connection: {e}")
            # Show error dialog
            error_dialog = Gtk.MessageDialog(
                transient_for=self,
                modal=True,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text=_("Failed to save connection"),
                secondary_text=str(e)
            )
            error_dialog.present()
    
    def _rebuild_connections_list(self):
        """Rebuild the sidebar connections list from manager state, avoiding duplicates."""
        try:
            self.rebuild_connection_list()
        except Exception:
            pass
    def _prompt_reconnect(self, connection):
        """Show a dialog asking if user wants to reconnect with new settings"""
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=_("Settings Changed"),
            secondary_text=_("The connection settings have been updated.\n"
                           "Would you like to reconnect with the new settings?")
        )
        dialog.connect("response", self._on_reconnect_response, connection)
        dialog.present()
    
    def _on_reconnect_response(self, dialog, response_id, connection):
        """Handle response from reconnect prompt"""
        dialog.destroy()
        
        # Only proceed if user clicked Yes and the connection is still active
        if response_id != Gtk.ResponseType.YES or connection not in self.active_terminals:
            # Clean up the stored terminal instance if it exists
            if hasattr(connection, '_terminal_instance'):
                delattr(connection, '_terminal_instance')
            return
            
        # Get the terminal instance either from active_terminals or the stored instance
        terminal = self.active_terminals.get(connection) or getattr(connection, '_terminal_instance', None)
        if not terminal:
            logger.warning("No terminal instance found for reconnection")
            return
            
        # Set controlled reconnect flag
        self._is_controlled_reconnect = True

        # Show reconnecting feedback
        self._show_reconnecting_message(connection)
        
        try:
            # Disconnect first (defer to avoid blocking)
            logger.debug("Disconnecting terminal before reconnection")
            def _safe_disconnect():
                try:
                    terminal.disconnect()
                    logger.debug("Terminal disconnected, scheduling reconnect")
                    # Store the connection temporarily in active_terminals if not present
                    if connection not in self.active_terminals:
                        self.active_terminals[connection] = terminal
                    # Reconnect after disconnect completes
                    GLib.timeout_add(1000, self._reconnect_terminal, connection)  # Increased delay
                except Exception as e:
                    logger.error(f"Error during disconnect: {e}")
                    GLib.idle_add(self._show_reconnect_error, connection, str(e))
                return False
            
            # Defer disconnect to avoid blocking the UI thread
            GLib.idle_add(_safe_disconnect)
            
        except Exception as e:
            logger.error(f"Error during reconnection: {e}")
            # Remove from active terminals if reconnection fails
            if connection in self.active_terminals:
                del self.active_terminals[connection]
                
            # Show error to user
            error_dialog = Gtk.MessageDialog(
                transient_for=self,
                modal=True,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text=_("Reconnection Failed"),
                secondary_text=_("Failed to reconnect with the new settings. Please try connecting again manually.")
            )
            error_dialog.present()
            
        finally:
            # Clean up the stored terminal instance
            if hasattr(connection, '_terminal_instance'):
                delattr(connection, '_terminal_instance')
                
            # Reset the flag after a delay to ensure it's not set during normal operations
            GLib.timeout_add(1000, self._reset_controlled_reconnect)
    
    def _reset_controlled_reconnect(self):
        """Reset the controlled reconnect flag"""
        self._is_controlled_reconnect = False
    
    def _reconnect_terminal(self, connection):
        """Reconnect a terminal with updated connection settings"""
        if connection not in self.active_terminals:
            logger.warning(f"Connection {connection.nickname} not found in active terminals")
            return False  # Don't repeat the timeout
            
        terminal = self.active_terminals[connection]
        
        try:
            logger.debug(f"Attempting to reconnect terminal for {connection.nickname}")
            
            # Reconnect with new settings
            if not terminal._connect_ssh():
                logger.error("Failed to reconnect with new settings")
                # Show error to user
                GLib.idle_add(self._show_reconnect_error, connection)
                return False
                
            logger.info(f"Successfully reconnected terminal for {connection.nickname}")
            
        except Exception as e:
            logger.error(f"Error reconnecting terminal: {e}", exc_info=True)
            GLib.idle_add(self._show_reconnect_error, connection, str(e))
            
        return False  # Don't repeat the timeout
        
    def _show_reconnect_error(self, connection, error_message=None):
        """Show an error message when reconnection fails"""
        # Ensure reconnecting feedback is hidden
        self._hide_reconnecting_message()
        # Remove from active terminals if reconnection fails
        if connection in self.active_terminals:
            del self.active_terminals[connection]
            
        # Update UI to show disconnected state
        if connection in self.connection_rows:
            self.connection_rows[connection].update_status()
        
        # Show error dialog
        error_dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text=_("Reconnection Failed"),
            secondary_text=error_message or _("Failed to reconnect with the new settings. Please try connecting again manually.")
        )
        error_dialog.present()
        
        # Clean up the dialog when closed
        error_dialog.connect("response", lambda d, r: d.destroy())

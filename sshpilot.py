#!/usr/bin/env python3
"""
Fresh SSH Manager - Completely clean implementation
"""

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Vte', '3.91')
from gi.repository import Gtk, Vte, GLib, Gdk, Gio, Pango
import json
import os
import threading
from pathlib import Path
import subprocess
import sys

print("=== PILOT: Initializing SSH Pilot ===")

class SSHConnection:
    def __init__(self, hostname, username, port=22, password=None, key_path=None, key_passphrase=None, name=None):
        self.hostname = hostname
        self.username = username
        self.port = port
        self.password = password
        self.key_path = key_path
        self.key_passphrase = key_passphrase
        self.name = name or f"{username}@{hostname}"

class FreshSSHManager:
    def __init__(self):
        print("=== FRESH: Initializing Fresh SSH Manager ===")
        self.connections = []
        self.connected_connections = {}
        self.config_file = os.path.expanduser("~/.fresh_ssh_manager_config.json")
        
        # Settings with defaults
        self.settings = {
            'terminal_theme': 'default',
            'font_family': 'Monospace',
            'font_size': 11,
            'background_color': '#000000',
            'foreground_color': '#ffffff',
            'pane_position': 260
        }
        
        # Load saved settings
        self.load_settings()
        
        # Create unique application ID
        app_id = f"com.example.fresh-ssh-manager-{os.getpid()}"
        print(f"=== FRESH: Using app ID: {app_id} ===")
        
        # Initialize GTK
        self.app = Gtk.Application(application_id=app_id)
        self.app.connect('activate', self.on_activate)
        
        # Add application actions for context menu
        self.setup_actions()
        print("=== FRESH: Application created and signal connected ===")
        
        # Main window
        self.window = None
        self.connection_listbox = None
        self.notebook = None
        
    def load_connections(self):
        """Load saved connections from config file"""
        print("=== FRESH: Loading connections ===")
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    data = json.load(f)
                    for conn_data in data.get('connections', []):
                        conn = SSHConnection(
                            hostname=conn_data['hostname'],
                            username=conn_data['username'],
                            port=conn_data.get('port', 22),
                            password=conn_data.get('password'),
                            key_path=conn_data.get('key_path'),
                            key_passphrase=conn_data.get('key_passphrase'),
                            name=conn_data.get('name')
                        )
                        self.connections.append(conn)
                print(f"=== FRESH: Loaded {len(self.connections)} connections ===")
            except Exception as e:
                print(f"=== FRESH: Error loading connections: {e} ===")
    
    def save_connections(self):
        """Save connections to config file"""
        print("=== FRESH: Saving connections ===")
        data = {'connections': []}
        for conn in self.connections:
            conn_data = {
                'hostname': conn.hostname,
                'username': conn.username,
                'port': conn.port,
                'name': conn.name
            }
            if conn.password:
                conn_data['password'] = conn.password
            if conn.key_path:
                conn_data['key_path'] = conn.key_path
            if conn.key_passphrase:
                conn_data['key_passphrase'] = conn.key_passphrase
            data['connections'].append(conn_data)
        
        try:
            with open(self.config_file, 'w') as f:
                json.dump(data, f, indent=2)
            print(f"=== FRESH: Saved {len(self.connections)} connections ===")
        except Exception as e:
            print(f"=== FRESH: Error saving connections: {e} ===")
    
    def detect_ssh_keys(self):
        """Detect available SSH keys in ~/.ssh/"""
        print("=== FRESH: Detecting SSH keys ===")
        ssh_dir = Path.home() / ".ssh"
        keys = []
        
        if ssh_dir.exists():
            for key_file in ssh_dir.glob("*"):
                if key_file.is_file() and not key_file.name.endswith('.pub'):
                    keys.append(str(key_file))
        
        print(f"=== FRESH: Found {len(keys)} SSH keys ===")
        return keys
    
    def load_css(self):
        """Load CSS styling"""
        print("=== FRESH: Loading CSS ===")
        css_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ssh_manager.css")
        if os.path.exists(css_file):
            try:
                css_provider = Gtk.CssProvider()
                css_provider.load_from_file(Gio.File.new_for_path(css_file))
                Gtk.StyleContext.add_provider_for_display(
                    Gdk.Display.get_default(),
                    css_provider,
                    Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                )
                print("=== FRESH: CSS loaded successfully ===")
            except Exception as e:
                print(f"=== FRESH: Error loading CSS: {e} ===")
        else:
            print("=== FRESH: CSS file not found ===")
    
    def setup_actions(self):
        """Setup application actions for context menu"""
        print("=== FRESH: Setting up actions ===")
        
        # Connect action
        connect_action = Gio.SimpleAction.new("connect", None)
        connect_action.connect("activate", self.on_context_connect)
        self.app.add_action(connect_action)
        
        # Edit action
        edit_action = Gio.SimpleAction.new("edit", None)
        edit_action.connect("activate", self.on_context_edit)
        self.app.add_action(edit_action)
        
        # Rename action
        rename_action = Gio.SimpleAction.new("rename", None)
        rename_action.connect("activate", self.on_context_rename)
        self.app.add_action(rename_action)
        
        # Delete action
        delete_action = Gio.SimpleAction.new("delete", None)
        delete_action.connect("activate", self.on_context_delete)
        self.app.add_action(delete_action)
        
        # Focus connection list action
        focus_list_action = Gio.SimpleAction.new("focus-list", None)
        focus_list_action.connect("activate", self.on_focus_connection_list)
        self.app.add_action(focus_list_action)
        
        # Set up keyboard shortcuts
        self.setup_keyboard_shortcuts()
    
    def setup_keyboard_shortcuts(self):
        """Setup application keyboard shortcuts"""
        print("=== FRESH: Setting up keyboard shortcuts ===")
        
        # Ctrl+L to focus connection list
        self.app.set_accels_for_action("app.focus-list", ["<Ctrl>l"])
    
    def on_focus_connection_list(self, action, parameter):
        """Focus the connection list"""
        print("=== FRESH: Focus connection list shortcut activated ===")
        if self.connection_listbox:
            self.connection_listbox.grab_focus()
            print("=== FRESH: Connection list focused ===")
    
    def on_context_connect(self, action, parameter):
        """Handle context menu connect action"""
        print("=== FRESH: Context menu connect ===")
        if hasattr(self, 'context_menu_connection') and self.context_menu_connection:
            self.create_terminal_tab(self.context_menu_connection)
    
    def on_context_edit(self, action, parameter):
        """Handle context menu edit action"""
        print("=== FRESH: Context menu edit ===")
        if hasattr(self, 'context_menu_connection') and self.context_menu_connection:
            self.show_edit_dialog(self.context_menu_connection)
    
    def on_context_rename(self, action, parameter):
        """Handle context menu rename action"""
        print("=== FRESH: Context menu rename ===")
        if hasattr(self, 'context_menu_connection') and self.context_menu_connection:
            self.show_rename_dialog(self.context_menu_connection)
    
    def on_context_delete(self, action, parameter):
        """Handle context menu delete action"""
        print("=== FRESH: Context menu delete ===")
        if hasattr(self, 'context_menu_connection') and self.context_menu_connection:
            conn = self.context_menu_connection
            
            # Disconnect if connected
            if conn.hostname in self.connected_connections:
                self.disconnect_host(conn.hostname)
            
            # Remove from list
            self.connections.remove(conn)
            self.save_connections()
            self.refresh_connection_list()
            print(f"=== FRESH: Deleted connection via context menu: {conn.name} ===")
    
    def on_activate(self, app):
        print("=== FRESH: on_activate called! ===")
        
        # Create window
        self.window = Gtk.ApplicationWindow(application=app, title="SSH Pilot")
        self.window.set_default_size(1200, 800)
        self.window.connect('close-request', self.on_window_close_request)
        print("=== FRESH: Window created ===")
        
        # Add global key controller for arrow key handling when no connections are active
        global_key_controller = Gtk.EventControllerKey()
        global_key_controller.connect('key-pressed', self.on_global_key_pressed)
        self.window.add_controller(global_key_controller)
        print("=== FRESH: Global key controller added ===")
        
        # Load CSS styling
        self.load_css()
        
        # Main layout with resizable panes
        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.paned.set_resize_start_child(False)  # Left panel fixed size preference
        self.paned.set_resize_end_child(True)     # Right panel gets extra space
        self.paned.set_shrink_start_child(False)  # Don't shrink left panel too much
        self.paned.set_shrink_end_child(True)     # Allow right panel to shrink
        
        # Load saved pane position or use default
        saved_position = self.settings.get('pane_position', 260)
        self.paned.set_position(saved_position)
        
        self.window.set_child(self.paned)
        print("=== FRESH: Resizable paned layout created ===")
        
        # Left panel for connection list
        left_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        left_panel.set_size_request(200, -1)  # Minimum width
        left_panel.set_hexpand(False)  # Don't expand horizontally
        left_panel.add_css_class("sidebar")
        self.paned.set_start_child(left_panel)
        print("=== FRESH: Left panel created ===")
        
        # Header with title and menu
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        header_box.set_margin_start(8)
        header_box.set_margin_end(8)
        header_box.set_margin_top(8)
        header_box.set_margin_bottom(4)
        left_panel.append(header_box)
        
        # Connection list title
        list_label = Gtk.Label(label="SSH Connections")
        list_label.set_halign(Gtk.Align.START)
        list_label.add_css_class("heading")
        list_label.set_hexpand(True)
        header_box.append(list_label)
        
        # Menu button
        self.menu_button = Gtk.MenuButton()
        self.menu_button.set_icon_name("open-menu-symbolic")
        self.menu_button.set_tooltip_text("Application Menu")
        self.setup_menu_button()
        header_box.append(self.menu_button)
        
        print("=== FRESH: Header with menu created ===")
        
        # Scrolled window for connection list
        scrolled_window = Gtk.ScrolledWindow()
        scrolled_window.set_vexpand(True)
        left_panel.append(scrolled_window)
        print("=== FRESH: Scrolled window created ===")
        
        # Connection list using ListBox
        self.connection_listbox = Gtk.ListBox()
        self.connection_listbox.set_can_focus(True)
        self.connection_listbox.connect('row-selected', self.on_connection_selected)
        
        # Add right-click context menu
        gesture = Gtk.GestureClick()
        gesture.set_button(3)  # Right mouse button
        gesture.connect('pressed', self.on_right_click)
        self.connection_listbox.add_controller(gesture)
        
        # Add double-click gesture
        double_click_gesture = Gtk.GestureClick()
        double_click_gesture.set_button(1)  # Left mouse button
        double_click_gesture.connect('pressed', self.on_double_click)
        self.connection_listbox.add_controller(double_click_gesture)
        
        # Add keyboard event controller for Enter key
        key_controller = Gtk.EventControllerKey()
        key_controller.connect('key-pressed', self.on_key_pressed)
        self.connection_listbox.add_controller(key_controller)
        
        scrolled_window.set_child(self.connection_listbox)
        print("=== FRESH: ListBox created and connected ===")
        
        # Buttons with improved layout
        button_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        button_container.set_margin_start(8)
        button_container.set_margin_end(8)
        button_container.set_margin_bottom(8)
        left_panel.append(button_container)
        
        # Primary actions (Add, Edit)
        primary_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        primary_box.set_homogeneous(True)
        button_container.append(primary_box)
        
        add_button = Gtk.Button(label="Add")
        add_button.connect('clicked', self.on_add_connection)
        add_button.add_css_class("suggested-action")
        primary_box.append(add_button)
        
        self.edit_button = Gtk.Button(label="Edit")
        self.edit_button.connect('clicked', self.on_edit_connection)
        self.edit_button.set_sensitive(False)
        primary_box.append(self.edit_button)
        
        # Connection actions (Connect, Disconnect)
        connection_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        connection_box.set_homogeneous(True)
        button_container.append(connection_box)
        
        self.connect_button = Gtk.Button(label="Connect")
        self.connect_button.connect('clicked', self.on_connect)
        self.connect_button.set_sensitive(False)
        connection_box.append(self.connect_button)
        
        self.disconnect_button = Gtk.Button(label="Disconnect")
        self.disconnect_button.connect('clicked', self.on_disconnect)
        self.disconnect_button.set_sensitive(False)
        connection_box.append(self.disconnect_button)
        
        # Destructive actions (Delete)
        danger_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        button_container.append(danger_box)
        
        self.delete_button = Gtk.Button(label="Delete")
        self.delete_button.connect('clicked', self.on_delete_connection)
        self.delete_button.set_sensitive(False)
        self.delete_button.add_css_class("destructive-action")
        danger_box.append(self.delete_button)
        
        print("=== FRESH: Buttons created ===")
        
        # Right panel for terminal
        right_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        right_panel.set_hexpand(True)
        right_panel.set_vexpand(True)
        self.paned.set_end_child(right_panel)
        
        # Notebook for tabs
        self.notebook = Gtk.Notebook()
        self.notebook.set_vexpand(True)
        self.notebook.connect('switch-page', self.on_tab_switched)
        right_panel.append(self.notebook)
        print("=== FRESH: Notebook created ===")
        
        # Add initial help screen
        self.show_help_screen()
        print("=== FRESH: Help screen added ===")
        
        # Load existing connections into list
        self.load_connections()
        self.refresh_connection_list()
        print("=== FRESH: Connections loaded and list refreshed ===")
        
        # Connect pane position change to save settings
        self.paned.connect('notify::position', self.on_pane_position_changed)
        
        print("=== FRESH: About to present window ===")
        self.window.present()
        print("=== FRESH: Window presented! ===")
    
    def load_settings(self):
        """Load settings from file"""
        settings_file = os.path.expanduser("~/.fresh_ssh_manager_settings.json")
        try:
            if os.path.exists(settings_file):
                with open(settings_file, 'r') as f:
                    saved_settings = json.load(f)
                    self.settings.update(saved_settings)
                    print(f"=== FRESH: Loaded settings: {self.settings} ===")
        except Exception as e:
            print(f"=== FRESH: Error loading settings: {e} ===")
    
    def save_settings(self):
        """Save settings to file"""
        settings_file = os.path.expanduser("~/.fresh_ssh_manager_settings.json")
        try:
            with open(settings_file, 'w') as f:
                json.dump(self.settings, f, indent=2)
                print(f"=== FRESH: Saved settings: {self.settings} ===")
        except Exception as e:
            print(f"=== FRESH: Error saving settings: {e} ===")
    
    def on_pane_position_changed(self, paned, param):
        """Handle pane position change"""
        position = paned.get_position()
        self.settings['pane_position'] = position
        self.save_settings()
        print(f"=== FRESH: Pane position changed to: {position} ===")
    
    def setup_menu_button(self):
        """Setup the application menu button"""
        # Create menu model
        menu_model = Gio.Menu()
        
        # Settings section
        settings_section = Gio.Menu()
        settings_section.append("Settings", "app.settings")
        menu_model.append_section(None, settings_section)
        
        # Help section
        help_section = Gio.Menu()
        help_section.append("Help", "app.help")
        help_section.append("About", "app.about")
        menu_model.append_section(None, help_section)
        
        # Set the menu
        self.menu_button.set_menu_model(menu_model)
        
        # Add actions
        self.add_menu_actions()
    
    def add_menu_actions(self):
        """Add menu actions"""
        # Settings action
        settings_action = Gio.SimpleAction.new("settings", None)
        settings_action.connect("activate", self.on_settings)
        self.app.add_action(settings_action)
        
        # Help action
        help_action = Gio.SimpleAction.new("help", None)
        help_action.connect("activate", self.on_help)
        self.app.add_action(help_action)
        
        # About action
        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self.on_about)
        self.app.add_action(about_action)
    
    def on_settings(self, action, parameter):
        """Show settings dialog"""
        print("=== FRESH: Settings menu clicked ===")
        self.show_settings_dialog()
    
    def on_help(self, action, parameter):
        """Show help dialog"""
        print("=== FRESH: Help menu clicked ===")
        self.show_help_dialog()
    
    def on_about(self, action, parameter):
        """Show about dialog"""
        print("=== FRESH: About menu clicked ===")
        self.show_about_dialog()
    
    def on_connection_selected(self, listbox, row):
        """Handle connection selection"""
        print("=== FRESH: Connection selected ===")
        if row and hasattr(row, 'connection'):
            conn = row.connection
            connected = conn.hostname in self.connected_connections
            
            self.connect_button.set_sensitive(not connected)
            self.disconnect_button.set_sensitive(connected)
            self.edit_button.set_sensitive(True)
            self.delete_button.set_sensitive(True)
        else:
            self.connect_button.set_sensitive(False)
            self.disconnect_button.set_sensitive(False)
            self.edit_button.set_sensitive(False)
            self.delete_button.set_sensitive(False)
    
    def refresh_connection_list(self):
        """Refresh the connection list display"""
        print("=== FRESH: Refreshing connection list ===")
        try:
            # Remember the currently selected connection
            selected_connection = None
            selected_row = self.connection_listbox.get_selected_row()
            if selected_row and hasattr(selected_row, 'connection'):
                selected_connection = selected_row.connection
                print(f"=== FRESH: Remembering selected connection: {selected_connection.name} ===")
            
            # Clear existing items
            child = self.connection_listbox.get_first_child()
            while child:
                next_child = child.get_next_sibling()
                self.connection_listbox.remove(child)
                child = next_child
            
            # Add connections
            for i, conn in enumerate(self.connections):
                print(f"=== FRESH: Adding connection {i+1}: {conn.name} ===")
                
                # Create row
                row = Gtk.ListBoxRow()
                
                # Create content box
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                box.set_margin_start(6)
                box.set_margin_end(6)
                box.set_margin_top(3)
                box.set_margin_bottom(3)
                
                # Name label
                name_label = Gtk.Label(label=conn.name)
                name_label.set_hexpand(True)
                name_label.set_halign(Gtk.Align.START)
                box.append(name_label)
                
                # Host label
                host_label = Gtk.Label(label=conn.hostname)
                host_label.set_halign(Gtk.Align.END)
                box.append(host_label)
                
                # Set color based on connection status
                connected = conn.hostname in self.connected_connections
                if connected:
                    name_label.add_css_class("connected")
                    host_label.add_css_class("connected")
                else:
                    name_label.remove_css_class("connected")
                    host_label.remove_css_class("connected")
                
                row.set_child(box)
                # Store connection reference as attribute instead of using deprecated set_data
                row.connection = conn
                
                self.connection_listbox.append(row)
                
                # Restore selection if this was the previously selected connection
                if selected_connection and conn.hostname == selected_connection.hostname:
                    self.connection_listbox.select_row(row)
                    print(f"=== FRESH: Restored selection to: {conn.name} ===")
            
            print(f"=== FRESH: Added {len(self.connections)} connections to list ===")
        except Exception as e:
            print(f"=== FRESH: Error refreshing connection list: {e} ===")
            import traceback
            traceback.print_exc()
    
    def on_add_connection(self, button):
        """Show add connection dialog"""
        print("=== FRESH: Add button clicked ===")
        dialog = AddConnectionDialog(self.window, self.detect_ssh_keys())
        dialog.connect('close-request', self.on_add_dialog_close)
        dialog.present()
    
    def on_add_dialog_close(self, dialog):
        """Handle add dialog close"""
        response = getattr(dialog, 'response', None)
        print(f"=== FRESH: Dialog response: {response} ===")
        if response == Gtk.ResponseType.OK:
            conn_data = dialog.get_connection_data()
            if conn_data['name'] and conn_data['hostname'] and conn_data['username']:
                conn = SSHConnection(
                    hostname=conn_data['hostname'],
                    username=conn_data['username'],
                    name=conn_data['name'],
                    port=conn_data['port'],
                    password=conn_data.get('password'),
                    key_path=conn_data.get('key_path'),
                    key_passphrase=conn_data.get('key_passphrase')
                )
                self.connections.append(conn)
                self.save_connections()
                self.refresh_connection_list()
                print(f"=== FRESH: Added connection: {conn.name} ===")
    
    def on_connect(self, button):
        """Connect to selected host"""
        print("=== FRESH: Connect button clicked ===")
        row = self.connection_listbox.get_selected_row()
        if row and hasattr(row, 'connection'):
            conn = row.connection
            print(f"=== FRESH: Connecting to {conn.name} ===")
            self.create_terminal_tab(conn)
    
    def on_disconnect(self, button):
        """Disconnect from selected host"""
        print("=== FRESH: Disconnect button clicked ===")
        row = self.connection_listbox.get_selected_row()
        if row and hasattr(row, 'connection'):
            conn = row.connection
            self.disconnect_host(conn.hostname)
    
    def on_edit_connection(self, button):
        """Edit selected connection"""
        print("=== FRESH: Edit button clicked ===")
        row = self.connection_listbox.get_selected_row()
        if row and hasattr(row, 'connection'):
            conn = row.connection
            self.show_edit_dialog(conn)
    
    def on_delete_connection(self, button):
        """Delete selected connection with confirmation"""
        print("=== FRESH: Delete button clicked ===")
        row = self.connection_listbox.get_selected_row()
        if row and hasattr(row, 'connection'):
            conn = row.connection
            self.show_delete_confirmation(conn)
    
    def on_right_click(self, gesture, n_press, x, y):
        """Handle right-click on connection list"""
        print("=== FRESH: Right-click detected ===")
        # Get the row at the click position
        row = self.connection_listbox.get_row_at_y(y)
        if row and hasattr(row, 'connection'):
            # Select the row
            self.connection_listbox.select_row(row)
            conn = row.connection
            
            # Show context menu
            self.show_context_menu(conn, x, y)
    
    def on_double_click(self, gesture, n_press, x, y):
        """Handle double-click on connection list"""
        # Only respond to actual double-clicks
        if n_press == 2:
            print("=== FRESH: Double-click detected ===")
            
            # Get the row at the click position
            row = self.connection_listbox.get_row_at_y(y)
            if row and hasattr(row, 'connection'):
                # Select the row
                self.connection_listbox.select_row(row)
                
                # Get connection
                conn = row.connection
                print(f"=== FRESH: Double-click on {conn.name} ===")
                
                # Check if already connected
                if conn.hostname in self.connected_connections:
                    # Switch to existing tab
                    print(f"=== FRESH: Switching to existing tab for {conn.name} ===")
                    self.switch_to_connection_tab(conn)
                else:
                    # Connect to machine
                    print(f"=== FRESH: Connecting via double-click to {conn.name} ===")
                    self.create_terminal_tab(conn)
    
    def on_key_pressed(self, controller, keyval, keycode, state):
        """Handle key press events on connection list"""
        # Check if Enter key was pressed
        if keyval == 65293:  # GDK_KEY_Return (Enter key)
            print("=== FRESH: Enter key pressed ===")
            
            # Get selected connection
            row = self.connection_listbox.get_selected_row()
            if row and hasattr(row, 'connection'):
                conn = row.connection
                print(f"=== FRESH: Enter key connecting to {conn.name} ===")
                
                # Check if already connected
                if conn.hostname in self.connected_connections:
                    # Switch to existing tab
                    print(f"=== FRESH: Enter key switching to existing tab for {conn.name} ===")
                    self.switch_to_connection_tab(conn)
                    # Update button states immediately for tab switching
                    self.on_connection_selected(self.connection_listbox, row)
                else:
                    # Connect to machine
                    print(f"=== FRESH: Enter key connecting to {conn.name} ===")
                    self.create_terminal_tab(conn)
                    # Button states will be updated after connection is established
                
                return True  # Event handled
        
        return False  # Event not handled
    
    def on_global_key_pressed(self, controller, keyval, keycode, state):
        """Global key handler to redirect arrow keys to ListBox when no connections are active"""
        # Check if arrow keys were pressed
        if keyval in [65362, 65364]:  # GDK_KEY_Up (65362) or GDK_KEY_Down (65364)
            print(f"=== FRESH: Global arrow key pressed (keyval: {keyval}) ===")
            
            # Check if no connections are currently active
            if len(self.connected_connections) == 0:
                print("=== FRESH: No connections active, redirecting arrow key to ListBox ===")
                
                # Move focus to ListBox
                self.connection_listbox.grab_focus()
                
                # Navigate in the ListBox based on arrow key
                if keyval == 65362:  # Up arrow
                    self.navigate_listbox_up()
                elif keyval == 65364:  # Down arrow
                    self.navigate_listbox_down()
                
                return True  # Event handled
        
        return False  # Event not handled
    
    def navigate_listbox_up(self):
        """Navigate up in the ListBox"""
        try:
            selected_row = self.connection_listbox.get_selected_row()
            if selected_row:
                # Get previous row
                prev_row = selected_row.get_prev_sibling()
                if prev_row:
                    self.connection_listbox.select_row(prev_row)
                    print("=== FRESH: Navigated up in ListBox ===")
            else:
                # No selection, select last row
                last_row = self.connection_listbox.get_last_child()
                if last_row:
                    self.connection_listbox.select_row(last_row)
                    print("=== FRESH: Selected last row ===")
        except Exception as e:
            print(f"=== FRESH: Error navigating up: {e} ===")

    def navigate_listbox_down(self):
        """Navigate down in the ListBox"""
        try:
            selected_row = self.connection_listbox.get_selected_row()
            if selected_row:
                # Get next row
                next_row = selected_row.get_next_sibling()
                if next_row:
                    self.connection_listbox.select_row(next_row)
                    print("=== FRESH: Navigated down in ListBox ===")
            else:
                # No selection, select first row
                first_row = self.connection_listbox.get_first_child()
                if first_row:
                    self.connection_listbox.select_row(first_row)
                    print("=== FRESH: Selected first row ===")
        except Exception as e:
            print(f"=== FRESH: Error navigating down: {e} ===")
    
    def show_context_menu(self, conn, x, y):
        """Show right-click context menu"""
        print(f"=== FRESH: Creating context menu for {conn.name} ===")
        
        # Store connection reference for menu actions
        self.context_menu_connection = conn
        
        # Create a simple popover menu
        popover = Gtk.Popover()
        popover.set_parent(self.connection_listbox)
        
        # Create menu box
        menu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        menu_box.set_margin_start(6)
        menu_box.set_margin_end(6)
        menu_box.set_margin_top(6)
        menu_box.set_margin_bottom(6)
        
        # Create menu buttons
        connect_btn = Gtk.Button(label="Connect")
        connect_btn.add_css_class("flat")
        connect_btn.set_halign(Gtk.Align.START)
        connect_btn.connect('clicked', self.on_context_menu_connect, popover)
        menu_box.append(connect_btn)
        
        edit_btn = Gtk.Button(label="Edit...")
        edit_btn.add_css_class("flat")
        edit_btn.set_halign(Gtk.Align.START)
        edit_btn.connect('clicked', self.on_context_menu_edit, popover)
        menu_box.append(edit_btn)
        
        delete_btn = Gtk.Button(label="Delete")
        delete_btn.add_css_class("flat")
        delete_btn.set_halign(Gtk.Align.START)
        delete_btn.connect('clicked', self.on_context_menu_delete, popover)
        menu_box.append(delete_btn)
        
        popover.set_child(menu_box)
        
        # Set position
        rect = Gdk.Rectangle()
        rect.x = x
        rect.y = y
        rect.width = 1
        rect.height = 1
        popover.set_pointing_to(rect)
        
        popover.popup()
        print("=== FRESH: Context menu created and shown ===")
    
    def on_context_menu_connect(self, button, popover):
        """Handle context menu connect"""
        print("=== FRESH: Context menu connect clicked ===")
        popover.popdown()
        if hasattr(self, 'context_menu_connection') and self.context_menu_connection:
            self.create_terminal_tab(self.context_menu_connection)
    
    def on_context_menu_edit(self, button, popover):
        """Handle context menu edit"""
        print("=== FRESH: Context menu edit clicked ===")
        popover.popdown()
        if hasattr(self, 'context_menu_connection') and self.context_menu_connection:
            self.show_edit_dialog(self.context_menu_connection)
    
    def on_context_menu_delete(self, button, popover):
        """Handle context menu delete with confirmation"""
        print("=== FRESH: Context menu delete clicked ===")
        popover.popdown()
        if hasattr(self, 'context_menu_connection') and self.context_menu_connection:
            self.show_delete_confirmation(self.context_menu_connection)
    
    def show_delete_confirmation(self, conn):
        """Show delete confirmation dialog"""
        dialog = Gtk.MessageDialog(
            transient_for=self.window,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f"Delete Connection '{conn.name}'?\n\nAre you sure you want to delete the connection to '{conn.hostname}'?\n\nThis action cannot be undone."
        )
        
        dialog.connect('response', self.on_delete_confirmation_response, conn)
        dialog.present()
    
    def on_delete_confirmation_response(self, dialog, response, conn):
        """Handle delete confirmation response"""
        if response == Gtk.ResponseType.YES:
            print(f"=== FRESH: Confirmed deletion of {conn.name} ===")
            
            # Disconnect if connected
            if conn.hostname in self.connected_connections:
                self.disconnect_host(conn.hostname)
            
            # Remove from list
            self.connections.remove(conn)
            self.save_connections()
            self.refresh_connection_list()
            print(f"=== FRESH: Deleted connection: {conn.name} ===")
        else:
            print(f"=== FRESH: Cancelled deletion of {conn.name} ===")
        
        dialog.destroy()
    
    def show_edit_dialog(self, conn):
        """Show edit connection dialog"""
        print(f"=== FRESH: Showing edit dialog for {conn.name} ===")
        dialog = EditConnectionDialog(self.window, conn, self.detect_ssh_keys())
        dialog.connect('close-request', self.on_edit_dialog_close, conn)
        dialog.present()
    
    def on_edit_dialog_close(self, dialog, conn):
        """Handle edit dialog close"""
        response = getattr(dialog, 'response', None)
        print(f"=== FRESH: Edit dialog response: {response} ===")
        if response == Gtk.ResponseType.OK:
            conn_data = dialog.get_connection_data()
            
            # Update connection properties
            conn.name = conn_data['name']
            conn.hostname = conn_data['hostname']
            conn.username = conn_data['username']
            conn.port = conn_data['port']
            conn.password = conn_data.get('password')
            conn.key_path = conn_data.get('key_path')
            conn.key_passphrase = conn_data.get('key_passphrase')
            
            self.save_connections()
            self.refresh_connection_list()
            print(f"=== FRESH: Updated connection: {conn.name} ===")
    
    def update_button_states_for_connection(self, hostname):
        """Update button states when connection status changes"""
        # Find the connection in the list and update buttons if it's selected
        selected_row = self.connection_listbox.get_selected_row()
        if selected_row and hasattr(selected_row, 'connection'):
            conn = selected_row.connection
            if conn.hostname == hostname:
                # This connection is selected, update button states
                print(f"=== FRESH: Updating button states for connected {hostname} ===")
                self.on_connection_selected(self.connection_listbox, selected_row)
                
                # Also refresh the connection list to update visual indicators
                self.refresh_connection_list()

    def setup_terminal_mouse_support(self, terminal):
        """Setup mouse copy/paste support for terminal"""
        # Enable right-click context menu
        right_click = Gtk.GestureClick()
        right_click.set_button(3)  # Right mouse button
        right_click.connect('pressed', self.on_terminal_right_click, terminal)
        terminal.add_controller(right_click)
        
        # Enable middle-click paste
        middle_click = Gtk.GestureClick()
        middle_click.set_button(2)  # Middle mouse button
        middle_click.connect('pressed', self.on_terminal_middle_click, terminal)
        terminal.add_controller(middle_click)
        
        print("=== FRESH: Mouse copy/paste support enabled for terminal ===")
    
    def on_terminal_right_click(self, gesture, n_press, x, y, terminal):
        """Handle right-click on terminal - show context menu"""
        print("=== FRESH: Terminal right-click detected ===")
        
        # Create context menu
        popover = Gtk.Popover()
        popover.set_parent(terminal)
        
        # Menu container
        menu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        menu_box.set_margin_start(6)
        menu_box.set_margin_end(6)
        menu_box.set_margin_top(6)
        menu_box.set_margin_bottom(6)
        
        # Copy button
        copy_btn = Gtk.Button(label="Copy")
        copy_btn.add_css_class("flat")
        copy_btn.set_halign(Gtk.Align.START)
        copy_btn.connect('clicked', self.on_terminal_copy, popover, terminal)
        menu_box.append(copy_btn)
        
        # Paste button
        paste_btn = Gtk.Button(label="Paste")
        paste_btn.add_css_class("flat")
        paste_btn.set_halign(Gtk.Align.START)
        paste_btn.connect('clicked', self.on_terminal_paste, popover, terminal)
        menu_box.append(paste_btn)
        
        # Separator
        separator = Gtk.Separator()
        separator.set_margin_top(3)
        separator.set_margin_bottom(3)
        menu_box.append(separator)
        
        # Select All button
        select_all_btn = Gtk.Button(label="Select All")
        select_all_btn.add_css_class("flat")
        select_all_btn.set_halign(Gtk.Align.START)
        select_all_btn.connect('clicked', self.on_terminal_select_all, popover, terminal)
        menu_box.append(select_all_btn)
        
        # Clear button
        clear_btn = Gtk.Button(label="Clear")
        clear_btn.add_css_class("flat")
        clear_btn.set_halign(Gtk.Align.START)
        clear_btn.connect('clicked', self.on_terminal_clear, popover, terminal)
        menu_box.append(clear_btn)
        
        popover.set_child(menu_box)
        
        # Set position at click location
        rect = Gdk.Rectangle()
        rect.x = x
        rect.y = y
        rect.width = 1
        rect.height = 1
        popover.set_pointing_to(rect)
        
        popover.popup()
    
    def on_terminal_middle_click(self, gesture, n_press, x, y, terminal):
        """Handle middle-click on terminal - paste from clipboard"""
        print("=== FRESH: Terminal middle-click paste ===")
        self.paste_to_terminal(terminal)
    
    def on_terminal_copy(self, button, popover, terminal):
        """Handle copy from terminal context menu"""
        print("=== FRESH: Terminal copy clicked ===")
        popover.popdown()
        self.copy_from_terminal(terminal)
    
    def on_terminal_paste(self, button, popover, terminal):
        """Handle paste to terminal context menu"""
        print("=== FRESH: Terminal paste clicked ===")
        popover.popdown()
        self.paste_to_terminal(terminal)
    
    def on_terminal_select_all(self, button, popover, terminal):
        """Handle select all in terminal"""
        print("=== FRESH: Terminal select all clicked ===")
        popover.popdown()
        terminal.select_all()
    
    def on_terminal_clear(self, button, popover, terminal):
        """Handle clear terminal"""
        print("=== FRESH: Terminal clear clicked ===")
        popover.popdown()
        terminal.reset(True, True)  # Clear screen and scrollback
    
    def copy_from_terminal(self, terminal):
        """Copy selected text from terminal to clipboard"""
        if terminal.get_has_selection():
            terminal.copy_clipboard()
            print("=== FRESH: Text copied to clipboard ===")
        else:
            print("=== FRESH: No text selected to copy ===")
    
    def paste_to_terminal(self, terminal):
        """Paste text from clipboard to terminal"""
        terminal.paste_clipboard()
        print("=== FRESH: Text pasted from clipboard ===")
    
    def show_settings_dialog(self):
        """Show settings dialog"""
        dialog = SettingsDialog(self.window, self.settings, self)  # Pass self as the app reference
        dialog.present()
    
    def on_settings_dialog_response(self, dialog, response):
        """Handle settings dialog response"""
        # Settings are applied in real-time, just clean up the dialog
        dialog.destroy()
    
    def update_setting(self, key, value):
        """Update a single setting and apply immediately"""
        self.settings[key] = value
        self.save_settings()
        self.apply_terminal_settings()
        print(f"=== FRESH: Updated setting {key} = {value} ===")
    
    def apply_terminal_settings(self):
        """Apply settings to all terminals"""
        print(f"=== FRESH: Applying settings to {self.notebook.get_n_pages()} tabs ===")
        # Apply to all existing terminals
        for i in range(self.notebook.get_n_pages()):
            page = self.notebook.get_nth_page(i)
            print(f"=== FRESH: Tab {i} type: {type(page)} ===")
            if isinstance(page, Vte.Terminal):
                print(f"=== FRESH: Configuring terminal {i} ===")
                self.configure_terminal_appearance(page)
            else:
                # The page might be a container, look for terminal inside
                def find_terminal(widget):
                    if isinstance(widget, Vte.Terminal):
                        print(f"=== FRESH: Found terminal in tab {i} ===")
                        self.configure_terminal_appearance(widget)
                        return True
                    # If it's a container, check its children
                    try:
                        child = widget.get_first_child()
                        while child:
                            if find_terminal(child):
                                return True
                            child = child.get_next_sibling()
                    except:
                        pass
                    return False
                find_terminal(page)
    
    def configure_terminal_appearance(self, terminal):
        """Configure terminal appearance based on settings"""
        print(f"=== FRESH: Configuring terminal with settings: {self.settings} ===")
        
        # Set font
        font_desc = Pango.FontDescription(f"{self.settings['font_family']} {self.settings['font_size']}")
        terminal.set_font(font_desc)
        print(f"=== FRESH: Set font: {self.settings['font_family']} {self.settings['font_size']} ===")
        
        # Set colors based on theme
        if self.settings['terminal_theme'] == 'dark':
            bg_color = Gdk.RGBA()
            bg_color.parse('#1e1e1e')
            fg_color = Gdk.RGBA()
            fg_color.parse('#ffffff')
            print("=== FRESH: Applied dark theme ===")
        elif self.settings['terminal_theme'] == 'light':
            bg_color = Gdk.RGBA()
            bg_color.parse('#ffffff')
            fg_color = Gdk.RGBA()
            fg_color.parse('#000000')
            print("=== FRESH: Applied light theme ===")
        elif self.settings['terminal_theme'] == 'custom':
            bg_color = Gdk.RGBA()
            bg_color.parse(self.settings['background_color'])
            fg_color = Gdk.RGBA()
            fg_color.parse(self.settings['foreground_color'])
            print(f"=== FRESH: Applied custom theme: {self.settings['background_color']}, {self.settings['foreground_color']} ===")
        else:  # default
            # Use default terminal colors
            bg_color = Gdk.RGBA()
            bg_color.parse('#000000')
            fg_color = Gdk.RGBA()
            fg_color.parse('#ffffff')
            print("=== FRESH: Applied default theme ===")
        
        terminal.set_colors(fg_color, bg_color, None)
        print("=== FRESH: Terminal appearance configured ===")
    
    def show_help_dialog(self):
        """Show help dialog"""
        help_text = """SSH Pilot - Easy SSH Connection Manager

SSH Pilot is a modern, user-friendly SSH connection manager that makes it easy to 
organize and connect to your remote servers.

🚀 Quick Start:
• Add connections using the "Add" button
• Double-click any connection to connect
• Use Ctrl+L to focus the connection list
• Right-click connections for more options

⌨️ Keyboard Shortcuts:
• Ctrl+L - Focus connection list
• Enter - Connect to selected server
• Arrow keys - Navigate connections
• Escape - Cancel dialogs

🔧 Features:
• Secure SSH key management
• Password and key-based authentication
• SSH key passphrase support
• Terminal themes and fonts
• Connection management
• Session persistence

Visit our GitHub repository for more information and updates!
"""
        
        dialog = Gtk.MessageDialog(
            transient_for=self.window,
            modal=True,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text=f"SSH Pilot Help\n\n{help_text}"
        )
        
        dialog.connect('response', lambda d, r: d.destroy())
        dialog.present()
    
    def show_about_dialog(self):
        """Show about dialog"""
        dialog = Gtk.AboutDialog()
        dialog.set_transient_for(self.window)
        dialog.set_modal(True)
        
        dialog.set_program_name("SSH Pilot")
        dialog.set_version("1.0")
        dialog.set_comments("Easy SSH Connection Manager")
        dialog.set_website("https://github.com/mfat/sshpilot")
        dialog.set_website_label("GitHub Repository")
        
        dialog.set_authors(["mFat"])
        dialog.set_copyright("© 2025 mFat")
        dialog.set_license_type(Gtk.License.GPL_3_0)
        
        # AboutDialog uses 'close-request' instead of 'response' in GTK4
        dialog.connect('close-request', lambda d: d.destroy())
        dialog.present()

    def disconnect_host(self, hostname):
        """Disconnect from host"""
        print(f"=== FRESH: Disconnecting from {hostname} ===")
        if hostname in self.connected_connections:
            conn_info = self.connected_connections[hostname]
            
            # Close terminal
            if conn_info['terminal']:
                try:
                    conn_info['terminal'].kill(Vte.TerminalExitStatus.NORMAL)
                except:
                    pass
            
            # Remove tab
            tab_index = self.find_tab_by_hostname(hostname)
            if tab_index is not None:
                self.notebook.remove_page(tab_index)
            
            # Remove from connected list
            del self.connected_connections[hostname]
            
            # Update button states after disconnection
            self.update_button_states_for_connection(hostname)
            
            # Show help screen if no connections remain
            if len(self.connected_connections) == 0:
                self.show_help_screen()
            
            print(f"=== FRESH: Disconnected from {hostname} ===")
    
    def show_help_screen(self):
        """Show help screen when no connections are active"""
        # Remove existing help tab if it exists
        self.remove_help_screen()
        
        # Create help content
        help_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        help_box.set_margin_start(40)
        help_box.set_margin_end(40)
        help_box.set_margin_top(40)
        help_box.set_margin_bottom(40)
        help_box.set_halign(Gtk.Align.CENTER)
        help_box.set_valign(Gtk.Align.CENTER)
        
        # Welcome title
        title_label = Gtk.Label()
        title_label.set_markup('<span size="x-large" weight="bold">Welcome to SSH Pilot</span>')
        title_label.set_halign(Gtk.Align.CENTER)
        help_box.append(title_label)
        
        # Instructions
        instructions_label = Gtk.Label()
        instructions_label.set_markup(
            '<span size="large">To get started:</span>\n\n'
            '1. Select a server from the list on the left\n'
            '2. Click "Connect" or press <b>Enter</b> to establish connection\n'
            '3. Use the terminal to interact with your server\n\n'
            '<i>If you don\'t have any servers configured, click "Add" to create your first connection.</i>'
        )
        instructions_label.set_halign(Gtk.Align.CENTER)
        instructions_label.set_justify(Gtk.Justification.CENTER)
        help_box.append(instructions_label)
        
        # Keyboard shortcuts section
        shortcuts_frame = Gtk.Frame()
        shortcuts_frame.set_label("Keyboard Shortcuts")
        shortcuts_frame.add_css_class("card")
        
        shortcuts_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        shortcuts_box.set_margin_start(20)
        shortcuts_box.set_margin_end(20)
        shortcuts_box.set_margin_top(15)
        shortcuts_box.set_margin_bottom(15)
        
        shortcuts = [
            ("Ctrl+L", "Focus connection list"),
            ("Enter", "Connect to selected server"),
            ("↑/↓ Arrow Keys", "Navigate server list"),
            ("Double-click", "Connect to server"),
            ("Right-click", "Context menu"),
            ("Ctrl+N", "Add new connection"),
            ("Ctrl+Q", "Quit application")
        ]
        
        for shortcut, description in shortcuts:
            shortcut_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            
            key_label = Gtk.Label()
            key_label.set_markup(f'<span font_family="monospace" weight="bold">{shortcut}</span>')
            key_label.set_size_request(120, -1)
            key_label.set_halign(Gtk.Align.START)
            shortcut_box.append(key_label)
            
            desc_label = Gtk.Label(label=description)
            desc_label.set_halign(Gtk.Align.START)
            shortcut_box.append(desc_label)
            
            shortcuts_box.append(shortcut_box)
        
        shortcuts_frame.set_child(shortcuts_box)
        help_box.append(shortcuts_frame)
        
        # Add help tab to notebook
        tab_label = Gtk.Label(label="Welcome")
        self.help_page_num = self.notebook.append_page(help_box, tab_label)
        self.notebook.set_current_page(self.help_page_num)
        
        print("=== FRESH: Help screen created and displayed ===")
    
    def remove_help_screen(self):
        """Remove help screen if it exists"""
        if hasattr(self, 'help_page_num') and self.help_page_num is not None:
            try:
                self.notebook.remove_page(self.help_page_num)
                self.help_page_num = None
                print("=== FRESH: Help screen removed ===")
            except Exception as e:
                print(f"=== FRESH: Error removing help screen: {e} ===")
    
    def find_tab_by_hostname(self, hostname):
        """Find tab index by hostname"""
        for i in range(self.notebook.get_n_pages()):
            page = self.notebook.get_nth_page(i)
            if hasattr(page, 'hostname') and page.hostname == hostname:
                return i
        return None
    
    def switch_to_connection_tab(self, conn):
        """Switch to existing tab for the connection"""
        print(f"=== FRESH: Looking for existing tab for {conn.name} ===")
        
        # Find and switch to existing tab
        for i in range(self.notebook.get_n_pages()):
            page = self.notebook.get_nth_page(i)
            if hasattr(page, 'hostname') and page.hostname == conn.hostname:
                print(f"=== FRESH: Found existing tab at page {i}, switching ===")
                self.notebook.set_current_page(i)
                
                # Set keyboard focus to the terminal for instant typing (with delay)
                GLib.timeout_add(50, self._set_terminal_focus, page, f"existing-{conn.name}")
                return True
        
        print(f"=== FRESH: No existing tab found for {conn.name} ===")
        return False
    
    def create_terminal_tab(self, conn):
        """Create a new terminal tab for the connection"""
        print(f"=== FRESH: Creating terminal tab for {conn.name} ===")
        
        # Check if already connected - switch to existing tab
        if conn.hostname in self.connected_connections:
            print(f"=== FRESH: Already connected to {conn.hostname}, switching to tab ===")
            if self.switch_to_connection_tab(conn):
                return
        
        # Create terminal
        terminal = Vte.Terminal()
        terminal.set_size_request(400, 300)
        
        # Configure terminal appearance
        self.configure_terminal_appearance(terminal)
        
        # Enable mouse copy/paste functionality
        self.setup_terminal_mouse_support(terminal)
        
        # Create tab label
        label = Gtk.Label(label=conn.name)
        
        # Remove help screen when first connection is made
        self.remove_help_screen()
        
        # Add to notebook
        page_num = self.notebook.append_page(terminal, label)
        self.notebook.set_current_page(page_num)
        
        # Store hostname reference
        terminal.hostname = conn.hostname
        
        # Connect SSH in background
        threading.Thread(target=self.connect_ssh, args=(conn, terminal), daemon=True).start()
        
        print(f"=== FRESH: Terminal tab created for {conn.name} ===")
    
    def connect_ssh(self, conn, terminal):
        """Connect to SSH host in background thread"""
        print(f"=== FRESH: Connecting SSH to {conn.hostname}:{conn.port} ===")
        try:
            # Handle SSH key with passphrase if needed
            if conn.key_path and conn.key_passphrase:
                print(f"=== FRESH: Adding SSH key with passphrase to agent: {conn.key_path} ===")
                self.add_key_to_agent(conn.key_path, conn.key_passphrase)
            
            # Build SSH command with port
            cmd = ['ssh', '-p', str(conn.port), f"{conn.username}@{conn.hostname}"]
            
            # Add SSH key if specified
            if conn.key_path:
                cmd.extend(['-i', conn.key_path])
                print(f"=== FRESH: Using SSH key: {conn.key_path} ===")
            
            print(f"=== FRESH: SSH command: {' '.join(cmd)} ===")
            
            # Connect terminal to SSH process
            GLib.idle_add(self._spawn_terminal, terminal, cmd, conn)
            
        except Exception as e:
            print(f"=== FRESH: Error connecting to {conn.hostname}: {e} ===")
    
    def add_key_to_agent(self, key_path, passphrase):
        """Add SSH key to ssh-agent with passphrase"""
        try:
            # Check if ssh-agent is running
            if 'SSH_AUTH_SOCK' not in os.environ:
                print("=== FRESH: SSH agent not running, key passphrase will be prompted during connection ===")
                return False
            
            # Check if the key is already loaded
            try:
                result = subprocess.run(['ssh-add', '-l'], capture_output=True, text=True, timeout=10)
                if result.returncode == 0 and key_path in result.stdout:
                    print(f"=== FRESH: Key {key_path} already loaded in SSH agent ===")
                    return True
            except Exception as e:
                print(f"=== FRESH: Error checking loaded keys: {e} ===")
            
            # Try using sshpass if available
            if self.is_command_available('sshpass'):
                try:
                    result = subprocess.run([
                        'sshpass', '-p', passphrase, 'ssh-add', key_path
                    ], capture_output=True, text=True, timeout=30)
                    
                    if result.returncode == 0:
                        print(f"=== FRESH: Successfully added key {key_path} to SSH agent using sshpass ===")
                        return True
                    else:
                        print(f"=== FRESH: sshpass failed: {result.stderr} ===")
                except Exception as e:
                    print(f"=== FRESH: Error using sshpass: {e} ===")
            
            # Try using expect if available
            if self.is_command_available('expect'):
                try:
                    expect_script = f'''#!/usr/bin/expect -f
set timeout 30
spawn ssh-add "{key_path}"
expect {{
    "Enter passphrase for*" {{
        send "{passphrase}\\r"
        expect {{
            "Identity added*" {{
                exit 0
            }}
            "Bad passphrase*" {{
                exit 1
            }}
            timeout {{
                exit 2
            }}
        }}
    }}
    "Could not open a connection to your authentication agent*" {{
        exit 3
    }}
    timeout {{
        exit 4
    }}
}}
'''
                    
                    # Write expect script to temporary file
                    import tempfile
                    with tempfile.NamedTemporaryFile(mode='w', suffix='.exp', delete=False) as f:
                        f.write(expect_script)
                        expect_file = f.name
                    
                    try:
                        # Make script executable
                        os.chmod(expect_file, 0o755)
                        
                        # Run expect script
                        result = subprocess.run([expect_file], capture_output=True, text=True, timeout=30)
                        
                        if result.returncode == 0:
                            print(f"=== FRESH: Successfully added key {key_path} to SSH agent using expect ===")
                            return True
                        else:
                            print(f"=== FRESH: expect failed with exit code: {result.returncode} ===")
                            
                    finally:
                        # Clean up temporary file
                        try:
                            os.unlink(expect_file)
                        except:
                            pass
                            
                except Exception as e:
                    print(f"=== FRESH: Error using expect: {e} ===")
            
            # If neither sshpass nor expect are available, inform user
            print("=== FRESH: Neither sshpass nor expect available. SSH will prompt for passphrase in terminal. ===")
            print("=== FRESH: Consider installing sshpass or expect for automatic passphrase handling. ===")
            return False
                    
        except Exception as e:
            print(f"=== FRESH: Error adding key to agent: {e} ===")
            return False
    
    def is_command_available(self, command):
        """Check if a command is available in the system"""
        try:
            result = subprocess.run(['which', command], capture_output=True, text=True, timeout=5)
            return result.returncode == 0
        except:
            return False
    
    def _spawn_terminal(self, terminal, cmd, conn):
        """Spawn terminal process on main thread"""
        try:
            print(f"=== FRESH: Spawning terminal for {conn.hostname} ===")
            # Spawn the terminal process with callback
            terminal.spawn_async(
                Vte.PtyFlags.DEFAULT,
                None,
                cmd,
                None,
                GLib.SpawnFlags.DEFAULT,
                None,
                None,
                -1,
                None,
                self._on_terminal_spawned,
                (terminal, conn.hostname)
            )
            
            # Mark as connected
            self.connected_connections[conn.hostname] = {'terminal': terminal}
            print(f"=== FRESH: Terminal spawned for {conn.hostname} ===")
            
            # Update button states after connection is established
            self.update_button_states_for_connection(conn.hostname)
            
            # Set keyboard focus to terminal for instant typing (with longer delay for new connections)
            GLib.timeout_add(500, self._set_terminal_focus, terminal, conn.hostname)
            
        except Exception as e:
            print(f"=== FRESH: Error spawning terminal for {conn.hostname}: {e} ===")
    
    def _on_terminal_spawned(self, terminal, pid, error, user_data):
        """Callback when terminal process is spawned"""
        terminal_obj, hostname = user_data
        if error:
            print(f"=== FRESH: Error spawning terminal for {hostname}: {error} ===")
        else:
            print(f"=== FRESH: Terminal process spawned successfully for {hostname}, PID: {pid} ===")
            # Set focus after the process is actually running
            GLib.timeout_add(1000, self._set_terminal_focus, terminal_obj, f"spawned-{hostname}")
    
    def on_tab_switched(self, notebook, page, page_num):
        """Handle tab switch to set keyboard focus"""
        print(f"=== FRESH: Tab switched to page {page_num} ===")
        try:
            # Get the current page (terminal) and set focus with delay
            current_page = notebook.get_nth_page(page_num)
            if current_page:
                GLib.timeout_add(50, self._set_terminal_focus, current_page, f"tab-{page_num}")
        except Exception as e:
            print(f"=== FRESH: Error setting focus on tab switch: {e} ===")
    
    def _set_terminal_focus(self, terminal, label):
        """Helper method to set terminal focus with better error handling"""
        try:
            print(f"=== FRESH: Setting keyboard focus for {label} ===")
            
            # Make sure the window has focus first and is active
            if self.window:
                self.window.present()
                
            # Make sure the terminal is realized and visible
            if terminal.get_realized() and terminal.get_visible():
                # Set can focus first
                terminal.set_can_focus(True)
                
                # Try to grab focus
                result = terminal.grab_focus()
                print(f"=== FRESH: grab_focus() result: {result} ===")
                
                # Also set as window focus
                if self.window:
                    self.window.set_focus(terminal)
                
                print(f"=== FRESH: Keyboard focus successfully set for {label} ===")
                
                # Verify focus
                if terminal.has_focus():
                    print(f"=== FRESH: Terminal has focus confirmed for {label} ===")
                else:
                    print(f"=== FRESH: Terminal focus not confirmed for {label} ===")
                    
            else:
                print(f"=== FRESH: Terminal not ready for {label}, retrying... ===")
                # Try again after a short delay
                return GLib.timeout_add(200, self._set_terminal_focus, terminal, label)
                
        except Exception as e:
            print(f"=== FRESH: Error setting focus for {label}: {e} ===")
        
        return False  # Don't repeat the timeout
    
    def on_window_close_request(self, window):
        """Handle window close request - warn if there are active connections"""
        print("=== FRESH: Window close requested ===")
        
        # Count active connections
        active_count = len(self.connected_connections)
        print(f"=== FRESH: Found {active_count} active connections ===")
        
        if active_count > 0:
            # Show warning dialog
            dialog = Gtk.MessageDialog(
                transient_for=window,
                modal=True,
                message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.YES_NO,
                text="Warning: Active SSH Connections"
            )
            
            if active_count == 1:
                dialog.set_property("secondary-text", 
                    "There is 1 active SSH connection. Closing the application will terminate this connection.\n\nAre you sure you want to exit?")
            else:
                dialog.set_property("secondary-text", 
                    f"There are {active_count} active SSH connections. Closing the application will terminate all connections.\n\nAre you sure you want to exit?")
            
            dialog.connect('response', self.on_exit_dialog_response)
            dialog.present()
            
            # Prevent window from closing until user responds
            return True
        else:
            print("=== FRESH: No active connections, allowing exit ===")
            # Allow window to close normally
            return False
    
    def on_exit_dialog_response(self, dialog, response):
        """Handle exit confirmation dialog response"""
        print(f"=== FRESH: Exit dialog response: {response} ===")
        
        if response == Gtk.ResponseType.YES:
            print("=== FRESH: User confirmed exit, closing all connections ===")
            # Close all active connections
            for hostname in list(self.connected_connections.keys()):
                self.disconnect_host(hostname)
            
            # Close the window
            self.window.close()
        else:
            print("=== FRESH: User cancelled exit ===")
        
        dialog.destroy()
    
    def run(self):
        print("=== FRESH: Starting application... ===")
        result = self.app.run(None)
        print(f"=== FRESH: Application finished with result: {result} ===")
        return result

class AddConnectionDialog(Gtk.Window):
    """Compact modern dialog for adding connections with passphrase support"""
    
    def __init__(self, parent, available_keys):
        super().__init__(title="Add Connection", transient_for=parent, modal=True)
        self.set_default_size(520, 580)  # Increased height significantly
        self.set_resizable(True)
        self.set_size_request(450, 500)  # Increased minimum size
        
        self.response = None
        
        # Header bar with buttons
        header_bar = Gtk.HeaderBar()
        header_bar.set_title_widget(Gtk.Label(label="Add Connection"))
        
        # Cancel button (left side)
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.add_css_class("destructive-action")
        cancel_btn.connect('clicked', self.on_cancel_clicked)
        header_bar.pack_start(cancel_btn)
        
        # Save button (right side)
        save_btn = Gtk.Button(label="Add")
        save_btn.add_css_class("suggested-action")
        save_btn.connect('clicked', self.on_save_clicked)
        header_bar.pack_end(save_btn)
        
        self.set_titlebar(header_bar)
        
        # Main scrollable content
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_margin_start(20)
        scrolled.set_margin_end(20)
        scrolled.set_margin_top(16)
        scrolled.set_margin_bottom(16)
        self.set_child(scrolled)
        
        # Compact form in a grid
        grid = Gtk.Grid()
        grid.set_row_spacing(12)
        grid.set_column_spacing(12)
        grid.set_hexpand(True)
        scrolled.set_child(grid)
        
        row = 0
        
        # Connection section
        section_label = Gtk.Label(label="Connection Details")
        section_label.add_css_class("heading")
        section_label.set_halign(Gtk.Align.START)
        section_label.set_margin_top(8)
        section_label.set_margin_bottom(4)
        grid.attach(section_label, 0, row, 2, 1)
        row += 1
        
        # Name
        self.name_entry = self.add_grid_entry(grid, row, "Name:", "My Server")
        row += 1
        
        # Hostname
        self.hostname_entry = self.add_grid_entry(grid, row, "Host:", "example.com")
        row += 1
        
        # Username and Port (side by side)
        user_label = Gtk.Label(label="User:")
        user_label.set_halign(Gtk.Align.START)
        grid.attach(user_label, 0, row, 1, 1)
        
        self.username_entry = Gtk.Entry()
        self.username_entry.set_text("user")
        self.username_entry.set_hexpand(True)
        grid.attach(self.username_entry, 1, row, 1, 1)
        row += 1
        
        port_label = Gtk.Label(label="Port:")
        port_label.set_halign(Gtk.Align.START)
        grid.attach(port_label, 0, row, 1, 1)
        
        self.port_entry = Gtk.Entry()
        self.port_entry.set_text("22")
        self.port_entry.set_width_chars(8)
        grid.attach(self.port_entry, 1, row, 1, 1)
        row += 1
        
        # Authentication section
        auth_section_label = Gtk.Label(label="Authentication")
        auth_section_label.add_css_class("heading")
        auth_section_label.set_halign(Gtk.Align.START)
        auth_section_label.set_margin_top(16)
        auth_section_label.set_margin_bottom(4)
        grid.attach(auth_section_label, 0, row, 2, 1)
        row += 1
        
        # Auth method
        method_label = Gtk.Label(label="Method:")
        method_label.set_halign(Gtk.Align.START)
        grid.attach(method_label, 0, row, 1, 1)
        
        self.auth_combo = Gtk.ComboBoxText()
        self.auth_combo.append("password", "Password")
        self.auth_combo.append("key", "SSH Key")
        self.auth_combo.set_active_id("password")
        self.auth_combo.connect('changed', self.on_auth_changed)
        self.auth_combo.set_hexpand(True)
        grid.attach(self.auth_combo, 1, row, 1, 1)
        row += 1
        
        # Password entry
        self.password_label = Gtk.Label(label="Password:")
        self.password_label.set_halign(Gtk.Align.START)
        grid.attach(self.password_label, 0, row, 1, 1)
        
        self.password_entry = Gtk.PasswordEntry()
        self.password_entry.set_hexpand(True)
        grid.attach(self.password_entry, 1, row, 1, 1)
        row += 1
        
        # SSH Key selection (initially hidden)
        self.key_label = Gtk.Label(label="SSH Key:")
        self.key_label.set_halign(Gtk.Align.START)
        self.key_label.set_visible(False)
        grid.attach(self.key_label, 0, row, 1, 1)
        
        self.key_combo = Gtk.ComboBoxText()
        self.key_combo.set_visible(False)
        self.key_combo.set_hexpand(True)
        
        # Populate SSH keys
        for key_path in available_keys:
            self.key_combo.append(key_path, os.path.basename(key_path))
        self.key_combo.append("browse", "Browse for key file...")
        
        if available_keys:
            self.key_combo.set_active(0)
        
        self.key_combo.connect('changed', self.on_key_combo_changed)
        grid.attach(self.key_combo, 1, row, 1, 1)
        row += 1
        
        # SSH Key Passphrase (initially hidden)
        self.passphrase_label = Gtk.Label(label="Key Passphrase:")
        self.passphrase_label.set_halign(Gtk.Align.START)
        grid.attach(self.passphrase_label, 0, row, 1, 1)
        
        self.passphrase_entry = Gtk.PasswordEntry()
        self.passphrase_entry.set_visible(False)
        self.passphrase_entry.set_hexpand(True)
        # Note: PasswordEntry doesn't support placeholder text in GTK4
        grid.attach(self.passphrase_entry, 1, row, 1, 1)
        row += 1
        
        # Add a helpful note label for passphrase
        self.passphrase_note = Gtk.Label(label="(Leave empty if key has no passphrase)")
        self.passphrase_note.set_halign(Gtk.Align.START)
        self.passphrase_note.add_css_class("dim-label")
        self.passphrase_note.set_margin_top(4)
        grid.attach(self.passphrase_note, 1, row, 1, 1)
        
        # Set initial visibility based on current authentication method
        self.on_auth_changed(self.auth_combo)
    
    def add_grid_entry(self, grid, row, label_text, default_value=""):
        """Add a label and entry to the grid"""
        label = Gtk.Label(label=label_text)
        label.set_halign(Gtk.Align.START)
        grid.attach(label, 0, row, 1, 1)
        
        entry = Gtk.Entry()
        entry.set_text(default_value)
        entry.set_hexpand(True)
        grid.attach(entry, 1, row, 1, 1)
        
        return entry
    
    def on_auth_changed(self, combo):
        """Handle authentication method change"""
        is_key = combo.get_active_id() == "key"
        
        # Toggle visibility for password fields
        self.password_label.set_visible(not is_key)
        self.password_entry.set_visible(not is_key)
        
        # Toggle visibility for SSH key fields
        self.key_label.set_visible(is_key)
        self.key_combo.set_visible(is_key)
        self.passphrase_label.set_visible(is_key)
        self.passphrase_entry.set_visible(is_key)
        self.passphrase_note.set_visible(is_key)  # Show/hide the note too
    
    def on_key_combo_changed(self, combo):
        """Handle SSH key combo selection change"""
        if combo.get_active_id() == "browse":
            dialog = Gtk.FileChooserDialog(
                title="Select SSH Key",
                transient_for=self,
                action=Gtk.FileChooserAction.OPEN
            )
            dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
            dialog.add_button("Select", Gtk.ResponseType.OK)
            
            ssh_dir = os.path.expanduser("~/.ssh")
            if os.path.exists(ssh_dir):
                dialog.set_current_folder(Gio.File.new_for_path(ssh_dir))
            
            dialog.connect('response', self.on_file_chooser_response)
            dialog.present()
    
    def on_file_chooser_response(self, dialog, response):
        """Handle file chooser response"""
        if response == Gtk.ResponseType.OK:
            file = dialog.get_file()
            if file:
                key_path = file.get_path()
                # Remove browse option, add new key, re-add browse
                self.key_combo.remove_all()
                self.key_combo.append(key_path, os.path.basename(key_path))
                self.key_combo.append("browse", "Browse for key file...")
                self.key_combo.set_active(0)
        dialog.destroy()
    
    def on_cancel_clicked(self, button):
        """Handle cancel button click"""
        self.response = Gtk.ResponseType.CANCEL
        self.close()
    
    def on_save_clicked(self, button):
        """Handle save button click"""
        self.response = Gtk.ResponseType.OK
        self.close()
    
    def get_connection_data(self):
        """Get the connection data from the form"""
        return {
            'name': self.name_entry.get_text().strip(),
            'hostname': self.hostname_entry.get_text().strip(),
            'username': self.username_entry.get_text().strip(),
            'port': int(self.port_entry.get_text().strip() or 22),
            'password': self.password_entry.get_text() if self.auth_combo.get_active_id() == "password" else "",
            'key_path': self.key_combo.get_active_id() if self.auth_combo.get_active_id() == "key" and self.key_combo.get_active_id() != "browse" else "",
            'key_passphrase': self.passphrase_entry.get_text() if self.auth_combo.get_active_id() == "key" else ""
        }

class EditConnectionDialog(Gtk.Window):
    """Compact modern dialog for editing connections with passphrase support"""
    
    def __init__(self, parent, connection, available_keys):
        super().__init__(title="Edit Connection", transient_for=parent, modal=True)
        self.connection = connection
        self.set_default_size(520, 580)  # Increased height significantly
        self.set_resizable(True)
        self.set_size_request(450, 500)  # Increased minimum size
        
        self.response = None
        
        # Header bar with buttons
        header_bar = Gtk.HeaderBar()
        header_bar.set_title_widget(Gtk.Label(label=f"Edit - {connection.name}"))
        
        # Cancel button (left side)
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.add_css_class("destructive-action")
        cancel_btn.connect('clicked', self.on_cancel_clicked)
        header_bar.pack_start(cancel_btn)
        
        # Save button (right side)
        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("suggested-action")
        save_btn.connect('clicked', self.on_save_clicked)
        header_bar.pack_end(save_btn)
        
        self.set_titlebar(header_bar)
        
        # Main scrollable content
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_margin_start(20)
        scrolled.set_margin_end(20)
        scrolled.set_margin_top(16)
        scrolled.set_margin_bottom(16)
        self.set_child(scrolled)
        
        # Compact form in a grid
        grid = Gtk.Grid()
        grid.set_row_spacing(12)
        grid.set_column_spacing(12)
        grid.set_hexpand(True)
        scrolled.set_child(grid)
        
        row = 0
        
        # Connection section
        section_label = Gtk.Label(label="Connection Details")
        section_label.add_css_class("heading")
        section_label.set_halign(Gtk.Align.START)
        section_label.set_margin_top(8)
        section_label.set_margin_bottom(4)
        grid.attach(section_label, 0, row, 2, 1)
        row += 1
        
        # Name
        self.name_entry = self.add_grid_entry(grid, row, "Name:", connection.name)
        row += 1
        
        # Hostname
        self.hostname_entry = self.add_grid_entry(grid, row, "Host:", connection.hostname)
        row += 1
        
        # Username and Port
        user_label = Gtk.Label(label="User:")
        user_label.set_halign(Gtk.Align.START)
        grid.attach(user_label, 0, row, 1, 1)
        
        self.username_entry = Gtk.Entry()
        self.username_entry.set_text(connection.username)
        self.username_entry.set_hexpand(True)
        grid.attach(self.username_entry, 1, row, 1, 1)
        row += 1
        
        port_label = Gtk.Label(label="Port:")
        port_label.set_halign(Gtk.Align.START)
        grid.attach(port_label, 0, row, 1, 1)
        
        self.port_entry = Gtk.Entry()
        self.port_entry.set_text(str(connection.port))
        self.port_entry.set_width_chars(8)
        grid.attach(self.port_entry, 1, row, 1, 1)
        row += 1
        
        # Authentication section
        auth_section_label = Gtk.Label(label="Authentication")
        auth_section_label.add_css_class("heading")
        auth_section_label.set_halign(Gtk.Align.START)
        auth_section_label.set_margin_top(16)
        auth_section_label.set_margin_bottom(4)
        grid.attach(auth_section_label, 0, row, 2, 1)
        row += 1
        
        # Auth method
        method_label = Gtk.Label(label="Method:")
        method_label.set_halign(Gtk.Align.START)
        grid.attach(method_label, 0, row, 1, 1)
        
        self.auth_combo = Gtk.ComboBoxText()
        self.auth_combo.append("password", "Password")
        self.auth_combo.append("key", "SSH Key")
        
        # Set current method
        if connection.key_path:
            self.auth_combo.set_active_id("key")
        else:
            self.auth_combo.set_active_id("password")
        
        self.auth_combo.connect('changed', self.on_auth_changed)
        self.auth_combo.set_hexpand(True)
        grid.attach(self.auth_combo, 1, row, 1, 1)
        row += 1
        
        # Password entry
        self.password_label = Gtk.Label(label="Password:")
        self.password_label.set_halign(Gtk.Align.START)
        grid.attach(self.password_label, 0, row, 1, 1)
        
        self.password_entry = Gtk.PasswordEntry()
        self.password_entry.set_text(connection.password or "")
        self.password_entry.set_hexpand(True)
        grid.attach(self.password_entry, 1, row, 1, 1)
        row += 1
        
        # SSH Key selection
        self.key_label = Gtk.Label(label="SSH Key:")
        self.key_label.set_halign(Gtk.Align.START)
        grid.attach(self.key_label, 0, row, 1, 1)
        
        self.key_combo = Gtk.ComboBoxText()
        self.key_combo.set_hexpand(True)
        
        # Populate SSH keys
        for key_path in available_keys:
            self.key_combo.append(key_path, os.path.basename(key_path))
        self.key_combo.append("browse", "Browse for key file...")
        
        # Set current key
        if connection.key_path and connection.key_path in available_keys:
            self.key_combo.set_active_id(connection.key_path)
        elif available_keys:
            self.key_combo.set_active(0)
        
        self.key_combo.connect('changed', self.on_key_combo_changed)
        grid.attach(self.key_combo, 1, row, 1, 1)
        row += 1
        
        # SSH Key Passphrase
        self.passphrase_label = Gtk.Label(label="Key Passphrase:")
        self.passphrase_label.set_halign(Gtk.Align.START)
        grid.attach(self.passphrase_label, 0, row, 1, 1)
        
        self.passphrase_entry = Gtk.PasswordEntry()
        self.passphrase_entry.set_text(getattr(connection, 'key_passphrase', '') or "")
        self.passphrase_entry.set_hexpand(True)
        # Note: PasswordEntry doesn't support placeholder text in GTK4
        grid.attach(self.passphrase_entry, 1, row, 1, 1)
        row += 1
        
        # Add a helpful note label for passphrase
        self.passphrase_note = Gtk.Label(label="(Leave empty if key has no passphrase)")
        self.passphrase_note.set_halign(Gtk.Align.START)
        self.passphrase_note.add_css_class("dim-label")
        self.passphrase_note.set_margin_top(4)
        grid.attach(self.passphrase_note, 1, row, 1, 1)
        
        # Set initial visibility based on current authentication method
        self.on_auth_changed(self.auth_combo)
    
    def add_grid_entry(self, grid, row, label_text, default_value=""):
        """Add a label and entry to the grid"""
        label = Gtk.Label(label=label_text)
        label.set_halign(Gtk.Align.START)
        grid.attach(label, 0, row, 1, 1)
        
        entry = Gtk.Entry()
        entry.set_text(default_value)
        entry.set_hexpand(True)
        grid.attach(entry, 1, row, 1, 1)
        
        return entry
    
    def on_auth_changed(self, combo):
        """Handle authentication method change"""
        is_key = combo.get_active_id() == "key"
        
        # Toggle visibility for password fields
        self.password_label.set_visible(not is_key)
        self.password_entry.set_visible(not is_key)
        
        # Toggle visibility for SSH key fields
        self.key_label.set_visible(is_key)
        self.key_combo.set_visible(is_key)
        self.passphrase_label.set_visible(is_key)
        self.passphrase_entry.set_visible(is_key)
        self.passphrase_note.set_visible(is_key)  # Show/hide the note too
    
    def on_key_combo_changed(self, combo):
        """Handle SSH key combo selection change"""
        if combo.get_active_id() == "browse":
            dialog = Gtk.FileChooserDialog(
                title="Select SSH Key",
                transient_for=self,
                action=Gtk.FileChooserAction.OPEN
            )
            dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
            dialog.add_button("Select", Gtk.ResponseType.OK)
            
            ssh_dir = os.path.expanduser("~/.ssh")
            if os.path.exists(ssh_dir):
                dialog.set_current_folder(Gio.File.new_for_path(ssh_dir))
            
            dialog.connect('response', self.on_file_chooser_response)
            dialog.present()
    
    def on_file_chooser_response(self, dialog, response):
        """Handle file chooser response"""
        if response == Gtk.ResponseType.OK:
            file = dialog.get_file()
            if file:
                key_path = file.get_path()
                # Remove browse option, add new key, re-add browse
                self.key_combo.remove_all()
                self.key_combo.append(key_path, os.path.basename(key_path))
                self.key_combo.append("browse", "Browse for key file...")
                self.key_combo.set_active(0)
        dialog.destroy()
    
    def on_cancel_clicked(self, button):
        """Handle cancel button click"""
        self.response = Gtk.ResponseType.CANCEL
        self.close()
    
    def on_save_clicked(self, button):
        """Handle save button click"""
        self.response = Gtk.ResponseType.OK
        self.close()
    
    def get_connection_data(self):
        """Get the connection data from the form"""
        return {
            'name': self.name_entry.get_text().strip(),
            'hostname': self.hostname_entry.get_text().strip(),
            'username': self.username_entry.get_text().strip(),
            'port': int(self.port_entry.get_text().strip() or 22),
            'password': self.password_entry.get_text() if self.auth_combo.get_active_id() == "password" else "",
            'key_path': self.key_combo.get_active_id() if self.auth_combo.get_active_id() == "key" and self.key_combo.get_active_id() != "browse" else "",
            'key_passphrase': self.passphrase_entry.get_text() if self.auth_combo.get_active_id() == "key" else ""
        }

class SettingsDialog(Gtk.Window):
    """Settings window for terminal themes and fonts with real-time updates"""
    
    def __init__(self, parent, current_settings, app_instance):
        super().__init__(title="Settings")
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(480, 400)
        self.set_resizable(False)
        
        self.current_settings = current_settings.copy()
        self.parent_app = app_instance  # Keep reference to app for real-time updates
        
        # Create main container with close button
        header_bar = Gtk.HeaderBar()
        header_bar.set_title_widget(Gtk.Label(label="Settings"))
        self.set_titlebar(header_bar)
        
        # Create main container
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        main_box.set_margin_start(24)
        main_box.set_margin_end(24)
        main_box.set_margin_top(20)
        main_box.set_margin_bottom(20)
        
        self.set_child(main_box)
        
        # Terminal section
        terminal_frame = self.create_section(main_box, "Terminal Appearance")
        
        # Theme selection
        theme_label = Gtk.Label(label="Theme:")
        theme_label.set_halign(Gtk.Align.START)
        theme_label.set_margin_bottom(6)
        terminal_frame.append(theme_label)
        
        self.theme_combo = Gtk.ComboBoxText()
        self.theme_combo.append_text("Default")
        self.theme_combo.append_text("Dark")
        self.theme_combo.append_text("Light")
        self.theme_combo.append_text("Custom")
        
        # Map settings to indices
        self.theme_map = {'default': 0, 'dark': 1, 'light': 2, 'custom': 3}
        self.theme_names = ['default', 'dark', 'light', 'custom']
        self.theme_combo.set_active(self.theme_map.get(current_settings['terminal_theme'], 0))
        self.theme_combo.connect('changed', self.on_theme_changed)
        self.theme_combo.set_margin_bottom(12)
        terminal_frame.append(self.theme_combo)
        
        # Custom colors section (initially hidden, shown only for custom theme)
        # Create a container that includes both title and content so we can hide everything
        self.colors_section_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        main_box.append(self.colors_section_container)
        
        # Add spacing before section
        spacer = Gtk.Box()
        spacer.set_size_request(-1, 16)
        self.colors_section_container.append(spacer)
        
        # Section title
        colors_title_label = Gtk.Label(label="Custom Colors")
        colors_title_label.set_halign(Gtk.Align.START)
        colors_title_label.add_css_class("heading")
        colors_title_label.set_margin_bottom(8)
        self.colors_section_container.append(colors_title_label)
        
        # Section content container
        self.colors_frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.colors_frame.set_margin_start(12)
        self.colors_section_container.append(self.colors_frame)
        
        # Background color
        bg_color_label = Gtk.Label(label="Background Color:")
        bg_color_label.set_halign(Gtk.Align.START)
        bg_color_label.set_margin_bottom(6)
        self.colors_frame.append(bg_color_label)
        
        self.bg_color_button = Gtk.ColorButton()
        bg_rgba = Gdk.RGBA()
        bg_rgba.parse(current_settings['background_color'])
        self.bg_color_button.set_rgba(bg_rgba)
        self.bg_color_button.set_title("Choose Background Color")
        self.bg_color_button.connect('color-set', self.on_bg_color_changed)
        self.bg_color_button.set_margin_bottom(12)
        self.colors_frame.append(self.bg_color_button)
        
        # Foreground color
        fg_color_label = Gtk.Label(label="Text Color:")
        fg_color_label.set_halign(Gtk.Align.START)
        fg_color_label.set_margin_bottom(6)
        self.colors_frame.append(fg_color_label)
        
        self.fg_color_button = Gtk.ColorButton()
        fg_rgba = Gdk.RGBA()
        fg_rgba.parse(current_settings['foreground_color'])
        self.fg_color_button.set_rgba(fg_rgba)
        self.fg_color_button.set_title("Choose Text Color")
        self.fg_color_button.connect('color-set', self.on_fg_color_changed)
        self.fg_color_button.set_margin_bottom(12)
        self.colors_frame.append(self.fg_color_button)
        
        # Font section
        font_frame = self.create_section(main_box, "Font Settings")
        
        # Font family
        font_family_label = Gtk.Label(label="Font Family:")
        font_family_label.set_halign(Gtk.Align.START)
        font_family_label.set_margin_bottom(6)
        font_frame.append(font_family_label)
        
        self.font_family_combo = Gtk.ComboBoxText()
        self.fonts = ["Monospace", "Ubuntu Mono", "Courier New", "DejaVu Sans Mono", "Liberation Mono"]
        for font in self.fonts:
            self.font_family_combo.append_text(font)
        
        # Set active font
        try:
            font_index = self.fonts.index(current_settings['font_family'])
            self.font_family_combo.set_active(font_index)
        except ValueError:
            self.font_family_combo.set_active(0)  # Default to first font
        
        self.font_family_combo.connect('changed', self.on_font_family_changed)
        self.font_family_combo.set_margin_bottom(12)
        font_frame.append(self.font_family_combo)
        
        # Font size
        font_size_label = Gtk.Label(label="Font Size:")
        font_size_label.set_halign(Gtk.Align.START)
        font_size_label.set_margin_bottom(6)
        font_frame.append(font_size_label)
        
        self.font_size_spin = Gtk.SpinButton()
        self.font_size_spin.set_range(8, 24)
        self.font_size_spin.set_increments(1, 1)
        self.font_size_spin.set_value(current_settings['font_size'])
        self.font_size_spin.connect('value-changed', self.on_font_size_changed)
        self.font_size_spin.set_margin_bottom(12)
        font_frame.append(self.font_size_spin)
        
        # Color preview section
        preview_frame = self.create_section(main_box, "Preview")
        
        self.preview_label = Gtk.Label()
        self.preview_label.set_markup('<span font_family="monospace" size="large">Sample Terminal Text</span>')
        self.preview_label.set_margin_top(8)
        self.preview_label.set_margin_bottom(8)
        self.preview_label.set_margin_start(12)
        self.preview_label.set_margin_end(12)
        preview_frame.append(self.preview_label)
        
        # Update preview
        self.update_preview()
        
        # Initial visibility
        self.on_theme_changed(self.theme_combo)
    
    def create_section(self, parent, title):
        """Create a section with title and frame"""
        # Add spacing before section (except first one)
        if parent.get_first_child():
            spacer = Gtk.Box()
            spacer.set_size_request(-1, 16)
            parent.append(spacer)
        
        # Section title
        title_label = Gtk.Label(label=title)
        title_label.set_halign(Gtk.Align.START)
        title_label.add_css_class("heading")
        title_label.set_margin_bottom(8)
        parent.append(title_label)
        
        # Section content container
        section_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        section_box.set_margin_start(12)
        parent.append(section_box)
        
        return section_box
    
    def on_theme_changed(self, combo):
        """Handle theme change"""
        theme_index = combo.get_active()
        theme = self.theme_names[theme_index] if theme_index >= 0 else 'default'
        self.colors_section_container.set_visible(theme == "custom")
        
        # Update setting in real-time
        self.parent_app.update_setting('terminal_theme', theme)
        self.update_preview()
    
    def on_font_family_changed(self, combo):
        """Handle font family change"""
        font_index = combo.get_active()
        font_family = self.fonts[font_index] if font_index >= 0 else "Monospace"
        
        # Update setting in real-time
        self.parent_app.update_setting('font_family', font_family)
        self.update_preview()
    
    def on_font_size_changed(self, spin_button):
        """Handle font size change"""
        font_size = int(spin_button.get_value())
        
        # Update setting in real-time
        self.parent_app.update_setting('font_size', font_size)
        self.update_preview()
    
    def on_bg_color_changed(self, color_button):
        """Handle background color change"""
        rgba = color_button.get_rgba()
        color_string = rgba.to_string()
        
        # Update setting in real-time
        self.parent_app.update_setting('background_color', color_string)
        self.update_preview()
    
    def on_fg_color_changed(self, color_button):
        """Handle foreground color change"""
        rgba = color_button.get_rgba()
        color_string = rgba.to_string()
        
        # Update setting in real-time
        self.parent_app.update_setting('foreground_color', color_string)
        self.update_preview()
    
    def update_preview(self):
        """Update the color preview"""
        theme = self.theme_names[self.theme_combo.get_active()]
        font_family = self.fonts[self.font_family_combo.get_active()]
        font_size = int(self.font_size_spin.get_value())
        
        # Get colors based on theme
        if theme == 'dark':
            bg_color = '#1e1e1e'
            fg_color = '#ffffff'
        elif theme == 'light':
            bg_color = '#ffffff'
            fg_color = '#000000'
        elif theme == 'custom':
            bg_rgba = self.bg_color_button.get_rgba()
            fg_rgba = self.fg_color_button.get_rgba()
            bg_color = bg_rgba.to_string()
            fg_color = fg_rgba.to_string()
        else:  # default
            bg_color = '#000000'
            fg_color = '#ffffff'
        
        # Update preview label
        markup = f'<span font_family="{font_family}" size="{font_size * 1000}" background="{bg_color}" foreground="{fg_color}">  Sample Terminal Text  </span>'
        self.preview_label.set_markup(markup)
    



def main():
    print("=== FRESH: main() called ===")
    app = FreshSSHManager()
    app.run()

if __name__ == "__main__":
    print("=== FRESH: Script started ===")
    main()
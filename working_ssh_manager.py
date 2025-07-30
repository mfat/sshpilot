#!/usr/bin/env python3
"""
Working SSH Manager - Clean version with all features
"""

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Vte', '3.91')
gi.require_version('Gdk', '4.0')

from gi.repository import Gtk, Gdk, Vte, GLib, Gio
import json
import os
import threading
from pathlib import Path
import subprocess

class SSHConnection:
    def __init__(self, hostname, username, port=22, password=None, key_path=None, name=None):
        self.hostname = hostname
        self.username = username
        self.port = port
        self.password = password
        self.key_path = key_path
        self.name = name or f"{username}@{hostname}"
        self.connected = False
        self.terminal = None
        self.process = None

class SSHManager:
    def __init__(self):
        print("Initializing SSH Manager...")
        self.connections = []
        self.connected_connections = {}
        self.config_file = os.path.expanduser("~/.ssh_manager_config.json")
        self.load_connections()
        
        # Initialize GTK
        self.app = Gtk.Application(application_id="com.example.sshmanager")
        self.app.connect('activate', self.on_activate)
        
        # Main window
        self.window = None
        self.connection_listbox = None
        self.notebook = None
        self.add_button = None
        self.connect_button = None
        self.disconnect_button = None
        self.rename_button = None
        self.delete_button = None
        
    def load_connections(self):
        """Load saved connections from config file"""
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
                            name=conn_data.get('name')
                        )
                        self.connections.append(conn)
            except Exception as e:
                print(f"Error loading connections: {e}")
    
    def save_connections(self):
        """Save connections to config file"""
        data = {
            'connections': []
        }
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
            data['connections'].append(conn_data)
        
        try:
            with open(self.config_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"Error saving connections: {e}")
    
    def detect_ssh_keys(self):
        """Detect available SSH keys in ~/.ssh/"""
        ssh_dir = Path.home() / ".ssh"
        keys = []
        
        if ssh_dir.exists():
            for key_file in ssh_dir.glob("*"):
                if key_file.is_file() and not key_file.name.endswith('.pub'):
                    keys.append(str(key_file))
        
        return keys
    
    def load_css(self):
        """Load CSS styling"""
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
            except Exception as e:
                print(f"Error loading CSS: {e}")
    
    def on_activate(self, app):
        """Initialize the main window"""
        print("Creating main window...")
        self.window = Gtk.ApplicationWindow(application=app, title="SSH Manager")
        self.window.set_default_size(1200, 800)
        
        # Load CSS styling
        self.load_css()
        
        # Main layout
        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.window.set_child(main_box)
        
        # Left panel for connection list
        left_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        left_panel.set_size_request(300, -1)
        main_box.append(left_panel)
        
        # Connection list
        list_label = Gtk.Label(label="Connections")
        list_label.set_margin_start(6)
        list_label.set_margin_end(6)
        list_label.set_margin_top(6)
        left_panel.append(list_label)
        
        # Scrolled window for connection list
        scrolled_window = Gtk.ScrolledWindow()
        scrolled_window.set_vexpand(True)
        left_panel.append(scrolled_window)
        
        # Connection list using ListBox
        print("Creating ListBox...")
        self.connection_listbox = Gtk.ListBox()
        self.connection_listbox.connect('row-selected', self.on_connection_selected)
        scrolled_window.set_child(self.connection_listbox)
        
        # Buttons
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        button_box.set_margin_start(6)
        button_box.set_margin_end(6)
        button_box.set_margin_bottom(6)
        left_panel.append(button_box)
        
        self.add_button = Gtk.Button(label="Add")
        self.add_button.connect('clicked', self.on_add_connection)
        button_box.append(self.add_button)
        
        self.connect_button = Gtk.Button(label="Connect")
        self.connect_button.connect('clicked', self.on_connect)
        self.connect_button.set_sensitive(False)
        button_box.append(self.connect_button)
        
        self.disconnect_button = Gtk.Button(label="Disconnect")
        self.disconnect_button.connect('clicked', self.on_disconnect)
        self.disconnect_button.set_sensitive(False)
        button_box.append(self.disconnect_button)
        
        # More buttons
        button_box2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        button_box2.set_margin_start(6)
        button_box2.set_margin_end(6)
        button_box2.set_margin_bottom(6)
        left_panel.append(button_box2)
        
        self.rename_button = Gtk.Button(label="Rename")
        self.rename_button.connect('clicked', self.on_rename_connection)
        self.rename_button.set_sensitive(False)
        button_box2.append(self.rename_button)
        
        self.delete_button = Gtk.Button(label="Delete")
        self.delete_button.connect('clicked', self.on_delete_connection)
        self.delete_button.set_sensitive(False)
        button_box2.append(self.delete_button)
        
        # Right panel for terminal
        right_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        right_panel.set_hexpand(True)
        main_box.append(right_panel)
        
        # Notebook for tabs
        self.notebook = Gtk.Notebook()
        self.notebook.set_vexpand(True)
        right_panel.append(self.notebook)
        
        # Load existing connections into list
        print("Loading connections...")
        self.refresh_connection_list()
        
        print("Presenting window...")
        self.window.present()
        print("Window should be visible now!")
    
    def on_connection_selected(self, listbox, row):
        """Handle connection selection"""
        if row:
            # Get the connection data from the row
            conn = row.get_data("connection")
            if conn:
                connected = conn.hostname in self.connected_connections
                
                self.connect_button.set_sensitive(not connected)
                self.disconnect_button.set_sensitive(connected)
                self.rename_button.set_sensitive(True)
                self.delete_button.set_sensitive(True)
            else:
                self.connect_button.set_sensitive(False)
                self.disconnect_button.set_sensitive(False)
                self.rename_button.set_sensitive(False)
                self.delete_button.set_sensitive(False)
        else:
            self.connect_button.set_sensitive(False)
            self.disconnect_button.set_sensitive(False)
            self.rename_button.set_sensitive(False)
            self.delete_button.set_sensitive(False)
    
    def refresh_connection_list(self):
        """Refresh the connection list display"""
        # Clear existing items
        while self.connection_listbox.get_first_child():
            self.connection_listbox.remove(self.connection_listbox.get_first_child())
        
        # Add connections
        for conn in self.connections:
            connected = conn.hostname in self.connected_connections
            
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
            if connected:
                name_label.add_css_class("connected")
                host_label.add_css_class("connected")
            
            row.set_child(box)
            row.set_data("connection", conn)
            
            self.connection_listbox.append(row)
    
    def get_selected_connection(self):
        """Get the currently selected connection"""
        row = self.connection_listbox.get_selected_row()
        if row:
            return row.get_data("connection")
        return None
    
    def on_add_connection(self, button):
        """Show add connection dialog"""
        dialog = AddConnectionDialog(self.window, self.detect_ssh_keys())
        dialog.connect('response', self.on_add_dialog_response)
        dialog.present()
    
    def on_add_dialog_response(self, dialog, response):
        """Handle add dialog response"""
        if response == Gtk.ResponseType.OK:
            conn_data = dialog.get_connection_data()
            conn = SSHConnection(
                hostname=conn_data['hostname'],
                username=conn_data['username'],
                port=conn_data['port'],
                password=conn_data.get('password'),
                key_path=conn_data.get('key_path'),
                name=conn_data['name']
            )
            self.connections.append(conn)
            self.save_connections()
            self.refresh_connection_list()
        
        dialog.destroy()
    
    def on_connect(self, button):
        """Connect to selected host"""
        conn = self.get_selected_connection()
        if conn and conn.hostname not in self.connected_connections:
            # Check if tab already exists
            existing_tab = self.find_tab_by_hostname(conn.hostname)
            if existing_tab:
                self.notebook.set_current_page(existing_tab)
                return
            
            # Create new terminal tab
            self.create_terminal_tab(conn)
    
    def on_disconnect(self, button):
        """Disconnect from selected host"""
        conn = self.get_selected_connection()
        if conn:
            self.disconnect_host(conn.hostname)
    
    def on_rename_connection(self, button):
        """Rename selected connection"""
        conn = self.get_selected_connection()
        if conn:
            dialog = RenameDialog(self.window, conn.name)
            dialog.connect('response', self.on_rename_dialog_response, conn)
            dialog.present()
    
    def on_rename_dialog_response(self, dialog, response, conn):
        """Handle rename dialog response"""
        if response == Gtk.ResponseType.OK:
            new_name = dialog.get_name()
            conn.name = new_name
            self.save_connections()
            self.refresh_connection_list()
        
        dialog.destroy()
    
    def on_delete_connection(self, button):
        """Delete selected connection"""
        conn = self.get_selected_connection()
        if conn:
            # Disconnect if connected
            if conn.hostname in self.connected_connections:
                self.disconnect_host(conn.hostname)
            
            # Remove from list
            self.connections.remove(conn)
            self.save_connections()
            self.refresh_connection_list()
    
    def find_tab_by_hostname(self, hostname):
        """Find tab index by hostname"""
        for i in range(self.notebook.get_n_pages()):
            page = self.notebook.get_nth_page(i)
            if hasattr(page, 'hostname') and page.hostname == hostname:
                return i
        return None
    
    def create_terminal_tab(self, conn):
        """Create a new terminal tab for the connection"""
        # Create terminal
        terminal = Vte.Terminal()
        terminal.set_size_request(400, 300)
        
        # Create tab label
        label = Gtk.Label(label=conn.name)
        label.set_margin_start(6)
        label.set_margin_end(6)
        
        # Create close button
        close_button = Gtk.Button()
        close_button.set_icon_name("window-close-symbolic")
        close_button.set_relief(Gtk.ReliefStyle.NONE)
        close_button.set_size_request(20, 20)
        close_button.connect('clicked', self.on_close_tab, conn.hostname)
        
        # Create tab header
        tab_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        tab_header.append(label)
        tab_header.append(close_button)
        
        # Add to notebook
        page_num = self.notebook.append_page(terminal, tab_header)
        self.notebook.set_current_page(page_num)
        
        # Store hostname reference
        terminal.hostname = conn.hostname
        
        # Connect in background thread
        threading.Thread(target=self.connect_ssh, args=(conn, terminal), daemon=True).start()
    
    def connect_ssh(self, conn, terminal):
        """Connect to SSH host in background thread"""
        try:
            # Build SSH command
            cmd = ['ssh']
            if conn.port != 22:
                cmd.extend(['-p', str(conn.port)])
            if conn.key_path:
                cmd.extend(['-i', conn.key_path])
            
            cmd.append(f"{conn.username}@{conn.hostname}")
            
            # Connect terminal to SSH process
            GLib.idle_add(self._spawn_terminal, terminal, cmd, conn)
            
        except Exception as e:
            print(f"Error connecting to {conn.hostname}: {e}")
    
    def _spawn_terminal(self, terminal, cmd, conn):
        """Spawn terminal process on main thread"""
        try:
            # Spawn the terminal process
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
                None
            )
            
            # Mark as connected
            self.connected_connections[conn.hostname] = {
                'terminal': terminal
            }
            
            # Update UI
            self.refresh_connection_list()
            
        except Exception as e:
            print(f"Error spawning terminal for {conn.hostname}: {e}")
    
    def disconnect_host(self, hostname):
        """Disconnect from host"""
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
            
            # Update UI
            self.refresh_connection_list()
    
    def on_close_tab(self, button, hostname):
        """Close terminal tab"""
        self.disconnect_host(hostname)
    
    def run(self):
        """Run the application"""
        print("Starting SSH Manager application...")
        return self.app.run(None)

class AddConnectionDialog(Gtk.Dialog):
    def __init__(self, parent, available_keys):
        super().__init__(title="Add Connection", transient_for=parent, modal=True)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("Add", Gtk.ResponseType.OK)
        
        self.set_default_size(400, 300)
        
        # Main content
        content_area = self.get_content_area()
        content_area.set_spacing(12)
        content_area.set_margin_start(12)
        content_area.set_margin_end(12)
        content_area.set_margin_top(12)
        content_area.set_margin_bottom(12)
        
        # Form fields
        self.name_entry = self.add_entry(content_area, "Connection Name:")
        self.hostname_entry = self.add_entry(content_area, "Hostname:")
        self.username_entry = self.add_entry(content_area, "Username:")
        self.port_entry = self.add_entry(content_area, "Port:", "22")
        
        # Authentication method
        auth_label = Gtk.Label(label="Authentication:")
        auth_label.set_halign(Gtk.Align.START)
        content_area.append(auth_label)
        
        self.auth_combo = Gtk.ComboBoxText()
        self.auth_combo.append("password", "Password")
        self.auth_combo.append("key", "SSH Key")
        self.auth_combo.set_active_id("password")
        self.auth_combo.connect('changed', self.on_auth_changed)
        content_area.append(self.auth_combo)
        
        # Password entry
        self.password_entry = Gtk.Entry()
        self.password_entry.set_visibility(False)
        self.password_entry.set_placeholder_text("Enter password")
        content_area.append(self.password_entry)
        
        # Key selection
        self.key_combo = Gtk.ComboBoxText()
        self.key_combo.append("", "Select SSH key...")
        for key in available_keys:
            self.key_combo.append(key, os.path.basename(key))
        self.key_combo.set_active_id("")
        self.key_combo.set_visible(False)
        content_area.append(self.key_combo)
        
        # Show/hide based on initial selection
        self.on_auth_changed(self.auth_combo)
    
    def add_entry(self, parent, label_text, default_value=""):
        """Add a labeled entry field"""
        label = Gtk.Label(label=label_text)
        label.set_halign(Gtk.Align.START)
        parent.append(label)
        
        entry = Gtk.Entry()
        entry.set_text(default_value)
        parent.append(entry)
        
        return entry
    
    def on_auth_changed(self, combo):
        """Handle authentication method change"""
        if combo.get_active_id() == "password":
            self.password_entry.set_visible(True)
            self.key_combo.set_visible(False)
        else:
            self.password_entry.set_visible(False)
            self.key_combo.set_visible(True)
    
    def get_connection_data(self):
        """Get connection data from form"""
        data = {
            'name': self.name_entry.get_text(),
            'hostname': self.hostname_entry.get_text(),
            'username': self.username_entry.get_text(),
            'port': int(self.port_entry.get_text() or "22")
        }
        
        if self.auth_combo.get_active_id() == "password":
            data['password'] = self.password_entry.get_text()
        else:
            data['key_path'] = self.key_combo.get_active_id()
        
        return data

class RenameDialog(Gtk.Dialog):
    def __init__(self, parent, current_name):
        super().__init__(title="Rename Connection", transient_for=parent, modal=True)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("Rename", Gtk.ResponseType.OK)
        
        self.set_default_size(300, 100)
        
        content_area = self.get_content_area()
        content_area.set_spacing(12)
        content_area.set_margin_start(12)
        content_area.set_margin_end(12)
        content_area.set_margin_top(12)
        content_area.set_margin_bottom(12)
        
        label = Gtk.Label(label="New name:")
        label.set_halign(Gtk.Align.START)
        content_area.append(label)
        
        self.name_entry = Gtk.Entry()
        self.name_entry.set_text(current_name)
        content_area.append(self.name_entry)
    
    def get_name(self):
        """Get the new name"""
        return self.name_entry.get_text()

def main():
    """Main entry point"""
    app = SSHManager()
    app.run()

if __name__ == "__main__":
    main() 
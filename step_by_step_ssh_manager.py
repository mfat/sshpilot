#!/usr/bin/env python3
"""
Step-by-step SSH Manager - Adding features gradually to identify the issue
"""

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Vte', '3.91')
gi.require_version('Gdk', '4.0')

from gi.repository import Gtk, Gdk, Vte, GLib, Gio
import json
import os
import threading
import paramiko
from pathlib import Path
import subprocess
import tempfile
import shutil

class SSHConnection:
    def __init__(self, hostname, username, port=22, password=None, key_path=None, name=None):
        self.hostname = hostname
        self.username = username
        self.port = port
        self.password = password
        self.key_path = key_path
        self.name = name or f"{username}@{hostname}"

class StepByStepSSHManager:
    def __init__(self):
        print("=== STEP: Initializing Step-by-Step SSH Manager ===")
        self.connections = []
        self.connected_connections = {}
        self.config_file = os.path.expanduser("~/.ssh_manager_config.json")
        
        # Initialize GTK
        self.app = Gtk.Application(application_id="com.example.step-ssh-manager")
        self.app.connect('activate', self.on_activate)
        print("=== STEP: Application created and signal connected ===")
        
        # Main window
        self.window = None
        self.connection_listbox = None
        self.notebook = None
        
    def load_connections(self):
        """Load saved connections from config file"""
        print("=== STEP: Loading connections ===")
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
                print(f"=== STEP: Loaded {len(self.connections)} connections ===")
            except Exception as e:
                print(f"=== STEP: Error loading connections: {e} ===")
    
    def detect_ssh_keys(self):
        """Detect available SSH keys in ~/.ssh/"""
        print("=== STEP: Detecting SSH keys ===")
        ssh_dir = Path.home() / ".ssh"
        keys = []
        
        if ssh_dir.exists():
            for key_file in ssh_dir.glob("*"):
                if key_file.is_file() and not key_file.name.endswith('.pub'):
                    keys.append(str(key_file))
        
        print(f"=== STEP: Found {len(keys)} SSH keys ===")
        return keys
    
    def on_activate(self, app):
        print("=== STEP: on_activate called! ===")
        
        # Create window
        self.window = Gtk.ApplicationWindow(application=app, title="Step-by-Step SSH Manager")
        self.window.set_default_size(1200, 800)
        print("=== STEP: Window created ===")
        
        # Main layout
        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.window.set_child(main_box)
        print("=== STEP: Main box created ===")
        
        # Left panel for connection list
        left_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        left_panel.set_size_request(300, -1)
        main_box.append(left_panel)
        print("=== STEP: Left panel created ===")
        
        # Connection list
        list_label = Gtk.Label(label="Connections")
        list_label.set_margin_start(6)
        list_label.set_margin_end(6)
        list_label.set_margin_top(6)
        left_panel.append(list_label)
        print("=== STEP: List label added ===")
        
        # Scrolled window for connection list
        scrolled_window = Gtk.ScrolledWindow()
        scrolled_window.set_vexpand(True)
        left_panel.append(scrolled_window)
        print("=== STEP: Scrolled window created ===")
        
        # Connection list using ListBox
        print("=== STEP: Creating ListBox... ===")
        self.connection_listbox = Gtk.ListBox()
        self.connection_listbox.connect('row-selected', self.on_connection_selected)
        scrolled_window.set_child(self.connection_listbox)
        print("=== STEP: ListBox created and connected ===")
        
        # Buttons
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        button_box.set_margin_start(6)
        button_box.set_margin_end(6)
        button_box.set_margin_bottom(6)
        left_panel.append(button_box)
        print("=== STEP: Button box created ===")
        
        add_button = Gtk.Button(label="Add")
        add_button.connect('clicked', self.on_add_connection)
        button_box.append(add_button)
        print("=== STEP: Add button created ===")
        
        connect_button = Gtk.Button(label="Connect")
        connect_button.connect('clicked', self.on_connect)
        button_box.append(connect_button)
        print("=== STEP: Connect button created ===")
        
        # Right panel for terminal
        right_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        right_panel.set_hexpand(True)
        main_box.append(right_panel)
        print("=== STEP: Right panel created ===")
        
        # Notebook for tabs
        self.notebook = Gtk.Notebook()
        self.notebook.set_vexpand(True)
        right_panel.append(self.notebook)
        print("=== STEP: Notebook created ===")
        
        # Load existing connections into list
        print("=== STEP: Loading connections... ===")
        self.load_connections()
        self.refresh_connection_list()
        print("=== STEP: Connections loaded and list refreshed ===")
        
        print("=== STEP: About to present window ===")
        self.window.present()
        print("=== STEP: Window presented! ===")
    
    def on_connection_selected(self, listbox, row):
        """Handle connection selection"""
        print("=== STEP: Connection selected ===")
        if row:
            conn = row.get_data("connection")
            if conn:
                print(f"=== STEP: Selected connection: {conn.name} ===")
    
    def refresh_connection_list(self):
        """Refresh the connection list display"""
        print("=== STEP: Refreshing connection list ===")
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
            
            row.set_child(box)
            row.set_data("connection", conn)
            
            self.connection_listbox.append(row)
        
        print(f"=== STEP: Added {len(self.connections)} connections to list ===")
    
    def on_add_connection(self, button):
        """Show add connection dialog"""
        print("=== STEP: Add button clicked ===")
        dialog = AddConnectionDialog(self.window, self.detect_ssh_keys())
        dialog.connect('response', self.on_add_dialog_response)
        dialog.present()
    
    def on_add_dialog_response(self, dialog, response):
        """Handle add dialog response"""
        print(f"=== STEP: Dialog response: {response} ===")
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
            self.refresh_connection_list()
            print(f"=== STEP: Added connection: {conn.name} ===")
        
        dialog.destroy()
    
    def on_connect(self, button):
        """Connect to selected host"""
        print("=== STEP: Connect button clicked ===")
    
    def run(self):
        print("=== STEP: Starting application... ===")
        result = self.app.run(None)
        print(f"=== STEP: Application finished with result: {result} ===")
        return result

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

def main():
    print("=== STEP: main() called ===")
    app = StepByStepSSHManager()
    app.run()

if __name__ == "__main__":
    print("=== STEP: Script started ===")
    main() 
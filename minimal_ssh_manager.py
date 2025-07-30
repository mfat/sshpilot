#!/usr/bin/env python3
"""
Minimal SSH Manager - Gradually adding features to identify the issue
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

class MinimalSSHManager:
    def __init__(self):
        print("=== MINIMAL: Initializing Minimal SSH Manager ===")
        self.connections = []
        self.connected_connections = {}
        self.config_file = os.path.expanduser("~/.ssh_manager_config.json")
        
        # Initialize GTK
        self.app = Gtk.Application(application_id="com.example.minimal-ssh-manager")
        self.app.connect('activate', self.on_activate)
        print("=== MINIMAL: Application created and signal connected ===")
        
        # Main window
        self.window = None
        
    def on_activate(self, app):
        print("=== MINIMAL: on_activate called! ===")
        
        # Create window
        self.window = Gtk.ApplicationWindow(application=app, title="Minimal SSH Manager")
        self.window.set_default_size(1200, 800)
        print("=== MINIMAL: Window created ===")
        
        # Main layout
        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.window.set_child(main_box)
        print("=== MINIMAL: Main box created ===")
        
        # Left panel
        left_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        left_panel.set_size_request(300, -1)
        main_box.append(left_panel)
        print("=== MINIMAL: Left panel created ===")
        
        # Title
        title_label = Gtk.Label(label="SSH Connections")
        title_label.set_margin_start(6)
        title_label.set_margin_end(6)
        title_label.set_margin_top(6)
        left_panel.append(title_label)
        print("=== MINIMAL: Title added ===")
        
        # Simple list
        list_box = Gtk.ListBox()
        list_box.set_margin_start(6)
        list_box.set_margin_end(6)
        left_panel.append(list_box)
        print("=== MINIMAL: List box created ===")
        
        # Add sample item
        row = Gtk.ListBoxRow()
        label = Gtk.Label(label="Sample Connection")
        label.set_margin_start(6)
        label.set_margin_end(6)
        label.set_margin_top(3)
        label.set_margin_bottom(3)
        row.set_child(label)
        list_box.append(row)
        print("=== MINIMAL: Sample item added ===")
        
        # Buttons
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        button_box.set_margin_start(6)
        button_box.set_margin_end(6)
        button_box.set_margin_bottom(6)
        left_panel.append(button_box)
        print("=== MINIMAL: Button box created ===")
        
        add_button = Gtk.Button(label="Add")
        add_button.connect('clicked', self.on_add_clicked)
        button_box.append(add_button)
        print("=== MINIMAL: Add button created ===")
        
        # Right panel
        right_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        right_panel.set_hexpand(True)
        main_box.append(right_panel)
        print("=== MINIMAL: Right panel created ===")
        
        # Terminal area
        terminal_label = Gtk.Label(label="Terminal Area")
        terminal_label.set_hexpand(True)
        terminal_label.set_vexpand(True)
        right_panel.append(terminal_label)
        print("=== MINIMAL: Terminal label added ===")
        
        print("=== MINIMAL: About to present window ===")
        self.window.present()
        print("=== MINIMAL: Window presented! ===")
    
    def on_add_clicked(self, button):
        print("=== MINIMAL: Add button clicked! ===")
    
    def run(self):
        print("=== MINIMAL: Starting application... ===")
        result = self.app.run(None)
        print(f"=== MINIMAL: Application finished with result: {result} ===")
        return result

def main():
    print("=== MINIMAL: main() called ===")
    app = MinimalSSHManager()
    app.run()

if __name__ == "__main__":
    print("=== MINIMAL: Script started ===")
    main() 
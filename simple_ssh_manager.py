#!/usr/bin/env python3
"""
Simple SSH Manager - Basic working version
"""

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Vte', '3.91')
from gi.repository import Gtk, Vte, GLib
import json
import os
from pathlib import Path

class SimpleSSHManager:
    def __init__(self):
        print("Initializing Simple SSH Manager...")
        
        # Create application
        self.app = Gtk.Application(application_id="com.example.simple-ssh-manager")
        self.app.connect('activate', self.on_activate)
        
        # Window
        self.window = None
        
    def on_activate(self, app):
        print("Creating window...")
        
        # Create window
        self.window = Gtk.ApplicationWindow(application=app, title="SSH Manager")
        self.window.set_default_size(800, 600)
        
        # Create main layout
        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.window.set_child(main_box)
        
        # Left panel
        left_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        left_panel.set_size_request(250, -1)
        main_box.append(left_panel)
        
        # Title
        title_label = Gtk.Label(label="SSH Connections")
        title_label.set_margin_start(6)
        title_label.set_margin_end(6)
        title_label.set_margin_top(6)
        left_panel.append(title_label)
        
        # Simple list
        list_box = Gtk.ListBox()
        list_box.set_margin_start(6)
        list_box.set_margin_end(6)
        left_panel.append(list_box)
        
        # Add some sample items
        sample_connections = [
            "server1.example.com",
            "server2.example.com", 
            "192.168.1.100"
        ]
        
        for conn in sample_connections:
            row = Gtk.ListBoxRow()
            label = Gtk.Label(label=conn)
            label.set_margin_start(6)
            label.set_margin_end(6)
            label.set_margin_top(3)
            label.set_margin_bottom(3)
            row.set_child(label)
            list_box.append(row)
        
        # Buttons
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        button_box.set_margin_start(6)
        button_box.set_margin_end(6)
        button_box.set_margin_bottom(6)
        left_panel.append(button_box)
        
        add_button = Gtk.Button(label="Add")
        add_button.connect('clicked', self.on_add_clicked)
        button_box.append(add_button)
        
        connect_button = Gtk.Button(label="Connect")
        connect_button.connect('clicked', self.on_connect_clicked)
        button_box.append(connect_button)
        
        # Right panel
        right_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        right_panel.set_hexpand(True)
        main_box.append(right_panel)
        
        # Terminal area
        terminal_label = Gtk.Label(label="Terminal Area")
        terminal_label.set_hexpand(True)
        terminal_label.set_vexpand(True)
        right_panel.append(terminal_label)
        
        print("Presenting window...")
        self.window.present()
        print("Window should be visible now!")
    
    def on_add_clicked(self, button):
        print("Add button clicked!")
        dialog = Gtk.Dialog(title="Add Connection", transient_for=self.window, modal=True)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Add", Gtk.ResponseType.OK)
        
        content_area = dialog.get_content_area()
        content_area.set_spacing(12)
        content_area.set_margin_start(12)
        content_area.set_margin_end(12)
        content_area.set_margin_top(12)
        content_area.set_margin_bottom(12)
        
        # Add some fields
        hostname_label = Gtk.Label(label="Hostname:")
        hostname_label.set_halign(Gtk.Align.START)
        content_area.append(hostname_label)
        
        hostname_entry = Gtk.Entry()
        hostname_entry.set_placeholder_text("Enter hostname")
        content_area.append(hostname_entry)
        
        dialog.present()
    
    def on_connect_clicked(self, button):
        print("Connect button clicked!")
    
    def run(self):
        print("Starting application...")
        return self.app.run(None)

def main():
    app = SimpleSSHManager()
    app.run()

if __name__ == "__main__":
    main() 
#!/usr/bin/env python3
"""
Test Vte import and basic functionality
"""

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Vte', '3.91')
from gi.repository import Gtk, Vte, GLib
import sys

class TestVteApp:
    def __init__(self):
        print("=== TEST: Initializing Test Vte App ===")
        
        # Create application
        self.app = Gtk.Application(application_id="com.example.test-vte")
        print("=== TEST: Application created ===")
        
        # Connect activate signal
        self.app.connect('activate', self.on_activate)
        print("=== TEST: Activate signal connected ===")
        
        # Window
        self.window = None
        
    def on_activate(self, app):
        print("=== TEST: on_activate called! ===")
        
        # Create window
        self.window = Gtk.ApplicationWindow(application=app, title="Test Vte")
        self.window.set_default_size(800, 600)
        print("=== TEST: Window created ===")
        
        # Create main layout
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.window.set_child(main_box)
        print("=== TEST: Main box created ===")
        
        # Create terminal
        try:
            terminal = Vte.Terminal()
            terminal.set_size_request(400, 300)
            main_box.append(terminal)
            print("=== TEST: Terminal created and added ===")
        except Exception as e:
            print(f"=== TEST ERROR: Failed to create terminal: {e} ===")
            # Add a label instead
            label = Gtk.Label(label="Terminal creation failed")
            main_box.append(label)
        
        # Show window
        print("=== TEST: About to present window ===")
        self.window.present()
        print("=== TEST: Window presented! ===")
    
    def run(self):
        print("=== TEST: Starting application... ===")
        result = self.app.run(None)
        print(f"=== TEST: Application finished with result: {result} ===")
        return result

def main():
    print("=== TEST: main() called ===")
    app = TestVteApp()
    app.run()

if __name__ == "__main__":
    print("=== TEST: Script started ===")
    main() 
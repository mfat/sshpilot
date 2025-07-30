#!/usr/bin/env python3
"""
Debug SSH Manager - To identify why on_activate is not called
"""

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Vte', '3.91')
from gi.repository import Gtk, Vte, GLib
import sys

class DebugSSHManager:
    def __init__(self):
        print("=== DEBUG: Initializing Debug SSH Manager ===")
        
        # Create application with explicit ID
        self.app = Gtk.Application(application_id="com.example.debug-ssh-manager")
        print("=== DEBUG: Application created ===")
        
        # Connect activate signal
        self.app.connect('activate', self.on_activate)
        print("=== DEBUG: Activate signal connected ===")
        
        # Window
        self.window = None
        
    def on_activate(self, app):
        print("=== DEBUG: on_activate called! ===")
        
        # Create window
        self.window = Gtk.ApplicationWindow(application=app, title="Debug SSH Manager")
        self.window.set_default_size(800, 600)
        print("=== DEBUG: Window created ===")
        
        # Create simple content
        label = Gtk.Label(label="SSH Manager is working!")
        self.window.set_child(label)
        print("=== DEBUG: Content added ===")
        
        # Show window
        print("=== DEBUG: About to present window ===")
        self.window.present()
        print("=== DEBUG: Window presented! ===")
    
    def run(self):
        print("=== DEBUG: Starting application... ===")
        print(f"=== DEBUG: Command line args: {sys.argv} ===")
        result = self.app.run(None)
        print(f"=== DEBUG: Application finished with result: {result} ===")
        return result

def main():
    print("=== DEBUG: main() called ===")
    app = DebugSSHManager()
    app.run()

if __name__ == "__main__":
    print("=== DEBUG: Script started ===")
    main() 
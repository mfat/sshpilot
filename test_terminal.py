#!/usr/bin/env python3
"""
Test script for the TerminalWidget with system SSH client
"""

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Gtk, GLib, Gio
import sys
import logging

# Add the src directory to the path
sys.path.append('.')
from src.sshpilot_pkg.github.mfat.sshpilot.terminal import TerminalWidget

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

class TestWindow(Gtk.ApplicationWindow):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.set_default_size(800, 600)
        self.set_title("SSH Terminal Test")
        
        # Create a connection dictionary
        connection = {
            'host': 'localhost',  # Change this to your test server
            'username': None,     # Will use current user
            'port': 22,          # Default SSH port
            'label': 'Test Connection',
            'id': 'test-conn-1'
        }
        
        # Create the terminal widget
        self.terminal = TerminalWidget(
            connection=connection,
            config={},  # Empty config for testing
            connection_manager=None  # Not needed for this test
        )
        
        # Add terminal to the window
        self.set_child(self.terminal)
        
        # Connect signals
        self.terminal.connect('connection-established', self.on_connection_established)
        self.terminal.connect('connection-failed', self.on_connection_failed)
        self.terminal.connect('connection-lost', self.on_connection_lost)
        
        logger.info("Test window initialized")
    
    def on_connection_established(self, widget):
        logger.info("Connection established!")
    
    def on_connection_failed(self, widget, error_msg):
        logger.error(f"Connection failed: {error_msg}")
        # Show error dialog
        dialog = Gtk.MessageDialog(
            transient_for=self,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text="Connection Failed"
        )
        dialog.format_secondary_text(str(error_msg))
        dialog.connect("response", lambda d, r: d.destroy())
        dialog.present()
    
    def on_connection_lost(self, widget):
        logger.info("Connection closed")

class TestApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id='io.github.mfat.sshpilot.test')
        self.window = None
    
    def do_activate(self):
        if not self.window:
            self.window = TestWindow(application=self)
        self.window.present()

if __name__ == '__main__':
    app = TestApp()
    exit_status = app.run(None)
    sys.exit(exit_status)

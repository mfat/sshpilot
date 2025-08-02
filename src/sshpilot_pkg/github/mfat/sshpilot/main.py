#!/usr/bin/env python3
"""
sshPilot - SSH connection manager with integrated terminal
Main application entry point
"""

import sys
import os
import logging
from logging.handlers import RotatingFileHandler

import gi
gi.require_version('Adw', '1')
gi.require_version('Gtk', '4.0')
gi.require_version('Vte', '3.91')

from gi.repository import Adw, Gtk, Gio, GLib

# Register resources before importing any UI modules
resource = Gio.Resource.load(
    os.path.join(os.path.dirname(__file__), 'resources', 'sshpilot.gresource')
)
Gio.resources_register(resource)

from .window import MainWindow

class SshPilotApplication(Adw.Application):
    """Main application class for sshPilot"""
    
    def __init__(self):
        super().__init__(
            application_id='io.github.mfat.sshpilot',
            flags=Gio.ApplicationFlags.FLAGS_NONE
        )
        
        # Set up logging
        self.setup_logging()
        
        # Create actions with keyboard shortcuts
        self.create_action('quit', self.quit, ['<primary>q'])
        self.create_action('new-connection', self.on_new_connection, ['<primary>n'])
        self.create_action('toggle-list', self.on_toggle_list, ['<primary>l'])
        self.create_action('new-key', self.on_new_key, ['<primary><shift>k'])
        self.create_action('show-resources', self.on_show_resources, ['<primary>r'])
        self.create_action('preferences', self.on_preferences, ['<primary>comma'])
        self.create_action('about', self.on_about)
        
        logging.info("sshPilot application initialized")

    def setup_logging(self):
        """Set up logging configuration"""
        # Create log directory if it doesn't exist
        log_dir = os.path.expanduser('~/.local/share/sshPilot')
        os.makedirs(log_dir, exist_ok=True)
        
        # Set log level based on environment variable
        log_level = logging.DEBUG if os.environ.get('SSHPILOT_DEBUG') else logging.INFO
        
        # Configure logging with file rotation
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                RotatingFileHandler(
                    os.path.join(log_dir, 'sshpilot.log'),
                    maxBytes=1024*1024,  # 1MB
                    backupCount=5
                ),
                logging.StreamHandler()
            ]
        )

    def create_action(self, name, callback, shortcuts=None):
        """Create a GAction with optional keyboard shortcuts"""
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)
        if shortcuts:
            self.set_accels_for_action(f"app.{name}", shortcuts)

    def quit(self, action=None, param=None):
        """Quit the application"""
        # Get the active window and check for active connections
        win = self.props.active_window
        if win and hasattr(win, 'check_active_connections_before_quit'):
            # Let the window handle the quit confirmation
            if not win.check_active_connections_before_quit():
                return  # User cancelled or there are active connections
        
        super().quit()

    def do_activate(self):
        """Called when the application is activated"""
        win = self.props.active_window
        if not win:
            win = MainWindow(application=self)
        win.present()

    def on_new_connection(self, action, param):
        """Handle new connection action"""
        logging.debug("New connection action triggered")
        if self.props.active_window:
            self.props.active_window.show_connection_dialog()

    def on_toggle_list(self, action, param):
        """Handle toggle list focus action"""
        logging.debug("Toggle list focus action triggered")
        if self.props.active_window:
            self.props.active_window.toggle_list_focus()

    def on_new_key(self, action, param):
        """Handle new SSH key action"""
        logging.debug("New SSH key action triggered")
        if self.props.active_window:
            self.props.active_window.show_key_dialog()

    def on_show_resources(self, action, param):
        """Handle show resources action"""
        logging.debug("Show resources action triggered")
        if self.props.active_window:
            self.props.active_window.show_resource_view()

    def on_preferences(self, action, param):
        """Handle preferences action"""
        logging.debug("Preferences action triggered")
        if self.props.active_window:
            self.props.active_window.show_preferences()

    def on_about(self, action, param):
        """Handle about dialog action"""
        logging.debug("About dialog action triggered")
        if self.props.active_window:
            self.props.active_window.show_about_dialog()

def main():
    """Main entry point"""
    app = SshPilotApplication()
    return app.run(sys.argv)

if __name__ == '__main__':
    main()
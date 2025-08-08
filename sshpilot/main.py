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
def load_resources():
    # Try to load resources from the installed location first
    installed_path = '/usr/share/io.github.mfat.sshpilot/io.github.mfat.sshpilot.gresource'
    if os.path.exists(installed_path):
        try:
            resource = Gio.Resource.load(installed_path)
            Gio.resources_register(resource)
            print(f"Loaded resources from installed location: {installed_path}")
            return True
        except GLib.Error as e:
            print(f"Failed to load resources from {installed_path}: {e}")
    
    # Get the absolute path to the current file
    current_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Try to find the project root by looking for the .git directory or other marker
    def find_project_root(start_path):
        path = os.path.abspath(start_path)
        while True:
            if os.path.exists(os.path.join(path, '.git')) or \
               os.path.exists(os.path.join(path, 'meson.build')):
                return path
            parent = os.path.dirname(path)
            if parent == path:  # Reached root directory
                return None
            path = parent
    
    project_root = find_project_root(current_dir)
    
    # Define possible resource locations
    possible_paths = []
    
    # Add paths relative to current simplified layout and project root if found
    # 1) Local resources next to this file (simplified layout under new/sshpilot/)
    possible_paths.append(os.path.join(current_dir, 'resources', 'sshpilot.gresource'))
    # 2) Project-root-relative development/build locations
    if project_root:
        possible_paths.extend([
            # Compiled resource in build directory
            os.path.join(project_root, '_build', 'io.github.mfat.sshpilot.gresource'),
            # Old source resource path in legacy layout (kept for compatibility)
            os.path.join(project_root, 'src', 'sshpilot_pkg', 'github', 'mfat', 'sshpilot', 'resources', 'sshpilot.gresource'),
            # New source resource path in simplified layout
            os.path.join(project_root, 'new', 'sshpilot', 'resources', 'sshpilot.gresource'),
        ])
    
    # Add absolute paths that might exist, with the user-specified path first
    possible_paths.extend([
        # User-specified locations (highest priority)
        '/home/mahdi/GitHub/sshpilot/new/sshpilot/resources/sshpilot.gresource',
        '/home/mahdi/GitHub/sshpilot/src/sshpilot_pkg/github/mfat/sshpilot/resources/sshpilot.gresource',
        # Development build location
        '/home/mahdi/GitHub/sshpilot/_build/io.github.mfat.sshpilot.gresource',
        # Flatpak/alternative location
        '/app/share/io.github.mfat.sshpilot/io.github.mfat.sshpilot.gresource'
    ])
    
    # Make paths unique while preserving order
    seen = set()
    possible_paths = [p for p in possible_paths if not (p in seen or seen.add(p))]
    
    print("Project root:", project_root)
    print("Searching for GResource file in:")
    for path in possible_paths:
        exists = os.path.exists(path)
        print(f"- {path} (exists: {exists})")
        if exists:
            try:
                size = os.path.getsize(path)
                print(f"  Size: {size} bytes")
            except Exception as e:
                print(f"  Error getting size: {e}")
    
    for path in possible_paths:
        if os.path.exists(path):
            try:
                resource = Gio.Resource.load(path)
                Gio.resources_register(resource)
                print(f"Loaded resources from development location: {path}")
                return True
            except GLib.Error as e:
                print(f"Failed to load resources from {path}: {e}")
    
    print("ERROR: Could not find or load the GResource bundle in any known location")
    return False

if not load_resources():
    print("Available files in project root:", os.listdir(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))))
    sys.exit(1)

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
        # Tab navigation accelerators
        self.create_action('tab-next', self.on_tab_next, ['<alt>Right'])
        self.create_action('tab-prev', self.on_tab_prev, ['<alt>Left'])
        # Close tab accelerator
        self.create_action('tab-close', self.on_tab_close, ['<primary>w'])
        
        # Connect to signals
        self.connect('shutdown', self.on_shutdown)
        self.connect('activate', self.on_activate)
        
        # Initialize window reference
        self.window = None
        
        logging.info("sshPilot application initialized")
    
    def on_activate(self, app):
        """Handle application activation"""
        # Create a new window if one doesn't exist
        if not self.window or not self.window.get_visible():
            from .window import MainWindow
            self.window = MainWindow(application=app)
            self.window.present()
        
    def on_shutdown(self, app):
        """Clean up all resources when application is shutting down"""
        logging.info("Application shutdown initiated, cleaning up...")
        from .terminal import process_manager
        process_manager.cleanup_all()
        logging.info("Cleanup completed")

    def setup_logging(self):
        """Set up logging configuration"""
        # Create log directory if it doesn't exist
        log_dir = os.path.expanduser('~/.local/share/sshPilot')
        os.makedirs(log_dir, exist_ok=True)
        
        # Set log level to DEBUG to capture all messages
        log_level = logging.DEBUG
        
        # Create a more detailed formatter
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # Clear any existing handlers
        logging.getLogger().handlers.clear()
        
        # File handler with rotation
        file_handler = RotatingFileHandler(
            os.path.join(log_dir, 'sshpilot.log'),
            maxBytes=10*1024*1024,  # 10MB
            backupCount=5,
            encoding='utf-8'
        )
        file_handler.setLevel(log_level)
        file_handler.setFormatter(formatter)
        
        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(log_level)
        console_handler.setFormatter(formatter)
        
        # Add handlers to root logger
        root_logger = logging.getLogger()
        root_logger.setLevel(log_level)
        root_logger.addHandler(file_handler)
        root_logger.addHandler(console_handler)
        
        # Set specific log levels for noisy modules, but allow runtime override via config
        try:
            from .config import Config
            cfg = Config()
            verbose = bool(cfg.get_setting('ssh.debug_enabled', False))
        except Exception:
            verbose = False
        logging.getLogger('asyncio').setLevel(logging.DEBUG if verbose else logging.INFO)
        logging.getLogger('gi').setLevel(logging.INFO if verbose else logging.WARNING)
        logging.getLogger('PIL').setLevel(logging.INFO if verbose else logging.WARNING)
        
        # App module logging: DEBUG if debug_enabled, else INFO
        app_level = logging.DEBUG if verbose else logging.INFO
        logging.getLogger('sshpilot').setLevel(app_level)
        logging.getLogger(__name__).setLevel(app_level)

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

    def on_tab_next(self, action, param):
        """Switch to next tab"""
        win = self.props.active_window
        if win and hasattr(win, '_select_tab_relative'):
            win._select_tab_relative(1)

    def on_tab_prev(self, action, param):
        """Switch to previous tab"""
        win = self.props.active_window
        if win and hasattr(win, '_select_tab_relative'):
            win._select_tab_relative(-1)

    def on_tab_close(self, action, param):
        """Close the currently selected tab"""
        win = self.props.active_window
        if not win:
            return
        try:
            page = win.tab_view.get_selected_page()
            if page:
                # Trigger the normal close flow (will prompt if enabled)
                win.tab_view.close_page(page)
        except Exception:
            pass

def main():
    """Main entry point"""
    app = SshPilotApplication()
    return app.run(None)  # Pass None to use default command line arguments

if __name__ == '__main__':
    main()
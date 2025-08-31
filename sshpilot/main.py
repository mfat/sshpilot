#!/usr/bin/env python3
"""
sshPilot - SSH connection manager with integrated terminal
Main application entry point
"""

import sys
import os
import logging
import argparse
from logging.handlers import RotatingFileHandler

import gi
gi.require_version('Adw', '1')
gi.require_version('Gtk', '4.0')
gi.require_version('Vte', '3.91')

from gi.repository import Adw, Gtk, Gio, GLib

# Register resources before importing any UI modules
def load_resources():
    """Load UI resources from known locations."""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    possible_paths = [
        os.path.join(current_dir, 'resources', 'sshpilot.gresource'),
        '/usr/share/io.github.mfat.sshpilot/io.github.mfat.sshpilot.gresource',
    ]

    for path in possible_paths:
        if os.path.exists(path):
            try:
                resource = Gio.Resource.load(path)
                Gio.resources_register(resource)
                logging.getLogger(__name__).debug("Loaded resources from: %s", path)
                return True
            except GLib.Error as e:
                logging.getLogger(__name__).error("Failed to load resources from %s: %s", path, e)
    logging.getLogger(__name__).error("Could not load GResource bundle")
    return False

if not load_resources():
    sys.exit(1)

from .window import MainWindow

class SshPilotApplication(Adw.Application):
    """Main application class for sshPilot"""

    def __init__(self, verbose: bool = False):
        super().__init__(
            application_id='io.github.mfat.sshpilot',
            flags=Gio.ApplicationFlags.FLAGS_NONE
        )

        # Command line verbosity override
        self.verbose_override = verbose

        # Set up logging
        self.setup_logging()
        
        # Apply saved application theme (light/dark/system)
        try:
            from .config import Config
            cfg = Config()
            saved_theme = str(cfg.get_setting('app-theme', 'default'))
            style_manager = Adw.StyleManager.get_default()
            if saved_theme == 'light':
                style_manager.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
            elif saved_theme == 'dark':
                style_manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
            else:
                style_manager.set_color_scheme(Adw.ColorScheme.DEFAULT)
        except Exception:
            pass
        
        # Create actions with keyboard shortcuts
        self.create_action('quit', self.on_quit_action, ['<primary>q'])
        self.create_action('new-connection', self.on_new_connection, ['<primary>n'])
        self.create_action('open-new-connection-tab', self.on_open_new_connection_tab, ['<primary><alt>n'])
        self.create_action('toggle-list', self.on_toggle_list, ['<primary>l'])
        self.create_action('search', self.on_search, ['<primary>f'])
        self.create_action('new-key', self.on_new_key, ['<primary><shift>k'])
        self.create_action('local-terminal', self.on_local_terminal, ['<primary><shift>t'])
        self.create_action('preferences', self.on_preferences, ['<primary>comma'])
        self.create_action('about', self.on_about)
        self.create_action('help', self.on_help, ['F1'])
        # Tab navigation accelerators
        self.create_action('tab-next', self.on_tab_next, ['<alt>Right'])
        self.create_action('tab-prev', self.on_tab_prev, ['<alt>Left'])
        # Close tab accelerator (use Ctrl+F4 to avoid conflicts with TUI editors like nano/vim)
        self.create_action('tab-close', self.on_tab_close, ['<primary>F4'])
        # Broadcast command to all SSH terminals
        self.create_action('broadcast-command', self.on_broadcast_command, ['<primary><shift>b'])
        
        # Connect to signals
        self.connect('shutdown', self.on_shutdown)
        self.connect('activate', self.on_activate)

        # Ensure Ctrl+C (SIGINT) follows the SAME path as clicking the window close button
        try:
            import signal

            def _handle_sigint(signum, frame):
                def _close_active_window():
                    win = self.props.active_window
                    if win:
                        try:
                            win.close()  # triggers MainWindow.on_close_request
                        except Exception:
                            pass
                    else:
                        try:
                            self.quit()
                        except Exception:
                            pass
                    return False
                GLib.idle_add(_close_active_window)
            signal.signal(signal.SIGINT, _handle_sigint)
        except Exception:
            pass
        
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
        file_handler.setFormatter(formatter)

        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)

        # Add handlers to root logger
        root_logger = logging.getLogger()
        root_logger.addHandler(file_handler)
        root_logger.addHandler(console_handler)

        # Determine verbosity via config or command line
        try:
            from .config import Config
            cfg = Config()
            verbose = bool(cfg.get_setting('ssh.debug_enabled', False))
        except Exception:
            verbose = False
        if getattr(self, 'verbose_override', False):
            verbose = True

        # Default to warnings unless verbose debugging is requested
        effective_level = logging.DEBUG if verbose else logging.WARNING
        file_handler.setLevel(effective_level)
        console_handler.setLevel(effective_level)
        root_logger.setLevel(effective_level)

        logging.getLogger('asyncio').setLevel(logging.DEBUG if verbose else logging.WARNING)
        logging.getLogger('gi').setLevel(logging.INFO if verbose else logging.WARNING)
        logging.getLogger('PIL').setLevel(logging.INFO if verbose else logging.WARNING)

        app_level = logging.DEBUG if verbose else logging.WARNING
        logging.getLogger('sshpilot').setLevel(app_level)
        logging.getLogger(__name__).setLevel(app_level)

    def create_action(self, name, callback, shortcuts=None):
        """Create a GAction with optional keyboard shortcuts"""
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)
        if shortcuts:
            self.set_accels_for_action(f"app.{name}", shortcuts)

    def on_quit_action(self, action=None, param=None):
        """Handle Ctrl+Q by closing the active window so the exact close flow runs."""
        win = self.props.active_window
        if win:
            try:
                win.close()
                return
            except Exception:
                pass
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

    def on_open_new_connection_tab(self, action, param):
        """Handle open new connection tab action (Ctrl+Alt+N)"""
        logging.debug("Open new connection tab action triggered")
        if self.props.active_window:
            # Forward to the window's action
            self.props.active_window.open_new_connection_tab_action.activate(None)

    def on_toggle_list(self, action, param):
        """Handle toggle list focus action"""
        logging.debug("Toggle list focus action triggered")
        if self.props.active_window:
            self.props.active_window.toggle_list_focus()

    def on_search(self, action, param):
        """Handle search action"""
        logging.debug("Search action triggered")
        if self.props.active_window:
            self.props.active_window.focus_search_entry()

    def on_new_key(self, action, param):
        """Handle new SSH key action"""
        logging.debug("New SSH key action triggered")
        if self.props.active_window:
            self.props.active_window.on_copy_key_to_server_clicked(None)

    def on_local_terminal(self, action, param):
        """Handle local terminal action"""
        logging.debug("Local terminal action triggered")
        if self.props.active_window:
            self.props.active_window.terminal_manager.show_local_terminal()

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

    def on_help(self, action, param):
        """Handle help action"""
        logging.debug("Help action triggered")
        if self.props.active_window:
            self.props.active_window.open_help_url()

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

    def on_broadcast_command(self, action, param):
        """Handle broadcast command action (Ctrl+Shift+B)"""
        logging.debug("Broadcast command action triggered")
        if self.props.active_window:
            # Forward to the window's action
            self.props.active_window.broadcast_command_action.activate(None)

def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="sshPilot SSH connection manager")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose debug logging")
    args = parser.parse_args()
    app = SshPilotApplication(verbose=args.verbose)
    return app.run(None)  # Pass None to use default command line arguments

if __name__ == '__main__':
    main()
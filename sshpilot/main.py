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
    # Simplified lookup: prefer installed site-packages path, with one system fallback.
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
                print(f"Loaded resources from: {path}")
                return True
            except GLib.Error as e:
                print(f"Failed to load resources from {path}: {e}")
    print("ERROR: Could not load GResource bundle")
    return False

if not load_resources():
    sys.exit(1)

from .window import MainWindow
from .platform_utils import is_macos, get_data_dir
from .preferences import should_hide_file_manager_options

class SshPilotApplication(Adw.Application):
    """Main application class for sshPilot"""

    def __init__(self, verbose: bool = False, isolated: bool = False, native_connect: bool = False):
        super().__init__(
            application_id='io.github.mfat.sshpilot',
            flags=Gio.ApplicationFlags.FLAGS_NONE
        )

        # Command line verbosity override
        self.verbose_override = verbose

        # Track whether native connect mode should be used for this run
        self.native_connect_override = True if native_connect else None
        self.native_connect_enabled = bool(native_connect)

        # Set up logging
        self.setup_logging()
        
        # Apply saved application theme (light/dark/system)
        self.config = None
        self._default_shortcuts = {}
        self._action_order = []
        self._accelerators_enabled = True
        self.accelerators_enabled = True
        self._config_handler = None

        try:
            from .config import Config
            cfg = Config()
            self.config = cfg
            try:
                self._accelerators_enabled = not bool(cfg.get_setting('terminal.pass_through_mode', False))
            except Exception:
                self._accelerators_enabled = True
            self._update_accelerators_enabled_flag()
            if hasattr(cfg, 'connect'):
                try:
                    self._config_handler = cfg.connect('setting-changed', self._on_config_setting_changed)
                except Exception:
                    self._config_handler = None
            saved_theme = str(cfg.get_setting('app-theme', 'default'))
            style_manager = Adw.StyleManager.get_default()
            if saved_theme == 'light':
                style_manager.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
            elif saved_theme == 'dark':
                style_manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
            else:
                style_manager.set_color_scheme(Adw.ColorScheme.DEFAULT)

            # Apply color overrides
            self.apply_color_overrides(cfg)

            configured_isolated = bool(cfg.get_setting('ssh.use_isolated_config', False))
            self.isolated_mode = bool(isolated or configured_isolated)

            # Update native connect state from configuration when not overridden
            try:
                native_cfg = bool(cfg.get_setting('ssh.native_connect', False))
            except Exception:
                native_cfg = False
            if self.native_connect_override is None:
                self.native_connect_enabled = native_cfg
            elif self.native_connect_enabled is False and native_cfg:
                # If config enables it and override is False-equivalent, honor override
                self.native_connect_enabled = bool(self.native_connect_override)
            else:
                # Override already active, ensure attribute is boolean
                self.native_connect_enabled = bool(self.native_connect_enabled or native_cfg)
        except Exception:
            self.isolated_mode = bool(isolated)
            if self.native_connect_override is None:
                self.native_connect_enabled = bool(native_connect)

        # Create actions with keyboard shortcuts
        # Use platform-specific shortcuts for better macOS compatibility
        mac = is_macos()

        if mac:
            # macOS-specific shortcuts using Meta key (Command key)
            self.create_action('quit', self.on_quit_action, ['<Meta><Shift>q'])
            self.create_action('new-connection', self.on_new_connection, ['<Meta>n'])
            self.create_action('open-new-connection-tab', self.on_open_new_connection_tab, ['<Meta><Alt>n'])
            self.create_action('toggle-list', self.on_toggle_list, ['<Meta>l'])
            self.create_action('search', self.on_search, ['<Meta>f'])
            self.create_action('new-key', self.on_new_key, ['<Meta><Shift>k'])
            self.create_action('edit-ssh-config', self.on_edit_ssh_config, ['<Meta><Shift>e'])
            if not should_hide_file_manager_options():
                self.create_action('manage-files', self.on_manage_files, ['<Meta><Shift>o'])
            logging.info("Using macOS-specific shortcuts (Meta key = Command key)")
        else:
            # Linux/Windows shortcuts using Primary key
            self.create_action('quit', self.on_quit_action, ['<primary><shift>q'])
            self.create_action('new-connection', self.on_new_connection, ['<primary>n'])
            self.create_action('open-new-connection-tab', self.on_open_new_connection_tab, ['<primary><alt>n'])
            self.create_action('toggle-list', self.on_toggle_list, ['<primary>l'])
            self.create_action('search', self.on_search, ['<primary>f'])
            self.create_action('new-key', self.on_new_key, ['<primary><shift>k'])
            self.create_action('edit-ssh-config', self.on_edit_ssh_config, ['<primary><shift>e'])
            if not should_hide_file_manager_options():
                self.create_action('manage-files', self.on_manage_files, ['<primary><shift>o'])
            logging.info("Using Linux/Windows shortcuts (Primary key = Ctrl key)")
        
        # Debug: Log registered shortcuts
        logging.info("Registered keyboard shortcuts:")
        logging.info("  Cmd+N: new-connection")
        logging.info("  Cmd+Shift+K: new-key")
        if mac:
            self.create_action('local-terminal', self.on_local_terminal, ['<Meta><Shift>t'])
            self.create_action('preferences', self.on_preferences, ['<Meta>comma'])
            self.create_action('tab-close', self.on_tab_close, ['<Meta>F4'])
            self.create_action('broadcast-command', self.on_broadcast_command, ['<Meta><Shift>b'])
        else:
            self.create_action('local-terminal', self.on_local_terminal, ['<primary><shift>t'])
            self.create_action('preferences', self.on_preferences, ['<primary>comma'])
            self.create_action('tab-close', self.on_tab_close, ['<primary>F4'])
            self.create_action('broadcast-command', self.on_broadcast_command, ['<primary><shift>b'])
        
        self.create_action('about', self.on_about)
        self.create_action('help', self.on_help, ['F1'])
        shortcuts_accel = ['<Meta><Shift>slash'] if mac else ['<primary><Shift>slash']
        self.create_action('shortcuts', self.on_shortcuts, shortcuts_accel)
        # Tab navigation accelerators
        self.create_action('tab-next', self.on_tab_next, ['<Alt>Right'])
        self.create_action('tab-prev', self.on_tab_prev, ['<Alt>Left'])
        
        # Tab overview accelerator
        if mac:
            self.create_action('tab-overview', self.on_tab_overview, ['<Meta><Shift>Tab'])
        else:
            self.create_action('tab-overview', self.on_tab_overview, ['<primary><shift>Tab'])
        
        # Quick connect accelerator
        if mac:
            self.create_action('quick-connect', self.on_quick_connect, ['<Meta><Alt>c'])
        else:
            self.create_action('quick-connect', self.on_quick_connect, ['<primary><Alt>c'])
        
        # Connect to signals
        self.connect('shutdown', self.on_shutdown)
        self.connect('activate', self.on_activate)

        # Ensure Ctrl (⌘ on macOS)+C (SIGINT) follows the SAME path as clicking the window close button
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
            self.window = MainWindow(application=app, isolated=self.isolated_mode)
            self.window.present()
        
    def on_shutdown(self, app):
        """Clean up all resources when application is shutting down"""
        logging.info("Application shutdown initiated, cleaning up...")
        if self._config_handler is not None and self.config is not None:
            try:
                self.config.disconnect(self._config_handler)
            except Exception:
                pass
            finally:
                self._config_handler = None
        self._update_accelerators_enabled_flag()
        from .terminal import process_manager
        process_manager.cleanup_all()
        logging.info("Cleanup completed")

    def setup_logging(self):
        """Set up logging configuration"""
        # Create log directory if it doesn't exist
        log_dir = get_data_dir()
        os.makedirs(log_dir, exist_ok=True)

        # Default log level is INFO for cleaner logs
        log_level = logging.INFO

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

        # Determine verbosity via config or command line
        try:
            from .config import Config
            cfg = Config()
            verbose = bool(cfg.get_setting('ssh.debug_enabled', False))
        except Exception:
            verbose = False
        if getattr(self, 'verbose_override', False):
            verbose = True

        effective_level = logging.DEBUG if verbose else logging.INFO
        file_handler.setLevel(effective_level)
        console_handler.setLevel(effective_level)
        root_logger.setLevel(effective_level)

        logging.getLogger('asyncio').setLevel(logging.DEBUG if verbose else logging.INFO)
        logging.getLogger('gi').setLevel(logging.INFO if verbose else logging.WARNING)
        logging.getLogger('PIL').setLevel(logging.INFO if verbose else logging.WARNING)

        app_level = logging.DEBUG if verbose else logging.INFO
        logging.getLogger('sshpilot').setLevel(app_level)
        logging.getLogger(__name__).setLevel(app_level)

    def _on_config_setting_changed(self, _config, key, value):
        if key != 'terminal.pass_through_mode':
            return

        self._accelerators_enabled = not bool(value)
        self._update_accelerators_enabled_flag()
        self.apply_shortcut_overrides()
        self._refresh_window_accelerators()

    def create_action(self, name, callback, shortcuts=None):
        """Create a GAction with optional keyboard shortcuts"""
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)
        if name not in self._action_order:
            self._action_order.append(name)
        self._default_shortcuts[name] = list(shortcuts) if shortcuts else None
        self._apply_shortcut_for_action(name)

    def _apply_shortcut_for_action(self, name: str):
        default = self._default_shortcuts.get(name)
        override = None
        if self.config is not None:
            try:
                override = self.config.get_shortcut_override(name)
            except Exception:
                override = None

        effective = default
        source = 'default'
        if override is not None:
            effective = override
            source = 'override'

        action_name = f"app.{name}"
        accelerators_blocked = not getattr(self, '_accelerators_enabled', True)
        if accelerators_blocked:
            self.set_accels_for_action(action_name, [])
            logging.debug(
                "Skipping accelerators for action '%s' because accelerators are suspended",
                name,
            )
            return
        if effective is None:
            self.set_accels_for_action(action_name, [])
            logging.debug(f"Registered action '{name}' without shortcuts")
        elif len(effective) == 0:
            self.set_accels_for_action(action_name, [])
            logging.debug(f"Action '{name}' shortcuts disabled via {source}")
        else:
            self.set_accels_for_action(action_name, effective)
            logging.debug(
                "Registered action '%s' with shortcuts from %s: %s",
                name,
                source,
                effective,
            )

    def apply_shortcut_overrides(self):
        """Reapply all shortcut overrides to the registered actions."""
        for name in self._action_order:
            self._apply_shortcut_for_action(name)

    def _refresh_window_accelerators(self):
        """Notify windows to refresh accelerator state."""
        try:
            windows = list(self.get_windows())
        except Exception:
            windows = []

        for win in windows:
            if hasattr(win, '_update_sidebar_accelerators'):
                try:
                    win._update_sidebar_accelerators()
                except Exception:
                    logging.debug("Failed to update window accelerators for current state")

    def _update_accelerators_enabled_flag(self):
        """Update the exposed accelerator enabled flag considering focus state."""
        self.accelerators_enabled = bool(getattr(self, '_accelerators_enabled', True))

    def get_registered_shortcut_defaults(self):
        """Return a mapping of action names to their default accelerators."""
        return {
            name: (shortcuts.copy() if isinstance(shortcuts, list) else shortcuts)
            for name, shortcuts in self._default_shortcuts.items()
        }

    def get_registered_action_order(self):
        """Return the order in which actions were registered."""
        return list(self._action_order)

    def quit(self):
        """Request application shutdown, showing confirmation if needed."""
        win = self.props.active_window
        if win and not getattr(win, "_is_quitting", False):
            try:
                if win.on_close_request(win):
                    return  # dialog will handle quitting or cancellation
            except Exception as e:
                logger.error(f"Error in close request: {e}")
                # If there's an error, force quit to prevent hanging
                super().quit()
                return
        super().quit()

    def on_quit_action(self, action=None, param=None):
        """Handle Ctrl (⌘ on macOS)+Q by routing through the application quit path."""
        self.quit()

    def on_edit_ssh_config(self, action=None, param=None):
        """Handle SSH config editor action."""
        win = self.props.active_window
        if win and hasattr(win, '_open_ssh_config_editor'):
            win._open_ssh_config_editor()

    def do_activate(self):
        """Called when the application is activated"""
        win = self.props.active_window
        if not win:
            win = MainWindow(application=self, isolated=self.isolated_mode)
        win.present()

    def on_new_connection(self, action, param):
        """Handle new connection action"""
        logging.info("New connection action triggered (Cmd+N)")
        if self.props.active_window:
            try:
                self.props.active_window.show_connection_dialog()
                logging.debug("Connection dialog shown successfully")
            except Exception as e:
                logging.error(f"Failed to show connection dialog: {e}")
        else:
            logging.warning("No active window found for new connection action")

    def on_open_new_connection_tab(self, action, param):
        """Handle open new connection tab action (Ctrl/⌘+Alt+N)"""
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
        logging.info("New SSH key action triggered (Cmd+Shift+K)")
        if self.props.active_window:
            try:
                # Check if there's a selected connection
                selected_row = self.props.active_window.connection_list.get_selected_row()
                if not selected_row or not getattr(selected_row, "connection", None):
                    # No connection selected, show a dialog to select one
                    logging.info("No connection selected, showing connection selection dialog")
                    self.props.active_window.show_connection_selection_for_ssh_copy()
                else:
                    # Use the selected connection
                    self.props.active_window.on_copy_key_to_server_clicked(None)
                logging.debug("SSH key copy dialog shown successfully")
            except Exception as e:
                logging.error(f"Failed to show SSH key copy dialog: {e}")
        else:
            logging.warning("No active window found for new SSH key action")

    def on_manage_files(self, action, param):
        """Handle manage files shortcut."""
        if should_hide_file_manager_options():
            return

        win = self.props.active_window
        if not win:
            return

        try:
            handler = getattr(win, 'on_manage_files_button_clicked', None)
            if callable(handler):
                handler(None)
                return

            connection_list = getattr(win, 'connection_list', None)
            if not connection_list:
                return

            row = connection_list.get_selected_row()
            connection = getattr(row, 'connection', None) if row else None
            if connection and hasattr(win, '_open_manage_files_for_connection'):
                win._open_manage_files_for_connection(connection)
        except Exception as exc:
            logging.error(f"Failed to open file manager via shortcut: {exc}")
            try:
                connection_name = getattr(connection, 'nickname', '') if 'connection' in locals() else ''
                if connection_name and hasattr(win, '_show_manage_files_error'):
                    win._show_manage_files_error(connection_name, str(exc))
            except Exception:
                pass

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

    def on_shortcuts(self, action, param):
        """Handle keyboard shortcuts overlay action"""
        logging.debug("Shortcuts action triggered")
        if self.props.active_window:
            self.props.active_window.show_shortcuts_window()

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

    def on_tab_overview(self, action, param):
        """Toggle tab overview"""
        win = self.props.active_window
        if not win or not hasattr(win, 'tab_overview'):
            return
        try:
            # Toggle the tab overview
            is_open = win.tab_overview.get_open()
            win.tab_overview.set_open(not is_open)
        except Exception as e:
            logging.error(f"Failed to toggle tab overview: {e}")

    def on_quick_connect(self, action, param):
        """Open quick connect dialog"""
        win = self.props.active_window
        if not win:
            return
        try:
            # Import here to avoid circular imports
            from .welcome_page import QuickConnectDialog
            dialog = QuickConnectDialog(win)
            dialog.present()
        except Exception as e:
            logging.error(f"Failed to open quick connect dialog: {e}")

    def on_broadcast_command(self, action, param):
        """Handle broadcast command action (Ctrl/⌘+Shift+B)"""
        logging.debug("Broadcast command action triggered")
        if self.props.active_window:
            # Forward to the window's action
            self.props.active_window.broadcast_command_action.activate(None)

    def apply_color_overrides(self, config):
        """Apply color overrides to the application"""
        try:
            import gi
            gi.require_version('Gtk', '4.0')
            from gi.repository import Gtk, Gdk
            
            # Get color overrides from config
            app_color = config.get_setting('app-color-override', None)
            accent_color = config.get_setting('accent-color-override', None)
            sidebar_color = config.get_setting('sidebar-color-override', None)
            
            # Build CSS with color overrides using proper Adwaita named colors
            css_rules = []
            
            if app_color:
                # Override all accent-related colors for comprehensive theming
                css_rules.append(f"@define-color accent_bg_color {app_color};")
                css_rules.append(f"@define-color accent_fg_color white;")
                css_rules.append(f"@define-color accent_color {app_color};")
                # Override selected colors (used for selected rows, list items, etc.)
                css_rules.append(f"@define-color theme_selected_bg_color {app_color};")
                css_rules.append(f"@define-color theme_selected_fg_color white;")
                css_rules.append(f"@define-color theme_unfocused_selected_bg_color {app_color};")
                css_rules.append(f"@define-color theme_unfocused_selected_fg_color white;")
                # Override window background colors
                css_rules.append(f"@define-color window_bg_color {app_color};")
                css_rules.append(f"@define-color theme_bg_color {app_color};")
                css_rules.append(f"@define-color theme_unfocused_bg_color {app_color};")
                # Override sidebar colors
                css_rules.append(f"@define-color sidebar_bg_color {app_color};")
                css_rules.append(f"@define-color secondary_sidebar_bg_color {app_color};")
            
            if accent_color:
                # Override accent colors regardless of app color
                css_rules.append(f"@define-color accent_color {accent_color};")
                css_rules.append(f"@define-color accent_bg_color {accent_color};")
                css_rules.append(f"@define-color accent_fg_color white;")
                css_rules.append(f"@define-color theme_selected_bg_color {accent_color};")
                css_rules.append(f"@define-color theme_selected_fg_color white;")
                css_rules.append(
                    f"@define-color theme_unfocused_selected_bg_color {accent_color};"
                )
                css_rules.append(
                    f"@define-color theme_unfocused_selected_fg_color white;"
                )
            
            if sidebar_color:
                # Override sidebar colors independently
                css_rules.append(f"@define-color sidebar_bg_color {sidebar_color};")
                css_rules.append(f"@define-color secondary_sidebar_bg_color {sidebar_color};")
            
            if css_rules:
                # Add specific CSS rules for row selection
                css_rules.append("")
                css_rules.append("/* Force row selection to use custom colors */")
                css_rules.append("row:selected {")
                css_rules.append("  background-color: @theme_selected_bg_color;")
                css_rules.append("  color: @theme_selected_fg_color;")
                css_rules.append("}")
                css_rules.append("")
                css_rules.append("row:selected:focus {")
                css_rules.append("  background-color: @theme_selected_bg_color;")
                css_rules.append("  color: @theme_selected_fg_color;")
                css_rules.append("}")
                css_rules.append("")
                css_rules.append("list row:selected {")
                css_rules.append("  background-color: @theme_selected_bg_color;")
                css_rules.append("  color: @theme_selected_fg_color;")
                css_rules.append("}")
                
                # Apply custom CSS
                provider = Gtk.CssProvider()
                css = "\n".join(css_rules)
                provider.load_from_data(css.encode('utf-8'))
                
                # Add provider to display
                display = Gdk.Display.get_default()
                if display:
                    Gtk.StyleContext.add_provider_for_display(
                        display, 
                        provider, 
                        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                    )
                    # Store provider reference for cleanup
                    display._color_override_provider = provider
                    logging.info("Applied color overrides on startup")
                
        except Exception as e:
            logging.error(f"Failed to apply color overrides: {e}")

def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="sshPilot SSH connection manager")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose debug logging")
    parser.add_argument("--isolated", action="store_true", help="Use isolated SSH configuration")
    parser.add_argument(
        "--native-connect",
        action="store_true",
        help="Use experimental native SSH command execution (ssh Host)",
    )
    args = parser.parse_args()
    app = SshPilotApplication(
        verbose=args.verbose,
        isolated=args.isolated,
        native_connect=args.native_connect,
    )
    return app.run(None)  # Pass None to use default command line arguments

if __name__ == '__main__':
    main()
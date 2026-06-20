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


def _clamp_thirdparty_loggers() -> None:
    """Pin chatty third-party loggers to WARNING.

    Called BEFORE any other module is imported so that keyring/paramiko/etc.
    don't dump DEBUG noise to the root logger at import-time, which happens
    before :meth:`SshPilotApplication.setup_logging` has had a chance to run.
    Re-applied (idempotently) inside setup_logging so direct callers of that
    method are also covered.
    """
    for noisy in (
        'keyring', 'keyring.backend',
        'paramiko', 'paramiko.transport', 'paramiko.transport.sftp',
        'gi', 'PIL', 'urllib3', 'asyncio',
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)


_clamp_thirdparty_loggers()

# Module logger — used for any non-class-attached logging below.
logger = logging.getLogger(__name__)

import gi
gi.require_version('Adw', '1')
gi.require_version('Gtk', '4.0')
gi.require_version('Vte', '3.91')

from gi.repository import Adw, Gtk, Gio, GLib, Gdk

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
                logger.debug("Loaded resources from: %s", path)

                # Add resource path to icon theme EARLY, before any UI is created
                # Following GNOME docs: https://developer.gnome.org/documentation/tutorials/themed-icons.html
                # and GTK4 API: https://docs.gtk.org/gtk4/class.IconTheme.html
                # We use set_resource_path() to prepend our base path so bundled icons are checked first
                # Note: Even with resource paths set, the icon theme system may still prioritize
                # system themes, so we also manually check resources in icon_utils.py
                try:
                    display = Gdk.Display.get_default()
                    if display:
                        theme = Gtk.IconTheme.get_for_display(display)
                        # Get existing paths using get_resource_path() API
                        existing_paths = list(theme.get_resource_path())
                        base_path = "/io/github/mfat/sshpilot/icons"
                        if base_path not in existing_paths:
                            # Prepend our path using set_resource_path() API (replaces all paths)
                            new_paths = [base_path] + existing_paths
                            theme.set_resource_path(new_paths)
                            logger.debug(
                                "Set icon theme resource paths (bundled first): %s...",
                                new_paths[:2],
                            )
                        elif existing_paths[0] != base_path:
                            # Already added, but ensure it's first using set_resource_path()
                            new_paths = [base_path] + [p for p in existing_paths if p != base_path]
                            theme.set_resource_path(new_paths)
                            logger.debug("Reordered resource paths to prioritize bundled icons")
                        else:
                            logger.debug("Bundled icon path already first: %s", base_path)
                except Exception as e:
                    logger.warning("Could not configure icon theme for bundled icons: %s", e)

                return True
            except GLib.Error as e:
                logger.error("Failed to load resources from %s: %s", path, e)
    logger.error("Could not load GResource bundle")
    return False

if not load_resources():
    sys.exit(1)

# Patch Gtk.Image to automatically prefer bundled icons
from .icon_utils import patch_gtk_image
patch_gtk_image()

from .window import MainWindow
from .platform_utils import is_macos, get_data_dir, get_state_dir
from .preferences import should_hide_file_manager_options
from .startup_info import print_startup_info

class SshPilotApplication(Adw.Application):
    """Main application class for sshPilot"""

    def __init__(self, verbose: bool = False, quiet: bool = False,
                 isolated: bool = False):
        super().__init__(
            application_id='io.github.mfat.sshpilot',
            flags=Gio.ApplicationFlags.FLAGS_NONE
        )

        # Command line verbosity overrides — mutually exclusive at argparse,
        # but defensively normalise here too.
        self.verbose_override = verbose and not quiet
        self.quiet_override = quiet and not verbose
        self.isolated_mode = isolated

        # Set up logging
        self.setup_logging()
        
        # Print startup information
        print_startup_info(isolated=isolated, verbose=verbose)
        
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
        except Exception:
            self.isolated_mode = bool(isolated)

        # Create actions with keyboard shortcuts
        # Use platform-specific shortcuts for better macOS compatibility
        mac = is_macos()

        if mac:
            # macOS-specific shortcuts using Meta key (Command key)
            self.create_action('quit', self.on_quit_action, ['<Meta><Shift>q'])
            self.create_action('new-connection', self.on_new_connection, ['<Meta>n'])
            self.create_action('open-new-connection-tab', self.on_open_new_connection_tab, ['<Meta><Alt>n'])
            self.create_action('toggle-list', self.on_toggle_list, ['<Meta><Shift>l'])
            self.create_action('search', self.on_search, ['<Meta>f'])
            self.create_action('terminal-search', self.on_terminal_search, ['<Meta><Shift>f'])
            self.create_action('new-key', self.on_new_key, ['<Meta><Shift>k'])
            self.create_action('edit-ssh-config', self.on_edit_ssh_config, ['<Meta><Shift>e'])
            if not should_hide_file_manager_options():
                self.create_action('manage-files', self.on_manage_files, ['<Meta><Shift>o'])
            logger.debug("Using macOS-specific shortcuts (Meta key = Command key)")
        else:
            # Linux/Windows shortcuts using Primary key
            self.create_action('quit', self.on_quit_action, ['<primary><shift>q'])
            self.create_action('new-connection', self.on_new_connection, ['<primary>n'])
            self.create_action('open-new-connection-tab', self.on_open_new_connection_tab, ['<primary><alt>n'])
            self.create_action('toggle-list', self.on_toggle_list, ['<primary><shift>l'])
            self.create_action('search', self.on_search, ['<primary>f'])
            self.create_action('terminal-search', self.on_terminal_search, ['<primary><shift>f'])
            self.create_action('new-key', self.on_new_key, ['<primary><shift>k'])
            self.create_action('edit-ssh-config', self.on_edit_ssh_config, ['<primary><shift>e'])
            if not should_hide_file_manager_options():
                self.create_action('manage-files', self.on_manage_files, ['<primary><shift>o'])
            logger.debug("Using Linux/Windows shortcuts (Primary key = Ctrl key)")

        # Sidebar toggle is a window action; register its default accels so
        # it shows up in the shortcut editor and respects overrides.
        self.register_window_shortcut(
            'toggle_sidebar', ['F9', '<Meta>b'] if mac else ['F9']
        )

        # Detailed shortcut list is verbose noise at INFO. Help → Keyboard
        # Shortcuts already shows this to users.
        if logger.isEnabledFor(logging.DEBUG):
            mod_label = "Cmd" if mac else "Ctrl"
            logger.debug("Registered keyboard shortcuts:")
            logger.debug("  %s+N: new-connection", mod_label)
            logger.debug("  %s+Shift+K: new-key", mod_label)
        if mac:
            self.create_action('local-terminal', self.on_local_terminal, ['<Meta><Shift>t'])
            self.create_action('preferences', self.on_preferences, ['<Meta>comma'])
            self.create_action('tab-close', self.on_tab_close, ['<Meta><Shift>w'])
            self.create_action('broadcast-command', self.on_broadcast_command, ['<Meta><Shift>b'])
            self.create_action('new-split-view-tab', self.on_new_split_view_tab, ['<Meta><Shift>s'])
        else:
            self.create_action('local-terminal', self.on_local_terminal, ['<primary><shift>t'])
            self.create_action('preferences', self.on_preferences, ['<primary>comma'])
            self.create_action('tab-close', self.on_tab_close, ['<primary><shift>w'])
            self.create_action('broadcast-command', self.on_broadcast_command, ['<primary><shift>b'])
            self.create_action('new-split-view-tab', self.on_new_split_view_tab, ['<primary><shift>s'])
        
        self.create_action('about', self.on_about)
        self.create_action('help', self.on_help, ['F1'])
        # Use the "question" keyval, not "<Shift>slash": holding Shift turns
        # "/" into "?", so the shifted-slash accelerator never matches the
        # actual key event. "<primary>question" is the standard binding that
        # fires on Ctrl+Shift+/ (Cmd+Shift+/ on macOS).
        shortcuts_accel = ['<Meta>question'] if mac else ['<primary>question']
        self.create_action('shortcuts', self.on_shortcuts, shortcuts_accel)
        # Tab navigation accelerators
        self.create_action('tab-next', self.on_tab_next, ['<primary>Page_Down'])
        self.create_action('tab-prev', self.on_tab_prev, ['<primary>Page_Up'])
        # Move the current tab within the tab bar
        self.create_action('tab-move-left', self.on_tab_move_left, ['<primary><shift>Page_Up'])
        self.create_action('tab-move-right', self.on_tab_move_right, ['<primary><shift>Page_Down'])

        # Tab overview accelerator
        if mac:
            self.create_action('tab-overview', self.on_tab_overview, ['<Meta><Shift>Tab'])
        else:
            self.create_action('tab-overview', self.on_tab_overview, ['<primary><shift>Tab'])
        
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
        
        logger.info("sshPilot application initialized")
    
    def on_activate(self, app):
        """Handle application activation"""
        # Create a new window if one doesn't exist
        if not self.window or not self.window.get_visible():
            from .window import MainWindow
            self.window = MainWindow(application=app, isolated=self.isolated_mode)
            self.window.present()

            # The window UI is built and presented and plugins are bound, so
            # it is now safe for plugins to do live UI/terminal work.
            try:
                host = getattr(self.window, 'plugin_host', None)
                if host is not None:
                    host.dispatch_app_started()
            except Exception:
                logger.exception("Plugin app_started dispatch failed")

        # Start the in-process passphrase-prompt server so SSH askpass prompts
        # render as modal children of the main window instead of stray helper
        # windows that can hide behind it on Wayland. start() is idempotent.
        try:
            win = self.window or self.props.active_window
            if win is not None:
                from . import askpass_server
                askpass_server.start(win)
        except Exception as exc:
            logger.debug(f"Failed to start askpass prompt server: {exc}")

    def on_shutdown(self, app):
        """Clean up all resources when application is shutting down"""
        logger.info("Application shutdown initiated, cleaning up...")

        # Notify plugins first (before any teardown), then best-effort
        # deactivate them.
        try:
            host = getattr(self, 'plugin_host', None)
            if host is None and self.window is not None:
                host = getattr(self.window, 'plugin_host', None)
            if host is not None:
                host.dispatch_app_shutdown()
        except Exception:
            logger.exception("Plugin app_shutdown dispatch failed")
        try:
            for lp in (getattr(self, 'loaded_plugins', None)
                       or (getattr(self.window, 'loaded_plugins', None)
                           if self.window is not None else None)
                       or []):
                pid = getattr(lp, 'plugin_id', None)
                try:
                    lp.instance.deactivate()
                except Exception:
                    logger.exception("Plugin %r deactivate failed", pid or '?')
                # Unwind everything the plugin registered so deactivate is
                # actually a teardown (events, protocols, UI pages).
                if pid:
                    try:
                        if host is not None:
                            host.events.unsubscribe_plugin(pid)
                            host.ui.remove_plugin_pages(pid)
                    except Exception:
                        logger.exception("Plugin %r host unwind failed", pid)
                    try:
                        from .plugins.registry import protocol_registry
                        protocol_registry().unregister_plugin(pid)
                    except Exception:
                        logger.exception("Plugin %r protocol unwind failed", pid)
        except Exception:
            pass

        # Stop the askpass prompt server and unlink its socket.
        try:
            from . import askpass_server
            askpass_server.stop()
        except Exception as exc:
            logger.debug(f"Failed to stop askpass prompt server: {exc}")

        # Close all file manager windows
        try:
            def _cleanup_window(window):
                cleanup = getattr(window, "_cleanup_manager", None)
                if callable(cleanup):
                    cleanup()
                elif hasattr(window, "_manager") and window._manager is not None:
                    window._manager.close()
                    window._manager = None
            
            windows = list(app.get_windows())
            logger.info(f"Found {len(windows)} windows from application during shutdown")
            for window in windows:
                try:
                    _cleanup_window(window)
                except Exception as exc:
                    logger.error(f"Error cleaning up window {window}: {exc}", exc_info=True)
            
            # Also check the global registry as a fallback
            try:
                from .file_manager_window import _file_manager_windows_registry
                registry_windows = list(_file_manager_windows_registry)
                logger.info(f"Found {len(registry_windows)} file manager windows in registry")
                for window in registry_windows:
                    if window in windows:
                        continue
                    try:
                        _cleanup_window(window)
                    except Exception as exc:
                        logger.error(f"Error cleaning up registry window {window}: {exc}", exc_info=True)
            except ImportError:
                pass  # Module might not be loaded
            except Exception as exc:
                logger.debug(f"Error accessing file manager registry: {exc}")
        except Exception as exc:
            logger.error(f"Error closing file manager windows: {exc}", exc_info=True)
        
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
        logger.info("Cleanup completed")

    def setup_logging(self):
        """Set up logging configuration.

        Logs live in ``$XDG_STATE_HOME/sshpilot/`` per the XDG Base Directory
        spec (state data includes "actions history (logs, …)"). We previously
        stored them in ``$XDG_DATA_HOME/sshpilot/``; existing log files there
        are migrated on first launch so users don't lose history.

        Inside a Flatpak sandbox the path resolves to
        ``~/.var/app/io.github.mfat.sshpilot/.local/state/sshpilot/`` which
        the sandbox grants the app write access to — verified by simulation
        and matches the convention used by other Flatpak GNOME apps on this
        system (Bitwarden, Authenticator, Cambalache, …).
        """
        log_dir = get_state_dir()
        # Defensive fallback: if the state dir can't be created for any
        # reason (filesystem mount-point quirks, exotic Flatpak versions,
        # corrupted XDG env vars), fall back to the old data-dir path so
        # the app still logs *somewhere* instead of crashing at startup.
        try:
            os.makedirs(log_dir, exist_ok=True)
        except OSError as exc:
            fallback = get_data_dir()
            try:
                os.makedirs(fallback, exist_ok=True)
            except OSError:
                # Last-ditch: tmpdir. Logging to a transient location is
                # better than failing setup_logging entirely.
                import tempfile
                fallback = os.path.join(tempfile.gettempdir(), 'sshpilot')
                os.makedirs(fallback, exist_ok=True)
            print(
                f"sshpilot: state dir {log_dir!r} unusable ({exc}); "
                f"logging to {fallback!r} instead",
                flush=True,
            )
            log_dir = fallback

        # One-shot migration of any pre-existing log files from the old
        # data-dir location. Runs only when the new dir has no main log yet
        # — i.e. on the very first launch after the path change. Idempotent
        # in practice because we move (not copy), so a subsequent launch
        # finds the file already in place.
        try:
            old_dir = get_data_dir()
            new_main = os.path.join(log_dir, 'sshpilot.log')
            if old_dir != log_dir and not os.path.exists(new_main) and os.path.isdir(old_dir):
                import glob, shutil as _sh
                moved = 0
                for old_path in glob.glob(os.path.join(old_dir, 'sshpilot.log*')):
                    try:
                        _sh.move(old_path, os.path.join(log_dir, os.path.basename(old_path)))
                        moved += 1
                    except Exception:
                        # Silent — we'll just leave the old file in place.
                        pass
                if moved:
                    # Defer to a print since logging isn't set up yet.
                    print(
                        f"sshpilot: migrated {moved} log file(s) "
                        f"from {old_dir} to {log_dir}",
                        flush=True,
                    )
        except Exception:
            # Migration is best-effort; never block startup over it.
            pass

        # Default log level is INFO for cleaner logs
        log_level = logging.INFO

        # Full timestamp + fully-qualified logger name on the file handler —
        # we want detail in bug-report logs. Console gets a shorter format
        # that's easier to scan.
        file_formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
        )
        console_formatter = logging.Formatter(
            '%(asctime)s %(levelname)-5s %(short_name)s: %(message)s',
            datefmt='%H:%M:%S',
        )

        class _ShortNameFilter(logging.Filter):
            """Strip the leading ``sshpilot.`` from logger names for the console."""

            def filter(self, record: logging.LogRecord) -> bool:
                name = record.name or ''
                if name.startswith('sshpilot.'):
                    record.short_name = name[len('sshpilot.'):]
                else:
                    record.short_name = name or '-'
                return True

        # --- Category filters for the per-feature log files. -------------
        # Master ``sshpilot.log`` always receives everything (it's what bug
        # reports cite). The category files are filtered convenience views.
        _SSH_CATEGORY_NAMES: tuple = (
            'paramiko',
            'sshpilot.connection_manager',
            'sshpilot.terminal',
            'sshpilot.terminal_manager',
            'sshpilot.terminal_backends',
            'sshpilot.ssh_utils',
            'sshpilot.ssh_config_utils',
            'sshpilot.ssh_config_editor',
            'sshpilot.ssh_connection_builder',
            'sshpilot.ssh_password_exec',
            'sshpilot.sshcopyid_window',
            'sshpilot.sshpilot_agent',
            'sshpilot.scp_utils',
            'sshpilot.sftp_utils',
            'sshpilot.known_hosts_editor',
        )

        def _matches_any(name: str, prefixes: tuple) -> bool:
            for p in prefixes:
                if name == p or name.startswith(p + '.'):
                    return True
            return False

        class _SshCategoryFilter(logging.Filter):
            """Pass paramiko + our SSH/connection/terminal modules."""

            def filter(self, record: logging.LogRecord) -> bool:
                return _matches_any(record.name or '', _SSH_CATEGORY_NAMES)

        class _AppCategoryFilter(logging.Filter):
            """Pass our own loggers EXCEPT the SSH-category ones.

            We deliberately don't let arbitrary third-party loggers leak into
            the app log — they'd just add noise no one can act on. Paramiko
            already goes to ssh.log via the filter above.
            """

            def filter(self, record: logging.LogRecord) -> bool:
                name = record.name or ''
                if not (name == 'sshpilot' or name.startswith('sshpilot.') or name == 'root'):
                    return False
                return not _matches_any(name, _SSH_CATEGORY_NAMES)

        # Clear any existing handlers
        logging.getLogger().handlers.clear()

        # --- Master file (everything) ------------------------------------
        # ``sshpilot.log`` is the authoritative log used by bug reports.
        file_handler = RotatingFileHandler(
            os.path.join(log_dir, 'sshpilot.log'),
            maxBytes=10*1024*1024,  # 10MB
            backupCount=5,
            encoding='utf-8'
        )
        file_handler.setLevel(log_level)
        file_handler.setFormatter(file_formatter)

        # --- App-only file -----------------------------------------------
        app_file_handler = RotatingFileHandler(
            os.path.join(log_dir, 'app.log'),
            maxBytes=10*1024*1024, backupCount=5, encoding='utf-8',
        )
        app_file_handler.setLevel(log_level)
        app_file_handler.setFormatter(file_formatter)
        app_file_handler.addFilter(_AppCategoryFilter())

        # --- SSH-only file ------------------------------------------------
        ssh_file_handler = RotatingFileHandler(
            os.path.join(log_dir, 'ssh.log'),
            maxBytes=10*1024*1024, backupCount=5, encoding='utf-8',
        )
        ssh_file_handler.setLevel(log_level)
        ssh_file_handler.setFormatter(file_formatter)
        ssh_file_handler.addFilter(_SshCategoryFilter())

        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(log_level)
        console_handler.setFormatter(console_formatter)
        console_handler.addFilter(_ShortNameFilter())

        # Add handlers to root logger
        root_logger = logging.getLogger()
        root_logger.setLevel(log_level)
        root_logger.addHandler(file_handler)
        root_logger.addHandler(app_file_handler)
        root_logger.addHandler(ssh_file_handler)
        root_logger.addHandler(console_handler)
        # Track the per-category handlers so subsequent level changes
        # (verbose / quiet) can be applied uniformly below.
        self._category_handlers = (file_handler, app_file_handler, ssh_file_handler)

        # Determine verbosity. Precedence (highest first):
        #   CLI --quiet  →  WARNING (ERROR-and-up only)
        #   CLI --verbose → DEBUG
        #   logging.level config key ('info' | 'debug')
        #   legacy ssh.debug_enabled (already migrated in config validator)
        quiet = bool(getattr(self, 'quiet_override', False))
        verbose = bool(getattr(self, 'verbose_override', False))
        if not (quiet or verbose):
            try:
                from .config import Config
                cfg = Config()
                level_setting = cfg.get_setting('logging.level', 'info')
                verbose = str(level_setting).lower() == 'debug'
            except Exception:
                verbose = False

        if quiet:
            effective_level = logging.WARNING
        elif verbose:
            effective_level = logging.DEBUG
        else:
            effective_level = logging.INFO
        for h in self._category_handlers:
            h.setLevel(effective_level)
        console_handler.setLevel(effective_level)
        root_logger.setLevel(effective_level)

        # Reapply third-party clamp. At default level they stay at WARNING
        # (so we don't drown the user in keyring/paramiko/PIL chatter). In
        # verbose mode the user has opted into the firehose — paramiko goes
        # to DEBUG so the SSH log file actually shows the protocol trace
        # ([chan 0] listdir, SFTP packets, etc.); other 3rd-party libraries
        # only get one notch up to INFO because their DEBUG levels are
        # rarely useful for sshPilot bug reports.
        _clamp_thirdparty_loggers()
        if verbose:
            for noisy in ('paramiko', 'paramiko.transport', 'paramiko.transport.sftp'):
                logging.getLogger(noisy).setLevel(logging.DEBUG)
            for noisy in ('keyring', 'gi', 'PIL', 'urllib3', 'asyncio'):
                logging.getLogger(noisy).setLevel(logging.INFO)

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

    def register_window_shortcut(self, name, shortcuts):
        """Register default accels for a win.* action (created on the window,
        not the app) so it appears in the shortcut editor and respects
        config overrides. ``set_accels_for_action`` does not require the
        action to exist yet."""
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

        action_name = f"win.{name}" if self.lookup_action(name) is None else f"app.{name}"
        accelerators_blocked = not getattr(self, '_accelerators_enabled', True)
        if accelerators_blocked:
            self.set_accels_for_action(action_name, [])
            logger.debug(
                "Skipping accelerators for action '%s' because accelerators are suspended",
                name,
            )
            return
        if effective is None:
            self.set_accels_for_action(action_name, [])
            logger.debug(f"Registered action '{name}' without shortcuts")
        elif len(effective) == 0:
            self.set_accels_for_action(action_name, [])
            logger.debug(f"Action '{name}' shortcuts disabled via {source}")
        else:
            self.set_accels_for_action(action_name, effective)
            logger.debug(
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
                    logger.debug("Failed to update window accelerators for current state")

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
            if win.on_close_request(win):
                return  # dialog will handle quitting or cancellation
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
        logger.debug("New connection action triggered")
        if self.props.active_window:
            try:
                self.props.active_window.show_connection_dialog()
                logger.debug("Connection dialog shown successfully")
            except Exception as e:
                logger.error(f"Failed to show connection dialog: {e}")
        else:
            logger.warning("No active window found for new connection action")

    def on_open_new_connection_tab(self, action, param):
        """Handle open new connection tab action (Ctrl/⌘+Alt+N)"""
        logger.debug("Open new connection tab action triggered")
        if self.props.active_window:
            # Forward to the window's action
            self.props.active_window.open_new_connection_tab_action.activate(None)

    def on_toggle_list(self, action, param):
        """Handle toggle list focus action"""
        logger.debug("Toggle list focus action triggered")
        if self.props.active_window:
            self.props.active_window.toggle_list_focus()

    def on_search(self, action, param):
        """Handle search action"""
        logger.debug("Search action triggered")
        if self.props.active_window:
            # The shortcut only ever turns search on (and focuses it). Hiding
            # the search bar is done exclusively via the toolbar search button.
            self.props.active_window.activate_search_entry()

    def on_new_key(self, action, param):
        """Handle new SSH key action"""
        logger.debug("New SSH key action triggered")
        if self.props.active_window:
            try:
                # Check if there's a selected connection
                selected_row = self.props.active_window.connection_list.get_selected_row()
                if not selected_row or not getattr(selected_row, "connection", None):
                    # No connection selected, show a dialog to select one
                    logger.info("No connection selected, showing connection selection dialog")
                    self.props.active_window.show_connection_selection_for_ssh_copy()
                else:
                    # Use the selected connection
                    self.props.active_window.on_copy_key_to_server_clicked(None)
                logger.debug("SSH key copy dialog shown successfully")
            except Exception as e:
                logger.error(f"Failed to show SSH key copy dialog: {e}")
        else:
            logger.warning("No active window found for new SSH key action")

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
            logger.error(f"Failed to open file manager via shortcut: {exc}")
            try:
                connection_name = getattr(connection, 'nickname', '') if 'connection' in locals() else ''
                if connection_name and hasattr(win, '_show_manage_files_error'):
                    win._show_manage_files_error(connection_name, str(exc))
            except Exception:
                pass

    def on_local_terminal(self, action, param):
        """Handle local terminal action"""
        logger.debug("Local terminal action triggered")
        if self.props.active_window:
            self.props.active_window.terminal_manager.show_local_terminal()

    def on_terminal_search(self, action, param):
        """Toggle the search overlay for the active terminal."""
        window = self.props.active_window
        if not window:
            return

        handler = getattr(window, 'toggle_terminal_search_overlay', None)
        if callable(handler):
            handler(select_all=True)

    def on_preferences(self, action, param):
        """Handle preferences action"""
        logger.debug("Preferences action triggered")
        if self.props.active_window:
            self.props.active_window.show_preferences()

    def on_about(self, action, param):
        """Handle about dialog action"""
        logger.debug("About dialog action triggered")
        if self.props.active_window:
            self.props.active_window.show_about_dialog()

    def on_help(self, action, param):
        """Handle help action"""
        logger.debug("Help action triggered")
        if self.props.active_window:
            self.props.active_window.open_help_url()

    def on_shortcuts(self, action, param):
        """Handle keyboard shortcuts overlay action"""
        logger.debug("Shortcuts action triggered")
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
        """Close the current tab — or, in a split-view tab, the focused pane."""
        win = self.props.active_window
        if not win:
            return
        try:
            if hasattr(win, '_close_active_tab_or_pane'):
                win._close_active_tab_or_pane()
            else:
                page = win.tab_view.get_selected_page()
                if page:
                    win.tab_view.close_page(page)
        except Exception:
            pass

    def on_tab_move_left(self, action, param):
        """Move the current tab one position to the left."""
        win = self.props.active_window
        if win and hasattr(win, '_move_tab_relative'):
            win._move_tab_relative(-1)

    def on_tab_move_right(self, action, param):
        """Move the current tab one position to the right."""
        win = self.props.active_window
        if win and hasattr(win, '_move_tab_relative'):
            win._move_tab_relative(1)

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
            logger.error(f"Failed to toggle tab overview: {e}")

    def on_broadcast_command(self, action, param):
        """Handle broadcast command action (Ctrl/⌘+Shift+B)"""
        logger.debug("Broadcast command action triggered")
        if self.props.active_window:
            # Forward to the window's action
            self.props.active_window.broadcast_command_action.activate(None)

    def on_new_split_view_tab(self, action, param=None):
        """Open a new empty split-view tab (Ctrl/⌘+Shift+S)."""
        logger.debug("New split view tab action triggered")
        win = self.props.active_window
        if win and hasattr(win, 'on_new_split_view_tab'):
            win.on_new_split_view_tab(action, param)

    def apply_color_overrides(self, config):
        """Apply color overrides to the application"""
        try:
            import gi
            gi.require_version('Gtk', '4.0')
            from gi.repository import Gtk, Gdk

            accent_color = config.get_setting('accent-color-override', None)

            if not accent_color:
                display = Gdk.Display.get_default()
                if display and hasattr(display, '_color_override_provider'):
                    Gtk.StyleContext.remove_provider_for_display(
                        display,
                        display._color_override_provider,
                    )
                    delattr(display, '_color_override_provider')
                return

            css_rules = [
                f"@define-color accent_color {accent_color};",
                f"@define-color accent_bg_color {accent_color};",
                "@define-color accent_fg_color white;",
                f"@define-color theme_selected_bg_color {accent_color};",
                "@define-color theme_selected_fg_color white;",
                f"@define-color theme_unfocused_selected_bg_color {accent_color};",
                "@define-color theme_unfocused_selected_fg_color white;",
            ]

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
                    if hasattr(display, '_color_override_provider'):
                        Gtk.StyleContext.remove_provider_for_display(
                            display,
                            display._color_override_provider,
                        )
                        delattr(display, '_color_override_provider')
                    Gtk.StyleContext.add_provider_for_display(
                        display,
                        provider,
                        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                    )
                    # Store provider reference for cleanup
                    display._color_override_provider = provider
                    logger.info("Applied accent color override on startup")

        except Exception as e:
            logger.error(f"Failed to apply color overrides: {e}")

def main():
    """Main entry point"""
    # Fast-path: handle --askpass before starting the GTK application.
    # This is reached when the app is invoked via a console-script entry point
    # (e.g. Homebrew pip-installed binary) where run.py is not used.
    if len(sys.argv) > 1 and sys.argv[1] == '--askpass':
        from .askpass_utils import run_askpass_and_write
        prompt = sys.argv[2] if len(sys.argv) > 2 else ""
        sys.exit(run_askpass_and_write(prompt))

    parser = argparse.ArgumentParser(description="sshPilot SSH connection manager")
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument(
        "--verbose", "-v", action="store_true",
        help="Verbose debug logging (overrides config)",
    )
    verbosity.add_argument(
        "--quiet", "-q", action="store_true",
        help="Only show warnings and errors (overrides config)",
    )
    parser.add_argument("--isolated", action="store_true", help="Use isolated SSH configuration")
    args = parser.parse_args()
    app = SshPilotApplication(
        verbose=args.verbose,
        quiet=args.quiet,
        isolated=args.isolated,
    )
    return app.run(None)  # Pass None to use default command line arguments

if __name__ == '__main__':
    main()
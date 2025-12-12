"""
Terminal Widget for sshPilot
Integrated VTE terminal with SSH connection handling using system SSH client
"""

import os
import sys
import logging
import signal
import time
import json
import re
import gi
from gettext import gettext as _
import asyncio
import threading
import weakref
import subprocess
import shutil
import pwd
from datetime import datetime
from typing import Optional, List
from .port_utils import get_port_checker
from .platform_utils import is_flatpak, is_macos
from .terminal_backends import BaseTerminalBackend, VTETerminalBackend, PyXtermTerminalBackend
from .ssh_connection_builder import build_ssh_connection, ConnectionContext

gi.require_version('Gtk', '4.0')
gi.require_version('Vte', '3.91')

gi.require_version('Adw', '1')
from gi.repository import Gtk, GObject, GLib, Vte, Pango, Gdk, Gio, Adw

logger = logging.getLogger(__name__)

class SSHProcessManager:
    """Manages SSH processes and ensures proper cleanup"""
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.processes = {}
            cls._instance.terminals = weakref.WeakSet()
            cls._instance.lock = threading.Lock()
            cls._instance.cleanup_thread = None
            cls._instance._start_cleanup_thread()
        return cls._instance
    
    def _start_cleanup_thread(self):
        """Start background cleanup thread"""
        # Disable automatic cleanup thread to prevent race conditions
        # Manual cleanup will happen on app shutdown via cleanup_all()
        logger.debug("Automatic SSH cleanup thread disabled to prevent race conditions")
    
    def _cleanup_loop(self):
        """Background cleanup loop"""
        while True:
            try:
                time.sleep(60)  # Increased from 30s to 60s to reduce interference
                self._cleanup_orphaned_processes()
            except Exception as e:
                logger.error(f"Error in cleanup loop: {e}")
    
    def _cleanup_orphaned_processes(self):
        """Clean up processes not tracked by active terminals"""
        with self.lock:
            active_pids = set()
            for terminal in list(self.terminals):
                try:
                    # Use stored PID instead of calling _get_terminal_pid() which can hang
                    pid = getattr(terminal, 'process_pid', None)
                    if pid:
                        active_pids.add(pid)
                        logger.debug(f"Terminal {id(terminal)} has active PID {pid}")
                except Exception as e:
                    logger.debug(f"Error getting stored PID from terminal: {e}")
            
            # Only clean up processes that are definitely orphaned AND old enough
            import time
            current_time = time.time()
            orphaned_pids = []
            
            for pid in list(self.processes.keys()):
                if pid not in active_pids:
                    # Check if process is old enough to be considered orphaned (10+ minutes)
                    process_info = self.processes.get(pid, {})
                    start_time = process_info.get('start_time')
                    if start_time and hasattr(start_time, 'timestamp'):
                        process_age = current_time - start_time.timestamp()
                        if process_age < 600:  # Less than 10 minutes old - be very conservative
                            logger.debug(f"Process {pid} is only {process_age:.1f}s old, skipping cleanup")
                            continue
                    else:
                        # If we don't have start time info, assume it's recent and skip cleanup
                        logger.debug(f"Process {pid} has no start_time info, skipping cleanup")
                        continue
                    
                    # Double-check: make sure the process actually exists before trying to kill it
                    try:
                        os.kill(pid, 0)  # Test if process exists
                        orphaned_pids.append(pid)
                        logger.debug(f"Found orphaned process {pid} (age: {process_age:.1f}s)")
                    except ProcessLookupError:
                        # Process already gone, just remove from tracking
                        logger.debug(f"Process {pid} already gone, removing from tracking")
                        if pid in self.processes:
                            del self.processes[pid]
                    except Exception as e:
                        logger.debug(f"Error checking process {pid}: {e}")
            
            # Clean up confirmed orphaned processes
            for pid in orphaned_pids:
                logger.info(f"Cleaning up orphaned process {pid}")
                self._terminate_process_by_pid(pid)
    
    def _terminate_process_by_pid(self, pid):
        """Terminate a process by PID"""
        try:
            # Always try process group first
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)

            # Wait with shorter timeout for faster cleanup
            for _ in range(3):  # 0.3 seconds max (reduced from 1 second)
                try:
                    os.killpg(pgid, 0)
                    time.sleep(0.1)
                except ProcessLookupError:
                    break
            else:
                # Force kill if still alive
                os.killpg(pgid, signal.SIGKILL)

            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass
            return True
        except Exception:
            return False
    
    def register_terminal(self, terminal):
        """Register a terminal for tracking"""
        self.terminals.add(terminal)
        logger.debug(f"Registered terminal {id(terminal)}")
    
    def cleanup_all(self):
        """Clean up all managed processes"""
        import signal
        
        def timeout_handler(signum, frame):
            logger.warning("Cleanup timeout - forcing exit")
            os._exit(1)
        
        # Set 5-second timeout for entire cleanup
        signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(5)
        
        try:
            logger.info("Cleaning up all SSH processes...")
            
            # First, mark all terminals as quitting to suppress signal handlers
            for terminal in list(self.terminals):
                terminal._is_quitting = True
            
            with self.lock:
                # Atomically extract and clear all processes
                processes_to_clean = dict(self.processes)
                self.processes.clear()
            
            # Clean up processes without holding the lock
            for pid, info in processes_to_clean.items():
                logger.debug(f"Cleaning up process {pid} (command: {info.get('command', 'unknown')})")
                self._terminate_process_by_pid(pid)
            
            # Clean up terminals separately
            for terminal in list(self.terminals):
                try:
                    if hasattr(terminal, 'disconnect') and hasattr(terminal, 'is_connected') and terminal.is_connected:
                        logger.debug(f"Disconnecting terminal {id(terminal)}")
                        terminal.disconnect()
                except Exception as e:
                    logger.error(f"Error cleaning up terminal {id(terminal)}: {e}")
            
            # Clear terminal references
            self.terminals.clear()
                
            logger.info("SSH process cleanup completed")
        finally:
            signal.alarm(0)  # Cancel timeout

# Global process manager instance
process_manager = SSHProcessManager()

class TerminalWidget(Gtk.Box):
    """A terminal widget that uses VTE for display and system SSH client for connections"""
    __gtype_name__ = 'TerminalWidget'
    
    # Signals
    __gsignals__ = {
        'connection-established': (GObject.SignalFlags.RUN_FIRST, None, ()),
        'connection-failed': (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'connection-lost': (GObject.SignalFlags.RUN_FIRST, None, ()),
        'title-changed': (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }
    
    def __init__(self, connection, config, connection_manager, group_color=None):

        # Initialize as a vertical Gtk.Box
        super().__init__(orientation=Gtk.Orientation.VERTICAL)

        # Store references
        self.connection = connection
        self.config = config
        self.connection_manager = connection_manager
        self.group_color = group_color


        # Process tracking
        self.process = None
        self.process_pid = None
        self.process_pgid = None
        self.is_connected = False
        self.watch_id = 0
        self.ssh_client = None
        self.session_id = str(id(self))  # Unique ID for this session
        self._is_quitting = False  # Flag to suppress signal handlers during quit
        self.last_error_message = None  # Store last SSH error for reporting
        self._fallback_timer_id = None  # GLib timeout ID for spawn fallback

        # Job detection state
        self._job_status = "UNKNOWN"  # IDLE, RUNNING, PROMPT, UNKNOWN
        self._shell_pgid = None  # Store shell process group ID for shell-agnostic detection
        
        # Current remote directory tracking (from window title)
        self._current_remote_directory = None  # Stores the current directory parsed from window title
        
        # Backend system
        self._backend_name = "vte"
        self.backend = None
        self.terminal_widget = None
        self._is_local_shell = False
        
        # Fullscreen state
        self._is_fullscreen = False
        self._fullscreen_sidebar_visible = None
        self._fullscreen_header_visible = None
        self._fullscreen_tab_bar_visible = None
        self._fullscreen_css_provider = None
        self._was_maximized = False
        self._fullscreen_banner_container = None
        self._fullscreen_banner_dismiss_button = None
        self._fullscreen_key_controller = None
        self._fullscreen_sidebar_collapsed = None
        
        # Register with process manager
        process_manager.register_terminal(self)
        
        # Connect to signals
        self.connect('destroy', self._on_destroy)
        
        # Connect to connection manager signals using GObject.GObject.connect directly
        self._connection_updated_handler = GObject.GObject.connect(connection_manager, 'connection-updated', self._on_connection_updated_signal)
        logger.debug("Connected to connection-updated signal")
        
        # Create scrolled window for terminal
        self.scrolled_window = Gtk.ScrolledWindow()
        self.scrolled_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        
        # Create backend first before setup
        self._shortcut_controller = None
        self._scroll_controller = None
        self._search_key_controller = None
        self._config_handler = None
        self._supported_encodings = None
        self._updating_encoding_config = False
        self._last_search_text = ''
        self._last_search_case_sensitive = False
        self._last_search_regex = False
        try:
            self._pass_through_mode = bool(self.config.get_setting('terminal.pass_through_mode', False))
        except Exception:
            self._pass_through_mode = False

        if hasattr(self.config, 'connect'):
            try:
                self._config_handler = self.config.connect('setting-changed', self._on_config_setting_changed)
            except Exception:
                self._config_handler = None

        # Create the backend before calling setup_terminal
        self.backend = self._create_backend()
        self.vte = getattr(self.backend, 'vte', None)
        self.terminal_widget = getattr(self.backend, 'widget', None)

        # Initialize terminal with basic settings and apply configured theme early
        self.setup_terminal()
        try:
            self.apply_theme()
        except Exception:
            pass
        
        # Add terminal to scrolled window and to the box via an overlay with a connecting view
        if self.terminal_widget is not None:
            self.scrolled_window.set_child(self.terminal_widget)
        self.overlay = Gtk.Overlay()
        self.overlay.set_child(self.scrolled_window)

        # Search overlay elements (revealer styled like other banners)
        self.search_revealer = Gtk.Revealer()
        self.search_revealer.set_reveal_child(False)
        self.search_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.search_revealer.set_halign(Gtk.Align.FILL)
        self.search_revealer.set_valign(Gtk.Align.START)
        self.search_revealer.set_hexpand(True)

        search_banner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        search_banner.add_css_class('banner')
        search_banner.set_hexpand(True)

        search_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        search_header.set_margin_start(12)
        search_header.set_margin_end(12)
        search_header.set_margin_top(8)
        search_header.set_margin_bottom(4)

        search_title = Gtk.Label(label=_("Search Terminal"))
        search_title.set_xalign(0)
        search_title.set_hexpand(True)
        search_title.add_css_class('title-4')
        search_header.append(search_title)

        from sshpilot import icon_utils
        self.search_close_button = Gtk.Button()
        icon_utils.set_button_icon(self.search_close_button, 'window-close-symbolic')
        self.search_close_button.add_css_class('flat')
        self.search_close_button.set_valign(Gtk.Align.CENTER)
        self.search_close_button.connect('clicked', lambda *_: self._hide_search_overlay())
        self.search_close_button.set_tooltip_text(_("Close terminal search"))
        search_header.append(self.search_close_button)

        search_banner.append(search_header)

        search_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        search_controls.set_margin_start(12)
        search_controls.set_margin_end(12)
        search_controls.set_margin_bottom(8)

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text(_("Search terminal history"))
        self.search_entry.set_hexpand(True)
        self.search_entry.connect('search-changed', self._on_search_entry_changed)
        self.search_entry.connect('activate', self._on_search_entry_activate)
        self.search_entry.connect('stop-search', self._on_search_entry_stop)

        entry_key_controller = Gtk.EventControllerKey()
        entry_key_controller.connect('key-pressed', self._on_search_entry_key_pressed)
        self.search_entry.add_controller(entry_key_controller)

        search_controls.append(self.search_entry)

        self.search_prev_button = Gtk.Button()
        icon_utils.set_button_icon(self.search_prev_button, 'go-up-symbolic')
        self.search_prev_button.set_tooltip_text(_("Find previous match"))
        self.search_prev_button.connect('clicked', self._on_search_previous)
        self.search_prev_button.set_sensitive(False)
        search_controls.append(self.search_prev_button)

        self.search_next_button = Gtk.Button()
        icon_utils.set_button_icon(self.search_next_button, 'go-down-symbolic')
        self.search_next_button.set_tooltip_text(_("Find next match"))
        self.search_next_button.connect('clicked', self._on_search_next)
        self.search_next_button.set_sensitive(False)
        search_controls.append(self.search_next_button)

        search_banner.append(search_controls)
        self.search_revealer.set_child(search_banner)

        # Install CSS for search banner to ensure solid background
        try:
            display = Gdk.Display.get_default()
            if display and not getattr(display, '_sshpilot_search_banner_css_installed', False):
                css_provider = Gtk.CssProvider()
                css_provider.load_from_data(b"""
                    .search-banner {
                        background-color: @headerbar_bg_color;
                        color: @headerbar_fg_color;
                        border-bottom: 1px solid @borders;
                    }
                """)
                Gtk.StyleContext.add_provider_for_display(
                    display, css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                )
                setattr(display, '_sshpilot_search_banner_css_installed', True)
        except Exception:
            pass

        # Add the search-banner CSS class to ensure solid background
        search_banner.add_css_class('search-banner')

        # Connecting overlay elements
        self.connecting_bg = Gtk.Box()
        self.connecting_bg.set_hexpand(True)
        self.connecting_bg.set_vexpand(True)
        try:
            provider = Gtk.CssProvider()
            provider.load_from_data(b".connecting-bg { background-color: #000000; }")
            display = Gdk.Display.get_default()
            if display:
                Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            if hasattr(self.connecting_bg, 'add_css_class'):
                self.connecting_bg.add_css_class('connecting-bg')
        except Exception:
            pass

        self.connecting_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.connecting_box.set_halign(Gtk.Align.CENTER)
        self.connecting_box.set_valign(Gtk.Align.CENTER)
        spinner = Gtk.Spinner()
        spinner.start()
        label = Gtk.Label()
        label.set_markup('<span color="#FFFFFF">Connecting</span>')
        self.connecting_box.append(spinner)
        self.connecting_box.append(label)

        self.overlay.add_overlay(self.connecting_bg)
        self.overlay.add_overlay(self.connecting_box)

        self.terminal_stack = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.terminal_stack.set_hexpand(True)
        self.terminal_stack.set_vexpand(True)
        self.terminal_stack.append(self.search_revealer)
        self.terminal_stack.append(self.overlay)

        # Set up drag and drop for SCP upload
        self._setup_drag_and_drop()

        # Disconnected banner with reconnect button at the bottom (separate panel below terminal)
        # Install CSS for a solid red background banner once
        try:
            display = Gdk.Display.get_default()
            if display and not getattr(display, '_sshpilot_banner_css_installed', False):
                css_provider = Gtk.CssProvider()
                css_provider.load_from_data(b"""
                    .error-toolbar.toolbar {
                        background-color: #cc0000;
                        color: #ffffff;
                        border-radius: 0;
                        padding-top: 10px;
                        padding-bottom: 10px;
                    }
                    .error-toolbar.toolbar label { color: #ffffff; }
                    .reconnect-button { background: #4a4a4a; color: #ffffff; border-radius: 4px; padding: 6px 10px; }
                    .reconnect-button:hover { background: #3f3f3f; }
                    .reconnect-button:active { background: #353535; }
                """)
                Gtk.StyleContext.add_provider_for_display(
                    display, css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                )
                setattr(display, '_sshpilot_banner_css_installed', True)
        except Exception:
            pass

        # Create error toolbar with same structure as sidebar toolbar
        self.disconnected_banner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.disconnected_banner.set_halign(Gtk.Align.FILL)
        self.disconnected_banner.set_valign(Gtk.Align.END)
        self.disconnected_banner.set_margin_start(0)
        self.disconnected_banner.set_margin_end(0)
        self.disconnected_banner.set_margin_top(0)
        self.disconnected_banner.set_margin_bottom(0)
        try:
            self.disconnected_banner.add_css_class('toolbar')
            self.disconnected_banner.add_css_class('error-toolbar')
            # Add a unique class per instance so we can set a per-widget min-height via CSS
            self._banner_unique_class = f"banner-{id(self)}"
            self.disconnected_banner.add_css_class(self._banner_unique_class)
        except Exception:
            pass
        # Banner content: icon + label + spacer + reconnect + dismiss, matching toolbar layout
        from sshpilot import icon_utils
        icon = icon_utils.new_image_from_icon_name('dialog-error-symbolic')
        icon.set_valign(Gtk.Align.CENTER)
        self.disconnected_banner.append(icon)
        self.disconnected_banner_label = Gtk.Label()
        self.disconnected_banner_label.set_halign(Gtk.Align.START)
        self.disconnected_banner_label.set_valign(Gtk.Align.CENTER)
        self.disconnected_banner_label.set_hexpand(True)
        self.disconnected_banner_label.set_text(_('Session ended.'))
        self.disconnected_banner.append(self.disconnected_banner_label)
        self.reconnect_button = Gtk.Button.new_with_label(_('Reconnect'))
        try:
            self.reconnect_button.add_css_class('reconnect-button')
        except Exception:
            pass
        self.reconnect_button.connect('clicked', self._on_reconnect_clicked)
        self.disconnected_banner.append(self.reconnect_button)

        # Dismiss button to hide the banner manually
        self.dismiss_button = Gtk.Button.new_with_label(_('Dismiss'))
        try:
            self.dismiss_button.add_css_class('flat')
            self.dismiss_button.add_css_class('reconnect-button')
        except Exception:
            pass
        self.dismiss_button.connect('clicked', lambda *_: self._set_disconnected_banner_visible(False))
        self.disconnected_banner.append(self.dismiss_button)
        self.disconnected_banner.set_visible(False)

        # Allow window to force an exact height match to the sidebar toolbar using per-widget CSS min-height
        self._banner_css_provider = None
        def _apply_external_height(new_h: int):
            try:
                h = max(0, int(new_h))
                display = Gdk.Display.get_default()
                if not display:
                    return
                css = f".{self._banner_unique_class} {{ min-height: {h}px; }}"
                provider = Gtk.CssProvider()
                provider.load_from_data(css.encode('utf-8'))
                Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
                # Keep a reference to prevent GC; latest provider wins at same priority
                self._banner_css_provider = provider
            except Exception:
                pass
        self.set_banner_height = _apply_external_height

        # Container to stack terminal (overlay) above the banner panel
        self.container_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.container_box.set_hexpand(True)
        self.container_box.set_vexpand(True)
        self.container_box.append(self.terminal_stack)
        self.container_box.append(self.disconnected_banner)

        self.append(self.container_box)

        # Set expansion properties
        self.scrolled_window.set_hexpand(True)
        self.scrolled_window.set_vexpand(True)
        if self.terminal_widget is not None:
            self.terminal_widget.set_hexpand(True)
            self.terminal_widget.set_vexpand(True)

        # Connect terminal signals and store handler IDs for cleanup
        self._child_exited_handler = None
        self._title_changed_handler = None
        self._termprops_changed_handler = None
        self._connect_backend_signals()
        
        # Apply theme
        self.force_style_refresh()
        
        # Set visibility of child widgets (GTK4 style)
        self.scrolled_window.set_visible(True)
        if self.terminal_widget is not None:
            self.terminal_widget.set_visible(True)
        
        # Show overlay initially
        self._set_connecting_overlay_visible(True)
        
        # Setup fullscreen keyboard shortcut (F11)
        self._setup_fullscreen_shortcut()
        
        logger.debug("Terminal widget initialized")

    def _create_backend(self, preferred: Optional[str] = None) -> BaseTerminalBackend:
        """Create the terminal backend based on configuration."""
        backend_name = preferred or "vte"
        if preferred is None and self.config:
            try:
                backend_name = self.config.get_setting("terminal.backend", backend_name)
            except Exception:
                backend_name = "vte"

        backend_name = (backend_name or "vte").lower()

        if backend_name == "pyxterm":
            try:
                backend = PyXtermTerminalBackend(self)
                if getattr(backend, "available", False):
                    logger.info("Using PyXterm terminal backend")
                    self._backend_name = "pyxterm"
                    return backend
                logger.warning("PyXterm backend unavailable, falling back to VTE")
            except Exception as e:
                logger.error(f"Failed to create PyXterm backend: {e}")
                logger.warning("PyXterm backend creation failed, falling back to VTE")

        logger.debug("Using VTE terminal backend")
        self._backend_name = "vte"
        return VTETerminalBackend(self)

    def _connect_backend_signals(self):
        """Connect to backend signals and store handler IDs."""
        backend = getattr(self, 'backend', None)
        if backend is None:
            return
        try:
            self._child_exited_handler = backend.connect_child_exited(self.on_child_exited)
        except Exception:
            self._child_exited_handler = None
        try:
            self._title_changed_handler = backend.connect_title_changed(self.on_title_changed)
        except Exception:
            self._title_changed_handler = None
        try:
            self._termprops_changed_handler = backend.connect_termprops_changed(self._on_termprops_changed)
        except Exception:
            self._termprops_changed_handler = None

    def _disconnect_backend_signals(self, backend: Optional[BaseTerminalBackend] = None):
        """Disconnect previously connected backend signals."""
        if backend is None:
            backend = getattr(self, 'backend', None)
        if backend is None:
            return
        try:
            if self._child_exited_handler is not None:
                backend.disconnect(self._child_exited_handler)
                self._child_exited_handler = None
        except Exception:
            pass
        try:
            if self._title_changed_handler is not None:
                backend.disconnect(self._title_changed_handler)
                self._title_changed_handler = None
        except Exception:
            pass
        try:
            if self._termprops_changed_handler is not None:
                backend.disconnect(self._termprops_changed_handler)
                self._termprops_changed_handler = None
        except Exception:
            pass

    def get_backend_name(self) -> str:
        """Get the name of the current backend."""
        return getattr(self, '_backend_name', 'vte')

    def ensure_backend(self, backend_name: Optional[str] = None) -> None:
        """Switch to the specified backend if different from current."""
        if backend_name is None:
            if self.config:
                try:
                    backend_name = self.config.get_setting("terminal.backend", "vte")
                except Exception:
                    backend_name = "vte"
            else:
                backend_name = "vte"

        backend_name = (backend_name or "vte").lower()
        current_name = self.get_backend_name()

        if current_name.lower() == backend_name.lower():
            return  # Already using the requested backend

        logger.info(f"Switching terminal backend from {current_name} to {backend_name}")

        # Disconnect old backend signals
        self._disconnect_backend_signals()

        # Clean up context menu popover and gesture before destroying backend
        # This prevents GTK warnings about children left when finalizing widgets
        if hasattr(self, '_menu_popover') and self._menu_popover is not None:
            try:
                # Popdown the menu if it's open
                if hasattr(self._menu_popover, 'popdown'):
                    self._menu_popover.popdown()
                # Detach from parent widget
                if hasattr(self._menu_popover, 'set_parent'):
                    self._menu_popover.set_parent(None)
                # Unparent the popover
                if hasattr(self._menu_popover, 'unparent'):
                    self._menu_popover.unparent()
                logger.debug("Detached context menu popover before backend switch")
            except Exception as e:
                logger.debug(f"Error detaching popover: {e}", exc_info=True)
        
        # Remove gesture controller from old backend widget
        # Try all possible widget locations where gesture might be attached
        if hasattr(self, '_menu_gesture') and self._menu_gesture is not None:
            widgets_to_check = []
            if hasattr(self, 'backend') and self.backend and hasattr(self.backend, 'widget'):
                widgets_to_check.append(self.backend.widget)
            if hasattr(self, 'terminal_widget') and self.terminal_widget:
                widgets_to_check.append(self.terminal_widget)
            if hasattr(self, 'vte') and self.vte:
                widgets_to_check.append(self.vte)
            
            for widget in widgets_to_check:
                try:
                    if hasattr(widget, 'remove_controller'):
                        widget.remove_controller(self._menu_gesture)
                        logger.debug(f"Removed context menu gesture from {type(widget).__name__}")
                        break  # Only need to remove once
                except Exception as e:
                    logger.debug(f"Error removing gesture from {type(widget).__name__}: {e}", exc_info=True)

        # Destroy old backend
        old_backend = getattr(self, 'backend', None)
        if old_backend is not None:
            try:
                old_backend.destroy()
            except Exception:
                pass

        # Remove old widget from scrolled window
        if self.terminal_widget is not None:
            try:
                self.scrolled_window.set_child(None)
            except Exception:
                pass

        # Create new backend
        self.backend = self._create_backend(backend_name)
        self.vte = getattr(self.backend, 'vte', None)
        self.terminal_widget = getattr(self.backend, 'widget', None)

        # Add new widget to scrolled window
        if self.terminal_widget is not None:
            self.scrolled_window.set_child(self.terminal_widget)
            self.terminal_widget.set_hexpand(True)
            self.terminal_widget.set_vexpand(True)
            self.terminal_widget.set_visible(True)

        # Reconnect signals
        self._connect_backend_signals()

        # Reapply theme and settings
        try:
            self.setup_terminal()
            self.apply_theme()
        except Exception:
            pass

    def _set_disconnected_banner_visible(self, visible: bool, message: str = None):
        try:
            # Allow callers (e.g., ssh-copy-id dialog) to suppress the red banner entirely
            if getattr(self, '_suppress_disconnect_banner', False):
                return
            if message:
                self.disconnected_banner_label.set_text(message)
            if hasattr(self.disconnected_banner, 'set_visible'):
                self.disconnected_banner.set_visible(visible)
        except Exception:
            pass

    def _on_reconnect_clicked(self, *args):
        """User clicked reconnect on the banner"""
        try:
            # Immediately hide banner and show connecting overlay
            self._set_disconnected_banner_visible(False)
            self._set_connecting_overlay_visible(True)
            # Rebuild the SSH command with the latest preferences before reconnecting
            def _prepare_and_connect():
                prepared = False
                try:
                    prepared = self._refresh_connection_command()
                except Exception as exc:
                    logger.error(f"Failed to refresh SSH command before reconnect: {exc}")
                    prepared = False

                if not prepared:
                    self._set_connecting_overlay_visible(False)
                    self._set_disconnected_banner_visible(True, _('Reconnect failed to start'))
                    return False

                if not self._connect_ssh():
                    # Show banner again if failed to start reconnect
                    self._set_connecting_overlay_visible(False)
                    self._set_disconnected_banner_visible(True, _('Reconnect failed to start'))
                return False

            GLib.idle_add(_prepare_and_connect)
        except Exception:
            self._set_connecting_overlay_visible(False)
            self._set_disconnected_banner_visible(True, _('Reconnect failed'))

    def _refresh_connection_command(self) -> bool:
        """Refresh the prepared SSH command using current preferences."""

        connection = getattr(self, 'connection', None)
        if not connection:
            logger.error('Reconnect requested without an active connection')
            return False

        try:
            if hasattr(connection, 'ssh_cmd'):
                connection.ssh_cmd = []
        except Exception as exc:
            logger.debug(f"Unable to reset cached ssh_cmd before reconnect: {exc}")

        connect_coro = None
        use_native = False
        try:
            use_native = bool(getattr(self.connection_manager, 'native_connect_enabled', False))
            if not use_native:
                app = Adw.Application.get_default()
                if app is not None and hasattr(app, 'native_connect_enabled'):
                    use_native = bool(app.native_connect_enabled)
        except Exception:
            use_native = False

        try:
            if use_native and hasattr(connection, 'native_connect'):
                connect_coro = connection.native_connect()
            elif hasattr(connection, 'connect'):
                connect_coro = connection.connect()
        except Exception as exc:
            logger.error(f"Failed to build connection coroutine for reconnect: {exc}")
            connect_coro = None

        if connect_coro is None:
            logger.error('Unable to refresh SSH command; missing connect coroutine')
            return False

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        try:
            if loop.is_running():
                future = asyncio.run_coroutine_threadsafe(connect_coro, loop)
                future.result()
            else:
                loop.run_until_complete(connect_coro)
        except Exception as exc:
            logger.error(f"Failed to refresh SSH command for reconnect: {exc}")
            return False

        return bool(getattr(connection, 'ssh_cmd', None))

    def _set_connecting_overlay_visible(self, visible: bool):
        try:
            if hasattr(self.connecting_bg, 'set_visible'):
                self.connecting_bg.set_visible(visible)
            if hasattr(self.connecting_box, 'set_visible'):
                self.connecting_box.set_visible(visible)
        except Exception:
            pass
    
    def _connect_ssh(self):
        """Connect to SSH host"""
        if not self.connection:
            logger.error("No connection configured")
            return False
            
        # Ensure terminal backend is properly initialized
        if not hasattr(self, 'backend') or self.backend is None:
            logger.error("Terminal backend not initialized")
            return False
        
        try:
            # Connect in a separate thread to avoid blocking UI
            thread = threading.Thread(target=self._connect_ssh_thread)
            thread.daemon = True
            thread.start()
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to start SSH connection: {e}")
            GLib.idle_add(self._on_connection_failed, str(e))
            return False
    
    def _connect_ssh_thread(self):
        """SSH connection thread: directly spawn SSH and rely on its output for errors."""
        try:
            GLib.idle_add(self._setup_ssh_terminal)
        except Exception as e:
            logger.error(f"SSH connection failed: {e}")
            GLib.idle_add(self._on_connection_failed, str(e))

    def _prepare_key_for_native_mode(self):
        """Ensure explicit keys are unlocked when native SSH mode is active."""
        connection = getattr(self, 'connection', None)
        if not connection:
            return

        if getattr(connection, 'identity_agent_disabled', False):
            logger.debug("IdentityAgent disabled; skipping native key preload")
            return

        manager = getattr(self, 'connection_manager', None)
        if not manager or not hasattr(manager, 'prepare_key_for_connection'):
            return

        try:
            key_select_mode = int(getattr(connection, 'key_select_mode', 0) or 0)
        except Exception:
            key_select_mode = 0

        if key_select_mode not in (1, 2):
            candidate_keys = self._resolve_native_identity_candidates()
            attempted = False

            for candidate in candidate_keys:
                expanded = os.path.expanduser(candidate)
                if not os.path.isfile(expanded):
                    logger.debug(
                        "Identity candidate not found for native key preload: %s",
                        candidate,
                    )
                    continue

                attempted = True

                try:
                    prepared = manager.prepare_key_for_connection(expanded)
                except Exception as exc:
                    logger.warning(
                        "Failed to prepare key for native SSH connection (%s): %s",
                        expanded,
                        exc,
                    )
                    continue

                if prepared:
                    logger.debug(
                        "Prepared key for native SSH connection: %s",
                        expanded,
                    )
                    return

                logger.warning(
                    "Key could not be prepared for native SSH connection: %s",
                    expanded,
                )

            if not attempted:
                logger.debug("No matching identity files found for native key preload")

            return

        keyfile = getattr(connection, 'keyfile', '') or ''
        if not keyfile or keyfile.startswith('Select key file'):
            return

        expanded_keyfile = os.path.expanduser(keyfile)
        key_path = expanded_keyfile if os.path.isfile(expanded_keyfile) else keyfile
        if not os.path.isfile(key_path):
            logger.debug("Explicit key not found on disk, skipping native preload: %s", keyfile)
            return

        try:
            prepared = manager.prepare_key_for_connection(key_path)
        except Exception as exc:
            logger.warning("Failed to prepare key for native SSH connection: %s", exc)
            return

        if prepared:
            logger.debug("Prepared key for native SSH connection: %s", key_path)
        else:
            logger.warning("Key could not be prepared for native SSH connection: %s", key_path)

    def _resolve_native_identity_candidates(self) -> List[str]:
        """Return identity file candidates for native SSH preload attempts."""

        connection = getattr(self, 'connection', None)
        if not connection:
            return []

        candidates: List[str] = []
        try:
            resolved = getattr(connection, 'resolved_identity_files', [])
        except Exception:
            resolved = []

        if isinstance(resolved, (list, tuple)):
            for value in resolved:
                expanded = os.path.expanduser(str(value))
                if os.path.isfile(expanded) and expanded not in candidates:
                    candidates.append(expanded)

        if candidates:
            return candidates

        if hasattr(connection, 'collect_identity_file_candidates'):
            try:
                fallback = connection.collect_identity_file_candidates()
            except Exception:
                fallback = []

            for value in fallback:
                expanded = os.path.expanduser(str(value))
                if os.path.isfile(expanded) and expanded not in candidates:
                    candidates.append(expanded)

        return candidates

    def _setup_ssh_terminal(self):
        """Set up terminal with direct SSH command using ssh_connection_builder (called from main thread)"""
        try:
            # Check for pre-built SSH command from connection (for compatibility)
            ssh_conn_cmd = None
            if hasattr(self.connection, 'ssh_cmd'):
                prepared = getattr(self.connection, 'ssh_cmd', None)


                if isinstance(prepared, (list, tuple)):
                    base_cmd = list(prepared)
                    using_prepared_cmd = len(base_cmd) > 0

            if not base_cmd:
                base_cmd = ['ssh']

            ssh_cmd = list(base_cmd)

            def ensure_option(option: str):
                if option not in ssh_cmd:
                    ssh_cmd.extend(['-o', option])

            def remove_option(option_name: str, keep_value: Optional[str] = None):
                prefix = f'{option_name}='
                idx = 0
                found_keep = False
                while idx < len(ssh_cmd):
                    if ssh_cmd[idx] == '-o' and idx + 1 < len(ssh_cmd):
                        opt_value = ssh_cmd[idx + 1]
                        if opt_value.startswith(prefix):
                            if (
                                keep_value is not None
                                and opt_value == f'{option_name}={keep_value}'
                                and not found_keep
                            ):
                                found_keep = True
                                idx += 2
                                continue
                            del ssh_cmd[idx:idx + 2]
                            continue
                    idx += 1

            def sync_option(option_name: str, desired_value: Optional[str]):
                if desired_value is None:
                    remove_option(option_name)
                else:
                    remove_option(option_name, desired_value)
                    ensure_option(f'{option_name}={desired_value}')

            def ensure_flag(flag: str):
                if flag not in ssh_cmd:
                    ssh_cmd.append(flag)

            def remove_flag(flag: str):
                while flag in ssh_cmd:
                    ssh_cmd.remove(flag)

            ssh_cfg = {}
            try:
                cfg_obj = getattr(self, 'config', None)
                if cfg_obj is not None and hasattr(cfg_obj, 'get_ssh_config'):
                    ssh_cfg = cfg_obj.get_ssh_config()
            except Exception:
                ssh_cfg = {}
            native_mode_enabled = bool(getattr(self.connection_manager, 'native_connect_enabled', False))
            try:
                app = Adw.Application.get_default()
                if not native_mode_enabled and app is not None and hasattr(app, 'native_connect_enabled'):
                    native_mode_enabled = bool(app.native_connect_enabled)
            except Exception:
                pass
            quick_connect_mode = bool(getattr(self.connection, 'quick_connect_command', ''))
            if native_mode_enabled:
                quick_connect_mode = False
                self._prepare_key_for_native_mode()
            password_auth_selected = False
            has_saved_password = False
            password_value = None
            auth_method = 0
            resolved_for_connection = ''

            def _resolve_host_for_connection() -> str:
                if not hasattr(self, 'connection') or not self.connection:
                    return ''
                try:
                    host_value = self.connection.get_effective_host()
                except AttributeError:
                    host_value = getattr(self.connection, 'hostname', '') or getattr(self.connection, 'host', '')
                if not host_value:
                    host_value = getattr(self.connection, 'nickname', '')
                return host_value or ''

            try:
                resolved_for_connection = _resolve_host_for_connection()
            except Exception:
                resolved_for_connection = ''

            try:
                # In our UI: 0 = key-based, 1 = password
                auth_method = getattr(self.connection, 'auth_method', 0)
                password_auth_selected = (auth_method == 1)
                # Try to fetch stored password regardless of auth method
                password_value = getattr(self.connection, 'password', None)
                if not password_value and hasattr(self, 'connection_manager') and self.connection_manager:
                    lookup_host = resolved_for_connection or _resolve_host_for_connection()
                    username_for_lookup = getattr(self.connection, 'username', None)
                    password_value = self.connection_manager.get_password(
                        lookup_host,
                        username_for_lookup,
                    )

                has_saved_password = bool(password_value)
            except Exception:
                auth_method = 0
                password_auth_selected = False
                has_saved_password = False

            using_password = password_auth_selected or has_saved_password

            # Initialize env early to ensure it's available in all code paths
            env = os.environ.copy()
            ssh_conn_cmd = None

            if not quick_connect_mode:
                batch_mode_pref = bool(ssh_cfg.get('batch_mode', False))
                desired_batch_mode = 'yes' if batch_mode_pref and not using_password else None
                sync_option('BatchMode', desired_batch_mode)

            if native_mode_enabled and not ssh_cmd:
                host_label = ''

                try:
                    cfg_obj = getattr(self, 'config', None)
                    if cfg_obj is not None and hasattr(cfg_obj, 'get_ssh_config'):
                        ssh_cfg = cfg_obj.get_ssh_config()
                except Exception:
                    ssh_cfg = {}
                
                native_mode_enabled = bool(getattr(self.connection_manager, 'native_connect_enabled', False))
                try:
                    app = Adw.Application.get_default()
                    if not native_mode_enabled and app is not None and hasattr(app, 'native_connect_enabled'):
                        native_mode_enabled = bool(app.native_connect_enabled)
                except Exception:
                    pass


                # Use custom known hosts file when available
                try:
                    if getattr(self, 'connection_manager', None):
                        kh_path = getattr(self.connection_manager, 'known_hosts_path', '')
                        if kh_path and os.path.exists(kh_path):
                            ensure_option(f'UserKnownHostsFile={kh_path}')
                except Exception:
                    logger.debug('Failed to set UserKnownHostsFile option', exc_info=True)

                # Ensure SSH exits immediately on failure rather than waiting in background
                sync_option('ExitOnForwardFailure', 'yes')

                # Only add verbose flag if explicitly enabled in config
                try:
                    verbosity = int(ssh_cfg.get('verbosity', 0))
                    debug_enabled = bool(ssh_cfg.get('debug_enabled', False))
                    v = max(0, min(3, verbosity))
                    remove_flag('-v')
                    for _ in range(v):
                        ssh_cmd.append('-v')
                    # Map verbosity to LogLevel to ensure messages are not suppressed by defaults
                    remove_option('LogLevel')
                    if v == 1:
                        ensure_option('LogLevel=VERBOSE')
                    elif v == 2:
                        ensure_option('LogLevel=DEBUG2')
                    elif v >= 3:
                        ensure_option('LogLevel=DEBUG3')
                    elif debug_enabled:
                        ensure_option('LogLevel=DEBUG')
                    if v > 0 or debug_enabled:
                        logger.debug("SSH verbosity configured: -v x %d, LogLevel set", v)
                except Exception as e:
                    logger.warning(f"Could not check SSH verbosity/debug settings: {e}")
                    # Default to non-verbose on error

                # Add key file/options only for key-based auth
                if not password_auth_selected:
                    # Get key selection mode
                    key_select_mode = 0
                    try:
                        key_select_mode = int(getattr(self.connection, 'key_select_mode', 0) or 0)
                    except Exception:
                        pass

                    keyfile_value = getattr(self.connection, 'keyfile', '') or ''
                    has_explicit_key = bool(
                        keyfile_value
                        and not str(keyfile_value).startswith('Select key file')
                        and os.path.isfile(keyfile_value)
                    )

                    if has_explicit_key and hasattr(self, 'connection_manager') and self.connection_manager:
                        if getattr(self.connection, 'identity_agent_disabled', False):
                            logger.debug(
                                "IdentityAgent disabled; skipping key preparation before connection"
                            )
                        else:
                            try:
                                if hasattr(self.connection_manager, 'prepare_key_for_connection'):
                                    key_prepared = self.connection_manager.prepare_key_for_connection(keyfile_value)
                                    if key_prepared:
                                        logger.debug(f"Key prepared for connection: {keyfile_value}")
                                    else:
                                        logger.warning(f"Failed to prepare key for connection: {keyfile_value}")
                            except Exception as e:
                                logger.warning(f"Error preparing key for connection: {e}")

                    # Only add specific key when a dedicated key mode is selected
                    if has_explicit_key and key_select_mode in (1, 2):
                        if keyfile_value not in ssh_cmd:
                            ssh_cmd.extend(['-i', keyfile_value])
                        logger.debug(f"Using SSH key: {keyfile_value}")
                        if key_select_mode == 1:
                            ensure_option('IdentitiesOnly=yes')

                        # Add certificate if specified
                        if hasattr(self.connection, 'certificate') and self.connection.certificate and \
                           os.path.isfile(self.connection.certificate):
                            ensure_option(f'CertificateFile={self.connection.certificate}')
                            logger.debug(f"Using SSH certificate: {self.connection.certificate}")
                    else:
                        logger.debug("Using default SSH key selection (key_select_mode=0 or no valid key specified)")

                    # If a password exists, allow all standard authentication methods
                    if has_saved_password:
                        ensure_option('PreferredAuthentications=gssapi-with-mic,hostbased,publickey,keyboard-interactive,password')
                else:
                    # Force password authentication when user chose password auth
                    ensure_option('PreferredAuthentications=password')
                    if getattr(self.connection, 'pubkey_auth_no', False):
                        ensure_option('PubkeyAuthentication=no')

                # Add X11 forwarding if enabled
                if hasattr(self.connection, 'x11_forwarding') and self.connection.x11_forwarding:
                    ensure_flag('-X')

                # Prepare command-related options (must appear before host)

                remote_cmd = ''
                local_cmd = ''
                try:
                    if hasattr(self.connection, 'remote_command'):
                        remote_cmd = (self.connection.remote_command or '').strip()
                    if not remote_cmd and hasattr(self.connection, 'data'):
                        remote_cmd = (self.connection.data.get('remote_command') or '').strip()
                except Exception:
                    remote_cmd = ''
                try:
                    if hasattr(self.connection, 'local_command'):
                        local_cmd = (self.connection.local_command or '').strip()
                    if not local_cmd and hasattr(self.connection, 'data'):
                        local_cmd = (self.connection.data.get('local_command') or '').strip()
                except Exception:
                    local_cmd = ''
                
                # Get port forwarding rules with conflict checking
                port_forwarding_rules = None
                if hasattr(self.connection, 'forwarding_rules'):
                    port_checker = get_port_checker()
                    port_conflicts = []
                    filtered_rules = []
                    
                    for rule in self.connection.forwarding_rules:
                        if not rule.get('enabled', True):
                            continue
                        
                        rule_type = rule.get('type')
                        listen_addr = (rule.get('listen_addr') or 'localhost').strip()
                        listen_port = rule.get('listen_port')
                        
                        # Check for local port conflicts (for local and dynamic forwarding)
                        if rule_type in ['local', 'dynamic'] and listen_port:
                            try:
                                conflicts = port_checker.get_port_conflicts([listen_port], listen_addr)
                                if conflicts:
                                    port, port_info = conflicts[0]
                                    conflict_msg = f"Port {port} is already in use"
                                    if port_info.process_name:
                                        conflict_msg += f" by {port_info.process_name} (PID: {port_info.pid})"
                                    port_conflicts.append(conflict_msg)
                                    continue  # Skip this rule
                            except Exception as e:
                                logger.debug(f"Could not check port conflict for {listen_port}: {e}")
                        
                        filtered_rules.append(rule)
                    
                    # Show port conflict warnings if any
                    if port_conflicts:
                        conflict_message = "Port forwarding conflicts detected:\n" + "\n".join([f"• {msg}" for msg in port_conflicts])
                        logger.warning(conflict_message)
                        GLib.idle_add(self._show_forwarding_error_dialog, conflict_message)
                    
                    port_forwarding_rules = filtered_rules if filtered_rules else None
                
                # Get known hosts path
                known_hosts_path = None
                try:
                    if getattr(self, 'connection_manager', None):
                        kh_path = getattr(self.connection_manager, 'known_hosts_path', '')
                        if kh_path and os.path.exists(kh_path):
                            known_hosts_path = kh_path
                except Exception:
                    pass
                
                # Get extra SSH config
                extra_ssh_config = getattr(self.connection, 'extra_ssh_config', '').strip() or None
                
                # Build connection context
                ctx = ConnectionContext(
                    connection=self.connection,
                    connection_manager=self.connection_manager,
                    config=self.config,
                    command_type='ssh',
                    extra_args=[],
                    port_forwarding_rules=port_forwarding_rules,
                    remote_command=remote_cmd if remote_cmd else None,
                    local_command=local_cmd if local_cmd else None,
                    extra_ssh_config=extra_ssh_config,
                    known_hosts_path=known_hosts_path,
                    native_mode=native_mode_enabled,
                    quick_connect_mode=quick_connect_mode,
                    quick_connect_command=getattr(self.connection, 'quick_connect_command', None) or None,
                )
                
                # Build SSH connection command
                ssh_conn_cmd = build_ssh_connection(ctx)
                ssh_cmd = ssh_conn_cmd.command
                env.update(ssh_conn_cmd.env)
                
                # Get password for sshpass if needed
                password_value = None
                if ssh_conn_cmd.use_sshpass and ssh_conn_cmd.password:
                    password_value = ssh_conn_cmd.password
                
                logger.debug(f"Built SSH command using ssh_connection_builder: {' '.join(ssh_cmd)}")

            # Handle password authentication with sshpass if available (terminal-specific FIFO handling)
            logger.debug(f"Initial environment SSH_ASKPASS: {env.get('SSH_ASKPASS', 'NOT_SET')}, SSH_ASKPASS_REQUIRE: {env.get('SSH_ASKPASS_REQUIRE', 'NOT_SET')}")

            if password_value:
                # Use sshpass for password authentication with FIFO (terminal-specific)
                import shutil
                sshpass_path = None
                
                # Check if sshpass is available and executable
                if os.path.exists('/app/bin/sshpass') and os.access('/app/bin/sshpass', os.X_OK):
                    sshpass_path = '/app/bin/sshpass'
                    logger.debug("Found sshpass at /app/bin/sshpass")
                elif shutil.which('sshpass'):
                    sshpass_path = shutil.which('sshpass')
                    logger.debug(f"Found sshpass in PATH: {sshpass_path}")
                else:
                    logger.debug("sshpass not found or not executable")
                
                if sshpass_path:
                    # Use the same approach as ssh_password_exec.py for consistency
                    from .ssh_password_exec import _mk_priv_dir, _write_once_fifo
                    import threading
                    
                    # Create private temp directory and FIFO
                    tmpdir = _mk_priv_dir()
                    fifo = os.path.join(tmpdir, "pw.fifo")
                    os.mkfifo(fifo, 0o600)
                    
                    # Start writer thread that writes the password exactly once
                    t = threading.Thread(target=_write_once_fifo, args=(fifo, password_value), daemon=True)
                    t.start()
                    
                    # Use sshpass with FIFO
                    ssh_cmd = [sshpass_path, "-f", fifo] + ssh_cmd
                    
                    # Important: strip askpass vars so OpenSSH won't try the askpass helper for passwords
                    env.pop("SSH_ASKPASS", None)
                    env.pop("SSH_ASKPASS_REQUIRE", None)
                    
                    logger.debug("Using sshpass with FIFO for password authentication")
                    logger.debug(f"Environment after removing SSH_ASKPASS: SSH_ASKPASS={env.get('SSH_ASKPASS', 'NOT_SET')}, SSH_ASKPASS_REQUIRE={env.get('SSH_ASKPASS_REQUIRE', 'NOT_SET')}")
                    
                    # Store tmpdir for cleanup
                    self._sshpass_tmpdir = tmpdir
                else:
                    # sshpass not available – allow interactive password prompt
                    env.pop("SSH_ASKPASS", None)
                    env.pop("SSH_ASKPASS_REQUIRE", None)
                    logger.warning("sshpass not available; falling back to interactive password prompt")
            
            # Enable askpass log forwarding if using askpass
            if ssh_conn_cmd and ssh_conn_cmd.use_askpass:
                self._enable_askpass_log_forwarding(include_existing=True)
            
            # Set TERM to a proper value only if missing or set to "dumb"
            if 'TERM' not in env or env.get('TERM', '').lower() == 'dumb':
                env['TERM'] = 'xterm-256color'
            env['SHELL'] = env.get('SHELL', '/bin/bash')
            env['SSHPILOT_FLATPAK'] = '1'
            # Add /app/bin to PATH for Flatpak compatibility
            if os.path.exists('/app/bin'):
                current_path = env.get('PATH', '')
                if '/app/bin' not in current_path:
                    env['PATH'] = f"/app/bin:{current_path}"
            
            # Convert environment dict to list format expected by VTE
            env_list = []
            for key, value in env.items():
                env_list.append(f"{key}={value}")
            
            # Log the command being executed for debugging
            logger.debug(f"Spawning SSH command: {ssh_cmd}")
            logger.debug(f"Environment PATH: {env.get('PATH', 'NOT_SET')}")
            
            # Create a new PTY for the terminal (VTE-specific, but backend may handle this)
            # According to VTE docs, we should set PTY size before spawning to avoid SIGWINCH
            pty = None
            if hasattr(self.backend, 'get_pty') and callable(self.backend.get_pty):
                pty = self.backend.get_pty()
            if pty is None and self.vte is not None:
                try:
                    pty = Vte.Pty.new_sync(Vte.PtyFlags.DEFAULT)
                    # Set PTY size before spawning to avoid child process receiving SIGWINCH
                    # Get terminal size (rows, columns)
                    try:
                        rows = self.vte.get_row_count()
                        cols = self.vte.get_column_count()
                        # Only set size if we have valid dimensions (not default 80x24)
                        if rows > 0 and cols > 0 and (rows != 24 or cols != 80):
                            pty.set_size(rows, cols)
                            logger.debug(f"Set PTY size to {rows}x{cols} before spawn")
                    except Exception as e:
                        logger.debug(f"Could not set PTY size before spawn: {e}")
                    # Associate PTY with Terminal so spawn_async uses it
                    try:
                        self.vte.set_pty(pty)
                    except Exception as e:
                        logger.debug(f"Could not set PTY on terminal: {e}")
                except Exception:
                    pass
            
            # Convert env_list to dict for backend
            env_dict = {}
            if env_list:
                for env_item in env_list:
                    if '=' in env_item:
                        key, value = env_item.split('=', 1)
                        env_dict[key] = value
            
            try:
                self.backend.spawn_async(
                    argv=ssh_cmd,
                    env=env_dict if env_dict else None,
                    cwd=os.path.expanduser('~') or '/',
                    flags=0,
                    child_setup=None,
                    callback=self._on_spawn_complete,
                    user_data=()
                )
            except GLib.Error as e:
                logger.error(f"VTE spawn failed with GLib error: {e}")
                # Check if it's a "No such file or directory" error for sshpass
                if "sshpass" in str(e) and "No such file or directory" in str(e):
                    logger.error("sshpass binary not found, falling back to askpass")
                    # Fall back to askpass method
                    self._fallback_to_askpass(ssh_cmd, env_list)
                else:
                    self._on_connection_failed(str(e))
                return
            except Exception as e:
                logger.error(f"VTE spawn failed with exception: {e}")
                self._on_connection_failed(str(e))
                return
            
            # Store the PTY for later cleanup
            self.pty = pty
            try:
                import time
                self._spawn_start_time = time.time()
            except Exception:
                self._spawn_start_time = None
            
            # Defer marking as connected until spawn completes
            try:
                self.apply_theme()
            except Exception:
                pass
            
            # Apply theme after connection is established
            self.apply_theme()
            
            # Focus the terminal
            if self.backend:
                self.backend.grab_focus()

            # Add fallback timer to hide spinner if spawn completion doesn't fire
            self._fallback_timer_id = GLib.timeout_add_seconds(5, self._fallback_hide_spinner)

            logger.info(f"SSH terminal connected to {self.connection}")
            
        except Exception as e:
            logger.error(f"Failed to setup SSH terminal: {e}")
            self._on_connection_failed(str(e))
            return

            if has_saved_password and password_value:
                # Use sshpass for password authentication
                import shutil
                sshpass_path = None
                
                # Check if sshpass is available and executable
                if os.path.exists('/app/bin/sshpass') and os.access('/app/bin/sshpass', os.X_OK):
                    sshpass_path = '/app/bin/sshpass'
                    logger.debug("Found sshpass at /app/bin/sshpass")
                elif shutil.which('sshpass'):
                    sshpass_path = shutil.which('sshpass')
                    logger.debug(f"Found sshpass in PATH: {sshpass_path}")
                else:
                    logger.debug("sshpass not found or not executable")
                
                if sshpass_path:
                    # Use the same approach as ssh_password_exec.py for consistency
                    from .ssh_password_exec import _mk_priv_dir, _write_once_fifo
                    import threading
                    
                    # Create private temp directory and FIFO
                    tmpdir = _mk_priv_dir()
                    fifo = os.path.join(tmpdir, "pw.fifo")
                    os.mkfifo(fifo, 0o600)
                    
                    # Start writer thread that writes the password exactly once
                    t = threading.Thread(target=_write_once_fifo, args=(fifo, password_value), daemon=True)
                    t.start()
                    
                    # Use sshpass with FIFO
                    ssh_cmd = [sshpass_path, "-f", fifo] + ssh_cmd
                    
                    # Important: strip askpass vars so OpenSSH won't try the askpass helper for passwords
                    env.pop("SSH_ASKPASS", None)
                    env.pop("SSH_ASKPASS_REQUIRE", None)
                    
                    logger.debug("Using sshpass with FIFO for password authentication")
                    logger.debug(f"Environment after removing SSH_ASKPASS: SSH_ASKPASS={env.get('SSH_ASKPASS', 'NOT_SET')}, SSH_ASKPASS_REQUIRE={env.get('SSH_ASKPASS_REQUIRE', 'NOT_SET')}")
                    
                    # Store tmpdir for cleanup
                    self._sshpass_tmpdir = tmpdir
                else:
                    # sshpass not available – allow interactive password prompt
                    env.pop("SSH_ASKPASS", None)
                    env.pop("SSH_ASKPASS_REQUIRE", None)
                    logger.warning("sshpass not available; falling back to interactive password prompt")
            elif (
                (password_auth_selected or auth_method == 0)
                and not has_saved_password
                and not getattr(self.connection, 'identity_agent_disabled', False)
            ):
                # Password may be required but none saved - allow interactive prompt
                logger.debug("No saved password - using interactive prompt if required")
            else:
                # Use askpass for passphrase prompts (key-based auth)
                from .askpass_utils import (
                    get_ssh_env_with_askpass,
                    get_ssh_env_with_forced_askpass,
                    lookup_passphrase,
                )

                requires_force = bool(
                    getattr(self.connection, 'identity_agent_disabled', False)
                )
                askpass_env = (
                    get_ssh_env_with_forced_askpass()
                    if requires_force
                    else get_ssh_env_with_askpass()
                )
                if requires_force:
                    key_passphrase = (
                        getattr(self.connection, 'key_passphrase', '') or ''
                    )
                    passphrase_available = bool(key_passphrase)
                    key_path_for_lookup = (
                        getattr(self.connection, 'keyfile', '') or ''
                    )

                    if key_passphrase:
                        logger.debug(
                            "IdentityAgent disabled: in-memory passphrase available from connection settings"
                        )

                    identity_candidates: List[str] = []

                    def _append_candidate(candidate: str) -> None:
                        if not candidate:
                            return
                        expanded = os.path.expanduser(str(candidate))
                        if expanded not in identity_candidates:
                            identity_candidates.append(expanded)

                    if key_path_for_lookup:
                        _append_candidate(key_path_for_lookup)

                    try:
                        resolved_identities = getattr(
                            self.connection, 'resolved_identity_files', []
                        )
                    except Exception:
                        resolved_identities = []

                    if isinstance(resolved_identities, (list, tuple)):
                        for candidate in resolved_identities:
                            _append_candidate(candidate)

                    try:
                        native_candidates = self._resolve_native_identity_candidates()
                    except Exception:
                        native_candidates = []

                    for candidate in native_candidates:
                        _append_candidate(candidate)

                    if identity_candidates:
                        logger.debug(
                            "IdentityAgent disabled: evaluating passphrase candidates %s",
                            identity_candidates,
                        )
                    else:
                        logger.debug(
                            "IdentityAgent disabled: no key candidates available for passphrase retrieval"
                        )

                    if (
                        key_passphrase
                        and identity_candidates
                        and hasattr(self, 'connection_manager')
                        and self.connection_manager
                        and hasattr(self.connection_manager, 'store_key_passphrase')
                    ):
                        for candidate in identity_candidates:
                            try:
                                self.connection_manager.store_key_passphrase(
                                    candidate,
                                    key_passphrase,
                                )
                                logger.debug(
                                    "IdentityAgent disabled: refreshed stored passphrase for %s",
                                    candidate,
                                )
                            except Exception as exc:
                                logger.debug(
                                    "IdentityAgent disabled: failed to refresh stored passphrase for %s: %s",
                                    candidate,
                                    exc,
                                )

                    if not passphrase_available:
                        for candidate in identity_candidates:
                            try:
                                looked_up = lookup_passphrase(candidate)
                            except Exception as exc:
                                logger.debug(
                                    "Passphrase lookup via askpass_utils failed for %s: %s",
                                    candidate,
                                    exc,
                                )
                                looked_up = ''

                            if looked_up:
                                logger.debug(
                                    "IdentityAgent disabled: located stored passphrase for %s",
                                    candidate,
                                )
                                passphrase_available = True
                                break

                            if (
                                hasattr(self, 'connection_manager')
                                and self.connection_manager
                                and hasattr(
                                    self.connection_manager, 'get_key_passphrase'
                                )
                            ):
                                try:
                                    stored = self.connection_manager.get_key_passphrase(
                                        candidate
                                    )
                                except Exception as exc:
                                    logger.debug(
                                        "Connection manager passphrase lookup failed for %s: %s",
                                        candidate,
                                        exc,
                                    )
                                    stored = None
                                if stored:
                                    logger.debug(
                                        "IdentityAgent disabled: connection manager supplied passphrase for %s",
                                        candidate,
                                    )
                                    passphrase_available = True
                                    break

                    if not passphrase_available:
                        askpass_env.pop('SSH_ASKPASS_REQUIRE', None)
                        logger.info(
                            "SSH askpass helper could not supply a key passphrase; "
                            "allowing interactive prompt instead"
                        )
                    else:
                        logger.debug(
                            "IdentityAgent disabled: keeping forced askpass to deliver stored passphrase"
                        )
                env.update(askpass_env)
                if requires_force:
                    logger.debug(
                        "IdentityAgent disabled for this host; forcing SSH askpass usage"
                    )
                self._enable_askpass_log_forwarding(include_existing=True)
            # Set TERM to a proper value only if missing or set to "dumb"
            if 'TERM' not in env or env.get('TERM', '').lower() == 'dumb':
                env['TERM'] = 'xterm-256color'
            env['SHELL'] = env.get('SHELL', '/bin/bash')
            env['SSHPILOT_FLATPAK'] = '1'
            # Add /app/bin to PATH for Flatpak compatibility
            if os.path.exists('/app/bin'):
                current_path = env.get('PATH', '')
                if '/app/bin' not in current_path:
                    env['PATH'] = f"/app/bin:{current_path}"
            
            # Convert environment dict to list format expected by VTE
            env_list = []
            for key, value in env.items():
                env_list.append(f"{key}={value}")
            
            # Log the command being executed for debugging
            logger.debug(f"Spawning SSH command: {ssh_cmd}")
            logger.debug(f"Environment PATH: {env.get('PATH', 'NOT_SET')}")
            
            # Create a new PTY for the terminal (VTE-specific, but backend may handle this)
            # According to VTE docs, we should set PTY size before spawning to avoid SIGWINCH
            pty = None
            if hasattr(self.backend, 'get_pty') and callable(self.backend.get_pty):
                pty = self.backend.get_pty()
            if pty is None and self.vte is not None:
                try:
                    pty = Vte.Pty.new_sync(Vte.PtyFlags.DEFAULT)
                    # Set PTY size before spawning to avoid child process receiving SIGWINCH
                    # Get terminal size (rows, columns)
                    try:
                        rows = self.vte.get_row_count()
                        cols = self.vte.get_column_count()
                        # Only set size if we have valid dimensions (not default 80x24)
                        if rows > 0 and cols > 0 and (rows != 24 or cols != 80):
                            pty.set_size(rows, cols)
                            logger.debug(f"Set PTY size to {rows}x{cols} before spawn")
                    except Exception as e:
                        logger.debug(f"Could not set PTY size before spawn: {e}")
                    # Associate PTY with Terminal so spawn_async uses it
                    try:
                        self.vte.set_pty(pty)
                    except Exception as e:
                        logger.debug(f"Could not set PTY on terminal: {e}")
                except Exception:
                    pass
            
            # Convert env_list to dict for backend
            env_dict = {}
            if env_list:
                for env_item in env_list:
                    if '=' in env_item:
                        key, value = env_item.split('=', 1)
                        env_dict[key] = value
            
            try:
                self.backend.spawn_async(
                    argv=ssh_cmd,
                    env=env_dict if env_dict else None,
                    cwd=os.path.expanduser('~') or '/',
                    flags=0,
                    child_setup=None,
                    callback=self._on_spawn_complete,
                    user_data=()
                )
            except GLib.Error as e:
                logger.error(f"VTE spawn failed with GLib error: {e}")
                # Check if it's a "No such file or directory" error for sshpass
                if "sshpass" in str(e) and "No such file or directory" in str(e):
                    logger.error("sshpass binary not found, falling back to askpass")
                    # Fall back to askpass method
                    self._fallback_to_askpass(ssh_cmd, env_list)
                else:
                    self._on_connection_failed(str(e))
                return
            except Exception as e:
                logger.error(f"VTE spawn failed with exception: {e}")
                self._on_connection_failed(str(e))
                return
            
            # Store the PTY for later cleanup
            self.pty = pty
            try:
                import time
                self._spawn_start_time = time.time()
            except Exception:
                self._spawn_start_time = None
            
            # Defer marking as connected until spawn completes
            try:
                self.apply_theme()
            except Exception:
                pass
            
            # Apply theme after connection is established
            self.apply_theme()
            
            # Focus the terminal
            if self.backend:
                self.backend.grab_focus()

            # Add fallback timer to hide spinner if spawn completion doesn't fire
            self._fallback_timer_id = GLib.timeout_add_seconds(5, self._fallback_hide_spinner)

            logger.info(f"SSH terminal connected to {self.connection}")
            
        except Exception as e:
            logger.error(f"Failed to setup SSH terminal: {e}")
            self._on_connection_failed(str(e))
    
    def _fallback_to_askpass(self, ssh_cmd, env_list):
        """Fallback when sshpass fails - allow interactive prompting"""
        try:
            logger.info("Falling back to interactive password prompt")

            # Remove sshpass from the command
            if ssh_cmd and ssh_cmd[0] == 'sshpass':
                ssh_cmd = ssh_cmd[3:]  # Remove sshpass, -f, and fifo_path

            # Strip any askpass variables from the environment list
            env_list = [e for e in env_list if not e.startswith('SSH_ASKPASS')]
            env_list = [e for e in env_list if not e.startswith('SSH_ASKPASS_REQUIRE')]

            logger.debug(f"Fallback SSH command: {ssh_cmd}")

            # Convert env_list to dict for backend
            env_dict = {}
            if env_list:
                for env_item in env_list:
                    if '=' in env_item:
                        key, value = env_item.split('=', 1)
                        env_dict[key] = value
            
            # Try spawning again without askpass
            self.backend.spawn_async(
                argv=ssh_cmd,
                env=env_dict if env_dict else None,
                cwd=os.path.expanduser('~') or '/',
                flags=0,
                child_setup=None,
                callback=self._on_spawn_complete,
                user_data=()
            )
        except Exception as e:
            logger.error(f"Fallback to interactive prompt failed: {e}")
            self._on_connection_failed(str(e))

    def _enable_askpass_log_forwarding(self, include_existing: bool = False) -> None:
        """Start forwarding askpass log lines into the application logger."""

        try:
            from .askpass_utils import ensure_askpass_log_forwarder, forward_askpass_log_to_logger
        except Exception as exc:
            logger.debug(f"Unable to import askpass log forwarder: {exc}")
            return

        ensure_askpass_log_forwarder()
        forward_askpass_log_to_logger(logger, include_existing=include_existing)

    def _on_spawn_complete(self, terminal_or_widget, pid_or_error=None, error=None, user_data=None):
        """Called when terminal spawn is complete
        
        Handles both VTE callback signature (terminal, pid, error, user_data)
        and backend callback signature (widget, exception).
        """
        # Handle backend callback signature (widget, exception)
        if error is None and pid_or_error is not None and isinstance(pid_or_error, Exception):
            error = pid_or_error
            pid = None
        elif isinstance(pid_or_error, int):
            pid = pid_or_error
        else:
            pid = pid_or_error
        
        # For backend callbacks, we might not get a pid
        if pid is None and hasattr(self.backend, 'get_child_pid'):
            try:
                pid = self.backend.get_child_pid()
            except Exception:
                pass
        # Skip if terminal is quitting
        if getattr(self, '_is_quitting', False):
            logger.debug("Terminal is quitting, skipping spawn complete handler")
            return

        # Cancel fallback timer if it's still pending
        if getattr(self, '_fallback_timer_id', None):
            try:
                GLib.source_remove(self._fallback_timer_id)
            except Exception:
                pass
            self._fallback_timer_id = None

        logger.debug(f"Flatpak debug: _on_spawn_complete called with pid={pid}, error={error}, user_data={user_data}")
        
        if error:
            logger.error(f"Terminal spawn failed: {error}")
            # Ensure theme is applied before showing error so bg doesn't flash white
            try:
                self.apply_theme()
            except Exception:
                pass
            self._on_connection_failed(str(error))
            return

        logger.debug(f"Terminal spawned with PID: {pid}")
        self.process_pid = pid
        
        try:
            # Get and store process group ID
            self.process_pgid = os.getpgid(pid)
            logger.debug(f"Process group ID: {self.process_pgid}")
            
            # Store shell PGID for job detection (this is the shell's process group)
            self._shell_pgid = self.process_pgid
            logger.debug(f"Shell PGID stored for job detection: {self._shell_pgid}")
            
            # Store process info for cleanup
            with process_manager.lock:
                # Determine command type based on connection type
                command_type = (
                    'bash'
                    if hasattr(self.connection, 'hostname') and self.connection.hostname == 'localhost'
                    else 'ssh'
                )
                process_manager.processes[pid] = {
                    'terminal': weakref.ref(self),
                    'start_time': datetime.now(),
                    'command': command_type,
                    'pgid': self.process_pgid
                }
            
            # Grab focus and apply theme
            if self.backend:
                self.backend.grab_focus()
            self.apply_theme()

            # Spawn succeeded; mark as connected and hide overlay
            self.is_connected = True
            
            # Update connection status in the connection manager (only for SSH connections)
            if hasattr(self, 'connection') and self.connection and hasattr(self.connection, 'hostname') and self.connection.hostname != 'localhost':
                if hasattr(self, 'connection_manager') and self.connection_manager:
                    self.connection_manager.update_connection_status(self.connection, True)
                    logger.debug(f"Terminal {self.session_id} updated connection status to connected")
                else:
                    logger.warning(f"Terminal {self.session_id} has no connection manager")
            elif hasattr(self, 'connection') and self.connection and hasattr(self.connection, 'hostname') and self.connection.hostname == 'localhost':
                logger.debug(f"Local terminal {self.session_id} spawned successfully")
            else:
                logger.warning(f"Terminal {self.session_id} has no connection object to update")
            
            self.emit('connection-established')
            self._set_connecting_overlay_visible(False)
            # Ensure any reconnect/disconnected banner is hidden upon successful spawn
            try:
                self._set_disconnected_banner_visible(False)
            except Exception:
                pass
            
        except Exception as e:
            logger.error(f"Error in spawn complete: {e}")
            self._on_connection_failed(str(e))
    
    def _fallback_hide_spinner(self):
        """Fallback method to hide spinner if spawn completion doesn't fire"""
        # Clear stored timer ID
        self._fallback_timer_id = None

        # Skip if terminal is quitting
        if getattr(self, '_is_quitting', False):
            logger.debug("Terminal is quitting, skipping fallback hide spinner")
            return False

        logger.debug("Flatpak debug: Fallback hide spinner called")

        # If a connection error was recorded, skip forcing a connected state
        if self.last_error_message:
            logger.debug("Fallback timer triggered after connection failure; ignoring")
            return False

        if not self.is_connected:
            logger.warning("Flatpak: Spawn completion callback didn't fire, forcing connection state")
            self.is_connected = True
            
            # Update connection status in the connection manager (only for SSH connections)
            if hasattr(self, 'connection') and self.connection and hasattr(self.connection, 'hostname') and self.connection.hostname != 'localhost':
                if hasattr(self, 'connection_manager') and self.connection_manager:
                    self.connection_manager.update_connection_status(self.connection, True)
                    logger.debug(f"Terminal {self.session_id} updated connection status to connected (fallback)")
                else:
                    logger.warning(f"Terminal {self.session_id} has no connection manager (fallback)")
            elif hasattr(self, 'connection') and self.connection and hasattr(self.connection, 'hostname') and self.connection.hostname == 'localhost':
                logger.debug(f"Local terminal {self.session_id} spawned successfully (fallback)")
            else:
                logger.warning(f"Terminal {self.session_id} has no connection object to update (fallback)")
            
            self.emit('connection-established')
            self._set_connecting_overlay_visible(False)
            try:
                self._set_disconnected_banner_visible(False)
            except Exception:
                pass
        return False  # Don't repeat the timer
        

    def _show_forwarding_error_dialog(self, message):
        try:
            dialog = Adw.MessageDialog(
                transient_for=self.get_root() if hasattr(self, 'get_root') else None,
                modal=True,
                heading="Port Forwarding Error",
                body=str(message)
            )
            dialog.add_response('ok', 'OK')
            dialog.set_default_response('ok')
            dialog.present()
        except Exception as e:
            logger.debug(f"Failed to present forwarding error dialog: {e}")
        return False

    def set_group_color(self, color: Optional[str]):
        """Update the stored group color and refresh the theme if needed."""
        self.group_color = color if color else None
        try:
            self.apply_theme()
        except Exception:
            logger.debug("Failed to reapply theme after group color update", exc_info=True)

    @staticmethod
    def _mix_rgba(base: Gdk.RGBA, other: Gdk.RGBA, ratio: float) -> Gdk.RGBA:
        ratio = max(0.0, min(1.0, ratio))
        mixed = Gdk.RGBA()
        mixed.red = base.red * (1.0 - ratio) + other.red * ratio
        mixed.green = base.green * (1.0 - ratio) + other.green * ratio
        mixed.blue = base.blue * (1.0 - ratio) + other.blue * ratio
        mixed.alpha = base.alpha * (1.0 - ratio) + other.alpha * ratio
        return mixed

    @staticmethod
    def _calculate_luminance(rgba: Gdk.RGBA) -> float:
        return 0.2126 * rgba.red + 0.7152 * rgba.green + 0.0722 * rgba.blue

    @classmethod
    def _contrast_color(cls, rgba: Gdk.RGBA) -> Gdk.RGBA:
        contrast = Gdk.RGBA()
        if cls._calculate_luminance(rgba) < 0.5:
            contrast.parse('#FFFFFF')
        else:
            contrast.parse('#000000')
        contrast.alpha = 1.0
        return contrast

    @staticmethod
    def _ensure_opaque(rgba: Gdk.RGBA) -> Gdk.RGBA:
        opaque = Gdk.RGBA()
        opaque.red = rgba.red
        opaque.green = rgba.green
        opaque.blue = rgba.blue
        opaque.alpha = 1.0
        return opaque

    def _parse_group_color(self) -> Optional[Gdk.RGBA]:
        if not self.group_color:
            return None
        rgba = Gdk.RGBA()
        try:
            parsed = rgba.parse(str(self.group_color))
        except Exception:
            logger.debug("Failed to parse terminal group color '%s'", self.group_color, exc_info=True)
            return None
        if not parsed or rgba.alpha <= 0:
            return None
        return rgba

    def apply_theme(self, theme_name=None):
        """Apply terminal theme and font settings

        Args:
            theme_name (str, optional): Name of the theme to apply. If None, uses the saved theme.
        """
        try:
            if theme_name is None and self.config:
                # Get the saved theme from config
                theme_name = self.config.get_setting('terminal.theme', 'default')
                
            # Get the theme profile from config
            if self.config:
                profile = self.config.get_terminal_profile(theme_name)
            else:
                # Fallback default theme
                profile = {
                    'foreground': '#000000',  # Black text
                    'background': '#FFFFFF',  # White background
                    'font': 'Monospace 12',
                    'cursor_color': '#000000',
                    'highlight_background': '#4A90E2',
                    'highlight_foreground': '#FFFFFF',
                    'palette': [
                        '#000000', '#CC0000', '#4E9A06', '#C4A000',
                        '#3465A4', '#75507B', '#06989A', '#D3D7CF',
                        '#555753', '#EF2929', '#8AE234', '#FCE94F',
                        '#729FCF', '#AD7FA8', '#34E2E2', '#EEEEEC'
                    ]
                }
            
            # Set colors
            fg_color = Gdk.RGBA()
            fg_color.parse(profile['foreground'])

            bg_color = Gdk.RGBA()
            bg_color.parse(profile['background'])

            cursor_color_value = profile.get('cursor_color')
            cursor_color = Gdk.RGBA()
            if not (cursor_color_value and cursor_color.parse(cursor_color_value)):
                cursor_color = self._get_contrast_color(bg_color)

            highlight_bg_value = profile.get('highlight_background')
            highlight_fg_value = profile.get('highlight_foreground')
            highlight_bg = Gdk.RGBA()
            highlight_fg = Gdk.RGBA()

            if not (highlight_bg_value and highlight_bg.parse(highlight_bg_value)):
                highlight_bg.parse('#4A90E2')

            if not (highlight_fg_value and highlight_fg.parse(highlight_fg_value)):
                highlight_fg = self._get_contrast_color(highlight_bg)

            override_rgba = self._get_group_color_rgba()
            use_group_color = False

            try:
                use_group_color = bool(
                    self.config.get_setting('ui.use_group_color_in_terminal', False)
                )
            except Exception:
                use_group_color = False

            if use_group_color and override_rgba is not None:
                bg_color = self._clone_rgba(override_rgba)  # Use exact group color
                fg_color = self._get_contrast_color(bg_color)

                contrast_for_bg = self._get_contrast_color(bg_color)
                mix_ratio = 0.35 if self._relative_luminance(bg_color) < 0.5 else 0.25
                highlight_bg = self._mix_rgba(bg_color, contrast_for_bg, mix_ratio)
                highlight_bg.alpha = 1.0
                highlight_fg = self._get_contrast_color(highlight_bg)
                cursor_color = self._clone_rgba(fg_color)


            # Prepare palette colors (16 ANSI colors)
            palette_colors = None
            if 'palette' in profile and profile['palette']:
                palette_colors = []
                for color_hex in profile['palette']:
                    color = Gdk.RGBA()
                    if color.parse(color_hex):
                        palette_colors.append(color)
                    else:
                        logger.warning(f"Failed to parse palette color: {color_hex}")
                        # Use a fallback color
                        fallback = Gdk.RGBA()
                        fallback.parse('#000000')
                        palette_colors.append(fallback)
                
                # Ensure we have exactly 16 colors
                while len(palette_colors) < 16:
                    fallback = Gdk.RGBA()
                    fallback.parse('#000000')
                    palette_colors.append(fallback)
                palette_colors = palette_colors[:16]  # Limit to 16 colors
            
            # Apply colors to terminal (VTE-specific, but backend.apply_theme should handle this)
            # For VTE backend, apply directly; for other backends, use apply_theme
            if self.vte is not None:
                self.vte.set_colors(fg_color, bg_color, palette_colors)
                self.vte.set_color_cursor(cursor_color)
                self.vte.set_color_highlight(highlight_bg)
                self.vte.set_color_highlight_foreground(highlight_fg)
            elif self.backend:
                # For non-VTE backends, use apply_theme which should handle colors
                self.backend.apply_theme(theme_name)

            self._applied_foreground_color = self._clone_rgba(fg_color)
            self._applied_background_color = self._clone_rgba(bg_color)
            self._applied_cursor_color = self._clone_rgba(cursor_color)
            self._applied_highlight_bg = self._clone_rgba(highlight_bg)
            self._applied_highlight_fg = self._clone_rgba(highlight_fg)

            # Also color the container background to prevent white flash before VTE paints
            try:
                rgba = bg_color
                # For Gtk4, setting the widget style via CSS provider
                # Track provider on display to avoid accumulation and conflicts
                display = Gdk.Display.get_default()
                if display:
                    # Remove previous terminal background provider if it exists
                    if hasattr(display, '_terminal_bg_provider'):
                        try:
                            Gtk.StyleContext.remove_provider_for_display(
                                display, display._terminal_bg_provider
                            )
                        except Exception:
                            pass
                    
                    # Create new provider with very specific selector to avoid affecting other widgets
                    # Only target TerminalWidget instances with terminal-bg class
                    provider = Gtk.CssProvider()
                    css = f"terminalwidget.terminal-bg, terminalwidget.terminal-bg > scrolledwindow.terminal-bg, terminalwidget.terminal-bg > scrolledwindow.terminal-bg > vte-terminal.terminal-bg {{ background-color: rgba({int(rgba.red*255)}, {int(rgba.green*255)}, {int(rgba.blue*255)}, {rgba.alpha}); }}"
                    provider.load_from_data(css.encode('utf-8'))
                    Gtk.StyleContext.add_provider_for_display(
                        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                    )
                    # Store provider reference for cleanup
                    display._terminal_bg_provider = provider
                
                # Add CSS class to terminal widgets only
                if hasattr(self, 'add_css_class'):
                    self.add_css_class('terminal-bg')
                if hasattr(self.scrolled_window, 'add_css_class'):
                    self.scrolled_window.add_css_class('terminal-bg')
                if hasattr(self.vte, 'add_css_class'):
                    self.vte.add_css_class('terminal-bg')
            except Exception as e:
                logger.debug(f"Failed to set container background: {e}")
            
            # Set font
            font_desc = Pango.FontDescription.from_string(profile['font'])
            if self.backend:
                self.backend.set_font(font_desc)
            
            # Force a redraw
            if self.backend:
                self.backend.queue_draw()
            
            logger.debug(f"Applied terminal theme: {theme_name or 'default'}")
            
        except Exception as e:
            logger.error(f"Failed to apply terminal theme: {e}")

    def _clone_rgba(self, rgba: Gdk.RGBA) -> Gdk.RGBA:
        clone = Gdk.RGBA()
        clone.red = rgba.red
        clone.green = rgba.green
        clone.blue = rgba.blue
        clone.alpha = rgba.alpha
        return clone

    def _get_group_color_rgba(self) -> Optional[Gdk.RGBA]:
        color_value = getattr(self, 'group_color', None)
        if not color_value:
            return None

        rgba = Gdk.RGBA()
        try:
            if rgba.parse(str(color_value)):
                rgba.alpha = 1.0 if rgba.alpha == 0 else rgba.alpha
                return rgba
        except Exception:
            logger.debug("Failed to parse group color '%s'", color_value, exc_info=True)
        return None

    def _mix_with_white(self, rgba: Gdk.RGBA, ratio: float = 0.35) -> Gdk.RGBA:
        ratio = max(0.0, min(1.0, ratio))
        mixed = Gdk.RGBA()
        mixed.red = min(1.0, rgba.red * ratio + (1 - ratio))
        mixed.green = min(1.0, rgba.green * ratio + (1 - ratio))
        mixed.blue = min(1.0, rgba.blue * ratio + (1 - ratio))
        mixed.alpha = 1.0
        return mixed

    def _relative_luminance(self, rgba: Gdk.RGBA) -> float:
        def to_linear(channel: float) -> float:
            if channel <= 0.03928:
                return channel / 12.92
            return ((channel + 0.055) / 1.055) ** 2.4

        r_lin = to_linear(rgba.red)
        g_lin = to_linear(rgba.green)
        b_lin = to_linear(rgba.blue)
        return 0.2126 * r_lin + 0.7152 * g_lin + 0.0722 * b_lin

    def _get_contrast_color(self, background: Gdk.RGBA) -> Gdk.RGBA:
        luminance = self._relative_luminance(background)
        contrast = Gdk.RGBA()
        if luminance > 0.5:
            contrast.parse('#1B1B1D')
        else:
            contrast.parse('#FFFFFF')
        contrast.alpha = 1.0
        return contrast

    def _apply_cursor_and_selection_colors(self):
        try:
            cursor_color = getattr(self, '_applied_cursor_color', None)
            background_color = getattr(self, '_applied_background_color', None)

            if cursor_color is None and background_color is not None:
                cursor_color = self._get_contrast_color(background_color)
            elif cursor_color is None:
                cursor_color = Gdk.RGBA()
                cursor_color.parse('#000000')

            if hasattr(self.vte, 'set_color_cursor') and cursor_color is not None:
                self.vte.set_color_cursor(cursor_color)
                logger.debug("Applied cursor color")

            highlight_bg = getattr(self, '_applied_highlight_bg', None)
            highlight_fg = getattr(self, '_applied_highlight_fg', None)

            if highlight_bg is None:
                highlight_bg = Gdk.RGBA()
                highlight_bg.parse('#4A90E2')

            if highlight_fg is None:
                highlight_fg = self._get_contrast_color(highlight_bg)

            if hasattr(self.vte, 'set_color_highlight'):
                self.vte.set_color_highlight(highlight_bg)
                logger.debug("Applied selection highlight color")

            if hasattr(self.vte, 'set_color_highlight_foreground'):
                self.vte.set_color_highlight_foreground(highlight_fg)
                logger.debug("Applied selection highlight foreground color")

        except Exception as e:
            logger.warning(f"Could not apply terminal highlight or cursor colors: {e}")

    def set_group_color(self, color_value, force: bool = False):
        normalized = color_value or None
        if not force and normalized == getattr(self, 'group_color', None):
            return

        self.group_color = normalized
        try:
            self.apply_theme()
        except Exception:
            logger.debug("Failed to reapply theme after group color update", exc_info=True)
            
    def force_style_refresh(self):
        """Force a style refresh of the terminal widget."""
        self.apply_theme()
    
    def setup_terminal(self):
        """Initialize the VTE terminal with appropriate settings."""
        logger.info("Setting up terminal...")
        
        try:
            # Set terminal font
            font_desc = Pango.FontDescription()
            font_desc.set_family("Monospace")
            font_desc.set_size(12 * Pango.SCALE)  # Slightly larger default font
            if self.backend:
                self.backend.set_font(font_desc)
            
            # Do not force a light default; theme will define colors
            self.apply_theme()
            
            # Set VTE-specific properties (only if using VTE backend)
            if self.vte is not None:
                # Set cursor properties
                try:
                    self.vte.set_cursor_blink_mode(Vte.CursorBlinkMode.ON)
                    self.vte.set_cursor_shape(Vte.CursorShape.BLOCK)
                except Exception as e:
                    logger.warning(f"Could not set cursor properties: {e}")
                
                # Set scrollback lines
                try:
                    self.vte.set_scrollback_lines(10000)
                except Exception as e:
                    logger.warning(f"Could not set scrollback lines: {e}")
                
                # Set word char exceptions (for double-click selection)
                try:
                    # Try the newer API first (VTE 0.60+)
                    if hasattr(self.vte, 'set_word_char_exceptions'):
                        self.vte.set_word_char_exceptions("@-./_~")
                        logger.debug("Set word char exceptions using VTE 0.60+ API")
                    # Fall back to the older API if needed
                    elif hasattr(self.vte, 'set_word_char_options'):
                        self.vte.set_word_char_options("@-./_~")
                        logger.debug("Set word char exceptions using older VTE API")
                except Exception as e:
                    logger.warning(f"Could not set word char options: {e}")
                
                self._apply_cursor_and_selection_colors()
                
                # Enable mouse reporting if available
                try:
                    if hasattr(self.vte, 'set_mouse_autohide'):
                        self.vte.set_mouse_autohide(True)
                        logger.debug("Enabled mouse autohide")
                except Exception as e:
                    logger.warning(f"Could not set mouse autohide: {e}")
                    
                encoding_value = 'UTF-8'
                try:
                    encoding_value = self.config.get_setting('terminal.encoding', 'UTF-8')
                except Exception:
                    encoding_value = 'UTF-8'
                self._apply_terminal_encoding(encoding_value, update_config_on_fallback=True)
                    
                # Enable bold text
                try:
                    if hasattr(self.vte, 'set_allow_bold'):
                        self.vte.set_allow_bold(True)
                        logger.debug("Enabled bold text")
                except Exception as e:
                    logger.warning(f"Could not enable bold text: {e}")
                
                # Show the terminal
                try:
                    self.vte.show()
                except Exception as e:
                    logger.warning(f"Could not show terminal: {e}")
                
            logger.info("Terminal setup complete")
            
        except Exception as e:
            logger.error(f"Error in setup_terminal: {e}", exc_info=True)
            raise
        
        # Install terminal shortcuts and custom context menu
        self._apply_pass_through_mode(self._pass_through_mode)
        self._setup_context_menu()

    def _get_supported_encodings(self):
        if self._supported_encodings is not None:
            return self._supported_encodings

        encodings = []
        try:
            for item in self.vte.get_encodings() or []:
                code = None
                if isinstance(item, (list, tuple)):
                    if item:
                        code = item[0]
                elif isinstance(item, str):
                    code = item
                if code and code not in encodings:
                    encodings.append(code)
        except Exception as exc:  # pragma: no cover - depends on VTE runtime
            logger.debug("Unable to query VTE encodings: %s", exc)

        if 'UTF-8' in encodings:
            encodings.insert(0, encodings.pop(encodings.index('UTF-8')))
        else:
            encodings.insert(0, 'UTF-8')

        self._supported_encodings = encodings
        return self._supported_encodings

    def _apply_terminal_encoding_idle(self, encoding_value):
        self._apply_terminal_encoding(encoding_value, update_config_on_fallback=True)
        return False

    def _apply_terminal_encoding(self, encoding_value, update_config_on_fallback=True):
        # For PyXterm.js backend, encoding is handled at PTY bridge level (via luit)
        # No need to validate against VTE's supported encodings
        if self.backend and hasattr(self.backend, '__class__'):
            backend_name = self.backend.__class__.__name__
            if backend_name == 'PyXtermTerminalBackend':
                # PyXterm.js handles encoding via luit at PTY bridge level
                # Accept any encoding - it will be wrapped with luit if needed
                requested = encoding_value.strip() if isinstance(encoding_value, str) else ''
                if requested:
                    logger.debug("Encoding '%s' will be handled at PTY bridge level for PyXterm.js backend", requested)
                return False
        
        # For VTE backend, validate encoding against VTE's supported list
        supported = self._get_supported_encodings()
        fallback = supported[0] if supported else 'UTF-8'

        requested = encoding_value.strip() if isinstance(encoding_value, str) else ''
        canonical = None
        if requested:
            if requested in supported:
                canonical = requested
            else:
                lower_requested = requested.lower()
                for code in supported:
                    if code.lower() == lower_requested:
                        canonical = code
                        break

        if canonical:
            target = canonical
            fallback_triggered = False
        else:
            target = fallback
            fallback_triggered = bool(requested)

        update_needed = update_config_on_fallback and target != requested

        try:
            if self.vte is not None:
                self.vte.set_encoding(target)
                logger.debug("Set terminal encoding to %s", target)
            else:
                # Encoding setting is VTE-specific; other backends handle encoding differently
                logger.debug("Encoding setting skipped for non-VTE backend")
        except Exception as exc:
            logger.warning("Could not set terminal encoding to %s: %s", target, exc)
            return False

        if fallback_triggered:
            self._notify_invalid_encoding(requested, target)

        if update_needed and hasattr(self.config, 'set_setting') and not self._updating_encoding_config:
            self._updating_encoding_config = True
            try:
                self.config.set_setting('terminal.encoding', target)
            finally:
                self._updating_encoding_config = False

        return False

    def _notify_invalid_encoding(self, requested, fallback):
        message = _(f"Encoding '{requested}' is not supported. Using {fallback} instead.")
        logger.warning(message)
        root = self.get_root()
        try:
            toast = Adw.Toast.new(message)
        except Exception:
            toast = None

        if toast is None:
            return

        try:
            if root and hasattr(root, 'toast_overlay') and root.toast_overlay is not None:
                root.toast_overlay.add_toast(toast)
            elif root and hasattr(root, 'add_toast'):
                root.add_toast(toast)
        except Exception:
            pass

    def setup_local_shell(self):
        """Set up the terminal for local shell (not SSH)"""
        logger.info("Setting up local shell terminal")
        try:
            # Hide connecting overlay immediately for local shell
            self._set_connecting_overlay_visible(False)
            
            # Set up the terminal for local shell
            self.setup_terminal()
            
            # Set initial title for local terminal
            self.emit('title-changed', 'Local Terminal')
            
            # Try agent-based approach first (fixes job control in Flatpak)
            if is_flatpak() and self._try_agent_based_shell():
                logger.info("Using agent-based local shell (with job control fix)")
                return
            
            # Fall back to direct spawn (legacy approach)
            logger.info("Using direct spawn for local shell (fallback)")
            self._setup_local_shell_direct()
            
        except Exception as e:
            logger.error(f"Failed to setup local shell: {e}")
            self.emit('connection-failed', str(e))
    
    def _get_terminal_size(self) -> tuple[int, int]:
        """
        Get the terminal size in columns and rows.
        Tries to get the actual allocated size from the terminal widget.
        
        Returns:
            Tuple of (cols, rows)
        """
        cols = 80
        rows = 24
        
        try:
            if getattr(self, 'vte', None) is not None:
                # Try to get size from VTE
                vte_cols = self.vte.get_column_count()
                vte_rows = self.vte.get_row_count()
                
                # Use VTE's reported size if it's reasonable (not the default 80x24)
                # VTE will return the actual size once the terminal is allocated
                if vte_cols >= 80 and vte_rows >= 24:
                    cols = vte_cols
                    rows = vte_rows
                    logger.debug(f"Got terminal size from VTE: {cols}x{rows}")
            elif getattr(self, 'backend', None) is not None:
                # Some backends may expose a widget with geometry hints
                widget = getattr(self.backend, 'widget', None)
                if widget and hasattr(widget, 'get_width_chars') and hasattr(widget, 'get_height_rows'):
                    widget_cols = widget.get_width_chars()
                    widget_rows = widget.get_height_rows()
                    if widget_cols and widget_cols > 0:
                        cols = widget_cols
                    if widget_rows and widget_rows > 0:
                        rows = widget_rows
        except Exception as e:
            logger.debug(f"Failed to determine terminal size from backend: {e}")
        
        return (cols, rows)
    
    def _try_agent_based_shell(self) -> bool:
        """
        Try to set up local shell using the agent (Ptyxis-style).
        This fixes job control issues in Flatpak.
        
        Returns:
            True if successful, False otherwise
        """
        try:
            from .agent_client import AgentClient
            
            # Create agent client
            client = AgentClient()
            
            # Get terminal size - try to get actual allocated size
            cols, rows = self._get_terminal_size()
            
            # If we still have default size (80x24), defer spawn until terminal is allocated
            if cols == 80 and rows == 24:
                logger.debug("Terminal not allocated yet, deferring agent spawn until size is available")
                # Store client for later use
                self._pending_agent_client = client
                # Use GTK4-compatible notify signals for size allocation
                # In GTK4, size-allocate signal was removed, use notify::allocated-width/height instead
                widget_to_connect = None
                if getattr(self, 'terminal_widget', None) is not None:
                    widget_to_connect = self.terminal_widget
                elif getattr(self, 'scrolled_window', None) is not None:
                    widget_to_connect = self.scrolled_window
                elif getattr(self, 'vte', None) is not None:
                    # Fallback to VTE widget itself
                    widget_to_connect = self.vte
                
                if widget_to_connect is not None:
                    def on_size_changed(widget, param_spec):
                        # Only spawn once - check if pending client exists
                        if not hasattr(self, '_pending_agent_client'):
                            return
                        
                        # Check if widget has been allocated (has non-zero dimensions)
                        # This is more reliable than just checking VTE size
                        widget_allocated = False
                        try:
                            allocated_width = widget.get_allocated_width() if hasattr(widget, 'get_allocated_width') else widget.get_width()
                            allocated_height = widget.get_allocated_height() if hasattr(widget, 'get_allocated_height') else widget.get_height()
                            
                            # Widget must have been allocated (non-zero size)
                            widget_allocated = allocated_width > 0 and allocated_height > 0
                            if not widget_allocated:
                                logger.debug(f"Widget not allocated yet: {allocated_width}x{allocated_height}")
                                return
                        except Exception as e:
                            logger.debug(f"Could not check widget allocation: {e}")
                            # If we can't get allocated size, fall back to VTE size check
                            widget_allocated = True  # Assume allocated if we can't check
                        
                        # Check if we now have a reasonable size from VTE
                        # If widget is allocated, spawn even if size is still 80x24 (might be actual size)
                        cols, rows = self._get_terminal_size()
                        logger.debug(f"Size check: widget_allocated={widget_allocated}, cols={cols}, rows={rows}")
                        
                        # Spawn if widget is allocated (even if size is 80x24, it might be the actual size)
                        if widget_allocated and cols >= 80 and rows >= 24:
                            client = self._pending_agent_client
                            delattr(self, '_pending_agent_client')
                            
                            # Disconnect both handlers to prevent duplicate calls
                            if hasattr(self, '_pending_size_handlers'):
                                for handler_id in self._pending_size_handlers:
                                    try:
                                        widget.disconnect(handler_id)
                                    except Exception:
                                        pass
                                delattr(self, '_pending_size_handlers')
                            else:
                                # Fallback to disconnect_by_func if handlers not stored
                                widget.disconnect_by_func(on_size_changed)
                            
                            logger.debug(f"Terminal allocated, spawning agent with size {cols}x{rows}")
                            self._spawn_agent_shell(client, cols, rows)
                    
                    try:
                        # Use notify signals for GTK4 compatibility
                        # Connect to both width and height to catch allocation
                        handler1 = widget_to_connect.connect('notify::allocated-width', on_size_changed)
                        handler2 = widget_to_connect.connect('notify::allocated-height', on_size_changed)
                        # Store handlers for cleanup if needed
                        if not hasattr(self, '_pending_size_handlers'):
                            self._pending_size_handlers = []
                        self._pending_size_handlers = [handler1, handler2]
                        
                        # Add a fallback timeout in case signals don't fire
                        # This ensures we spawn even if allocation detection fails
                        def fallback_spawn():
                            if hasattr(self, '_pending_agent_client'):
                                logger.debug("Fallback: Checking terminal size after timeout")
                                
                                # Check if widget is allocated
                                widget_allocated = False
                                try:
                                    if widget_to_connect:
                                        allocated_width = widget_to_connect.get_allocated_width() if hasattr(widget_to_connect, 'get_allocated_width') else widget_to_connect.get_width()
                                        allocated_height = widget_to_connect.get_allocated_height() if hasattr(widget_to_connect, 'get_allocated_height') else widget_to_connect.get_height()
                                        widget_allocated = allocated_width > 0 and allocated_height > 0
                                        logger.debug(f"Fallback: Widget allocated={widget_allocated}, size={allocated_width}x{allocated_height}")
                                except Exception as e:
                                    logger.debug(f"Fallback: Could not check widget allocation: {e}")
                                    widget_allocated = True  # Assume allocated if we can't check
                                
                                cols, rows = self._get_terminal_size()
                                logger.debug(f"Fallback: VTE size={cols}x{rows}")
                                
                                # Spawn with current size (even if still 80x24 or widget not fully allocated)
                                # It's better to have a terminal than none at all
                                client = self._pending_agent_client
                                delattr(self, '_pending_agent_client')
                                
                                # Disconnect handlers if they're still connected
                                if hasattr(self, '_pending_size_handlers') and widget_to_connect:
                                    for handler_id in self._pending_size_handlers:
                                        try:
                                            widget_to_connect.disconnect(handler_id)
                                        except Exception:
                                            pass
                                    delattr(self, '_pending_size_handlers')
                                
                                logger.info(f"Fallback: Spawning agent with size {cols}x{rows} (widget_allocated={widget_allocated})")
                                self._spawn_agent_shell(client, cols, rows)
                            return False  # Don't repeat
                        
                        # Set timeout to check after 500ms
                        GLib.timeout_add(500, fallback_spawn)
                        logger.debug("Connected to notify signals and set fallback timeout")
                        return True
                    except Exception as e:
                        logger.warning(f"Failed to connect notify signals, spawning immediately: {e}")
                        # Clean up pending client and fall through to spawn with current size
                        if hasattr(self, '_pending_agent_client'):
                            delattr(self, '_pending_agent_client')
                else:
                    # For non-VTE backends or if we can't find a widget to connect,
                    # spawn immediately with current size
                    logger.debug("No widget available for size notification, spawning immediately")
            
            # Spawn immediately if we have a reasonable size
            return self._spawn_agent_shell(client, cols, rows)
            
        except ImportError as e:
            logger.warning(f"Agent client not available: {e}")
            return False
        except Exception as e:
            logger.warning(f"Failed to setup agent-based shell: {e}")
            return False
    
    def _spawn_agent_shell(self, client, cols: int, rows: int) -> bool:
        """
        Actually spawn the agent shell with the given size.
        
        Args:
            client: AgentClient instance
            cols: Terminal columns
            rows: Terminal rows
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Working directory
            cwd = os.path.expanduser('~')
            
            # Check if verbose mode is enabled
            verbose = logger.getEffectiveLevel() <= logging.DEBUG
            
            # Build agent command
            command = client.build_agent_command(
                rows=rows,
                cols=cols,
                cwd=cwd,
                verbose=verbose
            )
            
            if not command:
                logger.warning("Could not build agent command, falling back to direct spawn")
                return False
            
            logger.info(f"Launching agent-based shell via flatpak-spawn with size {cols}x{rows}...")
            
            # Environment for agent
            env = os.environ.copy()
            # Set TERM to a proper value only if missing or set to "dumb"
            if 'TERM' not in env or env.get('TERM', '').lower() == 'dumb':
                env['TERM'] = 'xterm-256color'
            
            # Convert to list for VTE
            env_list = [f"{k}={v}" for k, v in env.items()]
            
            # Convert env_list to dict for backend
            env_dict = {}
            if env_list:
                for env_item in env_list:
                    if '=' in env_item:
                        key, value = env_item.split('=', 1)
                        env_dict[key] = value
            
            # Spawn the agent via backend
            # Agent code is embedded in the command via base64 encoding
            self.backend.spawn_async(
                argv=command,
                env=env_dict if env_dict else None,
                cwd=cwd,
                flags=0,
                child_setup=None,
                callback=self._on_agent_spawn_complete,
                user_data=None
            )
            
            # Add fallback timer
            self._fallback_timer_id = GLib.timeout_add_seconds(5, self._fallback_hide_spinner)
            
            return True
        except Exception as e:
            logger.error(f"Failed to spawn agent shell: {e}")
            return False
    
    def _setup_local_shell_direct(self):
        """
        Set up local shell using direct spawn (legacy approach).
        This is the fallback when agent is not available.
        """
        env = os.environ.copy()

        # Determine the user's preferred shell
        shell = None
        flatpak_spawn = None

        if is_flatpak():
            flatpak_spawn = shutil.which('flatpak-spawn')
            if flatpak_spawn:
                username = env.get('USER')
                if not username:
                    try:
                        username = pwd.getpwuid(os.getuid()).pw_name
                    except KeyError:
                        username = None

                if username:
                    try:
                        result = subprocess.run(
                            [flatpak_spawn, '--host', 'getent', 'passwd', username],
                            capture_output=True,
                            text=True,
                            check=True,
                        )
                        output = result.stdout.strip().splitlines()
                        if output:
                            host_entry = output[-1]
                            host_shell = host_entry.split(':')[-1].strip()
                            if host_shell:
                                shell = host_shell
                    except subprocess.CalledProcessError as e:
                        logger.debug(f"Failed to get host shell via flatpak-spawn: {e}")
                    except Exception as e:  # noqa: BLE001 - broad to ensure local shell fallback
                        logger.debug(f"Unexpected error determining host shell: {e}")

        if not shell:
            # Prioritize system passwd database over environment variable
            # The environment variable might not reflect the user's actual default shell
            try:
                shell = pwd.getpwuid(os.getuid()).pw_shell
            except (KeyError, AttributeError):
                shell = None
            
            # Fall back to environment variable if passwd lookup failed
            if not shell:
                shell = env.get('SHELL')
            
            # Final fallback
            if not shell:
                shell = '/bin/bash'

        # Ensure we have a proper environment
        env['SHELL'] = shell
        # Set TERM to a proper value only if missing or set to "dumb"
        if 'TERM' not in env or env.get('TERM', '').lower() == 'dumb':
            env['TERM'] = 'xterm-256color'
        
        # Ensure essential environment variables are set from passwd database
        # This ensures shells like zsh can properly load user configuration
        try:
            pw_entry = pwd.getpwuid(os.getuid())
            if 'USER' not in env or not env.get('USER'):
                env['USER'] = pw_entry.pw_name
            if 'LOGNAME' not in env or not env.get('LOGNAME'):
                env['LOGNAME'] = pw_entry.pw_name
            if 'HOME' not in env or not env.get('HOME'):
                env['HOME'] = pw_entry.pw_dir
        except (KeyError, AttributeError):
            # If passwd lookup fails, ensure at least USER is set
            if 'USER' not in env or not env.get('USER'):
                env['USER'] = os.getenv('USER', 'user')
            if 'LOGNAME' not in env or not env.get('LOGNAME'):
                env['LOGNAME'] = env.get('USER', 'user')
            if 'HOME' not in env or not env.get('HOME'):
                env['HOME'] = os.path.expanduser('~')

        # Convert environment dict to list for VTE compatibility
        env_list = []
        for key, value in env.items():
            env_list.append(f"{key}={value}")

        # Use interactive shell for all shells to match gnome-terminal and konsole behavior
        # Interactive shells load user's interactive config directly (.bashrc, .zshrc, etc.)
        # This is faster and matches what users expect from terminal emulators
        shell_flags = ['-i']  # Interactive shell (loads interactive config files)

        # Start the user's shell
        if flatpak_spawn:
            command = [flatpak_spawn, '--host', 'env'] + env_list + [shell] + shell_flags
        else:
            command = [shell] + shell_flags

        # Convert env_list to dict for backend
        env_dict = {}
        if env_list:
            for env_item in env_list:
                if '=' in env_item:
                    key, value = env_item.split('=', 1)
                    env_dict[key] = value
        
        # Create and configure PTY before spawning (for local terminals)
        # According to VTE docs, we should set PTY size before spawning to avoid SIGWINCH
        if self.vte is not None:
            try:
                # Check if PTY is already set
                existing_pty = None
                try:
                    existing_pty = self.vte.get_pty()
                except Exception:
                    pass
                
                # Create new PTY if not already set
                if existing_pty is None:
                    pty = Vte.Pty.new_sync(Vte.PtyFlags.DEFAULT)
                    # Set PTY size before spawning to avoid child process receiving SIGWINCH
                    try:
                        rows = self.vte.get_row_count()
                        cols = self.vte.get_column_count()
                        # Only set size if we have valid dimensions (not default 80x24)
                        if rows > 0 and cols > 0 and (rows != 24 or cols != 80):
                            pty.set_size(rows, cols)
                            logger.debug(f"Set PTY size to {rows}x{cols} before local terminal spawn")
                    except Exception as e:
                        logger.debug(f"Could not set PTY size before spawn: {e}")
                    # Associate PTY with Terminal so spawn_async uses it
                    try:
                        self.vte.set_pty(pty)
                    except Exception as e:
                        logger.debug(f"Could not set PTY on terminal: {e}")
            except Exception as e:
                logger.debug(f"Could not create/set PTY for local terminal: {e}")
        
        self.backend.spawn_async(
            argv=command,
            env=env_dict if env_dict else None,
            cwd=os.path.expanduser('~') or '/',
            flags=0,
            child_setup=None,
            callback=self._on_spawn_complete,
            user_data=()
        )

        # Add fallback timer to hide spinner if spawn completion doesn't fire
        self._fallback_timer_id = GLib.timeout_add_seconds(5, self._fallback_hide_spinner)

        logger.info("Local shell terminal setup initiated (direct spawn)")
    
    def _on_agent_spawn_complete(self, terminal, pid, error, user_data):
        """Callback when agent spawn completes"""
        if error:
            logger.error(f"Agent spawn failed: {error}")
            self.emit('connection-failed', str(error))
            return
        
        logger.info(f"Agent spawned successfully (PID: {pid})")
        
        # Hide the connecting overlay
        if self._fallback_timer_id:
            GLib.source_remove(self._fallback_timer_id)
            self._fallback_timer_id = None
        
        self._set_connecting_overlay_visible(False)
        
        # Store PID for cleanup
        self.process_pid = pid

    def _setup_context_menu(self):
        """Set up a robust per-terminal context menu and actions."""
        try:
            logger.debug("Setting up terminal context menu...")
            # Per-widget action group
            self._menu_actions = Gio.SimpleActionGroup()
            act_copy = Gio.SimpleAction.new("copy", None)
            act_copy.connect("activate", lambda a, p: self.copy_text())
            self._menu_actions.add_action(act_copy)
            act_paste = Gio.SimpleAction.new("paste", None)
            act_paste.connect("activate", lambda a, p: self.paste_text())
            self._menu_actions.add_action(act_paste)
            act_selall = Gio.SimpleAction.new("select_all", None)
            act_selall.connect("activate", lambda a, p: self.select_all())
            self._menu_actions.add_action(act_selall)
            
            # Add zoom actions
            act_zoom_in = Gio.SimpleAction.new("zoom_in", None)
            act_zoom_in.connect("activate", lambda a, p: self.zoom_in())
            self._menu_actions.add_action(act_zoom_in)
            
            act_zoom_out = Gio.SimpleAction.new("zoom_out", None)
            act_zoom_out.connect("activate", lambda a, p: self.zoom_out())
            self._menu_actions.add_action(act_zoom_out)
            
            act_reset_zoom = Gio.SimpleAction.new("reset_zoom", None)
            act_reset_zoom.connect("activate", lambda a, p: self.reset_zoom())
            self._menu_actions.add_action(act_reset_zoom)
            
            self.insert_action_group('term', self._menu_actions)

            # Menu model with keyboard shortcuts
            self._menu_model = Gio.Menu()

            if is_macos():
                self._menu_model.append(_("Copy\t⌘C"), "term.copy")
                self._menu_model.append(_("Paste\t⌘V"), "term.paste")
                self._menu_model.append(_("Select All\t⌘A"), "term.select_all")
                # Create a separator section for zoom options
                zoom_section = Gio.Menu()
                zoom_section.append(_("Zoom In\t⌘="), "term.zoom_in")
                zoom_section.append(_("Zoom Out\t⌘-"), "term.zoom_out")
                zoom_section.append(_("Reset Zoom\t⌘0"), "term.reset_zoom")
                self._menu_model.append_section(None, zoom_section)
            else:
                self._menu_model.append(_("Copy\tCtrl+Shift+C"), "term.copy")
                self._menu_model.append(_("Paste\tCtrl+Shift+V"), "term.paste")
                self._menu_model.append(_("Select All\tCtrl+Shift+A"), "term.select_all")
                # Create a separator section for zoom options
                zoom_section = Gio.Menu()
                zoom_section.append(_("Zoom In\tCtrl++"), "term.zoom_in")
                zoom_section.append(_("Zoom Out\tCtrl+-"), "term.zoom_out")
                zoom_section.append(_("Reset Zoom\tCtrl+0"), "term.reset_zoom")
                self._menu_model.append_section(None, zoom_section)

            # Popover - set parent to the terminal widget
            self._menu_popover = Gtk.PopoverMenu.new_from_model(self._menu_model)
            self._menu_popover.set_has_arrow(True)
            # Set parent to the terminal widget (use backend widget if vte is None)
            parent_widget = self.vte if self.vte is not None else (self.backend.widget if self.backend else self.terminal_widget)
            if parent_widget:
                self._menu_popover.set_parent(parent_widget)

            # Right-click gesture to open popover
            gesture = Gtk.GestureClick()
            gesture.set_button(0)
            def _on_pressed(gest, n_press, x, y):
                try:
                    btn = 0
                    try:
                        btn = gest.get_current_button()
                    except Exception:
                        pass
                    logger.debug(f"Context menu gesture: button={btn}, x={x}, y={y}")
                    if btn not in (Gdk.BUTTON_SECONDARY, 3):
                        logger.debug(f"Not a right-click button: {btn}")
                        return
                    # Stop event propagation to prevent other context menus
                    gest.set_state(Gtk.EventSequenceState.CLAIMED)
                    # Focus terminal first for reliable copy/paste
                    try:
                        if self.backend:
                            self.backend.grab_focus()
                    except Exception:
                        pass
                    # Position popover near click
                    try:
                        rect = Gdk.Rectangle()
                        rect.x = int(x)
                        rect.y = int(y)
                        rect.width = 1
                        rect.height = 1
                        self._menu_popover.set_pointing_to(rect)
                        logger.debug("Context menu positioned, showing popup")
                    except Exception as e:
                        logger.error(f"Failed to position context menu: {e}")
                    self._menu_popover.popup()
                except Exception as e:
                    logger.error(f"Context menu popup failed: {e}")
            gesture.connect('pressed', _on_pressed)
            # Store gesture reference for cleanup
            self._menu_gesture = gesture
            # Add gesture to the backend widget (VTE or WebView)
            if self.backend and self.backend.widget:
                self.backend.widget.add_controller(gesture)
                logger.debug(f"Added context menu gesture to backend widget: {type(self.backend).__name__}")
            elif self.vte is not None:
                self.vte.add_controller(gesture)
                logger.debug("Added context menu gesture to VTE widget")
            elif self.terminal_widget is not None:
                self.terminal_widget.add_controller(gesture)
                logger.debug("Added context menu gesture to terminal widget")
            logger.debug("Terminal context menu setup completed successfully")
        except Exception as e:
            logger.error(f"Context menu setup failed: {e}")

    def _install_shortcuts(self):
        """Install custom keyboard shortcuts for terminal operations."""
        if getattr(self, '_pass_through_mode', False):
            logger.debug("Pass-through mode active; skipping custom terminal shortcuts")
            return

        try:
            controller = getattr(self, '_shortcut_controller', None)
            if controller is None:
                controller = Gtk.ShortcutController()
                controller.set_scope(Gtk.ShortcutScope.LOCAL)
                controller.set_propagation_phase(Gtk.PropagationPhase.BUBBLE)

                def _schedule_vte_action(action, *action_args):
                    def _runner():
                        try:
                            action(*action_args)
                        except Exception as exc:
                            logger.debug("VTE shortcut action failed: %s", exc)
                        return False

                    GLib.idle_add(_runner)
                    return True

                def _cb_copy(widget, *args):
                    if self.backend:
                        return _schedule_vte_action(self.backend.copy_clipboard)
                    elif self.vte is not None:
                        if not self.vte.get_has_selection():
                            return False
                        return _schedule_vte_action(self.vte.copy_clipboard_format, Vte.Format.TEXT)
                    return False

                def _cb_paste(widget, *args):
                    if self.backend:
                        return _schedule_vte_action(self.backend.paste_clipboard)
                    elif self.vte is not None:
                        return _schedule_vte_action(self.vte.paste_clipboard)
                    return False

                def _cb_select_all(widget, *args):
                    if self.backend:
                        return _schedule_vte_action(self.backend.select_all)
                    elif self.vte is not None:
                        return _schedule_vte_action(self.vte.select_all)
                    return False

                if is_macos():
                    # macOS: Use standard Cmd+C/V for copy/paste, Cmd+Shift+C/V for terminal-specific operations
                    copy_trigger = "<Meta>c"
                    paste_trigger = "<Meta>v"
                    select_trigger = "<Meta>a"
                else:
                    # Linux/Windows: Use Ctrl+Shift+C/V for terminal copy/paste (standard for terminals)
                    copy_trigger = "<Primary><Shift>c"
                    paste_trigger = "<Primary><Shift>v"
                    select_trigger = "<Primary><Shift>a"

                controller.add_shortcut(Gtk.Shortcut.new(
                    Gtk.ShortcutTrigger.parse_string(copy_trigger),
                    Gtk.CallbackAction.new(_cb_copy)
                ))
                controller.add_shortcut(Gtk.Shortcut.new(
                    Gtk.ShortcutTrigger.parse_string(paste_trigger),
                    Gtk.CallbackAction.new(_cb_paste)
                ))
                controller.add_shortcut(Gtk.Shortcut.new(
                    Gtk.ShortcutTrigger.parse_string(select_trigger),
                    Gtk.CallbackAction.new(_cb_select_all)
                ))

                # Add zoom shortcuts
                if is_macos():
                    # macOS: Use Cmd+= (equals key), Cmd+-, and Cmd+0 for zoom
                    # Note: On macOS, Cmd+Shift+= is the same as Cmd+=
                    zoom_in_triggers = ["<Meta>equal"]
                    zoom_out_triggers = ["<Meta>minus"]
                    zoom_reset_trigger = "<Meta>0"
                else:
                    # Linux/Windows: Use Ctrl++, Ctrl+-, and Ctrl+0 for zoom
                    # Support both regular keys and numeric keypad variants
                    zoom_in_triggers = ["<Primary>equal", "<Primary>KP_Add"]
                    zoom_out_triggers = ["<Primary>minus", "<Primary>KP_Subtract"]
                    zoom_reset_trigger = "<Primary>0"

                logger.debug(f"Setting up terminal zoom shortcuts: in={zoom_in_triggers}, out={zoom_out_triggers}, reset={zoom_reset_trigger}")

                def _cb_zoom_in(widget, *args):
                    try:
                        self.zoom_in()
                    except Exception as exc:
                        logger.debug("Zoom in shortcut failed: %s", exc)
                    return True

                def _cb_zoom_out(widget, *args):
                    try:
                        self.zoom_out()
                    except Exception as exc:
                        logger.debug("Zoom out shortcut failed: %s", exc)
                    return True

                def _cb_reset_zoom(widget, *args):
                    try:
                        self.reset_zoom()
                    except Exception as exc:
                        logger.debug("Zoom reset shortcut failed: %s", exc)
                    return True

                # Add zoom in shortcuts (support both regular and keypad plus)
                for trig in zoom_in_triggers:
                    controller.add_shortcut(Gtk.Shortcut.new(
                        Gtk.ShortcutTrigger.parse_string(trig),
                        Gtk.CallbackAction.new(_cb_zoom_in)
                    ))

                # Add zoom out shortcuts (support both regular and keypad minus)
                for trig in zoom_out_triggers:
                    controller.add_shortcut(Gtk.Shortcut.new(
                        Gtk.ShortcutTrigger.parse_string(trig),
                        Gtk.CallbackAction.new(_cb_zoom_out)
                    ))

                controller.add_shortcut(Gtk.Shortcut.new(
                    Gtk.ShortcutTrigger.parse_string(zoom_reset_trigger),
                    Gtk.CallbackAction.new(_cb_reset_zoom)
                ))

                if self.vte is not None:
                    self.vte.add_controller(controller)
                elif self.terminal_widget is not None:
                    self.terminal_widget.add_controller(controller)
                self._shortcut_controller = controller

            if getattr(self, '_shortcut_controller', None) is not None:
                self._setup_mouse_wheel_zoom()

        except Exception as e:
            logger.debug(f"Failed to install shortcuts: {e}")

        try:
            self._ensure_search_key_controller()
        except Exception:
            pass
    
    def _setup_mouse_wheel_zoom(self):
        """Set up mouse wheel zoom functionality with Cmd+MouseWheel."""
        if getattr(self, '_scroll_controller', None) is not None:
            return

        try:
            mac = is_macos()

            scroll_controller = Gtk.EventControllerScroll()
            scroll_controller.set_flags(Gtk.EventControllerScrollFlags.VERTICAL)

            def _on_scroll(controller, dx, dy):
                try:
                    # Check if Command key (macOS) or Ctrl key (Linux/Windows) is pressed
                    modifiers = controller.get_current_event_state()
                    if mac:
                        # Check for Command key (Meta modifier)
                        if modifiers & Gdk.ModifierType.META_MASK:
                            if dy > 0:
                                self.zoom_out()
                            elif dy < 0:
                                self.zoom_in()
                            return True  # Consume the event
                    else:
                        # Check for Ctrl key
                        if modifiers & Gdk.ModifierType.CONTROL_MASK:
                            if dy > 0:
                                self.zoom_out()
                            elif dy < 0:
                                self.zoom_in()
                            return True  # Consume the event
                except Exception as e:
                    logger.debug(f"Error in mouse wheel zoom: {e}")
                return False  # Don't consume the event if modifier not pressed
            
            scroll_controller.connect('scroll', _on_scroll)
            if self.vte is not None:
                self.vte.add_controller(scroll_controller)
            elif self.terminal_widget is not None:
                self.terminal_widget.add_controller(scroll_controller)
            self._scroll_controller = scroll_controller
            logger.debug("Mouse wheel zoom functionality installed")

        except Exception as e:
            logger.debug(f"Failed to setup mouse wheel zoom: {e}")

    def _ensure_search_key_controller(self):
        """Attach the search shortcut controller to the terminal if needed."""
        if getattr(self, '_search_key_controller', None) is not None:
            return

        try:
            controller = Gtk.EventControllerKey()
            controller.connect('key-pressed', self._on_vte_search_key_pressed)
            if self.vte is not None:
                self.vte.add_controller(controller)
            elif self.terminal_widget is not None:
                self.terminal_widget.add_controller(controller)
            self._search_key_controller = controller
            logger.debug("Search key controller installed")
        except Exception as exc:
            logger.debug("Failed to install search key controller: %s", exc)

    def _on_vte_search_key_pressed(self, controller, keyval, keycode, state):
        """Handle global terminal search shortcuts on the VTE widget."""
        try:
            shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
            primary = bool(state & Gdk.ModifierType.CONTROL_MASK)
            meta = bool(state & Gdk.ModifierType.META_MASK)

            if keyval in (Gdk.KEY_f, Gdk.KEY_F) and (primary or meta):
                if hasattr(self, 'search_revealer') and self.search_revealer.get_reveal_child():
                    self._hide_search_overlay()
                else:
                    self._show_search_overlay(select_all=True)
                return True

            if keyval in (Gdk.KEY_g, Gdk.KEY_G) and (primary or meta):
                if shift:
                    self._on_search_previous()
                else:
                    self._on_search_next()
                return True

            if keyval == Gdk.KEY_Escape and hasattr(self, 'search_revealer') and self.search_revealer.get_reveal_child():
                self._hide_search_overlay()
                return True
        except Exception as exc:
            logger.debug("Terminal search key handling failed: %s", exc)
        return False

    def _remove_custom_shortcut_controllers(self):
        """Detach any custom shortcut or scroll controllers from the VTE widget."""
        ctrl = getattr(self, '_shortcut_controller', None)
        if ctrl is not None:
            try:
                if hasattr(self.vte, 'remove_controller'):
                    self.vte.remove_controller(ctrl)
            except Exception as exc:
                logger.debug("Failed to remove shortcut controller: %s", exc)
            finally:
                self._shortcut_controller = None

        scroll = getattr(self, '_scroll_controller', None)
        if scroll is not None:
            try:
                if hasattr(self.vte, 'remove_controller'):
                    self.vte.remove_controller(scroll)
            except Exception as exc:
                logger.debug("Failed to remove scroll controller: %s", exc)
            finally:
                self._scroll_controller = None

        search_ctrl = getattr(self, '_search_key_controller', None)
        if search_ctrl is not None:
            try:
                if hasattr(self.vte, 'remove_controller'):
                    self.vte.remove_controller(search_ctrl)
            except Exception as exc:
                logger.debug("Failed to remove search key controller: %s", exc)
            finally:
                self._search_key_controller = None

    def _apply_pass_through_mode(self, enabled: bool):
        """Enable or disable custom shortcut handling based on configuration."""
        enabled = bool(enabled)
        current = getattr(self, '_pass_through_mode', False)
        if enabled == current:
            if enabled:
                self._remove_custom_shortcut_controllers()
            else:
                if self._shortcut_controller is None:
                    self._install_shortcuts()
            return False

        self._pass_through_mode = enabled
        if enabled:
            self._remove_custom_shortcut_controllers()
        else:
            self._install_shortcuts()
        return False

    def _on_config_setting_changed(self, _config, key, value):
        if key == 'terminal.pass_through_mode':
            GLib.idle_add(self._apply_pass_through_mode, bool(value))
        elif key == 'terminal.encoding':
            if self._updating_encoding_config:
                return
            GLib.idle_add(self._apply_terminal_encoding_idle, value or '')

    # PTY forwarding is now handled automatically by VTE
    # No need for manual PTY management in this implementation
    
    def reconnect(self):
        """Reconnect the terminal with updated connection settings"""
        logger.info("Reconnecting terminal with updated settings...")
        was_connected = self.is_connected
        
        # Disconnect if currently connected
        if was_connected:
            self.disconnect()
        
        # Reconnect after a short delay to allow disconnection to complete
        def _reconnect():
            if self._connect_ssh():
                logger.info("Terminal reconnected with updated settings")
                # Ensure theme is applied after reconnection
                self.apply_theme()
                return True
            else:
                logger.error("Failed to reconnect terminal with updated settings")
                return False
        
        GLib.timeout_add(500, _reconnect)  # 500ms delay before reconnecting
    
    def _on_connection_updated_signal(self, sender, connection):
        """Signal handler for connection-updated signal"""
        self._on_connection_updated(connection)
        
    def _on_connection_updated(self, connection):
        """Called when connection settings are updated
        
        Note: We don't automatically reconnect here to prevent infinite loops.
        The main window will handle the reconnection flow after user confirmation.
        """
        if connection == self.connection:
            logger.info("Connection settings updated, waiting for user confirmation to reconnect...")
            # Just update our connection reference, don't reconnect automatically
            self.connection = connection
    
    def _on_connection_established(self):
        """Handle successful SSH connection"""
        logger.info(f"SSH connection to {self.connection.hostname} established")
        self.is_connected = True
        
        # Update connection status in the connection manager
        self.connection.is_connected = True
        self.connection_manager.emit('connection-status-changed', self.connection, True)
        
        self.emit('connection-established')

        # Apply theme after connection is established
        self.apply_theme()
        # Hide any reconnect banner on success
        self._set_disconnected_banner_visible(False)
        self.last_error_message = None
        
    def _on_connection_lost(self, message: str = None):
        """Handle SSH connection loss"""
        if self.is_connected:
            logger.info(f"SSH connection to {self.connection.hostname} lost")
            self.is_connected = False

            # Update connection status in the connection manager
            if hasattr(self, 'connection') and self.connection:
                self.connection.is_connected = False
                self.connection_manager.emit('connection-status-changed', self.connection, False)

            self.emit('connection-lost')
            # Show reconnect UI
            self._set_connecting_overlay_visible(False)
            banner_text = message or self.last_error_message or _('Connection lost.')
            self._set_disconnected_banner_visible(True, banner_text)
    
    def _on_terminal_input(self, widget, text, size):
        """Handle input from the terminal (handled automatically by VTE)"""
        pass
            
    def _on_terminal_resize(self, widget, width, height):
        """Handle terminal resize events"""
        # Update the SSH session if it exists
        if self.ssh_session and hasattr(self.ssh_session, 'change_terminal_size'):
            asyncio.create_task(
                self.ssh_session.change_terminal_size(
                    height, width, 0, 0
                )
            )
        
        # For local terminals with direct spawn, VTE automatically sends SIGWINCH
        # to the child process, so no additional action needed.
        # For agent-based shells, the agent runs in a separate process and
        # would need a mechanism to receive resize signals, which is not
        # currently implemented. The initial size should be correct now though.
    
    def _on_ssh_disconnected(self, exc):
        """Called when SSH connection is lost"""
        if self.is_connected:
            self.is_connected = False
            if exc:
                logger.error(f"SSH connection lost: {exc}")
            GLib.idle_add(lambda: self.emit('connection-lost'))
    
    def _setup_process_group(self, spawn_data):
        """Setup function called after fork but before exec"""
        # Create new process group for the child process
        os.setpgrp()
        
    def _get_terminal_pid(self):
        """Get the PID of the terminal's child process"""
        # First try the stored PID
        if self.process_pid:
            try:
                # Verify the process still exists
                os.kill(self.process_pid, 0)
                return self.process_pid
            except (ProcessLookupError, OSError):
                pass
        
        # Fall back to getting from PTY or VTE helpers
        try:
            # Prefer PID recorded at spawn complete
            if getattr(self, 'process_pid', None):
                return self.process_pid
            pty = None
            if self.backend:
                pty = self.backend.get_pty()
            if pty is None and self.vte is not None:
                try:
                    pty = self.vte.get_pty()
                except Exception:
                    pass
            if pty and hasattr(pty, 'get_pid'):
                pid = pty.get_pid()
                if pid:
                    self.process_pid = pid
                    return pid
        except Exception as e:
            logger.error(f"Error getting terminal PID: {e}")
        
        return None
        
    def _on_destroy(self, widget):
        """Handle widget destruction"""
        logger.debug(f"Terminal widget {self.session_id} being destroyed")

        # Disconnect backend signal handlers first to prevent callbacks on destroyed objects
        if hasattr(self, 'backend') and self.backend is not None:
            try:
                self._disconnect_backend_signals()
            except Exception as e:
                logger.error(f"Error disconnecting backend signals: {e}")
        elif hasattr(self, 'vte') and self.vte:
            try:
                if hasattr(self, '_child_exited_handler'):
                    self.vte.disconnect(self._child_exited_handler)
                    logger.debug("Disconnected child-exited signal handler")
                if hasattr(self, '_title_changed_handler'):
                    self.vte.disconnect(self._title_changed_handler)
                    logger.debug("Disconnected title-changed signal handler")
                if hasattr(self, '_termprops_changed_handler') and self._termprops_changed_handler is not None:
                    self.vte.disconnect(self._termprops_changed_handler)
                    logger.debug("Disconnected termprops-changed signal handler")
            except Exception as e:
                logger.error(f"Error disconnecting VTE signals: {e}")
        
        # Disconnect from connection manager signals
        if hasattr(self, '_connection_updated_handler') and hasattr(self.connection_manager, 'disconnect'):
            try:
                self.connection_manager.disconnect(self._connection_updated_handler)
                logger.debug("Disconnected from connection manager signals")
            except Exception as e:
                logger.error(f"Error disconnecting from connection manager: {e}")
        
        # Disconnect the terminal
        self.disconnect()

        # Remove custom controllers and disconnect config listeners
        try:
            self._remove_custom_shortcut_controllers()
        except Exception:
            pass

        if getattr(self, '_config_handler', None) is not None and hasattr(self.config, 'disconnect'):
            try:
                self.config.disconnect(self._config_handler)
            except Exception as exc:
                logger.debug("Failed to disconnect config handler: %s", exc)
            finally:
                self._config_handler = None

        # Remove from process manager terminals set (only if not already quitting)
        if not getattr(self, '_is_quitting', False):
            try:
                if self in process_manager.terminals:
                    process_manager.terminals.remove(self)
                    logger.debug(f"Removed terminal {self.session_id} from process manager terminals set")
            except Exception as e:
                logger.debug(f"Error removing terminal from process manager: {e}")

    def _terminate_process_tree(self, pid):
        """Terminate a process and all its children"""
        try:
            # First try to get the process group
            try:
                pgid = os.getpgid(pid)
                logger.debug(f"Terminating process group {pgid}")
                os.killpg(pgid, signal.SIGTERM)
                
                # Give processes a moment to shut down
                time.sleep(0.5)
                
                # Check if any processes are still running
                try:
                    os.killpg(pgid, 0)  # Check if process group exists
                    logger.debug(f"Process group {pgid} still running, sending SIGKILL")
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass  # Process group already gone
                    
            except ProcessLookupError:
                logger.debug(f"Process {pid} already terminated")
                return
                
            # Wait for process to terminate
            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass
                
        except Exception as e:
            logger.error(f"Error terminating process {pid}: {e}")
    
    def _cleanup_process(self, pid):
        """Clean up a process by PID"""
        if not pid:
            return False

        try:
            # Try to get process info from manager first
            pgid = None
            with process_manager.lock:
                if pid in process_manager.processes:
                    pgid = process_manager.processes[pid].get('pgid')
            
            # Fall back to getting PGID from system
            if not pgid:
                try:
                    pgid = os.getpgid(pid)
                except ProcessLookupError:
                    logger.debug(f"Process {pid} already terminated")
                    return True
            
            # First try a clean termination
            try:
                if pgid:
                    try:
                        os.killpg(pgid, signal.SIGTERM)
                        logger.debug(
                            f"Sent SIGTERM to process group {pgid}"
                        )
                    except ProcessLookupError:
                        logger.debug(
                            f"Process group {pgid} already terminated"
                        )
                os.kill(pid, signal.SIGTERM)
                logger.debug(f"Sent SIGTERM to process {pid} (PGID: {pgid})")


                # Wait for clean termination (shorter timeout for faster cleanup)
                for _ in range(2):  # Wait up to 0.2 seconds (reduced from 0.5 seconds)
                    try:
                        os.kill(pid, 0)
                        time.sleep(0.1)
                    except ProcessLookupError:
                        logger.debug(f"Process {pid} terminated cleanly")
                        break
                else:
                    # If still running, force kill
                    try:
                        os.kill(pid, 0)  # Check if still exists
                        logger.debug(f"Process {pid} still running, sending SIGKILL")
                        if pgid:
                            try:
                                os.killpg(pgid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

            except ProcessLookupError:
                pass

            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass
            return True

        except Exception as e:
            logger.error(f"Error terminating process {pid}: {e}")
            return False
    
    def disconnect(self):
        """Close the SSH connection and clean up resources"""
        if not self.is_connected:
            return
            
        logger.debug(f"Disconnecting SSH session {self.session_id}...")
        was_connected = self.is_connected
        self.is_connected = False
        
        # Guard UI emissions when the root window is quitting
        root = self.get_root() if hasattr(self, 'get_root') else None
        is_quitting = bool(getattr(root, '_is_quitting', False))
        
        # Only update manager / UI if not quitting
        if was_connected and hasattr(self, 'connection') and self.connection and not is_quitting:
            self.connection.is_connected = False
            if hasattr(self, 'connection_manager') and self.connection_manager:
                GLib.idle_add(self.connection_manager.emit, 'connection-status-changed', self.connection, False)
        
        try:
            # Try to get the terminal's child PID (with timeout protection)
            pid = None
            try:
                pid = self._get_terminal_pid()
            except Exception as e:
                logger.debug(f"Error getting terminal PID during disconnect: {e}")
            
            # Collect all PIDs that need to be cleaned up
            pids_to_clean = set()
            
            # Add the main process PID if available
            if pid:
                pids_to_clean.add(pid)
            
            # Add the process group ID if available
            if hasattr(self, 'process_pgid') and self.process_pgid:
                pids_to_clean.add(self.process_pgid)
            
            # Add any PIDs from the process manager (with lock timeout)
            try:
                with process_manager.lock:
                    for proc_pid, proc_info in list(process_manager.processes.items()):
                        if proc_info.get('terminal')() is self:
                            pids_to_clean.add(proc_pid)
                            if 'pgid' in proc_info:
                                pids_to_clean.add(proc_info['pgid'])
            except Exception as e:
                logger.debug(f"Error accessing process manager during disconnect: {e}")
            
            # Clean up all collected PIDs (with error handling for each)
            for cleanup_pid in pids_to_clean:
                if cleanup_pid:
                    try:
                        self._cleanup_process(cleanup_pid)
                    except Exception as e:
                        logger.debug(f"Error cleaning up PID {cleanup_pid}: {e}")
            
            # Clean up PTY if it exists
            if hasattr(self, 'pty') and self.pty:
                try:
                    self.pty.close()
                except Exception as e:
                    logger.error(f"Error closing PTY: {e}")
                finally:
                    self.pty = None
            
            # Clean up sshpass temporary directory if it exists
            if hasattr(self, '_sshpass_tmpdir') and self._sshpass_tmpdir:
                try:
                    import shutil
                    shutil.rmtree(self._sshpass_tmpdir, ignore_errors=True)
                    logger.debug(f"Cleaned up sshpass tmpdir: {self._sshpass_tmpdir}")
                except Exception as e:
                    logger.debug(f"Error cleaning up sshpass tmpdir: {e}")
                finally:
                    self._sshpass_tmpdir = None
            
            # Clean up from process manager (only if not quitting)
            if not getattr(self, '_is_quitting', False):
                try:
                    with process_manager.lock:
                        for proc_pid in list(process_manager.processes.keys()):
                            proc_info = process_manager.processes[proc_pid]
                            if proc_info.get('terminal')() is self:
                                logger.debug(f"Removing process {proc_pid} from process manager for terminal {self.session_id}")
                                del process_manager.processes[proc_pid]
                except Exception as e:
                    logger.debug(f"Error cleaning up from process manager: {e}")
            
            # Do not hard-reset here; keep current theme/colors
            
            logger.debug(f"Cleaned up {len(pids_to_clean)} processes for session {self.session_id}")
            
        except Exception as e:
            logger.error(f"Error during disconnect: {e}")
        finally:
            # Clean up references
            self.process_pid = None
            self.process_pgid = None
            
            # Only emit connection-lost signal if not quitting
            if not is_quitting:
                self.emit('connection-lost')
            logger.debug(f"SSH session {self.session_id} disconnected")
    
    def _on_connection_failed(self, error_message):
        """Handle connection failure (called from main thread)"""
        logger.error(f"Connection failed: {error_message}")

        # Cancel any pending fallback timer so we don't mark connection as successful
        if getattr(self, '_fallback_timer_id', None):
            try:
                GLib.source_remove(self._fallback_timer_id)
            except Exception:
                pass
            self._fallback_timer_id = None

        try:
            # Show raw error in terminal
            error_msg = f"\r\n\x1b[31m{error_message}\x1b[0m\r\n"
            if self.backend:
                self.backend.feed(error_msg.encode('utf-8'))
            elif self.vte is not None:
                self.vte.feed(error_msg.encode('utf-8'))

            self.is_connected = False

            # Clean up PTY if it exists
            if hasattr(self, 'pty') and self.pty:
                self.pty.close()
                del self.pty

            # Remember last error for later reporting
            self.last_error_message = error_message

            # Notify UI
            self.emit('connection-failed', error_message)

            # Show reconnect banner with the raw SSH error
            self._set_connecting_overlay_visible(False)
            self._set_disconnected_banner_visible(True, error_message)

        except Exception as e:
            logger.error(f"Error in _on_connection_failed: {e}")

    def on_child_exited(self, terminal, status):
        """Handle terminal child process exit"""
        # Skip if terminal is quitting
        if getattr(self, '_is_quitting', False):
            logger.debug("Terminal is quitting, skipping child exit handler")
            return
            
        logger.debug(f"Terminal child exited with status: {status}")
        
        # Defer the heavy work to avoid blocking the signal handler
        # This prevents potential deadlocks with the UI thread
        def _handle_exit_cleanup():
            try:
                self._handle_child_exit_cleanup(status)
            except Exception as e:
                logger.error(f"Error in exit cleanup: {e}")
            return False  # Don't repeat
        
        # Schedule cleanup on the main thread
        GLib.idle_add(_handle_exit_cleanup)
    
    def _handle_child_exit_cleanup(self, status):
        """Handle the actual cleanup work for child process exit (called from main thread)"""
        logger.debug(f"Starting exit cleanup for status {status}")

        # Clean up process tracking immediately since the process has already exited
        try:
            # Skip getting PID since process is already dead - just clear our tracking
            logger.debug("Clearing process tracking for dead process")
            
            # Clear our stored PID first to prevent any attempts to interact with dead process
            old_pid = getattr(self, 'process_pid', None)
            self.process_pid = None
            
            # Clean up process manager tracking
            with process_manager.lock:
                if old_pid and old_pid in process_manager.processes:
                    logger.debug(f"Removing dead process {old_pid} from tracking")
                    del process_manager.processes[old_pid]
                
                # Remove this terminal from tracking
                if self in process_manager.terminals:
                    logger.debug(f"Removing terminal {id(self)} from tracking")
                    process_manager.terminals.remove(self)
            
            logger.debug("Process tracking cleanup completed")
        except Exception as e:
            logger.error(f"Error cleaning up exited process tracking: {e}")

        # Normalize exit status: GLib may pass waitpid-style status
        exit_code = None
        try:
            if os.WIFEXITED(status):
                exit_code = os.WEXITSTATUS(status)
            else:
                # If not a normal exit or os.WIF* not applicable, best-effort mapping
                exit_code = status if 0 <= int(status) < 256 else ((int(status) >> 8) & 0xFF)
        except Exception:
            try:
                exit_code = int(status)
            except Exception:
                exit_code = status

        # If user explicitly typed 'exit' (clean status 0), update status and close tab immediately
        try:
            if exit_code == 0 and hasattr(self, 'get_root'):
                # Update connection status BEFORE closing the tab
                logger.debug("Clean exit detected, updating connection status before closing tab")
                if self.connection:
                    self.connection.is_connected = False
                self.is_connected = False
                
                # Emit connection status change signal
                if hasattr(self, 'connection_manager') and self.connection_manager and self.connection:
                    GLib.idle_add(self.connection_manager.emit, 'connection-status-changed', self.connection, False)
                
                root = self.get_root()
                if root and hasattr(root, 'tab_view'):
                    page = root.tab_view.get_page(self)
                    if page:
                        try:
                            setattr(root, '_suppress_close_confirmation', True)
                            root.tab_view.close_page(page)
                        finally:
                            try:
                                setattr(root, '_suppress_close_confirmation', False)
                            except Exception:
                                pass
                        return
        except Exception:
            pass

        # Check if this is a controlled reconnect to avoid interfering with the reconnection process
        try:
            if hasattr(self, 'get_root') and self.get_root():
                root = self.get_root()
                if hasattr(root, '_is_controlled_reconnect') and root._is_controlled_reconnect:
                    logger.debug("Controlled reconnect in progress, skipping connection status update")
                    return
        except Exception:
            pass
        
        # Non-zero or unknown exit: treat as connection lost and show banner
        logger.debug("Updating connection status after process exit")
        if self.connection:
            self.connection.is_connected = False

        # Don't call disconnect() here since the process has already exited
        # Just update the connection status and emit signals
        self.is_connected = False
        
        # Update connection manager status
        logger.debug("Scheduling connection manager status update")
        if hasattr(self, 'connection_manager') and self.connection_manager and self.connection:
            GLib.idle_add(self.connection_manager.emit, 'connection-status-changed', self.connection, False)
        
        # Defer all signal emissions and UI updates to prevent deadlocks
        def _finalize_exit_cleanup():
            try:
                logger.debug("Emitting connection-lost signal")
                self.emit('connection-lost')

                # Show reconnect UI with detailed error if available
                logger.debug("Updating UI elements")
                self._set_connecting_overlay_visible(False)
                banner_text = self.last_error_message
                if not banner_text:
                    if exit_code and exit_code != 0:
                        banner_text = _('SSH exited with status {code}').format(code=exit_code)
                    else:
                        banner_text = _('Session ended.')
                self._set_disconnected_banner_visible(True, banner_text)

                logger.debug("Exit cleanup completed successfully")
            except Exception as e:
                logger.error(f"Error in final exit cleanup: {e}")
            return False
        
        # Schedule final cleanup on next idle cycle
        GLib.idle_add(_finalize_exit_cleanup)

    def on_title_changed(self, terminal):
        """
        Handle terminal title change (fallback for older VTE versions).
        
        Note: This uses the deprecated get_window_title() method. On VTE 0.78+,
        title changes are handled via _on_termprops_changed() using TERMPROP_XTERM_TITLE.
        This handler is kept for backward compatibility.
        """
        try:
            # Try to use deprecated method as fallback (for VTE < 0.78)
            title = terminal.get_window_title()
            if title:
                # Parse directory from window title (Method 3: VTE Terminal Widget Approach)
                # The remote shell emits OSC escape sequences to set the window title
                # Common formats: "user@host: /path/to/dir", "/path/to/dir", "user@host:/path/to/dir"
                remote_dir = self._parse_directory_from_title(title)
                if remote_dir:
                    self._current_remote_directory = remote_dir
                    logger.debug(f"Parsed remote directory from window title (deprecated API): {remote_dir}")
                
                self.emit('title-changed', title)
        except Exception as e:
            # get_window_title() might not be available in newer VTE versions
            logger.debug(f"get_window_title() failed (may be deprecated): {e}")
        
        # If terminal is connected and a title update occurs (often when prompt is ready),
        # ensure the reconnect banner is hidden
        try:
            if getattr(self, 'is_connected', False):
                self._set_disconnected_banner_visible(False)
        except Exception:
            pass
    
    def _parse_directory_from_title(self, title: str) -> Optional[str]:
        """
        Parse the current directory from the terminal window title.
        
        Common title formats:
        - "/path/to/dir"
        - "user@host: /path/to/dir"
        - "user@host:/path/to/dir"
        - "SSH: user@host: /path/to/dir"
        - "user@host: ~/projects"
        
        Returns:
            The directory path if found, None otherwise.
        """
        if not title:
            return None
        
        try:
            # Remove common prefixes
            title = title.strip()
            
            # Try to find a path after ":" (common format: user@host: /path)
            if ':' in title:
                # Split by ':' and look for parts that look like paths
                parts = title.split(':')
                for part in reversed(parts):  # Check from end (path is usually last)
                    part = part.strip()
                    if part.startswith('/') or part.startswith('~'):
                        # Found something that looks like a path
                        return part
            
            # If title starts with '/' or '~', it might be just the path
            if title.startswith('/') or title.startswith('~'):
                return title
            
            # Try to extract path patterns
            # Look for paths that start with / or ~
            import re
            # Match paths starting with / or ~
            path_pattern = r'(?::\s*)?([/~][^\s]*|~\S*)'
            match = re.search(path_pattern, title)
            if match:
                return match.group(1).strip()
            
            return None
        except Exception as e:
            logger.debug(f"Failed to parse directory from title '{title}': {e}")
            return None
    
    def get_current_remote_directory(self) -> Optional[str]:
        """
        Get the current remote directory parsed from the window title.
        
        Returns:
            Current remote directory path, or None if not available.
        """
        return getattr(self, '_current_remote_directory', None)

    def on_bell(self, terminal):
        """Handle terminal bell"""
        # Could implement visual bell or notification here
        pass

    def copy_text(self):
        """Copy selected text to clipboard"""
        if self.backend:
            self.backend.copy_clipboard()
        elif self.vte is not None:
            if self.vte.get_has_selection():
                self.vte.copy_clipboard_format(Vte.Format.TEXT)

    def paste_text(self):
        """Paste text from clipboard"""
        if self.backend:
            self.backend.paste_clipboard()
        elif self.vte is not None:
            self.vte.paste_clipboard()

    def select_all(self):
        """Select all text in terminal"""
        if self.backend:
            self.backend.select_all()
        elif self.vte is not None:
            self.vte.select_all()

    def zoom_in(self):
        """Zoom in the terminal font"""
        try:
            current_scale = 1.0
            if self.backend:
                current_scale = self.backend.get_font_scale()
            new_scale = min(current_scale + 0.1, 5.0)  # Max zoom 5x
            if self.backend:
                self.backend.set_font_scale(new_scale)
            logger.debug(f"Terminal zoomed in to {new_scale:.1f}x")
        except Exception as e:
            logger.error(f"Failed to zoom in terminal: {e}")

    def zoom_out(self):
        """Zoom out the terminal font"""
        try:
            current_scale = 1.0
            if self.backend:
                current_scale = self.backend.get_font_scale()
            new_scale = max(current_scale - 0.1, 0.5)  # Min zoom 0.5x
            if self.backend:
                self.backend.set_font_scale(new_scale)
            logger.debug(f"Terminal zoomed out to {new_scale:.1f}x")
        except Exception as e:
            logger.error(f"Failed to zoom out terminal: {e}")

    def reset_zoom(self):
        """Reset terminal zoom to default (1.0x)"""
        try:
            if self.backend:
                self.backend.set_font_scale(1.0)
            logger.debug("Terminal zoom reset to 1.0x")
        except Exception as e:
            logger.error(f"Failed to reset terminal zoom: {e}")

    def reset_terminal(self):
        """Reset terminal"""
        if self.backend:
            self.backend.reset(True, True)
        elif self.vte is not None:
            self.vte.reset(True, True)

    def reset_and_clear(self):
        """Reset and clear terminal"""
        if self.backend:
            self.backend.reset(True, False)
        elif self.vte is not None:
            self.vte.reset(True, False)

    def _show_search_overlay(self, select_all: bool = False):
        """Reveal the terminal search overlay and focus the search entry."""
        try:
            if not hasattr(self, 'search_revealer') or not self.search_revealer:
                return
            self.search_revealer.set_reveal_child(True)
            if hasattr(self, 'search_entry') and self.search_entry:
                if select_all:
                    try:
                        self.search_entry.select_region(0, -1)
                    except Exception:
                        pass
                self.search_entry.grab_focus()
                self._set_search_navigation_sensitive(bool(self.search_entry.get_text()))
        except Exception as exc:
            logger.debug("Failed to show search overlay: %s", exc)

    def _hide_search_overlay(self):
        """Hide the search overlay and return focus to the terminal."""
        try:
            if hasattr(self, 'search_revealer') and self.search_revealer:
                self.search_revealer.set_reveal_child(False)
            self._set_search_error_state(False)
            if hasattr(self, 'vte') and self.vte:
                if self.backend:
                    self.backend.grab_focus()
        except Exception as exc:
            logger.debug("Failed to hide search overlay: %s", exc)

    def _set_search_navigation_sensitive(self, active: bool):
        """Enable or disable navigation buttons based on active search state."""
        try:
            for button in (getattr(self, 'search_prev_button', None), getattr(self, 'search_next_button', None)):
                if button is not None:
                    button.set_sensitive(bool(active))
        except Exception as exc:
            logger.debug("Failed to update search navigation sensitivity: %s", exc)

    def _set_search_error_state(self, has_error: bool):
        """Toggle error styling on the search entry when matches are not found."""
        entry = getattr(self, 'search_entry', None)
        if not entry:
            return
        try:
            if has_error:
                entry.add_css_class('error')
            else:
                entry.remove_css_class('error')
        except Exception:
            pass

    def _clear_search_pattern(self):
        """Clear any active search pattern from the terminal."""
        self._last_search_text = ''
        self._last_search_case_sensitive = False
        self._last_search_regex = False
        self._set_search_navigation_sensitive(False)
        self._set_search_error_state(False)
        try:
            if self.backend:
                self.backend.search_set_regex(None)
        except Exception:
            pass

    def _update_search_pattern(self, text: str, *, case_sensitive: bool = False, regex: bool = False,
                                move_forward: bool = True, update_entry: bool = False) -> bool:
        """Apply or update the search pattern on the VTE widget."""
        if not text:
            self._clear_search_pattern()
            return False

        pattern_changed = (
            text != self._last_search_text or
            case_sensitive != self._last_search_case_sensitive or
            regex != self._last_search_regex
        )

        try:
            if pattern_changed:
                pattern = text if regex else re.escape(text)
                # Use inline case-insensitive flag to avoid Vte.RegexFlags dependency
                if not case_sensitive and not pattern.startswith("(?i)"):
                    pattern = "(?i)" + pattern
                
                # Use backend abstraction for search
                if self.backend:
                    # For VTE backend, create Vte.Regex
                    if hasattr(self.backend, 'vte') and self.backend.vte:
                        search_regex = Vte.Regex.new_for_search(pattern, -1, 0)
                        self.backend.search_set_regex(search_regex)
                        if hasattr(self.backend.vte, 'search_set_wrap_around'):
                            self.backend.vte.search_set_wrap_around(True)
                    else:
                        # For PyXterm backend, pass the pattern as string
                        self.backend.search_set_regex(pattern)
                
                self._last_search_text = text
                self._last_search_case_sensitive = case_sensitive
                self._last_search_regex = regex

            self._set_search_navigation_sensitive(True)

            if move_forward:
                return self._run_search(True, update_entry=update_entry)

            if update_entry:
                self._set_search_error_state(False)

            return True
        except Exception as exc:
            logger.error(f"Search failed: {exc}")
            if update_entry:
                self._set_search_error_state(True)
            return False

    def _run_search(self, forward: bool = True, *, update_entry: bool = False) -> bool:
        """Execute search navigation in the requested direction."""
        try:
            if self.backend:
                found = self.backend.search_find_next() if forward else self.backend.search_find_previous()
            else:
                found = False
        except Exception as exc:
            logger.error(f"Search navigation failed: {exc}")
            found = False

        if update_entry:
            self._set_search_error_state(not found)

        return bool(found)

    def _on_search_entry_changed(self, entry):
        """React to text edits in the search entry."""
        text = entry.get_text() if entry else ''
        if not text:
            self._clear_search_pattern()
            return
        self._update_search_pattern(text, move_forward=True, update_entry=True)

    def _on_search_entry_activate(self, entry):
        """Handle Enter key in the search entry."""
        self._on_search_next()

    def _on_search_entry_stop(self, entry):
        """Handle stop-search events (Escape or clear button)."""
        if not entry.get_text():
            self._clear_search_pattern()
        self._hide_search_overlay()

    def _on_search_entry_key_pressed(self, controller, keyval, keycode, state):
        """Handle additional shortcuts while the search entry is focused."""
        try:
            shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
            primary = bool(state & Gdk.ModifierType.CONTROL_MASK)
            meta = bool(state & Gdk.ModifierType.META_MASK)

            if keyval in (Gdk.KEY_g, Gdk.KEY_G) and (primary or meta):
                if shift:
                    self._on_search_previous()
                else:
                    self._on_search_next()
                return True

            if keyval in (Gdk.KEY_f, Gdk.KEY_F) and (primary or meta):
                self._hide_search_overlay()
                return True

            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and shift:
                self._on_search_previous()
                return True

            if keyval == Gdk.KEY_Escape:
                self._hide_search_overlay()
                return True
        except Exception as exc:
            logger.debug("Search entry key handling failed: %s", exc)
        return False

    def _on_search_next(self, *_args):
        """Navigate to the next search match."""
        text = ''
        if hasattr(self, 'search_entry') and self.search_entry:
            text = self.search_entry.get_text()

        if text:
            if not self._update_search_pattern(text, move_forward=False, update_entry=True):
                return False
        elif not self._last_search_text:
            return False

        return self._run_search(True, update_entry=True)

    def _on_search_previous(self, *_args):
        """Navigate to the previous search match."""
        text = ''
        if hasattr(self, 'search_entry') and self.search_entry:
            text = self.search_entry.get_text()

        if text:
            if not self._update_search_pattern(text, move_forward=False, update_entry=True):
                return False
        elif not self._last_search_text:
            return False

        return self._run_search(False, update_entry=True)

    def search_text(self, text, case_sensitive=False, regex=False):
        """Search for text in terminal"""
        return self._update_search_pattern(
            text,
            case_sensitive=case_sensitive,
            regex=regex,
            move_forward=True,
            update_entry=False,
        )

    def get_connection_info(self):
        """Get connection information"""
        if self.connection:
            return {
                'nickname': self.connection.nickname,
                'hostname': self.connection.hostname,
                'username': self.connection.username,
                'connected': self.is_connected
            }
        return None

    def _is_local_terminal(self):
        """Check if this is a local terminal (not SSH)"""
        try:
            if not hasattr(self, 'connection') or not self.connection:
                return False
            return (hasattr(self.connection, 'hostname') and
                   self.connection.hostname == 'localhost')
        except Exception:
            return False

    def _on_termprops_changed(self, terminal, ids, user_data=None):
        """Handle terminal properties changes for job detection (local terminals only) and window title tracking"""
        # This method should only be called if the signal was successfully connected
        # (i.e., on VTE 0.78+), but add a safety check anyway
        if self._termprops_changed_handler is None:
            logger.debug("termprops-changed handler called but signal was not connected")
            return
            
        try:
            # Check which properties changed - ids should be a list of VteTerminalProp values
            if not ids:
                return
                
            # Convert ids to a set for efficient lookup if it's not already
            changed_props = set(ids) if hasattr(ids, '__iter__') else {ids}
            
            # Check for window title changes (TERMPROP_XTERM_TITLE) - works for both local and remote terminals
            # This replaces the deprecated get_window_title() method (VTE 0.78+)
            # TERMPROP_XTERM_TITLE is a Vte.PropertyType.STRING termprop that stores the xterm window title
            # as set by OSC 0 and OSC 2 escape sequences. It's a string constant 'xterm.title'.
            # Note: This termprop is NOT settable via termprop OSC (read-only).
            # Note: We check for title on any termprops change since checking the specific property ID
            # requires matching string names to integer IDs, which is complex. The operation is lightweight.
            if hasattr(Vte, 'TERMPROP_XTERM_TITLE'):
                try:
                    # Get the title using the modern termprops API
                    # Use get_termprop_string() with TERMPROP_XTERM_TITLE instead of deprecated get_window_title()
                    # Signature: get_termprop_string(prop: str) -> tuple[str | None, int]
                    # Returns: (title_string_or_none, size)
                    title, size = terminal.get_termprop_string(Vte.TERMPROP_XTERM_TITLE)
                    if title:
                        # Parse directory from window title (Method 3: VTE Terminal Widget Approach)
                        # The remote shell emits OSC 0 or OSC 2 escape sequences to set the window title
                        remote_dir = self._parse_directory_from_title(title)
                        if remote_dir:
                            self._current_remote_directory = remote_dir
                            logger.debug(f"Parsed remote directory from TERMPROP_XTERM_TITLE: {remote_dir}")
                        
                        # Emit title-changed signal for compatibility
                        self.emit('title-changed', title)
                except Exception as e:
                    logger.debug(f"Failed to get window title from TERMPROP_XTERM_TITLE: {e}")
            
            # Job detection is only enabled for local terminals
            if not self._is_local_terminal():
                return
            
            # Check if job finished (also gives exit status)
            # These constants are only available in VTE 0.78+
            if hasattr(Vte, 'TERMPROP_SHELL_POSTEXEC') and Vte.TERMPROP_SHELL_POSTEXEC in changed_props:
                ok, code = terminal.get_termprop_uint(Vte.TERMPROP_SHELL_POSTEXEC)
                if ok:
                    self._job_status = "IDLE"
                    logger.debug(f"Local terminal job finished with exit code: {code}")
                    return
            
            # Check if job is running
            if hasattr(Vte, 'TERMPROP_SHELL_PREEXEC') and Vte.TERMPROP_SHELL_PREEXEC in changed_props:
                ok, _ = terminal.get_termprop_value(Vte.TERMPROP_SHELL_PREEXEC)
                if ok:
                    self._job_status = "RUNNING"
                    logger.debug("Local terminal job is running")
                    return
            
            # Check if prompt is visible
            if hasattr(Vte, 'TERMPROP_SHELL_PRECMD') and Vte.TERMPROP_SHELL_PRECMD in changed_props:
                ok, _ = terminal.get_termprop_value(Vte.TERMPROP_SHELL_PRECMD)
                if ok:
                    self._job_status = "PROMPT"
                    logger.debug("Local terminal prompt is visible")
                    return
                
        except Exception as e:
            logger.debug(f"Error in termprops changed handler: {e}")

    def is_terminal_idle(self):
        """
        Check if the terminal is idle (no active job running).
        Only works for local terminals.
        
        Returns:
            bool: True if terminal is idle, False if job is running or unknown.
                  For SSH terminals, always returns False.
        """
        # Only enable job detection for local terminals
        if not self._is_local_terminal():
            logger.debug("Job detection not available for SSH terminals")
            return False
            
        try:
            # First try VTE termprops method (shell-specific)
            if self._job_status in ["IDLE", "PROMPT"]:
                return True
            elif self._job_status == "RUNNING":
                return False
            
            # Fall back to shell-agnostic PTY method
            return self._is_terminal_idle_pty()
            
        except Exception as e:
            logger.debug(f"Error checking terminal idle state: {e}")
            return False

    def _is_terminal_idle_pty(self):
        """
        Shell-agnostic check using PTY FD and POSIX job control.
        Only works for local terminals.
        
        Returns:
            bool: True if terminal is idle (at prompt), False if job is running
        """
        # Only enable job detection for local terminals
        if not self._is_local_terminal():
            return False
            
        try:
            if not hasattr(self, 'vte') or not self.vte:
                return False
                
            pty = None
            if self.backend:
                pty = self.backend.get_pty()
            if pty is None and self.vte is not None:
                try:
                    pty = self.vte.get_pty()
                except Exception:
                    pass
            if not pty:
                return False
                
            fd = pty.get_fd()
            if fd < 0:
                return False
            
            # Get foreground process group
            fg_pgid = os.tcgetpgrp(fd)
            
            # If we have stored shell PGID, compare with foreground PGID
            if self._shell_pgid is not None:
                idle = (fg_pgid == self._shell_pgid)
                logger.debug(f"Local terminal PTY job detection: fg_pgid={fg_pgid}, shell_pgid={self._shell_pgid}, idle={idle}")
                return idle
            
            # If no shell PGID stored, assume idle (conservative approach)
            logger.debug(f"Local terminal PTY job detection: fg_pgid={fg_pgid}, no shell_pgid stored, assuming idle")
            return True
            
        except Exception as e:
            logger.debug(f"Error in PTY job detection: {e}")
            return False

    def get_job_status(self):
        """
        Get the current job status of the terminal.
        Only works for local terminals.
        
        Returns:
            str: Current status - "IDLE", "RUNNING", "PROMPT", "UNKNOWN", or "SSH_TERMINAL"
        """
        if not self._is_local_terminal():
            return "SSH_TERMINAL"
        return self._job_status

    def _setup_fullscreen_shortcut(self):
        """Setup F11 keyboard shortcut for fullscreen toggle and ESC to exit fullscreen."""
        try:
            from gi.repository import Gdk
            
            # Create keyboard controller for F11 and ESC
            key_controller = Gtk.EventControllerKey()
            key_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            
            def on_key_pressed(controller, keyval, keycode, state):
                # F11 key - toggle fullscreen
                if keyval == Gdk.KEY_F11:
                    self.toggle_fullscreen()
                    return True
                # ESC key - exit fullscreen if currently in fullscreen
                elif keyval == Gdk.KEY_Escape and self._is_fullscreen:
                    self._exit_fullscreen()
                    return True  # Consume ESC to prevent NavigationSplitView from showing sidebar
                return False
            
            key_controller.connect('key-pressed', on_key_pressed)
            self.add_controller(key_controller)
            logger.debug("Fullscreen shortcut (F11) and ESC exit registered")
        except Exception as e:
            logger.debug(f"Failed to setup fullscreen shortcut: {e}", exc_info=True)
    
    def toggle_fullscreen(self):
        """Toggle fullscreen mode for the terminal widget."""
        if self._is_fullscreen:
            self._exit_fullscreen()
        else:
            self._enter_fullscreen()
    
    def _enter_fullscreen(self):
        """Enter fullscreen mode - hide sidebar, header bar, and tab bar."""
        if self._is_fullscreen:
            return
        
        try:
            root = self.get_root()
            if not root:
                logger.debug("Cannot enter fullscreen: window not found")
                return
            
            # Store current state
            self._fullscreen_sidebar_visible = None
            self._fullscreen_sidebar_show_content = None
            self._fullscreen_header_visible = None
            self._fullscreen_tab_bar_visible = None
            self._fullscreen_update_banner_visible = None
            self._fullscreen_broadcast_banner_visible = None
            
            # Store window state before going fullscreen
            try:
                # Check if window is maximized
                if hasattr(root, 'is_maximized'):
                    self._was_maximized = root.is_maximized()
                elif hasattr(root, 'get_maximized'):
                    self._was_maximized = root.get_maximized()
            except Exception:
                self._was_maximized = False
            
            # Hide sidebar if it exists
            if hasattr(root, 'split_view'):
                try:
                    # Check split view type using _split_variant attribute or method detection
                    split_variant = getattr(root, '_split_variant', None)
                    HAS_OVERLAY_SPLIT = hasattr(Adw, 'OverlaySplitView')
                    HAS_NAV_SPLIT = hasattr(Adw, 'NavigationSplitView')
                    
                    if HAS_OVERLAY_SPLIT and split_variant == 'overlay':
                        # OverlaySplitView: use set_show_sidebar (simpler API)
                        if hasattr(root.split_view, 'get_show_sidebar'):
                            self._fullscreen_sidebar_visible = root.split_view.get_show_sidebar()
                            root.split_view.set_show_sidebar(False)
                            logger.debug("OverlaySplitView sidebar hidden for fullscreen")
                    elif HAS_NAV_SPLIT and split_variant == 'navigation':
                        # NavigationSplitView: use collapsed and show_content
                        try:
                            # Store original state
                            self._fullscreen_sidebar_collapsed = root.split_view.get_collapsed()
                            self._fullscreen_sidebar_show_content = root.split_view.get_show_content()
                            # Hide sidebar: collapse and show content (content visible, sidebar hidden)
                            root.split_view.set_collapsed(True)
                            root.split_view.set_show_content(True)
                            logger.debug("NavigationSplitView sidebar hidden for fullscreen")
                        except Exception as e:
                            logger.debug(f"Failed to hide NavigationSplitView sidebar: {e}")
                    elif split_variant == 'paned':
                        # Gtk.Paned: hide the start child widget
                        sidebar_widget = root.split_view.get_start_child()
                        if sidebar_widget:
                            self._fullscreen_sidebar_visible = sidebar_widget.get_visible()
                            sidebar_widget.set_visible(False)
                            logger.debug("Gtk.Paned sidebar hidden for fullscreen")
                    else:
                        # Fallback: try common methods
                        if hasattr(root.split_view, 'get_show_sidebar'):
                            self._fullscreen_sidebar_visible = root.split_view.get_show_sidebar()
                            root.split_view.set_show_sidebar(False)
                        elif hasattr(root.split_view, 'get_sidebar_visible'):
                            self._fullscreen_sidebar_visible = root.split_view.get_sidebar_visible()
                            root.split_view.set_sidebar_visible(False)
                except Exception as e:
                    logger.debug(f"Failed to hide sidebar: {e}", exc_info=True)
            
            # Hide header bar - it's added to ToolbarView via add_top_bar()
            # Try multiple methods to ensure it's hidden
            if hasattr(root, 'header_bar'):
                try:
                    self._fullscreen_header_visible = root.header_bar.get_visible()
                    # Method 1: Direct visibility
                    root.header_bar.set_visible(False)
                    # Method 2: Also try hide() method
                    if hasattr(root.header_bar, 'hide'):
                        root.header_bar.hide()
                    logger.debug("Header bar hidden for fullscreen")
                except Exception as e:
                    logger.debug(f"Failed to hide header bar: {e}", exc_info=True)
            else:
                logger.debug("header_bar attribute not found on root window")
            
            # Hide tab bar if it exists
            if hasattr(root, 'tab_bar'):
                try:
                    self._fullscreen_tab_bar_visible = root.tab_bar.get_visible()
                    root.tab_bar.set_visible(False)
                    # Also try hide() method
                    if hasattr(root.tab_bar, 'hide'):
                        root.tab_bar.hide()
                    logger.debug("Tab bar hidden for fullscreen")
                except Exception as e:
                    logger.debug(f"Failed to hide tab bar: {e}", exc_info=True)
            else:
                logger.debug("tab_bar attribute not found on root window")
            
            # Hide update banner if it exists
            if hasattr(root, 'update_banner_container'):
                try:
                    self._fullscreen_update_banner_visible = root.update_banner_container.get_visible()
                    root.update_banner_container.set_visible(False)
                    logger.debug("Update banner hidden for fullscreen")
                except Exception as e:
                    logger.debug(f"Failed to hide update banner: {e}", exc_info=True)
            
            # Hide broadcast banner if it exists
            if hasattr(root, 'broadcast_banner'):
                try:
                    self._fullscreen_broadcast_banner_visible = root.broadcast_banner.get_visible()
                    root.broadcast_banner.set_visible(False)
                    logger.debug("Broadcast banner hidden for fullscreen")
                except Exception as e:
                    logger.debug(f"Failed to hide broadcast banner: {e}", exc_info=True)
            
            # Add CSS class to window for targeted hiding of header bar and tab bar
            try:
                root.add_css_class('terminal-fullscreen-mode')
                logger.debug("Added terminal-fullscreen-mode CSS class to window")
            except Exception as e:
                logger.debug(f"Failed to add CSS class: {e}")
            
            # Use CSS as a fallback to hide header bar and tab bar
            try:
                from gi.repository import Gdk
                display = Gdk.Display.get_default()
                if display:
                    css_provider = Gtk.CssProvider()
                    css = """
                    .terminal-fullscreen-mode headerbar {
                        opacity: 0 !important;
                        min-height: 0 !important;
                        max-height: 0 !important;
                        margin: 0 !important;
                        padding: 0 !important;
                    }
                    .terminal-fullscreen-mode tabbar {
                        opacity: 0 !important;
                        min-height: 0 !important;
                        max-height: 0 !important;
                        margin: 0 !important;
                        padding: 0 !important;
                    }
                    """
                    css_provider.load_from_data(css.encode('utf-8'))
                    Gtk.StyleContext.add_provider_for_display(
                        display, css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                    )
                    self._fullscreen_css_provider = css_provider
                    logger.debug("Applied CSS to hide header bar and tab bar")
            except Exception as e:
                logger.debug(f"Failed to apply CSS for fullscreen: {e}")
            
            # Make the window fullscreen (takes up entire screen)
            try:
                if hasattr(root, 'fullscreen'):
                    root.fullscreen()
                elif hasattr(root, 'set_fullscreen'):
                    root.set_fullscreen(True)
                logger.debug("Window set to fullscreen")
            except Exception as e:
                logger.debug(f"Failed to set window fullscreen: {e}", exc_info=True)
            
            # Add window-level key controller to catch ESC before NavigationSplitView handles it
            try:
                from gi.repository import Gdk
                self._fullscreen_key_controller = Gtk.EventControllerKey()
                self._fullscreen_key_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
                
                def on_fullscreen_key_pressed(controller, keyval, keycode, state):
                    # ESC key - exit fullscreen
                    if keyval == Gdk.KEY_Escape and self._is_fullscreen:
                        self._exit_fullscreen()
                        return True  # Consume ESC to prevent NavigationSplitView from showing sidebar
                    return False
                
                self._fullscreen_key_controller.connect('key-pressed', on_fullscreen_key_pressed)
                root.add_controller(self._fullscreen_key_controller)
                logger.debug("Window-level ESC handler added for fullscreen mode")
            except Exception as e:
                logger.debug(f"Failed to add window-level ESC handler: {e}", exc_info=True)
            
            # Create fullscreen banner container if it doesn't exist (but don't show it yet)
            if self._fullscreen_banner_container is None:
                self._create_fullscreen_banner()
            
            # Add banner to window level (above terminal) if not already added
            if self._fullscreen_banner_container and self._fullscreen_banner_container.get_parent() is None:
                # Find the content wrapper that contains banners and content_stack
                try:
                    if hasattr(root, 'content_stack'):
                        parent = root.content_stack.get_parent()
                        if parent:
                            # Prepend banner at the beginning to appear above everything
                            parent.prepend(self._fullscreen_banner_container)
                            logger.debug("Fullscreen banner added to window content area")
                except Exception as e:
                    logger.debug(f"Failed to add fullscreen banner to window: {e}", exc_info=True)
            
            # Show fullscreen banner with help text
            self._show_fullscreen_banner()
            
            self._is_fullscreen = True
            logger.debug("Entered terminal fullscreen mode")
            
            # Restore focus to terminal after fullscreen operations
            def restore_focus():
                try:
                    if hasattr(self, 'backend') and self.backend:
                        self.backend.grab_focus()
                    elif hasattr(self, 'vte') and hasattr(self.vte, 'grab_focus'):
                        self.vte.grab_focus()
                    elif hasattr(self, 'grab_focus'):
                        self.grab_focus()
                except Exception as e:
                    logger.debug(f"Failed to restore focus after fullscreen: {e}")
                return False
            GLib.idle_add(restore_focus)
        except Exception as e:
            logger.error(f"Failed to enter fullscreen: {e}", exc_info=True)
    
    def _exit_fullscreen(self):
        """Exit fullscreen mode - restore sidebar, header bar, and tab bar."""
        if not self._is_fullscreen:
            return
        
        try:
            root = self.get_root()
            if not root:
                logger.debug("Cannot exit fullscreen: window not found")
                self._is_fullscreen = False
                return
            
            # Restore sidebar
            if hasattr(root, 'split_view') and self._fullscreen_sidebar_visible is not None:
                try:
                    # Check split view type using _split_variant attribute or method detection
                    split_variant = getattr(root, '_split_variant', None)
                    HAS_OVERLAY_SPLIT = hasattr(Adw, 'OverlaySplitView')
                    HAS_NAV_SPLIT = hasattr(Adw, 'NavigationSplitView')
                    
                    if HAS_OVERLAY_SPLIT and split_variant == 'overlay':
                        # OverlaySplitView: use set_show_sidebar (simpler API)
                        if hasattr(root.split_view, 'set_show_sidebar'):
                            root.split_view.set_show_sidebar(self._fullscreen_sidebar_visible)
                            logger.debug("OverlaySplitView sidebar restored")
                    elif HAS_NAV_SPLIT and split_variant == 'navigation':
                        # NavigationSplitView: restore using collapsed and show_content
                        try:
                            if hasattr(self, '_fullscreen_sidebar_collapsed') and hasattr(self, '_fullscreen_sidebar_show_content'):
                                root.split_view.set_collapsed(self._fullscreen_sidebar_collapsed)
                                root.split_view.set_show_content(self._fullscreen_sidebar_show_content)
                                logger.debug(f"NavigationSplitView restored: collapsed={self._fullscreen_sidebar_collapsed}, show_content={self._fullscreen_sidebar_show_content}")
                            else:
                                # Fallback: if we don't have stored state, un-collapse to show both
                                root.split_view.set_collapsed(False)
                                logger.debug("NavigationSplitView restored to default (un-collapsed)")
                        except Exception as e:
                            logger.debug(f"Failed to restore NavigationSplitView sidebar: {e}")
                    elif split_variant == 'paned':
                        # Gtk.Paned: show the start child widget
                        sidebar_widget = root.split_view.get_start_child()
                        if sidebar_widget:
                            sidebar_widget.set_visible(self._fullscreen_sidebar_visible)
                            logger.debug("Gtk.Paned sidebar restored")
                    else:
                        # Fallback: try common methods
                        if hasattr(root.split_view, 'set_show_sidebar'):
                            root.split_view.set_show_sidebar(self._fullscreen_sidebar_visible)
                        elif hasattr(root.split_view, 'set_sidebar_visible'):
                            root.split_view.set_sidebar_visible(self._fullscreen_sidebar_visible)
                except Exception as e:
                    logger.debug(f"Failed to restore sidebar: {e}", exc_info=True)
            
            # Remove CSS class from window
            try:
                root.remove_css_class('terminal-fullscreen-mode')
                logger.debug("Removed terminal-fullscreen-mode CSS class from window")
            except Exception as e:
                logger.debug(f"Failed to remove CSS class: {e}")
            
            # Remove CSS provider if it was added
            if self._fullscreen_css_provider:
                try:
                    from gi.repository import Gdk
                    display = Gdk.Display.get_default()
                    if display:
                        Gtk.StyleContext.remove_provider_for_display(
                            display, self._fullscreen_css_provider
                        )
                    self._fullscreen_css_provider = None
                    logger.debug("Removed fullscreen CSS provider")
                except Exception as e:
                    logger.debug(f"Failed to remove CSS provider: {e}")
            
            # Restore header bar
            if hasattr(root, 'header_bar') and self._fullscreen_header_visible is not None:
                try:
                    root.header_bar.set_visible(self._fullscreen_header_visible)
                    # Also try show() method
                    if hasattr(root.header_bar, 'show'):
                        root.header_bar.show()
                except Exception as e:
                    logger.debug(f"Failed to restore header bar: {e}")
            
            # Restore tab bar
            if hasattr(root, 'tab_bar') and self._fullscreen_tab_bar_visible is not None:
                try:
                    root.tab_bar.set_visible(self._fullscreen_tab_bar_visible)
                    # Also try show() method
                    if hasattr(root.tab_bar, 'show'):
                        root.tab_bar.show()
                except Exception as e:
                    logger.debug(f"Failed to restore tab bar: {e}")
            
            # Restore update banner
            if hasattr(root, 'update_banner_container') and self._fullscreen_update_banner_visible is not None:
                try:
                    root.update_banner_container.set_visible(self._fullscreen_update_banner_visible)
                except Exception as e:
                    logger.debug(f"Failed to restore update banner: {e}")
            
            # Restore broadcast banner
            if hasattr(root, 'broadcast_banner') and self._fullscreen_broadcast_banner_visible is not None:
                try:
                    root.broadcast_banner.set_visible(self._fullscreen_broadcast_banner_visible)
                except Exception as e:
                    logger.debug(f"Failed to restore broadcast banner: {e}")
            
            # Unfullscreen the window
            try:
                if hasattr(root, 'unfullscreen'):
                    root.unfullscreen()
                elif hasattr(root, 'set_fullscreen'):
                    root.set_fullscreen(False)
                logger.debug("Window unfullscreen")
            except Exception as e:
                logger.debug(f"Failed to unfullscreen window: {e}", exc_info=True)
            
            # Restore maximized state if it was maximized before
            if self._was_maximized:
                try:
                    if hasattr(root, 'maximize'):
                        root.maximize()
                    elif hasattr(root, 'set_maximized'):
                        root.set_maximized(True)
                except Exception as e:
                    logger.debug(f"Failed to restore maximized state: {e}")
            
            # Remove window-level key controller
            if self._fullscreen_key_controller:
                try:
                    root.remove_controller(self._fullscreen_key_controller)
                    self._fullscreen_key_controller = None
                    logger.debug("Window-level ESC handler removed")
                except Exception as e:
                    logger.debug(f"Failed to remove window-level ESC handler: {e}", exc_info=True)
            
            # Hide fullscreen banner
            self._hide_fullscreen_banner()
            
            # Remove banner from window when exiting fullscreen
            if self._fullscreen_banner_container:
                try:
                    parent = self._fullscreen_banner_container.get_parent()
                    if parent:
                        parent.remove(self._fullscreen_banner_container)
                        logger.debug("Fullscreen banner removed from window")
                except Exception as e:
                    logger.debug(f"Failed to remove fullscreen banner from window: {e}", exc_info=True)
            
            self._is_fullscreen = False
            logger.debug("Exited terminal fullscreen mode")
            
            # Restore focus to terminal after exiting fullscreen
            def restore_focus():
                try:
                    if hasattr(self, 'backend') and self.backend:
                        self.backend.grab_focus()
                    elif hasattr(self, 'vte') and hasattr(self.vte, 'grab_focus'):
                        self.vte.grab_focus()
                    elif hasattr(self, 'grab_focus'):
                        self.grab_focus()
                except Exception as e:
                    logger.debug(f"Failed to restore focus after exiting fullscreen: {e}")
                return False
            GLib.idle_add(restore_focus)
        except Exception as e:
            logger.error(f"Failed to exit fullscreen: {e}", exc_info=True)
            self._is_fullscreen = False
    
    def _create_fullscreen_banner(self):
        """Create fullscreen banner container (but don't show it yet)."""
        try:
            # Create banner container if it doesn't exist (matching update banner style)
            if self._fullscreen_banner_container is None:
                # Use overlay to position dismiss button on top of banner (same as update banner)
                banner_overlay = Gtk.Overlay()
                # Make overlay expand to full width
                banner_overlay.set_hexpand(True)
                
                fullscreen_banner = Adw.Banner()
                fullscreen_banner.set_title(_("Press F11 to exit fullscreen mode"))
                fullscreen_banner.set_button_label(_("Exit Fullscreen"))
                
                # Set button style to "suggested" and connect click handler
                try:
                    # Use set_button_style if available (Adw 1.7+)
                    if hasattr(fullscreen_banner, 'set_button_style'):
                        # Use enum value for suggested style
                        fullscreen_banner.set_button_style(Adw.BannerButtonStyle.SUGGESTED)
                    else:
                        # Fallback: Apply suggested style via CSS
                        from gi.repository import Gdk
                        display = Gdk.Display.get_default()
                        if display:
                            css_provider = Gtk.CssProvider()
                            css = """
                            banner.fullscreen-banner button {
                                background-color: @suggested_bg_color;
                                color: @suggested_fg_color;
                            }
                            banner.fullscreen-banner button:hover {
                                background-color: @suggested_hover_bg_color;
                            }
                            banner.fullscreen-banner button:active {
                                background-color: @suggested_active_bg_color;
                            }
                            """
                            css_provider.load_from_data(css.encode('utf-8'))
                            Gtk.StyleContext.add_provider_for_display(
                                display, css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                            )
                            fullscreen_banner.add_css_class('fullscreen-banner')
                    
                    # Connect button click handler
                    def on_banner_button_clicked(banner):
                        self.toggle_fullscreen()
                    
                    fullscreen_banner.connect('button-clicked', on_banner_button_clicked)
                except Exception as e:
                    logger.debug(f"Failed to style fullscreen banner button: {e}", exc_info=True)
                
                # Make banner expand to fill available width (Adw.Banner supports hexpand per docs)
                fullscreen_banner.set_hexpand(True)
                banner_overlay.set_child(fullscreen_banner)
                
                # Create dismiss button with text, positioned at the left (same as update banner)
                dismiss_button = Gtk.Button()
                dismiss_button.set_label(_('Dismiss'))
                dismiss_button.set_halign(Gtk.Align.START)
                dismiss_button.set_valign(Gtk.Align.CENTER)
                dismiss_button.set_margin_start(12)
                dismiss_button.connect('clicked', self._on_fullscreen_banner_dismiss)
                banner_overlay.add_overlay(dismiss_button)
                
                self._fullscreen_banner_container = banner_overlay
                self._fullscreen_banner_dismiss_button = dismiss_button
                
                # Make banner container expand to fill full width
                self._fullscreen_banner_container.set_hexpand(True)
                self._fullscreen_banner_container.set_vexpand(False)
                
                # Banner will be added to window level when entering fullscreen
                # Store reference but don't add to any container yet
                self._fullscreen_banner_container.set_visible(False)  # Hidden by default
                
                # Configure banner positioning for full width
                self._fullscreen_banner_container.set_halign(Gtk.Align.FILL)
                self._fullscreen_banner_container.set_valign(Gtk.Align.START)
                # Remove margins to make it truly full width
                self._fullscreen_banner_container.set_margin_start(0)
                self._fullscreen_banner_container.set_margin_end(0)
                self._fullscreen_banner_container.set_margin_top(0)
                
                logger.debug("Fullscreen banner container created")
        except Exception as e:
            logger.error(f"Failed to create fullscreen banner: {e}", exc_info=True)
    
    def _show_fullscreen_banner(self):
        """Show fullscreen banner with help text and exit button."""
        try:
            # Ensure banner container exists
            if self._fullscreen_banner_container is None:
                self._create_fullscreen_banner()
            
            # Show the banner
            if self._fullscreen_banner_container:
                banner = self._fullscreen_banner_container.get_child()
                if banner and isinstance(banner, Adw.Banner):
                    banner.set_revealed(True)
                self._fullscreen_banner_container.set_visible(True)
            
            logger.debug("Fullscreen banner shown")
        except Exception as e:
            logger.debug(f"Failed to show fullscreen banner: {e}", exc_info=True)
    
    def _on_fullscreen_banner_dismiss(self, button):
        """Handle dismiss button click on fullscreen banner."""
        logger.debug("Fullscreen banner dismissed by user")
        self._hide_fullscreen_banner()
    
    def _hide_fullscreen_banner(self):
        """Hide fullscreen banner."""
        try:
            if self._fullscreen_banner_container:
                banner = self._fullscreen_banner_container.get_child()
                if banner and isinstance(banner, Adw.Banner):
                    banner.set_revealed(False)
                self._fullscreen_banner_container.set_visible(False)
            
            logger.debug("Fullscreen banner hidden")
        except Exception as e:
            logger.debug(f"Failed to hide fullscreen banner: {e}", exc_info=True)
    
    def _setup_drag_and_drop(self):
        """Set up drag and drop for SCP upload from filesystem."""
        try:
            # Create drop target for file drops from filesystem
            # According to GTK4 docs, filesystem drops come as Gdk.FileList
            # Use GObject.TYPE_NONE and set_gtypes to support multiple types
            drop_target = Gtk.DropTarget.new(type=GObject.TYPE_NONE, actions=Gdk.DragAction.COPY)
            drop_target.set_gtypes([Gdk.FileList, Gio.File])
            drop_target.connect("drop", self._on_file_drop)
            drop_target.connect("enter", self._on_drop_enter)
            drop_target.connect("leave", self._on_drop_leave)
            
            # Add drop target to the overlay (works for VTE backend)
            self.overlay.add_controller(drop_target)
            
            # Also add to backend widget for PyXterm (WebView)
            if self.backend and hasattr(self.backend, 'widget'):
                backend_widget = self.backend.widget
                if backend_widget and backend_widget != self.overlay:
                    # Create a separate drop target for the backend widget
                    backend_drop_target = Gtk.DropTarget.new(type=GObject.TYPE_NONE, actions=Gdk.DragAction.COPY)
                    backend_drop_target.set_gtypes([Gdk.FileList, Gio.File])
                    backend_drop_target.connect("drop", self._on_file_drop)
                    backend_drop_target.connect("enter", self._on_drop_enter)
                    backend_drop_target.connect("leave", self._on_drop_leave)
                    backend_widget.add_controller(backend_drop_target)
                    logger.debug("Drag and drop support added to backend widget (PyXterm)")
            
            logger.debug("Drag and drop support added to terminal")
        except Exception as e:
            logger.error(f"Failed to set up drag and drop: {e}", exc_info=True)
    
    def _on_drop_enter(self, drop_target, x, y):
        """Handle drag enter event - show visual feedback."""
        try:
            # Check if we have a valid connection
            if not self.connection or not self.is_connected:
                return Gdk.DragAction.NONE
            
            # Only accept drops if we have a remote connection (not local shell)
            if self._is_local_terminal():
                return Gdk.DragAction.NONE
            
            return Gdk.DragAction.COPY
        except Exception as e:
            logger.debug(f"Error in drop enter: {e}", exc_info=True)
            return Gdk.DragAction.NONE
    
    def _on_drop_leave(self, drop_target):
        """Handle drag leave event."""
        pass
    
    def _on_file_drop(self, drop_target, value, x, y):
        """Handle file drop event - initiate SCP upload."""
        try:
            # Check if we have a valid connection
            if not self.connection or not self.is_connected:
                logger.debug("Drop rejected: no active connection")
                return False
            
            # Only accept drops for remote connections (not local shell)
            if self._is_local_terminal():
                logger.debug("Drop rejected: local terminal")
                return False
            
            # Extract file paths from the drop value
            file_paths = []
            
            # Handle GObject.Value wrapper (GTK4 may wrap the value)
            if isinstance(value, GObject.Value):
                # Try different methods to extract the actual value
                extracted = None
                for getter in ("get_object", "get_boxed", "get"):
                    try:
                        extracted = getattr(value, getter)()
                        if extracted is not None:
                            break
                    except Exception:
                        continue
                if extracted is not None:
                    value = extracted
            
            # Handle Gdk.FileList (standard format for filesystem drops in GTK4)
            if isinstance(value, Gdk.FileList):
                files = value.get_files()
                for file in files:
                    if isinstance(file, Gio.File):
                        path = file.get_path()
                        if path:
                            file_paths.append(path)
            # Handle single Gio.File (fallback)
            elif isinstance(value, Gio.File):
                path = value.get_path()
                if path:
                    file_paths.append(path)
            # Handle list of Gio.File objects (fallback)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, Gio.File):
                        path = item.get_path()
                        if path:
                            file_paths.append(path)
            # Try to get path directly (might be a GFile-like object)
            elif hasattr(value, 'get_path'):
                try:
                    path = value.get_path()
                    if path:
                        file_paths.append(path)
                except Exception:
                    pass
            
            if not file_paths:
                logger.debug("Drop rejected: no valid file paths extracted from value type: %s", type(value))
                return False
            
            # Get MainWindow instance to call SCP upload
            root = self.get_root()
            if not root or not hasattr(root, '_start_scp_transfer'):
                logger.debug("Drop rejected: MainWindow not found")
                return False
            
            # Get current directory from the active terminal session
            # Method 3: Use VTE window-title-changed approach (primary method)
            # The remote shell emits OSC escape sequences that set the window title with the directory
            destination = self.get_current_remote_directory()
            
            # Fallback: If we don't have directory from window title, use the terminal-based method
            if not destination:
                logger.debug("Directory not available from window title, falling back to terminal-based method")
                try:
                    import time
                    import random
                    import subprocess
                    import shutil
                    from .ssh_utils import build_connection_ssh_options
                    
                    # Generate unique temp file name using timestamp and random number
                    temp_filename = f"/tmp/sshpilot_pwd_{int(time.time())}_{random.randint(1000, 9999)}.txt"
                    
                    # Send pwd command to active terminal session to write current directory to temp file
                    # Use $$ to get shell PID for uniqueness, or use the generated filename
                    pwd_cmd = f"pwd > {temp_filename}\n"
                    
                    logger.debug(f"Sending pwd command to terminal: {repr(pwd_cmd)}")
                    
                    # Send command to terminal backend
                    if hasattr(self, 'backend') and self.backend and hasattr(self.backend, 'feed_child'):
                        self.backend.feed_child(pwd_cmd.encode('utf-8'))
                    elif hasattr(self, 'vte') and self.vte:
                        self.vte.feed_child(pwd_cmd.encode('utf-8'))
                    else:
                        logger.warning("No terminal backend available to send pwd command")
                        raise Exception("Terminal backend not available")
                    
                    # Wait a moment for the command to execute
                    time.sleep(0.5)
                    
                    # Now read the temp file via SSH using ssh_connection_builder
                    from .ssh_connection_builder import build_ssh_connection, ConnectionContext
                    
                    # Build SSH connection command using ssh_connection_builder
                    ctx = ConnectionContext(
                        connection=self.connection,
                        connection_manager=self.connection_manager,
                        config=self.config,
                        command_type='ssh',
                        extra_args=[f"cat {temp_filename}"],  # Command to run
                        port_forwarding_rules=None,
                        remote_command=f"cat {temp_filename}",
                        local_command=None,
                        extra_ssh_config=None,
                        known_hosts_path=None,
                        native_mode=False,
                        quick_connect_mode=False,
                        quick_connect_command=None,
                    )
                    
                    ssh_conn_cmd = build_ssh_connection(ctx)
                    ssh_cmd = ssh_conn_cmd.command
                    env = ssh_conn_cmd.env.copy()
                    
                    logger.debug(f"Reading pwd from temp file: {' '.join(ssh_cmd)}")
                    result = subprocess.run(
                        ssh_cmd,
                        env=env,
                        text=True,
                        capture_output=True,
                        timeout=5,
                    )
                    
                    # Clean up temp file (best effort) - build cleanup command
                    try:
                        cleanup_ctx = ConnectionContext(
                            connection=self.connection,
                            connection_manager=self.connection_manager,
                            config=self.config,
                            command_type='ssh',
                            extra_args=[f"rm -f {temp_filename}"],
                            port_forwarding_rules=None,
                            remote_command=f"rm -f {temp_filename}",
                            local_command=None,
                            extra_ssh_config=None,
                            known_hosts_path=None,
                            native_mode=False,
                            quick_connect_mode=False,
                            quick_connect_command=None,
                        )
                        cleanup_cmd_obj = build_ssh_connection(cleanup_ctx)
                        cleanup_cmd = cleanup_cmd_obj.command
                        subprocess.run(cleanup_cmd, env=cleanup_cmd_obj.env, timeout=2, capture_output=True)
                    except Exception:
                        pass  # Ignore cleanup errors
                    
                    logger.debug(f"pwd file read result: returncode={result.returncode}, stdout={repr(result.stdout)}, stderr={repr(result.stderr)}")
                    
                    if result.returncode == 0:
                        if result.stdout:
                            remote_dir = result.stdout.strip()
                            if remote_dir:
                                destination = remote_dir
                                logger.info(f"Remote current directory: {destination}")
                            else:
                                logger.warning("pwd file was empty")
                        else:
                            logger.warning("pwd file read succeeded but stdout is empty")
                    else:
                        logger.warning(f"Failed to read pwd file: returncode={result.returncode}, stderr={result.stderr}")
                except Exception as e:
                    logger.error(f"Failed to get remote current directory: {e}", exc_info=True)
            
            # Fallback to home directory if we couldn't get current directory
            if not destination:
                destination = "~"
                logger.warning("Could not determine remote current directory, using home directory (~)")
            
            # Initiate SCP upload
            logger.info(f"Initiating SCP upload for {len(file_paths)} file(s) to {destination}")
            root._start_scp_transfer(
                self.connection,
                file_paths,
                destination,
                direction='upload'
            )
            
            return True
        except Exception as e:
            logger.error(f"Error handling file drop: {e}", exc_info=True)
            return False
    
    def has_active_job(self):
        """
        Check if the terminal has an active job running.
        Only works for local terminals.
        
        Returns:
            bool: True if job is running, False if idle or unknown.
                  For SSH terminals, always returns False.
        """
        if not self._is_local_terminal():
            logger.debug("Job detection not available for SSH terminals")
            return False
        return self._job_status == "RUNNING" or (self._job_status == "UNKNOWN" and not self._is_terminal_idle_pty())

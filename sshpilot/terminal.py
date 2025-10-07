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
import pwd
from datetime import datetime
from typing import Optional, List
from .port_utils import get_port_checker
from .platform_utils import is_macos

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
        
        # Set up the terminal
        self.vte = Vte.Terminal()
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

        # Initialize terminal with basic settings and apply configured theme early
        self.setup_terminal()
        try:
            self.apply_theme()
        except Exception:
            pass
        
        # Add terminal to scrolled window and to the box via an overlay with a connecting view
        self.scrolled_window.set_child(self.vte)
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

        self.search_close_button = Gtk.Button()
        self.search_close_button.set_icon_name('window-close-symbolic')
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
        self.search_prev_button.set_icon_name('go-up-symbolic')
        self.search_prev_button.set_tooltip_text(_("Find previous match"))
        self.search_prev_button.connect('clicked', self._on_search_previous)
        self.search_prev_button.set_sensitive(False)
        search_controls.append(self.search_prev_button)

        self.search_next_button = Gtk.Button()
        self.search_next_button.set_icon_name('go-down-symbolic')
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
        self.overlay.add_overlay(self.search_revealer)
        try:
            self.overlay.set_measure_overlay(self.search_revealer, True)
        except AttributeError:
            pass

        self.terminal_stack = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.terminal_stack.set_hexpand(True)
        self.terminal_stack.set_vexpand(True)
        self.terminal_stack.append(self.overlay)

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
        icon = Gtk.Image.new_from_icon_name('dialog-error-symbolic')
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
        self.vte.set_hexpand(True)
        self.vte.set_vexpand(True)

        # Connect terminal signals and store handler IDs for cleanup
        self._child_exited_handler = self.vte.connect('child-exited', self.on_child_exited)
        self._title_changed_handler = self.vte.connect('window-title-changed', self.on_title_changed)
        
        # Connect termprops-changed signal if available (VTE 0.78+)
        # This signal is used for job detection in local terminals
        self._termprops_changed_handler = None
        try:
            self._termprops_changed_handler = self.vte.connect('termprops-changed', self._on_termprops_changed)
            logger.debug("Connected termprops-changed signal (VTE 0.78+ feature)")
        except Exception as e:
            logger.debug(f"termprops-changed signal not available (older VTE version): {e}")
            # Job detection will be disabled for local terminals on older VTE versions
        
        # Apply theme
        self.force_style_refresh()
        
        # Set visibility of child widgets (GTK4 style)
        self.scrolled_window.set_visible(True)
        self.vte.set_visible(True)
        
        # Show overlay initially
        self._set_connecting_overlay_visible(True)
        logger.debug("Terminal widget initialized")

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
            # Reuse existing connect method
            if not self._connect_ssh():
                # Show banner again if failed to start reconnect
                self._set_connecting_overlay_visible(False)
                self._set_disconnected_banner_visible(True, _('Reconnect failed to start'))
        except Exception:
            self._set_connecting_overlay_visible(False)
            self._set_disconnected_banner_visible(True, _('Reconnect failed'))

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
            
        # Ensure VTE terminal is properly initialized
        if not hasattr(self, 'vte') or self.vte is None:
            logger.error("VTE terminal not initialized")
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
        """Set up terminal with direct SSH command (called from main thread)"""
        try:
            base_cmd = []
            using_prepared_cmd = False

            if hasattr(self.connection, 'ssh_cmd'):
                prepared = getattr(self.connection, 'ssh_cmd', None)
                if isinstance(prepared, (list, tuple)):
                    base_cmd = list(prepared)
                    using_prepared_cmd = len(base_cmd) > 0

            if not base_cmd:
                base_cmd = ['ssh']

            ssh_cmd = list(base_cmd)
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

            if native_mode_enabled and not ssh_cmd:
                host_label = ''
                try:
                    if hasattr(self.connection, 'resolve_host_identifier'):
                        host_label = self.connection.resolve_host_identifier()
                except Exception:
                    host_label = ''
                host_label = host_label or getattr(self.connection, 'host', '') or getattr(self.connection, 'hostname', '') or getattr(self.connection, 'nickname', '')
                if host_label:
                    ssh_cmd = ['ssh', host_label]
                else:
                    ssh_cmd = ['ssh']

            if not native_mode_enabled and not quick_connect_mode:
                host_candidates = set()
                if resolved_for_connection:
                    host_candidates.add(str(resolved_for_connection))
                else:
                    try:
                        resolved_for_connection = _resolve_host_for_connection()
                        if resolved_for_connection:
                            host_candidates.add(str(resolved_for_connection))
                    except Exception:
                        resolved_for_connection = ''

                for attr in ('hostname', 'host', 'nickname'):
                    value = getattr(self.connection, attr, '')
                    if value:
                        host_candidates.add(str(value))

                username_for_host = getattr(self.connection, 'username', '') or ''
                if username_for_host:
                    for value in list(host_candidates):
                        if value:
                            host_candidates.add(f"{username_for_host}@{value}")

                host_arg = None
                if using_prepared_cmd and ssh_cmd:
                    last_arg = ssh_cmd[-1]
                    if last_arg in host_candidates or (
                        last_arg and not str(last_arg).startswith('-') and ' ' not in str(last_arg)
                    ):
                        host_arg = ssh_cmd.pop()

                needs_host_append = not using_prepared_cmd
                if using_prepared_cmd and host_arg is None:
                    needs_host_append = False

                def ensure_option(option: str):
                    if option not in ssh_cmd:
                        ssh_cmd.extend(['-o', option])

                def ensure_flag(flag: str):
                    if flag not in ssh_cmd:
                        ssh_cmd.append(flag)

                # Read SSH behavior from config with sane defaults
                try:
                    ssh_cfg = self.config.get_ssh_config() if hasattr(self.config, 'get_ssh_config') else {}
                except Exception:
                    ssh_cfg = {}

                def _coerce_int(value, default=None):
                    try:
                        coerced = int(str(value))
                        if coerced <= 0:
                            return default
                        return coerced
                    except (TypeError, ValueError):
                        return default

                connect_timeout = _coerce_int(ssh_cfg.get('connection_timeout'), None)
                connection_attempts = _coerce_int(ssh_cfg.get('connection_attempts'), None)
                keepalive_interval = _coerce_int(ssh_cfg.get('keepalive_interval'), None)
                keepalive_count = _coerce_int(ssh_cfg.get('keepalive_count_max'), None)
                strict_host = str(ssh_cfg.get('strict_host_key_checking', '') or '').strip()
                auto_add_host_keys = bool(ssh_cfg.get('auto_add_host_keys', True))
                batch_mode = bool(ssh_cfg.get('batch_mode', False))
                compression = bool(ssh_cfg.get('compression', False))

                using_password = password_auth_selected or has_saved_password

                # Apply advanced args according to stored preferences
                # Only enable BatchMode when NOT doing password auth (BatchMode disables prompts)
                if batch_mode and not using_password:
                    ensure_option('BatchMode=yes')
                if connect_timeout is not None:
                    ensure_option(f'ConnectTimeout={connect_timeout}')
                if connection_attempts is not None:
                    ensure_option(f'ConnectionAttempts={connection_attempts}')
                if keepalive_interval is not None:
                    ensure_option(f'ServerAliveInterval={keepalive_interval}')
                if keepalive_count is not None:
                    ensure_option(f'ServerAliveCountMax={keepalive_count}')
                if strict_host:
                    ensure_option(f'StrictHostKeyChecking={strict_host}')
                if compression:
                    ensure_flag('-C')

                # Default to accepting new host keys non-interactively on fresh installs
                try:
                    if (not strict_host) and auto_add_host_keys:
                        ensure_option('StrictHostKeyChecking=accept-new')
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
                ensure_option('ExitOnForwardFailure=yes')

                # Only add verbose flag if explicitly enabled in config
                try:
                    ssh_cfg = self.config.get_ssh_config() if hasattr(self.config, 'get_ssh_config') else {}
                    verbosity = int(ssh_cfg.get('verbosity', 0))
                    debug_enabled = bool(ssh_cfg.get('debug_enabled', False))
                    v = max(0, min(3, verbosity))
                    existing_v = ssh_cmd.count('-v')
                    for _ in range(max(0, v - existing_v)):
                        ssh_cmd.append('-v')
                    # Map verbosity to LogLevel to ensure messages are not suppressed by defaults
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

                # If remote command is specified, request a TTY (twice for force allocation)
                if remote_cmd:
                    ssh_cmd.extend(['-t', '-t'])

                # If local command specified, allow and set it via options
                if local_cmd:
                    ensure_option('PermitLocalCommand=yes')
                    # Pass exactly as user provided, letting ssh parse quoting
                    ensure_option(f'LocalCommand={local_cmd}')

                # Add port forwarding rules with conflict checking
                if hasattr(self.connection, 'forwarding_rules'):
                    port_conflicts = []
                    port_checker = get_port_checker()

                    # Check for port conflicts before adding rules
                    for rule in self.connection.forwarding_rules:
                        if not rule.get('enabled', True):
                            continue

                        rule_type = rule.get('type')
                        listen_addr = rule.get('listen_addr', '127.0.0.1')
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

                        # Add the forwarding rule if no conflicts
                        if rule_type == 'dynamic' and listen_port:
                            try:
                                ssh_cmd.extend(['-D', f"{listen_addr}:{listen_port}"])
                                logger.debug(f"Added dynamic port forwarding: {listen_addr}:{listen_port}")
                            except Exception as e:
                                logger.error(f"Failed to set up dynamic forwarding: {e}")

                        elif rule_type == 'local' and listen_port and 'remote_host' in rule and 'remote_port' in rule:
                            try:
                                remote_host = rule.get('remote_host', 'localhost')
                                remote_port = rule.get('remote_port')
                                ssh_cmd.extend(['-L', f"{listen_addr}:{listen_port}:{remote_host}:{remote_port}"])
                                logger.debug(f"Added local port forwarding: {listen_addr}:{listen_port} -> {remote_host}:{remote_port}")
                            except Exception as e:
                                logger.error(f"Failed to set up local forwarding: {e}")

                        # Handle remote port forwarding (remote bind -> local destination)
                        elif rule_type == 'remote' and listen_port:
                            try:
                                local_host = rule.get('local_host') or rule.get('remote_host', 'localhost')
                                local_port = rule.get('local_port') or rule.get('remote_port')
                                if local_port:
                                    ssh_cmd.extend(['-R', f"{listen_addr}:{listen_port}:{local_host}:{local_port}"])
                                    logger.debug(f"Added remote port forwarding: {listen_addr}:{listen_port} -> {local_host}:{local_port}")
                            except Exception as e:
                                logger.error(f"Failed to set up remote forwarding: {e}")

                    # Show port conflict warnings if any
                    if port_conflicts:
                        conflict_message = "Port forwarding conflicts detected:\n" + "\n".join([f"• {msg}" for msg in port_conflicts])
                        logger.warning(conflict_message)
                        GLib.idle_add(self._show_forwarding_error_dialog, conflict_message)

                    # Add extra SSH config options from advanced tab
                    extra_ssh_config = getattr(self.connection, 'extra_ssh_config', '').strip()
                    if extra_ssh_config:
                        logger.debug(f"Adding extra SSH config options: {extra_ssh_config}")
                        # Parse and add each extra SSH config option
                        for line in extra_ssh_config.split('\n'):
                            line = line.strip()
                            if line and not line.startswith('#'):  # Skip empty lines and comments
                                # Split on first space to separate option and value
                                parts = line.split(' ', 1)
                                if len(parts) == 2:
                                    option, value = parts
                                    ssh_cmd.extend(['-o', f"{option}={value}"])
                                    logger.debug(f"Added SSH option: {option}={value}")
                                elif len(parts) == 1:
                                    # Option without value (e.g., "Compression yes" becomes "Compression=yes")
                                    option = parts[0]
                                    ssh_cmd.extend(['-o', f"{option}=yes"])
                                    logger.debug(f"Added SSH option: {option}=yes")

                    # Add NumberOfPasswordPrompts option before hostname and command
                    ensure_option('NumberOfPasswordPrompts=1')

                # Add port if not default (must be before host)
                if (
                    hasattr(self.connection, 'port')
                    and self.connection.port != 22
                    and '-p' not in ssh_cmd
                ):
                    ssh_cmd.extend(['-p', str(self.connection.port)])

                if host_arg is not None:
                    ssh_cmd.append(host_arg)
                elif needs_host_append:
                    host_for_cmd = ''
                    try:
                        if hasattr(self.connection, 'resolve_host_identifier'):
                            host_for_cmd = self.connection.resolve_host_identifier()
                    except Exception:
                        host_for_cmd = ''
                    if not host_for_cmd:
                        host_for_cmd = (
                            getattr(self.connection, 'hostname', '')
                            or getattr(self.connection, 'host', '')
                            or resolved_for_connection
                            or getattr(self.connection, 'nickname', '')
                        )
                    host_for_cmd = host_for_cmd or ''
                    if host_for_cmd:
                        user_for_cmd = getattr(self.connection, 'username', '') or ''
                        host_entry = f"{user_for_cmd}@{host_for_cmd}" if user_for_cmd else host_for_cmd
                        if host_entry and host_entry not in ssh_cmd:
                            ssh_cmd.append(host_entry)


                # Append remote command last so ssh treats it as the command to run, ensure shell remains active
                if remote_cmd:
                    final_remote_cmd = remote_cmd if 'exec $SHELL' in remote_cmd else f"{remote_cmd} ; exec $SHELL -l"
                    # Append as single argument; let shell on remote parse quotes. Keep as-is to allow user quoting.
                    ssh_cmd.append(final_remote_cmd)

                # Make sure ssh will prompt in our VTE if no saved password:
                if (password_auth_selected or auth_method == 0) and not has_saved_password:
                    if '-t' not in ssh_cmd and '-tt' not in ssh_cmd:
                        ssh_cmd.append('-t')  # force a TTY for interactive password

                # Log the SSH command
                try:
                    logger.debug(f"SSH command: {' '.join(ssh_cmd)}")
                except Exception:
                    logger.debug("Prepared SSH command")

            elif quick_connect_mode:
                try:
                    logger.debug(
                        "Using quick connect command for terminal: %s",
                        ' '.join(str(part) for part in ssh_cmd),
                    )
                except Exception:
                    logger.debug("Using quick connect command for terminal")
            elif native_mode_enabled:
                try:
                    logger.debug(
                        "Using native SSH command for terminal: %s",
                        ' '.join(str(part) for part in ssh_cmd),
                    )
                except Exception:
                    logger.debug("Using native SSH command for terminal")

            # Start the SSH process using VTE's spawn_async with our PTY
            logger.debug(f"Flatpak debug: About to spawn SSH with command: {ssh_cmd}")

            # Handle password authentication with sshpass if available
            env = os.environ.copy()
            logger.debug(f"Initial environment SSH_ASKPASS: {env.get('SSH_ASKPASS', 'NOT_SET')}, SSH_ASKPASS_REQUIRE: {env.get('SSH_ASKPASS_REQUIRE', 'NOT_SET')}")

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
            elif (password_auth_selected or auth_method == 0) and not has_saved_password:
                # Password may be required but none saved - allow interactive prompt
                logger.debug("No saved password - using interactive prompt if required")
            else:
                # Use askpass for passphrase prompts (key-based auth)
                from .askpass_utils import get_ssh_env_with_askpass
                askpass_env = get_ssh_env_with_askpass()
                env.update(askpass_env)
                self._enable_askpass_log_forwarding(include_existing=True)
            env['TERM'] = env.get('TERM', 'xterm-256color')
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
            
            # Create a new PTY for the terminal
            pty = Vte.Pty.new_sync(Vte.PtyFlags.DEFAULT)
            
            try:
                self.vte.spawn_async(
                    Vte.PtyFlags.DEFAULT,
                    os.path.expanduser('~') or '/',
                    ssh_cmd,
                    env_list,  # Use environment list for Flatpak
                    GLib.SpawnFlags.DEFAULT,
                    None,  # Child setup function
                    None,  # Child setup data
                    -1,    # Timeout (-1 = default)
                    None,  # Cancellable
                    self._on_spawn_complete,
                    ()     # User data - empty tuple for Flatpak VTE compatibility
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
            self.vte.grab_focus()

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

            # Try spawning again without askpass
            self.vte.spawn_async(
                Vte.PtyFlags.DEFAULT,
                os.path.expanduser('~') or '/',
                ssh_cmd,
                env_list,
                GLib.SpawnFlags.DEFAULT,
                None,
                None,
                -1,
                None,
                self._on_spawn_complete,
                ()
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

    def _on_spawn_complete(self, terminal, pid, error, user_data=None):
        """Called when terminal spawn is complete"""
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
            self.vte.grab_focus()
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

            cursor_color = Gdk.RGBA()
            cursor_color.parse(profile.get('cursor_color', profile['foreground']))

            highlight_bg = Gdk.RGBA()
            highlight_bg.parse(profile.get('highlight_background', '#4A90E2'))

            highlight_fg = Gdk.RGBA()
            highlight_fg.parse(profile.get('highlight_foreground', profile['foreground']))

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
                highlight_bg = self._clone_rgba(override_rgba)
                highlight_fg = self._get_contrast_color(highlight_bg)
                cursor_color = self._clone_rgba(highlight_fg)


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
            
            # Apply colors to terminal
            self.vte.set_colors(fg_color, bg_color, palette_colors)
            self.vte.set_color_cursor(cursor_color)
            self.vte.set_color_highlight(highlight_bg)
            self.vte.set_color_highlight_foreground(highlight_fg)

            # Also color the container background to prevent white flash before VTE paints
            try:
                rgba = bg_color
                # For Gtk4, setting the widget style via CSS provider
                provider = Gtk.CssProvider()
                css = f".terminal-bg {{ background-color: rgba({int(rgba.red*255)}, {int(rgba.green*255)}, {int(rgba.blue*255)}, {rgba.alpha}); }}"
                provider.load_from_data(css.encode('utf-8'))
                display = Gdk.Display.get_default()
                if display:
                    Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
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
            self.vte.set_font(font_desc)
            
            # Force a redraw
            self.vte.queue_draw()
            
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
            self.vte.set_font(font_desc)
            
            # Do not force a light default; theme will define colors
            self.apply_theme()
            
            # Set cursor properties
            self.vte.set_cursor_blink_mode(Vte.CursorBlinkMode.ON)
            self.vte.set_cursor_shape(Vte.CursorShape.BLOCK)
            
            # Set scrollback lines
            self.vte.set_scrollback_lines(10000)
            
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
            
            # Set cursor and selection colors
            try:
                cursor_color = Gdk.RGBA()
                cursor_color.parse('black')  # Black cursor
                
                if hasattr(self.vte, 'set_color_cursor'):
                    self.vte.set_color_cursor(cursor_color)
                    logger.debug("Set cursor color")
                
                # Set selection colors
                if hasattr(self.vte, 'set_color_highlight'):
                    highlight_bg = Gdk.RGBA()
                    highlight_bg.parse('#4A90E2')  # Light blue highlight
                    self.vte.set_color_highlight(highlight_bg)
                    logger.debug("Set highlight color")
                    
                    highlight_fg = Gdk.RGBA()
                    highlight_fg.parse('white')
                    if hasattr(self.vte, 'set_color_highlight_foreground'):
                        self.vte.set_color_highlight_foreground(highlight_fg)
                        logger.debug("Set highlight foreground color")
                        
            except Exception as e:
                logger.warning(f"Could not set terminal colors: {e}")
            
            # Enable mouse reporting if available
            if hasattr(self.vte, 'set_mouse_autohide'):
                self.vte.set_mouse_autohide(True)
                logger.debug("Enabled mouse autohide")
                
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
                
            logger.info("Terminal setup complete")
            
            # Enable bold text
            self.vte.set_allow_bold(True)
            
            # Show the terminal
            self.vte.show()
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
            self.vte.set_encoding(target)
            logger.debug("Set terminal encoding to %s", target)
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
            
            # Start a simple local shell - just like GNOME Terminal
            env = os.environ.copy()

            # Determine the user's preferred shell
            shell = env.get('SHELL') or pwd.getpwuid(os.getuid()).pw_shell or '/bin/bash'

            # Ensure we have a proper environment
            env['SHELL'] = shell
            if 'TERM' not in env:
                env['TERM'] = 'xterm-256color'

            # Set initial title for local terminal
            self.emit('title-changed', 'Local Terminal')

            # Convert environment dict to list for VTE compatibility
            env_list = []
            for key, value in env.items():
                env_list.append(f"{key}={value}")

            # Start the user's shell as a login shell
            self.vte.spawn_async(
                Vte.PtyFlags.DEFAULT,
                os.path.expanduser('~') or '/',
                [shell, '-l'],
                env_list,
                GLib.SpawnFlags.DEFAULT,
                None,
                None,
                -1,
                None,
                self._on_spawn_complete,
                ()
            )

            # Add fallback timer to hide spinner if spawn completion doesn't fire
            self._fallback_timer_id = GLib.timeout_add_seconds(5, self._fallback_hide_spinner)

            logger.info("Local shell terminal setup initiated")
            
        except Exception as e:
            logger.error(f"Failed to setup local shell: {e}")
            self.emit('connection-failed', str(e))

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
            # Set parent to the terminal widget
            self._menu_popover.set_parent(self.vte)

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
                    # Focus terminal first for reliable copy/paste
                    try:
                        self.vte.grab_focus()
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
            self.vte.add_controller(gesture)
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
                    if not self.vte.get_has_selection():
                        return False
                    return _schedule_vte_action(self.vte.copy_clipboard_format, Vte.Format.TEXT)

                def _cb_paste(widget, *args):
                    return _schedule_vte_action(self.vte.paste_clipboard)

                def _cb_select_all(widget, *args):
                    return _schedule_vte_action(self.vte.select_all)

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

                self.vte.add_controller(controller)
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
            self.vte.add_controller(scroll_controller)
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
            self.vte.add_controller(controller)
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
            pty = self.vte.get_pty()
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

        # Disconnect VTE signal handlers first to prevent callbacks on destroyed objects
        if hasattr(self, 'vte') and self.vte:
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
        """Handle terminal title change"""
        title = terminal.get_window_title()
        if title:
            self.emit('title-changed', title)
        # If terminal is connected and a title update occurs (often when prompt is ready),
        # ensure the reconnect banner is hidden
        try:
            if getattr(self, 'is_connected', False):
                self._set_disconnected_banner_visible(False)
        except Exception:
            pass

    def on_bell(self, terminal):
        """Handle terminal bell"""
        # Could implement visual bell or notification here
        pass

    def copy_text(self):
        """Copy selected text to clipboard"""
        if self.vte.get_has_selection():
            self.vte.copy_clipboard_format(Vte.Format.TEXT)

    def paste_text(self):
        """Paste text from clipboard"""
        self.vte.paste_clipboard()

    def select_all(self):
        """Select all text in terminal"""
        self.vte.select_all()

    def zoom_in(self):
        """Zoom in the terminal font"""
        try:
            current_scale = self.vte.get_font_scale()
            new_scale = min(current_scale + 0.1, 5.0)  # Max zoom 5x
            self.vte.set_font_scale(new_scale)
            logger.debug(f"Terminal zoomed in to {new_scale:.1f}x")
        except Exception as e:
            logger.error(f"Failed to zoom in terminal: {e}")

    def zoom_out(self):
        """Zoom out the terminal font"""
        try:
            current_scale = self.vte.get_font_scale()
            new_scale = max(current_scale - 0.1, 0.5)  # Min zoom 0.5x
            self.vte.set_font_scale(new_scale)
            logger.debug(f"Terminal zoomed out to {new_scale:.1f}x")
        except Exception as e:
            logger.error(f"Failed to zoom out terminal: {e}")

    def reset_zoom(self):
        """Reset terminal zoom to default (1.0x)"""
        try:
            self.vte.set_font_scale(1.0)
            logger.debug("Terminal zoom reset to 1.0x")
        except Exception as e:
            logger.error(f"Failed to reset terminal zoom: {e}")

    def reset_terminal(self):
        """Reset terminal"""
        self.vte.reset(True, True)

    def reset_and_clear(self):
        """Reset and clear terminal"""
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
                self.vte.grab_focus()
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
            self.vte.search_set_regex(None, 0)
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
                search_regex = Vte.Regex.new_for_search(pattern, -1, 0)
                self.vte.search_set_regex(search_regex, 0)
                self.vte.search_set_wrap_around(True)
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
            found = self.vte.search_find_next() if forward else self.vte.search_find_previous()
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
        """Handle terminal properties changes for job detection (local terminals only)"""
        # Only enable job detection for local terminals
        if not self._is_local_terminal():
            return
            
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
                
            pty = self.vte.get_pty()
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

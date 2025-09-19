"""
Main Window for sshPilot
Primary UI with connection list, tabs, and terminal management
"""

import copy
import os
import logging
import math
import re
import shlex
import time
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple


import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
try:
    gi.require_version('Vte', '3.91')
    from gi.repository import Vte
    _HAS_VTE = True
except Exception:
    _HAS_VTE = False

gi.require_version('PangoFT2', '1.0')
from gi.repository import Gtk, Adw, Gio, GLib, GObject, Gdk, Pango, PangoFT2
import subprocess
import threading

# Feature detection for libadwaita versions across distros
HAS_NAV_SPLIT = hasattr(Adw, 'NavigationSplitView')
HAS_OVERLAY_SPLIT = hasattr(Adw, 'OverlaySplitView')
HAS_TIMED_ANIMATION = hasattr(Adw, 'TimedAnimation')

from gettext import gettext as _

from .connection_manager import ConnectionManager, Connection
from .terminal import TerminalWidget
from .terminal_manager import TerminalManager
from .config import Config
from .key_manager import KeyManager, SSHKey
# Port forwarding UI is now integrated into connection_dialog.py
from .connection_dialog import ConnectionDialog
from .preferences import (
    PreferencesWindow,
    should_hide_external_terminal_options,
    should_hide_file_manager_options,
)
from .file_manager_integration import launch_remote_file_manager
from .sshcopyid_window import SshCopyIdWindow
from .groups import GroupManager
from .sidebar import GroupRow, ConnectionRow, build_sidebar

from .welcome_page import WelcomePage
from .actions import WindowActions, register_window_actions
from . import shutdown
from .search_utils import connection_matches
from .shortcut_utils import get_primary_modifier_label
from .platform_utils import is_macos, get_config_dir
from .ssh_utils import ensure_writable_ssh_home
from .scp_utils import assemble_scp_transfer_args

logger = logging.getLogger(__name__)


def _get_connection_host(connection) -> str:
    """Return the hostname for a connection object, falling back to legacy host attribute."""
    host = getattr(connection, 'hostname', None)
    if host:
        return str(host)
    legacy = getattr(connection, 'host', None)
    if legacy:
        return str(legacy)
    nickname = getattr(connection, 'nickname', '')
    return str(nickname or '')

class MainWindow(Adw.ApplicationWindow, WindowActions):
    """Main application window"""

    def __init__(self, *args, isolated: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.active_terminals = {}
        self.connections = []
        self._is_quitting = False  # Flag to prevent multiple quit attempts
        self._is_controlled_reconnect = False  # Flag to track controlled reconnection
        self._internal_file_manager_windows: List[Any] = []

        # Initialize managers
        self.config = Config()
        effective_isolated = isolated or bool(self.config.get_setting('ssh.use_isolated_config', False))
        key_dir = Path(get_config_dir()) if effective_isolated else None
        self.connection_manager = ConnectionManager(self.config, isolated_mode=effective_isolated)
        self.key_manager = KeyManager(key_dir)
        self.group_manager = GroupManager(self.config)
        
        # UI state
        self.active_terminals: Dict[Connection, TerminalWidget] = {}  # most recent terminal per connection
        self.connection_to_terminals: Dict[Connection, List[TerminalWidget]] = {}
        self.terminal_to_connection: Dict[TerminalWidget, Connection] = {}
        self.connection_rows = {}   # connection -> row_widget
        self._context_menu_row = None
        # Hide hosts toggle state
        try:
            self._hide_hosts = bool(self.config.get_setting('ui.hide_hosts', False))
        except Exception:
            self._hide_hosts = False
        
        # Set up window
        self.setup_window()
        self.setup_ui()
        self.setup_connections()
        self.setup_signals()

        # Terminal manager handles terminal-related operations
        self.terminal_manager = TerminalManager(self)
        
        # Add action for activating connections
        self.activate_action = Gio.SimpleAction.new('activate-connection', None)
        self.activate_action.connect('activate', self.on_activate_connection)
        self.add_action(self.activate_action)

        # Register remaining window actions
        register_window_actions(self)
        # (Toasts disabled) Remove any toast-related actions if previously defined
        try:
            if hasattr(self, '_toast_reconnect_action'):
                self.remove_action('toast-reconnect')
        except Exception:
            pass
        
        # Connect to close request signal
        self.connect('close-request', self.on_close_request)
        
        # Start with welcome view (tab view setup already shows welcome initially)
        
        logger.info("Main window initialized")

        # Install sidebar CSS
        try:
            self._install_sidebar_css()
        except Exception as e:
            logger.error(f"Failed to install sidebar CSS: {e}")

        # On startup, focus the first item in the connection list (not the toolbar buttons)
        # Delay this to ensure the UI is fully set up
        try:
            GLib.timeout_add(100, self._focus_connection_list_first_row)
        except Exception:
            pass
        
        # Check startup behavior setting and show appropriate view
        try:
            startup_behavior = self.config.get_setting('app-startup-behavior', 'terminal')
            if startup_behavior == 'terminal':
                # Show local terminal on startup
                GLib.idle_add(self.terminal_manager.show_local_terminal)
            # If startup_behavior == 'welcome', the welcome view is already shown by default
        except Exception as e:
            logger.error(f"Error handling startup behavior: {e}")
        
        # Mark startup as complete after a short delay to allow all initialization to finish
        GLib.timeout_add(500, lambda: setattr(self, '_startup_complete', True) or False)

    def _install_sidebar_css(self):
        """Install sidebar focus CSS"""
        try:
            # Install CSS for sidebar focus highlighting once per display
            display = Gdk.Display.get_default()
            if not display:
                logger.warning("No display available for CSS installation")
                return
            # Use an attribute on the display to avoid re-adding provider
            if getattr(display, '_sidebar_css_installed', False):
                return
            provider = Gtk.CssProvider()
            css = """
            /* Pulse highlight for selected rows */
            .pulse-highlight {
              background: alpha(@accent_bg_color, 0.5);
              border-radius: 8px;
              box-shadow: 0 0 0 0.5px alpha(@accent_bg_color, 0.28) inset;
              opacity: 0;
              transition: opacity 0.3s ease-in-out;
            }
            .pulse-highlight.on {
              opacity: 1;
            }

            /* optional: a subtle focus ring while the list is focused */
            row:selected:focus-within {
              /* box-shadow: 0 0 8px 2px @accent_bg_color inset; */
              /* border: 2px solid @accent_bg_color;  Adds a solid border of 2px thickness */
              border-radius: 8px;
            }
            
            /* Group styling */
            .group-expand-button {
              min-width: 16px;
              min-height: 16px;
              padding: 2px;
              border-radius: 4px;
            }
            
            .group-expand-button:hover {
              background: alpha(@accent_bg_color, 0.1);
            }
            
            /* Smooth drag indicator transitions */
            .drag-indicator {
              opacity: 0;
              transition: opacity 0.15s ease-in-out;
            }
            
            .drag-indicator.visible {
              opacity: 1;
            }
            
            /* Smooth transitions for connection rows during drag */
            .navigation-sidebar {
              transition: transform 0.1s ease-out, opacity 0.1s ease-out;
            }
            
            .navigation-sidebar.dragging {
              opacity: 0.7;
              transform: scale(0.98);
            }
            
            /* Group drop target highlight */
            .drop-target-group {
              background: alpha(@accent_bg_color, 0.25);
              border-radius: 8px;
              box-shadow: 0 0 0 2px @accent_bg_color inset,
                          0 2px 8px alpha(@accent_bg_color, 0.4);
              transform: scale(1.02);
              transition: all 0.2s ease-in-out;
              animation: group-drop-pulse 1.5s ease-in-out infinite;
            }
            
            @keyframes group-drop-pulse {
              0%, 100% { 
                box-shadow: 0 0 0 2px @accent_bg_color inset,
                           0 2px 8px alpha(@accent_bg_color, 0.4);
              }
              50% { 
                box-shadow: 0 0 0 3px @accent_bg_color inset,
                           0 4px 12px alpha(@accent_bg_color, 0.6);
              }
            }
            
            /* Drop target indicator styling */
            .drop-target-indicator {
              background: alpha(@accent_bg_color, 0.9);
              color: white;
              border-radius: 12px;
              padding: 4px 12px;
              margin: 4px 8px;
              font-weight: bold;
              font-size: 0.9em;
              animation: drop-indicator-bounce 0.6s ease-in-out;
            }
            
            @keyframes drop-indicator-bounce {
              0% { 
                transform: translateY(-10px) scale(0.8);
                opacity: 0;
              }
              60% {
                transform: translateY(2px) scale(1.05);
                opacity: 1;
              }
              100% {
                transform: translateY(0) scale(1);
                opacity: 1;
              }
            }

            """
            provider.load_from_data(css.encode('utf-8'))
            Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            setattr(display, '_sidebar_css_installed', True)
            logger.debug("Sidebar CSS installed successfully")
        except Exception as e:
            logger.error(f"Failed to install sidebar CSS: {e}")
            import traceback
            logger.debug(f"CSS installation traceback: {traceback.format_exc()}")

    def _toggle_class(self, widget, name, on):
        """Helper to toggle CSS class on a widget"""
        if on: 
            widget.add_css_class(name)
        else:
            widget.remove_css_class(name)



    def _select_only_row(self, row: Optional[Gtk.ListBoxRow]) -> None:
        """Select only the provided row, clearing any other selections."""
        if not row or not getattr(self, 'connection_list', None):
            return

        try:
            if hasattr(self.connection_list, 'unselect_all'):
                self.connection_list.unselect_all()
        except Exception:
            pass

        try:
            self.connection_list.select_row(row)
        except Exception:
            pass

    def _get_selected_connection_rows(self) -> List[Gtk.ListBoxRow]:
        """Return all selected rows that represent connections."""
        if not getattr(self, 'connection_list', None):
            return []

        try:
            selected_rows = list(self.connection_list.get_selected_rows())
        except Exception:
            selected_row = self.connection_list.get_selected_row()
            selected_rows = [selected_row] if selected_row else []

        return [row for row in selected_rows if hasattr(row, 'connection')]

    def _get_selected_group_rows(self) -> List[Gtk.ListBoxRow]:
        """Return all selected rows that represent groups."""
        if not getattr(self, 'connection_list', None):
            return []

        try:
            selected_rows = list(self.connection_list.get_selected_rows())
        except Exception:
            selected_row = self.connection_list.get_selected_row()
            selected_rows = [selected_row] if selected_row else []

        return [row for row in selected_rows if hasattr(row, 'group_id')]

    def _get_target_connection_rows(self, prefer_context: bool = False) -> List[Gtk.ListBoxRow]:
        """Return rows targeted by the current action, respecting context menus."""
        rows = self._get_selected_connection_rows()
        context_row = getattr(self, '_context_menu_row', None)

        if context_row and hasattr(context_row, 'connection'):
            if rows and context_row in rows:
                return rows
            if prefer_context or not rows:
                return [context_row]

        return rows

    def _connections_from_rows(self, rows: List[Gtk.ListBoxRow]) -> List[Connection]:
        """Return unique connections represented by the provided rows."""
        connections: List[Connection] = []
        seen_ids = set()
        for row in rows:
            connection = getattr(row, 'connection', None)
            if connection and id(connection) not in seen_ids:
                seen_ids.add(id(connection))
                connections.append(connection)
        return connections

    def _get_target_connections(self, prefer_context: bool = False) -> List[Connection]:
        """Return connection objects targeted by the current action."""
        rows = self._get_target_connection_rows(prefer_context=prefer_context)
        return self._connections_from_rows(rows)

    def _determine_neighbor_connection_row(
        self, target_rows: List[Gtk.ListBoxRow]
    ) -> Optional[Gtk.ListBoxRow]:
        """Find the closest remaining connection row after deleting target_rows."""
        if not target_rows or not getattr(self, 'connection_list', None):
            return None

        try:
            all_rows = list(self.connection_list)
        except Exception:
            # If iteration fails, fall back to default behavior.
            return None

        if not all_rows:
            return None

        index_map = {row: idx for idx, row in enumerate(all_rows)}
        target_indexes = sorted(
            index_map[row]
            for row in target_rows
            if row in index_map
        )

        if not target_indexes:
            return None

        max_index = target_indexes[-1]
        min_index = target_indexes[0]

        # Try to find the next connection row after the targeted range.
        for idx in range(max_index + 1, len(all_rows)):
            row = all_rows[idx]
            if hasattr(row, 'connection') and row not in target_rows:
                return row

        # Fall back to previous connection rows before the targeted range.
        for idx in range(min_index - 1, -1, -1):
            row = all_rows[idx]
            if hasattr(row, 'connection') and row not in target_rows:
                return row

        return None

    def _disconnect_connection_terminals(self, connection: Connection) -> None:
        """Disconnect all tracked terminals for a connection."""
        try:
            for term in list(self.connection_to_terminals.get(connection, [])):
                try:
                    if hasattr(term, 'disconnect'):
                        term.disconnect()
                except Exception:
                    pass

            term = self.active_terminals.get(connection)
            if term and hasattr(term, 'disconnect'):
                try:
                    term.disconnect()
                except Exception:
                    pass
        except Exception:
            pass

    def _prompt_delete_connections(
        self,
        connections: List[Connection],
        neighbor_row: Optional[Gtk.ListBoxRow] = None,
    ) -> None:
        """Show a confirmation dialog for deleting one or more connections."""
        unique_connections: List[Connection] = []
        seen_ids = set()
        for connection in connections:
            if connection and id(connection) not in seen_ids:
                seen_ids.add(id(connection))
                unique_connections.append(connection)

        if not unique_connections:
            return

        active_connections = [
            connection
            for connection in unique_connections
            if getattr(connection, 'is_connected', False)
            or bool(self.connection_to_terminals.get(connection, []))
        ]

        if active_connections:
            heading = _('Remove host?') if len(unique_connections) == 1 else _('Remove connections?')
            body = _('Close connections and remove host?') if len(unique_connections) == 1 else _(
                'Close connections and remove the selected hosts?'
            )
            dialog = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=heading,
                body=body,
            )
            dialog.add_response('cancel', _('Cancel'))
            dialog.add_response('close_remove', _('Close and Remove'))
            dialog.set_response_appearance('close_remove', Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response('close_remove')
            dialog.set_close_response('cancel')
        else:
            heading = _('Delete Connection?') if len(unique_connections) == 1 else _('Delete Connections?')
            if len(unique_connections) == 1:
                nickname = unique_connections[0].nickname if hasattr(unique_connections[0], 'nickname') else ''
                body = _('Are you sure you want to delete "{}"?').format(nickname)
            else:
                body = _('Are you sure you want to delete the selected connections?')

            dialog = Adw.MessageDialog.new(self, heading, body)
            dialog.add_response('cancel', _('Cancel'))
            dialog.add_response('delete', _('Delete'))
            dialog.set_response_appearance('delete', Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response('cancel')
            dialog.set_close_response('cancel')

        payload = {
            'connections': unique_connections,
            'neighbor_row': neighbor_row,
        }
        dialog.connect('response', self.on_delete_connection_response, payload)
        dialog.present()



    def pulse_selected_row(self, list_box: Gtk.ListBox, repeats=3, duration_ms=280):
        """Pulse the selected row with highlight effect"""
        row = list_box.get_selected_row() or (list_box.get_selected_rows()[0] if list_box.get_selected_rows() else None)
        if not row:
            return
        if not hasattr(row, "_pulse"):
            return
        # Ensure it's realized so opacity changes render
        if not row.get_mapped():
            row.realize()
        
        # Use CSS-based pulse for now
        pulse = row._pulse
        cycle_duration = max(300, duration_ms // repeats)  # Minimum 300ms per cycle for faster pulses
        
        def do_cycle(count):
            if count == 0:
                return False
            pulse.add_css_class("on")
            # Keep the pulse visible for a shorter time for snappier effect
            GLib.timeout_add(cycle_duration // 2, lambda: (
                pulse.remove_css_class("on"),
                # Add a shorter delay before the next pulse
                GLib.timeout_add(cycle_duration // 2, lambda: do_cycle(count - 1)) or True
            ) and False)
            return False

        GLib.idle_add(lambda: do_cycle(repeats))

    def _test_css_pulse(self, action, param):
        """Simple test to manually toggle CSS class"""
        row = self.connection_list.get_selected_row()
        if row and hasattr(row, "_pulse"):
            pulse = row._pulse
            pulse.add_css_class("on")
            GLib.timeout_add(1000, lambda: (
                pulse.remove_css_class("on")
            ) or False)

    def _setup_interaction_stop_pulse(self):
        """Set up event controllers to stop pulse effect on user interaction"""
        # Mouse click controller
        click_ctl = Gtk.GestureClick()
        click_ctl.connect("pressed", self._stop_pulse_on_interaction)
        self.connection_list.add_controller(click_ctl)
        
        # Key controller
        key_ctl = Gtk.EventControllerKey()
        key_ctl.connect("key-pressed", self._on_connection_list_key_pressed)
        self.connection_list.add_controller(key_ctl)
        
        # Scroll controller
        scroll_ctl = Gtk.EventControllerScroll()
        scroll_ctl.connect("scroll", self._stop_pulse_on_interaction)
        self.connection_list.add_controller(scroll_ctl)

    def _on_connection_list_key_pressed(self, controller, keyval, keycode, state):
        """Handle key presses in the connection list"""
        # Stop pulse effect on any key press
        self._stop_pulse_on_interaction(controller)
        
        # Handle Enter key specifically
        if keyval == Gdk.KEY_Return or keyval == Gdk.KEY_KP_Enter:
            selected_row = self.connection_list.get_selected_row()
            if selected_row and hasattr(selected_row, 'connection'):
                connection = selected_row.connection
                self._focus_most_recent_tab_or_open_new(connection)
                return True  # Consume the event to prevent row-activated
            return False  # Allow group rows to be handled by row-activated

        # Handle deletion keys to remove selected connections
        if keyval in (
            Gdk.KEY_Delete,
            Gdk.KEY_KP_Delete,
            Gdk.KEY_BackSpace,
        ):
            target_rows = self._get_target_connection_rows()
            connections = self._connections_from_rows(target_rows)

            if connections:
                neighbor_row = self._determine_neighbor_connection_row(target_rows)
                self._prompt_delete_connections(connections, neighbor_row)
                return True

            return False
        return False

    def _stop_pulse_on_interaction(self, controller, *args):
        """Stop any ongoing pulse effect when user interacts"""
        # Stop pulse on any row that has the 'on' class
        for row in self.connection_list:
            if hasattr(row, "_pulse"):
                pulse = row._pulse
                if "on" in pulse.get_css_classes():
                    pulse.remove_css_class("on")

    def _wire_pulses(self):
        """Wire pulse effects to trigger on focus-in only"""
        # Track if this is the initial startup focus
        self._is_initial_focus = True
        
        # When list gains keyboard focus (e.g., after Ctrl/⌘+L)
        focus_ctl = Gtk.EventControllerFocus()
        def on_focus_enter(*args):
            # Don't pulse on initial startup focus
            if self._is_initial_focus:
                self._is_initial_focus = False
                return
            self.pulse_selected_row(self.connection_list, repeats=1, duration_ms=600)
        focus_ctl.connect("enter", on_focus_enter)
        self.connection_list.add_controller(focus_ctl)
        
        # Stop pulse effect when user interacts with the list
        self._setup_interaction_stop_pulse()
        
        # Sidebar toggle action registered via register_window_actions

    def setup_window(self):
        """Configure main window properties"""
        self.set_title('sshPilot')
        self.set_icon_name('io.github.mfat.sshpilot')
        
        # Load window geometry
        geometry = self.config.get_window_geometry()
        self.set_default_size(geometry['width'], geometry['height'])
        self.set_resizable(True)
        
        # Connect window state signals
        self.connect('notify::default-width', self.on_window_size_changed)
        self.connect('notify::default-height', self.on_window_size_changed)
        # Ensure initial focus after the window is mapped
        try:
            self.connect('map', lambda *a: GLib.timeout_add(200, self._focus_connection_list_first_row))
        except Exception:
            pass

        # Global shortcuts for tab navigation: Alt+Right / Alt+Left
        try:
            nav = Gtk.ShortcutController()
            nav.set_scope(Gtk.ShortcutScope.GLOBAL)
            if hasattr(nav, 'set_propagation_phase'):
                nav.set_propagation_phase(Gtk.PropagationPhase.BUBBLE)

            def _cb_next(widget, *args):
                try:
                    self._select_tab_relative(1)
                except Exception:
                    pass
                return True

            def _cb_prev(widget, *args):
                try:
                    self._select_tab_relative(-1)
                except Exception:
                    pass
                return True

            nav.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string('<Alt>Right'),
                Gtk.CallbackAction.new(_cb_next)
            ))
            nav.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string('<Alt>Left'),
                Gtk.CallbackAction.new(_cb_prev)
            ))
            
            self.add_controller(nav)
        except Exception:
            pass
        
    def on_window_size_changed(self, window, param):
        """Handle window size changes and save the new dimensions"""
        width = self.get_default_width()
        height = self.get_default_height()
        logger.debug(f"Window size changed to: {width}x{height}")
        
        # Save the new window geometry
        self.config.set_window_geometry(width, height)

    def setup_ui(self):
        """Set up the user interface"""
        # Create main container
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        
        # Create header bar
        self.header_bar = Adw.HeaderBar()
        self.header_bar.set_title_widget(Gtk.Label(label="sshPilot"))
        
        # Add window controls (minimize, maximize, close)
        self.header_bar.set_show_start_title_buttons(True)
        self.header_bar.set_show_end_title_buttons(True)
        
        # Add sidebar toggle button to the left side of header bar
        self.sidebar_toggle_button = Gtk.ToggleButton()
        self.sidebar_toggle_button.set_can_focus(False)  # Remove focus from sidebar toggle
        
        # Sidebar always starts visible
        sidebar_visible = True
        
        self.sidebar_toggle_button.set_icon_name('sidebar-show-symbolic')
        self.sidebar_toggle_button.set_tooltip_text(
            f'Hide Sidebar (F9, {get_primary_modifier_label()}+B)'
        )
        # Button should not appear pressed when sidebar is visible
        self.sidebar_toggle_button.set_active(False)
        self.sidebar_toggle_button.connect('toggled', self.on_sidebar_toggle)
        self.header_bar.pack_start(self.sidebar_toggle_button)
        
        # Add view toggle button to switch between welcome and tabs
        self.view_toggle_button = Gtk.Button()
        self.view_toggle_button.set_icon_name('go-home-symbolic')
        self.view_toggle_button.set_tooltip_text('Show Start Page')
        self.view_toggle_button.connect('clicked', self.on_view_toggle_clicked)
        self.view_toggle_button.set_visible(False)  # Hidden by default
        self.header_bar.pack_start(self.view_toggle_button)
        
        # Add tab button to header bar (will be created later in setup_content_area)
        # This will be added after the tab view is created
        
        # Add header bar to main container only when using traditional split views
        if not (HAS_NAV_SPLIT or HAS_OVERLAY_SPLIT):
            main_box.append(self.header_bar)
        
        # Create main layout (fallback if split view widgets are unavailable)
        # Try OverlaySplitView first as it's more reliable
        if HAS_OVERLAY_SPLIT:
            self.split_view = Adw.OverlaySplitView()
            try:
                self.split_view.set_sidebar_width_fraction(0.25)
                self.split_view.set_min_sidebar_width(200)
                self.split_view.set_max_sidebar_width(400)
            except Exception:
                pass
            self.split_view.set_vexpand(True)
            self._split_variant = 'overlay'
            logger.debug("Using OverlaySplitView")
        elif HAS_NAV_SPLIT:
            self.split_view = Adw.NavigationSplitView()
            try:
                self.split_view.set_sidebar_width_fraction(0.25)
                self.split_view.set_min_sidebar_width(200)
                self.split_view.set_max_sidebar_width(400)
            except Exception:
                pass
            self.split_view.set_vexpand(True)
            self._split_variant = 'navigation'
            logger.debug("Using NavigationSplitView")
        else:
            self.split_view = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
            self.split_view.set_wide_handle(True)
            self.split_view.set_vexpand(True)
            self._split_variant = 'paned'
            logger.debug("Using Gtk.Paned fallback")
        
        # Sidebar always starts visible
        sidebar_visible = True
        
        # For OverlaySplitView, we need to explicitly show the sidebar
        if HAS_OVERLAY_SPLIT:
            try:
                self.split_view.set_show_sidebar(True)
                logger.debug("Set OverlaySplitView sidebar to visible")
            except Exception as e:
                logger.error(f"Failed to show OverlaySplitView sidebar: {e}")
        elif HAS_NAV_SPLIT:
            logger.debug("NavigationSplitView sidebar will be shown when content is set")
        
        # Create sidebar
        self.setup_sidebar()

        # Create main content area
        self.setup_content_area()

        # Add split view to main container
        main_box.append(self.split_view)

        # Sidebar is always visible on startup

        # Create toast overlay and set main content
        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(main_box)
        self.set_content(self.toast_overlay)

    def _set_sidebar_widget(self, widget: Gtk.Widget) -> None:
        if HAS_NAV_SPLIT or HAS_OVERLAY_SPLIT:
            try:
                self.split_view.set_sidebar(widget)
                return
            except Exception:
                pass
        # Fallback for Gtk.Paned
        try:
            self.split_view.set_start_child(widget)
        except Exception:
            pass

    def _set_content_widget(self, widget: Gtk.Widget) -> None:
        if HAS_NAV_SPLIT:
            try:
                if not hasattr(self, "_nav_view"):
                    self._nav_view = Adw.NavigationView()
                    self.split_view.set_content(self._nav_view)
                page = Adw.NavigationPage.new(widget, "")
                self._nav_view.push(page)
                return
            except Exception:
                pass
        elif HAS_OVERLAY_SPLIT:
            try:
                self.split_view.set_content(widget)
                return
            except Exception:
                pass
        # Fallback for Gtk.Paned
        try:
            self.split_view.set_end_child(widget)
        except Exception:
            pass

    def _get_sidebar_width(self) -> int:
        try:
            if (HAS_NAV_SPLIT or HAS_OVERLAY_SPLIT) and hasattr(self.split_view, 'get_max_sidebar_width'):
                return int(self.split_view.get_max_sidebar_width())
        except Exception:
            pass
        # Fallback: attempt to read allocation of the first child when using Paned
        try:
            sidebar = self.split_view.get_start_child()
            if sidebar is not None:
                alloc = sidebar.get_allocation()
                return int(alloc.width)
        except Exception:
            pass
        return 0


    
    def _generate_duplicate_nickname(self, base_nickname: str) -> str:
        """Generate a unique nickname for a duplicated connection."""
        try:
            existing_names = {
                str(getattr(conn, 'nickname', '')).strip()
                for conn in self.connection_manager.get_connections()
                if getattr(conn, 'nickname', None)
            }
        except Exception:
            existing_names = set()
        existing_lower = {name.lower() for name in existing_names if name}

        base = (base_nickname or '').strip()
        if not base:
            base = _('Connection')

        copy_label = _('Copy')
        pattern = re.compile(r"\s+\(" + re.escape(copy_label) + r"(?:\s+(\d+))?\)\s*$", re.IGNORECASE)
        base_clean = pattern.sub('', base).strip() or base

        def is_unique(name: str) -> bool:
            return name.lower() not in existing_lower

        candidate = f"{base_clean} ({copy_label})"
        if is_unique(candidate):
            return candidate

        index = 2
        while True:
            candidate = f"{base_clean} ({copy_label} {index})"
            if is_unique(candidate):
                return candidate
            index += 1

    def _show_duplicate_connection_error(self, connection: Optional[Connection], error: Exception) -> None:
        """Display an error dialog when duplication fails."""
        try:
            nickname = (getattr(connection, 'nickname', '') or _('Connection')).strip()
            heading = _('Duplicate Failed')
            body = _('Failed to duplicate connection "{name}".\n\n{details}').format(
                name=nickname,
                details=str(error) or _('An unknown error occurred.')
            )
            dialog = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=heading,
                body=body,
            )
            dialog.add_response('close', _('Close'))
            dialog.set_close_response('close')
            dialog.present()
        except Exception:
            pass

    def duplicate_connection(self, connection: Optional[Connection]) -> Optional[Connection]:
        """Duplicate an existing connection, persist it, and select the new entry."""
        if connection is None:
            return None

        try:
            try:
                base_data = getattr(connection, 'data', None)
                new_data = copy.deepcopy(base_data) if isinstance(base_data, dict) else {}
            except Exception:
                new_data = {}
            if not isinstance(new_data, dict):
                new_data = {}

            for key in list(new_data.keys()):
                if key.startswith('__') or key in {'aliases', 'password_changed'}:
                    new_data.pop(key, None)

            new_nickname = self._generate_duplicate_nickname(getattr(connection, 'nickname', ''))
            new_data['nickname'] = new_nickname

            host_value = (
                getattr(connection, 'hostname', '')
                or getattr(connection, 'host', '')
                or new_data.get('hostname', '')
                or new_data.get('host', '')
            )
            host_value = str(host_value).strip()
            if not host_value:
                host_value = new_nickname
            new_data['hostname'] = host_value
            new_data.pop('host', None)

            new_data['username'] = str(getattr(connection, 'username', new_data.get('username', '')) or '')

            try:
                new_data['port'] = int(getattr(connection, 'port', new_data.get('port', 22)) or 22)
            except Exception:
                new_data['port'] = 22

            try:
                new_data['auth_method'] = int(getattr(connection, 'auth_method', new_data.get('auth_method', 0)) or 0)
            except Exception:
                new_data['auth_method'] = 0

            keyfile_value = getattr(connection, 'keyfile', new_data.get('keyfile', '')) or ''
            if isinstance(keyfile_value, str) and keyfile_value.strip().lower().startswith('select key file'):
                keyfile_value = ''
            new_data['keyfile'] = keyfile_value

            certificate_value = getattr(connection, 'certificate', new_data.get('certificate', '')) or ''
            if isinstance(certificate_value, str) and certificate_value.strip().lower().startswith('select certificate'):
                certificate_value = ''
            new_data['certificate'] = certificate_value

            new_data['key_passphrase'] = getattr(connection, 'key_passphrase', new_data.get('key_passphrase', '')) or ''

            try:
                new_data['key_select_mode'] = int(getattr(connection, 'key_select_mode', new_data.get('key_select_mode', 0)) or 0)
            except Exception:
                new_data['key_select_mode'] = 0

            new_data['password'] = getattr(connection, 'password', new_data.get('password', '')) or ''
            new_data['x11_forwarding'] = bool(getattr(connection, 'x11_forwarding', new_data.get('x11_forwarding', False)))
            new_data['pubkey_auth_no'] = bool(getattr(connection, 'pubkey_auth_no', new_data.get('pubkey_auth_no', False)))
            new_data['forward_agent'] = bool(getattr(connection, 'forward_agent', new_data.get('forward_agent', False)))

            proxy_jump_value = getattr(connection, 'proxy_jump', new_data.get('proxy_jump', []))
            if isinstance(proxy_jump_value, str):
                proxy_jump_value = [h.strip() for h in re.split(r'[\s,]+', proxy_jump_value) if h.strip()]
            else:
                proxy_jump_value = list(proxy_jump_value or [])
            new_data['proxy_jump'] = proxy_jump_value

            new_data['proxy_command'] = getattr(connection, 'proxy_command', new_data.get('proxy_command', '')) or ''
            new_data['local_command'] = getattr(connection, 'local_command', new_data.get('local_command', '')) or ''
            new_data['remote_command'] = getattr(connection, 'remote_command', new_data.get('remote_command', '')) or ''
            new_data['extra_ssh_config'] = getattr(connection, 'extra_ssh_config', new_data.get('extra_ssh_config', '')) or ''

            forwarding_rules = getattr(connection, 'forwarding_rules', new_data.get('forwarding_rules', []))
            try:
                new_data['forwarding_rules'] = copy.deepcopy(list(forwarding_rules or []))
            except Exception:
                new_data['forwarding_rules'] = []

            source_path = getattr(connection, 'source', new_data.get('source'))
            if source_path:
                new_data['source'] = source_path
            else:
                new_data.pop('source', None)

            new_connection = Connection(new_data)
            try:
                new_connection.auth_method = int(new_data.get('auth_method', 0) or 0)
            except Exception:
                new_connection.auth_method = 0
            try:
                new_connection.key_select_mode = int(new_data.get('key_select_mode', 0) or 0)
            except Exception:
                new_connection.key_select_mode = 0
            new_connection.forwarding_rules = list(new_data.get('forwarding_rules', []))
            new_connection.proxy_jump = list(new_data.get('proxy_jump', []))
            new_connection.forward_agent = bool(new_data.get('forward_agent', False))
            new_connection.extra_ssh_config = new_data.get('extra_ssh_config', '')
            new_connection.certificate = new_data.get('certificate', '')

            original_group_id = self.group_manager.get_connection_group(connection.nickname)

            self.connection_manager.connections.append(new_connection)
            try:
                if not self.connection_manager.update_connection(new_connection, new_data):
                    raise RuntimeError(_('Failed to save duplicated connection.'))
            except Exception:
                try:
                    self.connection_manager.connections.remove(new_connection)
                except ValueError:
                    pass
                raise

            self.connection_manager.load_ssh_config()

            if original_group_id and original_group_id in getattr(self.group_manager, 'groups', {}):
                self.group_manager.move_connection(new_nickname, original_group_id)
                try:
                    self.group_manager.reorder_connection_in_group(new_nickname, connection.nickname, 'below')
                except Exception:
                    pass
            else:
                self.group_manager.move_connection(new_nickname, None)
                try:
                    root_connections = self.group_manager.root_connections
                    if new_nickname in root_connections and connection.nickname in root_connections:
                        root_connections.remove(new_nickname)
                        insert_at = root_connections.index(connection.nickname) + 1
                        root_connections.insert(insert_at, new_nickname)
                        self.group_manager._save_groups()
                except Exception:
                    pass

            self.rebuild_connection_list()

            duplicated = self.connection_manager.find_connection_by_nickname(new_nickname)
            if duplicated and duplicated in self.connection_rows:
                row = self.connection_rows[duplicated]
                self._select_only_row(row)
                try:
                    self.connection_list.scroll_to_row(row)
                except Exception:
                    pass
                try:
                    self.connection_list.grab_focus()
                except Exception:
                    pass
            return duplicated
        except Exception as error:
            self._show_duplicate_connection_error(connection, error)
            logger.error(f"Failed to duplicate connection: {error}", exc_info=True)
            return None

    def setup_sidebar(self):
        """Set up the sidebar with connection list"""
        sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar_box.add_css_class('sidebar')
        
        # Sidebar header
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.set_margin_start(12)
        header.set_margin_end(12)
        header.set_margin_top(12)
        header.set_margin_bottom(6)
        
        # # Title
        # title_label = Gtk.Label()
        # title_label.set_markup('<b>Connections</b>')
        # title_label.set_halign(Gtk.Align.START)
        # title_label.set_hexpand(True)
        # header.append(title_label)
        
        # Add connection button
        add_button = Gtk.Button.new_from_icon_name('list-add-symbolic')
        add_button.set_tooltip_text(
            f'Add Connection ({get_primary_modifier_label()}+N)'
        )
        add_button.connect('clicked', self.on_add_connection_clicked)
        try:
            add_button.set_can_focus(False)
        except Exception:
            pass
        header.append(add_button)

        # Search button
        search_button = Gtk.Button.new_from_icon_name('system-search-symbolic')
        # Platform-aware shortcut in tooltip
        shortcut = 'Cmd+F' if is_macos() else 'Ctrl+F'
        search_button.set_tooltip_text(f'Search Connections ({shortcut})')
        search_button.connect('clicked', lambda *_: self.focus_search_entry())
        try:
            search_button.set_can_focus(False)
        except Exception:
            pass
        header.append(search_button)

        # Hide/Show hostnames button (eye icon)
        def _update_eye_icon(btn):
            try:
                icon = 'view-conceal-symbolic' if self._hide_hosts else 'view-reveal-symbolic'
                btn.set_icon_name(icon)
                btn.set_tooltip_text('Show hostnames' if self._hide_hosts else 'Hide hostnames')
            except Exception:
                pass

        hide_button = Gtk.Button.new_from_icon_name('view-reveal-symbolic')
        _update_eye_icon(hide_button)
        def _on_toggle_hide(btn):
            try:
                self._hide_hosts = not self._hide_hosts
                # Persist setting
                try:
                    self.config.set_setting('ui.hide_hosts', self._hide_hosts)
                except Exception:
                    pass
                # Update all rows
                for row in self.connection_rows.values():
                    if hasattr(row, 'apply_hide_hosts'):
                        row.apply_hide_hosts(self._hide_hosts)
                # Update icon/tooltip
                _update_eye_icon(btn)
            except Exception:
                pass
        hide_button.connect('clicked', _on_toggle_hide)
        try:
            hide_button.set_can_focus(False)
        except Exception:
            pass
        header.append(hide_button)


        # Add spacer to push menu button to far right
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        header.append(spacer)

        # Menu button - positioned at the far right relative to sidebar
        menu_button = Gtk.MenuButton()
        menu_button.set_can_focus(False)
        menu_button.set_icon_name('open-menu-symbolic')
        menu_button.set_tooltip_text('Menu')
        menu_button.set_menu_model(self.create_menu())
        header.append(menu_button)

        sidebar_box.append(header)

        # Search container
        search_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        search_container.add_css_class('search-container')
        search_container.set_margin_start(2)
        search_container.set_margin_end(2)
        search_container.set_margin_bottom(6)
        
        # Search entry for filtering connections
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text(_('Search connections'))
        self.search_entry.connect('search-changed', self.on_search_changed)
        self.search_entry.connect('stop-search', self.on_search_stopped)
        search_key = Gtk.EventControllerKey()
        search_key.connect('key-pressed', self._on_search_entry_key_pressed)
        self.search_entry.add_controller(search_key)
        # Prevent search entry from being the default focus widget
        self.search_entry.set_can_focus(True)
        self.search_entry.set_focus_on_click(False)
        search_container.append(self.search_entry)
        
        # Store reference to search container for showing/hiding
        self.search_container = search_container
        
        # Hide search container by default
        search_container.set_visible(False)
        
        sidebar_box.append(search_container)

        # Connection list
        self.connection_scrolled = Gtk.ScrolledWindow()
        self.connection_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.connection_scrolled.set_vexpand(True)
        
        self.connection_list = Gtk.ListBox()
        self.connection_list.add_css_class("navigation-sidebar")
        self.connection_list.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        try:
            self.connection_list.set_can_focus(True)
        except Exception:
            pass
        
        # Wire pulse effects
        self._wire_pulses()
        
        # Connect signals
        self.connection_list.connect('row-selected', self.on_connection_selected)  # For button sensitivity
        self.connection_list.connect('row-activated', self.on_connection_activated)  # For Enter key/double-click
        
        # Make sure the connection list is focusable and can receive key events
        self.connection_list.set_focusable(True)
        self.connection_list.set_can_focus(True)
        self.connection_list.set_focus_on_click(True)
        self.connection_list.set_activate_on_single_click(False)  # Require double-click to activate
        
        # Set connection list as the default focus widget for the sidebar
        sidebar_box.set_focus_child(self.connection_list)
        
        # Set up drag and drop for reordering
        build_sidebar(self)

        # Right-click context menu using simple gesture without coordinate detection
        try:
            # Use a simple gesture but avoid all coordinate-based operations
            context_click = Gtk.GestureClick()
            context_click.set_button(Gdk.BUTTON_SECONDARY)  # Only handle right-click
            
            def _on_right_click(gesture, n_press, x, y):
                try:
                    logger.debug("Simple right-click detected - showing context menu for selected row")
                    
                    # Clear any existing pulse effects to prevent multiple highlights
                    self._stop_pulse_on_interaction(None)
                    
                    # Try to detect the clicked row, but fall back to selected row if detection fails
                    row = None
                    try:
                        # First try to find the row that was actually clicked using pick method
                        # This is safe now because we're not doing any selection operations
                        picked_widget = self.connection_list.pick(x, y, Gtk.PickFlags.DEFAULT)
                        widget = picked_widget
                        while widget is not None:
                            if isinstance(widget, Gtk.ListBoxRow):
                                row = widget
                                logger.debug("Using clicked row for context menu")
                                break
                            widget = widget.get_parent()
                            if widget == self.connection_list:
                                break
                    except Exception as e:
                        logger.debug(f"Failed to detect clicked row: {e}")
                    
                    # Fallback to selected row if click detection failed
                    if not row:
                        try:
                            row = self.connection_list.get_selected_row()
                            if row:
                                logger.debug("Using currently selected row for context menu (fallback)")
                            else:
                                # If no selection, use first row
                                first_visible = self.connection_list.get_row_at_index(0)
                                if first_visible:
                                    row = first_visible
                                    logger.debug("Using first row for context menu (no selection)")
                        except Exception as e:
                            logger.debug(f"Failed to get selected row: {e}")

                    if not row:
                        logger.debug("No row available for context menu")
                        return
                    
                    # Set context menu data
                    self._context_menu_row = row
                    self._context_menu_connection = getattr(row, 'connection', None)
                    self._context_menu_group_row = row if hasattr(row, 'group_id') else None
                    # Create popover menu and rely on default autohide behavior
                    pop = Gtk.Popover.new()
                    pop.set_has_arrow(True)
                    logger.debug("Created popover with default autohide")


                    # Create listbox for menu items
                    listbox = Gtk.ListBox(margin_top=2, margin_bottom=2, margin_start=2, margin_end=2)
                    listbox.set_selection_mode(Gtk.SelectionMode.NONE)
                    pop.set_child(listbox)
                    
                    # Simple popover close handler with cleanup
                    def _on_popover_closed(*args):
                        # Clean up the window focus handler when popover closes
                        if hasattr(pop, '_focus_handler_id') and hasattr(pop, '_window') and pop._window:
                            try:
                                pop._window.disconnect(pop._focus_handler_id)
                                logger.debug("Cleaned up window focus handler")
                            except Exception as e:
                                logger.debug(f"Error cleaning up focus handler: {e}")

                        logger.debug("Context menu closed")
                        try:
                            self._context_menu_row = None
                            self._context_menu_connection = None
                        except Exception:
                            pass
                    
                    pop.connect("closed", _on_popover_closed)
                    
                    # Close context menu when window becomes inactive (with delay to prevent immediate closure)
                    def _on_window_active_changed(window, pspec):
                        try:
                            # Add a small delay to avoid immediate closure when popover is first shown
                            def delayed_check():
                                try:
                                    # Only close if window is actually inactive and popover is still visible
                                    if not self.is_active() and pop and pop.get_visible():
                                        pop.popdown()
                                        logger.debug("Context menu closed due to window becoming inactive")
                                except Exception as e:
                                    logger.debug(f"Error in delayed focus check: {e}")
                                return False
                            GLib.timeout_add(50, delayed_check)
                        except Exception as e:
                            logger.debug(f"Error in window active change handler: {e}")
                    
                    # Connect to the window's notify::is-active signal after a brief delay
                    def connect_focus_handler():
                        try:
                            focus_handler_id = self.connect("notify::is-active", _on_window_active_changed)
                            pop._focus_handler_id = focus_handler_id
                            pop._window = self
                            logger.debug("Connected window focus handler")
                        except Exception as e:
                            logger.debug(f"Error connecting focus handler: {e}")
                        return False
                    
                    # Delay the connection slightly to avoid immediate triggering
                    GLib.timeout_add(100, connect_focus_handler)
                    
                    # Add menu items based on row type
                    if hasattr(row, 'group_id'):
                        # Group row context menu
                        logger.debug(f"Creating context menu for group row: {row.group_id}")

                        # Edit Group row
                        edit_row = Adw.ActionRow(title=_('Edit Group'))
                        edit_icon = Gtk.Image.new_from_icon_name('document-edit-symbolic')
                        edit_row.add_prefix(edit_icon)
                        edit_row.set_activatable(True)
                        edit_row.connect('activated', lambda *_: (self.on_edit_group_action(None, None), pop.popdown()))
                        listbox.append(edit_row)

                        # Delete Group row
                        delete_row = Adw.ActionRow(title=_('Delete Group'))
                        delete_icon = Gtk.Image.new_from_icon_name('user-trash-symbolic')
                        delete_row.add_prefix(delete_icon)
                        delete_row.set_activatable(True)
                        delete_row.connect('activated', lambda *_: (self.on_delete_group_action(None, None), pop.popdown()))
                        listbox.append(delete_row)
                    else:
                        # Connection row context menu
                        logger.debug(f"Creating context menu for connection row: {getattr(row, 'connection', None)}")

                        # Open New Connection row
                        new_row = Adw.ActionRow(title=_('Open New Connection'))
                        new_icon = Gtk.Image.new_from_icon_name('list-add-symbolic')
                        new_row.add_prefix(new_icon)
                        new_row.set_activatable(True)
                        new_row.connect('activated', lambda *_: (self.on_open_new_connection_action(None, None), pop.popdown()))
                        listbox.append(new_row)

                        # Edit Connection row
                        edit_row = Adw.ActionRow(title=_('Edit Connection'))
                        edit_icon = Gtk.Image.new_from_icon_name('document-edit-symbolic')
                        edit_row.add_prefix(edit_icon)
                        edit_row.set_activatable(True)
                        
                        edit_row.connect('activated', lambda *_: (self.on_edit_connection_action(None, None), pop.popdown()))
                        listbox.append(edit_row)

                        # Duplicate Connection row
                        duplicate_row = Adw.ActionRow(title=_('Duplicate Connection'))
                        duplicate_icon = Gtk.Image.new_from_icon_name('edit-copy-symbolic')
                        duplicate_row.add_prefix(duplicate_icon)
                        duplicate_row.set_activatable(True)
                        duplicate_row.connect('activated', lambda *_: (self.on_duplicate_connection_action(None, None), pop.popdown()))
                        listbox.append(duplicate_row)

                        # Manage Files row
                        if not should_hide_file_manager_options():
                            files_row = Adw.ActionRow(title=_('Manage Files'))
                            files_icon = Gtk.Image.new_from_icon_name('folder-symbolic')
                            files_row.add_prefix(files_icon)
                            files_row.set_activatable(True)
                            files_row.connect('activated', lambda *_: (self.on_manage_files_action(None, None), pop.popdown()))
                            listbox.append(files_row)

                        # Only show system terminal option when external terminals are available
                        if not should_hide_external_terminal_options():
                            terminal_row = Adw.ActionRow(title=_('Open in System Terminal'))
                            terminal_icon = Gtk.Image.new_from_icon_name('utilities-terminal-symbolic')
                            terminal_row.add_prefix(terminal_icon)
                            terminal_row.set_activatable(True)
                            terminal_row.connect('activated', lambda *_: (self.on_open_in_system_terminal_action(None, None), pop.popdown()))
                            listbox.append(terminal_row)

                        # Add grouping options
                        current_group_id = self.group_manager.get_connection_group(row.connection.nickname)
                        
                        # Always show "Move to Group" option
                        move_row = Adw.ActionRow(title=_('Move to Group'))
                        move_icon = Gtk.Image.new_from_icon_name('folder-symbolic')
                        move_row.add_prefix(move_icon)
                        move_row.set_activatable(True)
                        move_row.connect('activated', lambda *_: (self.on_move_to_group_action(None, None), pop.popdown()))
                        listbox.append(move_row)
                        
                        # Show "Ungroup" option if connection is currently in a group
                        if current_group_id:
                            ungroup_row = Adw.ActionRow(title=_('Ungroup'))
                            ungroup_icon = Gtk.Image.new_from_icon_name('folder-symbolic')
                            ungroup_row.add_prefix(ungroup_icon)
                            ungroup_row.set_activatable(True)
                            ungroup_row.connect('activated', lambda *_: (self.on_move_to_ungrouped_action(None, None), pop.popdown()))
                            listbox.append(ungroup_row)

                        # Delete Connection row (moved to bottom)
                        delete_row = Adw.ActionRow(title=_('Delete'))
                        delete_icon = Gtk.Image.new_from_icon_name('user-trash-symbolic')
                        delete_row.add_prefix(delete_icon)
                        delete_row.set_activatable(True)
                        delete_row.connect('activated', lambda *_: (self.on_delete_connection_action(None, None), pop.popdown()))
                        listbox.append(delete_row)
                    # Set popover parent to the selected row for proper anchoring
                    pop.set_parent(row)
                    
                    # Add a small delay to ensure proper display
                    def show_menu():
                        try:
                            pop.popup()
                            logger.debug("Context menu popup called")
                        except Exception as e:
                            logger.error(f"Failed to popup context menu: {e}")
                        return False
                    
                    GLib.idle_add(show_menu)
                    
                except Exception as e:
                    logger.error(f"Failed to create context menu: {e}")
            
            context_click.connect('pressed', _on_right_click)
            self.connection_list.add_controller(context_click)
        except Exception:
            pass
        
        # Add keyboard controller for Ctrl/⌘+Enter to open new connection
        try:
            key_controller = Gtk.ShortcutController()
            key_controller.set_scope(Gtk.ShortcutScope.LOCAL)
            
            def _on_ctrl_enter(widget, *args):
                try:
                    selected_row = self.connection_list.get_selected_row()
                    if selected_row and hasattr(selected_row, 'connection'):
                        connection = selected_row.connection
                        self.terminal_manager.connect_to_host(connection, force_new=True)
                except Exception as e:
                    logger.error(
                        f"Failed to open new connection with {get_primary_modifier_label()}+Enter: {e}"
                    )
                return True
            
            trigger = '<Meta>Return' if is_macos() else '<Primary>Return'
            
            key_controller.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string(trigger),
                Gtk.CallbackAction.new(_on_ctrl_enter)
            ))
            
            self.connection_list.add_controller(key_controller)
        except Exception as e:
            logger.debug(
                f"Failed to add {get_primary_modifier_label()}+Enter shortcut: {e}"
            )
        
        self.connection_scrolled.set_child(self.connection_list)
        sidebar_box.append(self.connection_scrolled)
        
        # Sidebar toolbar
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar.set_margin_start(6)
        toolbar.set_margin_end(6)
        toolbar.set_margin_top(6)
        toolbar.set_margin_bottom(6)
        toolbar.add_css_class('toolbar')
        try:
            # Expose the computed visual height so terminal banners can match
            min_h, nat_h, min_baseline, nat_baseline = toolbar.measure(Gtk.Orientation.VERTICAL, -1)
            self._toolbar_row_height = max(min_h, nat_h)
            # Also track the real allocated height dynamically
            def _on_toolbar_alloc(widget, allocation):
                try:
                    self._toolbar_row_height = allocation.height
                except Exception:
                    pass
            toolbar.connect('size-allocate', _on_toolbar_alloc)
        except Exception:
            self._toolbar_row_height = 36
        
        # Connection toolbar buttons
        self.connection_toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        
        # Edit button
        self.edit_button = Gtk.Button.new_from_icon_name('document-edit-symbolic')
        self.edit_button.set_tooltip_text('Edit Connection')
        self.edit_button.set_sensitive(False)
        self.edit_button.connect('clicked', self.on_edit_connection_clicked)
        self.connection_toolbar.append(self.edit_button)

        # Copy key to server button (ssh-copy-id)
        self.copy_key_button = Gtk.Button.new_from_icon_name('dialog-password-symbolic')
        self.copy_key_button.set_tooltip_text(
            f'Copy public key to server for passwordless login ({get_primary_modifier_label()}+Shift+K)'
        )
        self.copy_key_button.set_sensitive(False)
        self.copy_key_button.connect('clicked', self.on_copy_key_to_server_clicked)
        self.connection_toolbar.append(self.copy_key_button)

        # SCP transfer button
        self.scp_button = Gtk.Button.new_from_icon_name('document-send-symbolic')
        self.scp_button.set_tooltip_text('Transfer files with scp')
        self.scp_button.set_sensitive(False)
        self.scp_button.connect('clicked', self.on_scp_button_clicked)
        self.connection_toolbar.append(self.scp_button)

        # Manage files button (visibility controlled dynamically)
        self.manage_files_button = Gtk.Button.new_from_icon_name('folder-symbolic')
        self.manage_files_button.set_tooltip_text('Open file manager for remote server')
        self.manage_files_button.set_sensitive(False)
        self.manage_files_button.connect('clicked', self.on_manage_files_button_clicked)
        self.manage_files_button.set_visible(not should_hide_file_manager_options())
        self.connection_toolbar.append(self.manage_files_button)
        
        # System terminal button (only when external terminals are available)
        if not should_hide_external_terminal_options():
            self.system_terminal_button = Gtk.Button.new_from_icon_name('utilities-terminal-symbolic')
            self.system_terminal_button.set_tooltip_text('Open connection in system terminal')
            self.system_terminal_button.set_sensitive(False)
            self.system_terminal_button.connect('clicked', self.on_system_terminal_button_clicked)
            self.connection_toolbar.append(self.system_terminal_button)
        
        # Delete button
        self.delete_button = Gtk.Button.new_from_icon_name('user-trash-symbolic')
        self.delete_button.set_tooltip_text('Delete Connection')
        self.delete_button.set_sensitive(False)
        self.delete_button.connect('clicked', self.on_delete_connection_clicked)
        self.connection_toolbar.append(self.delete_button)
        
        # Group toolbar buttons
        self.group_toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        
        # Rename group button
        self.rename_group_button = Gtk.Button.new_from_icon_name('document-edit-symbolic')
        self.rename_group_button.set_tooltip_text('Rename Group')
        self.rename_group_button.set_sensitive(False)
        self.rename_group_button.connect('clicked', self.on_rename_group_clicked)
        self.group_toolbar.append(self.rename_group_button)
        
        # Delete group button
        self.delete_group_button = Gtk.Button.new_from_icon_name('user-trash-symbolic')
        self.delete_group_button.set_tooltip_text('Delete Group')
        self.delete_group_button.set_sensitive(False)
        self.delete_group_button.connect('clicked', self.on_delete_group_clicked)
        self.group_toolbar.append(self.delete_group_button)
        
        # Add both toolbars to main toolbar
        toolbar.append(self.connection_toolbar)
        toolbar.append(self.group_toolbar)
        
        # Spacer
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        toolbar.append(spacer)
        
        sidebar_box.append(toolbar)

        self._set_sidebar_widget(sidebar_box)
        logger.debug("Set sidebar widget")

    def _resolve_connection_list_event(
        self,
        x: float,
        y: float,
        scrolled_window: Optional[Gtk.ScrolledWindow] = None,
    ) -> Tuple[Optional[Gtk.ListBoxRow], float, float]:
        """Resolve the target row and viewport coordinates for a pointer event on the connection list."""

        try:
            event_x = float(x)
            event_y = float(y)
        except (TypeError, ValueError):
            return None, 0.0, 0.0

        adjusted_x = event_x
        adjusted_y = event_y
        hadjust_value = 0.0
        vadjust_value = 0.0


        if scrolled_window is None:
            try:
                scrolled_window = self.connection_list.get_ancestor(Gtk.ScrolledWindow)
            except Exception:
                scrolled_window = None

        if scrolled_window is not None:
            try:
                hadjustment = scrolled_window.get_hadjustment()
            except Exception:
                hadjustment = None
            else:
                if hadjustment is not None:
                    try:
                        hadjust_value = float(hadjustment.get_value())
                    except Exception:
                        hadjust_value = 0.0
                    else:
                        adjusted_x = event_x + hadjust_value


            try:
                vadjustment = scrolled_window.get_vadjustment()
            except Exception:
                vadjustment = None
            else:
                if vadjustment is not None:
                    try:
                        vadjust_value = float(vadjustment.get_value())
                    except Exception:
                        vadjust_value = 0.0
                    else:
                        adjusted_y = event_y + vadjust_value


        x_candidates: List[float] = [adjusted_x]
        if not math.isclose(adjusted_x, event_x):
            x_candidates.append(event_x)

        y_candidates: List[float] = [adjusted_y]
        if not math.isclose(adjusted_y, event_y):
            y_candidates.append(event_y)

        row: Optional[Gtk.ListBoxRow] = None
        pointer_y_source_index = 0
        for idx, candidate in enumerate(y_candidates):

            try:
                row = self.connection_list.get_row_at_y(int(candidate))
            except Exception:
                row = None
            if row:
                pointer_y_source_index = idx
                break
            row = self._connection_row_for_coordinate(candidate)
            if row:
                pointer_y_source_index = idx

                break

        if not row:
            return None, x_candidates[0], y_candidates[0]

        pointer_x_list = x_candidates[0]
        pointer_y_list = y_candidates[pointer_y_source_index]

        pointer_x_viewport = event_x
        pointer_y_viewport = event_y

        try:
            allocation = row.get_allocation()
        except Exception:
            allocation = None

        if allocation is not None:
            try:
                row_left = float(allocation.x)
                row_top = float(allocation.y)
                row_right = row_left + max(float(allocation.width) - 1.0, 0.0)
                row_bottom = row_top + max(float(allocation.height) - 1.0, 0.0)
            except Exception:
                row_left = row_top = 0.0
                row_right = row_bottom = 0.0


            if row_right < row_left:
                row_right = row_left
            if row_bottom < row_top:
                row_bottom = row_top

            row_left_viewport = row_left - hadjust_value
            row_right_viewport = row_right - hadjust_value
            row_top_viewport = row_top - vadjust_value
            row_bottom_viewport = row_bottom - vadjust_value

            if row_left_viewport > row_right_viewport:
                row_left_viewport, row_right_viewport = row_right_viewport, row_left_viewport
            if row_top_viewport > row_bottom_viewport:
                row_top_viewport, row_bottom_viewport = row_bottom_viewport, row_top_viewport

            pointer_x_candidates: List[float] = [pointer_x_viewport]
            pointer_x_from_list = pointer_x_list - hadjust_value
            if not math.isclose(pointer_x_from_list, pointer_x_viewport):
                pointer_x_candidates.append(pointer_x_from_list)
            event_x_minus_adjust = event_x - hadjust_value
            if hadjust_value and not math.isclose(event_x_minus_adjust, pointer_x_from_list):
                pointer_x_candidates.append(event_x_minus_adjust)

            for candidate in pointer_x_candidates:
                if row_left_viewport <= candidate <= row_right_viewport:
                    pointer_x_viewport = candidate
                    break
            else:
                midpoint_x = row_left_viewport + (row_right_viewport - row_left_viewport) / 2.0
                if row_left_viewport <= row_right_viewport:
                    pointer_x_viewport = max(
                        row_left_viewport, min(pointer_x_viewport, row_right_viewport)
                    )
                else:
                    pointer_x_viewport = midpoint_x

            pointer_y_candidates: List[float] = [pointer_y_viewport]
            pointer_y_from_list = pointer_y_list - vadjust_value
            if not math.isclose(pointer_y_from_list, pointer_y_viewport):
                pointer_y_candidates.append(pointer_y_from_list)
            event_y_minus_adjust = event_y - vadjust_value
            if vadjust_value and not math.isclose(event_y_minus_adjust, pointer_y_from_list):
                pointer_y_candidates.append(event_y_minus_adjust)

            for candidate in pointer_y_candidates:
                if row_top_viewport <= candidate <= row_bottom_viewport:
                    pointer_y_viewport = candidate
                    break
            else:
                midpoint_y = row_top_viewport + (row_bottom_viewport - row_top_viewport) / 2.0
                if row_top_viewport <= row_bottom_viewport:
                    pointer_y_viewport = max(
                        row_top_viewport, min(pointer_y_viewport, row_bottom_viewport)
                    )
                else:
                    pointer_y_viewport = midpoint_y

        return row, pointer_x_viewport, pointer_y_viewport


    def _connection_row_for_coordinate(self, coord: float) -> Optional[Gtk.ListBoxRow]:
        """Return the listbox row whose allocation includes the given list-space coordinate."""
        try:
            target = float(coord)
        except (TypeError, ValueError):
            return None

        try:
            child = self.connection_list.get_first_child()
        except Exception:
            return None

        while child is not None:
            try:
                if isinstance(child, Gtk.ListBoxRow):
                    allocation = child.get_allocation()
                    row_top = allocation.y
                    row_bottom = allocation.y + max(allocation.height - 1, 0)
                    if row_bottom < row_top:
                        row_bottom = row_top
                    if row_top <= target <= row_bottom:
                        return child
            except Exception:
                pass
            try:
                child = child.get_next_sibling()
            except Exception:
                break

        return None

    def setup_content_area(self):
        """Set up the main content area with stack for tabs and welcome view"""
        # Create stack to switch between welcome view and tab view
        self.content_stack = Gtk.Stack()
        self.content_stack.set_hexpand(True)
        self.content_stack.set_vexpand(True)
        
        # Create welcome/help view
        self.welcome_view = WelcomePage(self)
        self.content_stack.add_named(self.welcome_view, "welcome")
        
        # Create tab view
        self.tab_view = Adw.TabView()
        self.tab_view.set_hexpand(True)
        self.tab_view.set_vexpand(True)

        # Connect tab signals
        self.tab_view.connect('close-page', self.on_tab_close)
        self.tab_view.connect('page-attached', self.on_tab_attached)
        self.tab_view.connect('page-detached', self.on_tab_detached)
        # Track selected tab to keep row selection in sync
        self.tab_view.connect('notify::selected-page', self.on_tab_selected)

        # Whenever the window layout changes, propagate toolbar height to
        # any TerminalWidget so the reconnect banner exactly matches.
        try:
            # Capture the toolbar variable from this scope for measurement
            local_toolbar = locals().get('toolbar', None)
            def _sync_banner_heights(*args):
                try:
                    # Re-measure toolbar height in case style/theme changed
                    try:
                        if local_toolbar is not None:
                            min_h, nat_h, min_baseline, nat_baseline = local_toolbar.measure(Gtk.Orientation.VERTICAL, -1)
                            self._toolbar_row_height = max(min_h, nat_h)
                    except Exception:
                        pass
                    # Push exact allocated height to all terminal widgets (+5px)
                    for terms in self.connection_to_terminals.values():
                        for term in terms:
                            if hasattr(term, 'set_banner_height'):
                                term.set_banner_height(getattr(self, '_toolbar_row_height', 37) + 55)
                except Exception:
                    pass
            # Call once after UI is built and again after a short delay
            def _push_now():
                try:
                    height = getattr(self, '_toolbar_row_height', 36)
                    for terms in self.connection_to_terminals.values():
                        for term in terms:
                            if hasattr(term, 'set_banner_height'):
                                term.set_banner_height(height + 55)
                except Exception:
                    pass
                return False
            GLib.idle_add(_sync_banner_heights)
            GLib.timeout_add(200, _sync_banner_heights)
            GLib.idle_add(_push_now)
        except Exception:
            pass
        
        # Create tab overview
        self.tab_overview = Adw.TabOverview()
        self.tab_overview.set_view(self.tab_view)
        self.tab_overview.set_enable_new_tab(False)
        self.tab_overview.set_enable_search(True)
        # Hide window buttons in tab overview
        self.tab_overview.set_show_start_title_buttons(False)
        self.tab_overview.set_show_end_title_buttons(False)
        
        # Create tab bar
        self.tab_bar = Adw.TabBar()
        self.tab_bar.set_view(self.tab_view)
        self.tab_bar.set_autohide(True)
        
        # Add local terminal button before the tabs
        self.local_terminal_button = Gtk.Button()
        self.local_terminal_button.set_icon_name('tab-new-symbolic')
        self.local_terminal_button.add_css_class('flat')  # Make button flat
        
        # Set tooltip with keyboard shortcut
        mac = is_macos()
        primary = '⌘' if mac else 'Ctrl'
        shift = '⇧' if mac else 'Shift'
        shortcut_text = f'{primary}+{shift}+T'
        self.local_terminal_button.set_tooltip_text(_('Open Local Terminal ({})').format(shortcut_text))
        
        self.local_terminal_button.connect('clicked', self.on_local_terminal_button_clicked)
        self.tab_bar.set_start_action_widget(self.local_terminal_button)
        
        # Create tab content box
        tab_content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        tab_content_box.append(self.tab_bar)
        tab_content_box.append(self.tab_view)
        # Ensure background matches terminal theme to avoid white flash
        if hasattr(tab_content_box, 'add_css_class'):
            tab_content_box.add_css_class('terminal-bg')
        
        # Set the tab content box as the child of the tab overview
        self.tab_overview.set_child(tab_content_box)
        
        # Create and add tab button to header bar
        self.tab_button = Adw.TabButton()
        self.tab_button.set_view(self.tab_view)
        self.tab_button.connect('clicked', self.on_tab_button_clicked)
        self.tab_button.set_visible(False)  # Hidden by default, shown when tabs exist
        self.header_bar.pack_start(self.tab_button)
        
        self.content_stack.add_named(self.tab_overview, "tabs")
        # Also color the stack background
        if hasattr(self.content_stack, 'add_css_class'):
            self.content_stack.add_css_class('terminal-bg')
        
        # Start with welcome view visible
        self.content_stack.set_visible_child_name("welcome")

        if HAS_OVERLAY_SPLIT:
            content_box = Adw.ToolbarView()
            content_box.add_top_bar(self.header_bar)
            content_box.set_content(self.content_stack)
            self._set_content_widget(content_box)
            logger.debug("Set content widget for OverlaySplitView")
        elif HAS_NAV_SPLIT:
            content_box = Adw.ToolbarView()
            content_box.add_top_bar(self.header_bar)
            content_box.set_content(self.content_stack)
            self._set_content_widget(content_box)
            logger.debug("Set content widget for NavigationSplitView")
        else:
            self._set_content_widget(self.content_stack)
            logger.debug("Set content widget for other split view types")



    def create_menu(self):
        """Create application menu"""
        menu = Gio.Menu()
        
        # Add all menu items directly to the main menu
        menu.append('New Connection', 'app.new-connection')
        menu.append('Create Group', 'win.create-group')
        menu.append('Local Terminal', 'app.local-terminal')
        menu.append('Copy Key to Server', 'app.new-key')
        menu.append('SSH Config Editor', 'app.edit-ssh-config')
        menu.append('Known Hosts Editor', 'win.edit-known-hosts')
        menu.append('Broadcast Command', 'app.broadcast-command')
        menu.append('Preferences', 'app.preferences')

        # Help submenu with platform-aware keyboard shortcuts overlay
        help_menu = Gio.Menu()
        help_menu.append('Keyboard Shortcuts', 'app.shortcuts')
        help_menu.append('Documentation', 'app.help')
        menu.append_submenu('Help', help_menu)

        menu.append('About', 'app.about')
        menu.append('Quit', 'app.quit')

        return menu

    def setup_connections(self):
        """Load and display existing connections with grouping"""
        self.rebuild_connection_list()
        
        # Select first connection if available
        connections = self.connection_manager.get_connections()
        if connections:
            first_row = self.connection_list.get_row_at_index(0)
            if first_row:
                self._select_only_row(first_row)
                # Defer focus to the list to ensure keyboard navigation works immediately
                GLib.idle_add(self._focus_connection_list_first_row)
    
    def rebuild_connection_list(self):
        """Rebuild the connection list with groups"""
        # Save current scroll position
        scroll_position = None
        if hasattr(self, 'connection_scrolled') and self.connection_scrolled:
            vadj = self.connection_scrolled.get_vadjustment()
            if vadj:
                scroll_position = vadj.get_value()
        
        # Clear existing rows
        child = self.connection_list.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.connection_list.remove(child)
            child = next_child
        self.connection_rows.clear()
        
        # Get all connections
        connections = self.connection_manager.get_connections()
        search_text = ''
        if hasattr(self, 'search_entry') and self.search_entry:
            search_text = self.search_entry.get_text().strip().lower()

        if search_text:
            matches = [c for c in connections if connection_matches(c, search_text)]
            for conn in sorted(matches, key=lambda c: c.nickname.lower()):
                self.add_connection_row(conn)
            self._ungrouped_area_row = None
            # Restore scroll position
            if scroll_position is not None and hasattr(self, 'connection_scrolled') and self.connection_scrolled:
                vadj = self.connection_scrolled.get_vadjustment()
                if vadj:
                    GLib.idle_add(lambda: vadj.set_value(scroll_position))
            return

        connections_dict = {conn.nickname: conn for conn in connections}

        # Get group hierarchy
        hierarchy = self.group_manager.get_group_hierarchy()

        # Build the list with groups
        self._build_grouped_list(hierarchy, connections_dict, 0)

        # Add ungrouped connections at the end
        ungrouped_nicks = [
            conn.nickname for conn in connections
            if not self.group_manager.get_connection_group(conn.nickname)
        ]

        if ungrouped_nicks:
            # Keep root connection order in sync
            updated = False
            for nick in ungrouped_nicks:
                if nick not in self.group_manager.root_connections:
                    self.group_manager.root_connections.append(nick)
                    updated = True

            existing = set(ungrouped_nicks)
            if any(nick not in existing for nick in self.group_manager.root_connections):
                self.group_manager.root_connections = [
                    nick for nick in self.group_manager.root_connections
                    if nick in existing
                ]
                updated = True

            if updated:
                self.group_manager._save_groups()

            for nick in self.group_manager.root_connections:
                conn = connections_dict.get(nick)
                if conn:
                    self.add_connection_row(conn)


        # Store reference to ungrouped area (hidden by default)
        self._ungrouped_area_row = None
        
        # Restore scroll position
        if scroll_position is not None and hasattr(self, 'connection_scrolled') and self.connection_scrolled:
            vadj = self.connection_scrolled.get_vadjustment()
            if vadj:
                GLib.idle_add(lambda: vadj.set_value(scroll_position))
    def _build_grouped_list(self, hierarchy, connections_dict, level):
        """Recursively build the grouped connection list"""
        for group_info in hierarchy:
            # Add group row
            group_row = GroupRow(group_info, self.group_manager, connections_dict)
            group_row.connect('group-toggled', self._on_group_toggled)
            self.connection_list.append(group_row)
            
            # Add connections in this group if expanded
            if group_info.get('expanded', True):
                group_connections = []
                for conn_nickname in group_info.get('connections', []):
                    if conn_nickname in connections_dict:
                        group_connections.append(connections_dict[conn_nickname])
                
                # Use the order from the group's connections list (preserves custom ordering)
                for conn_nickname in group_info.get('connections', []):
                    if conn_nickname in connections_dict:
                        conn = connections_dict[conn_nickname]
                        self.add_connection_row(conn, level + 1)
            
            # Recursively add child groups
            if group_info.get('children'):
                self._build_grouped_list(group_info['children'], connections_dict, level + 1)
    
    def _on_group_toggled(self, group_row, group_id, expanded):
        """Handle group expand/collapse"""
        self.rebuild_connection_list()

        # Reselect the toggled group so focus doesn't jump to another row
        for row in self.connection_list:
            if hasattr(row, "group_id") and row.group_id == group_id:
                self._select_only_row(row)
                break
    
    def add_connection_row(self, connection: Connection, indent_level: int = 0):
        """Add a connection row to the list with optional indentation"""
        row = ConnectionRow(connection)
        
        # Apply indentation for grouped connections
        if indent_level > 0:
            row.set_indentation(indent_level)
        
        self.connection_list.append(row)
        self.connection_rows[connection] = row
        
        # Apply current hide-hosts setting to new row
        if hasattr(row, 'apply_hide_hosts'):
            row.apply_hide_hosts(getattr(self, '_hide_hosts', False))

    def on_search_changed(self, entry):
        """Handle search text changes and update connection list."""
        self.rebuild_connection_list()
        first_row = self.connection_list.get_row_at_index(0)
        if first_row:
            self._select_only_row(first_row)

    def on_search_stopped(self, entry):
        """Handle search stop (Esc key)."""
        entry.set_text('')
        self.rebuild_connection_list()
        # Hide the search container
        if hasattr(self, 'search_container') and self.search_container:
            self.search_container.set_visible(False)
        # Return focus to connection list
        if hasattr(self, 'connection_list') and self.connection_list:
            self.connection_list.grab_focus()

    def _on_search_entry_key_pressed(self, controller, keyval, keycode, state):
        """Handle key presses in search entry."""
        if keyval == Gdk.KEY_Down:
            # Move focus to connection list
            if hasattr(self, 'connection_list') and self.connection_list:
                first_row = self.connection_list.get_row_at_index(0)
                if first_row:
                    self._select_only_row(first_row)
                self.connection_list.grab_focus()
            return True
        elif keyval == Gdk.KEY_Return:
            # If there's search text, move to first result
            if hasattr(self, 'search_entry') and self.search_entry:
                search_text = self.search_entry.get_text().strip()
                if search_text:
                    first_row = self.connection_list.get_row_at_index(0)
                    if first_row:
                        self._select_only_row(first_row)
                        self.connection_list.grab_focus()
                    return True
                else:
                    # No search text, just move to connection list
                    if hasattr(self, 'connection_list') and self.connection_list:
                        first_row = self.connection_list.get_row_at_index(0)
                        if first_row:
                            self._select_only_row(first_row)
                        self.connection_list.grab_focus()
                    return True
        return False

    def setup_signals(self):
        """Connect to manager signals"""
        # Connection manager signals - use connect_after to avoid conflict with GObject.connect
        self.connection_manager.connect_after('connection-added', self.on_connection_added)
        self.connection_manager.connect_after('connection-removed', self.on_connection_removed)
        self.connection_manager.connect_after('connection-status-changed', self.on_connection_status_changed)
        
        # Config signals
        self.config.connect('setting-changed', self.on_setting_changed)



    def show_welcome_view(self):
        """Show the welcome/help view when no connections are active"""
        # Remove terminal background styling so welcome uses app theme colors
        if hasattr(self.content_stack, 'remove_css_class'):
            try:
                self.content_stack.remove_css_class('terminal-bg')
            except Exception:
                pass
        # Ensure welcome fills the pane
        if hasattr(self, 'welcome_view'):
            try:
                self.welcome_view.set_hexpand(True)
                self.welcome_view.set_vexpand(True)
            except Exception:
                pass
        self.content_stack.set_visible_child_name("welcome")
        GLib.idle_add(self._focus_connection_list_first_row)

        # Update view toggle button
        if hasattr(self, 'view_toggle_button'):
            # Check if there are any active tabs
            has_tabs = len(self.tab_view.get_pages()) > 0
            if has_tabs:
                self.view_toggle_button.set_icon_name('go-home-symbolic')
                self.view_toggle_button.set_tooltip_text('Hide Start Page')
                self.view_toggle_button.set_visible(True)
            else:
                self.view_toggle_button.set_visible(False)  # Hide button when no tabs
        
        logger.info("Showing welcome view")

    def _focus_connection_list_first_row(self):
        """Focus the connection list and ensure the first row is selected (startup only)."""
        try:
            if not hasattr(self, 'connection_list') or self.connection_list is None:
                return False
            
            # Check if the connection list is properly attached to its parent
            if not self.connection_list.get_parent():
                return False
                
            # Only auto-select first row during initial startup, not during normal operations
            # Check if this is being called during startup vs normal operations
            if not hasattr(self, '_startup_complete'):
                # During startup - select first row if no selection exists
                try:
                    selected_rows = list(self.connection_list.get_selected_rows())
                except Exception:
                    selected_row = self.connection_list.get_selected_row()
                    selected_rows = [selected_row] if selected_row else []
                first_row = self.connection_list.get_row_at_index(0)
                if not selected_rows and first_row:
                    self._select_only_row(first_row)
            
            # Always focus the connection list when requested
            if self.connection_list.get_parent():
                self.connection_list.grab_focus()
        except Exception as e:
            logger.debug(f"Focus connection list failed: {e}")
            pass
        return False

    def focus_connection_list(self):
        """Focus the connection list and show a toast notification."""
        try:
            if hasattr(self, 'connection_list') and self.connection_list:
                # If sidebar is hidden, show it first
                if hasattr(self, 'sidebar_toggle_button') and self.sidebar_toggle_button:
                    if self.sidebar_toggle_button.get_active():
                        self.sidebar_toggle_button.set_active(False)
                
                # Ensure a row is selected before focusing
                try:
                    selected_rows = list(self.connection_list.get_selected_rows())
                except Exception:
                    selected_row = self.connection_list.get_selected_row()
                    selected_rows = [selected_row] if selected_row else []
                logger.debug(f"Focus connection list - current selection count: {len(selected_rows)}")
                if not selected_rows:
                    # Select the first row regardless of type
                    first_row = self.connection_list.get_row_at_index(0)
                    logger.debug(f"Focus connection list - first row: {first_row}")
                    if first_row:
                        self._select_only_row(first_row)
                        logger.debug(f"Focus connection list - selected first row: {first_row}")
                
                self.connection_list.grab_focus()
                
                # Pulse the selected row
                self.pulse_selected_row(self.connection_list, repeats=1, duration_ms=600)
                
                # Show toast notification
                toast = Adw.Toast.new(
                    f"Switched to connection list — ↑/↓ navigate, Enter open, {get_primary_modifier_label()}+Enter new tab"
                )
                toast.set_timeout(3)  # seconds
                if hasattr(self, 'toast_overlay'):
                    self.toast_overlay.add_toast(toast)
        except Exception as e:
            logger.error(f"Error focusing connection list: {e}")

    def focus_search_entry(self):
        """Toggle search on/off and show appropriate toast notification."""
        try:
            if hasattr(self, 'search_entry') and self.search_entry:
                # If sidebar is hidden, show it first
                if hasattr(self, 'sidebar_toggle_button') and self.sidebar_toggle_button:
                    if self.sidebar_toggle_button.get_active():
                        self.sidebar_toggle_button.set_active(False)
                
                # Toggle search container visibility
                if hasattr(self, 'search_container') and self.search_container:
                    is_visible = self.search_container.get_visible()
                    self.search_container.set_visible(not is_visible)
                    
                    if not is_visible:
                        # Search was hidden, now showing it
                        # Focus the search entry
                        self.search_entry.grab_focus()
                        
                        # Select all text if there's any
                        text = self.search_entry.get_text()
                        if text:
                            self.search_entry.select_region(0, len(text))
                        
                        # Show toast notification
                        toast = Adw.Toast.new(
                            "Search mode — Type to filter connections, Esc to clear and hide"
                        )
                        toast.set_timeout(3)  # seconds
                        if hasattr(self, 'toast_overlay'):
                            self.toast_overlay.add_toast(toast)
                    else:
                        # Search was visible, now hiding it
                        # Clear search text
                        self.search_entry.set_text('')
                        self.rebuild_connection_list()
                        
                        # Return focus to connection list
                        if hasattr(self, 'connection_list') and self.connection_list:
                            self.connection_list.grab_focus()
                        
                        # Show toast notification
                        toast = Adw.Toast.new(
                            f"Search hidden — {get_primary_modifier_label()}+F to search again"
                        )
                        toast.set_timeout(2)  # seconds
                        if hasattr(self, 'toast_overlay'):
                            self.toast_overlay.add_toast(toast)
        except Exception as e:
            logger.error(f"Failed to toggle search entry: {e}")
    
    def show_tab_view(self):
        """Show the tab view when connections are active"""
        # Re-apply terminal background when switching back to tabs
        if hasattr(self.content_stack, 'add_css_class'):
            try:
                self.content_stack.add_css_class('terminal-bg')
            except Exception:
                pass
        self.content_stack.set_visible_child_name("tabs")
        
        # Update view toggle button
        if hasattr(self, 'view_toggle_button'):
            self.view_toggle_button.set_icon_name('go-home-symbolic')
            self.view_toggle_button.set_tooltip_text('Show Start Page')
            self.view_toggle_button.set_visible(True)  # Show button when tabs are active
        
        logger.info("Showing tab view")

    def show_connection_dialog(
            self,
            connection: Connection = None,
            *,
            skip_group_warning: bool = False,
            force_split_from_group: bool = False,
            split_group_source: Optional[str] = None,
            split_original_nickname: Optional[str] = None,
    ):
        """Show connection dialog for adding/editing connections"""
        logger.info(f"Show connection dialog for: {connection}")

        # Refresh connection from disk to ensure latest auth method
        if connection is not None:
            try:
                self.connection_manager.load_ssh_config()
                refreshed = self.connection_manager.find_connection_by_nickname(connection.nickname)
                if refreshed:
                    connection = refreshed
            except Exception:
                pass

        if connection is not None and not skip_group_warning:
            block_info = None
            try:
                source_path = split_group_source or getattr(connection, 'source', None)
                block_info = self.connection_manager.get_host_block_details(connection.nickname, source_path)
            except Exception as e:
                logger.debug(f"Failed to inspect host block for {connection.nickname}: {e}")
            if block_info and len(block_info.get('hosts') or []) > 1:
                self._prompt_group_edit_options(connection, block_info)
                return

        split_source_for_dialog = split_group_source or (getattr(connection, 'source', None) if connection else None)
        original_token = split_original_nickname or (connection.nickname if connection else None)

        # Create connection dialog
        dialog = ConnectionDialog(
            self,
            connection,
            self.connection_manager,
            force_split_from_group=force_split_from_group,
            split_group_source=split_source_for_dialog,
            split_original_nickname=original_token,
        )
        dialog.connect('connection-saved', self.on_connection_saved)
        dialog.present()



    def _prompt_group_edit_options(self, connection: Connection, block_info: Dict[str, Any]):
        """Present options when editing a grouped host"""
        try:
            host_label = getattr(connection, 'nickname', '')
            other_hosts = max(0, len(block_info.get('hosts') or []) - 1)
            message = _("\"{host}\" is part of a configuration block with [{count}] other hosts. How would you like to apply your changes?").format(host=host_label, count=other_hosts)

            dialog = Adw.MessageDialog.new(self, _("Warning"), message)
            dialog.add_response('cancel', _('Cancel'))
            dialog.add_response('manual', _('Manually Edit SSH Configuration'))
            dialog.add_response('split', _('Edit as Separate Connection'))
            dialog.set_response_appearance('manual', Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response('manual')
            dialog.set_close_response('cancel')

            source_path = block_info.get('source') or getattr(connection, 'source', None)
            original_name = getattr(connection, 'nickname', None)

            def on_response(dlg, response):
                dlg.destroy()
                if response == 'manual':
                    self._open_ssh_config_editor()
                elif response == 'split':
                    self.show_connection_dialog(
                        connection,
                        skip_group_warning=True,
                        force_split_from_group=True,
                        split_group_source=source_path,
                        split_original_nickname=original_name,
                    )

            dialog.connect('response', on_response)
            dialog.present()
        except Exception as e:
            logger.error(f"Failed to present group edit options: {e}")
            self.show_connection_dialog(connection, skip_group_warning=True)

    def _open_ssh_config_editor(self):
        try:
            from .ssh_config_editor import SSHConfigEditorWindow
            editor = SSHConfigEditorWindow(self, self.connection_manager, on_saved=self._on_ssh_config_editor_saved)
            editor.present()
        except Exception as e:
            logger.error(f"Failed to open SSH config editor: {e}")

    def _on_ssh_config_editor_saved(self):
        try:
            self.connection_manager.load_ssh_config()
            self.rebuild_connection_list()
        except Exception as e:
            logger.error(f"Failed to refresh connections after SSH config save: {e}")

    def show_connection_selection_for_ssh_copy(self):
        """Show a dialog to select a connection for SSH key copy"""
        logger.info("Showing connection selection dialog for SSH key copy")
        
        # Get all connections
        connections = self.connection_manager.get_connections()
        if not connections:
            # No connections available, show new connection dialog instead
            logger.info("No connections available, showing new connection dialog")
            self.show_connection_dialog()
            return
        
        # Create a simple selection dialog
        dialog = Adw.MessageDialog(
            transient_for=self,
            modal=True,
            heading=_("Select Server for SSH Key Copy"),
            body=_("Choose a server to copy your SSH key to:")
        )
        
        # Add a list box with connections
        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        
        for connection in connections:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            
            # Connection info
            info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            name_label = Gtk.Label(label=connection.nickname)
            name_label.set_halign(Gtk.Align.START)
            name_label.set_css_classes(['title-4'])
            
            host_value = _get_connection_host(connection)
            host_label = Gtk.Label(label=f"{connection.username}@{host_value}:{connection.port}")
            host_label.set_halign(Gtk.Align.START)
            host_label.set_css_classes(['dim-label'])
            
            info_box.append(name_label)
            info_box.append(host_label)
            box.append(info_box)
            
            row.set_child(box)
            row.connection = connection
            list_box.append(row)
        
        # Add the list box to the dialog
        dialog.set_extra_child(list_box)
        
        # Add response buttons
        dialog.add_response('cancel', _('Cancel'))
        dialog.add_response('new', _('New Connection'))
        dialog.add_response('select', _('Select'))
        dialog.set_response_appearance('new', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_response_appearance('select', Adw.ResponseAppearance.DEFAULT)
        
        def on_response(dialog, response):
            if response == 'new':
                # Show new connection dialog
                self.show_connection_dialog()
            elif response == 'select':
                # Get selected connection and proceed with SSH key copy
                selected_row = list_box.get_selected_row()
                if selected_row and hasattr(selected_row, 'connection'):
                    connection = selected_row.connection
                    logger.info(f"Selected connection for SSH key copy: {connection.nickname}")
                    try:
                        from .sshcopyid_window import SshCopyIdWindow
                        win = SshCopyIdWindow(self, connection, self.key_manager, self.connection_manager)
                        win.present()
                    except Exception as e:
                        logger.error(f"Failed to show SSH key copy dialog: {e}")
            dialog.destroy()
        
        dialog.connect('response', on_response)
        dialog.present()

    # --- Helpers (use your existing ones if already present) ---------------------

    def _error_dialog(self, heading: str, body: str, detail: str = ""):
        try:
            msg = Adw.MessageDialog(transient_for=self, modal=True,
                                    heading=heading, body=(body + (f"\n\n{detail}" if detail else "")))
            msg.add_response("ok", _("OK"))
            msg.set_default_response("ok")
            msg.set_close_response("ok")
            msg.present()
        except Exception:
            pass

    def _info_dialog(self, heading: str, body: str):
        try:
            msg = Adw.MessageDialog(transient_for=self, modal=True,
                                    heading=heading, body=body)
            msg.add_response("ok", _("OK"))
            msg.set_default_response("ok")
            msg.set_close_response("ok")
            msg.present()
        except Exception:
            pass


    # --- Single, simplified key generator (no copy-to-server inside) ------------

    def show_key_dialog(self, on_success=None):
        """
        Single key generation dialog (Adw). Optional passphrase.
        No copy-to-server in this dialog. If provided, `on_success(key)` is called.
        """
        try:
            dlg = Adw.Dialog.new()
            dlg.set_title(_("Generate SSH Key"))

            tv = Adw.ToolbarView()
            hb = Adw.HeaderBar()
            hb.set_title_widget(Gtk.Label(label=_("New SSH Key")))
            tv.add_top_bar(hb)

            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            content.set_margin_top(18); content.set_margin_bottom(18)
            content.set_margin_start(18); content.set_margin_end(18)
            content.set_size_request(500, -1)

            form = Adw.PreferencesGroup()

            name_row = Adw.EntryRow()
            name_row.set_title(_("Key file name"))
            name_row.set_text("id_ed25519")
            
            # Add real-time validation
            def on_name_changed(entry):
                key_name = (entry.get_text() or "").strip()
                if key_name and not key_name.startswith(".") and "/" not in key_name:
                    key_path = self.key_manager.ssh_dir / key_name
                    if key_path.exists():
                        entry.add_css_class("error")
                        entry.set_title(_("Key file name (already exists)"))
                    else:
                        entry.remove_css_class("error")
                        entry.set_title(_("Key file name"))
                else:
                    entry.remove_css_class("error")
                    entry.set_title(_("Key file name"))
            
            name_row.connect("changed", on_name_changed)
            form.add(name_row)

            type_row = Adw.ComboRow()
            type_row.set_title(_("Key type"))
            types = Gtk.StringList.new(["ed25519", "rsa"])
            type_row.set_model(types)
            type_row.set_selected(0)
            form.add(type_row)

            pass_switch = Adw.SwitchRow()
            pass_switch.set_title(_("Encrypt with passphrase"))
            pass_switch.set_active(False)
            form.add(pass_switch)

            pass_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            pass1 = Gtk.PasswordEntry()
            pass1.set_property("placeholder-text", _("Passphrase"))
            pass2 = Gtk.PasswordEntry()
            pass2.set_property("placeholder-text", _("Confirm passphrase"))
            pass_box.append(pass1); pass_box.append(pass2)
            pass_box.set_visible(False)



            def on_pass_toggle(*_):
                pass_box.set_visible(pass_switch.get_active())
            pass_switch.connect("notify::active", on_pass_toggle)

            # Buttons
            btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            btn_box.set_halign(Gtk.Align.END)
            btn_cancel = Gtk.Button.new_with_label(_("Cancel"))
            btn_primary = Gtk.Button.new_with_label(_("Generate"))
            try:
                btn_primary.add_css_class("suggested-action")
            except Exception:
                pass
            btn_box.append(btn_cancel); btn_box.append(btn_primary)

            # Compose
            content.append(form)
            content.append(pass_box)
            content.append(btn_box)
            tv.set_content(content)
            dlg.set_child(tv)

            def close_dialog(*args):
                try:
                    dlg.force_close()
                except Exception:
                    pass

            btn_cancel.connect("clicked", close_dialog)

            def do_generate(*args):
                try:
                    key_name = (name_row.get_text() or "").strip()
                    if not key_name:
                        raise ValueError(_("Enter a key file name (e.g. id_ed25519)"))
                    if "/" in key_name or key_name.startswith("."):
                        raise ValueError(_("Key file name must not contain '/' or start with '.'"))

                    # Check if key already exists before attempting generation
                    key_path = self.key_manager.ssh_dir / key_name
                    if key_path.exists():
                        # Suggest alternative names
                        base_name = key_name
                        counter = 1
                        while (self.key_manager.ssh_dir / f"{base_name}_{counter}").exists():
                            counter += 1
                        suggestion = f"{base_name}_{counter}"
                        
                        raise ValueError(_("A key named '{}' already exists. Try '{}' instead.").format(key_name, suggestion))

                    kt = "ed25519" if type_row.get_selected() == 0 else "rsa"

                    passphrase = None
                    if pass_switch.get_active():
                        p1 = pass1.get_text() or ""
                        p2 = pass2.get_text() or ""
                        logger.debug(f"SshCopyIdWindow: Passphrase lengths - p1: {len(p1)}, p2: {len(p2)}")
                        if p1 != p2:
                            logger.debug("SshCopyIdWindow: Passphrases do not match")
                            raise ValueError("Passphrases do not match")
                        passphrase = p1
                        logger.info("SshCopyIdWindow: Passphrase enabled")
                        logger.debug("SshCopyIdWindow: Passphrase validation successful")

                    logger.info(f"SshCopyIdWindow: Calling key_manager.generate_key with name='{key_name}', type='{kt}'")
                    logger.debug(f"SshCopyIdWindow: Key generation parameters - name='{key_name}', type='{kt}', "
                               f"size={3072 if kt == 'rsa' else 0}, passphrase={'<set>' if passphrase else 'None'}")
                    
                    new_key = self._km.generate_key(
                        key_name=key_name,
                        key_type=kt,
                        key_size=3072 if kt == "rsa" else 0,
                        comment=None,
                        passphrase=passphrase,
                    )
                    
                    if not new_key:
                        logger.debug("SshCopyIdWindow: Key generation returned None")
                        raise RuntimeError("Key generation failed. See logs for details.")

                    logger.info(f"SshCopyIdWindow: Key generated successfully: {new_key.private_path}")
                    logger.debug(f"SshCopyIdWindow: Generated key details - private_path='{new_key.private_path}', "
                               f"public_path='{new_key.public_path}'")
                    
                    # Ensure the key files are properly written and accessible
                    import time
                    logger.debug("SshCopyIdWindow: Waiting 0.5s for files to be written")
                    time.sleep(0.5)  # Small delay to ensure files are written
                    
                    # Verify the key files exist and are accessible
                    private_exists = os.path.exists(new_key.private_path)
                    public_exists = os.path.exists(new_key.public_path)
                    logger.debug(f"SshCopyIdWindow: File existence check - private: {private_exists}, public: {public_exists}")
                    
                    if not private_exists:
                        logger.debug(f"SshCopyIdWindow: Private key file missing: {new_key.private_path}")
                        raise RuntimeError(f"Private key file not found: {new_key.private_path}")
                    if not public_exists:
                        logger.debug(f"SshCopyIdWindow: Public key file missing: {new_key.public_path}")
                        raise RuntimeError(f"Public key file not found: {new_key.public_path}")
                    
                    logger.info(f"SshCopyIdWindow: Key files verified, starting ssh-copy-id")
                    logger.debug("SshCopyIdWindow: All key files verified successfully")
                    
                    # Run your terminal ssh-copy-id flow
                    logger.debug("SshCopyIdWindow: Calling _show_ssh_copy_id_terminal_using_main_widget()")
                    self._parent._show_ssh_copy_id_terminal_using_main_widget(self._conn, new_key)
                    logger.debug("SshCopyIdWindow: Terminal window launched, closing dialog")
                    self.close()

                except Exception as e:
                    logger.error(f"SshCopyIdWindow: Generate and copy failed: {e}")
                    logger.debug(f"SshCopyIdWindow: Exception details: {type(e).__name__}: {str(e)}")
                    self._error("Generate & Copy failed",
                                "Could not generate a new key and copy it to the server.",
                                str(e))

            btn_primary.connect("clicked", do_generate)
            dlg.present()
            return dlg
        except Exception as e:
            logger.error("Failed to present key generator: %s", e)


    # --- Integrate generator into ssh-copy-id chooser ---------------------------

    def on_copy_key_to_server_clicked(self, _button):
        logger.info("Main window: ssh-copy-id button clicked")
        logger.debug("Main window: Starting ssh-copy-id process")
        
        selected_row = self.connection_list.get_selected_row()
        if not selected_row or not getattr(selected_row, "connection", None):
            logger.warning("Main window: No connection selected for ssh-copy-id")
            # Show message dialog
            try:
                error_dialog = Adw.MessageDialog(
                    transient_for=self,
                    modal=True,
                    heading=_("No Server Selected"),
                    body=_("Select a server first!")
                )
                error_dialog.add_response('ok', _('OK'))
                error_dialog.present()
            except Exception as e:
                logger.error(f"Failed to show error dialog: {e}")
            return
        connection = selected_row.connection
        logger.info(f"Main window: Selected connection: {getattr(connection, 'nickname', 'unknown')}")
        logger.debug(f"Main window: Connection details - host: {getattr(connection, 'hostname', getattr(connection, 'host', 'unknown'))}, "
                    f"username: {getattr(connection, 'username', 'unknown')}, "
                    f"port: {getattr(connection, 'port', 22)}")

        try:
            logger.info("Main window: Creating SshCopyIdWindow")
            logger.debug("Main window: Initializing SshCopyIdWindow with key_manager and connection_manager")
            win = SshCopyIdWindow(self, connection, self.key_manager, self.connection_manager)
            logger.info("Main window: SshCopyIdWindow created successfully, presenting")
            win.present()
        except Exception as e:
            logger.error(f"Main window: ssh-copy-id window failed: {e}")
            logger.debug(f"Main window: Exception details: {type(e).__name__}: {str(e)}")
            # Fallback error if window cannot be created
            try:
                md = Adw.MessageDialog(transient_for=self, modal=True,
                                       heading="Error",
                                       body=f"Could not open the Copy Key window.\n\n{e}")
                md.add_response("ok", "OK")
                md.present()
            except Exception:
                pass

    def show_known_hosts_editor(self):
        """Show known hosts editor window"""
        logger.info("Show known hosts editor window")
        try:
            from .known_hosts_editor import KnownHostsEditorWindow
            editor = KnownHostsEditorWindow(self, self.connection_manager)
            editor.present()
        except Exception as e:
            logger.error(f"Failed to open known hosts editor: {e}")

    def show_shortcut_editor(self):
        """Launch the shortcut editor window"""
        logger.info("Show shortcut editor window")
        try:
            from .shortcut_editor import ShortcutEditorWindow
            editor = ShortcutEditorWindow(self)
            editor.present()
        except Exception as e:
            logger.error(f"Failed to open shortcut editor: {e}")

    def show_preferences(self):
        """Show preferences dialog"""
        logger.info("Show preferences dialog")
        try:
            preferences_window = PreferencesWindow(self, self.config)
            preferences_window.present()
        except Exception as e:
            logger.error(f"Failed to show preferences dialog: {e}")


    def show_about_dialog(self):
        """Show about dialog"""
        # Use Adw.AboutDialog to get support-url and issue-url properties
        about = Adw.AboutDialog()
        about.set_application_name('sshPilot')
        try:
            from . import __version__ as APP_VERSION
        except Exception:
            APP_VERSION = "0.0.0"
        about.set_version(APP_VERSION)
        about.set_application_icon('io.github.mfat.sshpilot')
        about.set_license_type(Gtk.License.GPL_3_0)
        about.set_website('https://sshpilot.app')
        about.set_issue_url('https://github.com/mfat/sshpilot/issues')
        about.set_copyright('© 2025 mFat')
        about.set_developers(['mFat <newmfat@gmail.com>'])
        about.set_translator_credits('')
        
        # Present the dialog as a child of this window
        about.present(self)

    def open_help_url(self):
        """Open the SSH Pilot wiki in the default browser"""
        try:
            import subprocess
            import webbrowser
            
            # Try to open the URL using the default browser
            url = "https://github.com/mfat/sshpilot/wiki"
            
            # Use webbrowser module which handles platform differences
            webbrowser.open(url)
            
            logger.info(f"Opened help URL: {url}")
        except Exception as e:
            logger.error(f"Failed to open help URL: {e}")
            # Fallback: show an error dialog
            try:
                dialog = Gtk.MessageDialog(
                    transient_for=self,
                    modal=True,
                    message_type=Gtk.MessageType.ERROR,
                    buttons=Gtk.ButtonsType.OK,
                    text="Failed to open help",
                    secondary_text=f"Could not open the help URL. Please visit:\n{url}"
                )
                dialog.present()
            except Exception:
                pass

    def show_shortcuts_window(self):
        """Display keyboard shortcuts using Gtk.ShortcutsWindow"""
        # Always rebuild to show current shortcuts (including user customizations)
        self._shortcuts_window = self._build_shortcuts_window()
        try:
            self.set_help_overlay(self._shortcuts_window)
        except Exception:
            pass
        self._shortcuts_window.present()

    def _build_shortcuts_window(self):
        mac = is_macos()
        primary = '<Meta>' if mac else '<primary>'

        win = Gtk.ShortcutsWindow(transient_for=self, modal=True)
        win.set_title(_('Keyboard Shortcuts'))

        # Build header bar with shortcut editor button
        header_bar = Adw.HeaderBar()
        header_bar.set_show_start_title_buttons(False)

        edit_button = Gtk.Button.new_with_mnemonic(_("Edit _Shortcuts…"))
        edit_button.add_css_class('flat')
        edit_button.set_tooltip_text(_("Open the shortcut editor"))
        edit_button.connect('clicked', lambda _btn: self.show_shortcut_editor())
        header_bar.pack_end(edit_button)

        win.set_titlebar(header_bar)

        section = Gtk.ShortcutsSection()
        section.set_property('title', _('Keyboard Shortcuts'))

        # Use enhanced static shortcuts that can show customizations without causing crashes
        self._add_safe_current_shortcuts(section, primary)

        win.add_section(section)
        return win

    def _add_safe_current_shortcuts(self, section, primary):
        """Add shortcuts with current customizations using a safe approach"""
        # Get current shortcuts safely
        current_shortcuts = self._get_safe_current_shortcuts()
        
        # General shortcuts group
        group_general = Gtk.ShortcutsGroup()
        group_general.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Toggle Sidebar'), accelerator='F9'))
        
        # Add general shortcuts with current values
        general_actions = [
            ('quit', _('Quit')),
            ('preferences', _('Preferences')),
            ('help', _('Documentation')),
            ('shortcuts', _('Keyboard Shortcuts')),
            ('edit-ssh-config', _('SSH Config Editor')),
        ]
        
        for action_name, title in general_actions:
            shortcuts = current_shortcuts.get(action_name)
            if shortcuts:
                accelerator = ' '.join(shortcuts)
                group_general.add_shortcut(Gtk.ShortcutsShortcut(
                    title=title, accelerator=accelerator))
        
        section.add_group(group_general)

        # Connection management shortcuts
        group_connections = Gtk.ShortcutsGroup()
        connection_actions = [
            ('new-connection', _('New Connection')),
            ('search', _('Search Connections')),
            ('toggle-list', _('Focus Connection List')),
            ('quick-connect', _('Quick Connect')),
            ('open-new-connection-tab', _('Open New Tab')),
            ('new-key', _('Copy Key to Server')),
        ]
        
        for action_name, title in connection_actions:
            shortcuts = current_shortcuts.get(action_name)
            if shortcuts:
                accelerator = ' '.join(shortcuts)
                group_connections.add_shortcut(Gtk.ShortcutsShortcut(
                    title=title, accelerator=accelerator))
        
        section.add_group(group_connections)

        # Terminal shortcuts
        group_terminal = Gtk.ShortcutsGroup()
        terminal_actions = [
            ('local-terminal', _('Local Terminal')),
            ('broadcast-command', _('Broadcast Command')),
        ]
        
        for action_name, title in terminal_actions:
            shortcuts = current_shortcuts.get(action_name)
            if shortcuts:
                accelerator = ' '.join(shortcuts)
                group_terminal.add_shortcut(Gtk.ShortcutsShortcut(
                    title=title, accelerator=accelerator))
        
        section.add_group(group_terminal)

        # Tab navigation shortcuts
        group_tabs = Gtk.ShortcutsGroup()
        tab_actions = [
            ('tab-next', _('Next Tab')),
            ('tab-prev', _('Previous Tab')),
            ('tab-close', _('Close Tab')),
            ('tab-overview', _('Tab Overview')),
        ]
        
        for action_name, title in tab_actions:
            shortcuts = current_shortcuts.get(action_name)
            if shortcuts:
                accelerator = ' '.join(shortcuts)
                group_tabs.add_shortcut(Gtk.ShortcutsShortcut(
                    title=title, accelerator=accelerator))
        
        section.add_group(group_tabs)

    def _get_safe_current_shortcuts(self):
        """Safely get current shortcuts including customizations"""
        shortcuts = {}
        try:
            app = self.get_application()
            if not app:
                return shortcuts
            
            # Get defaults first
            if hasattr(app, 'get_registered_shortcut_defaults'):
                defaults = app.get_registered_shortcut_defaults()
                shortcuts.update(defaults)
            
            # Apply overrides
            if hasattr(app, 'config') and app.config:
                for action_name in shortcuts.keys():
                    try:
                        override = app.config.get_shortcut_override(action_name)
                        if override is not None:
                            if override:  # Not empty
                                shortcuts[action_name] = override
                            else:  # Disabled
                                shortcuts.pop(action_name, None)
                    except Exception:
                        continue
            
        except Exception as e:
            logger.debug(f"Error getting current shortcuts: {e}")
        
        return shortcuts

    def _add_fallback_shortcuts(self, section, primary):
        """Add fallback static shortcuts if dynamic generation fails"""
        # General shortcuts
        group_general = Gtk.ShortcutsGroup()
        group_general.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Toggle Sidebar'), accelerator='F9'))
        group_general.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('SSH Config Editor'), accelerator=f"{primary}<Shift>e"))
        group_general.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Preferences'), accelerator=f"{primary}comma"))
        group_general.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Documentation'), accelerator='F1'))
        group_general.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Keyboard Shortcuts'), accelerator=f"{primary}<Shift>slash"))
        group_general.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Quit'), accelerator=f"{primary}<Shift>q"))
        section.add_group(group_general)

        # Connection management shortcuts
        group_connections = Gtk.ShortcutsGroup()
        group_connections.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('New Connection'), accelerator=f"{primary}n"))
        group_connections.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Search Connections'), accelerator=f"{primary}f"))
        group_connections.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Focus Connection List'), accelerator=f"{primary}l"))
        group_connections.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Quick Connect'), accelerator=f"{primary}<Alt>c"))
        section.add_group(group_connections)

        # Terminal shortcuts
        group_terminal = Gtk.ShortcutsGroup()
        group_terminal.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Local Terminal'), accelerator=f"{primary}<Shift>t"))
        group_terminal.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Broadcast Command'), accelerator=f"{primary}<Shift>b"))
        section.add_group(group_terminal)

        # Tab navigation shortcuts
        group_tabs = Gtk.ShortcutsGroup()
        group_tabs.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Open New Tab'), accelerator=f"{primary}<Alt>n"))
        group_tabs.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Next Tab'), accelerator='<Alt>Right'))
        group_tabs.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Previous Tab'), accelerator='<Alt>Left'))
        group_tabs.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Close Tab'), accelerator=f"{primary}F4"))
        group_tabs.add_shortcut(Gtk.ShortcutsShortcut(
            title=_('Tab Overview'), accelerator=f"{primary}<Shift>Tab"))
        section.add_group(group_tabs)

    def toggle_list_focus(self):
        """Toggle focus between connection list and terminal"""
        if self.connection_list.has_focus():
            # Focus current terminal
            current_page = self.tab_view.get_selected_page()
            if current_page:
                child = current_page.get_child()
                if hasattr(child, 'vte'):
                    child.vte.grab_focus()
        else:
            # Focus connection list with toast notification
            self.focus_connection_list()

    def _select_tab_relative(self, delta: int):
        """Select tab relative to current index, wrapping around."""
        try:
            n = self.tab_view.get_n_pages()
            if n <= 0:
                return
            current = self.tab_view.get_selected_page()
            # If no current selection, pick first
            if not current:
                page = self.tab_view.get_nth_page(0)
                if page:
                    self.tab_view.set_selected_page(page)
                return
            # Find current index
            idx = 0
            for i in range(n):
                if self.tab_view.get_nth_page(i) == current:
                    idx = i
                    break
            new_index = (idx + delta) % n
            page = self.tab_view.get_nth_page(new_index)
            if page:
                self.tab_view.set_selected_page(page)
        except Exception:
            pass


    # Signal handlers
    def on_connection_click(self, gesture, n_press, x, y):
        """Handle clicks on the connection list"""
        # Get the row that was clicked
        row, _, _ = self._resolve_connection_list_event(x, y)
        if row is None:
            return

        if n_press == 1:  # Single click - just select
            try:
                state = gesture.get_current_event_state()
            except Exception:
                state = 0

            multi_mask = (
                Gdk.ModifierType.CONTROL_MASK
                | Gdk.ModifierType.SHIFT_MASK
                | getattr(Gdk.ModifierType, 'PRIMARY_ACCELERATOR_MASK', 0)
            )

            if state & multi_mask:
                # Allow default multi-selection behavior
                return

            self._select_only_row(row)
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        elif n_press == 2:  # Double click - connect
            if hasattr(row, 'connection'):
                self._cycle_connection_tabs_or_open(row.connection)
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def on_connection_activated(self, list_box, row):
        """Handle connection activation (Enter key)"""
        logger.debug(f"Connection activated - row: {row}, has connection: {hasattr(row, 'connection') if row else False}")
        if row and hasattr(row, 'connection'):
            self._cycle_connection_tabs_or_open(row.connection)
        elif row and hasattr(row, 'group_id'):
            # Handle group row activation - toggle expand/collapse
            logger.debug(f"Group row activated - toggling expand/collapse for group: {row.group_id}")
            row._toggle_expand()
            

        
    def on_connection_activate(self, list_box, row):
        """Handle connection activation (Enter key or double-click)"""
        if row and hasattr(row, 'connection'):
            self._cycle_connection_tabs_or_open(row.connection)
            return True  # Stop event propagation
        return False
        
    def on_activate_connection(self, action, param):
        """Handle the activate-connection action"""
        row = self.connection_list.get_selected_row()
        if row and hasattr(row, 'connection'):
            self._cycle_connection_tabs_or_open(row.connection)

    def _focus_most_recent_tab(self, connection: Connection) -> None:
        """Focus the most recent tab for a connection if one exists.

        Does nothing if the connection has no open tabs.
        """
        try:
            terms_for_conn = []
            try:
                n = self.tab_view.get_n_pages()
            except Exception:
                n = 0
            for i in range(n):
                page = self.tab_view.get_nth_page(i)
                child = page.get_child() if hasattr(page, 'get_child') else None
                if child is not None and self.terminal_to_connection.get(child) == connection:
                    terms_for_conn.append(child)

            if not terms_for_conn:
                return

            target_term = self.active_terminals.get(connection)
            if target_term not in terms_for_conn:
                target_term = terms_for_conn[0]

            page = self.tab_view.get_page(target_term)
            if page is None:
                return

            if self.tab_view.get_selected_page() != page:
                self.tab_view.set_selected_page(page)

            self.active_terminals[connection] = target_term
            try:
                target_term.vte.grab_focus()
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Failed to focus most recent tab for {getattr(connection, 'nickname', '')}: {e}")


    def _focus_most_recent_tab_or_open_new(self, connection: Connection):
        """If there are open tabs for this server, focus the most recent one.
        Otherwise open a new tab for the server.
        """
        try:
            # Check if there are open tabs for this connection
            terms_for_conn = []
            try:
                n = self.tab_view.get_n_pages()
            except Exception:
                n = 0
            for i in range(n):
                page = self.tab_view.get_nth_page(i)
                child = page.get_child() if hasattr(page, 'get_child') else None
                if child is not None and self.terminal_to_connection.get(child) == connection:
                    terms_for_conn.append(child)

            if terms_for_conn:
                # Focus the most recent tab for this connection
                most_recent_term = self.active_terminals.get(connection)
                if most_recent_term and most_recent_term in terms_for_conn:
                    # Use the most recent terminal
                    target_term = most_recent_term
                else:
                    # Fallback to the first tab for this connection
                    target_term = terms_for_conn[0]
                
                page = self.tab_view.get_page(target_term)
                if page is not None:
                    self.tab_view.set_selected_page(page)
                    # Update most-recent mapping
                    self.active_terminals[connection] = target_term
                    # Give focus to the VTE terminal so user can start typing immediately
                    target_term.vte.grab_focus()
                    return

            # No existing tabs for this connection -> open a new one
            self.terminal_manager.connect_to_host(connection, force_new=False)
        except Exception as e:
            logger.error(f"Failed to focus most recent tab or open new for {getattr(connection, 'nickname', '')}: {e}")

    def _cycle_connection_tabs_or_open(self, connection: Connection):
        """If there are open tabs for this server, cycle to the next one (wrap).
        Otherwise open a new tab for the server.
        """
        try:
            # Collect current pages in visual/tab order
            terms_for_conn = []
            try:
                n = self.tab_view.get_n_pages()
            except Exception:
                n = 0
            for i in range(n):
                page = self.tab_view.get_nth_page(i)
                child = page.get_child() if hasattr(page, 'get_child') else None
                if child is not None and self.terminal_to_connection.get(child) == connection:
                    terms_for_conn.append(child)

            if terms_for_conn:
                # Determine current index among this connection's tabs
                selected = self.tab_view.get_selected_page()
                current_idx = -1
                if selected is not None:
                    current_child = selected.get_child()
                    for i, t in enumerate(terms_for_conn):
                        if t == current_child:
                            current_idx = i
                            break
                # Compute next index (wrap)
                next_idx = (current_idx + 1) % len(terms_for_conn) if current_idx >= 0 else 0
                next_term = terms_for_conn[next_idx]
                page = self.tab_view.get_page(next_term)
                if page is not None:
                    self.tab_view.set_selected_page(page)
                    # Update most-recent mapping
                    self.active_terminals[connection] = next_term
                    try:
                        next_term.vte.grab_focus()
                    except Exception:
                        pass
                    return

            # No existing tabs for this connection -> open a new one
            self.terminal_manager.connect_to_host(connection, force_new=False)
            try:
                self._focus_most_recent_tab(connection)
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Failed to cycle or open for {getattr(connection, 'nickname', '')}: {e}")

    def on_tab_selected(self, tab_view: Adw.TabView, _pspec=None) -> None:
        """Update active terminal mapping when the user switches tabs."""
        try:
            page = tab_view.get_selected_page()
            if page is None:
                return
            child = page.get_child() if hasattr(page, 'get_child') else None
            if child is None:
                return
            connection = self.terminal_to_connection.get(child)
            if connection:
                # Check if this is a local terminal
                if _get_connection_host(connection) == 'localhost':
                    # Local terminal - clear selection
                    try:
                        if hasattr(self.connection_list, 'unselect_all'):
                            self.connection_list.unselect_all()
                        else:
                            current = self.connection_list.get_selected_row()
                            if current is not None:
                                self.connection_list.unselect_row(current)
                    except Exception:
                        pass
                else:
                    # Regular connection terminal - select the corresponding row
                    self.active_terminals[connection] = child
                    row = self.connection_rows.get(connection)
                    if row:
                        selected_rows = []
                        try:
                            selected_rows = list(self.connection_list.get_selected_rows())
                        except Exception:
                            current = self.connection_list.get_selected_row()
                            if current:
                                selected_rows = [current]
                        if row not in selected_rows:
                            self._select_only_row(row)
            else:
                # Other non-connection terminal - clear selection
                try:
                    if hasattr(self.connection_list, 'unselect_all'):
                        self.connection_list.unselect_all()
                    else:
                        current = self.connection_list.get_selected_row()
                        if current is not None:
                            self.connection_list.unselect_row(current)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Failed to sync tab selection: {e}")

    def on_connection_selected(self, list_box, row):
        """Handle connection list selection change"""
        try:
            connection_rows = self._get_selected_connection_rows()
            group_rows = self._get_selected_group_rows()
        except Exception:
            connection_rows = []
            group_rows = []

        has_connections = bool(connection_rows)
        has_groups = bool(group_rows)

        if has_connections and not has_groups:
            self.connection_toolbar.set_visible(True)
            self.group_toolbar.set_visible(False)

            multiple_connections = len(connection_rows) > 1
            self.edit_button.set_sensitive(not multiple_connections)
            if hasattr(self, 'copy_key_button'):
                self.copy_key_button.set_sensitive(not multiple_connections)
            if hasattr(self, 'scp_button'):
                self.scp_button.set_sensitive(not multiple_connections)
            self.manage_files_button.set_sensitive(
                not multiple_connections and not should_hide_file_manager_options()
            )
            self.manage_files_button.set_visible(not should_hide_file_manager_options())
            if hasattr(self, 'system_terminal_button') and self.system_terminal_button:
                self.system_terminal_button.set_sensitive(not multiple_connections)
            self.delete_button.set_sensitive(True)
            self.rename_group_button.set_sensitive(False)
            self.delete_group_button.set_sensitive(False)
        elif has_groups and not has_connections:
            self.connection_toolbar.set_visible(False)
            self.group_toolbar.set_visible(True)

            allow_single_group = len(group_rows) == 1
            self.delete_button.set_sensitive(False)
            if hasattr(self, 'copy_key_button'):
                self.copy_key_button.set_sensitive(False)
            if hasattr(self, 'scp_button'):
                self.scp_button.set_sensitive(False)
            self.manage_files_button.set_sensitive(False)
            self.manage_files_button.set_visible(not should_hide_file_manager_options())
            if hasattr(self, 'system_terminal_button') and self.system_terminal_button:
                self.system_terminal_button.set_sensitive(False)
            self.rename_group_button.set_sensitive(allow_single_group)
            self.delete_group_button.set_sensitive(allow_single_group)
        else:
            self.connection_toolbar.set_visible(False)
            self.group_toolbar.set_visible(False)
            self.delete_button.set_sensitive(False)
            if hasattr(self, 'copy_key_button'):
                self.copy_key_button.set_sensitive(False)
            if hasattr(self, 'scp_button'):
                self.scp_button.set_sensitive(False)
            self.manage_files_button.set_sensitive(False)
            self.manage_files_button.set_visible(not should_hide_file_manager_options())
            if hasattr(self, 'system_terminal_button') and self.system_terminal_button:
                self.system_terminal_button.set_sensitive(False)
            self.rename_group_button.set_sensitive(False)
            self.delete_group_button.set_sensitive(False)

    def on_add_connection_clicked(self, button):
        """Handle add connection button click"""
        self.show_connection_dialog()

    def on_edit_connection_clicked(self, button):
        """Handle edit connection button click"""
        selected_row = self.connection_list.get_selected_row()
        if selected_row:
            if hasattr(selected_row, 'connection'):
                self.show_connection_dialog(selected_row.connection)
            else:
                logger.debug("Cannot edit group row")

    def on_sidebar_toggle(self, button):
        """Handle sidebar toggle button click"""
        try:
            # Button active state now represents the action to perform
            # True = hide sidebar, False = show sidebar
            should_hide = button.get_active()
            is_visible = not should_hide
            self._toggle_sidebar_visibility(is_visible)
            
            # Update button icon and tooltip based on current sidebar state
            if is_visible:
                button.set_icon_name('sidebar-show-symbolic')
                button.set_tooltip_text(
                    f'Hide Sidebar (F9, {get_primary_modifier_label()}+B)'
                )
            else:
                button.set_icon_name('sidebar-show-symbolic')
                button.set_tooltip_text(
                    f'Show Sidebar (F9, {get_primary_modifier_label()}+B)'
                )
            
            # No need to save state - sidebar always starts visible
                
        except Exception as e:
            logger.error(f"Failed to toggle sidebar: {e}")

    def on_view_toggle_clicked(self, button):
        """Handle view toggle button click to switch between welcome and tabs"""
        try:
            # Check which view is currently visible
            current_view = self.content_stack.get_visible_child_name()
            
            if current_view == "welcome":
                # Switch to tab view
                self.show_tab_view()
            else:
                # Switch to welcome view
                self.show_welcome_view()
                
        except Exception as e:
            logger.error(f"Failed to toggle view: {e}")

    def _toggle_sidebar_visibility(self, is_visible):
        """Helper method to toggle sidebar visibility"""
        try:
            logger.debug(f"Toggle sidebar visibility requested: {is_visible}, split variant: {getattr(self, '_split_variant', 'unknown')}")
            if HAS_OVERLAY_SPLIT and getattr(self, '_split_variant', '') == 'overlay':
                # For OverlaySplitView
                self.split_view.set_show_sidebar(is_visible)
                logger.debug(f"Set OverlaySplitView sidebar visibility to: {is_visible}")
            elif HAS_NAV_SPLIT and getattr(self, '_split_variant', '') == 'navigation':
                # NavigationSplitView doesn't have set_show_sidebar method
                # The sidebar visibility is controlled by the navigation view
                logger.debug(f"NavigationSplitView sidebar visibility toggle requested: {is_visible}")
            else:
                # For Gtk.Paned fallback
                sidebar_widget = self.split_view.get_start_child()
                if sidebar_widget:
                    sidebar_widget.set_visible(is_visible)
                    logger.debug(f"Set Gtk.Paned sidebar visibility to: {is_visible}")
        except Exception as e:
            logger.error(f"Failed to toggle sidebar visibility: {e}")



    def on_scp_button_clicked(self, button):
        """Prompt the user to choose between uploading or downloading with scp."""
        try:
            selected_row = self.connection_list.get_selected_row()
            if not selected_row:
                return
            connection = getattr(selected_row, 'connection', None)
            if not connection:
                return

            chooser = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_('Transfer files with scp'),
                body=_('Choose whether you want to upload local files to the server or download remote paths to your computer.')
            )
            chooser.add_response('cancel', _('Cancel'))
            chooser.add_response('upload', _('Upload to server…'))
            chooser.add_response('download', _('Download from server…'))
            chooser.set_default_response('upload')
            chooser.set_close_response('cancel')

            def _on_choice(dlg, response):
                dlg.close()
                if response == 'upload':
                    self._start_scp_upload_flow(connection)
                elif response == 'download':
                    self._prompt_scp_download(connection)

            chooser.connect('response', _on_choice)
            chooser.present()
        except Exception as e:
            logger.error(f'SCP transfer chooser failed: {e}')

    def _start_scp_upload_flow(self, connection):
        """Kick off the upload flow using a portal-aware file chooser."""
        try:
            file_dialog = Gtk.FileDialog(title=_('Select files to upload'))
            file_dialog.open_multiple(
                self,
                None,
                lambda fd, res: self._on_upload_files_chosen(fd, res, connection),
            )
        except Exception as e:
            logger.error(f'Upload dialog failed: {e}')

    def _prompt_scp_download(self, connection):
        """Ask the user for remote source paths and local destination for downloads."""
        try:
            prompt = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_('Download files from server'),
                body=_('Enter one or more remote paths (one per line) and the local destination directory. Files will be transferred using scp without a file chooser.')
            )
            prompt.add_response('cancel', _('Cancel'))
            prompt.add_response('download', _('Download'))
            prompt.set_default_response('download')
            prompt.set_close_response('cancel')

            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

            remote_label = Gtk.Label(label=_('Remote path(s) (one per line)'))
            remote_label.set_halign(Gtk.Align.START)
            remote_label.set_wrap(True)
            box.append(remote_label)

            remote_buffer = Gtk.TextBuffer()
            remote_view = Gtk.TextView(buffer=remote_buffer)
            remote_view.set_wrap_mode(Gtk.WrapMode.NONE)
            try:
                remote_view.set_monospace(True)
            except Exception:
                pass

            remote_scroller = Gtk.ScrolledWindow()
            remote_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            remote_scroller.set_min_content_height(96)
            remote_scroller.set_child(remote_view)
            box.append(remote_scroller)

            dest_row = Adw.EntryRow(title=_('Local destination'))
            try:
                default_download_dir = Path.home() / 'Downloads'
                dest_row.set_text(str(default_download_dir))
            except Exception:
                dest_row.set_text('~/')
            box.append(dest_row)

            prompt.set_extra_child(box)

            def _update_state(*_args):
                start_iter = remote_buffer.get_start_iter()
                end_iter = remote_buffer.get_end_iter()
                remote_text = remote_buffer.get_text(start_iter, end_iter, False).strip()
                has_remote = bool(self._parse_remote_sources_text(remote_text))
                has_dest = bool(dest_row.get_text().strip())
                try:
                    prompt.set_response_enabled('download', has_remote and has_dest)
                except Exception:
                    pass

            remote_buffer.connect('changed', _update_state)
            dest_row.connect('notify::text', _update_state)
            _update_state()

            def _on_response(dlg, response):
                if response != 'download':
                    dlg.close()
                    return
                start_iter = remote_buffer.get_start_iter()
                end_iter = remote_buffer.get_end_iter()
                remote_text = remote_buffer.get_text(start_iter, end_iter, False)
                sources = self._parse_remote_sources_text(remote_text)
                destination = dest_row.get_text().strip()
                dlg.close()
                if not sources or not destination:
                    return
                self._start_scp_transfer(connection, sources, destination, direction='download')

            prompt.connect('response', _on_response)
            prompt.present()
        except Exception as e:
            logger.error(f'SCP download prompt failed: {e}')

    def on_manage_files_button_clicked(self, button):
        """Handle manage files button click from toolbar"""
        try:
            selected_row = self.connection_list.get_selected_row()
            if not selected_row:
                return
            connection = getattr(selected_row, 'connection', None)
            if not connection:
                return

            self._open_manage_files_for_connection(connection)
        except Exception as e:
            logger.error(f"Manage files button click failed: {e}")

    def on_system_terminal_button_clicked(self, button):
        """Handle system terminal button click from toolbar"""
        try:
            selected_row = self.connection_list.get_selected_row()
            if not selected_row:
                return
            connection = getattr(selected_row, 'connection', None)
            if not connection:
                return
            
            # Use the same logic as the context menu action
            self.open_in_system_terminal(connection)
        except Exception as e:
            logger.error(f"System terminal button click failed: {e}")

    def _show_ssh_copy_id_terminal_using_main_widget(self, connection, ssh_key, force=False):
        """Show a window with header bar and embedded terminal running ssh-copy-id.

        Requirements:
        - Terminal expands horizontally, no borders around it
        - Header bar contains Cancel and Close buttons
        """
        logger.info("Main window: Starting ssh-copy-id terminal window creation")
        host_value = _get_connection_host(connection)
        logger.debug(f"Main window: Connection details - host: {host_value}, "
                    f"username: {getattr(connection, 'username', 'unknown')}, "
                    f"port: {getattr(connection, 'port', 22)}")
        logger.debug(f"Main window: SSH key details - private_path: {getattr(ssh_key, 'private_path', 'unknown')}, "
                    f"public_path: {getattr(ssh_key, 'public_path', 'unknown')}")
        
        try:
            target = f"{connection.username}@{host_value}" if getattr(connection, 'username', '') else host_value
            pub_name = os.path.basename(getattr(ssh_key, 'public_path', '') or '')
            body_text = _('This will add your public key to the server\'s ~/.ssh/authorized_keys so future logins can use SSH keys.')
            logger.debug(f"Main window: Target: {target}, public key name: {pub_name}")
            
            dlg = Adw.Window()
            dlg.set_transient_for(self)
            dlg.set_modal(True)
            logger.debug("Main window: Created modal window")
            try:
                dlg.set_title(_('ssh-copy-id'))
            except Exception:
                pass
            try:
                dlg.set_default_size(920, 520)
            except Exception:
                pass

            # Header bar with Cancel
            header = Adw.HeaderBar()
            title_widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            title_label = Gtk.Label(label=_('ssh-copy-id'))
            title_label.set_halign(Gtk.Align.START)
            subtitle_label = Gtk.Label(label=_('Copying {key} to {target}').format(key=pub_name or _('selected key'), target=target))
            subtitle_label.set_halign(Gtk.Align.START)
            try:
                title_label.add_css_class('title-2')
                subtitle_label.add_css_class('dim-label')
            except Exception:
                pass
            title_widget.append(title_label)
            title_widget.append(subtitle_label)
            header.set_title_widget(title_widget)

            # Close button is omitted; window has native close (X)

            # Content: TerminalWidget without connecting spinner/banner
            content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            content_box.set_hexpand(True)
            content_box.set_vexpand(True)
            try:
                content_box.set_margin_top(12)
                content_box.set_margin_bottom(12)
                content_box.set_margin_start(6)
                content_box.set_margin_end(6)
            except Exception:
                pass
            # Optional info text under header bar
            info_lbl = Gtk.Label(label=body_text)
            info_lbl.set_halign(Gtk.Align.START)
            try:
                info_lbl.add_css_class('dim-label')
                info_lbl.set_wrap(True)
            except Exception:
                pass
            content_box.append(info_lbl)

            term_widget = TerminalWidget(connection, self.config, self.connection_manager)
            # Hide connecting overlay and suppress disconnect banner for this non-SSH task
            try:
                term_widget._set_connecting_overlay_visible(False)
                setattr(term_widget, '_suppress_disconnect_banner', True)
                term_widget._set_disconnected_banner_visible(False)
            except Exception:
                pass
            term_widget.set_hexpand(True)
            term_widget.set_vexpand(True)
            # No frame: avoid borders around the terminal
            content_box.append(term_widget)

            # Bottom button area with Close button
            button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            button_box.set_halign(Gtk.Align.END)
            button_box.set_margin_top(12)
            
            cancel_btn = Gtk.Button(label=_('Close'))
            try:
                cancel_btn.add_css_class('suggested-action')
            except Exception:
                pass
            button_box.append(cancel_btn)
            
            content_box.append(button_box)

            # Root container combines header and content
            root_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            root_box.append(header)
            root_box.append(content_box)
            dlg.set_content(root_box)

            def _on_cancel(btn):
                try:
                    if hasattr(term_widget, 'disconnect'):
                        term_widget.disconnect()
                except Exception:
                    pass
                dlg.close()
            cancel_btn.connect('clicked', _on_cancel)
            # No explicit close button; use window close (X)

            # Build ssh-copy-id command with options derived from connection settings
            logger.debug("Main window: Building ssh-copy-id command arguments")
            argv = self._build_ssh_copy_id_argv(
                connection,
                ssh_key,
                force,
                known_hosts_path=self.connection_manager.known_hosts_path,
            )
            cmdline = ' '.join([GLib.shell_quote(a) for a in argv])
            logger.info("Starting ssh-copy-id: %s", ' '.join(argv))
            logger.info("Full command line: %s", cmdline)
            logger.debug(f"Main window: Command argv: {argv}")
            logger.debug(f"Main window: Shell-quoted command: {cmdline}")

            # Helper to write colored lines into the terminal
            def _feed_colored_line(text: str, color: str):
                colors = {
                    'red': '\x1b[31m',
                    'green': '\x1b[32m',
                    'yellow': '\x1b[33m',
                    'blue': '\x1b[34m',
                }
                prefix = colors.get(color, '')
                try:
                    term_widget.vte.feed(("\r\n" + prefix + text + "\x1b[0m\r\n").encode('utf-8'))
                except Exception:
                    pass

            # Initial info line
            _feed_colored_line(_('Running ssh-copy-id…'), 'yellow')

            # Handle password authentication consistently with terminal connections
            logger.debug("Main window: Setting up authentication environment")
            env = os.environ.copy()
            logger.debug(f"Main window: Environment variables count: {len(env)}")
            
            # Determine auth method and check for saved password
            logger.debug("Main window: Determining authentication preferences")
            try:
                auth_method = int(getattr(connection, 'auth_method', 0) or 0)
                prefer_password = (auth_method == 1)
            except Exception as e2:
                logger.debug(f"Main window: Failed to get auth method from connection object: {e2}")
                auth_method = 0
                prefer_password = False

            has_saved_password = bool(self.connection_manager.get_password(host_value, connection.username))
            combined_auth = (auth_method == 0 and has_saved_password)
            logger.debug(f"Main window: Has saved password: {has_saved_password}")
            logger.debug(
                f"Main window: Authentication setup - prefer_password={prefer_password}, combined_auth={combined_auth}, has_saved_password={has_saved_password}"
            )

            if (prefer_password or combined_auth) and has_saved_password:
                # Use sshpass for password authentication
                logger.debug("Main window: Using sshpass for password authentication")
                import shutil
                sshpass_path = None
                
                # Check if sshpass is available and executable
                logger.debug("Main window: Checking for sshpass availability")
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
                    logger.debug("Main window: Setting up sshpass with FIFO")
                    from .ssh_password_exec import _mk_priv_dir, _write_once_fifo
                    import threading
                    
                    # Create private temp directory and FIFO
                    logger.debug("Main window: Creating private temp directory")
                    tmpdir = _mk_priv_dir()
                    fifo = os.path.join(tmpdir, "pw.fifo")
                    logger.debug(f"Main window: FIFO path: {fifo}")
                    os.mkfifo(fifo, 0o600)
                    logger.debug("Main window: FIFO created with permissions 0o600")
                    
                    # Start writer thread that writes the password exactly once
                    saved_password = self.connection_manager.get_password(host_value, connection.username)
                    logger.debug(f"Main window: Retrieved saved password, length: {len(saved_password) if saved_password else 0}")
                    t = threading.Thread(target=_write_once_fifo, args=(fifo, saved_password), daemon=True)
                    t.start()
                    logger.debug("Main window: Password writer thread started")
                    
                    # Use sshpass with FIFO
                    original_argv = argv.copy()
                    argv = [sshpass_path, "-f", fifo] + argv
                    logger.debug(f"Main window: Modified argv - added sshpass: {argv}")
                    
                    # Important: strip askpass vars so OpenSSH won't try the askpass helper for passwords
                    env.pop("SSH_ASKPASS", None)
                    env.pop("SSH_ASKPASS_REQUIRE", None)
                    logger.debug("Main window: Removed SSH_ASKPASS environment variables")
                    
                    logger.debug("Using sshpass with FIFO for ssh-copy-id password authentication")
                    
                    # Store tmpdir for cleanup (will be cleaned up when process exits)
                    def cleanup_tmpdir():
                        try:
                            import shutil
                            shutil.rmtree(tmpdir, ignore_errors=True)
                        except Exception:
                            pass
                    import atexit
                    atexit.register(cleanup_tmpdir)
                else:
                    # sshpass not available – allow interactive password prompt
                    logger.warning("Main window: sshpass not available, falling back to interactive prompt")
                    env.pop("SSH_ASKPASS", None)
                    env.pop("SSH_ASKPASS_REQUIRE", None)
            elif (prefer_password or combined_auth) and not has_saved_password:
                # Password may be required but none saved - let SSH prompt interactively
                # Don't set any askpass environment variables
                logger.debug("Main window: Password auth selected but no saved password - using interactive prompt")
            else:
                # Use askpass for passphrase prompts (key-based auth)
                logger.debug("Main window: Using askpass for key-based authentication")
                from .askpass_utils import get_ssh_env_with_askpass
                askpass_env = get_ssh_env_with_askpass()
                logger.debug(f"Main window: Askpass environment variables: {list(askpass_env.keys())}")
                env.update(askpass_env)

            ensure_writable_ssh_home(env)

            # Ensure /app/bin is first in PATH for Flatpak compatibility
            logger.debug("Main window: Setting up PATH for Flatpak compatibility")
            if os.path.exists('/app/bin'):
                current_path = env.get('PATH', '')
                logger.debug(f"Main window: Current PATH: {current_path}")
                if '/app/bin' not in current_path:
                    env['PATH'] = f"/app/bin:{current_path}"
                    logger.debug(f"Main window: Updated PATH: {env['PATH']}")
                else:
                    logger.debug("Main window: /app/bin already in PATH")
            else:
                logger.debug("Main window: /app/bin does not exist, skipping PATH modification")
            
            cmdline = ' '.join([GLib.shell_quote(a) for a in argv])
            logger.info("Starting ssh-copy-id: %s", ' '.join(argv))
            logger.debug(f"Main window: Final command line: {cmdline}")
            envv = [f"{k}={v}" for k, v in env.items()]
            logger.debug(f"Main window: Environment variables count: {len(envv)}")

            try:
                logger.debug("Main window: Spawning ssh-copy-id process in VTE terminal")
                logger.debug(f"Main window: Working directory: {os.path.expanduser('~') or '/'}")
                logger.debug(f"Main window: Command: ['bash', '-lc', '{cmdline}']")
                
                term_widget.vte.spawn_async(
                    Vte.PtyFlags.DEFAULT,
                    os.path.expanduser('~') or '/',
                    ['bash', '-lc', cmdline],
                    envv,  # <— use merged env
                    GLib.SpawnFlags.DEFAULT,
                    None,
                    None,
                    -1,
                    None,
                    None
                )
                logger.debug("Main window: ssh-copy-id process spawned successfully")

                # Show result modal when the command finishes
                def _on_copyid_exited(vte, status):
                    logger.debug(f"Main window: ssh-copy-id process exited with raw status: {status}")
                    # Normalize exit code
                    exit_code = None
                    try:
                        if os.WIFEXITED(status):
                            exit_code = os.WEXITSTATUS(status)
                            logger.debug(f"Main window: Process exited normally, exit code: {exit_code}")
                        else:
                            exit_code = status if 0 <= int(status) < 256 else ((int(status) >> 8) & 0xFF)
                            logger.debug(f"Main window: Process did not exit normally, normalized exit code: {exit_code}")
                    except Exception as e:
                        logger.debug(f"Main window: Error normalizing exit status: {e}")
                        try:
                            exit_code = int(status)
                            logger.debug(f"Main window: Converted status to int: {exit_code}")
                        except Exception as e2:
                            logger.debug(f"Main window: Failed to convert status to int: {e2}")
                            exit_code = status

                    logger.info(f"ssh-copy-id exited with status: {status}, normalized exit_code: {exit_code}")
                    
                    # Simple verification: just check exit code like default ssh-copy-id
                    ok = (exit_code == 0)
                    
                    # Get error details from output if failed
                    error_details = None
                    if not ok:
                        try:
                            content = term_widget.vte.get_text_range(0, 0, -1, -1, None)
                            if content:
                                # Look for common error patterns in the output
                                content_lower = content.lower()
                                if 'permission denied' in content_lower:
                                    error_details = 'Permission denied - check user credentials and server permissions'
                                elif 'connection refused' in content_lower:
                                    error_details = 'Connection refused - check server address and SSH service'
                                elif 'authentication failed' in content_lower:
                                    error_details = 'Authentication failed - check username and password/key'
                                elif 'no such file or directory' in content_lower:
                                    error_details = 'File not found - check if SSH directory exists on server'
                                elif 'operation not permitted' in content_lower:
                                    error_details = 'Operation not permitted - check server permissions'
                                else:
                                    # Extract the last few lines of output for context
                                    lines = content.strip().split('\n')
                                    if lines:
                                        error_details = f"Error details: {lines[-1]}"
                        except Exception as e:
                            logger.debug(f"Main window: Error extracting error details: {e}")
                    
                    if ok:
                        logger.info("ssh-copy-id completed successfully")
                        logger.debug("Main window: ssh-copy-id succeeded, showing success message")
                        _feed_colored_line(_('Public key was installed successfully.'), 'green')
                    else:
                        logger.error(f"ssh-copy-id failed with exit code: {exit_code}")
                        logger.debug(f"Main window: ssh-copy-id failed with exit code {exit_code}")
                        _feed_colored_line(_('Failed to install the public key.'), 'red')
                        if error_details:
                            _feed_colored_line(error_details, 'red')

                    def _present_result_dialog():
                        logger.debug(f"Main window: Presenting result dialog - success: {ok}")
                        msg = Adw.MessageDialog(
                            transient_for=dlg,
                            modal=True,
                            heading=_('Success') if ok else _('Error'),
                            body=(_('Public key copied to {}@{}').format(connection.username, host_value)
                                  if ok else _('Failed to copy the public key. Check logs for details.')),
                        )
                        msg.add_response('ok', _('OK'))
                        msg.set_default_response('ok')
                        msg.set_close_response('ok')
                        msg.present()
                        logger.debug("Main window: Result dialog presented")
                        return False

                    GLib.idle_add(_present_result_dialog)

                try:
                    term_widget.vte.connect('child-exited', _on_copyid_exited)
                except Exception:
                    pass
            except Exception as e:
                logger.error(f'Failed to spawn ssh-copy-id in TerminalWidget: {e}')
                logger.debug(f'Main window: Exception details: {type(e).__name__}: {str(e)}')
                dlg.close()
                # No fallback method available
                logger.error(f'Terminal ssh-copy-id failed: {e}')
                self._error_dialog(_("SSH Key Copy Error"),
                                  _("Failed to copy SSH key to server."), 
                                  f"Terminal error: {str(e)}\n\nPlease check:\n• Network connectivity\n• SSH server configuration\n• User permissions")
                return

            dlg.present()
            logger.debug("Main window: ssh-copy-id terminal window presented successfully")
        except Exception as e:
            logger.error(f'VTE ssh-copy-id window failed: {e}')
            logger.debug(f'Main window: Exception details: {type(e).__name__}: {str(e)}')
            self._error_dialog(_("SSH Key Copy Error"),
                              _("Failed to create ssh-copy-id terminal window."), 
                              f"Error: {str(e)}\n\nThis could be due to:\n• Missing VTE terminal widget\n• Display/GTK issues\n• System resource limitations")



    def _build_ssh_copy_id_argv(
        self,
        connection,
        ssh_key,
        force: bool = False,
        known_hosts_path: Optional[str] = None,
    ):
        """Construct argv for ssh-copy-id honoring saved UI auth preferences."""
        logger.info(f"Building ssh-copy-id argv for key: {getattr(ssh_key, 'public_path', 'unknown')}")
        logger.debug(f"Main window: Building ssh-copy-id command arguments")
        logger.debug(f"Main window: Connection object: {type(connection)}")
        logger.debug(f"Main window: SSH key object: {type(ssh_key)}")
        logger.debug(f"Main window: Force option: {force}")
        logger.info(f"Key object attributes: private_path={getattr(ssh_key, 'private_path', 'unknown')}, public_path={getattr(ssh_key, 'public_path', 'unknown')}")
        host_value = _get_connection_host(connection)
        
        # Verify the public key file exists
        logger.debug(f"Main window: Checking if public key file exists: {ssh_key.public_path}")
        if not os.path.exists(ssh_key.public_path):
            logger.error(f"Public key file does not exist: {ssh_key.public_path}")
            logger.debug(f"Main window: Public key file missing: {ssh_key.public_path}")
            raise RuntimeError(f"Public key file not found: {ssh_key.public_path}")
        
        logger.debug(f"Main window: Public key file verified: {ssh_key.public_path}")
        argv = ['ssh-copy-id']
        
        # Add force option if enabled
        if force:
            argv.append('-f')
            logger.debug("Main window: Added force option (-f) to ssh-copy-id")
        
        argv.extend(['-i', ssh_key.public_path])
        logger.debug(f"Main window: Base command: {argv}")
        try:
            port = getattr(connection, 'port', 22)
            logger.debug(f"Main window: Connection port: {port}")
            if port and port != 22:
                argv += ['-p', str(connection.port)]
                logger.debug(f"Main window: Added port option: -p {connection.port}")
        except Exception as e:
            logger.debug(f"Main window: Error getting port: {e}")
            pass
        # Honor app SSH settings: strict host key checking / auto-add
        logger.debug("Main window: Loading SSH configuration")
        try:
            cfg = Config()
            ssh_cfg = cfg.get_ssh_config() if hasattr(cfg, 'get_ssh_config') else {}
            logger.debug(f"Main window: SSH config: {ssh_cfg}")
            strict_val = str(ssh_cfg.get('strict_host_key_checking', '') or '').strip()
            auto_add = bool(ssh_cfg.get('auto_add_host_keys', True))
            logger.debug(f"Main window: SSH settings - strict_val='{strict_val}', auto_add={auto_add}")
            if strict_val:
                argv += ['-o', f'StrictHostKeyChecking={strict_val}']
                logger.debug(f"Main window: Added strict host key checking: {strict_val}")
            elif auto_add:
                argv += ['-o', 'StrictHostKeyChecking=accept-new']
                logger.debug("Main window: Added auto-accept new host keys")
        except Exception as e:
            logger.debug(f"Main window: Error loading SSH config: {e}")
            argv += ['-o', 'StrictHostKeyChecking=accept-new']
            logger.debug("Main window: Using default strict host key checking: accept-new")

        if known_hosts_path:
            argv += ['-o', f'UserKnownHostsFile={known_hosts_path}']

        # Derive auth prefs from saved config and connection
        logger.debug("Main window: Determining authentication preferences")
        prefer_password = False
        key_mode = 0
        keyfile = getattr(connection, 'keyfile', '') or ''
        logger.debug(f"Main window: Connection keyfile: '{keyfile}'")

        try:
            auth_method = int(getattr(connection, 'auth_method', 0) or 0)
            prefer_password = (auth_method == 1)
        except Exception as e2:
            logger.debug(f"Main window: Error getting auth method from connection object: {e2}")
            auth_method = 0
            prefer_password = False

        has_saved_password = bool(self.connection_manager.get_password(host_value, connection.username))
        combined_auth = (auth_method == 0 and has_saved_password)

        try:
            # key_select_mode is saved in ssh config, our connection object should have it post-load
            key_mode = int(getattr(connection, 'key_select_mode', 0) or 0)
            logger.debug(f"Main window: Key select mode: {key_mode}")
        except Exception as e:
            logger.debug(f"Main window: Error getting key select mode: {e}")
            key_mode = 0

        # Validate keyfile path
        try:
            keyfile_ok = bool(keyfile) and os.path.isfile(keyfile)
            logger.debug(f"Main window: Keyfile validation - keyfile='{keyfile}', exists={keyfile_ok}")
        except Exception as e:
            logger.debug(f"Main window: Error validating keyfile: {e}")
            keyfile_ok = False

        # Priority: if UI selected a specific key and it exists, use it; otherwise fall back to password prefs/try-all
        logger.debug(f"Main window: Applying authentication options - key_mode={key_mode}, keyfile_ok={keyfile_ok}, prefer_password={prefer_password}, combined_auth={combined_auth}")
        
        # For ssh-copy-id, we should NOT add IdentityFile options because:
        # 1. ssh-copy-id should use the same key for authentication that it's copying
        # 2. The -i parameter already specifies which key to copy
        # 3. Adding IdentityFile would cause ssh-copy-id to use a different key for auth
        
        if key_mode == 1 and keyfile_ok:
            # Don't add IdentityFile for ssh-copy-id - it should use the key being copied
            logger.debug(f"Main window: Skipping IdentityFile for ssh-copy-id - using key being copied for authentication")
        else:
            # Apply authentication preferences
            if prefer_password:
                argv += ['-o', 'PreferredAuthentications=password']
                if getattr(connection, 'pubkey_auth_no', False):
                    argv += ['-o', 'PubkeyAuthentication=no']
                    logger.debug("Main window: Added password authentication options - PubkeyAuthentication=no, PreferredAuthentications=password")
                else:
                    logger.debug("Main window: Added password authentication option - PreferredAuthentications=password")
            elif combined_auth:
                argv += [
                    '-o',
                    'PreferredAuthentications=gssapi-with-mic,hostbased,publickey,keyboard-interactive,password'
                ]
                logger.debug(
                    "Main window: Added combined authentication options - "
                    "PreferredAuthentications=gssapi-with-mic,hostbased,publickey,keyboard-interactive,password"
                )
        
        # Target
        target = f"{connection.username}@{host_value}" if getattr(connection, 'username', '') else host_value
        argv.append(target)
        logger.debug(f"Main window: Added target: {target}")
        logger.debug(f"Main window: Final argv: {argv}")
        return argv

    def _on_upload_files_chosen(self, dialog, result, connection):
        try:
            files_model = dialog.open_multiple_finish(result)
            if not files_model or files_model.get_n_items() == 0:
                return
            files = [files_model.get_item(i) for i in range(files_model.get_n_items())]

            prompt = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_('Remote destination'),
                body=_('Enter a remote directory (e.g., ~/ or /var/tmp). Files will be uploaded using scp.')
            )
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            dest_row = Adw.EntryRow(title=_('Remote directory'))
            dest_row.set_text('~')
            box.append(dest_row)
            prompt.set_extra_child(box)
            prompt.add_response('cancel', _('Cancel'))
            prompt.add_response('upload', _('Upload'))
            prompt.set_default_response('upload')
            prompt.set_close_response('cancel')

            def _go(d, resp):
                d.close()
                if resp != 'upload':
                    return
                remote_dir = dest_row.get_text().strip() or '~'
                self._start_scp_transfer(
                    connection,
                    [f.get_path() for f in files],
                    remote_dir,
                    direction='upload',
                )

            prompt.connect('response', _go)
            prompt.present()
        except Exception as e:
            logger.error(f'File selection failed: {e}')

    def _parse_remote_sources_text(self, text: str) -> List[str]:
        """Parse manual remote path input into a list of usable paths."""
        stripped = (text or '').strip()
        if not stripped:
            return []
        lines = [line.strip() for line in stripped.splitlines() if line.strip()]
        if len(lines) > 1:
            return lines
        try:
            return [token for token in shlex.split(stripped) if token]
        except ValueError:
            return [stripped]

    def _start_scp_transfer(self, connection, sources, destination, *, direction: str):
        """Run scp using the same terminal window layout as ssh-copy-id."""
        try:
            self._show_scp_terminal_window(connection, sources, destination, direction)
        except Exception as e:
            logger.error(f'scp {direction} failed to start: {e}')


    def _show_scp_terminal_window(self, connection, sources, destination, direction):
        try:
            host_value = _get_connection_host(connection)
            target = f"{connection.username}@{host_value}" if getattr(connection, 'username', '') else host_value

            if direction == 'upload':
                title_text = _('Upload files (scp)')
                subtitle_text = _('Uploading to {target}:{path}').format(target=target, path=destination)
                info_text = _('We will use scp to upload file(s) to the selected server.')
                start_message = _('Starting upload…')
                success_message = _('Upload finished successfully.')
                failure_message = _('Upload failed. See output above.')
                result_heading_ok = _('Upload complete')
                result_heading_fail = _('Upload failed')
                result_body_ok = _('Files uploaded to {target}:{path}').format(target=target, path=destination)
            elif direction == 'download':
                title_text = _('Download files (scp)')
                subtitle_text = _('Downloading from {target}').format(target=target)
                info_text = _('We will use scp to download file(s) from the selected server into {dest}.').format(dest=destination)
                start_message = _('Starting download…')
                success_message = _('Download finished successfully.')
                failure_message = _('Download failed. See output above.')
                result_heading_ok = _('Download complete')
                result_heading_fail = _('Download failed')
                result_body_ok = _('Files downloaded to {dest}').format(dest=destination)
            else:
                raise ValueError(f'Unsupported scp direction: {direction}')

            dlg = Adw.Window()
            dlg.set_transient_for(self)
            dlg.set_modal(True)
            try:
                dlg.set_title(title_text)
            except Exception:
                pass
            try:
                dlg.set_default_size(920, 520)
            except Exception:
                pass

            header = Adw.HeaderBar()
            title_widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            title_label = Gtk.Label(label=title_text)
            title_label.set_halign(Gtk.Align.START)
            subtitle_label = Gtk.Label(label=subtitle_text)
            subtitle_label.set_halign(Gtk.Align.START)
            try:
                title_label.add_css_class('title-2')
                subtitle_label.add_css_class('dim-label')
            except Exception:
                pass
            title_widget.append(title_label)
            title_widget.append(subtitle_label)
            header.set_title_widget(title_widget)

            cancel_btn = Gtk.Button(label=_('Cancel'))
            try:
                cancel_btn.add_css_class('flat')
            except Exception:
                pass
            header.pack_start(cancel_btn)

            content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            content_box.set_hexpand(True)
            content_box.set_vexpand(True)
            try:
                content_box.set_margin_top(12)
                content_box.set_margin_bottom(12)
                content_box.set_margin_start(6)
                content_box.set_margin_end(6)
            except Exception:
                pass

            info_lbl = Gtk.Label(label=info_text)
            info_lbl.set_halign(Gtk.Align.START)
            try:
                info_lbl.add_css_class('dim-label')
                info_lbl.set_wrap(True)
            except Exception:
                pass
            content_box.append(info_lbl)

            term_widget = TerminalWidget(connection, self.config, self.connection_manager)
            try:
                term_widget._set_connecting_overlay_visible(False)
                setattr(term_widget, '_suppress_disconnect_banner', True)
                term_widget._set_disconnected_banner_visible(False)
            except Exception:
                pass
            term_widget.set_hexpand(True)
            term_widget.set_vexpand(True)
            content_box.append(term_widget)

            root_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            root_box.append(header)
            root_box.append(content_box)
            dlg.set_content(root_box)

            def _on_cancel(btn):
                try:
                    if hasattr(self, '_scp_askpass_helpers'):
                        for helper_path in getattr(self, '_scp_askpass_helpers', []):
                            try:
                                os.unlink(helper_path)
                            except Exception:
                                pass
                        self._scp_askpass_helpers.clear()
                except Exception:
                    pass

                try:
                    if hasattr(term_widget, 'disconnect'):
                        term_widget.disconnect()
                except Exception:
                    pass
                dlg.close()

            cancel_btn.connect('clicked', _on_cancel)

            argv = self._build_scp_argv(
                connection,
                sources,
                destination,
                direction=direction,
                known_hosts_path=self.connection_manager.known_hosts_path,
            )

            env = os.environ.copy()

            if hasattr(self, '_scp_askpass_env') and self._scp_askpass_env:
                env.update(self._scp_askpass_env)
                logger.debug(f"SCP: Using askpass environment for key passphrase: {list(self._scp_askpass_env.keys())}")
                self._scp_askpass_env = {}

            if getattr(self, '_scp_strip_askpass', False):
                env.pop('SSH_ASKPASS', None)
                env.pop('SSH_ASKPASS_REQUIRE', None)
                logger.debug('SCP: Removed SSH_ASKPASS variables for interactive password prompt')
                self._scp_strip_askpass = False

                try:
                    keyfile = getattr(connection, 'keyfile', '') or ''
                    if keyfile and os.path.isfile(keyfile):
                        if hasattr(self, 'connection_manager') and self.connection_manager:
                            key_prepared = self.connection_manager.prepare_key_for_connection(keyfile)
                            if key_prepared:
                                logger.debug(f"SCP: Key prepared for connection: {keyfile}")
                            else:
                                logger.warning(f"SCP: Failed to prepare key for connection: {keyfile}")
                except Exception as e:
                    logger.warning(f"SCP: Error preparing key for connection: {e}")
            else:
                logger.debug('SCP: Password authentication handled by sshpass in command line')

            if os.path.exists('/app/bin'):
                current_path = env.get('PATH', '')
                if '/app/bin' not in current_path:
                    env['PATH'] = f"/app/bin:{current_path}"

            cmdline = ' '.join([GLib.shell_quote(a) for a in argv])
            envv = [f"{k}={v}" for k, v in env.items()]
            logger.debug(f"SCP: Final environment variables: SSH_ASKPASS={env.get('SSH_ASKPASS', 'NOT_SET')}, SSH_ASKPASS_REQUIRE={env.get('SSH_ASKPASS_REQUIRE', 'NOT_SET')}")
            logger.debug(f"SCP: Command line: {cmdline}")

            def _feed_colored_line(text: str, color: str):
                colors = {
                    'red': '\x1b[31m',
                    'green': '\x1b[32m',
                    'yellow': '\x1b[33m',
                    'blue': '\x1b[34m',
                }
                prefix = colors.get(color, '')
                try:
                    term_widget.vte.feed(("\r\n" + prefix + text + "\x1b[0m\r\n").encode('utf-8'))
                except Exception:
                    pass

            _feed_colored_line(start_message, 'yellow')

            try:
                term_widget.vte.spawn_async(
                    Vte.PtyFlags.DEFAULT,
                    os.path.expanduser('~') or '/',
                    ['bash', '-lc', cmdline],
                    envv,
                    GLib.SpawnFlags.DEFAULT,
                    None,
                    None,
                    -1,
                    None,
                    None
                )

                def _on_scp_exited(vte, status):
                    exit_code = None
                    try:
                        if os.WIFEXITED(status):
                            exit_code = os.WEXITSTATUS(status)
                        else:
                            exit_code = status if 0 <= int(status) < 256 else ((int(status) >> 8) & 0xFF)
                    except Exception:
                        try:
                            exit_code = int(status)
                        except Exception:
                            exit_code = status
                    ok = (exit_code == 0)
                    if ok:
                        _feed_colored_line(success_message, 'green')
                    else:
                        _feed_colored_line(failure_message, 'red')

                    def _present_result_dialog():
                        try:
                            if hasattr(self, '_scp_askpass_helpers'):
                                for helper_path in getattr(self, '_scp_askpass_helpers', []):
                                    try:
                                        os.unlink(helper_path)
                                    except Exception:
                                        pass
                                self._scp_askpass_helpers.clear()
                        except Exception:
                            pass

                        msg = Adw.MessageDialog(
                            transient_for=dlg,
                            modal=True,
                            heading=result_heading_ok if ok else result_heading_fail,
                            body=(result_body_ok if ok else _('scp exited with an error. Please review the log output.')),
                        )
                        msg.add_response('ok', _('OK'))
                        msg.set_default_response('ok')
                        msg.set_close_response('ok')
                        msg.present()
                        return False

                    GLib.idle_add(_present_result_dialog)

                try:
                    term_widget.vte.connect('child-exited', _on_scp_exited)
                except Exception:
                    pass
            except Exception as e:
                logger.error(f'Failed to spawn scp in TerminalWidget: {e}')
                dlg.close()
                return

            dlg.present()
        except Exception as e:
            logger.error(f'Failed to open scp terminal window: {e}')
    def _build_scp_argv(
        self,
        connection,
        sources,
        destination,
        *,
        direction: str,
        known_hosts_path: Optional[str] = None,
    ):
        argv = ['scp', '-v']
        host_value = _get_connection_host(connection)
        target = f"{connection.username}@{host_value}" if getattr(connection, 'username', '') else host_value
        transfer_sources, transfer_destination = assemble_scp_transfer_args(
            target,
            sources,
            destination,
            direction,
        )
        # Port
        try:
            if getattr(connection, 'port', 22) and connection.port != 22:
                argv += ['-P', str(connection.port)]
        except Exception:
            pass
        # Auth/SSH options similar to ssh-copy-id
        try:
            cfg = Config()
            ssh_cfg = cfg.get_ssh_config() if hasattr(cfg, 'get_ssh_config') else {}
            strict_val = str(ssh_cfg.get('strict_host_key_checking', '') or '').strip()
            auto_add = bool(ssh_cfg.get('auto_add_host_keys', True))
            if strict_val:
                argv += ['-o', f'StrictHostKeyChecking={strict_val}']
            elif auto_add:
                argv += ['-o', 'StrictHostKeyChecking=accept-new']
        except Exception:
            argv += ['-o', 'StrictHostKeyChecking=accept-new']

        if known_hosts_path:
            argv += ['-o', f'UserKnownHostsFile={known_hosts_path}']
        # Prefer password if selected
        prefer_password = False
        key_mode = 0
        keyfile = getattr(connection, 'keyfile', '') or ''
        try:
            auth_method = int(getattr(connection, 'auth_method', 0) or 0)
            prefer_password = (auth_method == 1)
        except Exception:
            auth_method = 0
            prefer_password = False
        has_saved_password = bool(self.connection_manager.get_password(host_value, connection.username)) if hasattr(self, 'connection_manager') and self.connection_manager else False
        combined_auth = (auth_method == 0 and has_saved_password)
        try:
            key_mode = int(getattr(connection, 'key_select_mode', 0) or 0)
        except Exception:
            key_mode = 0
        try:
            keyfile_ok = bool(keyfile) and os.path.isfile(keyfile)
        except Exception:
            keyfile_ok = False
        # Handle authentication with saved credentials
        if key_mode == 1 and keyfile_ok:
            argv += ['-i', keyfile, '-o', 'IdentitiesOnly=yes']
            
            # Try to get saved passphrase for the key
            try:
                if hasattr(self, 'connection_manager') and self.connection_manager:
                    saved_passphrase = self.connection_manager.get_key_passphrase(keyfile)
                    if saved_passphrase:
                        # Use the secure askpass script for passphrase authentication
                        # This avoids storing passphrases in plain text temporary files
                        from .askpass_utils import get_ssh_env_with_forced_askpass, get_scp_ssh_options
                        askpass_env = get_ssh_env_with_forced_askpass()
                        # Store for later use in the main execution
                        if not hasattr(self, '_scp_askpass_env'):
                            self._scp_askpass_env = {}
                        self._scp_askpass_env.update(askpass_env)
                        logger.debug(f"SCP: Stored askpass environment for key passphrase: {list(askpass_env.keys())}")
                        
                        # Add SSH options to force public key authentication and prevent password fallback
                        argv += get_scp_ssh_options()
            except Exception as e:
                logger.debug(f"Failed to get saved passphrase for SCP: {e}")
                
        elif prefer_password or combined_auth:
            if prefer_password:
                argv += ['-o', 'PreferredAuthentications=password']
                if getattr(connection, 'pubkey_auth_no', False):
                    argv += ['-o', 'PubkeyAuthentication=no']
            else:
                argv += [
                    '-o',
                    'PreferredAuthentications=gssapi-with-mic,hostbased,publickey,keyboard-interactive,password'
                ]
            
            # Try to get saved password
            try:
                if hasattr(self, 'connection_manager') and self.connection_manager:
                    saved_password = self.connection_manager.get_password(host_value, connection.username)
                    if saved_password:
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
                            t = threading.Thread(target=_write_once_fifo, args=(fifo, saved_password), daemon=True)
                            t.start()
                            
                            # Use sshpass with FIFO
                            argv = [sshpass_path, "-f", fifo] + argv
                            
                            logger.debug("Using sshpass with FIFO for SCP password authentication")
                            
                            # Store tmpdir for cleanup (will be cleaned up when process exits)
                            def cleanup_tmpdir():
                                try:
                                    import shutil
                                    shutil.rmtree(tmpdir, ignore_errors=True)
                                except Exception:
                                    pass
                            import atexit
                            atexit.register(cleanup_tmpdir)
                        else:
                            # sshpass not available – allow interactive password prompt
                            logger.warning("SCP: sshpass unavailable, falling back to interactive prompt")
                            self._scp_strip_askpass = True
                    else:
                        # No saved password - will use interactive prompt
                        logger.debug("SCP: Password auth selected but no saved password - using interactive prompt")
            except Exception as e:
                logger.debug(f"Failed to get saved password for SCP: {e}")
        
        for p in transfer_sources:
            argv.append(p)
        argv.append(transfer_destination)
        return argv

    def on_delete_connection_clicked(self, button):
        """Handle delete connection button click"""
        target_rows = self._get_target_connection_rows()
        if not target_rows:
            logger.debug("Delete requested without any connection selection")
            return

        connections = self._connections_from_rows(target_rows)
        neighbor_row = self._determine_neighbor_connection_row(target_rows)

        self._prompt_delete_connections(connections, neighbor_row)

    def on_rename_group_clicked(self, button):
        """Handle rename group button click"""
        selected_row = self.connection_list.get_selected_row()
        if selected_row and hasattr(selected_row, 'group_id'):
            self.on_edit_group_action(None, None)

    def on_delete_group_clicked(self, button):
        """Handle delete group button click"""
        selected_row = self.connection_list.get_selected_row()
        if selected_row and hasattr(selected_row, 'group_id'):
            self.on_delete_group_action(None, None)

    def on_delete_connection_response(self, dialog, response, payload):
        """Handle delete connection dialog response"""
        try:
            neighbor_row = None
            connections: List[Connection]

            if isinstance(payload, dict):
                connections = payload.get('connections', []) or []
                neighbor_row = payload.get('neighbor_row')
            elif isinstance(payload, (list, tuple)):
                connections = list(payload)
            else:
                connections = [payload] if payload else []

            if response not in {'delete', 'close_remove'}:
                return

            for connection in connections:
                if not connection:
                    continue
                if response == 'close_remove':
                    self._disconnect_connection_terminals(connection)
                self.connection_manager.remove_connection(connection)

            # No automatic selection or focus changes after deletion
            # Let the connection removal handler manage the UI state
        except Exception as e:
            logger.error(f"Failed to delete connections: {e}")

    def _on_tab_close_confirmed(self, dialog, response_id, tab_view, page):
        """Handle response from tab close confirmation dialog"""
        dialog.destroy()
        if response_id == 'close':
            self._close_tab(tab_view, page)
        # If cancelled, do nothing - the tab remains open
    
    def _close_tab(self, tab_view, page):
        """Close the tab and clean up resources"""
        if hasattr(page, 'get_child'):
            child = page.get_child()
            if hasattr(child, 'disconnect'):
                # Get the connection associated with this terminal using reverse map
                connection = self.terminal_to_connection.get(child)
                # Disconnect the terminal
                child.disconnect()
                # Clean up multi-tab tracking maps
                try:
                    if connection is not None:
                        # Remove from list for this connection
                        if connection in self.connection_to_terminals and child in self.connection_to_terminals[connection]:
                            self.connection_to_terminals[connection].remove(child)
                            if not self.connection_to_terminals[connection]:
                                del self.connection_to_terminals[connection]
                        # Update most-recent mapping
                        if connection in self.active_terminals and self.active_terminals[connection] is child:
                            remaining = self.connection_to_terminals.get(connection)
                            if remaining:
                                self.active_terminals[connection] = remaining[-1]
                            else:
                                del self.active_terminals[connection]
                    if child in self.terminal_to_connection:
                        del self.terminal_to_connection[child]
                except Exception:
                    pass
        
        # Close the tab page
        tab_view.close_page(page)
        
        # Update the UI based on the number of remaining tabs
        GLib.idle_add(self._update_ui_after_tab_close)
    
    def on_tab_close(self, tab_view, page):
        """Handle tab close - THE KEY FIX: Never call close_page ourselves"""
        # If we are closing pages programmatically (e.g., after deleting a
        # connection), suppress the confirmation dialog and allow the default
        # close behavior to proceed.
        if getattr(self, '_suppress_close_confirmation', False):
            return False
        # Get the connection for this tab
        connection = None
        terminal = None
        if hasattr(page, 'get_child'):
            child = page.get_child()
            if hasattr(child, 'disconnect'):
                terminal = child
                connection = self.terminal_to_connection.get(child)
        
        if not connection:
            # For non-terminal tabs, allow immediate close
            return False  # Allow the default close behavior
        
        # Check if confirmation is required
        confirm_disconnect = self.config.get_setting('confirm-disconnect', True)
        
        if confirm_disconnect:
            # Store tab view and page as instance variables
            self._pending_close_tab_view = tab_view
            self._pending_close_page = page
            self._pending_close_connection = connection
            self._pending_close_terminal = terminal
            
            # Show confirmation dialog
            host_value = _get_connection_host(connection)
            dialog = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_("Close connection to {}").format(connection.nickname or host_value),
                body=_("Are you sure you want to close this connection?")
            )
            dialog.add_response('cancel', _("Cancel"))
            dialog.add_response('close', _("Close"))
            dialog.set_response_appearance('close', Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response('close')
            dialog.set_close_response('cancel')
            
            # Connect to response signal before showing the dialog
            dialog.connect('response', self._on_tab_close_response)
            dialog.present()
            
            # Prevent the default close behavior while we show confirmation
            return True
        else:
            # If no confirmation is needed, just allow the default close behavior.
            # The default handler will close the page, which in turn triggers the
            # terminal disconnection via the page's 'unmap' or 'destroy' signal.
            return False

    def _on_tab_close_response(self, dialog, response_id):
        """Handle the response from the close confirmation dialog."""
        # Retrieve the pending tab info
        tab_view = self._pending_close_tab_view
        page = self._pending_close_page
        terminal = self._pending_close_terminal

        if response_id == 'close':
            # User confirmed, disconnect the terminal. The tab will be removed
            # by the AdwTabView once we finish the close operation.
            if terminal and hasattr(terminal, 'disconnect'):
                terminal.disconnect()
            # Now, tell the tab view to finish closing the page.
            tab_view.close_page_finish(page, True)
            
            # Update tab button visibility after closing
            self._update_tab_button_visibility()
            
            # Check if this was the last tab and show welcome screen if needed
            if tab_view.get_n_pages() == 0:
                self.show_welcome_view()
        else:
            # User cancelled, so we reject the close request.
            # This is the critical step that makes the close button work again.
            tab_view.close_page_finish(page, False)

        dialog.destroy()
        # Clear pending state to avoid memory leaks
        self._pending_close_tab_view = None
        self._pending_close_page = None
        self._pending_close_connection = None
        self._pending_close_terminal = None
    
    def on_tab_attached(self, tab_view, page, position):
        """Handle tab attached"""
        self._update_tab_button_visibility()

    def _update_tab_button_visibility(self):
        """Update TabButton visibility based on number of tabs"""
        try:
            if hasattr(self, 'tab_button'):
                has_tabs = self.tab_view.get_n_pages() > 0
                self.tab_button.set_visible(has_tabs)
        except Exception as e:
            logger.error(f"Failed to update tab button visibility: {e}")

    def on_tab_detached(self, tab_view, page, position):
        """Handle tab detached"""
        # Cleanup terminal-to-connection maps when a page is detached
        try:
            if hasattr(page, 'get_child'):
                child = page.get_child()
                if child in self.terminal_to_connection:
                    connection = self.terminal_to_connection.get(child)
                    # Remove reverse map
                    del self.terminal_to_connection[child]
                    # Remove from per-connection list
                    if connection in self.connection_to_terminals and child in self.connection_to_terminals[connection]:
                        self.connection_to_terminals[connection].remove(child)
                        if not self.connection_to_terminals[connection]:
                            del self.connection_to_terminals[connection]
                    # Update most recent mapping if needed
                    if connection in self.active_terminals and self.active_terminals[connection] is child:
                        remaining = self.connection_to_terminals.get(connection)
                        if remaining:
                            self.active_terminals[connection] = remaining[-1]
                        else:
                            del self.active_terminals[connection]
        except Exception:
            pass

        # Update tab button visibility
        self._update_tab_button_visibility()
        
        # Show welcome view if no more tabs are left
        if tab_view.get_n_pages() == 0:
            self.show_welcome_view()
        else:
            # Update button visibility when tabs remain
            if hasattr(self, 'view_toggle_button'):
                self.view_toggle_button.set_visible(True)

    def on_local_terminal_button_clicked(self, button):
        """Handle local terminal button click"""
        try:
            logger.info("Local terminal button clicked")
            self.terminal_manager.show_local_terminal()
        except Exception as e:
            logger.error(f"Failed to open local terminal: {e}")

    def on_tab_button_clicked(self, button):
        """Handle tab button click to open/close tab overview and switch to tab view"""
        try:
            # First, ensure we're showing the tab view stack
            self.show_tab_view()
            
            # Then toggle the tab overview
            is_open = self.tab_overview.get_open()
            self.tab_overview.set_open(not is_open)
        except Exception as e:
            logger.error(f"Failed to toggle tab overview: {e}")


            

    def on_connection_added(self, manager, connection):
        """Handle new connection added"""
        self.group_manager.connections.setdefault(connection.nickname, None)
        if connection.nickname not in self.group_manager.root_connections:
            self.group_manager.root_connections.append(connection.nickname)
            self.group_manager._save_groups()
        self.rebuild_connection_list()

    def on_connection_removed(self, manager, connection):
        """Handle connection removed from the connection manager"""
        logger.info(f"Connection removed: {connection.nickname}")

        # Save current scroll position before any UI changes
        scroll_position = None
        if hasattr(self, 'connection_scrolled') and self.connection_scrolled:
            vadj = self.connection_scrolled.get_vadjustment()
            if vadj:
                scroll_position = vadj.get_value()

        # Remove from UI if it exists
        if connection in self.connection_rows:
            row = self.connection_rows[connection]
            self.connection_list.remove(row)
            del self.connection_rows[connection]
        
        # Remove from group manager
        self.group_manager.connections.pop(connection.nickname, None)
        if connection.nickname in self.group_manager.root_connections:
            self.group_manager.root_connections.remove(connection.nickname)
        self.group_manager._save_groups()

        # Close all terminals for this connection and clean up maps
        terminals = list(self.connection_to_terminals.get(connection, []))
        # Suppress confirmation while we programmatically close pages
        self._suppress_close_confirmation = True
        try:
            for term in terminals:
                try:
                    page = self.tab_view.get_page(term)
                    if page:
                        self.tab_view.close_page(page)
                except Exception:
                    pass
                try:
                    if hasattr(term, 'disconnect'):
                        term.disconnect()
                except Exception:
                    pass
                # Remove reverse map entry for each terminal
                try:
                    if term in self.terminal_to_connection:
                        del self.terminal_to_connection[term]
                except Exception:
                    pass
        finally:
            self._suppress_close_confirmation = False
        if connection in self.connection_to_terminals:
            del self.connection_to_terminals[connection]
        if connection in self.active_terminals:
            del self.active_terminals[connection]

        # Restore scroll position without auto-selecting any row
        def _restore_scroll_only():
            if scroll_position is not None and hasattr(self, 'connection_scrolled') and self.connection_scrolled:
                vadj = self.connection_scrolled.get_vadjustment()
                if vadj:
                    vadj.set_value(scroll_position)
            return False

        # Use idle_add to restore scroll position after UI updates complete
        GLib.idle_add(_restore_scroll_only)

    def on_connection_status_changed(self, manager, connection, is_connected):
        """Handle connection status change"""
        logger.debug(f"Connection status changed: {connection.nickname} - {'Connected' if is_connected else 'Disconnected'}")
        if connection in self.connection_rows:
            row = self.connection_rows[connection]
            # Force update the connection's is_connected state
            connection.is_connected = is_connected
            # Update the row's status
            row.update_status()
            # Force a redraw of the row
            row.queue_draw()

        # If this was a controlled reconnect and we are now connected, reset the flag
        if is_connected and getattr(self, '_is_controlled_reconnect', False):
            self._is_controlled_reconnect = False

        # Use the same reliable status to control terminal banners
        try:
            for term in self.connection_to_terminals.get(connection, []) or []:
                if hasattr(term, '_set_disconnected_banner_visible'):
                    if is_connected:
                        term._set_disconnected_banner_visible(False)
                    else:
                        # Do not force-show here to avoid duplicate messages; terminals handle showing on failure/loss
                        pass
        except Exception:
            pass

    def on_setting_changed(self, config, key, value):
        """Handle configuration setting change"""
        logger.debug(f"Setting changed: {key} = {value}")
        
        # Apply relevant changes
        if key.startswith('terminal.'):
            # Update terminal themes/fonts
            for terms in self.connection_to_terminals.values():
                for terminal in terms:
                    terminal.apply_theme()

    def on_window_size_changed(self, window, param):
        """Handle window size change"""
        width = self.get_default_size()[0]
        height = self.get_default_size()[1]
        sidebar_width = self._get_sidebar_width()
        
        self.config.save_window_geometry(width, height, sidebar_width)

    def simple_close_handler(self, window):
        """Handle window close - distinguish between tab close and window close"""
        logger.info("")
        
        try:
            # Check if we have any tabs open
            n_pages = self.tab_view.get_n_pages()
            logger.info(f" Number of tabs: {n_pages}")
            
            # If we have tabs, close all tabs first and then quit
            if n_pages > 0:
                logger.info(" CLOSING ALL TABS FIRST")
                # Close all tabs
                while self.tab_view.get_n_pages() > 0:
                    page = self.tab_view.get_nth_page(0)
                    self.tab_view.close_page(page)
            
            # Now quit the application
            logger.info(" QUITTING APPLICATION")
            app = self.get_application()
            if app:
                app.quit()
                
        except Exception as e:
            logger.error(f" ERROR IN WINDOW CLOSE: {e}")
            # Force quit even if there's an error
            app = self.get_application()
            self.show_quit_confirmation_dialog()
            return False  # Don't quit yet, let dialog handle it
        
        # No active connections, safe to quit
        self._do_quit()
        return True  # Safe to quit

    def on_close_request(self, window):
        """Handle window close request - MAIN ENTRY POINT"""
        if self._is_quitting:
            return False  # Already quitting, allow close
            
        # Check for active connections across all tabs
        actually_connected = {}
        local_terminals = []
        ssh_terminals = []
        
        for conn, terms in self.connection_to_terminals.items():
            for term in terms:
                if getattr(term, 'is_connected', False):
                    actually_connected.setdefault(conn, []).append(term)
                    # Categorize terminals
                    if hasattr(term, '_is_local_terminal') and term._is_local_terminal():
                        local_terminals.append(term)
                    else:
                        ssh_terminals.append(term)
        
        # If there are SSH terminals, always show warning
        if ssh_terminals:
            self.show_quit_confirmation_dialog()
            return True  # Prevent close, let dialog handle it
        
        # If there are only local terminals, check their job status
        if local_terminals:
            # Check if any local terminal has an active job
            has_active_jobs = False
            for term in local_terminals:
                if hasattr(term, 'has_active_job') and term.has_active_job():
                    has_active_jobs = True
                    break
            
            # If any local terminal has an active job, show warning
            if has_active_jobs:
                self.show_quit_confirmation_dialog()
                return True  # Prevent close, let dialog handle it
        
        # No active connections or all local terminals are idle, safe to close
        return False  # Allow close

    def show_quit_confirmation_dialog(self):
        """Show confirmation dialog when quitting with active connections"""
        # Bring the main window to the foreground first
        try:
            self.present()
        except Exception as e:
            logger.debug(f"Failed to bring window to foreground: {e}")
        
        # Categorize connected terminals
        connected_items = []
        local_terminals = []
        ssh_terminals = []
        
        for conn, terms in self.connection_to_terminals.items():
            for term in terms:
                if getattr(term, 'is_connected', False):
                    connected_items.append((conn, term))
                    # Categorize terminals
                    if hasattr(term, '_is_local_terminal') and term._is_local_terminal():
                        local_terminals.append((conn, term))
                    else:
                        ssh_terminals.append((conn, term))
        
        active_count = len(connected_items)
        
        # Determine dialog content based on terminal types
        if ssh_terminals:
            # SSH terminals present - use original messaging
            if active_count == 1:
                message = f"You have 1 open terminal tab."
                detail = "Closing the application will disconnect this connection."
            else:
                message = f"You have {active_count} open terminal tabs."
                detail = f"Closing the application will disconnect all connections."
            heading = "Active SSH Connections"
        else:
            # Only local terminals with active jobs
            if active_count == 1:
                message = f"You have 1 local terminal with an active job."
                detail = "Closing the application will terminate the running process."
            else:
                message = f"You have {active_count} local terminals with active jobs."
                detail = f"Closing the application will terminate all running processes."
            heading = "Active Local Terminal Jobs"
        
        dialog = Adw.AlertDialog()
        dialog.set_heading(heading)
        dialog.set_body(f"{message}\n\n{detail}")
        
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('quit', 'Quit Anyway')
        dialog.set_response_appearance('quit', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response('quit')
        dialog.set_close_response('cancel')
        
        dialog.connect('response', self.on_quit_confirmation_response)
        app = self.get_application()
        if app is not None:
            app.hold()

        dialog.present(self)

    def on_quit_confirmation_response(self, dialog, response):
        """Handle quit confirmation dialog response"""
        app = self.get_application()
        try:
            if response == 'quit':
                # Start cleanup process
                shutdown.cleanup_and_quit(self)
        finally:
            if app is not None:
                app.release()
            dialog.close()



    def on_open_new_connection_action(self, action, param=None):
        """Open a new tab for the selected connection via context menu."""
        try:
            connection = getattr(self, '_context_menu_connection', None)
            if connection is None:
                # Fallback to selected row if any
                row = self.connection_list.get_selected_row()
                connection = getattr(row, 'connection', None) if row else None
            if connection is None:
                return
            self.terminal_manager.connect_to_host(connection, force_new=True)
        except Exception as e:
            logger.error(f"Failed to open new connection tab: {e}")

    def on_open_new_connection_tab_action(self, action, param=None):
        """Open a new tab for the selected connection via global shortcut (Ctrl/⌘+Alt+N)."""
        try:
            # Get the currently selected connection
            row = self.connection_list.get_selected_row()
            if row and hasattr(row, 'connection'):
                connection = row.connection
                self.terminal_manager.connect_to_host(connection, force_new=True)
            else:
                # If no connection is selected, show a message or fall back to new connection dialog
                logger.debug(
                    f"No connection selected for {get_primary_modifier_label()}+Alt+N, opening new connection dialog"
                )
                self.show_connection_dialog()
        except Exception as e:
            logger.error(
                f"Failed to open new connection tab with {get_primary_modifier_label()}+Alt+N: {e}"
            )

    def on_manage_files_action(self, action, param=None):
        """Handle manage files action from context menu"""
        if hasattr(self, '_context_menu_connection') and self._context_menu_connection:
            connection = self._context_menu_connection
            try:
                self._open_manage_files_for_connection(connection)
            except Exception as e:
                logger.error(f"Error opening file manager: {e}")

    def _open_manage_files_for_connection(self, connection):
        """Open files for the supplied connection using the best integration."""

        nickname = getattr(connection, 'nickname', None) or getattr(connection, 'hostname', None) or getattr(connection, 'host', None) or getattr(connection, 'username', 'Remote Host')
        host_value = _get_connection_host(connection)
        username = getattr(connection, 'username', '') or ''
        port_value = getattr(connection, 'port', 22)
        effective_port = port_value if port_value and port_value != 22 else None

        def error_callback(error_msg):
            message = error_msg or "Failed to open file manager"
            logger.error(f"Failed to open file manager for {nickname}: {message}")
            self._show_manage_files_error(str(nickname), message)

        success, error_msg, window = launch_remote_file_manager(
            user=str(username or ''),
            host=str(host_value or ''),
            port=effective_port,
            nickname=str(nickname),
            parent_window=self,
            error_callback=error_callback,
        )

        if success:
            logger.info(f"Started file manager for {nickname}")
            if window is not None:
                self._track_internal_file_manager_window(window)
        else:
            message = error_msg or "Failed to start file manager process"
            logger.error(f"Failed to start file manager process for {nickname}: {message}")
            self._show_manage_files_error(str(nickname), message)

    def _track_internal_file_manager_window(self, window):
        """Keep a reference to in-app file manager windows to prevent GC."""

        if window in self._internal_file_manager_windows:
            return
        self._internal_file_manager_windows.append(window)

        def _cleanup(*_args):
            try:
                self._internal_file_manager_windows.remove(window)
            except ValueError:
                pass
            return False

        try:
            if hasattr(window, 'connect'):
                window.connect('close-request', _cleanup)
        except Exception:  # pragma: no cover - defensive
            logger.debug('Unable to attach close handler to internal file manager window')

    def on_edit_connection_action(self, action, param=None):
        """Handle edit connection action from context menu"""
        try:
            connection = getattr(self, '_context_menu_connection', None)
            if connection is None:
                # Fallback to selected row if any
                row = self.connection_list.get_selected_row()
                connection = getattr(row, 'connection', None) if row else None
            if connection is None:
                return
            self.show_connection_dialog(connection)
        except Exception as e:
            logger.error(f"Failed to edit connection: {e}")

    def on_delete_connection_action(self, action, param=None):
        """Handle delete connection action from context menu"""
        try:
            target_rows = self._get_target_connection_rows(prefer_context=True)
            if not target_rows:
                return

            connections = self._connections_from_rows(target_rows)
            neighbor_row = self._determine_neighbor_connection_row(target_rows)

            self._prompt_delete_connections(connections, neighbor_row)
        except Exception as e:
            logger.error(f"Failed to delete connection: {e}")

    def on_open_in_system_terminal_action(self, action, param=None):
        """Handle open in system terminal action from context menu"""
        try:
            connection = getattr(self, '_context_menu_connection', None)
            if connection is None:
                # Fallback to selected row if any
                row = self.connection_list.get_selected_row()
                connection = getattr(row, 'connection', None) if row else None
            if connection is None:
                return
            
            self.open_in_system_terminal(connection)
        except Exception as e:
            logger.error(f"Failed to open in system terminal: {e}")

    def on_broadcast_command_action(self, action, param=None):
        """Handle broadcast command action - shows dialog to input command"""
        try:
            # Check if there are any SSH terminals open
            ssh_terminals_count = 0
            for i in range(self.tab_view.get_n_pages()):
                page = self.tab_view.get_nth_page(i)
                if page is None:
                    continue
                terminal_widget = page.get_child()
                if terminal_widget is None or not hasattr(terminal_widget, 'vte'):
                    continue
                if hasattr(terminal_widget, 'connection'):
                    if (hasattr(terminal_widget.connection, 'nickname') and
                            terminal_widget.connection.nickname == "Local Terminal"):
                        continue
                    if hasattr(terminal_widget.connection, 'hostname'):
                        ssh_terminals_count += 1
            
            if ssh_terminals_count == 0:
                # Show message dialog
                try:
                    error_dialog = Adw.MessageDialog(
                        transient_for=self,
                        modal=True,
                        heading=_("No SSH Terminals Open"),
                        body=_("Connect to your server first!")
                    )
                    error_dialog.add_response('ok', _('OK'))
                    error_dialog.present()
                except Exception as e:
                    logger.error(f"Failed to show error dialog: {e}")
                return
            
            # Create a custom dialog window instead of using Adw.MessageDialog
            dialog = Gtk.Dialog(
                title=_("Broadcast Command"),
                transient_for=self,
                modal=True,
                destroy_with_parent=True
            )
            
            # Set dialog properties
            dialog.set_default_size(400, 150)
            dialog.set_resizable(False)
            
            # Get the content area
            content_area = dialog.get_content_area()
            content_area.set_margin_start(20)
            content_area.set_margin_end(20)
            content_area.set_margin_top(20)
            content_area.set_margin_bottom(20)
            content_area.set_spacing(12)
            
            # Add label
            label = Gtk.Label(label=_("Enter a command to send to all open SSH terminals:"))
            label.set_wrap(True)
            label.set_xalign(0)
            content_area.append(label)
            
            # Add text entry
            entry = Gtk.Entry()
            entry.set_placeholder_text(_("e.g., ls -la"))
            entry.set_activates_default(True)
            entry.set_hexpand(True)
            content_area.append(entry)
            
            # Add buttons
            dialog.add_button(_('Cancel'), Gtk.ResponseType.CANCEL)
            send_button = dialog.add_button(_('Send'), Gtk.ResponseType.OK)
            send_button.get_style_context().add_class('suggested-action')
            
            # Set default button
            dialog.set_default_response(Gtk.ResponseType.OK)
            
            # Connect to response signal
            def on_response(dialog, response):
                if response == Gtk.ResponseType.OK:
                    command = entry.get_text().strip()
                    if command:
                        sent_count, failed_count = self.terminal_manager.broadcast_command(command)
                        
                        # Show result dialog
                        result_dialog = Adw.MessageDialog(
                            transient_for=self,
                            modal=True,
                            heading=_("Command Sent"),
                            body=_("Command sent to {} SSH terminals. {} failed.").format(sent_count, failed_count)
                        )
                        result_dialog.add_response('ok', _('OK'))
                        result_dialog.present()
                    else:
                        # Show error for empty command
                        error_dialog = Adw.MessageDialog(
                            transient_for=self,
                            modal=True,
                            heading=_("Error"),
                            body=_("Please enter a command to send.")
                        )
                        error_dialog.add_response('ok', _('OK'))
                        error_dialog.present()
                dialog.destroy()
            
            dialog.connect('response', on_response)
            
            # Show the dialog
            dialog.present()
            
            # Focus the entry after the dialog is shown
            def focus_entry():
                entry.grab_focus()
                return False
            
            GLib.idle_add(focus_entry)
            
        except Exception as e:
            logger.error(f"Failed to show broadcast command dialog: {e}")
            # Show error dialog
            try:
                error_dialog = Adw.MessageDialog(
                    transient_for=self,
                    modal=True,
                    heading=_("Error"),
                    body=_("Failed to open broadcast command dialog: {}").format(str(e))
                )
                error_dialog.add_response('ok', _('OK'))
                error_dialog.present()
            except Exception:
                pass
    
    def on_create_group_action(self, action, param=None):
        """Handle create group action"""
        try:
            # Create dialog for group name input
            dialog = Gtk.Dialog(
                title=_("Create New Group"),
                transient_for=self,
                modal=True,
                destroy_with_parent=True
            )
            
            dialog.set_default_size(400, 150)
            dialog.set_resizable(False)
            
            content_area = dialog.get_content_area()
            content_area.set_margin_start(20)
            content_area.set_margin_end(20)
            content_area.set_margin_top(20)
            content_area.set_margin_bottom(20)
            content_area.set_spacing(12)
            
            # Add label
            label = Gtk.Label(label=_("Enter a name for the new group:"))
            label.set_wrap(True)
            label.set_xalign(0)
            content_area.append(label)
            
            # Add text entry
            entry = Gtk.Entry()
            entry.set_placeholder_text(_("e.g., Production Servers"))
            entry.set_activates_default(True)
            entry.set_hexpand(True)
            content_area.append(entry)
            
            # Add buttons
            dialog.add_button(_('Cancel'), Gtk.ResponseType.CANCEL)
            create_button = dialog.add_button(_('Create'), Gtk.ResponseType.OK)
            create_button.get_style_context().add_class('suggested-action')
            
            dialog.set_default_response(Gtk.ResponseType.OK)
            
            def on_response(dialog, response):
                if response == Gtk.ResponseType.OK:
                    group_name = entry.get_text().strip()
                    if group_name:
                        self.group_manager.create_group(group_name)
                        self.rebuild_connection_list()
                    else:
                        # Show error for empty name
                        error_dialog = Adw.MessageDialog(
                            transient_for=self,
                            modal=True,
                            heading=_("Error"),
                            body=_("Please enter a group name.")
                        )
                        error_dialog.add_response('ok', _('OK'))
                        error_dialog.present()
                dialog.destroy()
            
            dialog.connect('response', on_response)
            dialog.present()
            
            def focus_entry():
                entry.grab_focus()
                return False
            
            GLib.idle_add(focus_entry)
            
        except Exception as e:
            logger.error(f"Failed to show create group dialog: {e}")
    
    def on_edit_group_action(self, action, param=None):
        """Handle edit group action"""
        try:
            logger.debug("Edit group action triggered")
            # Get the group row from context menu or selected row
            selected_row = getattr(self, '_context_menu_group_row', None)
            if not selected_row:
                selected_row = self.connection_list.get_selected_row()
            logger.debug(f"Selected row: {selected_row}")
            if not selected_row:
                logger.debug("No selected row")
                return
            if not hasattr(selected_row, 'group_id'):
                logger.debug("Selected row is not a group row")
                return
            
            group_id = selected_row.group_id
            logger.debug(f"Group ID: {group_id}")
            group_info = self.group_manager.groups.get(group_id)
            if not group_info:
                logger.debug(f"Group info not found for ID: {group_id}")
                return
            
            # Create dialog for group name editing
            dialog = Gtk.Dialog(
                title=_("Edit Group"),
                transient_for=self,
                modal=True,
                destroy_with_parent=True
            )
            
            dialog.set_default_size(400, 150)
            dialog.set_resizable(False)
            
            content_area = dialog.get_content_area()
            content_area.set_margin_start(20)
            content_area.set_margin_end(20)
            content_area.set_margin_top(20)
            content_area.set_margin_bottom(20)
            content_area.set_spacing(12)
            
            # Add label
            label = Gtk.Label(label=_("Enter a new name for the group:"))
            label.set_wrap(True)
            label.set_xalign(0)
            content_area.append(label)
            
            # Add text entry
            entry = Gtk.Entry()
            entry.set_text(group_info['name'])
            entry.set_activates_default(True)
            entry.set_hexpand(True)
            content_area.append(entry)
            
            # Add buttons
            dialog.add_button(_('Cancel'), Gtk.ResponseType.CANCEL)
            save_button = dialog.add_button(_('Save'), Gtk.ResponseType.OK)
            save_button.get_style_context().add_class('suggested-action')
            
            dialog.set_default_response(Gtk.ResponseType.OK)
            
            def on_response(dialog, response):
                if response == Gtk.ResponseType.OK:
                    new_name = entry.get_text().strip()
                    if new_name:
                        group_info['name'] = new_name
                        self.group_manager._save_groups()
                        self.rebuild_connection_list()
                    else:
                        # Show error for empty name
                        error_dialog = Adw.MessageDialog(
                            transient_for=self,
                            modal=True,
                            heading=_("Error"),
                            body=_("Please enter a group name.")
                        )
                        error_dialog.add_response('ok', _('OK'))
                        error_dialog.present()
                dialog.destroy()
            
            dialog.connect('response', on_response)
            dialog.present()
            
            def focus_entry():
                entry.grab_focus()
                entry.select_region(0, -1)
                return False
            
            GLib.idle_add(focus_entry)
            
        except Exception as e:
            logger.error(f"Failed to show edit group dialog: {e}")
    
    
    def on_move_to_ungrouped_action(self, action, param=None):
        """Handle move to ungrouped action"""
        try:
            connections = self._get_target_connections(prefer_context=True)
            if not connections:
                return

            for connection in connections:
                nickname = getattr(connection, 'nickname', None)
                if nickname:
                    self.group_manager.move_connection(nickname, None)
            self.rebuild_connection_list()

        except Exception as e:
            logger.error(f"Failed to move connection to ungrouped: {e}")
    
    def on_move_to_group_action(self, action, param=None):
        """Handle move to group action"""
        try:
            connections = self._get_target_connections(prefer_context=True)
            if not connections:
                return

            connection_nicknames = [
                conn.nickname for conn in connections if hasattr(conn, 'nickname')
            ]
            if not connection_nicknames:
                return

            # Get available groups
            available_groups = self.get_available_groups()
            logger.debug(f"Available groups for move dialog: {len(available_groups)} groups")
            
            # Show group selection dialog
            dialog = Gtk.Dialog(
                title=_("Move to Group"),
                transient_for=self,
                modal=True,
                destroy_with_parent=True
            )
            
            dialog.set_default_size(400, 300)
            dialog.set_resizable(False)
            
            content_area = dialog.get_content_area()
            content_area.set_margin_start(20)
            content_area.set_margin_end(20)
            content_area.set_margin_top(20)
            content_area.set_margin_bottom(20)
            content_area.set_spacing(12)
            
            # Add label
            if len(connection_nicknames) == 1:
                label_text = _("Select a group to move the connection to:")
            else:
                label_text = _("Select a group to move the selected connections to:")
            label = Gtk.Label(label=label_text)
            label.set_wrap(True)
            label.set_xalign(0)
            content_area.append(label)

            # Add list box for groups
            listbox = Gtk.ListBox()
            listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
            listbox.set_vexpand(True)

            # Add inline group creation section
            create_section_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            create_section_box.set_margin_start(12)
            create_section_box.set_margin_end(12)
            create_section_box.set_margin_top(6)
            create_section_box.set_margin_bottom(6)

            # Create new group label
            create_label = Gtk.Label(label=_("Create New Group"))
            create_label.set_xalign(0)
            create_label.add_css_class("heading")
            create_section_box.append(create_label)

            # Create new group entry and button
            create_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

            self.create_group_entry = Gtk.Entry()
            self.create_group_entry.set_placeholder_text(_("Enter group name"))
            self.create_group_entry.set_hexpand(True)
            create_box.append(self.create_group_entry)

            self.create_group_button = Gtk.Button(label=_("Create"))
            self.create_group_button.add_css_class("suggested-action")
            self.create_group_button.set_sensitive(False)
            create_box.append(self.create_group_button)

            create_section_box.append(create_box)

            # Add the create section to content area
            content_area.append(create_section_box)
            
            # Add separator
            separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            content_area.append(separator)
            
            # Add existing groups label
            if available_groups:
                existing_label = Gtk.Label(label=_("Existing Groups"))
                existing_label.set_xalign(0)
                existing_label.add_css_class("heading")
                content_area.append(existing_label)
            
            # Add groups to list
            selected_group_id = None
            for group in available_groups:
                row = Gtk.ListBoxRow()
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
                
                # Add group icon
                icon = Gtk.Image.new_from_icon_name('folder-symbolic')
                icon.set_pixel_size(16)
                box.append(icon)
                
                # Add group name
                label = Gtk.Label(label=group['name'])
                label.set_xalign(0)
                label.set_hexpand(True)
                box.append(label)
                
                row.set_child(box)
                row.group_id = group['id']
                listbox.append(row)
            
            content_area.append(listbox)
            
            # Add buttons
            dialog.add_button(_('Cancel'), Gtk.ResponseType.CANCEL)
            move_button = dialog.add_button(_('Move'), Gtk.ResponseType.OK)
            move_button.get_style_context().add_class('suggested-action')
            
            dialog.set_default_response(Gtk.ResponseType.OK)
            
            # Connect entry and button events
            def on_entry_changed(entry):
                text = entry.get_text().strip()
                self.create_group_button.set_sensitive(bool(text))

            def on_entry_activated(entry):
                text = entry.get_text().strip()
                if text:
                    on_create_group_clicked()

            def on_create_group_clicked():
                group_name = self.create_group_entry.get_text().strip()
                if group_name:
                    try:
                        # Create the new group
                        new_group_id = self.group_manager.create_group(group_name)
                        # Move all selected connections to the new group
                        for nickname in connection_nicknames:
                            self.group_manager.move_connection(nickname, new_group_id)
                        # Rebuild the connection list
                        self.rebuild_connection_list()
                        # Close the dialog
                        dialog.destroy()
                    except ValueError as e:
                        # Show error dialog for duplicate group name
                        error_dialog = Gtk.Dialog(
                            title=_("Group Already Exists"),
                            transient_for=dialog,
                            modal=True,
                            destroy_with_parent=True
                        )
                        error_dialog.set_default_size(400, 150)
                        error_dialog.set_resizable(False)
                        
                        content_area = error_dialog.get_content_area()
                        content_area.set_margin_start(20)
                        content_area.set_margin_end(20)
                        content_area.set_margin_top(20)
                        content_area.set_margin_bottom(20)
                        
                        # Add error message
                        error_label = Gtk.Label(label=str(e))
                        error_label.set_wrap(True)
                        error_label.set_xalign(0)
                        content_area.append(error_label)
                        
                        # Add OK button
                        error_dialog.add_button(_('OK'), Gtk.ResponseType.OK)
                        error_dialog.set_default_response(Gtk.ResponseType.OK)
                        
                        def on_error_response(dialog, response):
                            dialog.destroy()
                        
                        error_dialog.connect('response', on_error_response)
                        error_dialog.present()
                        
                        # Clear the entry and focus it for retry
                        self.create_group_entry.set_text("")
                        self.create_group_entry.grab_focus()

            self.create_group_entry.connect('changed', on_entry_changed)
            self.create_group_entry.connect('activate', on_entry_activated)
            self.create_group_button.connect('clicked', lambda btn: on_create_group_clicked())

            def on_response(dialog, response):
                if response == Gtk.ResponseType.OK:
                    selected_row = listbox.get_selected_row()
                    if selected_row:
                        target_group_id = selected_row.group_id
                        for nickname in connection_nicknames:
                            self.group_manager.move_connection(nickname, target_group_id)
                        self.rebuild_connection_list()
                dialog.destroy()
            
            dialog.connect('response', on_response)
            dialog.present()
            
        except Exception as e:
            logger.error(f"Failed to show move to group dialog: {e}")
    

    

    def move_connection_to_group(self, connection_nickname: str, target_group_id: str = None):
        """Move a connection to a specific group"""
        try:
            self.group_manager.move_connection(connection_nickname, target_group_id)
            self.rebuild_connection_list()
        except Exception as e:
            logger.error(f"Failed to move connection {connection_nickname} to group: {e}")
    
    def get_available_groups(self) -> List[Dict]:
        """Get list of available groups for selection"""
        return self.group_manager.get_all_groups()

    def open_in_system_terminal(self, connection):
        """Open the connection in the system's default terminal"""
        try:
            host_value = _get_connection_host(connection)
            port_text = f" -p {connection.port}" if hasattr(connection, 'port') and connection.port != 22 else ""
            ssh_command = f"ssh{port_text} {connection.username}@{host_value}" if getattr(connection, 'username', '') else f"ssh{port_text} {host_value}"

            use_external = self.config.get_setting('use-external-terminal', False)
            if use_external:
                terminal_command = self._get_user_preferred_terminal()
            else:
                terminal_command = self._get_default_terminal_command()

            if not terminal_command:
                common_terminals = [
                    'gnome-terminal', 'konsole', 'xterm', 'alacritty',
                    'kitty', 'terminator', 'tilix', 'xfce4-terminal'
                ]
                for term in common_terminals:
                    try:
                        result = subprocess.run(['which', term], capture_output=True, text=True, timeout=2)
                        if result.returncode == 0:
                            terminal_command = [term]
                            break
                    except Exception:
                        continue

            if not terminal_command:
                try:
                    result = subprocess.run(['which', 'xdg-terminal'], capture_output=True, text=True, timeout=2)
                    if result.returncode == 0:
                        terminal_command = ['xdg-terminal']
                except Exception:
                    pass

            if not terminal_command:
                self._show_terminal_error_dialog()
                return

            self._open_system_terminal(terminal_command, ssh_command)

        except Exception as e:
            logger.error(f"Failed to open system terminal: {e}")
            self._show_terminal_error_dialog()

    def _open_system_terminal(self, terminal_command: List[str], ssh_command: str):
        """Launch a terminal command with an SSH command."""
        try:
            if is_macos():
                app = None
                if terminal_command and terminal_command[0] == 'open':
                    # handle commands like ['open', '-a', 'App']
                    if len(terminal_command) >= 3 and terminal_command[1] == '-a':
                        app = os.path.basename(terminal_command[2])
                if app:
                    app_lower = app.lower()
                    if app_lower in ['terminal', 'terminal.app']:
                        script = f'tell app "Terminal" to do script "{ssh_command}"\ntell app "Terminal" to activate'
                        cmd = ['osascript', '-e', script]
                    elif app_lower in ['iterm', 'iterm2', 'iterm.app']:
                        script = (
                            'tell application "iTerm"\n'
                            '    if (count of windows) = 0 then\n'
                            '        create window with default profile\n'
                            '    end if\n'
                            '    tell current window\n'
                            '        create tab with default profile\n'
                            f'        tell current session to write text "{ssh_command}"\n'
                            '    end tell\n'
                            '    activate\n'
                            'end tell'
                        )
                        cmd = ['osascript', '-e', script]
                    elif app_lower == 'warp':
                        cmd = ['open', f'warp://{ssh_command}']
                        # Warp handles focus automatically via URL scheme
                    elif app_lower in ['alacritty', 'kitty']:
                        cmd = ['open', '-a', app, '--args', '-e', 'bash', '-lc', f'{ssh_command}; exec bash']
                        # Launch terminal and then activate it
                        subprocess.Popen(cmd, start_new_session=True)
                        time.sleep(0.5)  # Give the app time to launch
                        activate_script = f'tell application "{app}" to activate'
                        subprocess.Popen(['osascript', '-e', activate_script])
                        return
                    elif app_lower == 'ghostty':
                        cmd = ['open', '-na', app, '--args', '-e', ssh_command]
                        # Launch terminal and then activate it
                        subprocess.Popen(cmd, start_new_session=True)
                        time.sleep(0.5)  # Give the app time to launch
                        activate_script = f'tell application "{app}" to activate'
                        subprocess.Popen(['osascript', '-e', activate_script])
                        return
                    else:
                        cmd = ['open', '-a', app, '--args', 'bash', '-lc', f'{ssh_command}; exec bash']
                        # Launch terminal and then activate it
                        subprocess.Popen(cmd, start_new_session=True)
                        time.sleep(0.5)  # Give the app time to launch
                        activate_script = f'tell application "{app}" to activate'
                        subprocess.Popen(['osascript', '-e', activate_script])
                        return
                else:
                    cmd = terminal_command + ['--args', 'bash', '-lc', f'{ssh_command}; exec bash']
            else:
                terminal_basename = os.path.basename(terminal_command[0])
                if terminal_basename in ['gnome-terminal', 'tilix', 'xfce4-terminal']:
                    cmd = terminal_command + ['--', 'bash', '-c', f'{ssh_command}; exec bash']
                elif terminal_basename in ['konsole', 'terminator', 'guake']:
                    cmd = terminal_command + ['-e', f'bash -c "{ssh_command}; exec bash"']
                elif terminal_basename in ['alacritty', 'kitty']:
                    cmd = terminal_command + ['-e', 'bash', '-c', f'{ssh_command}; exec bash']
                elif terminal_basename == 'xterm':
                    cmd = terminal_command + ['-e', f'bash -c "{ssh_command}; exec bash"']
                elif terminal_basename == 'xdg-terminal':
                    cmd = terminal_command + [ssh_command]
                else:
                    cmd = terminal_command + [ssh_command]

            logger.info(f"Launching system terminal: {' '.join(cmd)}")
            subprocess.Popen(cmd, start_new_session=True)
            
            # Try to bring the terminal to front on Linux
            if not is_macos():
                try:
                    # Try wmctrl first (more reliable)
                    result = subprocess.run(['which', 'wmctrl'], capture_output=True, timeout=1)
                    if result.returncode == 0:
                        time.sleep(0.5)  # Give the terminal time to launch
                        terminal_basename = os.path.basename(terminal_command[0])
                        subprocess.Popen(['wmctrl', '-a', terminal_basename], 
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    else:
                        # Fallback to xdotool
                        result = subprocess.run(['which', 'xdotool'], capture_output=True, timeout=1)
                        if result.returncode == 0:
                            time.sleep(0.5)  # Give the terminal time to launch
                            terminal_basename = os.path.basename(terminal_command[0])
                            subprocess.Popen(['xdotool', 'search', '--name', terminal_basename, 'windowactivate'], 
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    # Ignore focus errors - terminal launching is more important
                    pass

        except Exception as e:
            logger.error(f"Failed to open system terminal: {e}")
            self._show_terminal_error_dialog()

    def _open_connection_in_external_terminal(self, connection):
        """Open the connection in the user's preferred external terminal"""
        try:
            host_value = _get_connection_host(connection)
            port_text = f" -p {connection.port}" if hasattr(connection, 'port') and connection.port != 22 else ""
            ssh_command = f"ssh{port_text} {connection.username}@{host_value}" if getattr(connection, 'username', '') else f"ssh{port_text} {host_value}"

            terminal = self._get_user_preferred_terminal()
            if not terminal:
                terminal = self._get_default_terminal_command()

            if not terminal:
                self._show_terminal_error_dialog()
                return

            self._open_system_terminal(terminal, ssh_command)

        except Exception as e:
            logger.error(f"Failed to open connection in external terminal: {e}")
            self._show_terminal_error_dialog()

    def _get_default_terminal_command(self) -> Optional[List[str]]:
        """Get the default terminal command from desktop environment"""
        try:
            if is_macos():
                # Map bundle identifiers to display names in preference order.
                mac_terms = {
                    'com.apple.Terminal': 'Terminal',
                    'com.googlecode.iterm2': 'iTerm',

                    'dev.warp.Warp': 'Warp',
                    'io.alacritty': 'Alacritty',
                    'net.kovidgoyal.kitty': 'Kitty',
                    'com.mitmaro.ghostty': 'Ghostty',
                }

                for bundle_id, name in mac_terms.items():
                    # First try AppleScript lookup by app name
                    try:
                        result = subprocess.run(
                            ['osascript', '-e', f'id of app "{name}"'],
                            capture_output=True,
                            text=True,
                            timeout=2,
                        )
                        if result.returncode == 0 and bundle_id in result.stdout:
                            return ['open', '-a', name]
                    except Exception:
                        pass

                    # Fallback to Spotlight metadata search by bundle identifier
                    try:
                        result = subprocess.run(
                            ['mdfind', f'kMDItemCFBundleIdentifier=={bundle_id}'],
                            capture_output=True,
                            text=True,
                            timeout=2,
                        )
                        if result.returncode == 0 and result.stdout.strip():
                            return ['open', '-a', name]
                    except Exception:
                        pass

                return None

            desktop = os.environ.get('XDG_CURRENT_DESKTOP', '').lower()

            if 'gnome' in desktop:
                return ['gnome-terminal']
            elif 'kde' in desktop or 'plasma' in desktop:
                return ['konsole']
            elif 'xfce' in desktop:
                return ['xfce4-terminal']
            elif 'cinnamon' in desktop:
                return ['gnome-terminal']
            elif 'mate' in desktop:
                return ['mate-terminal']
            elif 'lxqt' in desktop:
                return ['qterminal']
            elif 'lxde' in desktop:
                return ['lxterminal']

            common_terminals = [
                'gnome-terminal', 'konsole', 'xfce4-terminal', 'alacritty',
                'kitty', 'terminator', 'tilix', 'guake'
            ]

            for term in common_terminals:
                try:
                    result = subprocess.run(['which', term], capture_output=True, text=True, timeout=2)
                    if result.returncode == 0:
                        return [term]
                except Exception:
                    continue

            return None

        except Exception as e:
            logger.error(f"Failed to get default terminal: {e}")
            return None
    
    def _get_user_preferred_terminal(self) -> Optional[List[str]]:
        """Get the user's preferred terminal from settings"""
        try:
            preferred_terminal = self.config.get_setting('external-terminal', 'gnome-terminal')

            if preferred_terminal == 'custom':
                custom_path = self.config.get_setting('custom-terminal-path', '')
                if custom_path:
                    if is_macos():
                        return ['open', '-a', custom_path]
                    return [custom_path]
                else:
                    logger.warning("Custom terminal path is not set, falling back to built-in terminal")
                    return None

            if is_macos():
                # Preferences may store either an app name ("iTerm") or a full
                # command ("open -a iTerm").  If the value already starts with
                # "open" use it verbatim, otherwise build an "open -a" command
                # for the specified app.
                if preferred_terminal.startswith('open'):
                    return shlex.split(preferred_terminal)

                return ['open', '-a', preferred_terminal]

            try:
                result = subprocess.run(['which', preferred_terminal], capture_output=True, text=True, timeout=2)
                if result.returncode == 0:
                    return [preferred_terminal]
                else:
                    logger.warning(f"Preferred terminal '{preferred_terminal}' not found, falling back to built-in terminal")
                    return None
            except Exception as e:
                logger.error(f"Failed to check preferred terminal '{preferred_terminal}': {e}")
                return None

        except Exception as e:
            logger.error(f"Failed to get user preferred terminal: {e}")
            return None



    def _show_terminal_error_dialog(self):
        """Show error dialog when no terminal is found"""
        try:
            dialog = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_("No Terminal Found"),
                body=_("Could not find a suitable terminal application. Please install a terminal like gnome-terminal, konsole, or xterm.")
            )
            
            dialog.add_response("ok", _("OK"))
            dialog.set_default_response("ok")
            dialog.set_close_response("ok")
            dialog.present()
            
        except Exception as e:
            logger.error(f"Failed to show terminal error dialog: {e}")

    def _show_manage_files_error(self, connection_name: str, error_message: str):
        """Show error dialog for manage files failure"""
        try:
            # Determine error type for appropriate messaging
            is_ssh_error = "ssh connection" in error_message.lower() or "connection failed" in error_message.lower()
            is_timeout_error = "timeout" in error_message.lower()
            
            if is_ssh_error or is_timeout_error:
                heading = _("SSH Connection Failed")
                body = _("Could not establish SSH connection to the server. Please check:")
                
                suggestions = [
                    _("• Server is running and accessible"),
                    _("• SSH service is enabled on the server"),
                    _("• Firewall allows SSH connections"),
                    _("• Your SSH keys or credentials are correct"),
                    _("• Network connectivity to the server")
                ]
            else:
                heading = _("File Manager Error")
                body = _("Failed to open file manager for remote server.")
                suggestions = [
                    _("• Try again in a moment"),
                    _("• Check if the server is accessible"),
                    _("• Ensure you have proper permissions")
                ]
            
            # Create suggestions box
            suggestions_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            suggestions_box.set_margin_top(12)
            
            for suggestion in suggestions:
                label = Gtk.Label(label=suggestion)
                label.set_halign(Gtk.Align.START)
                label.set_wrap(True)
                suggestions_box.append(label)
            
            msg = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=heading,
                body=body
            )
            msg.set_extra_child(suggestions_box)
            
            # Add technical details if available
            if error_message and error_message.strip():
                detail_label = Gtk.Label(label=error_message)
                detail_label.add_css_class("dim-label")
                detail_label.set_wrap(True)
                detail_label.set_margin_top(8)
                suggestions_box.append(detail_label)
            
            msg.add_response("ok", _("OK"))
            msg.set_default_response("ok")
            msg.set_close_response("ok")
            msg.present()
            
        except Exception as e:
            logger.error(f"Failed to show manage files error dialog: {e}")

    def _do_quit(self):
        """Actually quit the application - FINAL STEP"""
        try:
            logger.info("Quitting application")
            
            # Save window geometry
            self._save_window_state()
            
            # Get the application and quit
            app = self.get_application()
            if app:
                app.quit()
            else:
                # Fallback: close the window directly
                self.close()
                
        except Exception as e:
            logger.error(f"Error during final quit: {e}")
            # Force exit as last resort
            import sys
            sys.exit(0)
        
        return False  # Don't repeat timeout

    def _save_window_state(self):
        """Save window state before quitting"""
        try:
            width, height = self.get_default_size()
            sidebar_width = getattr(self.split_view, 'get_sidebar_width', lambda: 250)()
            self.config.save_window_geometry(width, height, sidebar_width)
            logger.debug(f"Saved window geometry: {width}x{height}, sidebar: {sidebar_width}")
        except Exception as e:
            logger.error(f"Failed to save window state: {e}")
            self.welcome_view.set_visible(False)
            self.tab_view.set_visible(True)
            # Update tab titles in case they've changed
            self._update_tab_titles()
    
    def _update_tab_titles(self):
        """Update tab titles"""
        for page in self.tab_view.get_pages():
            child = page.get_child()
            if hasattr(child, 'connection'):
                page.set_title(child.connection.nickname)
    
    def on_connection_saved(self, dialog, connection_data):
        """Handle connection saved from dialog"""
        try:
            if dialog.is_editing:
                # Update existing connection
                old_connection = dialog.connection
                is_connected = old_connection in self.active_terminals
                
                # Store the current terminal instance if connected
                terminal = self.active_terminals.get(old_connection) if is_connected else None
                
                try:
                    logger.info(
                        "Window.on_connection_saved(edit): saving '%s' with %d forwarding rules",
                        old_connection.nickname, len(connection_data.get('forwarding_rules', []) or [])
                    )
                except Exception:
                    pass
                
                # Detect if anything actually changed; avoid unnecessary writes/prompts
                def _norm_str(v):
                    try:
                        s = ('' if v is None else str(v)).strip()
                        # Treat keyfile placeholders as empty
                        if s.lower().startswith('select key file') or 'select key file or leave empty' in s.lower():
                            return ''
                        return s
                    except Exception:
                        return ''
                def _norm_rules(rules):
                    try:
                        return list(rules or [])
                    except Exception:
                        return []
                existing = {
                    'nickname': _norm_str(getattr(old_connection, 'nickname', '')),
                    'hostname': _norm_str(getattr(old_connection, 'hostname', getattr(old_connection, 'host', ''))),
                    'username': _norm_str(getattr(old_connection, 'username', '')),
                    'port': int(getattr(old_connection, 'port', 22) or 22),
                    'auth_method': int(getattr(old_connection, 'auth_method', 0) or 0),
                    'keyfile': _norm_str(getattr(old_connection, 'keyfile', '')),
                    'certificate': _norm_str(getattr(old_connection, 'certificate', '')),
                    'key_select_mode': int(getattr(old_connection, 'key_select_mode', 0) or 0),
                    'password': _norm_str(getattr(old_connection, 'password', '')),
                    'key_passphrase': _norm_str(getattr(old_connection, 'key_passphrase', '')),
                    'x11_forwarding': bool(getattr(old_connection, 'x11_forwarding', False)),
                    'forwarding_rules': _norm_rules(getattr(old_connection, 'forwarding_rules', [])),
                    'local_command': _norm_str(getattr(old_connection, 'local_command', '') or (getattr(old_connection, 'data', {}).get('local_command') if hasattr(old_connection, 'data') else '')),
                    'remote_command': _norm_str(getattr(old_connection, 'remote_command', '') or (getattr(old_connection, 'data', {}).get('remote_command') if hasattr(old_connection, 'data') else '')),
                    'extra_ssh_config': _norm_str(getattr(old_connection, 'extra_ssh_config', '') or (getattr(old_connection, 'data', {}).get('extra_ssh_config') if hasattr(old_connection, 'data') else '')),
                }
                incoming = {
                    'nickname': _norm_str(connection_data.get('nickname')),
                    'hostname': _norm_str(connection_data.get('hostname') or connection_data.get('host')),
                    'username': _norm_str(connection_data.get('username')),
                    'port': int(connection_data.get('port') or 22),
                    'auth_method': int(connection_data.get('auth_method') or 0),
                    'keyfile': _norm_str(connection_data.get('keyfile')),
                    'certificate': _norm_str(connection_data.get('certificate')),
                    'key_select_mode': int(connection_data.get('key_select_mode') or 0),
                    'password': _norm_str(connection_data.get('password')),
                    'key_passphrase': _norm_str(connection_data.get('key_passphrase')),
                    'x11_forwarding': bool(connection_data.get('x11_forwarding', False)),
                    'forwarding_rules': _norm_rules(connection_data.get('forwarding_rules')),
                    'local_command': _norm_str(connection_data.get('local_command')),
                    'remote_command': _norm_str(connection_data.get('remote_command')),
                    'extra_ssh_config': _norm_str(connection_data.get('extra_ssh_config')),
                }
                # Determine if anything meaningful changed by comparing canonical SSH config blocks
                try:
                    existing_block = self.connection_manager.format_ssh_config_entry(existing)
                    incoming_block = self.connection_manager.format_ssh_config_entry(incoming)
                    # Also include auth_method/password/key_select_mode delta in change detection
                    pw_changed_flag = bool(connection_data.get('password_changed', False))
                    ksm_changed = (existing.get('key_select_mode', 0) != incoming.get('key_select_mode', 0))
                    changed = (existing_block != incoming_block) or (existing['auth_method'] != incoming['auth_method']) or pw_changed_flag or ksm_changed or (existing['password'] != incoming['password'])
                except Exception:
                    # Fallback to dict comparison if formatter fails
                    changed = existing != incoming

                # Extra guard: if key_select_mode or auth_method differs from the object's current value, force changed
                try:
                    if int(connection_data.get('key_select_mode', -1)) != int(getattr(old_connection, 'key_select_mode', -1)):
                        changed = True
                    if int(connection_data.get('auth_method', -1)) != int(getattr(old_connection, 'auth_method', -1)):
                        changed = True
                except Exception:
                    pass

                # Always force update when editing connections - skip change detection entirely for forwarding rules
                logger.info("Editing connection '%s' - forcing update to ensure forwarding rules are synced", existing['nickname'])

                logger.debug(f"Updating connection '{old_connection.nickname}'")

                # Ensure auth_method always present and normalized
                try:
                    connection_data['auth_method'] = int(connection_data.get('auth_method', getattr(old_connection, 'auth_method', 0)) or 0)
                except Exception:
                    connection_data['auth_method'] = 0

                original_nickname = old_connection.nickname

                # Update connection in manager first
                if not self.connection_manager.update_connection(old_connection, connection_data):
                    logger.error("Failed to update connection in SSH config")
                    return

                # Preserve group assignment if nickname changed
                new_nickname = connection_data['nickname']
                if original_nickname != new_nickname:
                    try:
                        self.group_manager.rename_connection(original_nickname, new_nickname)
                    except Exception:
                        pass

                # Update connection attributes in memory (ensure forwarding rules kept)
                old_connection.nickname = connection_data['nickname']
                old_connection.hostname = connection_data['hostname']
                old_connection.host = old_connection.hostname
                old_connection.username = connection_data['username']
                old_connection.port = connection_data['port']
                old_connection.keyfile = connection_data['keyfile']
                old_connection.certificate = connection_data.get('certificate', '')
                old_connection.password = connection_data['password']
                old_connection.key_passphrase = connection_data['key_passphrase']
                old_connection.auth_method = connection_data['auth_method']
                # Persist key selection mode in-memory so the dialog reflects it without restart
                try:
                    old_connection.key_select_mode = int(connection_data.get('key_select_mode', getattr(old_connection, 'key_select_mode', 0)) or 0)
                except Exception:
                    pass
                old_connection.x11_forwarding = connection_data['x11_forwarding']
                old_connection.forwarding_rules = list(connection_data.get('forwarding_rules', []))
                # Update commands
                try:
                    old_connection.local_command = connection_data.get('local_command', '')
                    old_connection.remote_command = connection_data.get('remote_command', '')
                    old_connection.extra_ssh_config = connection_data.get('extra_ssh_config', '')
                except Exception:
                    pass
                
                # The connection has already been updated in-place, so we don't need to reload from disk
                # The forwarding rules are already updated in the connection_data
                


                # Update UI
                if old_connection in self.connection_rows:
                    # Get the row before potentially modifying the dictionary
                    row = self.connection_rows[old_connection]
                    # Remove the old connection from the dictionary
                    del self.connection_rows[old_connection]
                    # Add it back with the updated connection object
                    self.connection_rows[old_connection] = row
                    # Update the display
                    row.update_display()
                else:
                    # If the connection is not in the rows, rebuild the list
                    self.rebuild_connection_list()
                
                logger.info(f"Updated connection: {old_connection.nickname}")
                
                # If the connection is active, ask if user wants to reconnect
                if is_connected and terminal is not None:
                    # Store the terminal in the connection for later use
                    old_connection._terminal_instance = terminal
                    self._prompt_reconnect(old_connection)
                
            else:
                # Create new connection
                connection = Connection(connection_data)
                # Ensure the in-memory object has the chosen auth_method immediately
                try:
                    connection.auth_method = int(connection_data.get('auth_method', 0))
                except Exception:
                    connection.auth_method = 0
                # Ensure key selection mode is applied immediately
                try:
                    connection.key_select_mode = int(connection_data.get('key_select_mode', 0) or 0)
                except Exception:
                    connection.key_select_mode = 0
                # Ensure certificate is applied immediately
                try:
                    connection.certificate = connection_data.get('certificate', '')
                except Exception:
                    connection.certificate = ''
                # Ensure extra SSH config settings are applied immediately
                try:
                    connection.extra_ssh_config = connection_data.get('extra_ssh_config', '')
                except Exception:
                    connection.extra_ssh_config = ''
                # Add the new connection to the manager's connections list
                self.connection_manager.connections.append(connection)
                

                
                # Save the connection to SSH config and emit the connection-added signal
                if self.connection_manager.update_connection(connection, connection_data):
                    # Reload from SSH config and rebuild list immediately
                    try:
                        self.connection_manager.load_ssh_config()
                        self.rebuild_connection_list()
                    except Exception:
                        pass
                    # Reload config after saving
                    try:

                        self.connection_manager.load_ssh_config()
                        self.rebuild_connection_list()

                    except Exception:
                        pass
                    # Sync forwarding rules from a fresh reload to ensure UI matches disk
                    try:
                        reloaded_new = self.connection_manager.find_connection_by_nickname(connection.nickname)
                        if reloaded_new:
                            connection.forwarding_rules = list(reloaded_new.forwarding_rules or [])
                            logger.info("New connection '%s' has %d rules after write", connection.nickname, len(connection.forwarding_rules))
                    except Exception:
                        pass
                    # Manually add the connection to the UI since we're not using the signal
                    # Row list was rebuilt from config; no manual add required
                    logger.info(f"Created new connection: {connection_data['nickname']}")
                else:
                    logger.error("Failed to save connection to SSH config")
                
        except Exception as e:
            logger.error(f"Failed to save connection: {e}")
            # Show error dialog
            error_dialog = Gtk.MessageDialog(
                transient_for=self,
                modal=True,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text=_("Failed to save connection"),
                secondary_text=str(e)
            )
            error_dialog.present()
    
    def _rebuild_connections_list(self):
        """Rebuild the sidebar connections list from manager state, avoiding duplicates."""
        try:
            self.rebuild_connection_list()
        except Exception:
            pass
    def _prompt_reconnect(self, connection):
        """Show a dialog asking if user wants to reconnect with new settings"""
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=_("Settings Changed"),
            secondary_text=_("The connection settings have been updated.\n"
                           "Would you like to reconnect with the new settings?")
        )
        dialog.connect("response", self._on_reconnect_response, connection)
        dialog.present()
    
    def _on_reconnect_response(self, dialog, response_id, connection):
        """Handle response from reconnect prompt"""
        dialog.destroy()
        
        # Only proceed if user clicked Yes and the connection is still active
        if response_id != Gtk.ResponseType.YES or connection not in self.active_terminals:
            # Clean up the stored terminal instance if it exists
            if hasattr(connection, '_terminal_instance'):
                delattr(connection, '_terminal_instance')
            return
            
        # Get the terminal instance either from active_terminals or the stored instance
        terminal = self.active_terminals.get(connection) or getattr(connection, '_terminal_instance', None)
        if not terminal:
            logger.warning("No terminal instance found for reconnection")
            return

        # Ensure the tab for this connection is focused so the user can
        # observe the reconnection process even if another tab was
        # previously active.
        try:
            self._focus_most_recent_tab(connection)
        except Exception:
            pass

        # Set controlled reconnect flag
        self._is_controlled_reconnect = True


        
        try:
            # Disconnect first (defer to avoid blocking)
            logger.debug("Disconnecting terminal before reconnection")
            def _safe_disconnect():
                try:
                    terminal.disconnect()
                    logger.debug("Terminal disconnected, scheduling reconnect")
                    # Store the connection temporarily in active_terminals if not present
                    if connection not in self.active_terminals:
                        self.active_terminals[connection] = terminal
                    # Reconnect after disconnect completes
                    GLib.timeout_add(1000, self._reconnect_terminal, connection)  # Increased delay
                except Exception as e:
                    logger.error(f"Error during disconnect: {e}")
                    GLib.idle_add(self._show_reconnect_error, connection, str(e))
                return False
            
            # Defer disconnect to avoid blocking the UI thread
            GLib.idle_add(_safe_disconnect)
            
        except Exception as e:
            logger.error(f"Error during reconnection: {e}")
            # Remove from active terminals if reconnection fails
            if connection in self.active_terminals:
                del self.active_terminals[connection]
                
            # Show error to user
            error_dialog = Gtk.MessageDialog(
                transient_for=self,
                modal=True,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text=_("Reconnection Failed"),
                secondary_text=_("Failed to reconnect with the new settings. Please try connecting again manually.")
            )
            error_dialog.present()
            
        finally:
            # Clean up the stored terminal instance
            if hasattr(connection, '_terminal_instance'):
                delattr(connection, '_terminal_instance')
                
            # Reset the flag after a delay to ensure it's not set during normal operations
            GLib.timeout_add(1000, self._reset_controlled_reconnect)
    
    def _reset_controlled_reconnect(self):
        """Reset the controlled reconnect flag"""
        self._is_controlled_reconnect = False
    
    def _reconnect_terminal(self, connection):
        """Reconnect a terminal with updated connection settings"""
        if connection not in self.active_terminals:
            logger.warning(f"Connection {connection.nickname} not found in active terminals")
            return False  # Don't repeat the timeout
            
        terminal = self.active_terminals[connection]
        
        try:
            logger.debug(f"Attempting to reconnect terminal for {connection.nickname}")
            
            # Reconnect with new settings
            if not terminal._connect_ssh():
                logger.error("Failed to reconnect with new settings")
                # Show error to user
                GLib.idle_add(self._show_reconnect_error, connection)
                return False
                
            logger.info(f"Successfully reconnected terminal for {connection.nickname}")
            
        except Exception as e:
            logger.error(f"Error reconnecting terminal: {e}", exc_info=True)
            GLib.idle_add(self._show_reconnect_error, connection, str(e))
            
        return False  # Don't repeat the timeout
        
    def _show_reconnect_error(self, connection, error_message=None):
        """Show an error message when reconnection fails"""
        # Remove from active terminals if reconnection fails
        if connection in self.active_terminals:
            del self.active_terminals[connection]
            
        # Update UI to show disconnected state
        if connection in self.connection_rows:
            self.connection_rows[connection].update_status()
        
        # Show error dialog
        error_dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text=_("Reconnection Failed"),
            secondary_text=error_message or _("Failed to reconnect with the new settings. Please try connecting again manually.")
        )
        error_dialog.present()
        
        # Clean up the dialog when closed
        error_dialog.connect("response", lambda d, r: d.destroy())

"""
Terminal Widget for sshPilot
Integrated VTE terminal with SSH connection handling using system SSH client
"""

import os
import logging
import signal
import time
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
from typing import Optional
from .platform_utils import is_flatpak, is_macos
from .terminal_backends import (
    BaseTerminalBackend,
    VTETerminalBackend,
    PyXtermTerminalBackend,
    PyXtermBridgeBackend,
)
from .plugins.api import PluginContext, ProtocolError
from .plugins.registry import protocol_registry

gi.require_version('Gtk', '4.0')
gi.require_version('Vte', '3.91')

gi.require_version('Adw', '1')
from gi.repository import Gtk, GObject, GLib, Vte, Pango, Gdk, Gio, Adw

logger = logging.getLogger(__name__)

# SSHProcessManager and the process_manager singleton were extracted to
# ssh_process_manager.py (GTK-free). Re-exported here so existing
# `from .terminal import SSHProcessManager` / `process_manager` callers keep working.
from .ssh_process_manager import SSHProcessManager, process_manager  # noqa: F401
from .terminal_color_utils import (
    mix_rgba,
    clone_rgba,
    relative_luminance,
    get_contrast_color,
)
from .terminal_search import TerminalSearch
from .terminal_fullscreen import FullscreenController


# Substrings (lowercased) in terminal output that mean an ssh attempt is failing.
# Used to gate CONNECTING→CONNECTED promotion (never promote while one is present)
# and to recover a precise failure reason at exit time.
_SSH_FAILURE_MARKERS = (
    'permission denied',
    'connection refused',
    'no route to host',
    'network is unreachable',
    'could not resolve',
    'name or service not known',
    'nodename nor servname',
    'host key verification failed',
    'connection timed out',
    'operation timed out',
    'too many authentication failures',
    'connection reset',
    'connection closed',
    'broken pipe',
    # telnet's connect failure ("telnet: Unable to connect to remote host")
    'unable to connect',
)

# Positive login evidence — appears in ssh -v output or login banners once the
# session is actually established.
_SSH_SUCCESS_MARKERS = (
    'authenticated to',
    'entering interactive session',
    'last login:',
    'pseudo-terminal will',
)

# Line-start prefixes that are ssh's own chatter (not remote shell output). A
# line that doesn't start with one of these is treated as real remote output.
_SSH_NOISE_PREFIXES = (
    'debug',
    'ssh:',
    'warning:',
    'openssh',
    'kex',
    'channel ',
    'authenticated',
    'connecting to',
    'pledge',
    # telnet's pre-connect chatter ("Trying 10.0.0.5...")
    'trying ',
)


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
        # Per-terminal authoritative lifecycle state. The connection-level state
        # is aggregated from all its terminals by
        # ``window._recompute_connection_state``. ``is_connected`` stays as the
        # boolean compat view (True only when CONNECTED).
        from .connection_manager import ConnectionState
        self.connection_state = ConnectionState.UNKNOWN
        self.connection_state_reason = ''
        self._connect_grace_timer_id = None  # evidence poller: promotes CONNECTING→CONNECTED
        self._connect_poll_count = 0
        self._connect_failure_hint = ''  # failure line scraped while connecting
        self.session_id = str(id(self))  # Unique ID for this session
        self._is_quitting = False  # Flag to suppress signal handlers during quit
        self.last_error_message = None  # Store last SSH error for reporting
        self._last_error_detail = None  # Structured context for the Details dialog
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

        # Fullscreen (state + window juggling) lives in a composed controller.
        self._fullscreen = FullscreenController(self)

        # Register with process manager
        process_manager.register_terminal(self)
        
        # Connect to signals
        self.connect('destroy', self._on_destroy)
        
        # Connect to connection manager signals using GObject.GObject.connect directly
        self._connection_updated_handler = GObject.GObject.connect(connection_manager, 'connection-updated', self._on_connection_updated_signal)
        logger.debug("Connected to connection-updated signal")
        
        # Create scrolled window for terminal
        self.scrolled_window = Gtk.ScrolledWindow()
        # Horizontal policy NEVER so VTE always reflows its column count to the
        # available width instead of showing a horizontal scrollbar at narrow
        # sizes (matches gnome-terminal / ptyxis). Vertical stays AUTOMATIC for
        # the scrollback buffer.
        self.scrolled_window.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scrolled_window.set_overlay_scrolling(True)
        
        # Create backend first before setup
        self._shortcut_controller = None
        self._scroll_controller = None
        self._config_handler = None
        self._supported_encodings = None
        self._updating_encoding_config = False
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
            if hasattr(self.backend, "ensure_shell_loaded"):
                self.backend.ensure_shell_loaded()
        self.overlay = Gtk.Overlay()
        self.overlay.set_child(self.scrolled_window)

        # Search overlay (widgets + state + backend driving) live in a composed
        # object; TerminalWidget keeps thin forwarders for the external API.
        self._search = TerminalSearch(self)
        self.search_revealer = self._search.search_revealer

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
        # Float search revealer over the terminal so toggling it does not
        # change the terminal's allocated height (which would cause VTE to
        # send a resize/SIGWINCH and make the content flicker).
        self.overlay.add_overlay(self.search_revealer)

        self.terminal_stack = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.terminal_stack.set_hexpand(True)
        self.terminal_stack.set_vexpand(True)
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
        self.disconnected_banner.set_hexpand(True)
        self.disconnected_banner.set_vexpand(False)
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

        # Details button — opens a dialog with the full error report (and Copy).
        self.error_details_button = Gtk.Button.new_with_label(_('Details'))
        try:
            self.error_details_button.add_css_class('reconnect-button')
        except Exception:
            pass
        self.error_details_button.connect('clicked', lambda *_: self._show_error_details_dialog())
        self.disconnected_banner.append(self.error_details_button)

        # Dismiss button to hide the banner manually
        self.dismiss_button = Gtk.Button.new_with_label(_('Dismiss'))
        try:
            self.dismiss_button.add_css_class('flat')
            self.dismiss_button.add_css_class('reconnect-button')
        except Exception:
            pass
        self.dismiss_button.connect('clicked', lambda *_: self._set_disconnected_banner_visible(False))
        self.disconnected_banner.append(self.dismiss_button)

        # The banner now lives in the layout flow and uses its natural (compact)
        # height. Height-matching to the sidebar toolbar was only needed when it
        # floated as an overlay; keep the no-op so existing callers in window.py
        # stay harmless without inflating the banner.
        self._banner_css_provider = None
        def _apply_external_height(new_h: int):
            return
        self.set_banner_height = _apply_external_height

        # Wrap the banner in a Revealer (hidden by default; an error reveals it
        # with a slide-up animation). The revealer sits in the layout flow BELOW
        # the terminal (not as an overlay child) so revealing it makes room for
        # the banner — pushing the terminal up — instead of floating over and
        # masking the bottom rows of output. It only appears on disconnect/error,
        # so the brief reflow is fine.
        self.disconnected_revealer = Gtk.Revealer()
        self.disconnected_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.disconnected_revealer.set_transition_duration(200)
        self.disconnected_revealer.set_halign(Gtk.Align.FILL)
        self.disconnected_revealer.set_hexpand(True)
        self.disconnected_revealer.set_vexpand(False)
        self.disconnected_revealer.set_reveal_child(False)
        self.disconnected_revealer.set_child(self.disconnected_banner)
        self.terminal_stack.append(self.disconnected_revealer)

        # Container for terminal stack only
        self.container_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.container_box.set_hexpand(True)
        self.container_box.set_vexpand(True)
        self.container_box.append(self.terminal_stack)

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
        self._fullscreen.setup_shortcut()
        
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

        # PyXterm.js is the embedded (in-process PTY bridge) backend — no server.
        # "pyxterm2" is kept as an alias for any config that set it during testing.
        if backend_name in ("pyxterm", "pyxterm2"):
            try:
                backend = PyXtermBridgeBackend(self)
                if getattr(backend, "available", False):
                    logger.info("Using PyXterm.js embedded terminal backend")
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

    def _set_disconnected_banner_visible(self, visible: bool, message: Optional[str] = None):
        try:
            # Allow callers (e.g., ssh-copy-id dialog) to suppress the red banner entirely
            if getattr(self, '_suppress_disconnect_banner', False):
                return
            if message:
                self.disconnected_banner_label.set_text(message)
            # The Revealer owns visibility now (animated slide up/down).
            if getattr(self, 'disconnected_revealer', None) is not None:
                self.disconnected_revealer.set_reveal_child(visible)
            elif hasattr(self.disconnected_banner, 'set_visible'):
                self.disconnected_banner.set_visible(visible)
        except Exception:
            pass

    # --- Error detail (banner Details dialog) -------------------------------
    _ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')

    def _record_error_detail(self, reason: str, exit_code=None) -> None:
        """Snapshot everything we know about the current failure for the Details
        dialog. Reads only data already available (no extra ssh calls)."""
        try:
            conn = getattr(self, 'connection', None)
            tail = self._ANSI_RE.sub('', self._scrape_recent_terminal_text(4000) or '').strip()
            self._last_error_detail = {
                'nickname': getattr(conn, 'nickname', '') or '',
                'host': getattr(conn, 'hostname', '') or getattr(conn, 'host', '') or '',
                'username': getattr(conn, 'username', '') or '',
                'reason': reason or '',
                'exit_code': exit_code,
                'raw': self.last_error_message or '',
                'hint': getattr(self, '_connect_failure_hint', '') or '',
                'stderr_tail': tail,
            }
        except Exception:
            logger.debug("Failed to record error detail", exc_info=True)

    def _format_error_detail(self, detail=None) -> str:
        """Build the paste-ready report. Leads with reason + raw ssh output; the
        numeric exit code is a small trailing line (255 is just a catch-all)."""
        detail = detail or self._last_error_detail or {}
        nick = detail.get('nickname') or _('Connection')
        user = detail.get('username') or ''
        host = detail.get('host') or ''
        target = f"{user}@{host}" if user and host else (host or user)
        lines = []
        lines.append(f"{_('Connection')}: {nick}" + (f" ({target})" if target else ""))
        if detail.get('reason'):
            lines.append(f"{_('Reason')}: {detail['reason']}")
        # Prefer the explicit error/hint line if present and not already the reason.
        err = detail.get('raw') or detail.get('hint') or ''
        if err and err.strip() and err.strip() != (detail.get('reason') or '').strip():
            lines.append(f"{_('Error')}: {err.strip()}")
        tail = detail.get('stderr_tail') or ''
        if tail:
            lines.append("")
            lines.append(_('--- SSH output ---'))
            lines.append(tail)
        code = detail.get('exit_code')
        if code is not None:
            lines.append("")
            lines.append(f"ssh exit: {code}")
        return "\n".join(lines).strip() or _('No additional details available.')

    def _show_error_details_dialog(self) -> None:
        """Popup with the full, selectable error report plus a Copy button."""
        try:
            text = self._format_error_detail()
            root = self.get_root() if hasattr(self, 'get_root') else None

            body = Gtk.Label(label=text)
            body.set_selectable(True)
            body.set_wrap(True)
            body.set_xalign(0)
            body.set_yalign(0)
            body.add_css_class('monospace')
            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            scroller.set_min_content_width(480)
            scroller.set_min_content_height(260)
            scroller.set_child(body)

            dialog = Adw.MessageDialog(
                transient_for=root if isinstance(root, Gtk.Window) else None,
                modal=True,
                heading=_('Connection Error Details'),
            )
            dialog.set_extra_child(scroller)
            dialog.add_response('copy', _('Copy'))
            dialog.add_response('close', _('Close'))
            dialog.set_default_response('close')
            dialog.set_close_response('close')

            def _on_response(dlg, response):
                if response == 'copy':
                    try:
                        clipboard = self.get_clipboard()
                        clipboard.set(self._format_error_detail())
                    except Exception:
                        logger.debug("Failed to copy error detail", exc_info=True)
                dlg.close()
            dialog.connect('response', _on_response)
            dialog.present()
        except Exception:
            logger.debug("Failed to show error details dialog", exc_info=True)

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
                    self._record_error_detail(_('Reconnect failed to start'))
                    self._set_disconnected_banner_visible(True, _('Reconnect failed to start'))
                    return False

                if not self._connect_ssh():
                    # Show banner again if failed to start reconnect
                    self._set_connecting_overlay_visible(False)
                    self._record_error_detail(_('Reconnect failed to start'))
                    self._set_disconnected_banner_visible(True, _('Reconnect failed to start'))
                return False

            GLib.idle_add(_prepare_and_connect)
        except Exception:
            self._set_connecting_overlay_visible(False)
            self._record_error_detail(_('Reconnect failed'))
            self._set_disconnected_banner_visible(True, _('Reconnect failed'))

    def _refresh_connection_command(self) -> bool:
        """Refresh the prepared SSH command using current preferences."""

        connection = getattr(self, 'connection', None)
        if not connection:
            logger.error('Reconnect requested without an active connection')
            return False

        if getattr(connection, 'protocol', 'ssh') != 'ssh':
            # Plugin protocols rebuild their command statelessly in
            # build_spawn(); there is no prepared SSH command to refresh.
            return True

        try:
            if hasattr(connection, 'ssh_cmd'):
                connection.ssh_cmd = []
            # Drop the cached builder result so reconnect re-derives the command,
            # environment, and auth (a stale askpass/agent decision must not leak).
            if hasattr(connection, 'ssh_connection_cmd'):
                connection.ssh_connection_cmd = None
        except Exception as exc:
            logger.debug(f"Unable to reset cached ssh_cmd before reconnect: {exc}")

        # Native-only connection (connect() delegates to native_connect()).
        connect_coro = None
        try:
            if hasattr(connection, 'native_connect'):
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
            pre_cmd = ''
            try:
                pre_cmd = (getattr(self.connection, 'pre_command', '') or '').strip()
                if not pre_cmd and hasattr(self.connection, 'data'):
                    pre_cmd = (self.connection.data.get('pre_command') or '').strip()
            except Exception:
                pre_cmd = ''
            if pre_cmd:
                logger.info(f"Running pre-connection command: {pre_cmd}")
                try:
                    result = subprocess.run(
                        pre_cmd,
                        shell=True,
                        timeout=30,
                    )
                    if result.returncode != 0:
                        logger.warning(f"Pre-connection command exited with code {result.returncode}: {pre_cmd}")
                except subprocess.TimeoutExpired:
                    logger.warning(f"Pre-connection command timed out: {pre_cmd}")
                except Exception as pre_exc:
                    logger.warning(f"Pre-connection command failed: {pre_exc}")

            # Preload this host's key(s) into ssh-agent before spawning ssh so a
            # passphrased key locked in gnome-keyring gets unlocked and can sign
            # (the agent is never disabled). Done here, on the connect worker
            # thread, so the GLib main loop stays free and OUR askpass dialog can
            # render for a not-stored passphrase. Best-effort; never blocks spawn.
            # SSH-only: plugin protocols have no agent/keys.
            if getattr(self.connection, 'protocol', 'ssh') == 'ssh':
                try:
                    preload = getattr(self.connection, '_preload_keys_into_agent', None)
                    if callable(preload):
                        preload(self.config)
                except Exception as preload_exc:
                    logger.debug(f"Key preload skipped/failed: {preload_exc}")

            GLib.idle_add(self._setup_ssh_terminal)
        except Exception as e:
            logger.error(f"SSH connection failed: {e}")
            GLib.idle_add(self._on_connection_failed, str(e))

    def _setup_ssh_terminal(self):
        """Set up terminal with direct SSH command using ssh_connection_builder (called from main thread)"""
        # Shutdown race guard: the connect worker schedules this via idle_add, so
        # it can run after cleanup_all() marked this terminal quitting (or the
        # window began closing). Spawning SSH now leaks a process past shutdown.
        root = self.get_root() if hasattr(self, 'get_root') else None
        if getattr(self, '_is_quitting', False) or getattr(root, '_is_quitting', False):
            logger.debug("Terminal/window quitting, skipping SSH spawn")
            return
        try:
            # The spawn command comes from the connection's protocol backend
            # (sshpilot.plugins). For SSH ("plugin zero") this is a pure
            # indirection over the same prepared build_ssh_connection() result
            # that Connection.native_connect()/connect() produced, so argv/env
            # are identical to consuming connection.ssh_connection_cmd directly.
            # The terminal handles only the runtime mechanics that cannot live
            # in a pure command builder: askpass log forwarding (passphrase +
            # login password from resolve_native_auth), terminal env tweaks,
            # and the PTY/spawn.
            # NOTE: self.backend is the *terminal* backend (VTE vs fallback);
            # protocol backends are a different axis.
            proto = getattr(self.connection, 'protocol', 'ssh')
            protocol_backend = protocol_registry().get_or_none(proto)
            working_dir = None
            # Fail closed: a non-SSH connection whose backend isn't registered
            # (plugin disabled / failed to load / API mismatch) must not silently
            # fall back to an ssh invocation.
            if (protocol_backend is None and proto != 'ssh'
                    and getattr(self.connection, 'ssh_connection_cmd', None) is None):
                GLib.idle_add(
                    self._on_connection_failed,
                    _("No backend for protocol '{}'. The plugin may be disabled, "
                      "failed to load, or targets a different API version.").format(proto))
                return
            if protocol_backend is not None:
                # Scope the spawn context to the plugin that registered the
                # protocol so a backend's ctx.settings/secrets resolve to its
                # own namespace. host=None: build_spawn must not use ui/events.
                pid = protocol_registry().plugin_id_for(proto) or 'core'
                plugin_ctx = PluginContext.for_spawn(
                    plugin_id=pid,
                    app_config=self.config,
                    connection_manager=self.connection_manager,
                    protocol_registry=protocol_registry(),
                )
                try:
                    spec = protocol_backend.build_spawn(self.connection, plugin_ctx)
                except ProtocolError as e:
                    GLib.idle_add(self._on_connection_failed, str(e))
                    return
                ssh_cmd = list(spec.argv)
                env = dict(spec.env)
                working_dir = spec.working_directory
                use_askpass = bool(spec.extras.get('use_askpass'))
                use_sshpass = bool(spec.extras.get('use_sshpass'))
                password_value = spec.extras.get('password')
            elif (ssh_conn_cmd := getattr(self.connection, 'ssh_connection_cmd', None)) is not None:
                # No backend registered (plugin system unavailable): consume the
                # prepared command directly, exactly as before the plugin seam.
                ssh_cmd = list(ssh_conn_cmd.command)
                env = dict(ssh_conn_cmd.env)
                use_askpass = bool(getattr(ssh_conn_cmd, 'use_askpass', False))
                use_sshpass = bool(getattr(ssh_conn_cmd, 'use_sshpass', False))
                password_value = ssh_conn_cmd.password
            else:
                # Fallback: a bare prepared command list from an older path, or a
                # minimal ssh invocation as a last resort.
                prepared = getattr(self.connection, 'ssh_cmd', None)
                if isinstance(prepared, (list, tuple)) and prepared:
                    ssh_cmd = list(prepared)
                else:
                    ssh_cmd = ['ssh']
                env = os.environ.copy()
                use_askpass = False
                use_sshpass = False
                password_value = None

            # Route identity/agent env injection through the selected identity provider
            # (default: system ssh-agent), so all injection goes through one seam and
            # honors the configured default. Idempotent over the inherited environment.
            from .identity import get_identity_manager
            env = get_identity_manager().apply_selected_to_env(env)

            # Remember whether a stored password was supplied this attempt, so an
            # auth failure can say "saved password rejected" rather than a generic
            # "authentication failed". Delivery is via askpass (not sshpass/PTY).
            self._used_stored_password = bool(password_value)

            logger.debug(f"SSH command from builder: {' '.join(ssh_cmd)}")

            # Auth secrets (login password + key passphrase) are delivered by
            # SSH_ASKPASS from resolve_native_auth — REQUIRE=prefer so MFA/OTP
            # prompts declined by the helper appear on this terminal's TTY.
            # Do not wrap with sshpass or arm PTY password autofill here.
            if use_sshpass:
                logger.debug(
                    "Ignoring use_sshpass on terminal spawn; askpass owns password delivery"
                )

            # Forward askpass helper log lines into our logger while connecting so
            # passphrase/password-prompt activity is visible. Only new lines are
            # forwarded (the log file persists for the whole session).
            if use_askpass:
                self._enable_askpass_log_forwarding(include_existing=False)

            # Terminal-specific environment tweaks.
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
                    cwd=working_dir or os.path.expanduser('~') or '/',
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
                    self._fallback_to_askpass(ssh_cmd, env_list, working_dir)
                else:
                    self._on_connection_failed(str(e))
                return
            except Exception as e:
                logger.error(f"VTE spawn failed with exception: {e}")
                self._on_connection_failed(str(e))
                return
            
            # Store the PTY for later cleanup
            self.pty = pty

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
    
    def _fallback_to_askpass(self, ssh_cmd, env_list, working_dir=None):
        """Fallback when sshpass fails - allow interactive prompting"""
        try:
            logger.info("Falling back to interactive password prompt")

            # Remove sshpass from the command
            if ssh_cmd and ssh_cmd[0] == 'sshpass':
                ssh_cmd = ssh_cmd[3:]  # Remove sshpass, -f, and fifo_path

            # Strip any askpass variables from the environment list, then force never
            env_list = [e for e in env_list if not e.startswith('SSH_ASKPASS=') and not e.startswith('SSH_ASKPASS_REQUIRE=')]
            env_list.append('SSH_ASKPASS_REQUIRE=never')

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
                cwd=working_dir or os.path.expanduser('~') or '/',
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

        # Arm the one-shot PTY auto-fill (e.g. answer a remote sudo prompt).
        try:
            self._install_pty_autofill()
        except Exception:
            logger.debug("Could not arm PTY auto-fill", exc_info=True)

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
                if hasattr(self.connection, 'hostname') and self.connection.hostname == 'localhost':
                    command_type = 'bash'
                else:
                    command_type = getattr(self.connection, 'protocol', 'ssh') or 'ssh'
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

            # The ssh process spawned — but that only means the subprocess
            # started, NOT that it authenticated or reached the host. Enter
            # CONNECTING and promote to CONNECTED only on real login evidence
            # (remote termprops via _on_termprops_changed) or, failing that, if
            # the process is still alive after a short grace period. A fast
            # failure (auth/refused/unreachable) exits first and is classified
            # as FAILED, so the indicator never flashes green on a dead link.
            from .connection_manager import ConnectionState
            is_remote = (
                hasattr(self, 'connection') and self.connection
                and getattr(self.connection, 'hostname', None) != 'localhost'
            )
            if is_remote and hasattr(self, 'connection_manager') and self.connection_manager:
                self.connection_state = ConnectionState.CONNECTING
                self.connection_state_reason = ''
                self.is_connected = False
                # Don't downgrade a connection that already has a live terminal.
                if self.connection.get_status() != ConnectionState.CONNECTED:
                    self.connection_manager.update_connection_state(
                        self.connection, ConnectionState.CONNECTING
                    )
                self._start_connect_grace()
                logger.debug(f"Terminal {self.session_id} entered CONNECTING")
            else:
                # Local terminal (or no manager): a shell with no auth step, so
                # a successful spawn is a successful connection.
                self.connection_state = ConnectionState.CONNECTED
                self.is_connected = True
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
        """Fallback for the Flatpak case where the spawn-complete callback never
        fires. Promotes only on real evidence — never merely because the process
        is alive (a still-connecting socket is alive but not connected)."""
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

        from .connection_manager import ConnectionState
        if self.connection_state == ConnectionState.CONNECTED:
            return False

        verdict = self._scan_connect_evidence()
        if verdict == 'connected':
            logger.debug("Spawn-complete didn't fire; promoting on evidence (fallback)")
            self._mark_connected()
        elif verdict == 'pending':
            # Keep evaluating instead of force-promoting an unconfirmed session.
            if self.connection_state != ConnectionState.CONNECTING:
                self.connection_state = ConnectionState.CONNECTING
            self._start_connect_grace()
        # 'failed' → leave it; the child-exit handler will classify FAILED.
        return False  # Don't repeat the timer

    # --- Connection lifecycle helpers (Phase 3 gating) ----------------------
    def _scan_connect_evidence(self):
        """Inspect recent terminal output and decide whether the CONNECTING
        session has real evidence of being connected, failing, or still pending.

        Returns one of 'connected', 'failed', 'pending'. This is what replaces
        the old "process is alive" heuristic — a socket stuck in the TCP connect
        phase is alive but produces no remote output, so it stays 'pending'.
        """
        text = (self._scrape_recent_terminal_text(4000) or '').lower()
        if not text:
            return 'pending'

        # A visible ssh failure means the attempt is dying — never promote.
        for marker in _SSH_FAILURE_MARKERS:
            if marker in text:
                # Remember the matching line so the exit reason is precise even
                # if the final bytes aren't in the buffer when ssh exits.
                for line in text.splitlines():
                    if marker in line:
                        self._connect_failure_hint = line.strip()
                        break
                return 'failed'

        # Explicit positive evidence (ssh -v progress / login banner).
        if any(marker in text for marker in _SSH_SUCCESS_MARKERS):
            return 'connected'

        # Any line that isn't ssh's own chatter is remote output (a shell prompt
        # or MOTD) — strong evidence the session is live.
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(_SSH_NOISE_PREFIXES):
                continue
            return 'connected'

        return 'pending'

    def _start_connect_grace(self):
        """Start the evidence poller that promotes CONNECTING→CONNECTED once the
        terminal shows real remote output (prompt/title/banner). It never
        promotes on liveness alone, so a connecting-but-not-connected socket
        stays CONNECTING until it either produces output or the attempt exits."""
        self._cancel_connect_grace()
        self._connect_poll_count = 0
        self._connect_failure_hint = ''
        # Poll once a second; termprops usually promotes title-setting shells
        # instantly, so this is the backup path for quieter servers.
        self._connect_grace_timer_id = GLib.timeout_add(
            1000, self._on_connect_grace_elapsed
        )
        # Embedded backend: also scan for evidence on every PTY output batch, so
        # promotion happens in milliseconds rather than on the 1 s poll tick.
        backend = getattr(self, 'backend', None)
        if backend is not None and hasattr(backend, 'add_output_hook'):
            try:
                backend.add_output_hook(self._on_bridge_connect_evidence)
            except Exception:
                logger.debug("Could not register connect-evidence output hook", exc_info=True)

    def _cancel_connect_grace(self):
        backend = getattr(self, 'backend', None)
        if backend is not None and hasattr(backend, 'remove_output_hook'):
            try:
                backend.remove_output_hook(self._on_bridge_connect_evidence)
            except Exception:
                pass
        if getattr(self, '_connect_grace_timer_id', None):
            try:
                GLib.source_remove(self._connect_grace_timer_id)
            except Exception:
                pass
            self._connect_grace_timer_id = None

    def _on_connect_grace_elapsed(self):
        # Repeating timer: return True to keep polling, False to stop.
        from .connection_manager import ConnectionState
        if getattr(self, '_is_quitting', False) or self.connection_state != ConnectionState.CONNECTING:
            self._connect_grace_timer_id = None
            return False

        verdict = self._scan_connect_evidence()
        if verdict == 'connected':
            logger.debug(f"Terminal {self.session_id}: remote output observed, marking connected")
            self._connect_grace_timer_id = None
            self._mark_connected()
            return False
        if verdict == 'failed':
            # Stop polling; the child-exit handler classifies it as FAILED.
            self._connect_grace_timer_id = None
            return False

        # 'pending' — keep waiting, but don't poll forever.
        self._connect_poll_count += 1
        if self._connect_poll_count >= 60:  # ≈60s, well past typical ConnectTimeout
            self._connect_grace_timer_id = None
            # Defense-in-depth: if the ssh child is still alive this long with no
            # recorded failure, it is connected (auth/network failures exit well
            # before now). Promote rather than leaving the icon stuck on
            # "connecting" forever when neither termprops nor scraping fired.
            child_alive = False
            try:
                child_alive = bool(self.backend and self.backend.get_child_pid())
            except Exception:
                child_alive = False
            if child_alive and not self.last_error_message:
                logger.debug(f"Terminal {self.session_id}: grace elapsed, child alive — marking connected")
                self._mark_connected()
            else:
                logger.debug(f"Terminal {self.session_id}: no connect evidence after grace window; staying pending")
            return False
        return True

    def _mark_connected(self):
        """Promote a CONNECTING session to CONNECTED (idempotent). Called only on
        real evidence: termprops, or remote output seen by the evidence poller."""
        from .connection_manager import ConnectionState
        if self.connection_state == ConnectionState.CONNECTED:
            return
        self._cancel_connect_grace()
        if getattr(self, '_fallback_timer_id', None):
            try:
                GLib.source_remove(self._fallback_timer_id)
            except Exception:
                pass
            self._fallback_timer_id = None

        self.connection_state = ConnectionState.CONNECTED
        self.connection_state_reason = ''
        self.is_connected = True
        self.last_error_message = None

        if (
            hasattr(self, 'connection') and self.connection
            and getattr(self.connection, 'hostname', None) != 'localhost'
            and hasattr(self, 'connection_manager') and self.connection_manager
        ):
            self.connection_manager.update_connection_state(
                self.connection, ConnectionState.CONNECTED
            )
            logger.debug(f"Terminal {self.session_id} promoted to CONNECTED")

        self.emit('connection-established')
        self._set_connecting_overlay_visible(False)
        try:
            self._set_disconnected_banner_visible(False)
        except Exception:
            pass

    def _scrape_recent_terminal_text(self, max_chars=2000):
        """Best-effort read of the terminal tail, used only to classify a failure
        reason when ssh wrote its error to the PTY (not to last_error_message)."""
        try:
            if getattr(self, 'backend', None) is not None and hasattr(self.backend, 'get_content'):
                return self.backend.get_content(max_chars) or ''
        except Exception:
            pass
        return ''

    # -- one-shot PTY auto-fill (sudo prompt, ssh password on key-based auth) ----
    def arm_password_pty_autofill(self, password: str) -> None:
        """Queue a one-shot fill for ssh's password prompt (``classify_prompt``).

        Call before spawn (or before ``_install_pty_autofill``). Residual prompts
        such as 2FA stay in the terminal for the user. Safe to call from SCP /
        ssh-copy-id paths that spawn on a TerminalWidget without going through
        ``_setup_ssh_terminal``.
        """
        from .askpass_utils import classify_prompt

        fills = getattr(self, '_pty_autofills', None)
        if fills is None:
            fills = self._pty_autofills = []
        fills.insert(0, (
            lambda text: classify_prompt(text) == 'password',
            password,
        ))

    def _install_pty_autofill(self):
        """Arm a watcher that types a canned response the first time a known
        prompt appears in the terminal. Used for a remote ``sudo`` password
        prompt or ssh's own password prompt: the secret travels through the
        encrypted PTY exactly as if typed, never on a command line. Fills come
        from the ``_pty_autofills`` queue of ``(matcher, response)`` entries
        (matcher: substring or callable over the scraped tail) and/or the legacy
        single-slot ``_pty_autofill`` tuple; no-op when neither is set."""
        autofill = getattr(self, '_pty_autofill', None)
        if (not getattr(self, '_pty_autofills', None)
                and (not autofill or not autofill[0])):
            return
        self._pty_autofill_done = False
        vte = getattr(self, 'vte', None)
        if vte is None:
            # No Vte.Terminal (embedded PyXterm backend): drive the same one-shot
            # watcher from the backend's PTY output stream instead of VTE's
            # 'contents-changed' signal. get_content()/feed_child() work there too.
            backend = getattr(self, 'backend', None)
            if backend is not None and hasattr(backend, 'add_output_hook'):
                try:
                    backend.add_output_hook(self._pty_autofill_tick)
                    self._pty_autofill_timeout_id = GLib.timeout_add_seconds(
                        30, self._cancel_pty_autofill)
                except Exception:
                    logger.debug("Could not arm PTY auto-fill via backend", exc_info=True)
            return
        try:
            self._pty_autofill_handler = vte.connect(
                'contents-changed', self._on_pty_autofill_changed)
        except Exception:
            logger.debug("Could not connect PTY auto-fill watcher", exc_info=True)
            return
        # Safety: give up after 30s so we never linger or leak the handler if the
        # prompt never shows (e.g. cached sudo credentials, wrong command).
        self._pty_autofill_timeout_id = GLib.timeout_add_seconds(
            30, self._cancel_pty_autofill)

    def _on_pty_autofill_changed(self, _vte):
        fills = getattr(self, '_pty_autofills', None) or []
        legacy = (None if getattr(self, '_pty_autofill_done', True)
                  else getattr(self, '_pty_autofill', None))
        if not fills and not legacy:
            return False
        text = self._scrape_recent_terminal_text(max_chars=4000) or ''

        def _matches(matcher):
            try:
                return matcher(text) if callable(matcher) else (matcher in text)
            except Exception:
                return False

        # Fire at most one fill per output batch (a single trailing prompt can
        # only be one prompt), queued fills first — the ssh password prompt
        # precedes any post-login prompt like sudo's.
        response = None
        for entry in list(fills):
            if entry[0] and _matches(entry[0]):
                fills.remove(entry)
                response = entry[1]
                break
        if response is None and legacy and legacy[0] and _matches(legacy[0]):
            self._pty_autofill = None
            self._pty_autofill_done = True
            response = legacy[1]
        if response is None:
            return False
        try:
            data = (response + '\n').encode('utf-8')
            if getattr(self, 'backend', None) is not None and hasattr(self.backend, 'feed_child'):
                self.backend.feed_child(data)
            elif getattr(self, 'vte', None) is not None:
                self.vte.feed_child(data)
        except Exception:
            logger.debug("PTY auto-fill feed failed", exc_info=True)
        if not fills and (getattr(self, '_pty_autofill_done', True)
                          or not getattr(self, '_pty_autofill', None)):
            self._cancel_pty_autofill()
        return False

    def _cancel_pty_autofill(self):
        """Disconnect the auto-fill watcher and drop the cached responses."""
        self._pty_autofill_done = True
        self._pty_autofill = None
        self._pty_autofills = None
        handler_id = getattr(self, '_pty_autofill_handler', None)
        if handler_id:
            try:
                vte = getattr(self, 'vte', None)
                if vte is not None:
                    vte.disconnect(handler_id)
            except Exception:
                pass
            self._pty_autofill_handler = None
        # Embedded PyXterm backend: stop the output-driven watcher.
        backend = getattr(self, 'backend', None)
        if backend is not None and hasattr(backend, 'remove_output_hook'):
            try:
                backend.remove_output_hook(self._pty_autofill_tick)
            except Exception:
                pass
        tid = getattr(self, '_pty_autofill_timeout_id', None)
        if tid:
            try:
                GLib.source_remove(tid)
            except Exception:
                pass
            self._pty_autofill_timeout_id = None
        return False  # one-shot GLib timeout

    def _pty_autofill_tick(self):
        """Output-hook adapter for the embedded backend's auto-fill watcher."""
        self._on_pty_autofill_changed(None)

    def _on_bridge_connect_evidence(self):
        """Embedded-backend connect-evidence scan, driven by each PTY output batch
        (registered as an output hook while CONNECTING). Promotes to CONNECTED the
        instant real remote output appears, instead of waiting for the 1 s poller.
        Uses the same evidence matcher as the poller, so ssh's own local-side
        chatter never falsely promotes; failures are left to the poller/child-exit."""
        from .connection_manager import ConnectionState
        if self.connection_state != ConnectionState.CONNECTING:
            return
        if self._scan_connect_evidence() == 'connected':
            self._mark_connected()

    def handle_backend_title(self, title):
        """Handle an OSC 0/2 title from the embedded backend: update the tab title
        and, like VTE's termprops path, promote a CONNECTING remote session (a
        remote shell setting its title is login evidence)."""
        if not title:
            # Empty title events (e.g. an OSC 0/2 clear during init) are not
            # evidence — ignore them entirely so they can't promote prematurely.
            return
        try:
            remote_dir = self._parse_directory_from_title(title)
            if remote_dir:
                self._current_remote_directory = remote_dir
            self.emit('title-changed', title)
        except Exception:
            logger.debug("handle_backend_title: title update failed", exc_info=True)
        # A non-empty remote title is login evidence — promote a CONNECTING remote
        # session, mirroring VTE's termprops path.
        try:
            from .connection_manager import ConnectionState
            if self.connection_state == ConnectionState.CONNECTING and not self._is_local_terminal():
                self._mark_connected()
        except Exception:
            pass

    def _classify_exit(self, exit_code, was_connected, extra_text=''):
        """Map an ssh exit into (ConnectionState, reason) from the exit code and
        the captured error text. Distinguishes auth/unreachable failures from a
        clean disconnect or a dropped-after-connected session."""
        from .connection_manager import ConnectionState
        # Include any failure line the connect-evidence poller captured, so the
        # precise reason survives even if ssh's final output isn't in the buffer
        # by the time the child-exit handler scrapes it.
        msg = (
            f"{self.last_error_message or ''}\n"
            f"{extra_text or ''}\n"
            f"{getattr(self, '_connect_failure_hint', '') or ''}"
        ).lower()

        if 'permission denied' in msg or 'authentication failed' in msg \
                or 'too many authentication failures' in msg:
            # If we fed a stored password and the server still denied access, the
            # saved password is almost certainly the culprit — say so, so the user
            # knows to fix it instead of staring at a generic message. (Not for the
            # "too many authentication failures" case, which is about offered keys.)
            # But a saved password is only "supplied" when ssh actually asked for
            # one: ssh's final denial lists the methods the server accepts, and
            # "Permission denied (publickey)" means no password prompt ever fired
            # (wrong key, cancelled MFA, …) — don't blame the saved password then.
            if getattr(self, '_used_stored_password', False) \
                    and ('permission denied' in msg or 'authentication failed' in msg):
                methods = re.search(r'permission denied \(([^)]*)\)', msg)
                if methods is None or 'password' in methods.group(1) \
                        or 'keyboard-interactive' in methods.group(1):
                    return ConnectionState.FAILED, 'Saved password rejected'
            return ConnectionState.FAILED, 'Authentication failed'
        if 'connection refused' in msg:
            return ConnectionState.FAILED, 'Connection refused'
        if 'no route to host' in msg or 'network is unreachable' in msg:
            return ConnectionState.FAILED, 'Host unreachable'
        if 'could not resolve' in msg or 'name or service not known' in msg \
                or 'nodename nor servname' in msg:
            return ConnectionState.FAILED, 'Host not found'
        if 'host key verification failed' in msg:
            return ConnectionState.FAILED, 'Host key verification failed'
        if 'connection timed out' in msg or 'operation timed out' in msg:
            return ConnectionState.FAILED, 'Connection timed out'
        if 'timeout, server' in msg or 'timed out waiting' in msg:
            # ServerAlive keepalive gave up on a previously-live session.
            if was_connected:
                return ConnectionState.DISCONNECTED, 'Connection lost'
            return ConnectionState.FAILED, 'Connection timed out'

        # ssh's own fatal errors exit with 255. Plugin protocols don't reserve
        # an exit code: any non-zero exit before a session was established is
        # a failed connection.
        is_ssh = getattr(getattr(self, 'connection', None), 'protocol', 'ssh') == 'ssh'
        if (exit_code == 255 and is_ssh) or (exit_code and not is_ssh):
            if was_connected:
                return ConnectionState.DISCONNECTED, 'Connection lost'
            return ConnectionState.FAILED, (self.last_error_message or 'Connection failed')

        # Other non-zero: a remote shell/command exited after a real session.
        return ConnectionState.DISCONNECTED, ''


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
                cursor_color = get_contrast_color(bg_color)

            highlight_bg_value = profile.get('highlight_background')
            highlight_fg_value = profile.get('highlight_foreground')
            highlight_bg = Gdk.RGBA()
            highlight_fg = Gdk.RGBA()

            if not (highlight_bg_value and highlight_bg.parse(highlight_bg_value)):
                highlight_bg.parse('#4A90E2')

            if not (highlight_fg_value and highlight_fg.parse(highlight_fg_value)):
                highlight_fg = get_contrast_color(highlight_bg)

            override_rgba = self._get_group_color_rgba()
            use_group_color = False

            try:
                use_group_color = bool(
                    self.config.get_setting('ui.use_group_color_in_terminal', False)
                )
            except Exception:
                use_group_color = False

            if use_group_color and override_rgba is not None:
                bg_color = clone_rgba(override_rgba)  # Use exact group color
                fg_color = get_contrast_color(bg_color)

                contrast_for_bg = get_contrast_color(bg_color)
                mix_ratio = 0.35 if relative_luminance(bg_color) < 0.5 else 0.25
                highlight_bg = mix_rgba(bg_color, contrast_for_bg, mix_ratio)
                highlight_bg.alpha = 1.0
                highlight_fg = get_contrast_color(highlight_bg)
                cursor_color = clone_rgba(fg_color)


            # Prepare palette colors (16 ANSI colors)
            palette_colors = None
            if profile.get('palette'):
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

            self._applied_background_color = clone_rgba(bg_color)
            self._applied_cursor_color = clone_rgba(cursor_color)
            self._applied_highlight_bg = clone_rgba(highlight_bg)
            self._applied_highlight_fg = clone_rgba(highlight_fg)

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
                    # Target TerminalWidget + scrolled child + VTE or PyXterm WebView.
                    provider = Gtk.CssProvider()
                    css = (
                        f"terminalwidget.terminal-bg, "
                        f"terminalwidget.terminal-bg > scrolledwindow.terminal-bg, "
                        f"terminalwidget.terminal-bg > scrolledwindow.terminal-bg > vte-terminal.terminal-bg, "
                        f"terminalwidget.terminal-bg > scrolledwindow.terminal-bg > *.terminal-bg "
                        f"{{ background-color: rgba({int(rgba.red*255)}, {int(rgba.green*255)}, "
                        f"{int(rgba.blue*255)}, {rgba.alpha}); }}"
                    )
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
                backend_widget = getattr(getattr(self, 'backend', None), 'widget', None)
                if backend_widget is not None and hasattr(backend_widget, 'add_css_class'):
                    backend_widget.add_css_class('terminal-bg')
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

    def _apply_cursor_and_selection_colors(self):
        try:
            cursor_color = getattr(self, '_applied_cursor_color', None)
            background_color = getattr(self, '_applied_background_color', None)

            if cursor_color is None and background_color is not None:
                cursor_color = get_contrast_color(background_color)
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
                highlight_fg = get_contrast_color(highlight_bg)

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

                # Scroll behavior: snap to the prompt when the user types, but
                # don't yank the view to the bottom on background output while
                # the user is scrolled up reading scrollback.
                try:
                    if hasattr(self.vte, 'set_scroll_on_keystroke'):
                        self.vte.set_scroll_on_keystroke(True)
                    if hasattr(self.vte, 'set_scroll_on_output'):
                        self.vte.set_scroll_on_output(False)
                except Exception as e:
                    logger.warning(f"Could not set scroll behavior: {e}")

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

                # Enable OSC 8 hyperlink support (links emitted by apps via escape sequences)
                self._hovered_hyperlink_uri = None
                try:
                    if hasattr(self.vte, 'set_allow_hyperlink'):
                        self.vte.set_allow_hyperlink(True)
                        logger.debug("Enabled OSC 8 hyperlink support")
                except Exception as e:
                    logger.warning(f"Could not enable OSC 8 hyperlink support: {e}")

                # Register URL regex so VTE underlines plain-text URLs on hover.
                # PCRE2_MULTILINE (0x00000400) is required by VTE's match_add_regex.
                self._url_regex_tag = -1
                try:
                    PCRE2_MULTILINE = 0x00000400
                    _url_pattern = (
                        r'(?:https?|ftp)://[^\s\t\n\r<>"{}|\\^`\[\]]'
                        r'*[^\s\t\n\r<>"{}|\\^`\[\].,;:!?]'
                    )
                    _url_regex = Vte.Regex.new_for_match(
                        _url_pattern, len(_url_pattern), PCRE2_MULTILINE
                    )
                    self._url_regex_tag = self.vte.match_add_regex(_url_regex, 0)
                    self.vte.match_set_cursor_name(self._url_regex_tag, "pointer")
                    logger.debug(f"Registered URL regex, tag={self._url_regex_tag}")
                except Exception as e:
                    logger.warning(f"Could not register URL regex: {e}")

                # Motion controller – tracks which URL cell the cursor is over,
                # and re-grabs focus on enter so VTE's native hover detection
                # (cursor shape, underline) stays active after other UI elements
                # have taken focus.
                try:
                    _motion_ctrl = Gtk.EventControllerMotion()
                    _motion_ctrl.connect('motion', self._on_vte_motion)
                    _motion_ctrl.connect('enter', self._on_vte_pointer_enter)
                    self.vte.add_controller(_motion_ctrl)
                    self._url_motion_controller = _motion_ctrl
                except Exception as e:
                    logger.warning(f"Could not add URL motion controller: {e}")

                # Nudge VTE to re-evaluate match highlighting whenever new content
                # arrives (e.g. after a paste).  queue_draw() alone won't update
                # VTE's internal m_match_hilite, but it ensures the underline
                # drawn by our manual cursor management below is repainted.
                try:
                    self.vte.connect('contents-changed', lambda t: t.queue_draw())
                except Exception as e:
                    logger.warning(f"Could not connect contents-changed: {e}")

                # Copy-on-select: when enabled in preferences, mirror the
                # selection into the clipboard automatically.
                try:
                    self.vte.connect('selection-changed', self._on_selection_changed)
                except Exception as e:
                    logger.warning(f"Could not connect selection-changed: {e}")

                # Key controller – tracks Ctrl state for Ctrl+click URL opening.
                # Reading it from the click event is unreliable; a dedicated key
                # controller is more robust.
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

    def _on_vte_pointer_enter(self, controller, x, y):
        """Called when the pointer enters the VTE widget area.

        Cursor shape is managed explicitly in _on_vte_motion via set_cursor(),
        so VTE does not need keyboard focus for hover detection.  VTE's own
        EventControllerMotion fires for the widget under the pointer regardless
        of focus — just like GNOME Terminal, which underlines URLs even without
        focus.

        Previously this called grab_focus() to restore VTE's native cursor-
        shape mechanism, but that triggered a focus-in event which cleared
        VTE's internal m_match_hilite hover state, breaking URL underlines
        after paste.  Now that cursor shape is driven from Python (set_cursor),
        the grab_focus is not needed here.
        """

    def _vte_uri_at(self, x: float, y: float) -> Optional[str]:
        """Return the URI at widget coordinates, or None.

        Prefer OSC 8 hyperlinks over regex matches (same precedence as GNOME
        Terminal). Uses the coordinate APIs from
        https://api.pygobject.gnome.org/Vte-3.91/class-Terminal.html —
        ``check_hyperlink_at`` / ``check_match_at`` (since 0.70), with
        ``match_check`` as a fallback for older VTE.
        """
        if self.vte is None:
            return None

        # OSC 8 explicit hyperlinks
        if hasattr(self.vte, 'check_hyperlink_at'):
            try:
                uri = self.vte.check_hyperlink_at(x, y)
                if uri:
                    return uri
            except Exception:
                pass

        # Plain-text regex matches registered via match_add_regex
        if hasattr(self.vte, 'check_match_at'):
            try:
                result = self.vte.check_match_at(x, y)
                if result:
                    candidate = result[0] if isinstance(result, (tuple, list)) else result
                    if candidate:
                        return candidate
            except Exception as e:
                logger.debug(f"check_match_at error: {e}")
        elif hasattr(self.vte, 'match_check'):
            try:
                char_width = self.vte.get_char_width()
                char_height = self.vte.get_char_height()
                if char_width > 0 and char_height > 0:
                    result = self.vte.match_check(int(x / char_width), int(y / char_height))
                    if result:
                        candidate = result[0] if isinstance(result, (tuple, list)) else result
                        if candidate:
                            return candidate
            except Exception as e:
                logger.debug(f"match_check error: {e}")
        return None

    @staticmethod
    def _click_has_link_modifier(state) -> bool:
        """True when the click should activate a link (GNOME Terminal: Ctrl+click).

        GNOME Terminal's ``terminal_screen_capture_click_pressed_cb`` only opens
        when ``state & GDK_CONTROL_MASK``. On macOS use Cmd (Meta) instead —
        Ctrl+click is commonly mapped to right-click there.
        """
        if is_macos():
            return bool(state & Gdk.ModifierType.META_MASK)
        return bool(state & Gdk.ModifierType.CONTROL_MASK)

    def _on_vte_motion(self, controller, x, y):
        """Detect URL under the mouse cursor (both OSC 8 links and plain-text regexes)."""
        if self.vte is None:
            return
        try:
            uri = self._vte_uri_at(x, y)
            event = controller.get_current_event()

            # Only update when we have a definitive answer; never clear via a
            # missing event (the cursor may still be on the same link)
            if uri or event:
                self._hovered_hyperlink_uri = uri or None

            # VTE's internal hover state (underline + cursor shape) is only
            # updated when VTE processes its own GDK events.  After a paste the
            # mouse may not have moved, so VTE never runs that path and the
            # visual feedback is missing even though match_check() works fine.
            # Manually driving the widget cursor here gives us an independent
            # fallback that doesn't depend on VTE's internal bookkeeping.
            try:
                if uri:
                    self.vte.set_cursor(Gdk.Cursor.new_from_name("pointer", None))
                elif event:
                    # set_cursor(None) would inherit the parent's default arrow;
                    # restore VTE's own I-beam explicitly so terminal text shows
                    # the text cursor like other terminals.
                    self.vte.set_cursor(Gdk.Cursor.new_from_name("text", None))
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"URL hover error: {e}")

    def _on_open_link_activated(self, action, param):
        """Open the hyperlink that was under the cursor when the context menu was triggered."""
        uri = getattr(self, '_context_menu_hyperlink_uri', None) or getattr(self, '_hovered_hyperlink_uri', None)
        if uri:
            try:
                Gio.AppInfo.launch_default_for_uri(uri, None)
                logger.debug(f"Opened link: {uri}")
            except Exception as e:
                logger.warning(f"Failed to open link '{uri}': {e}")

    def _on_copy_link_activated(self, action, param):
        """Copy the hyperlink to the clipboard."""
        uri = getattr(self, '_context_menu_hyperlink_uri', None) or getattr(self, '_hovered_hyperlink_uri', None)
        if uri:
            try:
                display = Gdk.Display.get_default()
                clipboard = display.get_clipboard()
                clipboard.set(uri)
                logger.debug(f"Copied link to clipboard: {uri}")
            except Exception as e:
                logger.warning(f"Failed to copy link '{uri}': {e}")

    def _do_save_contents(self):
        """Save the terminal's scrollback to a plain-text file.

        Uses Gtk.FileDialog (portal-backed, so it works inside the Flatpak
        sandbox) to pick the destination, then dumps VTE's retained buffer via
        write_contents_sync. Only the in-memory scrollback is available; lines
        scrolled past the scrollback limit are gone.
        """
        if self.vte is None:
            logger.warning("Save Output is only available with the VTE backend")
            return

        # Default file name from the connection nickname when available.
        base = getattr(self.connection, 'nickname', None) or 'terminal'
        safe = ''.join(c if (c.isalnum() or c in '-_.') else '_' for c in str(base)) or 'terminal'

        dialog = Gtk.FileDialog.new()
        dialog.set_title(_("Save Terminal Output"))
        dialog.set_initial_name(f"{safe}.txt")

        def _on_done(dlg, result):
            try:
                gfile = dlg.save_finish(result)
            except GLib.Error:
                return  # user cancelled or portal denied
            if gfile is None:
                return
            stream = None
            try:
                stream = gfile.replace(None, False, Gio.FileCreateFlags.NONE, None)
                self.vte.write_contents_sync(stream, Vte.WriteFlags.DEFAULT, None)
            except GLib.Error as exc:
                logger.error("Failed to save terminal output: %s", exc)
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("Failed to save terminal output: %s", exc, exc_info=True)
            finally:
                if stream is not None:
                    try:
                        stream.close(None)
                    except Exception:
                        pass

        try:
            parent = self.get_root()
        except Exception:
            parent = None
        try:
            dialog.save(parent, None, _on_done)
        except Exception as exc:
            logger.error("Could not open save dialog: %s", exc, exc_info=True)

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
        if isinstance(self.backend, PyXtermTerminalBackend):
            # Applied on spawn (luit wrap); existing sessions need reconnect.
            requested = encoding_value.strip() if isinstance(encoding_value, str) else ''
            if requested:
                logger.debug(
                    "Encoding '%s' will be handled at PTY bridge level for PyXterm.js backend",
                    requested,
                )
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

    def _show_toast(self, message, timeout=3):
        """Show a transient toast in the main window's toast overlay."""
        root = self.get_root()
        try:
            toast = Adw.Toast.new(message)
            toast.set_timeout(timeout)
        except Exception:
            return

        try:
            if root and hasattr(root, 'toast_overlay') and root.toast_overlay is not None:
                root.toast_overlay.add_toast(toast)
            elif root and hasattr(root, 'add_toast'):
                root.add_toast(toast)
        except Exception:
            pass

    def _has_terminal_selection(self) -> bool:
        """Whether the terminal currently has a text selection.

        Used to decide if a copy actually put something on the clipboard. The
        backend reports it when it can (VTE); a backend that can't answer
        synchronously (PyXterm) is treated optimistically as having a selection.
        """
        try:
            if self.backend is not None:
                getter = getattr(self.backend, 'get_has_selection', None)
                if getter is not None:
                    return bool(getter())
                return True
            if self.vte is not None:
                return bool(self.vte.get_has_selection())
        except Exception:
            pass
        return False

    def _notify_invalid_encoding(self, requested, fallback):
        message = _(f"Encoding '{requested}' is not supported. Using {fallback} instead.")
        logger.warning(message)
        self._show_toast(message)

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
                        # Use get_width()/get_height() instead of deprecated get_allocated_width()/get_allocated_height()
                        # (deprecated since GTK 4.12)
                        widget_allocated = False
                        try:
                            allocated_width = widget.get_width()
                            allocated_height = widget.get_height()
                            
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
                                # Use get_width()/get_height() instead of deprecated get_allocated_width()/get_allocated_height()
                                # (deprecated since GTK 4.12)
                                widget_allocated = False
                                try:
                                    if widget_to_connect:
                                        allocated_width = widget_to_connect.get_width()
                                        allocated_height = widget_to_connect.get_height()
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
            
            # Environment for agent. Route env injection through the selected identity
            # provider so child processes (e.g. ssh run from this shell) reach the
            # user's ssh-agent via the same seam as SSH connections.
            from .identity import get_identity_manager
            env = get_identity_manager().apply_selected_to_env(os.environ.copy())
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
        # Route env injection through the selected identity provider (one seam for all
        # SSH_AUTH_SOCK injection); idempotent over the inherited environment.
        from .identity import get_identity_manager
        env = get_identity_manager().apply_selected_to_env(os.environ.copy())

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
            # Idempotent: drop any controllers/popover from a prior setup pass so we
            # don't stack duplicate gestures (which open two menus per right-click).
            self._teardown_context_menu()
            self._menu_controller_registry = []
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

            # Open Link / Copy Link actions
            act_open_link = Gio.SimpleAction.new("open_link", None)
            act_open_link.connect("activate", self._on_open_link_activated)
            self._menu_actions.add_action(act_open_link)
            self._context_menu_hyperlink_uri = None

            act_copy_link = Gio.SimpleAction.new("copy_link", None)
            act_copy_link.connect("activate", self._on_copy_link_activated)
            self._menu_actions.add_action(act_copy_link)

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

            act_search = Gio.SimpleAction.new("search", None)
            act_search.connect("activate", lambda a, p: self._show_search_overlay(select_all=True))
            self._menu_actions.add_action(act_search)

            act_save = Gio.SimpleAction.new("save_contents", None)
            act_save.connect("activate", lambda a, p: self._do_save_contents())
            self._menu_actions.add_action(act_save)

            self.insert_action_group('term', self._menu_actions)

            # Menu model with keyboard shortcuts
            self._menu_model = Gio.Menu()
            self._link_section_in_menu = False

            # Link section is built once and inserted/removed dynamically so
            # "Open Link" and "Copy Link" are completely hidden when no URL is
            # under the cursor.  PopoverMenu tracks GMenuModel::items-changed
            # and rebuilds before popup() is called.
            self._link_menu = Gio.Menu()
            self._link_menu.append(_("Open Link"), "term.open_link")
            self._link_menu.append(_("Copy Link"), "term.copy_link")

            if is_macos():
                self._menu_model.append(_("Copy\t⌘C"), "term.copy")
                self._menu_model.append(_("Paste\t⌘V"), "term.paste")
                self._menu_model.append(_("Select All\t⌘A"), "term.select_all")
                zoom_section = Gio.Menu()
                zoom_section.append(_("Zoom In\t⌘="), "term.zoom_in")
                zoom_section.append(_("Zoom Out\t⌘-"), "term.zoom_out")
                zoom_section.append(_("Reset Zoom\t⌘0"), "term.reset_zoom")
                self._menu_model.append_section(None, zoom_section)
                search_section = Gio.Menu()
                search_section.append(_("Search\t⌘F"), "term.search")
                search_section.append(_("Save Output…"), "term.save_contents")
                self._menu_model.append_section(None, search_section)
            else:
                self._menu_model.append(_("Copy\tCtrl+Shift+C"), "term.copy")
                self._menu_model.append(_("Paste\tCtrl+Shift+V"), "term.paste")
                self._menu_model.append(_("Select All\tCtrl+Shift+A"), "term.select_all")
                zoom_section = Gio.Menu()
                zoom_section.append(_("Zoom In\tCtrl++"), "term.zoom_in")
                zoom_section.append(_("Zoom Out\tCtrl+-"), "term.zoom_out")
                zoom_section.append(_("Reset Zoom\tCtrl+0"), "term.reset_zoom")
                self._menu_model.append_section(None, zoom_section)
                search_section = Gio.Menu()
                search_section.append(_("Search\tCtrl+Shift+F"), "term.search")
                search_section.append(_("Save Output…"), "term.save_contents")
                self._menu_model.append_section(None, search_section)

            # Popover parent + dismissal strategy.
            #
            # A grabbing (autohide) GtkPopover cannot establish its input grab over a
            # WebKit WebView (the PyXterm.js backend) on Wayland — GDK logs "Tried to
            # map a grabbing popup with a non-top most parent" and retries every frame,
            # so the menu maps *without* a working grab and never sees a click-outside.
            # That's why it only closes by activating an item.
            #
            # For the WebView backend, disable autohide (no grab, no warning) and drive
            # dismissal manually: on the next press in the terminal, on focus-out, and
            # on Escape. VTE is a normal widget where autohide works, so leave it.
            self._menu_popover = Gtk.PopoverMenu.new_from_model(self._menu_model)
            self._menu_popover.set_has_arrow(True)
            if self.vte is not None:
                parent_widget = self.vte
            else:
                parent_widget = self.scrolled_window or (
                    self.backend.widget if self.backend else self.terminal_widget
                )
            if parent_widget:
                self._menu_popover.set_parent(parent_widget)
            self._menu_parent_widget = parent_widget

            self._menu_needs_manual_dismiss = self.vte is None
            if self._menu_needs_manual_dismiss:
                self._menu_popover.set_autohide(False)
                self._install_manual_menu_dismissal(parent_widget)

            # Right-click gesture to open the context menu (BUBBLE phase is fine for right-click)
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
                    # A non-autohide menu (WebView backend) must be dismissed on the
                    # next press in the terminal — this is the common "click away".
                    if getattr(self, '_menu_needs_manual_dismiss', False):
                        self._dismiss_context_menu()
                    if btn not in (Gdk.BUTTON_SECONDARY, 3):
                        logger.debug(f"Not a right-click button: {btn}")
                        return
                    # Paste-on-right-click: when enabled, a plain right-click
                    # pastes the clipboard; Shift+right-click still opens the menu.
                    try:
                        paste_on_rc = bool(
                            self.config.get_setting('terminal.paste_on_right_click', False)
                        )
                    except Exception:
                        paste_on_rc = False
                    shift_held = False
                    try:
                        state = gest.get_current_event_state()
                        shift_held = bool(state & Gdk.ModifierType.SHIFT_MASK)
                    except Exception:
                        shift_held = False
                    if paste_on_rc and not shift_held:
                        gest.set_state(Gtk.EventSequenceState.CLAIMED)
                        try:
                            if self.backend:
                                self.backend.grab_focus()
                        except Exception:
                            pass
                        self.paste_text()
                        return
                    # Stop event propagation to prevent other context menus
                    gest.set_state(Gtk.EventSequenceState.CLAIMED)
                    # Focus terminal first for reliable copy/paste
                    try:
                        if self.backend:
                            self.backend.grab_focus()
                    except Exception:
                        pass
                    # Show or hide the link section based on whether a URL is
                    # under the cursor.  Insert/remove from the live model so
                    # the items are completely absent (not just greyed out).
                    try:
                        uri = getattr(self, '_hovered_hyperlink_uri', None)
                        self._context_menu_hyperlink_uri = uri
                        has_link = bool(uri)
                        in_menu = getattr(self, '_link_section_in_menu', False)
                        if has_link and not in_menu:
                            self._menu_model.insert_section(0, None, self._link_menu)
                            self._link_section_in_menu = True
                        elif not has_link and in_menu:
                            self._menu_model.remove(0)
                            self._link_section_in_menu = False
                    except Exception:
                        pass
                    # Position popover near the click. The gesture is on the backend
                    # widget; when the popover is parented elsewhere (the scrolling
                    # container, for the WebView backend) translate the point into the
                    # parent's coordinate space.
                    try:
                        px, py = x, y
                        try:
                            src = gest.get_widget()
                            dest = getattr(self, '_menu_parent_widget', None)
                            if src is not None and dest is not None and src is not dest:
                                ok, tx, ty = src.translate_coordinates(dest, x, y)
                                if ok:
                                    px, py = tx, ty
                        except Exception:
                            pass
                        rect = Gdk.Rectangle()
                        rect.x = int(px)
                        rect.y = int(py)
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
            # Add gesture to the backend widget (VTE or WebView), recorded so a repeat
            # setup pass removes it instead of stacking a duplicate.
            if self.backend and self.backend.widget:
                self._register_menu_controller(self.backend.widget, gesture)
                logger.debug(f"Added context menu gesture to backend widget: {type(self.backend).__name__}")
            elif self.vte is not None:
                self._register_menu_controller(self.vte, gesture)
                logger.debug("Added context menu gesture to VTE widget")
            elif self.terminal_widget is not None:
                self._register_menu_controller(self.terminal_widget, gesture)
                logger.debug("Added context menu gesture to terminal widget")

            # CAPTURE-phase left-click gesture for Ctrl+click URL opening
            # (GNOME Terminal: terminal_screen_capture_click_pressed_cb requires
            # GDK_CONTROL_MASK). Must use CAPTURE so it runs before VTE's
            # text-selection handler; we only claim when the modifier is held
            # AND a URL is under the cursor.
            if self.vte is not None:
                url_gesture = Gtk.GestureClick()
                url_gesture.set_button(Gdk.BUTTON_PRIMARY)
                url_gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
                def _on_url_click(gest, n_press, x, y):
                    try:
                        if n_press != 1:
                            return
                        # Plain click must reach VTE for cursor placement /
                        # selection — only Ctrl+click (Cmd+click on macOS)
                        # activates links, matching GNOME Terminal.
                        try:
                            state = gest.get_current_event_state()
                        except Exception:
                            return
                        if not self._click_has_link_modifier(state):
                            return

                        uri = self._vte_uri_at(x, y)
                        if not uri:
                            return  # no URL here – let VTE handle the click normally

                        gest.set_state(Gtk.EventSequenceState.CLAIMED)
                        Gio.AppInfo.launch_default_for_uri(uri, None)
                        logger.debug(f"Opened URL via Ctrl/Cmd+click: {uri}")
                    except Exception as e:
                        logger.warning(f"URL click failed: {e}")
                url_gesture.connect('pressed', _on_url_click)
                self._register_menu_controller(self.vte, url_gesture)
                self._url_click_gesture = url_gesture
                logger.debug("Added CAPTURE-phase Ctrl+click URL gesture to VTE widget")

            logger.debug("Terminal context menu setup completed successfully")
        except Exception as e:
            logger.error(f"Context menu setup failed: {e}")

    def _dismiss_context_menu(self):
        """Hide the context menu popover if it is showing (manual-dismiss backends)."""
        popover = getattr(self, '_menu_popover', None)
        if popover is None:
            return
        try:
            if popover.get_visible():
                popover.popdown()
        except Exception:
            pass

    def _register_menu_controller(self, widget, controller):
        """Attach a context-menu controller and record it so a later teardown can
        remove exactly what was added (no wrong-widget remove_controller warnings)."""
        if widget is None or controller is None:
            return
        try:
            widget.add_controller(controller)
            self._menu_controller_registry.append((widget, controller))
        except Exception:
            logger.debug("Failed to add context-menu controller", exc_info=True)

    def _teardown_context_menu(self):
        """Remove previously-installed context-menu controllers/popover.

        ``setup_terminal()`` (hence ``_setup_context_menu``) can run more than once on
        the same backend widget — e.g. prewarm adoption sets the terminal up, then
        ``setup_local_shell`` sets it up again. Without this, each pass stacked another
        gesture on the same widget, so a single right-click opened two popovers."""
        for widget, controller in getattr(self, '_menu_controller_registry', []):
            try:
                widget.remove_controller(controller)
            except Exception:
                pass
        self._menu_controller_registry = []
        self._menu_gesture = None
        self._menu_focus_controller = None
        self._menu_key_controller = None
        popover = getattr(self, '_menu_popover', None)
        if popover is not None:
            try:
                popover.popdown()
            except Exception:
                pass
            try:
                popover.set_parent(None)
            except Exception:
                pass
            self._menu_popover = None

    def _install_manual_menu_dismissal(self, focus_widget):
        """Dismiss the non-autohide WebView context menu on focus-out and Escape.

        The next in-terminal press is handled by the context-menu gesture itself;
        this covers clicking to another widget/app (focus leaves the terminal) and
        the Escape key. A focus-out is honored only after confirming (on idle) that
        focus did not move *into* the popover, so opening the menu can't self-close.
        """
        try:
            focus_ctl = Gtk.EventControllerFocus()

            def _on_leave(_c):
                def _maybe_close():
                    try:
                        pop = getattr(self, '_menu_popover', None)
                        if pop is None or not pop.get_visible():
                            return False
                        root = pop.get_root()
                        focus = root.get_focus() if root is not None else None
                        inside = bool(focus is not None and (focus is pop or focus.is_ancestor(pop)))
                        if not inside:
                            pop.popdown()
                    except Exception:
                        pass
                    return False
                GLib.idle_add(_maybe_close)

            focus_ctl.connect("leave", _on_leave)
            self._register_menu_controller(focus_widget, focus_ctl)
            self._menu_focus_controller = focus_ctl
        except Exception:
            logger.debug("Could not attach context-menu focus controller", exc_info=True)

        try:
            key_ctl = Gtk.EventControllerKey()

            def _on_key(_c, keyval, _keycode, _state):
                if keyval == Gdk.KEY_Escape:
                    self._dismiss_context_menu()
                return False

            key_ctl.connect("key-pressed", _on_key)
            self._register_menu_controller(focus_widget, key_ctl)
            self._menu_key_controller = key_ctl
        except Exception:
            logger.debug("Could not attach context-menu key controller", exc_info=True)

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
                        had_selection = self._has_terminal_selection()
                        result = _schedule_vte_action(self.backend.copy_clipboard)
                        if had_selection:
                            self._show_toast(_("Copied to clipboard"))
                        return result
                    elif self.vte is not None:
                        if not self.vte.get_has_selection():
                            return False
                        result = _schedule_vte_action(self.vte.copy_clipboard_format, Vte.Format.TEXT)
                        self._show_toast(_("Copied to clipboard"))
                        return result
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
            self._search._ensure_search_key_controller()
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

        if getattr(self, '_search', None) is not None:
            self._search.teardown_key_controller()

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

            # Reaping is left to VTE's GLib child-watch source (it spawned this
            # child); waitpid() here would make GLib's waitid() fail with ECHILD
            # and emit a GLib-WARNING (fatal under G_DEBUG=fatal-warnings).
            return True

        except Exception as e:
            logger.error(f"Error terminating process {pid}: {e}")
            return False
    
    def disconnect(self):
        """Close the SSH connection and clean up resources"""
        # Guard UI emissions when the root window is quitting. Computed up front
        # so it is always bound: disconnect() can be called while is_connected is
        # already False (e.g. pressing Reconnect after a failed connection), in
        # which case the block below is skipped but the finally clause still
        # references is_quitting.
        root = self.get_root() if hasattr(self, 'get_root') else None
        is_quitting = bool(getattr(root, '_is_quitting', False))

        if self.is_connected:
            logger.debug(f"Disconnecting SSH session {self.session_id}...")
            self.is_connected = False

            # Only update manager / UI if not quitting
            if hasattr(self, 'connection') and self.connection and not is_quitting:
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

        # Cancel any pending promotion so we never mark this as successful.
        if getattr(self, '_fallback_timer_id', None):
            try:
                GLib.source_remove(self._fallback_timer_id)
            except Exception:
                pass
            self._fallback_timer_id = None
        self._cancel_connect_grace()

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

            # Mark the connection FAILED so the sidebar reflects it (classify the
            # message into a concise reason where we can).
            from .connection_manager import ConnectionState
            _state, _reason = self._classify_exit(255, False)
            self.connection_state = ConnectionState.FAILED
            self.connection_state_reason = _reason or error_message
            if hasattr(self, 'connection_manager') and self.connection_manager and self.connection \
                    and getattr(self.connection, 'hostname', None) != 'localhost':
                self.connection_manager.update_connection_state(
                    self.connection, ConnectionState.FAILED, self.connection_state_reason
                )

            # Notify UI
            self.emit('connection-failed', error_message)

            # Show reconnect banner with the raw SSH error
            self._set_connecting_overlay_visible(False)
            self._record_error_detail(error_message)
            self._set_disconnected_banner_visible(True, error_message)

        except Exception as e:
            logger.error(f"Error in _on_connection_failed: {e}")

    def on_child_exited(self, terminal, status):
        """Handle terminal child process exit"""
        # Skip if terminal is quitting
        if getattr(self, '_is_quitting', False):
            logger.debug("Terminal is quitting, skipping child exit handler")
            return

        # Embedded one-shot command UIs (e.g. ssh-copy-id dialog) handle exit
        # themselves and must not run connection teardown side effects here.
        if getattr(self, '_suppress_connection_exit_handling', False):
            logger.debug("Skipping connection exit handling for embedded command terminal")
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

        # Capture whether the session was ever confirmed connected (before we
        # reset state below) and stop any pending promotion — the process is
        # gone, so it must never be promoted to CONNECTED after this.
        from .connection_manager import ConnectionState
        was_connected = (self.connection_state == ConnectionState.CONNECTED)
        self._cancel_connect_grace()

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
                self.connection_state = ConnectionState.DISCONNECTED
                self.is_connected = False

                # Emit connection status change signal
                if hasattr(self, 'connection_manager') and self.connection_manager and self.connection:
                    GLib.idle_add(
                        self.connection_manager.update_connection_state,
                        self.connection, ConnectionState.DISCONNECTED, '',
                    )
                
                root = self.get_root()
                if root and hasattr(root, 'tab_view'):
                    # Safe lookup: this terminal may be embedded in a split-view
                    # pane (not in the main tab_view), which would otherwise trip
                    # the get_page CRITICAL assertion.
                    if hasattr(root, '_page_for_child'):
                        page = root._page_for_child(self)
                    else:
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
        
        # Non-zero or unknown exit: classify into FAILED (auth/unreachable/…) vs
        # DISCONNECTED (a previously-live session that dropped or ended).
        logger.debug("Updating connection status after process exit")
        # When ssh wrote its error to the PTY rather than to last_error_message
        # (the common case for auth/unreachable failures), scrape the tail so we
        # can still classify the reason.
        scraped = '' if self.last_error_message else self._scrape_recent_terminal_text()
        exit_state, exit_reason = self._classify_exit(exit_code, was_connected, scraped)
        self.connection_state = exit_state
        self.connection_state_reason = exit_reason

        # Don't call disconnect() here since the process has already exited
        # Just update the connection status and emit signals
        self.is_connected = False

        # Update connection manager status with the classified state + reason.
        logger.debug(f"Scheduling connection state update: {exit_state.value} ({exit_reason})")
        if hasattr(self, 'connection_manager') and self.connection_manager and self.connection:
            GLib.idle_add(
                self.connection_manager.update_connection_state,
                self.connection, exit_state, exit_reason or '',
            )

        # Defer all signal emissions and UI updates to prevent deadlocks
        def _finalize_exit_cleanup():
            try:
                logger.debug("Emitting connection-lost signal")
                self.emit('connection-lost')

                # Show reconnect UI with a reason-aware message.
                logger.debug("Updating UI elements")
                self._set_connecting_overlay_visible(False)
                banner_text = self.last_error_message or exit_reason
                if not banner_text:
                    if exit_code and exit_code != 0:
                        banner_text = _('SSH exited with status {code}').format(code=exit_code)
                    else:
                        banner_text = _('Session ended.')
                self._record_error_detail(exit_reason or banner_text, exit_code=exit_code)
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

    def _on_selection_changed(self, *_args):
        """Copy-on-select: mirror the terminal selection into the clipboard when
        the preference is enabled. Silent (no toast — the signal fires on every
        change during a drag-select), and only when a selection actually exists
        (the signal also fires on deselect)."""
        try:
            if not self.config.get_setting('terminal.copy_on_select', False):
                return
            if self.backend and self.backend.get_has_selection():
                self.backend.copy_clipboard()
            elif self.vte is not None and self.vte.get_has_selection():
                self.vte.copy_clipboard_format(Vte.Format.TEXT)
        except Exception:
            logger.debug("copy-on-select failed", exc_info=True)

    def copy_text(self):
        """Copy selected text to clipboard"""
        if self.backend:
            had_selection = self._has_terminal_selection()
            self.backend.copy_clipboard()
            if had_selection:
                self._show_toast(_("Copied to clipboard"))
        elif self.vte is not None:
            if self.vte.get_has_selection():
                self.vte.copy_clipboard_format(Vte.Format.TEXT)
                self._show_toast(_("Copied to clipboard"))

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

    # --- Search: forwarders to the composed TerminalSearch (self._search) ---
    # External code binds these names (window.py, the backend `owner`
    # callbacks, the "search" GAction), so keep them resolving on the widget.

    def _show_search_overlay(self, select_all: bool = False):
        return self._search._show_search_overlay(select_all)

    def _hide_search_overlay(self):
        return self._search._hide_search_overlay()

    def handle_search_result(self, *args, **kwargs):
        return self._search.handle_search_result(*args, **kwargs)

    def handle_search_results(self, *args, **kwargs):
        return self._search.handle_search_results(*args, **kwargs)

    def search_text(self, text, case_sensitive=False, regex=False):
        return self._search.search_text(text, case_sensitive=case_sensitive, regex=regex)

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

            # Login evidence: a remote terminal emitting termprops (title/cwd via
            # the remote shell's OSC sequences) means the session reached the
            # shell. Promote a pending CONNECTING session to CONNECTED here so we
            # confirm on real activity rather than just the process spawning.
            try:
                from .connection_manager import ConnectionState
                if self.connection_state == ConnectionState.CONNECTING \
                        and not self._is_local_terminal():
                    self._mark_connected()
            except Exception:
                pass

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
            # Works for any backend that exposes a real PTY (VTE or the embedded
            # PyXterm bridge), so it is no longer gated on self.vte.
            pty = None
            if self.backend and hasattr(self.backend, 'get_pty'):
                pty = self.backend.get_pty()
            if pty is None and getattr(self, 'vte', None) is not None:
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

    # --- Fullscreen: thin forwarder to the composed FullscreenController ---
    def toggle_fullscreen(self):
        return self._fullscreen.toggle_fullscreen()

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
                    
                    # Generate unique temp file name using timestamp and random number
                    temp_filename = f"/tmp/sshpilot_pwd_{int(time.time())}_{random.randint(1000, 9999)}.txt"
                    
                    # Send pwd command to active terminal session to write current directory to temp file
                    # Use $$ to get shell PID for uniqueness, or use the generated filename
                    pwd_cmd = f"pwd > {temp_filename}\n"
                    
                    logger.debug(f"Sending pwd command to terminal: {pwd_cmd!r}")
                    
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
                        extra_args=[],
                        port_forwarding_rules=None,
                        remote_command=f"cat {temp_filename}",
                        local_command=None,
                        extra_ssh_config=None,
                        known_hosts_path=None,
                        native_mode=True,
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
                            extra_args=[],
                            port_forwarding_rules=None,
                            remote_command=f"rm -f {temp_filename}",
                            local_command=None,
                            extra_ssh_config=None,
                            known_hosts_path=None,
                            native_mode=True,
                        )
                        cleanup_cmd_obj = build_ssh_connection(cleanup_ctx)
                        cleanup_cmd = cleanup_cmd_obj.command
                        subprocess.run(cleanup_cmd, env=cleanup_cmd_obj.env, timeout=2, capture_output=True)
                    except Exception:
                        pass  # Ignore cleanup errors
                    
                    logger.debug(f"pwd file read result: returncode={result.returncode}, stdout={result.stdout!r}, stderr={result.stderr!r}")
                    
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

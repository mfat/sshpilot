"""
Terminal Widget for sshPilot
Integrated VTE terminal for daemon-backed sessions and local shell presentation
"""

from .api.connection_identity import connection_id_for
import os
import logging
import signal
import time
import re
import gi
from gettext import gettext as _
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

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, GObject, GLib, Pango, Gdk, Gio, Adw

logger = logging.getLogger(__name__)

# SSHProcessManager and the process_manager singleton were extracted to
# ssh_process_manager.py (GTK-free). Re-exported here so existing
# `from .terminal import SSHProcessManager` / `process_manager` callers keep working.
from .ssh_process_manager import SSHProcessManager, process_manager  # noqa: F401
from .terminal_search import TerminalSearch
from .core.connection_evidence import classify_connection_evidence


# Installed once per process. Inner padding for the terminal text so the
# prompt doesn't hug the card's rounded left edge (VTE >= 0.76 honors CSS
# padding; the padding area is painted with the terminal background).
_terminal_padding_css_installed = False


def _finish_capture_gesture(gesture, handled: bool) -> None:
    """Resolve a capture gesture without leaving it competing with VTE."""
    state = (
        Gtk.EventSequenceState.CLAIMED
        if handled
        else Gtk.EventSequenceState.DENIED
    )
    gesture.set_state(state)


def _context_click_is_handled(
    button: int,
    *,
    paste_on_right_click: bool,
    shift_held: bool,
    native_vte_menu: bool,
) -> bool:
    """Whether SSH Pilot, rather than the backend, owns this pointer sequence."""
    if button not in (Gdk.BUTTON_SECONDARY, 3):
        return False
    return (paste_on_right_click and not shift_held) or not native_vte_menu


def _context_gesture_button(manual_dismiss: bool) -> int:
    """Observe all PyXterm presses for dismissal, but only VTE right-clicks."""
    return 0 if manual_dismiss else Gdk.BUTTON_SECONDARY


def _link_click_is_handled(
    n_press: int,
    *,
    active: bool,
    modifier_held: bool,
    uri: Optional[str],
) -> bool:
    """Whether a click qualifies for SSH Pilot's link-opening action."""
    return n_press == 1 and active and modifier_held and bool(uri)


def sanitize_local_shell_env(env):
    """Return a copy of *env* stripped of the launching terminal's identity.

    sshPilot inherits the environment of the terminal it was launched from.
    On macOS that includes ``TERM_PROGRAM`` / ``TERM_PROGRAM_VERSION`` /
    ``TERM_SESSION_ID`` pointing at Apple Terminal, so the embedded local
    shell would believe it is running inside Apple Terminal and macOS loads
    its shell-session integration — printing a ``Restored session: ...``
    banner sshPilot never asked for. The shell sshPilot spawns is its own, so
    it is re-identified as ``sshPilot`` instead.

    The input dict is never mutated. Non-macOS hosts are returned as an
    unchanged copy, so no unrelated Linux behavior changes.
    """
    sanitized = dict(env)
    if not is_macos():
        return sanitized
    sanitized.pop("TERM_PROGRAM_VERSION", None)
    sanitized.pop("TERM_SESSION_ID", None)
    sanitized["TERM_PROGRAM"] = "sshPilot"
    return sanitized


def _ensure_terminal_padding_css() -> None:
    global _terminal_padding_css_installed
    if _terminal_padding_css_installed:
        return
    try:
        provider = Gtk.CssProvider()
        # Bare vte-terminal: every VTE in this process is ours. (A subclass
        # does NOT get a CSS node from __gtype_name__ — TerminalWidget's node
        # is plain "box", so scoping via "terminalwidget" matches nothing.)
        #
        # In fullscreen terminal mode (.terminal-fullscreen-mode on the window),
        # remove all padding/margins/borders for true fullscreen experience.
        provider.load_from_data(b"""
vte-terminal {
    padding: 4px 8px;
}

/* Terminal card with margins for windowed mode */
.terminal-card {
    margin: 4px;
}

/* True fullscreen: no margins, padding, or card styling */
.terminal-fullscreen-mode vte-terminal {
    padding: 0;
}

.terminal-fullscreen-mode .terminal-card {
    margin: 0;
    border-radius: 0;
}

.terminal-fullscreen-mode .card {
    background: transparent;
    box-shadow: none;
}
""")
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        _terminal_padding_css_installed = True
    except Exception:  # pragma: no cover - headless/test doubles
        logger.debug("Terminal padding CSS install failed", exc_info=True)


class TerminalWidget(Gtk.Box):
    """A terminal widget for daemon sessions, local shells, and presentation."""
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
        from .connection_model import ConnectionState
        self.connection_state = ConnectionState.UNKNOWN
        self.connection_state_reason = ''
        self._connect_grace_timer_id = None  # evidence poller: promotes CONNECTING→CONNECTED
        self._connect_poll_count = 0
        self._connect_failure_hint = ''  # failure line scraped while connecting
        self.session_id = str(id(self))  # Unique ID for this session
        self._is_quitting = False  # Flag to suppress signal handlers during quit
        self._destroyed = False  # Flag to suppress VTE interactions during teardown
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

        # Daemon session support
        self._daemon_mode = False
        self._daemon_controller = None
        self._daemon_tab_state = None
        self._daemon_interaction_dialogs = None
        self._daemon_commit_handler = None
        self._daemon_size_handler = None
        self._daemon_exit_handled = False
        self._view_only_overlay = None
        self._reconnect_handler = None
        self._shell_output_seen = False
        self._shell_output_seen_after_running = False
        self._pending_shell_ready_feeds = []

        # Register with process manager
        process_manager.register_terminal(self)

        # Connect to signals
        self.connect('destroy', self._on_destroy)

        # Connect to connection manager signals using connect_after
        self._connection_updated_handler = connection_manager.connect_after('connection-updated', self._on_connection_updated_signal)
        logger.debug("Connected to connection-updated signal")

        # Container for the terminal widget + its scrollbar. NOT a
        # Gtk.ScrolledWindow: VTE's own docs (src/vtegtk.cc) say "you should
        # not place a VteTerminal inside a GtkScrolledWindow container, since
        # they are incompatible... pack the terminal in a horizontal GtkBox
        # together with a GtkScrollbar which uses the GtkAdjustment returned
        # from gtk_scrollable_get_vadjustment()." VteTerminal already
        # implements Gtk.Scrollable and reflows its column count to the
        # available width on its own (matches gnome-terminal / ptyxis; there
        # is never a horizontal scrollbar). Non-Scrollable backends (the
        # WebKit-based PyXterm bridge) are appended without a scrollbar and
        # manage their own internal scrolling — see
        # _set_terminal_container_child().
        self.terminal_container = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self._vte_scrollbar = None

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
        self.terminal_widget = self.backend.widget

        # Search overlay lives in a composed object. Create it BEFORE
        # setup_terminal(): that call runs _install_shortcuts(), which attaches
        # the search key controller via self._search. Widget construction does
        # not depend on backend implementation details, so this ordering keeps
        # Ctrl+F/Ctrl+G/Esc available for every emulator.
        self._search = TerminalSearch(self)
        self.search_revealer = self._search.search_revealer

        # Initialize terminal with basic settings and apply configured theme early
        self.setup_terminal()
        try:
            self.apply_theme()
        except Exception:
            pass

        # Add terminal to its container and to the box via an overlay with a connecting view
        if self.terminal_widget is not None:
            self._set_terminal_container_child(self.terminal_widget)
            if hasattr(self.backend, "ensure_shell_loaded"):
                self.backend.ensure_shell_loaded()
        self.overlay = Gtk.Overlay()
        self.overlay.set_child(self.terminal_container)

        # Connecting overlay elements
        self.connecting_bg = Gtk.Box()
        self.connecting_bg.set_hexpand(True)
        self.connecting_bg.set_vexpand(True)
        # .connecting-bg is defined in the bundled style.css (loaded once at startup).
        if hasattr(self.connecting_bg, 'add_css_class'):
            self.connecting_bg.add_css_class('connecting-bg')

        self.connecting_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.connecting_box.set_halign(Gtk.Align.CENTER)
        self.connecting_box.set_valign(Gtk.Align.CENTER)
        spinner = Gtk.Spinner()
        spinner.start()
        label = Gtk.Label()
        label.set_markup(_('<span color="#FFFFFF">Connecting</span>'))
        self.connecting_box.append(spinner)
        self.connecting_box.append(label)

        self.overlay.add_overlay(self.connecting_bg)
        self.overlay.add_overlay(self.connecting_box)

        self.terminal_stack = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.terminal_stack.set_hexpand(True)
        self.terminal_stack.set_vexpand(True)
        # Search participates in normal layout so both terminal backends get a
        # viewport that excludes the visible controls.  Keep it above the
        # terminal overlay; reconnect and save banners remain below it.
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

        # Optional "Save as new connection" prompt (CLI / ad-hoc). Same layout
        # pattern as the disconnected banner: sibling BELOW the VTE in the
        # vertical stack so revealing it pushes the terminal up instead of
        # masking the bottom rows of output.
        self._save_connection_prompt_on_save = None
        self._save_connection_prompt_on_dismiss = None
        self.save_connection_banner = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.save_connection_banner.set_halign(Gtk.Align.FILL)
        self.save_connection_banner.set_valign(Gtk.Align.END)
        self.save_connection_banner.set_hexpand(True)
        self.save_connection_banner.set_vexpand(False)
        self.save_connection_banner.set_margin_start(0)
        self.save_connection_banner.set_margin_end(0)
        self.save_connection_banner.set_margin_top(0)
        self.save_connection_banner.set_margin_bottom(0)
        try:
            self.save_connection_banner.add_css_class('toolbar')
        except Exception:
            pass

        save_icon = Gtk.Image.new_from_icon_name('document-save-symbolic')
        self.save_connection_banner.append(save_icon)
        self.save_connection_banner_label = Gtk.Label(
            label=_('Save this connection for next time?'))
        self.save_connection_banner_label.set_halign(Gtk.Align.START)
        self.save_connection_banner_label.set_hexpand(True)
        self.save_connection_banner_label.set_wrap(True)
        self.save_connection_banner.append(self.save_connection_banner_label)

        self.save_connection_button = Gtk.Button.new_with_label(
            _('Save as new connection'))
        try:
            self.save_connection_button.add_css_class('suggested-action')
        except Exception:
            pass
        self.save_connection_button.connect(
            'clicked', self._on_save_connection_prompt_save)
        self.save_connection_banner.append(self.save_connection_button)

        self.save_connection_dismiss_button = Gtk.Button.new_with_label(_('Dismiss'))
        try:
            self.save_connection_dismiss_button.add_css_class('flat')
        except Exception:
            pass
        self.save_connection_dismiss_button.connect(
            'clicked', self._on_save_connection_prompt_dismiss)
        self.save_connection_banner.append(self.save_connection_dismiss_button)

        self.save_connection_revealer = Gtk.Revealer()
        self.save_connection_revealer.set_transition_type(
            Gtk.RevealerTransitionType.SLIDE_UP)
        self.save_connection_revealer.set_transition_duration(200)
        self.save_connection_revealer.set_halign(Gtk.Align.FILL)
        self.save_connection_revealer.set_hexpand(True)
        self.save_connection_revealer.set_vexpand(False)
        self.save_connection_revealer.set_reveal_child(False)
        self.save_connection_revealer.set_child(self.save_connection_banner)
        self.terminal_stack.append(self.save_connection_revealer)

        # Container for terminal stack only
        self.container_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.container_box.set_hexpand(True)
        self.container_box.set_vexpand(True)
        self.container_box.append(self.terminal_stack)

        # Rounded-corner card framing the terminal, matching the file manager
        # panes. overflow=HIDDEN clips the VTE content to the rounded corners.
        # Use CSS class for margins so fullscreen mode can override them.
        _ensure_terminal_padding_css()
        self.container_box.add_css_class("card")
        self.container_box.add_css_class("terminal-card")
        self.container_box.set_overflow(Gtk.Overflow.HIDDEN)

        self.append(self.container_box)

        # Files panel (embedded SFTP file manager shown below the terminal);
        # see set_file_panel() / clear_file_panel().
        self._file_panel = None
        self._file_panel_paned = None
        self._file_panel_teardown = None

        # Set expansion properties
        self.terminal_container.set_hexpand(True)
        self.terminal_container.set_vexpand(True)
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
        self.terminal_container.set_visible(True)
        if self.terminal_widget is not None:
            self.terminal_widget.set_visible(True)

        # Show overlay initially
        self._set_connecting_overlay_visible(True)

        logger.debug("Terminal widget initialized")

    def _set_terminal_container_child(self, widget) -> None:
        """Install *widget* as the sole content of ``self.terminal_container``.

        VTE's own docs (src/vtegtk.cc) say a VteTerminal must not be placed
        inside a Gtk.ScrolledWindow; pack it in a Gtk.Box with a
        Gtk.Scrollbar bound to its own vertical Gtk.Adjustment instead
        (VteTerminal implements Gtk.Scrollable). Other backends — the
        WebKit-based PyXterm bridge — don't implement Gtk.Scrollable and
        manage their own internal scrolling, so they're appended with no
        scrollbar. Used both at initial construction and by ensure_backend()
        when swapping backends live.
        """
        child = self.terminal_container.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.terminal_container.remove(child)
            child = next_child
        self._vte_scrollbar = None
        if widget is None:
            return
        self.terminal_container.append(widget)
        if isinstance(widget, Gtk.Scrollable):
            scrollbar = Gtk.Scrollbar(
                orientation=Gtk.Orientation.VERTICAL,
                adjustment=widget.get_vadjustment(),
            )
            scrollbar.set_vexpand(True)
            # Approximates the auto-hiding overlay scrollbar ScrolledWindow
            # gave us for free; a plain Gtk.Scrollbar has no such behavior
            # built in, so this is cosmetic best-effort, not a guarantee of
            # pixel-identical styling.
            scrollbar.add_css_class('overlay-indicator')
            self.terminal_container.append(scrollbar)
            self._vte_scrollbar = scrollbar

    # ── files panel (embedded file manager below the terminal) ──────────────

    def has_file_panel(self) -> bool:
        return self._file_panel is not None

    def set_file_panel(self, panel, teardown=None) -> None:
        """Show *panel* below the terminal in a vertical Gtk.Paned.

        The page child stays this TerminalWidget (the paned is internal), so
        all tab bookkeeping keyed on the page child is unaffected. *teardown*
        is invoked from clear_file_panel() so the caller can dispose the
        embedded file-manager controller synchronously (outside GC).
        """
        if self._file_panel is not None:
            self.clear_file_panel()

        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        paned.set_wide_handle(True)
        paned.set_hexpand(True)
        paned.set_vexpand(True)
        paned.set_shrink_start_child(False)
        paned.set_shrink_end_child(False)
        self.remove(self.container_box)
        paned.set_start_child(self.container_box)
        paned.set_end_child(panel)
        self.append(paned)

        self._file_panel = panel
        self._file_panel_paned = paned
        self._file_panel_teardown = teardown

        def _apply_position() -> bool:
            if self._file_panel_paned is not paned:
                return False  # panel was cleared before allocation
            height = self.get_height()
            if height <= 0:
                return True  # not allocated yet — retry on idle
            paned.set_position(int(height * 0.55))
            return False

        if _apply_position():
            GLib.idle_add(_apply_position)

    def clear_file_panel(self) -> None:
        """Remove the files panel and restore the terminal-only layout."""
        paned = self._file_panel_paned
        teardown = self._file_panel_teardown
        self._file_panel = None
        self._file_panel_paned = None
        self._file_panel_teardown = None
        if paned is None:
            return
        # Dispose the controller while the widget tree is still intact, so the
        # embed's Python 'destroy' handlers never fire during GC (segfault).
        if teardown is not None:
            try:
                teardown()
            except Exception:
                logger.debug("Files panel teardown failed", exc_info=True)
        try:
            # set_*_child(None) instead of unparent(): unparenting a Paned
            # child can silently fail in GTK4 (see split_view._release_paned).
            paned.set_end_child(None)
            paned.set_start_child(None)
            self.remove(paned)
            self.append(self.container_box)
        except Exception:
            logger.debug("Files panel layout restore failed", exc_info=True)

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

        # Daemon input/resize handlers belong to the old backend and must never
        # survive an emulator switch.
        daemon_io_was_installed = bool(
            getattr(self, '_daemon_commit_handler', None) is not None
            or getattr(self, '_daemon_size_handler', None) is not None
        )
        self._uninstall_daemon_backend_io()

        # Disconnect old backend signals
        self._disconnect_backend_signals()

        # Detach the shortcut/scroll/search controllers from the widget that is
        # about to be destroyed. Do this before terminal_widget is repointed,
        # or controller_host() would resolve to the new widget. The
        # setup_terminal() call at the end reinstalls them on the new widget:
        # it runs _apply_pass_through_mode(), which reinstalls whenever
        # _shortcut_controller is None -- which is exactly what this clears.
        self._remove_custom_shortcut_controllers()

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

        # Remove old widget (and its scrollbar, if any) from the container
        if self.terminal_widget is not None:
            try:
                self._set_terminal_container_child(None)
            except Exception:
                pass

        # Create new backend
        self.backend = self._create_backend(backend_name)
        self.terminal_widget = self.backend.widget

        # Add new widget to the container
        if self.terminal_widget is not None:
            self._set_terminal_container_child(self.terminal_widget)
            self.terminal_widget.set_hexpand(True)
            self.terminal_widget.set_vexpand(True)
            self.terminal_widget.set_visible(True)

        # Reconnect signals
        self._connect_backend_signals()
        if daemon_io_was_installed:
            self._install_daemon_backend_io()

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

    def show_save_connection_prompt(self, *, on_save=None, on_dismiss=None):
        """Reveal the in-terminal save-connection bar (layout-flow, not overlay)."""
        self._save_connection_prompt_on_save = on_save
        self._save_connection_prompt_on_dismiss = on_dismiss
        try:
            if getattr(self, 'save_connection_revealer', None) is not None:
                self.save_connection_revealer.set_reveal_child(True)
        except Exception:
            logger.debug('Failed to show save-connection prompt', exc_info=True)

    def hide_save_connection_prompt(self):
        """Hide the save-connection prompt without invoking callbacks."""
        try:
            if getattr(self, 'save_connection_revealer', None) is not None:
                self.save_connection_revealer.set_reveal_child(False)
        except Exception:
            pass
        self._save_connection_prompt_on_save = None
        self._save_connection_prompt_on_dismiss = None

    def _on_save_connection_prompt_save(self, *_args):
        callback = self._save_connection_prompt_on_save
        self.hide_save_connection_prompt()
        if callable(callback):
            try:
                callback(self)
            except Exception:
                logger.debug('Save-connection prompt save callback failed', exc_info=True)

    def _on_save_connection_prompt_dismiss(self, *_args):
        callback = self._save_connection_prompt_on_dismiss
        self.hide_save_connection_prompt()
        if callable(callback):
            try:
                callback(self)
            except Exception:
                logger.debug('Save-connection prompt dismiss callback failed', exc_info=True)

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

            reconnect_handler = getattr(self, '_reconnect_handler', None)
            if callable(reconnect_handler):
                if reconnect_handler(self) is False:
                    self._set_connecting_overlay_visible(False)
                    self._record_error_detail(_('Reconnect failed to start'))
                    self._set_disconnected_banner_visible(
                        True, _('Reconnect failed to start')
                    )
                return

            self._set_connecting_overlay_visible(False)
            self._record_error_detail(_('Daemon reconnect handler is unavailable'))
            self._set_disconnected_banner_visible(True, _('Reconnect failed to start'))
        except Exception:
            self._set_connecting_overlay_visible(False)
            self._record_error_detail(_('Reconnect failed'))
            self._set_disconnected_banner_visible(True, _('Reconnect failed'))

    def _set_connecting_overlay_visible(self, visible: bool):
        try:
            if hasattr(self.connecting_bg, 'set_visible'):
                self.connecting_bg.set_visible(visible)
            if hasattr(self.connecting_box, 'set_visible'):
                self.connecting_box.set_visible(visible)
        except Exception:
            pass

    # Daemon terminal session support

    @property
    def is_daemon_backed(self):
        """Whether this terminal is using daemon-backed SSH."""
        return self._daemon_mode

    @property
    def daemon_tab_state(self):
        """Current daemon terminal tab state."""
        return self._daemon_tab_state

    @property
    def has_input_ownership(self):
        """Whether this terminal has input ownership (daemon mode)."""
        if not self._daemon_mode:
            return True  # Local terminals always have input
        return self._daemon_controller and self._daemon_controller.input_owner

    def rebind_daemon_client(self, client):
        """Push a replaced daemon client into this terminal's session controller.

        Called after a transport reconnect so a deferred callback that fires
        later (e.g. a session-opened success racing the old transport's
        shutdown) uses the live client instead of the closed one."""
        if self._daemon_controller is not None:
            set_client = getattr(self._daemon_controller, "set_client", None)
            if callable(set_client):
                set_client(client)

    def take_input_control(self):
        """Claim input ownership for this daemon attachment when unowned."""
        if not self._daemon_mode or not self._daemon_controller:
            return False
        tab = self._daemon_controller.tab_state
        if not tab.session_id or not tab.attachment_id:
            return False
        if tab.input_owner:
            self._hide_view_only_indicator()
            return True
        try:
            from .api.models.terminal import ClaimTerminalInputRequest

            client = self._daemon_controller._client
            bridge = self._daemon_controller._bridge
            request = ClaimTerminalInputRequest(
                session_id=tab.session_id,
                attachment_id=tab.attachment_id,
            )

            def _claimed(_value):
                self._daemon_controller._tab_state.input_owner = True
                self._hide_view_only_indicator()
                # Resize was silently dropped (no ownership) for as long as
                # this attachment was view-only; sync the daemon to our
                # actual current size now that we can resize again.
                self._resync_daemon_terminal_size()

            def _failed(error):
                logger.info(
                    "Could not claim terminal input ownership code=%s",
                    getattr(getattr(error, "code", None), "value", "denied"),
                )
                self._show_view_only_indicator()

            bridge.submit(
                lambda: client.claim_terminal_input(request),
                on_success=_claimed,
                on_error=_failed,
            )
            return True
        except Exception as error:
            logger.error("Failed to claim daemon input ownership: %s", error)
            return False

    def start_daemon_session(self, client, bridge, connection_id, remote_command=None, force_tty=False):
        """Start daemon-backed SSH session instead of local spawn."""
        # The daemon session runtime resolves the latest connection snapshot;
        # an optional remote command (e.g. docker exec/logs) is carried through
        # OpenSessionRequest so the SSH child runs it after the target host.
        # force_tty forces a remote TTY allocation (-t) so interactive commands
        # like `docker exec -it` get a PTY on the far side.
        try:
            from .daemon_interaction_dialogs import DaemonInteractionDialogs
            from .terminal_session_controller import DaemonTerminalSessionController

            # Mark as daemon mode
            self._daemon_mode = True
            self._daemon_exit_handled = False

            # Create view ID
            view_id = f"gtk-{self.session_id}"

            # Create controller
            self._daemon_controller = DaemonTerminalSessionController(
                client=client,
                bridge=bridge,
                connection_id=connection_id,
                view_id=view_id,
                on_output=self._on_daemon_output,
                on_continuity_lost=self._on_daemon_continuity_lost,
                on_error=self._on_daemon_error,
                on_state_changed=self._update_daemon_connection_state,
            )

            self._daemon_tab_state = self._daemon_controller.tab_state
            self._daemon_interaction_dialogs = DaemonInteractionDialogs(
                client,
                bridge,
                self,
            )

            # Set connecting state
            self.connection_state = self.connection_state.__class__.CONNECTING
            self.connection_state_reason = 'Opening daemon session...'
            self._set_connecting_overlay_visible(True)

            # Get terminal dimensions from the active backend (VTE or PyXterm).
            dimensions = self._daemon_terminal_dimensions()

            # Open session
            self._daemon_controller.open(
                connection_id, dimensions, remote_command=remote_command,
                force_tty=bool(force_tty),
            )

            # Keystrokes + resize via the backend abstraction (not VTE-specific).
            self._install_daemon_backend_io()

            return True

        except Exception as e:
            logger.error(f"Failed to start daemon session: {e}")
            self._uninstall_daemon_backend_io()
            self._daemon_mode = False
            self._daemon_controller = None
            self._daemon_tab_state = None
            self._on_connection_failed(str(e))
            return False

    def attach_daemon_session(
        self,
        client,
        bridge,
        session_id,
        *,
        connection_id=None,
        from_sequence: int = 0,
        request_input: bool = True,
    ):
        """Attach this view to an existing daemon session without opening a new one."""
        try:
            from .daemon_interaction_dialogs import DaemonInteractionDialogs
            from .terminal_session_controller import (
                DaemonTerminalSessionController,
                TerminalSessionState,
            )

            self._daemon_mode = True
            self._daemon_exit_handled = False
            view_id = f"gtk-{self.session_id}"
            resolved_connection_id = connection_id
            if resolved_connection_id is None and self._daemon_tab_state is not None:
                resolved_connection_id = self._daemon_tab_state.connection_id
            if resolved_connection_id is None:

                resolved_connection_id = connection_id_for(
                    self.connection
                )

            self._daemon_controller = DaemonTerminalSessionController(
                client=client,
                bridge=bridge,
                connection_id=resolved_connection_id,
                view_id=view_id,
                on_output=self._on_daemon_output,
                on_continuity_lost=self._on_daemon_continuity_lost,
                on_error=self._on_daemon_error,
                on_state_changed=self._update_daemon_connection_state,
            )
            self._daemon_tab_state = self._daemon_controller.tab_state
            self._daemon_tab_state.session_id = session_id
            self._daemon_tab_state.state = TerminalSessionState.DETACHED
            self._daemon_interaction_dialogs = DaemonInteractionDialogs(
                client,
                bridge,
                self,
            )
            self._daemon_interaction_dialogs.set_session(session_id)
            self.connection_state = self.connection_state.__class__.CONNECTING
            self.connection_state_reason = "Attaching to daemon session..."
            self._set_connecting_overlay_visible(True)
            self._install_daemon_backend_io()
            self._daemon_controller.attach(
                want_output=True,
                request_input=request_input,
                from_sequence=from_sequence,
            )
            return True
        except Exception as error:
            if getattr(getattr(error, "code", None), "value", None) == "session_already_closed":
                logger.info("Discarding stale daemon session attachment")
                self._uninstall_daemon_backend_io()
                self._daemon_mode = False
                self._daemon_controller = None
                self._daemon_tab_state = None
                return False
            logger.error("Failed to attach daemon session: %s", error)
            self._uninstall_daemon_backend_io()
            self._daemon_mode = False
            self._daemon_controller = None
            self._daemon_tab_state = None
            self._on_connection_failed(str(error))
            return False

    def _uninstall_daemon_backend_io(self) -> None:
        """Drop previously installed daemon commit/size handlers (idempotent)."""
        backend = getattr(self, 'backend', None)
        if backend is None:
            self._daemon_commit_handler = None
            self._daemon_size_handler = None
            return
        for attr in ('_daemon_commit_handler', '_daemon_size_handler'):
            handler = getattr(self, attr, None)
            if handler is None:
                continue
            try:
                backend.disconnect(handler)
            except Exception:
                logger.debug("Failed to disconnect daemon backend handler", exc_info=True)
            setattr(self, attr, None)

    def _install_daemon_backend_io(self) -> None:
        """Wire commit/resize through the terminal backend abstraction.

        Idempotent: disconnects any previous handlers first so a second
        attach/start on the same widget cannot stack VTE signal handlers
        (which would duplicate each keystroke to the daemon).
        """
        backend = getattr(self, 'backend', None)
        if backend is None:
            return
        self._uninstall_daemon_backend_io()
        try:
            connect_commit = getattr(backend, 'connect_commit', None)
            if callable(connect_commit):
                self._daemon_commit_handler = connect_commit(self._on_daemon_commit)
        except Exception:
            self._daemon_commit_handler = None
            logger.debug("Failed to connect daemon commit handler", exc_info=True)
        try:
            connect_size = getattr(backend, 'connect_size_changed', None)
            if callable(connect_size):
                self._daemon_size_handler = connect_size(self._on_daemon_size_changed)
        except Exception:
            self._daemon_size_handler = None
            logger.debug("Failed to connect daemon size handler", exc_info=True)

    def _feed_display(self, data: bytes) -> None:
        """Paint bytes on the active terminal display via the backend abstraction."""
        backend = getattr(self, 'backend', None)
        if backend is None:
            raise RuntimeError("No terminal backend to feed display output")
        backend.feed(data)

    def _on_daemon_output(self, data):
        """Handle daemon terminal output."""
        try:
            self._feed_display(data)

            # First real output frame means the remote shell is rendering
            # (banner/prompt), not merely attached.
            if data and not self._shell_output_seen:
                self._shell_output_seen = True
                if not self._daemon_running_gate_active():
                    self._flush_pending_shell_ready_feeds()

            # Authoritative gate: flush only on output observed after the
            # session reached RUNNING (authenticated). RUNNING fires at login,
            # before the remote shell renders its banner — feeding on the
            # transition double-echoes the command (the tty echoes the bytes,
            # then the shell re-echoes them once it reads the buffer).
            if self._daemon_running_gate_active():
                if data and getattr(self._daemon_controller, "session_running", False):
                    self._shell_output_seen_after_running = True
                    self._flush_pending_shell_ready_feeds()

            # Update connection state based on daemon session state
            self._update_daemon_connection_state()

        except Exception as e:
            logger.error(f"Failed to feed daemon output to terminal: {e}")

    def _on_daemon_continuity_lost(self):
        """Handle daemon terminal continuity loss."""
        try:
            # A known-good terminal prefix may end in the middle of an escape
            # sequence.  Reset the emulator parser before displaying local
            # text; the damaged remote stream is permanently halted by the
            # binding and must never resume in this parser state.
            self.backend.reset(False, True)
            marker = b"\r\n[Earlier terminal output is no longer available]\r\n"
            self._feed_display(marker)
        except Exception as e:
            logger.error(f"Failed to feed continuity loss marker: {e}")

    def _on_daemon_error(self, error):
        """Handle daemon terminal errors.

        Binary terminal INPUT_ERROR frames and transient input/resize faults
        must not tear down a live session — that race (attach/STARTING vs early
        keystrokes) was failing connections with "The terminal input was rejected".
        """
        try:
            from .api.errors import ErrorCode

            code = getattr(error, "code", None)
            if code is ErrorCode.TERMINAL_INPUT_BACKPRESSURE:
                logger.warning("Terminal input was not delivered due to backpressure")
                self._show_toast(
                    _("Terminal input was not delivered; please try again."),
                    timeout=5,
                )
                return
            if code in {
                ErrorCode.TERMINAL_INPUT_OWNER_REQUIRED,
                ErrorCode.TERMINAL_ATTACHMENT_REQUIRED,
                ErrorCode.SESSION_INVALID_STATE,
            }:
                logger.info(
                    "Transient daemon terminal I/O error (ignored for connection): %s",
                    error,
                )
                return
            if getattr(error, "retryable", False) and code in {
                ErrorCode.SERVER_BUSY,
                ErrorCode.OPERATION_TIMED_OUT,
            }:
                logger.info(
                    "Retryable daemon terminal error (ignored for connection): %s",
                    error,
                )
                return
        except Exception:
            pass
        logger.error(f"Daemon terminal error: {error}")
        self._on_connection_failed(str(error))

    def _daemon_terminal_dimensions(self):
        """Rows/cols from the active backend for daemon open/resize."""
        from .api.models.terminal import TerminalDimensions

        rows, columns = 24, 80
        backend = getattr(self, 'backend', None)
        if backend is not None:
            try:
                size = backend.get_size()
                rows, columns = int(size[0]), int(size[1])
            except Exception:
                pass
        return TerminalDimensions(
            rows=max(1, min(1000, rows)),
            columns=max(1, min(1000, columns)),
        )

    def _resync_daemon_terminal_size(self):
        """Push the current widget size to the daemon and re-arm polling.

        Called when input ownership is newly granted. A single synchronous
        read here can still race GTK's own layout pass (the widget may not
        have finished settling into its real on-screen size yet), so this
        also invalidates the backend's tick-poll cache — that guarantees a
        redelivery on the very next frame even if this read was stale,
        without waiting for an actual further resize to produce one
        (GH #1164 follow-up).
        """
        try:
            self._daemon_controller.resize(self._daemon_terminal_dimensions())
        except Exception as e:
            logger.debug(f"Failed to sync daemon terminal size: {e}")
        backend = getattr(self, 'backend', None)
        invalidate = getattr(backend, 'invalidate_size_tracking', None)
        if callable(invalidate):
            invalidate()

    def _on_daemon_commit(self, terminal, text, size):
        """Handle backend input commit for daemon terminals."""
        if not self._daemon_controller or not self.has_input_ownership:
            return

        try:
            data = text.encode('utf-8') if isinstance(text, str) else text
            self._daemon_controller.send_input(data)
        except Exception as e:
            logger.error(f"Failed to send input to daemon: {e}")

    def _on_daemon_size_changed(self, terminal, char_width, char_height):
        """Handle backend size change for daemon terminals."""
        if not self._daemon_controller or not self.has_input_ownership:
            return

        try:
            dimensions = self._daemon_terminal_dimensions()
            self._daemon_controller.resize(dimensions)
        except Exception as e:
            logger.error(f"Failed to resize daemon terminal: {e}")

    def _show_view_only_indicator(self):
        """Show non-intrusive view-only indicator for daemon terminals."""
        if self._view_only_overlay:
            return  # Already shown

        try:
            from gi.repository import Gtk

            # Create overlay banner
            self._view_only_overlay = Gtk.InfoBar()
            self._view_only_overlay.set_message_type(Gtk.MessageType.INFO)
            self._view_only_overlay.set_show_close_button(False)

            label = Gtk.Label(label="View only - another user controls this terminal")
            self._view_only_overlay.add_child(label)

            # Add to top of terminal
            self.prepend(self._view_only_overlay)
            self._view_only_overlay.set_visible(True)

        except Exception as e:
            logger.error(f"Failed to show view-only indicator: {e}")

    def _hide_view_only_indicator(self):
        """Hide view-only indicator."""
        if self._view_only_overlay:
            try:
                self.remove(self._view_only_overlay)
                self._view_only_overlay = None
            except Exception as e:
                logger.error(f"Failed to hide view-only indicator: {e}")

    def feed_child_data(self, data):
        """Feed bytes to the active input owner for this terminal.

        Canonical widget-level input API. Daemon-backed SSH routes through
        ``TerminalSessionController.send_input`` (never a local VTE child).
        Local/legacy terminals feed the GTK-owned backend/VTE child.
        """
        if getattr(self, '_daemon_mode', False):
            controller = getattr(self, '_daemon_controller', None)
            if controller is not None:
                if self.has_input_ownership:
                    controller.send_input(data)
                else:
                    self._show_view_only_indicator()
                return
            # Daemon mode without a controller: refuse local child feed so a
            # missing attachment cannot accidentally type into a stale PTY.
            logger.debug("Daemon terminal has no controller; dropping feed")
            return

        # Local / legacy terminal — GTK owns the child process.
        backend = getattr(self, 'backend', None)
        if backend is None:
            raise RuntimeError("No terminal backend for local input")
        if not backend.supports_feature("local_process"):
            from .terminal_backends import TerminalBackendCapabilityError
            raise TerminalBackendCapabilityError(
                "Active terminal backend does not support local process input"
            )
        backend.feed_child_data(data)

    def _daemon_running_gate_active(self) -> bool:
        """Whether the daemon RUNNING signal is authoritative for this tab."""
        controller = getattr(self, "_daemon_controller", None)
        if controller is None:
            return False
        return bool(getattr(controller, "session_events_subscribed", False))

    def _flush_pending_shell_ready_feeds(self) -> None:
        """Send queued shell-ready feeds once the shell is ready and owned."""
        if not self.has_input_ownership:
            return
        pending, self._pending_shell_ready_feeds = (
            self._pending_shell_ready_feeds,
            [],
        )
        for feed in pending:
            try:
                self.feed_child_data(feed)
            except Exception as e:
                logger.error(f"Failed to flush shell-ready feed: {e}")

    def feed_child_data_when_shell_ready(self, data):
        """Feed bytes only once the remote shell is ready.

        Authoritative path (the controller observes daemon session events):
        defer automated feeds until output has been observed after the session
        reached RUNNING — the daemon reports RUNNING once it has authenticated
        (OpenSSH diagnostics / PTY evidence), which precedes the remote shell
        rendering its banner. Feeding before that banner double-echoes the fed
        command (the tty echoes the bytes, then the shell re-echoes them once
        it reads the buffer). User keystrokes bypass this gate through
        ``_on_daemon_commit``, so the password can still be typed while
        automated feeds wait.

        Fallback path (no RUNNING signal — e.g. a restored/detached attach
        that does not subscribe to events): wait for the first rendered output
        frame, as before. Non-daemon terminals feed immediately.
        """
        if not getattr(self, "_daemon_mode", False):
            self.feed_child_data(data)
            return
        if self._daemon_running_gate_active():
            if getattr(self, "_shell_output_seen_after_running", False):
                self._flush_pending_shell_ready_feeds()
                self.feed_child_data(data)
                return
            self._pending_shell_ready_feeds.append(data)
            return
        if getattr(self, "_shell_output_seen", False):
            self.feed_child_data(data)
            return
        self._pending_shell_ready_feeds.append(data)

    def _handle_daemon_close(self, is_quitting=False):
        """Handle close policy for daemon terminals."""
        try:
            from .daemon_terminal_policy import resolve_tab_close_policy, TerminalClosePolicy

            if is_quitting:
                override = getattr(self, "_daemon_quit_close_policy", None)
                if override is not None:
                    policy = override
                else:
                    from .daemon_terminal_policy import resolve_app_close_policy
                    policy = resolve_app_close_policy(self.config)
            else:
                policy = resolve_tab_close_policy(self.config)

            if policy == TerminalClosePolicy.DETACH:
                try:
                    from .daemon_session_restore import DaemonSessionRestoreManager

                    DaemonSessionRestoreManager(self.config).save_session_metadata(
                        self._daemon_tab_state,
                        tab_title=getattr(self.connection, "nickname", "Terminal"),
                    )
                except Exception:
                    logger.debug("Failed to persist daemon restore metadata", exc_info=True)
                self._daemon_controller.detach()
                logger.debug(f"Detached from daemon session {self._daemon_tab_state.session_id}")
            elif policy == TerminalClosePolicy.TERMINATE:
                self._daemon_controller.close()
                logger.debug(f"Terminated daemon session {self._daemon_tab_state.session_id}")
            elif policy == TerminalClosePolicy.ASK and not is_quitting:
                self._show_daemon_close_dialog()
                return  # Dialog will handle the actual close
            elif is_quitting:
                # Quit ends every session. Detaching here used to leave the
                # session (and the daemon holding it) alive after the app was
                # gone, which is exactly what quit must not do.
                self._daemon_controller.close()
                logger.debug(
                    f"Terminated daemon session {self._daemon_tab_state.session_id} on quit"
                )
            else:
                self._daemon_controller.detach()
                logger.debug(f"Detached from daemon session {self._daemon_tab_state.session_id}")

            # Clean up daemon state
            self._uninstall_daemon_backend_io()
            self._daemon_mode = False
            self._daemon_controller = None
            self._daemon_tab_state = None
            self._hide_view_only_indicator()

            # Update connection state
            self.is_connected = False

        except Exception as e:
            logger.error(f"Failed to handle daemon close: {e}")

    def _show_daemon_close_dialog(self):
        """Show close policy dialog for daemon terminals."""
        try:
            from gi.repository import Adw

            root = self.get_root() if hasattr(self, 'get_root') else None
            if not root:
                # Fall back to detach if no parent window
                self._daemon_controller.detach()
                return

            dialog = Adw.AlertDialog.new(
                "Close Terminal Session",
                "What should happen to the remote terminal session?"
            )

            dialog.add_response("detach", "Detach")
            dialog.add_response("terminate", "Terminate")
            dialog.add_response("cancel", "Cancel")

            dialog.set_response_appearance("terminate", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_response_appearance("detach", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("detach")
            dialog.set_close_response("cancel")

            dialog.connect("response", self._on_daemon_close_dialog_response)
            dialog.present(root)

        except Exception as e:
            logger.error(f"Failed to show daemon close dialog: {e}")
            # Fall back to detach on error
            self._daemon_controller.detach()

    def _on_daemon_close_dialog_response(self, dialog, response):
        """Handle daemon close dialog response."""
        try:
            if response == "detach":
                self._daemon_controller.detach()
                logger.debug(f"User chose to detach from daemon session {self._daemon_tab_state.session_id}")
            elif response == "terminate":
                self._daemon_controller.close()
                logger.debug(f"User chose to terminate daemon session {self._daemon_tab_state.session_id}")
            elif response == "cancel":
                return  # Don't close

            # Clean up daemon state if we proceeded with close
            if response != "cancel":
                self._uninstall_daemon_backend_io()
                self._daemon_mode = False
                self._daemon_controller = None
                self._daemon_tab_state = None
                self._hide_view_only_indicator()

                # Update connection state
                self.is_connected = False

                # Emit connection-lost signal to close the tab
                self.emit('connection-lost')

        except Exception as e:
            logger.error(f"Failed to handle daemon close dialog response: {e}")

    def _update_daemon_connection_state(self):
        """Update connection state based on daemon session state."""
        if not self._daemon_controller:
            return

        # A state notification may carry the RUNNING transition (or granted
        # input ownership) — retry flushing any feeds deferred by
        # ``feed_child_data_when_shell_ready``. Only when the active gate has
        # already cleared: the authoritative gate clears on output observed
        # after RUNNING (never on the RUNNING transition itself, which
        # precedes the banner and would double-echo the feed).
        if self._pending_shell_ready_feeds:
            ready = (
                getattr(self, "_shell_output_seen_after_running", False)
                if self._daemon_running_gate_active()
                else getattr(self, "_shell_output_seen", False)
            )
            if ready:
                GLib.idle_add(self._flush_pending_shell_ready_feeds)

        try:
            from .terminal_session_controller import TerminalSessionState

            daemon_state = self._daemon_controller.state
            old_connected = self.is_connected
            tab = self._daemon_controller.tab_state
            dialogs = getattr(self, "_daemon_interaction_dialogs", None)
            if (
                dialogs is not None
                and tab.session_id is not None
                and getattr(dialogs, "_session_id", None) != tab.session_id
            ):
                dialogs.set_session(tab.session_id)

            if daemon_state == TerminalSessionState.ACTIVE:
                self.is_connected = True
                self.connection_state = self.connection_state.__class__.CONNECTED
                self.connection_state_reason = 'Connected'
                self._set_connecting_overlay_visible(False)

                # Check input ownership and show/hide view-only indicator
                if not self.has_input_ownership:
                    self._show_view_only_indicator()
                else:
                    self._hide_view_only_indicator()
                    if not old_connected:
                        # Input ownership (required to resize — see
                        # _on_daemon_size_changed) may only just have been
                        # granted by this same attach result. The widget can
                        # already have grown to its real on-screen size while
                        # ownership was still pending, and every resize
                        # signal during that window was silently dropped for
                        # lack of ownership (GH #1164 follow-up) — with no
                        # catch-up, the remote PTY/tmux stays at whatever
                        # size the session opened with. Sync now that we may
                        # actually resize.
                        self._resync_daemon_terminal_size()

                # Emit connection-established if newly connected
                if not old_connected:
                    GLib.idle_add(self.emit, 'connection-established')

            elif daemon_state in {
                TerminalSessionState.OPENING,
                TerminalSessionState.ATTACHING,
                TerminalSessionState.REPLAYING,
                TerminalSessionState.RECOVERING,
            }:
                self.connection_state = self.connection_state.__class__.CONNECTING
                self.connection_state_reason = f'Daemon: {daemon_state.value}'
                self._set_connecting_overlay_visible(True)

            elif daemon_state in {TerminalSessionState.FAILED, TerminalSessionState.CLOSED}:
                self.is_connected = False
                self.connection_state = self.connection_state.__class__.DISCONNECTED
                self.connection_state_reason = f'Daemon: {daemon_state.value}'
                self._set_connecting_overlay_visible(False)

                # Emit connection-lost if was connected
                if old_connected:
                    GLib.idle_add(self.emit, 'connection-lost')

                if daemon_state == TerminalSessionState.CLOSED:
                    # The daemon session ended underneath the tab (remote
                    # reboot, killed connection, or the user typing exit).
                    self._handle_daemon_session_exit(old_connected)

            elif daemon_state == TerminalSessionState.DETACHED:
                # Keep connected state but update reason
                self.connection_state_reason = 'Detached'

        except Exception as e:
            logger.error(f"Failed to update daemon connection state: {e}")

    def _handle_daemon_session_exit(self, was_connected):
        """React to a daemon-initiated session end (reboot, dropped link, exit).

        Mirrors the legacy child-exit handling: a clean exit (the user typed
        exit) closes the tab; any other exit shows the reconnect banner.
        Runs at most once per daemon session.
        """
        if getattr(self, '_daemon_exit_handled', False):
            return
        self._daemon_exit_handled = True

        try:
            exit_info = getattr(self._daemon_controller, 'exit_info', None)
            exit_code = getattr(exit_info, 'exit_code', None) if exit_info else None
            signal = getattr(exit_info, 'signal', None) if exit_info else None

            if (
                exit_info is not None
                and exit_code == 0
                and not signal
                and self.last_error_message is None
            ):
                # Clean exit: close the tab (mirrors _handle_child_exit_cleanup).
                # The last_error_message guard matters because a session can
                # reach CLOSED via FAILED (session_runtime classified it as a
                # real failure and _on_connection_failed already recorded a
                # banner) — exit_code is still 0 in that case, and without
                # this guard the tab would vanish and silently hide the
                # banner that was just shown.
                root = self.get_root() if hasattr(self, 'get_root') else None
                if root and hasattr(root, 'tab_view'):
                    # Safe lookup: this terminal may be embedded in a
                    # split-view pane (not in the main tab_view).
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

            # Unexpected end (remote reboot, killed connection, ...): show the
            # reconnect banner, classified the same way as the legacy path.
            exit_state, exit_reason = self._classify_exit(exit_code, was_connected, '')
            self.connection_state = exit_state
            self.connection_state_reason = exit_reason or 'Session ended'
            banner_text = self.last_error_message or exit_reason
            if not banner_text:
                if exit_code:
                    banner_text = _('SSH exited with status {code}').format(code=exit_code)
                elif signal:
                    banner_text = _('Session terminated by signal {sig}').format(sig=signal)
                else:
                    banner_text = _('Session ended.')
            self._record_error_detail(exit_reason or banner_text, exit_code=exit_code)
            self._set_disconnected_banner_visible(True, banner_text)
        except Exception as e:
            logger.error(f"Failed to handle daemon session exit: {e}")

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

            # The child process spawned — that only means the session process
            # started, not that it authenticated or reached a remote host. Enter
            # CONNECTING and promote to CONNECTED only on real login evidence
            # (remote termprops via _on_termprops_changed) or, failing that, if
            # the process is still alive after a short grace period. A fast
            # failure (auth/refused/unreachable) exits first and is classified
            # as FAILED, so the indicator never flashes green on a dead link.
            from .connection_model import ConnectionState
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

            from .connection_model import ConnectionState
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
        evidence = classify_connection_evidence(
            self._scrape_recent_terminal_text(4000) or ''
        )
        if evidence.failure_reason:
            self._connect_failure_hint = evidence.failure_reason
        return evidence.verdict

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
        from .connection_model import ConnectionState
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

        # 'pending' — keep waiting. Never promote on liveness alone: a socket
        # still stuck in the TCP-connect phase (firewall silently dropping SYNs,
        # no ConnectTimeout set) is alive but has never reached the host, and
        # _scan_connect_evidence already returns 'connected' the moment any real
        # remote output appears. After the initial ~60s grace window, keep
        # checking at a slower rate so a late authentication/network recovery can
        # still promote on real remote output rather than being abandoned.
        self._connect_poll_count += 1
        if self._connect_poll_count == 60:  # ≈60s: switch to slow polling
            self._connect_grace_timer_id = GLib.timeout_add_seconds(
                5, self._on_connect_grace_elapsed
            )
            logger.debug(
                f"Terminal {self.session_id}: no connect evidence after grace "
                "window; continuing slow polling"
            )
            return False
        return True

    def _mark_connected(self):
        """Promote a CONNECTING session to CONNECTED (idempotent). Called only on
        real evidence: termprops, or remote output seen by the evidence poller."""
        from .connection_model import ConnectionState
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
        such as 2FA stay in the terminal for the user. This is presentation
        support for one-shot command UIs; it does not own remote transport.
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
        single-slot ``_pty_autofill`` tuple; no-op when neither is set.

        Ownership: local/legacy GTK-owned terminals only. Daemon-backed SSH
        authenticates via interaction dialogs and must not scrape/autofill into
        a nonexistent local child (or replay after reattach).
        """
        if getattr(self, '_daemon_mode', False):
            logger.debug("Skipping PTY autofill on daemon-backed terminal")
            self._pty_autofill = None
            self._pty_autofills = None
            return
        autofill = getattr(self, '_pty_autofill', None)
        if (not getattr(self, '_pty_autofills', None)
                and (not autofill or not autofill[0])):
            return
        self._pty_autofill_done = False
        backend = getattr(self, "backend", None)
        if backend is None:
            return
        try:
            if hasattr(backend, "add_output_hook"):
                backend.add_output_hook(self._pty_autofill_tick)
                self._pty_autofill_handler = None
            else:
                self._pty_autofill_handler = backend.connect_content_changed(
                    self._on_pty_autofill_changed
                )
        except Exception:
            logger.debug("Could not arm PTY auto-fill via backend", exc_info=True)
            return
        # Safety: give up after 30s so we never linger or leak the handler if the
        # prompt never shows (e.g. cached sudo credentials, wrong command).
        self._pty_autofill_timeout_id = GLib.timeout_add_seconds(
            30, self._cancel_pty_autofill)

    def _on_pty_autofill_changed(self, _vte):
        if getattr(self, '_daemon_mode', False):
            # Daemon SSH must not deliver autofill through a local child feed.
            self._cancel_pty_autofill()
            return False
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
            # Never log ``response`` — it is a secret.
            data = (response + '\n').encode('utf-8')
            self.feed_child_data(data)
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
                self.backend.disconnect(handler_id)
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
        from .connection_model import ConnectionState
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
            from .connection_model import ConnectionState
            if self.connection_state == ConnectionState.CONNECTING and not self._is_local_terminal():
                self._mark_connected()
        except Exception:
            pass

    def _classify_exit(self, exit_code, was_connected, extra_text=''):
        """Map an ssh exit into (ConnectionState, reason) from the exit code and
        the captured error text. Distinguishes auth/unreachable failures from a
        clean disconnect or a dropped-after-connected session."""
        from .connection_model import ConnectionState
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
        """Delegate theme rendering entirely to the active emulator backend."""
        if self.backend is None:
            raise RuntimeError("No terminal backend available for theme application")
        self.backend.apply_theme(theme_name)

    def grab_terminal_focus(self) -> None:
        """Focus the active emulator without exposing its implementation."""
        if self.backend is not None:
            self.backend.grab_focus()

    def queue_terminal_draw(self) -> None:
        """Request an emulator redraw through the backend."""
        if self.backend is not None:
            self.backend.queue_draw()

    def show_terminal(self) -> None:
        """Show the active emulator through the backend."""
        if self.backend is not None:
            self.backend.show()

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
        """Configure the active terminal through the backend contract."""
        if self.backend is None:
            raise RuntimeError("No terminal backend available for configuration")
        font_desc = Pango.FontDescription()
        font_desc.set_family("Monospace")
        font_desc.set_size(12 * Pango.SCALE)
        self.backend.set_font(font_desc)
        encoding = "UTF-8"
        try:
            encoding = self.config.get_setting("terminal.encoding", "UTF-8")
        except Exception:
            pass
        self.backend.configure({"encoding": encoding, "scrollback_lines": 10000})
        self.backend.apply_theme()
        # VTE owns hover highlighting and cursor changes for both its
        # registered regex and OSC 8 hyperlinks.  Python only looks a URI up
        # for an explicit click or context-menu action.
        self.backend.setup_link_handling(
            self._on_vte_motion,
            self._on_vte_pointer_enter,
            self._on_selection_changed,
        )
        self._apply_pass_through_mode(self._pass_through_mode)
        self._setup_context_menu()
        # Apply macOS Option key passthrough
        if is_macos():
            try:
                enabled = self.config.get_setting(
                    'terminal.macos_option_key_passthrough', False
                )
                if hasattr(self.backend, 'set_macos_option_key_passthrough'):
                    self.backend.set_macos_option_key_passthrough(bool(enabled))
            except Exception:
                logger.debug("Failed to apply macOS Option key passthrough", exc_info=True)

    def _on_vte_pointer_enter(self, controller, x, y):
        """Compatibility no-op; VTE owns pointer-enter link handling."""

    def _vte_uri_at(self, x: float, y: float) -> Optional[str]:
        """Return the URI at widget coordinates through the backend.

        Full one-shot lookup (OSC 8 + plain-text regex) — used by the
        Ctrl+click and context-menu gestures only.
        """
        if getattr(self, '_destroyed', False) or getattr(self, '_is_quitting', False):
            return None
        if not self.backend.supports_feature("hyperlinks"):
            return None
        return self.backend.hyperlink_at(x, y)

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
        """Compatibility no-op: never query VTE screen coordinates on motion."""

    def _on_open_link_activated(self, action, param):
        """Open the hyperlink that was under the cursor when the context menu was triggered."""
        uri = getattr(self, '_context_menu_hyperlink_uri', None)
        if uri:
            try:
                Gio.AppInfo.launch_default_for_uri(uri, None)
                logger.debug(f"Opened link: {uri}")
            except Exception as e:
                logger.warning(f"Failed to open link '{uri}': {e}")

    def _on_copy_link_activated(self, action, param):
        """Copy the hyperlink to the clipboard."""
        uri = getattr(self, '_context_menu_hyperlink_uri', None)
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
        if not self.backend.supports_feature("save_output"):
            logger.warning("Save Output is not supported by the active backend")
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
                self.backend.save_contents(stream)
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
        if self.backend.supports_feature("encoding"):
            try:
                encodings = self.backend.get_supported_encodings()
            except Exception as exc:
                logger.debug("Unable to query backend encodings: %s", exc)

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
            self.backend.set_encoding(target)
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
        """Return ``(columns, rows)`` from the active backend."""
        try:
            rows, columns = self.backend.get_size()
            return int(columns), int(rows)
        except Exception:
            return 80, 24

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
                elif getattr(self, 'terminal_container', None) is not None:
                    widget_to_connect = self.terminal_container

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
            env = sanitize_local_shell_env(
                get_identity_manager().apply_selected_to_env(os.environ.copy())
            )
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
        """Set up a local shell when the optional local agent is unavailable."""
        # Route env injection through the selected identity provider (one seam for all
        # SSH_AUTH_SOCK injection); idempotent over the inherited environment.
        from .identity import get_identity_manager
        env = sanitize_local_shell_env(
            get_identity_manager().apply_selected_to_env(os.environ.copy())
        )

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
            parent_widget = self.backend.widget if self.backend else self.terminal_widget
            self._menu_parent_widget = parent_widget

            def _prepare_context_menu(x=None, y=None):
                """Snapshot the URI at the actual invocation coordinates."""
                uri = self._vte_uri_at(x, y) if x is not None and y is not None else None
                self._context_menu_hyperlink_uri = uri
                has_link = bool(uri)
                in_menu = getattr(self, '_link_section_in_menu', False)
                if has_link and not in_menu:
                    self._menu_model.insert_section(0, None, self._link_menu)
                    self._link_section_in_menu = True
                elif not has_link and in_menu:
                    self._menu_model.remove(0)
                    self._link_section_in_menu = False

            self._native_vte_context_menu = False
            if self.backend.supports_feature("native_context_menu"):
                def _on_native_context_menu(showing):
                    if showing:
                        coordinates = getattr(
                            self, '_pending_context_menu_coordinates', None)
                        if coordinates is None:
                            # Keyboard invocation has no meaningful mouse cell.
                            _prepare_context_menu()
                        else:
                            _prepare_context_menu(*coordinates)
                    else:
                        self._pending_context_menu_coordinates = None
                self._native_vte_context_menu = self.backend.setup_native_context_menu(
                    self._menu_popover, _on_native_context_menu)

            # VTE parents, positions, presents and unparents native context
            # menus itself. Only the legacy/WebView popover is application-owned.
            if not self._native_vte_context_menu and parent_widget:
                self._menu_popover.set_parent(parent_widget)

            self._menu_needs_manual_dismiss = not self.backend.supports_feature("hyperlinks")
            if self._menu_needs_manual_dismiss:
                self._menu_popover.set_autohide(False)
                self._install_manual_menu_dismissal(parent_widget)

            # Capture records coordinates before VTE handles the event; it
            # claims only when SSH Pilot handles the click itself. PyXterm's
            # non-autohide menu needs every button for manual click-away
            # dismissal, while VTE only needs secondary-button observation.
            gesture = Gtk.GestureClick()
            gesture.set_button(_context_gesture_button(self._menu_needs_manual_dismiss))
            gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            def _on_pressed(gest, n_press, x, y):
                handled = False
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
                    if getattr(self, '_native_vte_context_menu', False):
                        # EventContext coordinates are not exposed by the GI
                        # binding. Record only plain Python numbers; VTE still
                        # owns recognition, placement and popup lifecycle.
                        self._pending_context_menu_coordinates = (float(x), float(y))
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
                    native_vte_menu = bool(
                        getattr(self, '_native_vte_context_menu', False)
                    )
                    if not _context_click_is_handled(
                        btn,
                        paste_on_right_click=paste_on_rc,
                        shift_held=shift_held,
                        native_vte_menu=native_vte_menu,
                    ):
                        return
                    if paste_on_rc and not shift_held:
                        self._pending_context_menu_coordinates = None
                        try:
                            if self.backend:
                                self.backend.grab_focus()
                        except Exception:
                            pass
                        self.paste_text()
                        handled = True
                        return
                    # VTE 0.76+ owns recognition, placement and popup lifecycle.
                    # This gesture exists on that path solely to preserve the
                    # paste-on-right-click preference above.
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
                        _prepare_context_menu(x, y)
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
                    handled = True
                except Exception as e:
                    logger.error(f"Context menu popup failed: {e}")
                finally:
                    _finish_capture_gesture(gest, handled)
            gesture.connect('pressed', _on_pressed)
            # Store gesture reference for cleanup
            self._menu_gesture = gesture
            # Add gesture to the backend widget (VTE or WebView), recorded so a repeat
            # setup pass removes it instead of stacking a duplicate.
            if self.backend and self.backend.widget:
                self._register_menu_controller(self.backend.widget, gesture)
                logger.debug(f"Added context menu gesture to backend widget: {type(self.backend).__name__}")
            elif self.terminal_widget is not None:
                self._register_menu_controller(self.terminal_widget, gesture)
                logger.debug("Added context menu gesture to terminal widget")

            # CAPTURE-phase left-click gesture for Ctrl+click URL opening
            # (GNOME Terminal: terminal_screen_capture_click_pressed_cb requires
            # GDK_CONTROL_MASK). Must use CAPTURE so it runs before VTE's
            # text-selection handler; we only claim when the modifier is held
            # AND a URL is under the cursor.
            if self.backend.supports_feature("hyperlinks"):
                url_gesture = Gtk.GestureClick()
                url_gesture.set_button(Gdk.BUTTON_PRIMARY)
                url_gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
                def _on_url_click(gest, n_press, x, y):
                    handled = False
                    try:
                        active = not (
                            getattr(self, '_destroyed', False)
                            or getattr(self, '_is_quitting', False)
                        )
                        # Plain click must reach VTE for cursor placement /
                        # selection — only Ctrl+click (Cmd+click on macOS)
                        # activates links, matching GNOME Terminal.
                        try:
                            state = gest.get_current_event_state()
                        except Exception:
                            return
                        modifier_held = self._click_has_link_modifier(state)
                        uri = (
                            self._vte_uri_at(x, y)
                            if n_press == 1 and active and modifier_held
                            else None
                        )
                        if not _link_click_is_handled(
                            n_press,
                            active=active,
                            modifier_held=modifier_held,
                            uri=uri,
                        ):
                            return

                        Gio.AppInfo.launch_default_for_uri(uri, None)
                        handled = True
                        logger.debug(f"Opened URL via Ctrl/Cmd+click: {uri}")
                    except Exception as e:
                        logger.warning(f"URL click failed: {e}")
                    finally:
                        _finish_capture_gesture(gest, handled)
                url_gesture.connect('pressed', _on_url_click)
                self._register_menu_controller(self.backend.widget, url_gesture)
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
            if getattr(self, '_native_vte_context_menu', False):
                try:
                    self.backend.clear_native_context_menu()
                except Exception:
                    pass
            else:
                try:
                    popover.set_parent(None)
                except Exception:
                    pass
            self._menu_popover = None
        self._native_vte_context_menu = False

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
                    return False

                def _cb_paste(widget, *args):
                    if self.backend:
                        return _schedule_vte_action(self.backend.paste_clipboard)
                    return False

                def _cb_select_all(widget, *args):
                    if self.backend:
                        return _schedule_vte_action(self.backend.select_all)
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

                host = self.controller_host()
                if host is not None:
                    host.add_controller(controller)
                self._shortcut_controller = controller

            if getattr(self, '_shortcut_controller', None) is not None:
                self._setup_mouse_wheel_zoom()

        except Exception as e:
            logger.debug(f"Failed to install shortcuts: {e}")

        # self._search is created before setup_terminal() (which calls this), so it
        # is normally present; guard + log rather than a bare except so a future
        # init-order regression is visible instead of silently dropping the
        # keyboard search shortcuts.
        search = getattr(self, '_search', None)
        if search is not None:
            try:
                search._ensure_search_key_controller()
            except Exception:
                logger.debug("Failed to install search key controller", exc_info=True)
        else:
            logger.warning("Search key controller not installed: _search missing at shortcut setup")

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
            host = self.controller_host()
            if host is not None:
                host.add_controller(scroll_controller)
            self._scroll_controller = scroll_controller
            logger.debug("Mouse wheel zoom functionality installed")

        except Exception as e:
            logger.debug(f"Failed to setup mouse wheel zoom: {e}")

    def controller_host(self):
        """Widget custom event controllers live on.

        The VTE backend exposes a `vte` widget; PyXterm only has `terminal_widget`.
        Attach and detach must agree on the target, or a teardown silently leaves
        the controller on the widget it was added to.
        """
        return getattr(self, 'terminal_widget', None) or getattr(
            getattr(self, 'backend', None), 'widget', None
        )

    def _remove_custom_shortcut_controllers(self):
        """Detach any custom shortcut or scroll controllers from the terminal widget."""
        host = self.controller_host()
        ctrl = getattr(self, '_shortcut_controller', None)
        if ctrl is not None:
            try:
                if hasattr(host, 'remove_controller'):
                    host.remove_controller(ctrl)
            except Exception as exc:
                logger.debug("Failed to remove shortcut controller: %s", exc)
            finally:
                self._shortcut_controller = None

        scroll = getattr(self, '_scroll_controller', None)
        if scroll is not None:
            try:
                if hasattr(host, 'remove_controller'):
                    host.remove_controller(scroll)
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

    def _apply_macos_option_key_passthrough(self, enabled: bool) -> bool:
        """Apply macOS Option key passthrough setting to the backend."""
        backend = getattr(self, 'backend', None)
        if backend is not None and hasattr(backend, 'set_macos_option_key_passthrough'):
            backend.set_macos_option_key_passthrough(enabled)
        return False

    def _on_config_setting_changed(self, _config, key, value):
        if key == 'terminal.pass_through_mode':
            GLib.idle_add(self._apply_pass_through_mode, bool(value))
        elif key == 'terminal.encoding':
            if self._updating_encoding_config:
                return
            GLib.idle_add(self._apply_terminal_encoding_idle, value or '')
        elif key == 'terminal.macos_option_key_passthrough':
            GLib.idle_add(self._apply_macos_option_key_passthrough, bool(value))

    # PTY forwarding is now handled automatically by VTE
    # No need for manual PTY management in this implementation

    def reconnect(self):
        """Reconnect the terminal with updated connection settings"""
        logger.info("Reconnecting terminal with updated settings...")
        reconnect_handler = getattr(self, '_reconnect_handler', None)
        if callable(reconnect_handler):
            return reconnect_handler(self)

        logger.error('Daemon reconnect handler is unavailable')
        return False

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

        return None

    def _on_destroy(self, widget):
        """Handle widget destruction"""
        logger.debug(f"Terminal widget {self.session_id} being destroyed")
        # Suppress any in-flight VTE interactions (motion/enter callbacks still
        # queued on the motion controller) before the screen state is released.
        self._destroyed = True

        # Disconnect backend signal handlers first to prevent callbacks on destroyed objects
        if hasattr(self, 'backend') and self.backend is not None:
            try:
                self._disconnect_backend_signals()
            except Exception as e:
                logger.error(f"Error disconnecting backend signals: {e}")

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

    def _repaint_connection_status(self, root) -> None:
        """Ask the window to re-render this connection's sidebar rows.

        Best-effort and never raises: a failure to repaint must not abort the
        disconnect, nor the tab close that called it.
        """
        handler = getattr(root, 'on_connection_status_changed', None)
        if not callable(handler):
            return
        try:
            GLib.idle_add(handler, None, self.connection, False)
        except Exception:
            logger.debug('Failed to schedule connection status repaint', exc_info=True)

    def disconnect(self):
        """Close the SSH connection and clean up resources"""
        # Guard UI emissions when the root window is quitting. Computed up front
        # so it is always bound: disconnect() can be called while is_connected is
        # already False (e.g. pressing Reconnect after a failed connection), in
        # which case the block below is skipped but the finally clause still
        # references is_quitting.
        root = None
        try:
            if hasattr(self, 'get_root'):
                root = self.get_root()
        except RuntimeError:
            pass
        # Prefer this terminal's own flag: cleanup_all() sets terminal._is_quitting
        # directly, and by the time it runs the window may already be unrooted
        # (get_root() → None), which would otherwise miss the quit fast-path.
        is_quitting = bool(getattr(self, '_is_quitting', False)) or bool(
            getattr(root, '_is_quitting', False)
        )

        # Handle daemon mode close policy
        if self._daemon_mode and self._daemon_controller:
            return self._handle_daemon_close(is_quitting)

        if self.is_connected:
            logger.debug(f"Disconnecting SSH session {self.session_id}...")
            self.is_connected = False

            # Only update manager / UI if not quitting
            if hasattr(self, 'connection') and self.connection and not is_quitting:
                self.connection.is_connected = False
                # Connection status is daemon-authoritative now: this window's
                # `connection_manager` is a ConnectionPresentationStore (a
                # read-only DTO projection) with no `emit`, so the GObject-era
                # push raised AttributeError straight out of disconnect() and
                # aborted whatever was closing the tab. Repaint the sidebar
                # rows directly — that repaint was the emit's only observable
                # effect, since on_connection_status_changed is render-only and
                # `is_connected` above is the authoritative bit it renders.
                self._repaint_connection_status(root)

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

            # Clean up all collected PIDs (with error handling for each).
            # During quit the process manager's cleanup_all() has already
            # SIGKILLed these same PIDs, so re-running the SIGTERM-then-poll
            # path here only wastes its full timeout on an already-dead (zombie)
            # child — skip it and let the manager's kill stand.
            if not is_quitting:
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

            self.is_connected = False

            # Clean up PTY if it exists
            if hasattr(self, 'pty') and self.pty:
                self.pty.close()
                del self.pty

            # Remember last error for later reporting
            self.last_error_message = error_message

            # Mark the connection FAILED so the sidebar reflects it (classify the
            # message into a concise reason where we can).
            from .connection_model import ConnectionState
            _state, _reason = self._classify_exit(255, False)
            self.connection_state = ConnectionState.FAILED
            self.connection_state_reason = _reason or error_message
            update_connection_state = getattr(
                getattr(self, 'connection_manager', None),
                'update_connection_state',
                None,
            )
            if (
                callable(update_connection_state)
                and self.connection
                and getattr(self.connection, 'hostname', None) != 'localhost'
            ):
                update_connection_state(
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
        from .connection_model import ConnectionState
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
                remote_dir = None
                if not getattr(self, '_native_cwd_available', False):
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
        except Exception:
            logger.debug("copy-on-select failed", exc_info=True)

    def copy_text(self):
        """Copy selected text to clipboard"""
        if self.backend:
            had_selection = self._has_terminal_selection()
            self.backend.copy_clipboard()
            if had_selection:
                self._show_toast(_("Copied to clipboard"))

    def paste_text(self):
        """Paste text from clipboard"""
        if self.backend:
            self.backend.paste_clipboard()

    def select_all(self):
        """Select all text in terminal"""
        if self.backend:
            self.backend.select_all()

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

    def _on_termprops_changed(self, _widget, event, user_data=None):
        """Handle backend-neutral title and shell lifecycle properties."""
        if not event:
            return
        try:
            from .connection_model import ConnectionState
            if (self.connection_state == ConnectionState.CONNECTING
                    and not self._is_local_terminal()):
                self._mark_connected()
        except Exception:
            pass

        cwd_uri = event.get("cwd_uri")
        if cwd_uri:
            try:
                from urllib.parse import unquote, urlparse
                parsed = urlparse(cwd_uri)
                if parsed.scheme == "file" and parsed.path:
                    self._current_remote_directory = unquote(parsed.path)
                    self._native_cwd_available = True
            except Exception:
                logger.debug("Could not parse VTE current-directory URI", exc_info=True)

        title = event.get("title")
        if title:
            # OSC 7 is authoritative; title parsing remains a fallback for
            # shells which do not report a current-directory URI.
            if not getattr(self, '_native_cwd_available', False):
                remote_dir = self._parse_directory_from_title(title)
                if remote_dir:
                    self._current_remote_directory = remote_dir
            self.emit("title-changed", title)
        if not self._is_local_terminal():
            return
        if "postexec" in event:
            self._job_status = "IDLE"
            logger.debug("Local terminal job finished with exit code: %s", event["postexec"])
        elif "preexec" in event:
            self._job_status = "RUNNING"
        elif "precmd" in event:
            self._job_status = "PROMPT"

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
            # PyXterm bridge), so it is no longer gated on the VTE widget.
            pty = None
            if self.backend and hasattr(self.backend, 'get_pty'):
                pty = self.backend.get_pty()
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

    # --- Fullscreen: a request to the window, which owns the state ---
    def toggle_fullscreen(self):
        """Ask the toplevel window to toggle fullscreen.

        The terminal deliberately holds no fullscreen state and installs no F11
        controller: the window owns both, so closing this widget can never
        strand the window fullscreen (issue #1102). F11 itself is handled by
        the window-level controller; this stays for callers that already hold a
        terminal.
        """
        root = self.get_root()
        controller = getattr(root, 'fullscreen_controller', None)
        if controller is None:
            logger.debug('No window fullscreen controller available')
            return None
        return controller.toggle()

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

            root = self.get_root()
            scp_controller = getattr(root, "scp_controller", None) if root else None
            start_scp = getattr(scp_controller, "start_scp_transfer", None)
            if not callable(start_scp):
                logger.debug("Drop rejected: daemon SCP controller unavailable")
                return False
            destination = self.get_current_remote_directory()
            if not destination:
                logger.debug("Drop rejected: remote directory is unavailable")
                return False
            start_scp(
                self.connection,
                file_paths,
                destination,
                direction="upload",
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

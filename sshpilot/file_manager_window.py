"""In-app two-pane SFTP file manager window.

This module provides a libadwaita based window that mimics a traditional
file manager experience while running entirely inside sshPilot.  It exposes
two panes that can each browse an independent remote path.  All filesystem
operations are executed on background worker threads to keep the UI
responsive and results are marshalled back to the main GTK loop using
``GLib.idle_add``.  The implementation intentionally favours clarity over raw
performance – the goal is to provide a dependable fallback for situations
where a native GVFS/GIO based file manager is not available (e.g. Flatpak
deployments).

The window follows the GNOME HIG by composing libadwaita widgets such as
``Adw.ToolbarView`` and ``Adw.HeaderBar``.  Each pane exposes both list and
grid representations of directory contents, navigation controls, progress
indicators and toast based feedback.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import posixpath
import shutil
import threading
import weakref
from concurrent.futures import Future, CancelledError
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from gettext import gettext as _


from gi.repository import Adw, Gio, GLib, GObject, Gdk, Gtk

# Try to import GtkSourceView for syntax highlighting.
# Probe a type so a missing shared library (incomplete bundle) disables GtkSource
# instead of failing later with a cryptic GI error.
try:
    import gi
    gi.require_version('GtkSource', '5')
    from gi.repository import GtkSource
    _gtksource_probe = GtkSource.View  # noqa: F841 — force shared-library load
    _HAS_GTKSOURCE = True
except Exception:  # noqa: BLE001 — ImportError/ValueError/GError/TypeError
    _HAS_GTKSOURCE = False
    GtkSource = None

from .platform_utils import is_flatpak
from .text_editor import RemoteFileEditorWindow
from .file_manager import (
    create_file_manager_backend,
    FileEntry,
    FilePane,
    SFTPProgressDialog,
    TransferCancelledException,
    _HAS_ALERT_DIALOG,
    _load_first_doc_path,
    _load_grant_for_host,
    _sftp_path_exists,
    stat_isdir,
)

import logging


logger = logging.getLogger(__name__)



# Global registry to track live file manager windows. Used to ensure
# managers are cleaned up even if the application does not hold references.
_file_manager_windows_registry: weakref.WeakSet = weakref.WeakSet()


class FileManagerWindow(Adw.Window):
    """Top-level window hosting two :class:`FilePane` instances."""

    def __init__(
        self,
        application: Adw.Application,
        *,
        host: str,
        username: str,
        port: int = 22,
        initial_path: str = "~",
        nickname: Optional[str] = None,
        connection: Any = None,
        connection_manager: Any = None,
        ssh_config: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(application=application, title="")
        # Register this window in the global registry for cleanup
        _file_manager_windows_registry.add(self)
        self._host = host
        self._username = username
        self._port = port or 22
        self._nickname = nickname
        self._connection = connection
        self._connection_manager = connection_manager
        self._ssh_config = dict(ssh_config) if ssh_config else None
        # Set default and minimum sizes following GNOME HIG
        self.set_default_size(1000, 640)
        # Set minimum size to ensure usability (GNOME HIG recommends minimum 360px width)
        self.set_size_request(600, 400)
        # Ensure window is resizable (this is the default, but being explicit)
        self.set_resizable(True)
        # Ensure window decorations are shown (minimize, maximize, close buttons)
        self.set_decorated(True)
        
        # Progress state
        self._current_future: Optional[Future] = None
        
        # Password dialog state
        self._password_dialog_shown = False
        self._password_retry_count = 0
        self._max_password_retries = 3

        # PTY-backed ControlMaster establishing the connection (master-first
        # flow); guards against stacking masters from rapid reconnects.
        self._master_session = None
        self._auth_dialog = None
        self._auth_master_active = False

        # Use ToolbarView like other Adw.Window instances
        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)
        self._toolbar_view = toolbar_view
        self._embedded_parent: Optional[Gtk.Widget] = None
        
        # Create header bar with window controls
        header_bar = Adw.HeaderBar()
        self._header_bar = header_bar
        title_parts = []
        if nickname and nickname.strip():
            title_parts.append(str(nickname).strip())
        base_identity = f"{username}@{host}"
        if not title_parts or title_parts[0] != base_identity:
            title_parts.append(base_identity)
        header_bar.set_title_widget(Gtk.Label(label=" ".join(title_parts)))
        # Enable window controls (minimize, maximize, close) following GNOME HIG
        header_bar.set_show_start_title_buttons(True)
        header_bar.set_show_end_title_buttons(True)
        
        # Add toggle button to hide/show local pane
        self._local_pane_toggle = Gtk.ToggleButton()
        from sshpilot import icon_utils
        icon_utils.set_button_icon(self._local_pane_toggle, "view-dual-symbolic")
        self._local_pane_toggle.set_tooltip_text("Hide Local Pane")
        self._local_pane_toggle.set_active(False)  # Start unselected
        self._local_pane_toggle.add_css_class("flat")  # Flat style
        self._local_pane_toggle.connect("toggled", self._on_local_pane_toggle)
        header_bar.pack_start(self._local_pane_toggle)
        
        # Create toast overlay first and set it as toolbar content
        self._toast_overlay = Adw.ToastOverlay()
        self._progress_dialog: Optional[SFTPProgressDialog] = None
        self._connection_error_reported = False
        self._password_dialog_shown = False
        
        # Apply custom styling to toasts
        css_provider = Gtk.CssProvider()
        toast_css = """
        toast {
            /* Frosted glass effect */
            background-color: alpha(black, 0.6);

            /* Pill shape */
            border-radius: 99px; /* A large value creates the pill shape */

            /* Clean typography */
            color: white;
            font-weight: 500; /* Medium weight for a modern feel */
            font-size: 1.05em;

            /* Subtle details */
            padding: 8px 20px;
            margin: 10px;
            border: 1px solid alpha(white, 0.1);
            box-shadow: 0 5px 15px alpha(black, 0.2);
        }
        
        toast label {
            /* Style toast labels */
            color: white;
            font-weight: 500;
        }
        
        toast button {
            /* Style toast buttons if any */
            color: white;
            background-color: alpha(white, 0.2);
            border: 1px solid alpha(white, 0.3);
            border-radius: 6px;
            padding: 4px 8px;
        }
        
        toast button.circular.flat {
            /* Style close button */
            color: white;
            background-color: alpha(black, 0.6);
            border: 1px solid alpha(white, 0.1);
        }
        
        /* Drop target styling */
        .drop-target-active {
            background-color: alpha(@accent_color, 0.1);
            border: 2px dashed @accent_color;
            border-radius: 8px;
        }
        
        /* Pane toolbar styling to replace nested ToolbarView */
        /* Scope to file manager window only to avoid affecting sidebar toolbar */
        /* Use window colors for better cross-platform compatibility */
        .filemanagerwindow .toolbar,
        .filemanagerwindow toolbar {
            background-color: @window_bg_color;
            color: @window_fg_color;
            border-bottom: 1px solid @borders;
        }
        
        .filemanagerwindow .toolbar windowhandle,
        .filemanagerwindow toolbar windowhandle {
            background-color: @window_bg_color;
            color: @window_fg_color;
        }
        
        /* Pane divider styling */
        paned {
            background-color: @window_bg_color;
        }
        
        paned separator {
            background-color: @borders;
            border: none;
            min-width: 1px;
            min-height: 1px;
        }
        
        paned.horizontal separator {
            min-width: 1px;
        }
        
        paned.vertical separator {
            min-height: 1px;
        }
        
        /* Action bar styling */
        .inline-toolbar {
            background-color: @window_bg_color;
            color: @window_fg_color;
            border-top: 1px solid @borders;
        }
        """
        css_provider.load_from_data(toast_css.encode())
        self._toast_overlay.get_style_context().add_provider(
            css_provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
        )
        
        # Apply CSS only to this window, not globally, to avoid affecting sidebar toolbar
        # Use a unique CSS name for the file manager window to scope the styles
        self.add_css_class("filemanagerwindow")
        self.get_style_context().add_provider(
            css_provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
        )
        
        toolbar_view.set_content(self._toast_overlay)
        toolbar_view.add_top_bar(header_bar)

        # Create the main content area and set it as toast overlay child
        panes = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        panes.set_wide_handle(False)
        # Set position to split evenly by default (50%)
        panes.set_position(500)  # This will be adjusted when window is resized
        # Enable resizing and shrinking for both panes following GNOME HIG
        panes.set_resize_start_child(True)
        panes.set_resize_end_child(True)
        panes.set_shrink_start_child(False)
        panes.set_shrink_end_child(False)
        
        # Set panes as the child of toast overlay
        self._toast_overlay.set_child(panes)
        # Connect to size changes to maintain proportional split
        self.connect("notify::default-width", self._on_window_resize)
        # Also connect to the panes widget size changes
        panes.connect("notify::width-request", self._on_panes_size_changed)


        self._left_pane = FilePane("Local")
        self._right_pane = FilePane("Remote")
        self._left_pane.set_file_manager_window(self)
        self._right_pane.set_file_manager_window(self)
        self._left_pane.set_partner_pane(self._right_pane)
        self._right_pane.set_partner_pane(self._left_pane)
        panes.set_start_child(self._left_pane)
        # Overlay so a host-picker placeholder can cover the remote pane when
        # the window is opened without a server.
        self._right_overlay = Gtk.Overlay()
        self._right_overlay.set_child(self._right_pane)
        panes.set_end_child(self._right_overlay)
        self._install_remote_host_button()

        # Seed each pane with the persisted default zoom level. Each pane
        # tracks its own level from here on (zooming one does not affect the
        # other); the last pane zoomed persists its level so new file manager
        # windows pick up the most recent choice.
        initial_level = FilePane._load_saved_icon_size_level()
        for pane in (self._left_pane, self._right_pane):
            pane._icon_size_level = initial_level
            if pane.toolbar is not None and hasattr(pane.toolbar, "set_zoom_level"):
                pane.toolbar.set_zoom_level(initial_level)

        
        # Store reference to panes for resize handling
        self._panes = panes
        self._last_split_width = 0

        # Connect to size-allocate to maintain proportional split
        self.connect("notify::default-width", self._on_window_resize)

        # Set initial proportional split
        GLib.idle_add(self._set_initial_split_position)

        # Initialize panes: left is LOCAL home, right is REMOTE home (~)
        self._pending_paths: Dict[FilePane, Optional[str]] = {
            self._left_pane: None,
            self._right_pane: initial_path,
        }
        self._pending_highlights: Dict[FilePane, Optional[str]] = {
            self._left_pane: None,
            self._right_pane: None,
        }
        # Track which panes are being refreshed (to show success toast)
        self._refreshing_panes: set = set()
        # Track loading toast timeouts per pane (to cancel them if loading completes quickly)
        self._loading_toast_timeouts: Dict[FilePane, Optional[int]] = {
            self._left_pane: None,
            self._right_pane: None,
        }

        for pane in (self._left_pane, self._right_pane):
            initial_show_hidden = getattr(pane, "_show_hidden", False)
            if hasattr(pane, "set_show_hidden"):
                pane.set_show_hidden(initial_show_hidden, preserve_selection=True)


        self._clipboard_entries: List[FileEntry] = []
        self._clipboard_directory: Optional[str] = None
        self._clipboard_source_pane: Optional[FilePane] = None
        self._clipboard_operation: Optional[str] = None


        # Prime the left (local) pane with local home directory initially
        try:
            local_home = os.path.expanduser("~")
            self._load_local(local_home)
            self._left_pane.push_history(local_home)
        except Exception as exc:
            self._left_pane.show_toast(f"Failed to load local home: {exc}")

        # Connect pane signals
        for pane in (self._left_pane, self._right_pane):
            pane.connect("path-changed", self._on_path_changed, pane)
            pane.connect("request-operation", self._on_request_operation, pane)
            pane.set_can_paste(False)

        # In Flatpak, schedule restoration after initialization is complete
        if is_flatpak():
            GLib.idle_add(self._restore_flatpak_folder)

        # Connect close-request and destroy handlers to clean up resources
        self.connect("close-request", self._on_close_request)
        self.connect("destroy", self._on_destroy)

        # No host yet → cover the remote pane with a host picker instead of
        # connecting; picking a server starts the connection.
        self._manager = None
        if self._host:
            self._start_connection()
        else:
            self._show_host_picker_placeholder()

    def _start_connection(self) -> None:
        """Create the SFTP backend for the current host and connect."""
        # A fresh attempt supersedes any load-error state on the remote pane.
        self._right_pane._clear_load_error()
        connection = self._connection
        connection_manager = self._connection_manager
        username = self._username
        host = self._host

        # Initialize SFTP manager and connect signals
        initial_password = None
        if connection is not None:
            initial_password = getattr(connection, "password", None) or None

        # Check for saved password before attempting connection
        # This matches the logic in _connect_impl to ensure we find passwords
        if not initial_password and connection_manager is not None:
            lookup_user = username
            if connection is not None:
                lookup_user = getattr(connection, "username", None) or username

            # Try multiple host identifiers to match storage logic
            lookup_hosts = []
            if connection is not None:
                hostname = getattr(connection, "hostname", None)
                host_attr = getattr(connection, "host", None)
                nickname_attr = getattr(connection, "nickname", None)

                if hostname:
                    lookup_hosts.append(hostname)
                if host_attr and host_attr not in lookup_hosts:
                    lookup_hosts.append(host_attr)
                if nickname_attr and nickname_attr not in lookup_hosts:
                    lookup_hosts.append(nickname_attr)

            if not lookup_hosts:
                lookup_hosts = [host]

            if connection is not None:
                try:
                    retrieved = connection_manager.get_connection_password(connection)
                    if retrieved:
                        initial_password = retrieved
                except Exception:
                    pass
            if initial_password is None:
                for lookup_host in lookup_hosts:
                    try:
                        retrieved = connection_manager.get_password(lookup_host, lookup_user)
                        if retrieved:
                            logger.debug(
                                "Built-in file manager: Found password for %s@%s using identifier '%s'",
                                lookup_user,
                                lookup_host,
                                lookup_host,
                            )
                            initial_password = retrieved
                            break
                    except Exception as exc:
                        logger.debug(
                            "Built-in file manager: Password lookup failed for %s@%s (identifier '%s'): %s",
                            lookup_user,
                            lookup_host,
                            lookup_host,
                            exc,
                        )

        self._manager = create_file_manager_backend(
            host,
            username,
            self._port,
            password=initial_password,
            connection=connection,
            connection_manager=connection_manager,
            ssh_config=self._ssh_config,
        )

        # Connect signals with error handling
        try:
            self._manager.connect("connected", self._on_connected)
            self._manager.connect("connection-error", self._on_connection_error)
            self._manager.connect("authentication-required", self._on_authentication_required)
            self._manager.connect("progress", self._on_progress)
            self._manager.connect("operation-error", self._on_operation_error)
            self._manager.connect("directory-loaded", self._on_directory_loaded)
            self._manager.connect("directory-counts", self._on_directory_counts)
        except Exception as exc:
            logger.exception("Error connecting signals: %s", exc)

        # Show initial progress before connecting
        try:
            self._show_progress(0.1, "Connecting…")
        except Exception as exc:
            logger.exception("Error showing progress: %s", exc)

        # Spinner + status in the remote pane while the connection is set up
        try:
            target = (str(self._nickname).strip() if self._nickname else '') or host
            self._right_pane.show_connecting(f"Connecting to {target}…")
        except (AttributeError, RuntimeError, GLib.GError):
            pass

        # If no password found and password auth is enabled, show dialog before connecting
        # Check for both None and empty string
        has_password = initial_password and initial_password.strip()
        logger.debug(f"Built-in file manager: Password check - initial_password={'***' if initial_password else 'None'}, has_password={bool(has_password)}, password_auth_enabled={self._is_password_auth_enabled(connection) if connection else False}")

        if not has_password and connection_manager is not None:
            if self._is_password_auth_enabled(connection):
                logger.debug("Built-in file manager: No password found, password auth enabled, showing password dialog before connection")
                password = self._show_password_dialog_before_connect(username, host, connection)
                if password:
                    # Update the manager's password
                    self._manager._password = password
                    logger.debug("Built-in file manager: Password provided via dialog, updating manager")
                elif password is None:
                    # User cancelled, don't attempt connection
                    logger.debug("Built-in file manager: User cancelled password dialog")
                    self._on_connection_error(None, "Authentication cancelled")
                    return
            else:
                logger.debug("Built-in file manager: No password found, but password auth not enabled, proceeding with key-based auth")

        # Start connection after everything is set up
        try:
            self._connect_via_master()
        except Exception as exc:
            logger.exception("Error connecting to server: %s", exc)

    def _connect_via_master(self) -> None:
        """Master-first connect: ride a live ControlMaster socket when one
        exists; otherwise establish one on an app-owned PTY (MasterSession) so
        interactive prompts (2FA codes, PINs, host keys) can be answered, then
        let the PTY-less SFTP worker ride the socket. Falls back to a plain
        direct connect when there is no Connection object to build from."""
        connection = self._connection
        if connection is None:
            self._manager.connect_to_server()
            return
        if self._auth_master_active:
            logger.debug("Master session already active; ignoring reconnect")
            return
        # The pre-connect dialog stores the password on the manager only;
        # surface it on the connection so the master can auto-answer with it
        # (the worker's _build_argv does the same at build time).
        manager_password = getattr(self._manager, "_password", None)
        if manager_password:
            try:
                connection.password = manager_password
            except Exception:
                pass
        self._auth_master_active = True

        def _probe_and_start() -> None:
            from .ssh_master_session import MasterSession, check_master_alive

            app_config = None
            try:
                from .config import Config

                app_config = Config()
            except Exception:
                pass
            if check_master_alive(connection, self._connection_manager, app_config):
                logger.debug("Live ControlMaster socket found; connecting directly")
                GLib.idle_add(self._on_master_ready)
                return
            try:
                session = MasterSession(
                    connection,
                    self._connection_manager,
                    app_config,
                    on_ready=lambda: GLib.idle_add(self._on_master_ready),
                    on_needs_interaction=lambda fd, transcript: GLib.idle_add(
                        self._on_master_needs_interaction, fd, transcript
                    ),
                    on_need_password=lambda prompt: GLib.idle_add(
                        self._on_master_need_password, prompt
                    ),
                    on_failed=lambda tail: GLib.idle_add(
                        self._on_master_failed, tail
                    ),
                )
                self._master_session = session
                session.start()
            except Exception as exc:
                logger.exception("Failed to start master session: %s", exc)
                GLib.idle_add(self._on_master_failed, str(exc))

        threading.Thread(
            target=_probe_and_start, name="fm-master-probe", daemon=True
        ).start()

    def _master_display_name(self) -> str:
        nickname = (
            getattr(self._connection, "nickname", None) if self._connection else None
        )
        return nickname or f"{self._username}@{self._host}"

    def _on_master_ready(self) -> bool:
        self._auth_master_active = False
        self._master_session = None
        if self._auth_dialog is not None:
            try:
                self._auth_dialog.finish()
            except Exception:
                pass
            self._auth_dialog = None
        self._password_dialog_shown = False
        self._password_retry_count = 0
        manager = getattr(self, "_manager", None)
        if manager is not None:
            try:
                manager.connect_to_server()
            except Exception as exc:
                logger.exception("Error connecting to server: %s", exc)
        return False

    def _on_master_needs_interaction(self, master_fd: int, transcript: str) -> bool:
        if self._auth_dialog is not None:
            return False
        from .auth_terminal_dialog import AuthTerminalDialog

        dialog = AuthTerminalDialog(
            self._master_display_name(),
            master_fd,
            transcript,
            on_cancelled=self._on_auth_dialog_cancelled,
        )
        self._auth_dialog = dialog
        dialog.present(self)
        return False

    def _on_master_need_password(self, prompt_line: str) -> bool:
        session = self._master_session
        if session is None or self._password_dialog_shown:
            return False
        self._password_dialog_shown = True
        try:
            from .window import show_ssh_password_dialog

            display_name = self._master_display_name()
            password = show_ssh_password_dialog(
                from_widget=self,
                connection=self._connection,
                host=self._host,
                username=self._username,
                display_name=display_name,
                connection_manager=self._connection_manager,
                heading=_("Password Required"),
                body=_(
                    "{name} is asking for a password:"
                ).format(name=display_name),
            )
        finally:
            self._password_dialog_shown = False
        if password:
            session.send_secret(password)
        else:
            session.cancel()
            self._on_master_cancelled()
        return False

    def _on_auth_dialog_cancelled(self) -> None:
        self._auth_dialog = None
        session = self._master_session
        if session is not None:
            session.cancel()
        self._on_master_cancelled()

    def _on_master_cancelled(self) -> None:
        self._auth_master_active = False
        self._master_session = None
        self._on_connection_error(
            getattr(self, "_manager", None), _("Authentication cancelled")
        )

    def _on_master_failed(self, transcript_tail: str) -> bool:
        self._auth_master_active = False
        self._master_session = None
        if self._auth_dialog is not None:
            try:
                self._auth_dialog.finish()
            except Exception:
                pass
            self._auth_dialog = None
        lines = [
            line.strip()
            for line in (transcript_tail or "").splitlines()
            if line.strip() and not line.lstrip().startswith("debug")
        ]
        message = lines[-1] if lines else _("Could not establish the SSH connection")
        self._on_connection_error(getattr(self, "_manager", None), message)
        return False

    def _cancel_master_session(self) -> None:
        session = self._master_session
        self._master_session = None
        self._auth_master_active = False
        if session is not None:
            try:
                session.cancel()
            except Exception:
                pass
        if self._auth_dialog is not None:
            try:
                self._auth_dialog.finish()
            except Exception:
                pass
            self._auth_dialog = None

    # -- remote host-picker button (same pattern as the Docker Console) --

    def _install_remote_host_button(self) -> None:
        """Replace the right pane's static "Remote" title with a host-picker
        button: icon + current host + caret, opening the shared picker."""
        toolbar = getattr(self._right_pane, 'toolbar', None)
        label = getattr(toolbar, '_pane_label', None)
        parent = label.get_parent() if label is not None else None
        if parent is None:
            return
        btn = Gtk.Button()
        btn.add_css_class('flat')
        btn.set_tooltip_text(_("Choose remote host"))
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        icon = Gtk.Image.new_from_icon_name('computer-symbolic')
        icon.set_pixel_size(16)
        box.append(icon)
        self._remote_host_label = Gtk.Label()
        self._remote_host_label.set_css_classes(['title'])
        box.append(self._remote_host_label)
        caret = Gtk.Image.new_from_icon_name('pan-down-symbolic')
        caret.set_pixel_size(12)
        box.append(caret)
        btn.set_child(box)
        btn.connect('clicked', self._on_remote_host_button_clicked)
        label.set_visible(False)
        parent.insert_child_after(btn, label)
        self._remote_host_button = btn
        self._update_remote_host_button()

    def _update_remote_host_button(self) -> None:
        text = ((str(self._nickname).strip() if self._nickname else '')
                or self._host or _("Select host"))
        self._remote_host_label.set_text(text)

    def _on_remote_host_button_clicked(self, btn) -> None:
        from .host_picker import show_host_picker
        cm = self._connection_manager
        connections = cm.get_connections() if cm else []
        if not connections:
            self._right_pane.show_toast(_("No connections available"))
            return
        show_host_picker(None, btn, self._switch_remote_host,
                         connections=connections)

    def _teardown_backend(self) -> None:
        """Close the current SFTP backend and reset auth/error state."""
        manager = self._manager
        self._manager = None
        if manager is not None:
            try:
                manager.close()
            except Exception:
                logger.debug("Error closing previous SFTP backend", exc_info=True)
        self._connection_error_reported = False
        self._password_dialog_shown = False
        self._password_retry_count = 0

    def _reconnect(self) -> None:
        """Retry the connection to the current host from scratch."""
        self._teardown_backend()
        if self._host:
            self._start_connection()

    def _switch_remote_host(self, connection) -> None:
        """Point the remote pane at another host: tear down the current
        backend and reconnect via the shared pick handler."""
        if connection is self._connection and self._manager is not None:
            return
        self._teardown_backend()
        # Land in the new host's home, not the old host's last path, and
        # drop the old host's navigation history.
        self._pending_paths[self._right_pane] = "~"
        self._right_pane._history.clear()
        self._on_placeholder_host_picked(connection)

    # -- no-server host picker (same picker as empty split-view panes) ---

    def _show_host_picker_placeholder(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)

        icon = Gtk.Image.new_from_icon_name("folder-remote-symbolic")
        icon.set_pixel_size(48)
        icon.add_css_class("dim-label")
        box.append(icon)

        lbl = Gtk.Label(label=_("No server selected"))
        lbl.add_css_class("dim-label")
        lbl.add_css_class("title-3")
        box.append(lbl)

        btn = Gtk.Button(label=_("Select host"))
        btn.add_css_class("suggested-action")
        btn.add_css_class("pill")
        btn.set_halign(Gtk.Align.CENTER)

        def _on_clicked(_b):
            from .host_picker import show_host_picker
            cm = self._connection_manager
            show_host_picker(
                None, btn, self._on_placeholder_host_picked,
                connections=cm.get_connections() if cm else [],
            )

        btn.connect("clicked", _on_clicked)
        box.append(btn)

        self._host_picker_placeholder = box
        self._right_overlay.add_overlay(box)

    def _on_placeholder_host_picked(self, connection) -> None:
        from .connection_display import get_connection_host, get_connection_alias
        host = get_connection_host(connection) or get_connection_alias(connection) or ''
        if not host:
            self._right_pane.show_toast(_("Connection has no host"), timeout=5000)
            return
        self._host = host
        self._username = getattr(connection, 'username', '') or ''
        try:
            self._port = int(getattr(connection, 'port', 22) or 22)
        except (TypeError, ValueError):
            self._port = 22
        self._nickname = getattr(connection, 'nickname', None)
        self._connection = connection

        placeholder = getattr(self, '_host_picker_placeholder', None)
        if placeholder is not None:
            self._right_overlay.remove_overlay(placeholder)
            self._host_picker_placeholder = None

        title_parts = []
        if self._nickname and str(self._nickname).strip():
            title_parts.append(str(self._nickname).strip())
        base_identity = f"{self._username}@{host}"
        if not title_parts or title_parts[0] != base_identity:
            title_parts.append(base_identity)
        self._header_bar.set_title_widget(Gtk.Label(label=" ".join(title_parts)))
        self._update_remote_host_button()

        callback = getattr(self, '_host_picked_callback', None)
        if callback is not None:
            try:
                callback(connection)
            except Exception:
                logger.debug("host-picked callback failed", exc_info=True)

        self._start_connection()

    def detach_for_embedding(self, parent: Optional[Gtk.Widget] = None) -> Gtk.Widget:
        """Detach the window content for embedding in another container."""

        self._embedded_parent = parent
        content = getattr(self, '_toolbar_view', None)
        if content is None:
            raise RuntimeError("File manager UI is not initialised")

        enable_embedding_mode = getattr(self, 'enable_embedding_mode', None)
        if callable(enable_embedding_mode):
            enable_embedding_mode()

        try:
            current_child = self.get_content()
        except Exception:
            current_child = None

        if current_child is content:
            try:
                self.set_content(None)
            except Exception:
                # Fallback to unparent if set_content is unavailable
                try:
                    content.unparent()
                except Exception:
                    pass

        return content

    def enable_embedding_mode(self) -> None:
        """Adjust the window chrome for embedded usage."""

        if getattr(self, '_embedded_mode', False):
            return

        self._embedded_mode = True

        try:
            self.set_decorated(False)
        except Exception:  # pragma: no cover - defensive
            pass

        header_bar = getattr(self, '_header_bar', None)
        if header_bar is not None:
            try:
                header_bar.set_show_start_title_buttons(False)
                header_bar.set_show_end_title_buttons(False)
                header_bar.set_visible(False)
            except Exception:  # pragma: no cover - defensive UI cleanup
                try:
                    header_bar.hide()
                except Exception:
                    pass

        toolbar_view = getattr(self, '_toolbar_view', None)
        if toolbar_view is not None:
            try:
                toolbar_view.add_css_class('embedded')
            except Exception:  # pragma: no cover - optional styling
                pass

    # -- signal handlers ------------------------------------------------



    def _clear_progress_toast(self) -> None:
        """Clear the progress dialog safely."""
        if hasattr(self, '_progress_dialog') and self._progress_dialog is not None:
            try:
                self._progress_dialog.close()
            except (AttributeError, RuntimeError, GLib.GError):
                # Dialog might be destroyed or invalid, ignore
                pass
            finally:
                self._progress_dialog = None


    def _show_progress(self, fraction: float, message: str) -> None:
        """Update progress dialog if active."""
        if hasattr(self, '_progress_dialog') and self._progress_dialog is not None:
            try:
                self._progress_dialog.update_progress(fraction, message)
            except (AttributeError, RuntimeError, GLib.GError):
                # Dialog might be destroyed or invalid, ignore
                pass

    def _on_local_pane_toggle(self, toggle_button: Gtk.ToggleButton) -> None:
        """Handle local pane toggle button."""
        is_active = toggle_button.get_active()
        
        if is_active:
            # Hide local pane (button is pressed/selected)
            self._left_pane.set_visible(False)
            toggle_button.set_tooltip_text("Show Local Pane")
        else:
            # Show local pane (button is unpressed/unselected)
            self._left_pane.set_visible(True)
            toggle_button.set_tooltip_text("Hide Local Pane")

    def _on_connected(self, *_args) -> None:
        """Handle successful connection: reset password state and load directories."""
        self._password_dialog_shown = False
        self._password_retry_count = 0
        logger.debug(
            "Built-in file manager: Connection successful, reset password dialog state"
        )
        self._show_progress(0.4, "Connected")
        try:
            self._right_pane.set_connecting_status("Connected — loading files…")
        except Exception:
            pass
        for pane, pending in self._pending_paths.items():
            if pending:
                self._manager.listdir(pending)


    def _on_progress(self, _manager, fraction: float, message: str) -> None:
        self._show_progress(fraction, message)

    def _on_operation_error(self, _manager, message: str) -> None:
        """Handle operation error with toast."""
        # Cancel any pending loading toast timeouts since operation failed
        for pane, timeout_id in self._loading_toast_timeouts.items():
            if timeout_id is not None:
                GLib.source_remove(timeout_id)
                self._loading_toast_timeouts[pane] = None
                # Dismiss any loading toast that might be showing
                try:
                    pane.dismiss_toasts()
                except (AttributeError, RuntimeError, GLib.GError):
                    pass
        
        # A pane with a pending path means this error came from a directory
        # load — show the in-pane error state with Retry instead of a toast.
        target = next(
            (p for p, pending in self._pending_paths.items() if pending is not None),
            None,
        )
        if target is not None:
            failed_path = self._pending_paths[target]
            self._pending_paths[target] = None
            try:
                target.dismiss_toasts()
            except (AttributeError, RuntimeError, GLib.GError):
                pass
            target.show_load_error(failed_path, message)
            return

        from .file_manager.pane import present_error_alert, toast_overflows
        if toast_overflows(message):
            present_error_alert(self._toast_overlay, message)
            return
        try:
            toast = Adw.Toast.new(message)
            toast.set_priority(Adw.ToastPriority.HIGH)
            self._toast_overlay.add_toast(toast)
        except (AttributeError, RuntimeError, GLib.GError):
            # Overlay might be destroyed or invalid, ignore
            pass

    def _on_connection_error(self, _manager, message: str) -> None:
        """Handle connection error with toast and password-dialog state reset."""
        error_text = message or ""
        if (
            "authentication" not in error_text.lower()
            and "password" not in error_text.lower()
        ):
            self._password_dialog_shown = False
            self._password_retry_count = 0
            logger.debug(
                "Built-in file manager: Non-authentication connection error, "
                "reset password dialog state"
            )

        def show_error():
            try:
                self._clear_progress_toast()

                if getattr(self, '_connection_error_reported', False):
                    return

                # Show the in-pane error state (with Retry) on the remote pane
                # so the failure can't be mistaken for an empty directory, and
                # drop the persistent "Loading remote directory..." toast.
                if hasattr(self, '_right_pane') and self._right_pane:
                    timeout_id = self._loading_toast_timeouts.get(self._right_pane)
                    if timeout_id is not None:
                        GLib.source_remove(timeout_id)
                        self._loading_toast_timeouts[self._right_pane] = None
                    try:
                        self._right_pane.dismiss_toasts()
                    except (AttributeError, RuntimeError, GLib.GError):
                        pass
                    self._right_pane.show_load_error(
                        self._pending_paths.get(self._right_pane),
                        message or "Connection failed",
                    )
                elif hasattr(self, '_toast_overlay') and self._toast_overlay:
                    toast = Adw.Toast.new(message or "Connection failed")
                    toast.set_priority(Adw.ToastPriority.HIGH)
                    self._toast_overlay.add_toast(toast)
            except (AttributeError, RuntimeError, GLib.GError, TypeError) as exc:
                # Overlay might be destroyed or invalid, ignore
                logger.debug(f"Error showing connection error toast: {exc}")
            return False  # Don't repeat

        GLib.idle_add(show_error)

    def _cleanup_manager(self) -> None:
        """Close the file manager backend and clear UI state."""
        self._cancel_master_session()
        manager = getattr(self, "_manager", None)
        if manager is not None:
            try:
                logger.info("Cleaning up file manager backend resources")
                manager.close()
            except Exception as exc:
                logger.error(f"Error closing file manager backend: {exc}", exc_info=True)
            finally:
                self._manager = None
        self._clear_progress_toast()
        try:
            _file_manager_windows_registry.discard(self)
        except Exception:
            pass

    def _on_close_request(self, window) -> bool:
        """Handle window close request - clean up resources."""
        logger.info("FileManagerWindow close-request received, cleaning up")
        self._cleanup_manager()
        # Allow the window to close
        return False

    def _on_destroy(self, window) -> None:
        """Handle window destroy - ensure cleanup happens even if close-request wasn't called."""
        logger.info("FileManagerWindow destroy received, ensuring cleanup")
        self._cleanup_manager()

    def _is_password_auth_enabled(self, connection: Any = None) -> bool:
        """Check if password authentication is enabled/required for this connection.
        
        Returns True only if password auth is explicitly required or preferred:
        - auth_method == 1 (password auth explicitly selected)
        - pubkey_auth_no == True (pubkey disabled, password required)
        - preferred_authentications contains 'password' as the primary/preferred method
        
        Returns False for:
        - auth_method == 0 (key-based auth) with pubkey enabled
        - Combined auth scenarios (key-based with password fallback)
        - All key_select_mode values (0=try all, 1=specific key with IdentitiesOnly, 2=specific key without IdentitiesOnly)
        """
        if connection is None:
            return False
        
        try:
            # Check auth_method (1 = password, 0 = key-based)
            # This is the PRIMARY indicator - if it's 0, it's key-based auth, period
            auth_method = int(getattr(connection, "auth_method", 0) or 0)
            
            # If auth_method is 0 (key-based), don't show password prompt
            # Even if password is in PreferredAuthentications, it's just a fallback
            if auth_method == 0:
                logger.debug("Password auth disabled: auth_method == 0 (key-based auth)")
                return False
            
            # If auth_method is 1, password auth is explicitly selected
            if auth_method == 1:
                logger.debug("Password auth enabled: auth_method == 1")
                return True
            
            # Check if pubkey auth is disabled (forces password auth)
            if getattr(connection, "pubkey_auth_no", False):
                logger.debug("Password auth enabled: pubkey_auth_no == True")
                return True
            
            # Check preferred_authentications - only if password is the primary/preferred method
            # If publickey comes before password, it's key-based with password fallback - don't show prompt
            preferred_auth = getattr(connection, "preferred_authentications", None)
            if preferred_auth:
                auth_list = []
                if isinstance(preferred_auth, (list, tuple)):
                    auth_list = [str(a).lower() for a in preferred_auth]
                elif isinstance(preferred_auth, str):
                    auth_list = [a.strip().lower() for a in preferred_auth.split(',')]
                
                if auth_list:
                    # Only return True if password is the first/preferred method
                    if auth_list[0] == "password":
                        return True
                    
                    # If publickey comes before password, it's key-based auth (password is just fallback)
                    password_idx = auth_list.index("password") if "password" in auth_list else -1
                    publickey_idx = auth_list.index("publickey") if "publickey" in auth_list else -1
                    
                    # If publickey is not in the list at all and password is, password might be required
                    if publickey_idx == -1 and password_idx >= 0:
                        return True
                    
                    # If publickey comes before password, it's key-based auth - don't show prompt
                    if publickey_idx >= 0 and password_idx >= 0 and publickey_idx < password_idx:
                        return False
        except Exception as exc:
            logger.debug(f"Error checking password auth status: {exc}")
        
        # Default: key-based auth (auth_method == 0) - don't show password prompt
        return False

    def _show_password_dialog_before_connect(
        self, user: str, host: str, connection: Any = None
    ) -> Optional[str]:
        """Show password dialog before attempting connection."""
        from .window import show_ssh_password_dialog

        return show_ssh_password_dialog(
            from_widget=self,
            connection=connection,
            host=host,
            username=user,
            connection_manager=self._connection_manager,
        )

    def _on_authentication_required(self, _manager, error_message: str) -> None:
        """Handle authentication failure by showing password dialog."""
        logger.debug(f"Built-in file manager: _on_authentication_required called, error_message={error_message}")
        logger.debug(f"Built-in file manager: _password_dialog_shown={self._password_dialog_shown}, _password_retry_count={self._password_retry_count}")
        
        # Use GLib.idle_add to ensure we're on the main thread
        def show_password_dialog():
            try:
                self._clear_progress_toast()
                
                # Only show password dialog if password authentication is enabled
                if not self._is_password_auth_enabled(self._connection):
                    logger.debug("Built-in file manager: Authentication failed but password auth not enabled, showing error")
                    self._on_connection_error(_manager, "Authentication failed. Please check your SSH keys or enable password authentication.")
                    return False
                
                # Don't show multiple password dialogs
                if self._password_dialog_shown:
                    logger.debug("Built-in file manager: Password dialog already shown, ignoring duplicate authentication-required signal")
                    return False
                
                # Check retry limit
                if self._password_retry_count >= self._max_password_retries:
                    logger.warning(f"Built-in file manager: Maximum password retry limit ({self._max_password_retries}) reached")
                    self._on_connection_error(_manager, f"Authentication failed after {self._max_password_retries} attempts. Please check your password.")
                    return False
                
                self._password_dialog_shown = True
                self._password_retry_count += 1
                logger.debug(
                    "Built-in file manager: Showing password dialog "
                    "(attempt %s/%s)",
                    self._password_retry_count,
                    self._max_password_retries,
                )

                username = self._manager._username
                host = self._manager._host
                nickname = (
                    getattr(self._connection, "nickname", None)
                    if self._connection
                    else None
                )
                display_name = nickname or f"{username}@{host}"
                from .window import show_ssh_password_dialog

                password = show_ssh_password_dialog(
                    from_widget=self,
                    connection=self._connection,
                    host=host,
                    username=username,
                    display_name=display_name,
                    connection_manager=self._connection_manager,
                    heading=_("Password Required"),
                    body=_(
                        "Authentication failed for {name}.\n\n"
                        "Please enter your password:"
                    ).format(name=display_name),
                )
                self._password_dialog_shown = False

                if password:
                    try:
                        self._manager._password = password
                        if self._connection is not None:
                            self._connection.password = password
                        self._manager.connect_to_server()
                    except Exception as exc:
                        logger.error("Error calling connect_to_server: %s", exc)
                        self._on_connection_error(
                            self._manager, f"Failed to connect: {exc}"
                        )
                elif password is None:
                    self._password_retry_count = 0
                    self._on_connection_error(
                        self._manager, "Authentication cancelled"
                    )

                return False
            except Exception as exc:
                logger.error(
                    "Built-in file manager: Error in show_password_dialog: %s",
                    exc,
                    exc_info=True,
                )
                self._password_dialog_shown = False
                return False

        GLib.idle_add(show_password_dialog)

    def _on_directory_loaded(
        self, _manager, path: str, entries: Iterable[FileEntry]
    ) -> None:
        entries_list = list(entries)  # Convert to list for logging and reuse
        logger.debug(f"_on_directory_loaded: path={path}, entries_count={len(entries_list)}")
        
        # Prefer the pane explicitly waiting for this exact path; otherwise
        # assign to the next pane that still has a pending request. This makes
        # initial dual loads robust even if the backend normalizes paths.
        target = next((pane for pane, pending in self._pending_paths.items() if pending == path), None)
        logger.debug(f"_on_directory_loaded: target pane found by exact path match: {target is not None}")
        
        if target is None:
            # Prefer whichever pane still has an outstanding remote refresh. If
            # no pane recorded the request (e.g. backend normalised the path
            # before we tracked it) make the remote pane the default so results
            # are never routed to the local view.
            target = next(
                (pane for pane, pending in self._pending_paths.items() if pending is not None),
                None,
            )
            if target is None and self._right_pane in self._pending_paths:
                target = self._right_pane
            if target is None:
                target = self._left_pane
            logger.debug(f"_on_directory_loaded: fallback target pane: {(target == self._right_pane and 'remote') or 'local'}")
        
        # Clear the pending flag for the resolved pane
        logger.debug(f"_on_directory_loaded: clearing pending path for target pane")
        self._pending_paths[target] = None
        
        # Cancel loading toast timeout if still pending
        timeout_id = self._loading_toast_timeouts.get(target)
        if timeout_id is not None:
            GLib.source_remove(timeout_id)
            self._loading_toast_timeouts[target] = None
            logger.debug(f"_on_directory_loaded: cancelled loading toast timeout for target pane")

        logger.debug(f"_on_directory_loaded: calling show_entries on target pane")
        target.show_entries(path, entries_list)
        self._apply_pending_highlight(target)
        target.push_history(path)
        
        # Dismiss any loading toast after directory load is fully complete
        try:
            target.dismiss_toasts()
            logger.debug(f"_on_directory_loaded: dismissed loading toasts for target pane")
        except (AttributeError, RuntimeError, GLib.GError):
            # Method might not exist or overlay might be destroyed, ignore
            pass
        
        # Show success toast if this was a refresh
        if target in self._refreshing_panes:
            try:
                target.show_toast("Directory refreshed", timeout=2)
                logger.debug(f"_on_directory_loaded: showed refresh success toast for {('remote' if target._is_remote else 'local')} pane")
            except (AttributeError, RuntimeError, GLib.GError):
                pass
            finally:
                self._refreshing_panes.discard(target)
        
        logger.debug(f"_on_directory_loaded: completed directory load for {path}")

    def _on_directory_counts(self, _manager, path: str, counts) -> None:
        """Background folder item-counts arrived; forward to whichever pane is
        currently showing this path (the pane also guards on its current path)."""
        for pane in (self._left_pane, self._right_pane):
            if pane is None:
                continue
            try:
                if getattr(pane, "_current_path", None) == path:
                    pane.update_item_counts(path, counts)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("update_item_counts failed: %s", exc)

    # -- local filesystem helpers ---------------------------------------

    def _load_local(self, path: str) -> None:
        """Load local directory contents into the left pane.

        This is a synchronous operation using the local filesystem.
        """
        try:
            path = os.path.expanduser(path or "~")
            if not os.path.isabs(path):
                path = os.path.abspath(path)
            if not os.path.isdir(path):
                raise NotADirectoryError(f"Not a directory: {path}")

            entries: List[FileEntry] = []
            with os.scandir(path) as it:
                for dirent in it:
                    try:
                        stat = dirent.stat(follow_symlinks=False)
                        is_dir = dirent.is_dir(follow_symlinks=False)
                        item_count = None
                        
                        # Count items in directory
                        if is_dir:
                            try:
                                with os.scandir(dirent.path) as dir_it:
                                    item_count = len(list(dir_it))
                            except Exception:
                                # If we can't read the directory, set count to None
                                item_count = None
                        
                        entries.append(
                            FileEntry(
                                name=dirent.name,
                                is_dir=is_dir,
                                size=getattr(stat, "st_size", 0) or 0,
                                modified=getattr(stat, "st_mtime", 0.0) or 0.0,
                                item_count=item_count,
                            )
                        )
                    except Exception:
                        # Skip entries we cannot stat
                        continue

            # Show results in the left pane
            self._left_pane.show_entries(path, entries)
            self._apply_pending_highlight(self._left_pane)
            
            # Show success toast if this was a refresh
            if self._left_pane in self._refreshing_panes:
                try:
                    self._left_pane.show_toast("Directory reloadeds", timeout=2)
                    logger.debug(f"_load_local: showed refresh success toast for local pane")
                except (AttributeError, RuntimeError, GLib.GError):
                    pass
                finally:
                    self._refreshing_panes.discard(self._left_pane)
        except Exception as exc:
            self._left_pane.show_toast(str(exc))
            # Clear refresh flag on error
            self._refreshing_panes.discard(self._left_pane)

    def _on_path_changed(self, pane: FilePane, path: str, user_data=None) -> None:
        # Detect if this is a refresh (same path as current)
        current_path = getattr(pane, "_current_path", None)
        is_refresh = current_path and os.path.normpath(path) == os.path.normpath(current_path)
        
        # Route local vs remote browsing
        if pane is self._left_pane:
            # Local pane: expand ~ and navigate local filesystem
            local_path = os.path.expanduser(path) if path.startswith("~") else path
            if not local_path:
                local_path = os.path.expanduser("~")
            # Mark as refreshing if it's a refresh
            if is_refresh:
                self._refreshing_panes.add(pane)
            try:
                self._load_local(local_path)
                # Only push history if not triggered by Back
                if getattr(pane, "_suppress_history_push", False):
                    pane._suppress_history_push = False
                else:
                    pane.push_history(local_path)
            except Exception as exc:
                pane.show_toast(str(exc))
                # Clear refresh flag on error
                self._refreshing_panes.discard(pane)
        else:
            # Remote pane: use SFTP manager. Absent (picker mode) or
            # disconnected (failed connect) → remember the path and, when a
            # host is known, reconnect; the path is listed on 'connected'.
            manager = self._manager
            if manager is None or getattr(manager, '_client', None) is None:
                self._pending_paths[pane] = path
                if manager is not None and self._host:
                    self._reconnect()
                return
            self._pending_paths[pane] = path

            # Cancel any existing loading toast timeout for this pane
            timeout_id = self._loading_toast_timeouts.get(pane)
            if timeout_id is not None:
                GLib.source_remove(timeout_id)
                self._loading_toast_timeouts[pane] = None
            
            # Mark as refreshing if it's a refresh
            if is_refresh:
                self._refreshing_panes.add(pane)
            # Only push history if not triggered by Back
            if getattr(pane, "_suppress_history_push", False):
                pane._suppress_history_push = False
            else:
                pane.push_history(path)
            
            # Start a timeout to show loading toast if directory takes a while to load
            def show_loading_toast():
                """Show loading toast after delay if still loading."""
                # Check if this path is still pending (hasn't loaded yet)
                if self._pending_paths.get(pane) == path:
                    try:
                        pane.show_toast("Loading directory…", timeout=-1)
                        logger.debug(f"Showing loading toast for pane at path: {path}")
                    except (AttributeError, RuntimeError, GLib.GError):
                        pass
                self._loading_toast_timeouts[pane] = None
                return False  # Don't repeat
            
            # Show loading toast after 500ms if directory hasn't loaded yet
            timeout_id = GLib.timeout_add(500, show_loading_toast)
            self._loading_toast_timeouts[pane] = timeout_id
            
            self._manager.listdir(path)

    def _restore_flatpak_folder(self) -> bool:
        """Open the local pane on a granted folder after window init.

        Prefer the user's home folder when it has been granted (the sandbox can't
        reach ``~`` otherwise); fall back to the most recently granted folder.
        """
        try:
            home = os.path.expanduser("~")
            portal_result = _load_grant_for_host(home) or _load_first_doc_path()
            if portal_result:
                portal_path, doc_id, entry = portal_result
                logger.debug(f"Scheduled restoration of: {portal_path} (doc_id={doc_id})")
                # Directly call _load_local instead of emitting signals
                self._load_local(portal_path)
                self._left_pane.push_history(portal_path)
                logger.info(f"Successfully restored access to folder: {portal_path}")
        except Exception as e:
            logger.warning(f"Failed to restore Flatpak folder access: {e}")
        return False  # Don't repeat this idle callback

    def _check_file_conflicts(self, files_to_transfer: List[Tuple[str, str]], operation_type: str, callback: Callable[[List[Tuple[str, str]]], None]) -> None:
        """Check for file conflicts and show resolution dialog if needed.

        Args:
            files_to_transfer: List of (source, destination) tuples
            operation_type: "upload" or "download"
            callback: Function to call with resolved file list
        """
        logger.debug("=== CHECKING FILE CONFLICTS ===")
        logger.debug("Operation type: %s", operation_type)
        logger.debug("Files to transfer: %s", files_to_transfer)

        def _finalize_conflicts(conflicts: List[Tuple[str, str]]) -> None:
            logger.debug("Total conflicts found: %d", len(conflicts))

            if not conflicts:
                # No conflicts, proceed with all transfers
                # But first, verify connection is still valid for uploads
                if operation_type == "upload":
                    if self._manager is None:
                        logger.error("_finalize_conflicts: Manager is None, connection was closed during conflict check")
                        # Try to show error to user - find a pane to show toast
                        if hasattr(self, '_right_pane') and self._right_pane:
                            self._right_pane.show_toast("Connection lost. Please reconnect and try again.")
                        return
                    
                    try:
                        with self._manager._lock:
                            if self._manager._sftp is None:
                                logger.error("_finalize_conflicts: SFTP connection closed during conflict check")
                                # Try to show error to user - find a pane to show toast
                                if hasattr(self, '_right_pane') and self._right_pane:
                                    self._right_pane.show_toast("Connection lost. Please reconnect and try again.")
                                return
                    except Exception as e:
                        logger.error(f"_finalize_conflicts: Error checking connection: {e}")
                        if hasattr(self, '_right_pane') and self._right_pane:
                            self._right_pane.show_toast(f"Connection error: {e!s}")
                        return
                
                logger.debug("No conflicts, proceeding with transfers")
                callback(files_to_transfer)
                return

            # Check if manager is still available before showing conflict dialog (for uploads)
            if operation_type == "upload" and self._manager is None:
                logger.error("_finalize_conflicts: Manager is None, connection was closed during conflict check")
                if hasattr(self, '_right_pane') and self._right_pane:
                    self._right_pane.show_toast("Connection lost. Please reconnect and try again.")
                return
            
            # Show conflict resolution dialog
            conflict_count = len(conflicts)
            total_count = len(files_to_transfer)

            if conflict_count == 1:
                filename = os.path.basename(conflicts[0][1])
                title = "File Already Exists"
                message = f"'{filename}' already exists in the destination folder."
            else:
                title = "Files Already Exist"
                message = f"{conflict_count} of {total_count} files already exist in the destination folder."

            dialog = Adw.AlertDialog.new(title, message)
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("skip", "Skip Existing")
            dialog.add_response("replace", "Replace All")
            dialog.set_default_response("skip")
            dialog.set_close_response("cancel")

            def _on_conflict_response(_dialog, response: str) -> None:
                dialog.close()

                if response == "cancel":
                    return
                
                # Check if manager is still available for uploads
                if operation_type == "upload" and self._manager is None:
                    logger.error("_on_conflict_response: Manager is None, connection was closed")
                    if hasattr(self, '_right_pane') and self._right_pane:
                        self._right_pane.show_toast("Connection lost. Please reconnect and try again.")
                    return
                
                elif response == "skip":
                    # Only transfer files that don't conflict
                    non_conflicting = [item for item in files_to_transfer if item not in conflicts]
                    if non_conflicting:
                        callback(non_conflicting)
                        # Show toast about skipped files
                        if conflict_count == 1:
                            filename = os.path.basename(conflicts[0][1])
                            self._left_pane.show_toast(f"Skipped existing file: {filename}")
                        else:
                            self._left_pane.show_toast(f"Skipped {conflict_count} existing files")
                elif response == "replace":
                    # Transfer all files, replacing existing ones
                    callback(files_to_transfer)

            dialog.connect("response", _on_conflict_response)
            
            # Get the correct parent widget (handles both embedded tab and separate window cases)
            # Adw.AlertDialog.present() accepts a Gtk.Widget, so we can pass the embedded parent directly
            try:
                dialog_parent = self
                if self._embedded_parent is not None:
                    # If embedded as a tab, use the parent widget directly
                    dialog_parent = self._embedded_parent
                else:
                    # If standalone window, try to get transient parent if any
                    try:
                        transient = self.get_transient_for()
                        if transient is not None:
                            dialog_parent = transient
                    except Exception:
                        pass
                
                dialog.present(dialog_parent)  # Present with correct parent to center properly
            except Exception as e:
                # Fallback: present without parent if there's an error
                logger.error(f"Failed to present conflict dialog with parent: {e}", exc_info=True)
                dialog.present()  # Present without parent as fallback

        def _idle_finalize(conflicts: List[Tuple[str, str]]) -> bool:
            _finalize_conflicts(conflicts)
            return False

        if operation_type == "download":
            conflicts: List[Tuple[str, str]] = []
            for source, dest in files_to_transfer:
                logger.debug("Checking: %s -> %s", source, dest)
                exists = os.path.exists(dest)
                logger.debug("  Local file exists: %s", exists)
                if exists:
                    conflicts.append((source, dest))
                    logger.debug("  CONFLICT DETECTED: %s", dest)

            _finalize_conflicts(conflicts)
            return

        if operation_type == "upload":
            if not files_to_transfer:
                _finalize_conflicts([])
                return

            if self._manager is None:
                logger.warning("Upload conflict check requested without an active SFTP manager")
                _finalize_conflicts([])
                return

            pending = {"remaining": len(files_to_transfer)}
            conflicts: List[Tuple[str, str]] = []

            for source, dest in files_to_transfer:
                logger.debug("Checking: %s -> %s", source, dest)

                def _on_result(fut: Future, pair: Tuple[str, str] = (source, dest)) -> None:
                    try:
                        exists = fut.result()
                    except Exception as exc:
                        error_str = str(exc).lower()
                        # Check if connection was closed
                        if "connection" in error_str and ("closed" in error_str or "dropped" in error_str):
                            logger.error("Connection closed during conflict check for %s: %s", pair[1], exc)
                            # Don't proceed with conflict resolution if connection is closed
                            # Check if manager is still available
                            if self._manager is None:
                                logger.error("Manager is None, aborting conflict check")
                                if hasattr(self, '_right_pane') and self._right_pane:
                                    self._right_pane.show_toast("Connection lost. Please reconnect and try again.")
                                return
                        else:
                            logger.warning("Failed to check remote path %s: %s", pair[1], exc)
                        exists = False

                    logger.debug("  Remote file exists: %s", exists)
                    if exists:
                        conflicts.append(pair)
                        logger.debug("  CONFLICT DETECTED: %s", pair[1])

                    pending["remaining"] -= 1
                    if pending["remaining"] == 0:
                        # Before finalizing, check if manager is still available for uploads
                        if operation_type == "upload" and self._manager is None:
                            logger.error("Manager is None when finalizing conflicts, connection was closed")
                            if hasattr(self, '_right_pane') and self._right_pane:
                                self._right_pane.show_toast("Connection lost. Please reconnect and try again.")
                            return
                        GLib.idle_add(_idle_finalize, list(conflicts))

                future = self._manager.path_exists(dest)
                future.add_done_callback(_on_result)

            return

        # Unknown operation type, default to proceeding
        _finalize_conflicts([])

    def _on_request_operation(self, pane: FilePane, action: str, payload, user_data=None) -> None:
        if action in {"copy", "cut"} and isinstance(payload, dict):
            entries = list(payload.get("entries") or [])
            if not entries:
                pane.show_toast("Nothing selected")
                return

            directory = payload.get("directory") or pane.toolbar.path_entry.get_text() or "/"
            if pane is self._left_pane:
                # Use the actual current path instead of the display path from path entry
                # This handles Flatpak portal paths correctly
                current_path = getattr(pane, '_current_path', None)
                if current_path:
                    directory = current_path
                else:
                    directory = self._normalize_local_path(directory)
            else:
                directory = directory or "/"

            self._clipboard_entries = [dataclasses.replace(entry) for entry in entries]
            self._clipboard_directory = directory
            self._clipboard_source_pane = pane
            self._clipboard_operation = action
            self._update_paste_targets()

            if len(entries) == 1:
                message = f"{'Cut' if action == 'cut' else 'Copied'} {entries[0].name}"
            else:
                message = f"{'Cut' if action == 'cut' else 'Copied'} {len(entries)} items"
            pane.show_toast(message)
            return

        if action == "paste":
            if not self._clipboard_entries or self._clipboard_source_pane is None:
                pane.show_toast("Clipboard is empty")
                return

            destination = ""
            force_move = False
            if isinstance(payload, dict):
                destination = payload.get("directory") or pane.toolbar.path_entry.get_text() or "/"
                force_move = bool(payload.get("force_move"))
            
            # For local pane destinations, use actual current path instead of display path
            if pane is self._left_pane:
                current_path = getattr(pane, '_current_path', None)
                if current_path:
                    destination = current_path
                else:
                    destination = self._normalize_local_path(destination or pane.toolbar.path_entry.get_text() or "/")
            else:
                destination = pane.toolbar.path_entry.get_text() or "/"

            move_requested = force_move or self._clipboard_operation == "cut"
            source_pane = self._clipboard_source_pane
            source_dir = self._clipboard_directory or "/"
            entries = list(self._clipboard_entries)

            if source_pane is self._left_pane and pane is self._left_pane:
                self._perform_local_clipboard_operation(entries, source_dir, destination, move_requested)
            elif source_pane is self._right_pane and pane is self._right_pane:
                self._perform_remote_clipboard_operation(entries, source_dir, destination, move_requested)
            elif source_pane is self._left_pane and pane is self._right_pane:
                self._perform_local_to_remote_clipboard_operation(entries, source_dir, destination, move_requested)
            elif source_pane is self._right_pane and pane is self._left_pane:
                self._perform_remote_to_local_clipboard_operation(entries, source_dir, destination, move_requested)
            else:
                pane.show_toast("Paste target is unavailable")
                return

            if move_requested:
                self._clear_clipboard()
            else:
                self._update_paste_targets()
            return

        if action == "mkdir":
            dialog = Adw.AlertDialog.new("New Folder", "Enter a name for the new folder")
            entry = Gtk.Entry()
            entry.set_text("New Folder")
            dialog.set_extra_child(entry)
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("ok", "Create")
            dialog.set_default_response("ok")
            dialog.set_close_response("cancel")

            def _on_response(_dialog, response: str) -> None:
                if response == "ok":
                    name = entry.get_text().strip()
                    if name:
                        current_dir = pane.toolbar.path_entry.get_text() or "/"
                        if pane is self._left_pane:
                            target_dir = self._normalize_local_path(current_dir)
                            new_path = os.path.join(target_dir, name)
                        else:
                            new_path = posixpath.join(current_dir or "/", name)
                        if pane is self._left_pane:
                            try:
                                os.makedirs(new_path, exist_ok=False)
                            except FileExistsError:
                                pane.show_toast("Folder already exists")
                            except Exception as exc:
                                pane.show_toast(str(exc))
                            else:
                                # Refresh local listing
                                self._pending_highlights[self._left_pane] = name
                                self._load_local(os.path.dirname(new_path) or "/")
                        else:
                            future = self._manager.mkdir(new_path)
                            
                            # Simple direct refresh after operation completes
                            def _on_mkdir_done(completed_future):
                                try:
                                    completed_future.result()  # Check for errors
                                    logger.debug(f"mkdir completed successfully, refreshing pane")
                                    # Direct refresh of the current directory
                                    GLib.idle_add(lambda: self._force_refresh_pane(pane, highlight_name=name))
                                except Exception as e:
                                    logger.error(f"mkdir failed: {e}")
                            
                            future.add_done_callback(_on_mkdir_done)
                dialog.close()

            def _focus_entry():
                entry.grab_focus()
                entry.select_region(0, -1)  # Select all text

            def _on_entry_activate(_entry):
                # Trigger the "ok" response when Enter is pressed
                _on_response(dialog, "ok")

            entry.connect("activate", _on_entry_activate)
            dialog.connect("response", _on_response)
            dialog.present()
            # Focus the entry after the dialog is shown
            GLib.idle_add(_focus_entry)
        elif action == "newfile":
            dialog = Adw.AlertDialog.new("New File", "Enter a name for the new file")
            entry = Gtk.Entry()
            entry.set_text("untitled.txt")
            dialog.set_extra_child(entry)
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("ok", "Create")
            dialog.set_default_response("ok")
            dialog.set_close_response("cancel")

            def _on_response(_dialog, response: str) -> None:
                if response == "ok":
                    name = entry.get_text().strip()
                    if name:
                        current_dir = pane.toolbar.path_entry.get_text() or "/"
                        if pane is self._left_pane:
                            target_dir = self._normalize_local_path(current_dir)
                            new_path = os.path.join(target_dir, name)
                            try:
                                with open(new_path, "x"):
                                    pass
                            except FileExistsError:
                                pane.show_toast("File already exists")
                            except Exception as exc:
                                pane.show_toast(str(exc))
                            else:
                                self._pending_highlights[self._left_pane] = name
                                self._load_local(os.path.dirname(new_path) or "/")
                                self._open_path_in_editor(pane, new_path, name)
                        else:
                            new_path = posixpath.join(current_dir or "/", name)
                            future = self._manager.touch(new_path)

                            def _on_touch_done(completed_future, p=new_path, n=name) -> None:
                                try:
                                    completed_future.result()
                                except FileExistsError:
                                    GLib.idle_add(pane.show_toast, "File already exists")
                                    return
                                except Exception as e:
                                    GLib.idle_add(pane.show_toast, f"Failed to create file: {e}")
                                    return

                                def _after() -> bool:
                                    self._force_refresh_pane(pane, highlight_name=n)
                                    self._open_path_in_editor(pane, p, n)
                                    return False

                                GLib.idle_add(_after)

                            future.add_done_callback(_on_touch_done)
                dialog.close()

            def _focus_entry():
                entry.grab_focus()
                # Pre-select the base name (before the extension) for quick typing.
                text = entry.get_text()
                dot = text.rfind(".")
                entry.select_region(0, dot if dot > 0 else -1)

            def _on_entry_activate(_entry):
                _on_response(dialog, "ok")

            entry.connect("activate", _on_entry_activate)
            dialog.connect("response", _on_response)
            dialog.present()
            GLib.idle_add(_focus_entry)
        elif action == "rename" and isinstance(payload, dict):
            entries = payload.get("entries") or []
            directory = payload.get("directory") or pane.toolbar.path_entry.get_text() or "/"
            if not entries:
                return

            entry = entries[0]

            if pane is self._left_pane:
                base_dir = self._normalize_local_path(directory)
                source = os.path.join(base_dir, entry.name)
                join = os.path.join
            else:
                base_dir = directory or "/"
                source = posixpath.join(base_dir, entry.name)
                join = posixpath.join

            dialog = Adw.AlertDialog.new("Rename Item", f"Enter a new name for {entry.name}")
            name_entry = Gtk.Entry()
            name_entry.set_text(entry.name)
            dialog.set_extra_child(name_entry)
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("ok", "Rename")
            dialog.set_default_response("ok")
            dialog.set_close_response("cancel")

            def _on_rename(_dialog, response: str) -> None:
                if response != "ok":
                    dialog.close()
                    return
                new_name = name_entry.get_text().strip()
                if not new_name:
                    pane.show_toast("Name cannot be empty")
                    dialog.close()
                    return
                if new_name == entry.name:
                    dialog.close()
                    return
                target = join(base_dir, new_name)
                if pane is self._left_pane:
                    try:
                        os.rename(source, target)
                    except Exception as exc:
                        pane.show_toast(str(exc))
                    else:
                        pane.show_toast(f"Renamed to {new_name}")
                        self._pending_highlights[self._left_pane] = new_name
                        self._load_local(base_dir)
                else:
                    future = self._manager.rename(source, target)
                    
                    # Simple direct refresh after operation completes
                    def _on_rename_done(completed_future):
                        try:
                            completed_future.result()  # Check for errors
                            logger.debug(f"rename completed successfully, refreshing pane")
                            # Direct refresh of the current directory
                            GLib.idle_add(lambda: self._force_refresh_pane(pane, highlight_name=new_name))
                        except Exception as e:
                            logger.error(f"rename failed: {e}")
                    
                    future.add_done_callback(_on_rename_done)
                    pane.show_toast(f"Renaming to {new_name}…")
                dialog.close()

            def _focus_entry():
                name_entry.grab_focus()
                name_entry.select_region(0, -1)  # Select all text

            def _on_entry_activate(_entry):
                # Trigger the "ok" response when Enter is pressed
                _on_rename(dialog, "ok")

            name_entry.connect("activate", _on_entry_activate)
            dialog.connect("response", _on_rename)
            dialog.present()
            # Focus the entry after the dialog is shown
            GLib.idle_add(_focus_entry)
        elif action == "delete" and isinstance(payload, dict):
            entries = payload.get("entries") or []
            directory = payload.get("directory") or pane.toolbar.path_entry.get_text() or "/"
            if not entries:
                return

            if pane is self._left_pane:
                base_dir = self._normalize_local_path(directory)
            else:
                base_dir = directory or "/"

            count = len(entries)
            if count == 1:
                message = f"Delete {entries[0].name}?"
                title = "Delete Item"
            else:
                message = f"Delete {count} items?"
                title = "Delete Items"

            dialog = Adw.AlertDialog.new(title, message)
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("ok", "Delete")
            dialog.set_default_response("cancel")
            dialog.set_close_response("cancel")

            def _on_delete(_dialog, response: str) -> None:
                if response != "ok":
                    dialog.close()
                    return
                if pane is self._left_pane:
                    deleted = 0
                    errors: List[str] = []
                    for selected_entry in entries:
                        target_path = os.path.join(base_dir, selected_entry.name)
                        try:
                            if selected_entry.is_dir:
                                shutil.rmtree(target_path)
                            else:
                                os.remove(target_path)
                            deleted += 1
                        except FileNotFoundError:
                            errors.append(f"{selected_entry.name} no longer exists")
                        except Exception as exc:
                            errors.append(str(exc))
                    if deleted:
                        message = (
                            "Deleted 1 item"
                            if deleted == 1
                            else f"Deleted {deleted} items"
                        )
                        pane.show_toast(message)
                        self._load_local(base_dir)
                    if errors:
                        pane.show_toast(errors[0])
                else:
                    # Delete entries sequentially to avoid race conditions and hangs
                    errors: List[str] = []
                    total_count = len(entries)
                    
                    logger.info(f"Starting sequential deletion of {total_count} remote entries")
                    
                    def _delete_next(index: int) -> None:
                        """Delete the next entry in the list, then continue with the next one."""
                        if index >= total_count:
                            # All deletions complete
                            logger.info(f"All {total_count} deletions completed, refreshing pane")
                            GLib.idle_add(
                                lambda: self._on_all_deletes_complete(pane, base_dir, errors, total_count)
                            )
                            return
                        
                        selected_entry = entries[index]
                        target_path = posixpath.join(base_dir, selected_entry.name)
                        entry_name = selected_entry.name
                        
                        logger.info(f"Deleting {index + 1}/{total_count}: '{entry_name}'")
                        
                        def _on_delete_done(future_result: Future) -> None:
                            try:
                                future_result.result()  # Check for errors
                                logger.info(f"Successfully deleted '{entry_name}'")
                            except Exception as e:
                                error_msg = f"Failed to delete {entry_name}: {e!s}"
                                logger.error(f"Delete failed for '{entry_name}': {error_msg}", exc_info=True)
                                errors.append(error_msg)
                            
                            # Continue with next deletion on the main loop
                            GLib.idle_add(lambda: _delete_next(index + 1))
                        
                        try:
                            future = self._manager.remove(target_path)
                            future.add_done_callback(_on_delete_done)
                        except Exception as exc:
                            logger.error(f"Failed to create remove future for {entry_name}: {exc}", exc_info=True)
                            errors.append(f"Failed to delete {entry_name}: {exc!s}")
                            GLib.idle_add(lambda: _delete_next(index + 1))
                    
                    # Start sequential deletion
                    _delete_next(0)
                    
                    pane.show_toast(
                        "Deleting 1 item…" if count == 1 else f"Deleting {count} items…"
                    )
                dialog.close()

            dialog.connect("response", _on_delete)
            
            # Get the correct parent widget (handles both embedded tab and separate window cases)
            # Adw.AlertDialog.present() accepts a Gtk.Widget, so we can pass the embedded parent directly
            try:
                dialog_parent = self
                if self._embedded_parent is not None:
                    # If embedded as a tab, use the parent widget directly
                    dialog_parent = self._embedded_parent
                else:
                    # If standalone window, try to get transient parent if any
                    try:
                        transient = self.get_transient_for()
                        if transient is not None:
                            dialog_parent = transient
                    except Exception:
                        pass
                
                dialog.present(dialog_parent)  # Present with correct parent to center properly
            except Exception as e:
                # Fallback: present without parent if there's an error
                logger.error(f"Failed to present delete dialog with parent: {e}", exc_info=True)
                dialog.present()  # Present without parent as fallback
        elif action == "upload":
            # Upload can be triggered from either pane, but we need to determine the target pane
            if pane is self._left_pane:
                # Upload from local to remote
                target_pane = self._right_pane
            elif pane is self._right_pane:
                # Upload from local to remote (when triggered from remote pane)
                target_pane = pane
            else:
                return

            remote_root = target_pane.toolbar.path_entry.get_text() or "/"
            raw_items: object | None = None

            move_sources: List[pathlib.Path] = []
            move_source_dir: Optional[str] = None
            if isinstance(user_data, dict):
                move_sources = list(user_data.get("move_sources") or [])
                move_source_dir = user_data.get("move_source_dir")
            move_sources_set = {path.resolve() for path in move_sources}

            if isinstance(payload, dict):
                destination = payload.get("destination")
                if isinstance(destination, pathlib.Path):
                    remote_root = destination.as_posix()
                elif isinstance(destination, str) and destination:
                    remote_root = destination
                raw_items = payload.get("paths")
            else:
                raw_items = payload

            paths: List[pathlib.Path] = []

            def _collect(item: object | None) -> None:
                if item is None:
                    return
                if isinstance(item, (list, tuple, set, frozenset)):
                    for value in item:
                        _collect(value)
                    return
                if isinstance(item, pathlib.Path):
                    paths.append(item)
                elif isinstance(item, Gio.File):
                    local_path = item.get_path()
                    if local_path:
                        paths.append(pathlib.Path(local_path))
                elif isinstance(item, str):
                    paths.append(pathlib.Path(item))

            _collect(raw_items)

            if not paths:
                pane.show_toast("No files selected for upload")
                return

            available_paths: List[pathlib.Path] = []
            missing: List[pathlib.Path] = []
            for candidate in paths:
                try:
                    if candidate.exists():
                        available_paths.append(candidate)
                    else:
                        missing.append(candidate)
                except OSError:
                    missing.append(candidate)

            if missing and not available_paths:
                pane.show_toast("Selected items are not accessible")
                return
            if missing and available_paths:
                pane.show_toast(f"Skipping inaccessible items: {missing[0].name}")

            # Prepare list of files to transfer for conflict checking
            files_to_transfer = []
            for path_obj in available_paths:
                destination = posixpath.join(remote_root or "/", path_obj.name)
                files_to_transfer.append((str(path_obj), destination))
            
            # Check for conflicts and handle accordingly  
            def _proceed_with_upload(resolved_files: List[Tuple[str, str]]) -> None:
                if not resolved_files:
                    logger.warning("_proceed_with_upload: No files to upload")
                    return
                
                # Check if manager is still available and connected
                if self._manager is None:
                    pane.show_toast("Upload failed: Connection lost")
                    logger.error("_proceed_with_upload: Manager is None")
                    return
                
                # Check if SFTP connection is still valid
                try:
                    with self._manager._lock:
                        if self._manager._sftp is None:
                            pane.show_toast("Upload failed: Connection closed. Please reconnect.")
                            logger.error("_proceed_with_upload: SFTP connection is None")
                            return
                except Exception as e:
                    logger.error(f"_proceed_with_upload: Error checking connection: {e}")
                    pane.show_toast(f"Upload failed: {e!s}")
                    return
                
                total_files = len(resolved_files)
                logger.info(
                    "Starting upload of %d file%s",
                    total_files, "" if total_files == 1 else "s",
                )
                
                for local_path_str, destination in resolved_files:
                    path_obj = pathlib.Path(local_path_str)

                    try:
                        logger.debug(f"_proceed_with_upload: Starting upload of {path_obj.name}")
                        if path_obj.is_dir():
                            future = self._manager.upload_directory(path_obj, destination)
                        else:
                            future = self._manager.upload(path_obj, destination)

                        # Show progress dialog for upload (pass total_files for multi-file support)
                        self._show_progress_dialog(
                            "upload", path_obj.name, future,
                            total_files=total_files,
                            source_path=str(path_obj),
                            destination_path=destination,
                        )
                        self._attach_refresh(
                            future,
                            refresh_remote=target_pane,
                            highlight_name=path_obj.name,
                        )
                        if move_sources_set and path_obj.resolve() in move_sources_set:
                            cleanup_dir = move_source_dir or str(path_obj.parent)
                            self._schedule_local_move_cleanup(future, path_obj, cleanup_dir)
                    except Exception as e:
                        error_msg = str(e)
                        logger.error(f"_proceed_with_upload: Error uploading {path_obj.name}: {error_msg}", exc_info=True)
                        pane.show_toast(f"Error uploading {path_obj.name}: {error_msg}")

            self._check_file_conflicts(files_to_transfer, "upload", _proceed_with_upload)
        elif action == "download" and isinstance(payload, dict):
            logger.debug("=== DOWNLOAD OPERATION CALLED ===")
            logger.debug("Payload: %s", payload)

            if pane is self._left_pane and payload.get("entries"):
                remote_pane = getattr(self, "_right_pane", None)
                if isinstance(remote_pane, FilePane):
                    pane = remote_pane

            move_remote_sources: List[str] = []
            move_remote_pane: Optional[FilePane] = None
            if isinstance(user_data, dict):
                move_remote_sources = list(user_data.get("move_remote_sources") or [])
                move_remote_pane = user_data.get("move_remote_pane")
            move_remote_set = set(move_remote_sources)

            entries = payload.get("entries") or []
            directory = payload.get("directory")
            logger.debug("Entries to download: %s", [e.name for e in entries])
            logger.debug("Directory: %s", directory)
            if not directory:
                if pane is self._right_pane:
                    directory = pane.toolbar.path_entry.get_text() or "/"
                else:
                    remote_pane = getattr(self, "_right_pane", None)
                    if isinstance(remote_pane, FilePane):
                        directory = remote_pane.toolbar.path_entry.get_text() or "/"
                    else:
                        directory = "/"
            destination_base = payload.get("destination")

            if not entries or destination_base is None:
                pane.show_toast("Invalid download request")
                return

            if not isinstance(destination_base, pathlib.Path):
                destination_base = pathlib.Path(destination_base)

            # Prepare list of files to transfer for conflict checking
            files_to_transfer = []
            for entry in entries:
                source = posixpath.join(directory or "/", entry.name)
                target_path = destination_base / entry.name
                files_to_transfer.append((source, str(target_path)))
            
            # Check for conflicts and handle accordingly
            def _proceed_with_download(resolved_files: List[Tuple[str, str]]) -> None:
                total_files = len(resolved_files)
                for idx, (source, target_path_str) in enumerate(resolved_files):
                    target_path = pathlib.Path(target_path_str)
                    entry_name = os.path.basename(target_path_str)

                    # Find the original entry to check if it's a directory
                    entry_is_dir = False
                    for entry in entries:
                        if entry.name == entry_name:
                            entry_is_dir = entry.is_dir
                            break
                    
                    try:
                        if entry_is_dir:
                            future = self._manager.download_directory(source, target_path)
                        else:
                            future = self._manager.download(source, target_path)
                        # Pass total_files so dialog can be reused for multiple files
                        self._show_progress_dialog(
                            "download", entry_name, future,
                            total_files=total_files,
                            source_path=source,
                            destination_path=str(target_path),
                        )
                        self._attach_refresh(
                            future,
                            refresh_local_path=str(destination_base),
                            highlight_name=entry_name,
                        )
                        if move_remote_set and source in move_remote_set:
                            self._schedule_remote_move_cleanup(
                                future,
                                source,
                                move_remote_pane or pane,
                            )
                    except Exception as e:
                        pane.show_toast(f"Error downloading {entry_name}: {e!s}")

            self._check_file_conflicts(files_to_transfer, "download", _proceed_with_download)


    def _on_window_resize(self, window, pspec) -> None:
        """Maintain proportional paned split when window is resized following GNOME HIG"""
        self._update_split_position()

    def _on_content_size_allocate(self, _widget: Gtk.Widget, allocation: Gdk.Rectangle) -> None:
        """Adjust split position based on the actual allocated width of the content."""
        width = getattr(allocation, "width", 0) or 0
        if width <= 0:
            return
        self._update_split_position(width)

    def _on_panes_size_changed(self, panes: Gtk.Paned, pspec: GObject.ParamSpec) -> None:
        """Handle panes widget size changes to maintain proportional split."""
        # Get the current allocation width
        width = panes.get_allocated_width()
        if width > 0:
            self._update_split_position(width)

    def _set_initial_split_position(self) -> None:
        """Set the initial proportional split position after the widget is realized."""
        panes = getattr(self, "_panes", None)
        if panes is None:
            return
        
        # Wait for the widget to be allocated
        width = panes.get_allocated_width()
        if width > 0:
            self._update_split_position(width)
            return False  # Don't repeat
        else:
            return True  # Try again later

    def _compute_effective_split_width(self) -> int:
        """Determine the appropriate width to use when sizing the split view."""
        panes = getattr(self, "_panes", None)
        if panes is None:
            return 0

        if getattr(self, "_embedded_mode", False):
            overlay = getattr(self, "_toast_overlay", None)
            if overlay is not None:
                try:
                    width = overlay.get_allocated_width()
                except Exception:
                    width = 0
                if width:
                    return width

            try:
                width = panes.get_allocated_width()
            except Exception:
                width = 0
            if width:
                return width

        try:
            return self.get_width()
        except Exception:
            return 0

    def _update_split_position(self, width: Optional[int] = None) -> None:
        """Update the split position, preserving user adjustments where possible."""
        panes = getattr(self, "_panes", None)
        if panes is None:
            return

        if width is None or width <= 0:
            width = self._compute_effective_split_width()

        if not width:
            return

        last_width = getattr(self, "_last_split_width", 0)
        if width == last_width:
            return

        self._last_split_width = width

        try:
            panes.set_position(max(width // 2, 1))
        except Exception:
            pass

    def _attach_refresh(
        self,
        future: Optional[Future],
        *,
        refresh_remote: Optional[FilePane] = None,
        refresh_local_path: Optional[str] = None,
        highlight_name: Optional[str] = None,
    ) -> None:
        if future is None:
            logger.debug("_attach_refresh: future is None, skipping")
            return

        logger.debug(f"_attach_refresh: attaching refresh callback, refresh_remote={refresh_remote is not None}, highlight_name={highlight_name}")

        def _on_done(completed: Future) -> None:
            operation_succeeded = False
            apply_highlight = True
            try:
                completed.result()
                operation_succeeded = True
                logger.debug("_attach_refresh: operation completed successfully")
            except TransferCancelledException:
                # Transfer was cancelled mid-stream. The partial file (if any)
                # was already cleaned up by the worker. Refresh the listing so
                # the user sees the directory's current state, but skip the
                # highlight — the file we were transferring isn't there.
                logger.debug("_attach_refresh: transfer cancelled — refresh without highlight")
                operation_succeeded = True
                apply_highlight = False
            except Exception as e:
                error_str = str(e).lower()
                # Check if it's a socket/connection closed error - upload might still have succeeded
                if "socket is closed" in error_str or ("connection" in error_str and "closed" in error_str):
                    logger.debug(f"_attach_refresh: operation completed but socket closed: {e}")
                    # Still try to refresh - the upload might have succeeded before socket closed
                    operation_succeeded = True
                else:
                    logger.debug(f"_attach_refresh: operation failed with {e}")
                    # For other errors, don't refresh
                    return

            # Only refresh if operation succeeded (or socket closed, which might mean success)
            if operation_succeeded:
                if highlight_name and apply_highlight:
                    if refresh_remote is not None:
                        self._pending_highlights[refresh_remote] = highlight_name
                        logger.debug(f"_attach_refresh: set pending highlight {highlight_name} for remote pane")
                    elif refresh_local_path is not None:
                        self._pending_highlights[self._left_pane] = highlight_name
                        logger.debug(f"_attach_refresh: set pending highlight {highlight_name} for local pane")
                if refresh_remote is not None:
                    logger.debug("_attach_refresh: scheduling remote refresh")
                    GLib.idle_add(self._refresh_remote_listing, refresh_remote)
                if refresh_local_path:
                    logger.debug(f"_attach_refresh: scheduling local refresh for {refresh_local_path}")
                    GLib.idle_add(self._refresh_local_listing, refresh_local_path)

        future.add_done_callback(_on_done)

    def _on_all_deletes_complete(self, pane: FilePane, base_dir: str, errors: List[str], total_count: int) -> None:
        """Handle completion of all delete operations."""
        success_count = total_count - len(errors)
        
        if success_count > 0:
            message = (
                "Deleted 1 item"
                if success_count == 1
                else f"Deleted {success_count} items"
            )
            pane.show_toast(message)
        
        if errors:
            # Show first error
            pane.show_toast(errors[0])
            logger.error(f"Delete operation completed with {len(errors)} errors out of {total_count} items")
        
        # Refresh the pane to show updated directory contents
        if pane is self._right_pane:
            self._refresh_remote_listing(pane)
        else:
            self._load_local(base_dir)

    def _apply_pending_highlight(self, pane: FilePane) -> None:
        name = self._pending_highlights.get(pane)
        if not name:
            return
        self._pending_highlights[pane] = None
        pane.highlight_entry(name)

    def _force_refresh_pane(self, pane: FilePane, highlight_name: Optional[str] = None) -> None:
        """Force refresh a pane by directly calling listdir and updating UI"""
        path = pane.toolbar.path_entry.get_text() or "/"
        logger.debug(f"_force_refresh_pane: refreshing {('remote' if pane._is_remote else 'local')} pane for path: {path}")
        
        # Mark as refreshing to show success toast
        self._refreshing_panes.add(pane)
        
        if highlight_name:
            self._pending_highlights[pane] = highlight_name
            logger.debug(f"_force_refresh_pane: set pending highlight {highlight_name}")
        
        if pane._is_remote:
            # For remote pane, use SFTP
            self._pending_paths[pane] = path
            if self._manager is None:
                return
            try:
                logger.debug(f"_force_refresh_pane: calling manager.listdir for {path}")
                self._manager.listdir(path)
            except Exception as e:
                error_str = str(e).lower()
                # Check if it's a socket/connection closed error
                if "socket is closed" in error_str or ("connection" in error_str and "closed" in error_str):
                    logger.warning(f"_force_refresh_pane: connection closed, attempting to reconnect and refresh")
                    # Try to reconnect and then refresh
                    # The connection should be automatically re-established on next operation
                    # For now, just show a message and let user manually refresh
                    pane.show_toast("Connection closed. Please refresh manually.", timeout=3)
                else:
                    logger.error(f"_force_refresh_pane: listdir failed: {e}")
                    pane.show_toast(f"Refresh failed: {e}")
                # Clear refresh flag on error
                self._refreshing_panes.discard(pane)
        else:
            # For local pane, refresh directly
            try:
                self._load_local(path)
            except Exception as e:
                logger.error(f"_force_refresh_pane: local refresh failed: {e}")
                pane.show_toast(f"Refresh failed: {e}")
                # Clear refresh flag on error
                self._refreshing_panes.discard(pane)

    def _refresh_remote_listing(self, pane: FilePane) -> bool:
        """Legacy method - use _force_refresh_pane instead"""
        self._force_refresh_pane(pane)
        return False

    def _refresh_local_listing(self, path: str) -> bool:
        target = self._normalize_local_path(path)
        # Use the actual current path instead of the display path from path entry
        # This handles Flatpak portal paths correctly
        current_path = getattr(self._left_pane, '_current_path', None)
        if current_path:
            current = current_path
        else:
            current = self._normalize_local_path(self._left_pane.toolbar.path_entry.get_text())
        if target == current:
            self._load_local(target)
        else:
            self._pending_highlights[self._left_pane] = None
        return False

    def _update_paste_targets(self) -> None:
        can_paste = bool(self._clipboard_entries)
        for pane in (self._left_pane, self._right_pane):
            if isinstance(pane, FilePane):
                pane.set_can_paste(can_paste)

    def _clear_clipboard(self) -> None:
        self._clipboard_entries = []
        self._clipboard_directory = None
        self._clipboard_source_pane = None
        self._clipboard_operation = None
        self._update_paste_targets()

    def _resolve_local_entry_path(self, directory: str, entry: FileEntry) -> pathlib.Path:
        base = pathlib.Path(self._normalize_local_path(directory))
        return base / entry.name

    def _resolve_remote_entry_path(self, directory: str, entry: FileEntry) -> str:
        base = directory or "/"
        return posixpath.join(base, entry.name)

    def _perform_local_clipboard_operation(
        self,
        entries: List[FileEntry],
        source_dir: str,
        destination_dir: str,
        move: bool,
    ) -> None:
        source_dir_norm = self._normalize_local_path(source_dir)
        destination_dir_norm = self._normalize_local_path(destination_dir)
        source_base = pathlib.Path(source_dir_norm)
        destination_base = pathlib.Path(destination_dir_norm)
        destination_base.mkdir(parents=True, exist_ok=True)

        completed = 0
        errors: List[str] = []

        for entry in entries:
            source_path = source_base / entry.name
            destination_path = destination_base / entry.name
            try:
                if move:
                    shutil.move(str(source_path), str(destination_path))
                else:
                    if entry.is_dir:
                        if destination_path.exists():
                            raise FileExistsError(f"{entry.name} already exists")
                        shutil.copytree(source_path, destination_path)
                    else:
                        if destination_path.exists():
                            raise FileExistsError(f"{entry.name} already exists")
                        shutil.copy2(source_path, destination_path)
                completed += 1
            except FileExistsError as exc:
                errors.append(str(exc))
            except Exception as exc:
                errors.append(f"{entry.name}: {exc}")

        if completed:
            if entries:
                self._pending_highlights[self._left_pane] = entries[0].name
            GLib.idle_add(self._refresh_local_listing, destination_dir_norm)
            if move and destination_dir_norm != source_dir_norm:
                GLib.idle_add(self._refresh_local_listing, source_dir_norm)
            message = (
                "Moved 1 item"
                if move and completed == 1
                else f"Moved {completed} items"
                if move
                else "Copied 1 item"
                if completed == 1
                else f"Copied {completed} items"
            )
            self._left_pane.show_toast(message)

        if errors:
            self._left_pane.show_toast(errors[0])

    def _perform_remote_clipboard_operation(
        self,
        entries: List[FileEntry],
        source_dir: str,
        destination_dir: str,
        move: bool,
    ) -> None:
        if not entries:
            return
        manager = getattr(self, "_manager", None)
        if manager is None:
            self._right_pane.show_toast("Remote connection unavailable")
            return

        skipped: List[str] = []
        work_items: List[tuple[str, str, FileEntry, bool]] = []

        for entry in entries:
            source_path = self._resolve_remote_entry_path(source_dir, entry)
            destination_path = self._resolve_remote_entry_path(destination_dir, entry)

            if entry.is_dir and self._is_remote_descendant(source_path, destination_path):
                skipped.append(
                    f"Cannot paste '{entry.name}' into its own subdirectory"
                )
                continue

            work_items.append(
                (source_path, destination_path, entry, entry.is_dir)
            )

        if not work_items:
            if skipped:
                self._right_pane.show_toast(skipped[0])
            return

        operation = "move" if move else "copy"
        total_files = len(work_items)

        for source_path, destination_path, entry, is_dir in work_items:

            def _impl(
                src=source_path,
                dest=destination_path,
                copy_is_dir=is_dir,
            ):
                sftp = getattr(manager, "_sftp", None)
                if sftp is None:
                    raise RuntimeError("SFTP session is not connected")
                self._ensure_remote_directory(sftp, posixpath.dirname(dest))
                if copy_is_dir:
                    self._copy_remote_directory(sftp, src, dest)
                else:
                    self._copy_remote_file(sftp, src, dest)

            future = manager._submit(_impl)
            self._show_progress_dialog(
                operation,
                entry.name,
                future,
                total_files=total_files,
                source_path=source_path,
                destination_path=destination_path,
            )
            self._attach_refresh(
                future,
                refresh_remote=self._right_pane,
                highlight_name=entry.name,
            )
            if move:
                self._schedule_remote_move_cleanup(future, source_path, self._right_pane)

        if skipped:
            self._right_pane.show_toast(skipped[0])


    def _perform_local_to_remote_clipboard_operation(
        self,
        entries: List[FileEntry],
        source_dir: str,
        destination_dir: str,
        move: bool,
    ) -> None:
        source_dir_norm = self._normalize_local_path(source_dir)
        paths: List[pathlib.Path] = []
        for entry in entries:
            path = pathlib.Path(source_dir_norm) / entry.name
            if path.exists():
                paths.append(path)
        if not paths:
            self._left_pane.show_toast("Files are no longer available")
            return

        payload = {"paths": paths, "destination": destination_dir}
        user_data = None
        if move:
            user_data = {
                "move_sources": paths,
                "move_source_dir": source_dir_norm,
            }
        self._on_request_operation(self._left_pane, "upload", payload, user_data=user_data)


    def _perform_remote_to_local_clipboard_operation(
        self,
        entries: List[FileEntry],
        source_dir: str,
        destination_dir: str,
        move: bool,
    ) -> None:
        """Paste copied/cut remote entries into the local pane by downloading
        them — reuses the existing ``download`` handler (files + directories,
        conflict checks, progress dialog, refresh, and cut → remote delete)."""
        if not entries:
            return
        if getattr(self, "_manager", None) is None:
            self._left_pane.show_toast("Remote connection unavailable")
            return

        destination = pathlib.Path(self._normalize_local_path(destination_dir))
        payload = {
            "entries": entries,
            "directory": source_dir,
            "destination": destination,
        }
        user_data = None
        if move:
            sources = [self._resolve_remote_entry_path(source_dir, e) for e in entries]
            user_data = {
                "move_remote_sources": sources,
                "move_remote_pane": self._right_pane,
            }
        self._on_request_operation(self._right_pane, "download", payload, user_data=user_data)


    def _open_path_in_editor(self, pane: FilePane, path: str, name: str) -> None:
        """Open a (typically just-created) file in the text editor. Best-effort —
        the file already exists on disk/remote regardless of whether this opens."""
        try:
            is_local = pane is self._left_pane
            editor = RemoteFileEditorWindow(
                parent=self,
                file_path=path,
                file_name=name,
                is_local=is_local,
                sftp_manager=None if is_local else getattr(self, "_manager", None),
                file_manager_window=self,
            )
            editor.present()
        except Exception as e:
            logger.debug("Failed to open new file in editor: %s", e)


    def _schedule_local_move_cleanup(
        self,
        future: Future,
        source_path: pathlib.Path,
        source_dir: str,
    ) -> None:
        def _cleanup(completed: Future, path: pathlib.Path = source_path, base_dir: str = source_dir) -> None:
            try:
                completed.result()
            except Exception:
                return
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except FileNotFoundError:
                pass
            except Exception as exc:
                GLib.idle_add(self._left_pane.show_toast, f"Failed to remove {path.name}: {exc}")
            GLib.idle_add(self._refresh_local_listing, base_dir)

        future.add_done_callback(_cleanup)

    def _schedule_remote_move_cleanup(
        self,
        future: Future,
        source_path: str,
        pane: FilePane,
    ) -> None:
        def _cleanup(completed: Future, path: str = source_path, target_pane: FilePane = pane) -> None:
            try:
                completed.result()
            except Exception:
                return
            cleanup_future = self._manager.remove(path)
            self._attach_refresh(cleanup_future, refresh_remote=target_pane)

        future.add_done_callback(_cleanup)

    @staticmethod
    def _ensure_remote_directory(sftp: Any, path: str) -> None:
        if not path:
            return
        components = []
        while path and path not in {"/", ""}:
            components.append(path)
            path = posixpath.dirname(path)
        for component in reversed(components):
            try:
                sftp.mkdir(component)
            except OSError:
                continue

    @staticmethod
    def _remote_path_exists(sftp: Any, path: str) -> bool:
        return _sftp_path_exists(sftp, path)

    @staticmethod
    def _is_remote_descendant(source_path: str, destination_path: str) -> bool:
        source_norm = posixpath.normpath(source_path)
        dest_norm = posixpath.normpath(destination_path)
        if source_norm in {"", ".", "/"}:
            return False
        if dest_norm == source_norm:
            return True
        source_prefix = source_norm.rstrip("/")
        if not source_prefix:
            return False
        return dest_norm.startswith(f"{source_prefix}/")

    def _copy_remote_file(
        self, sftp: Any, source_path: str, destination_path: str
    ) -> None:
        if self._remote_path_exists(sftp, destination_path):
            raise FileExistsError(f"{posixpath.basename(destination_path)} already exists")

        with sftp.open(source_path, "rb") as src_file, sftp.open(destination_path, "wb") as dst_file:
            while True:
                chunk = src_file.read(32768)
                if not chunk:
                    break
                dst_file.write(chunk)

    def _copy_remote_directory(
        self, sftp: Any, source_path: str, destination_path: str
    ) -> None:
        if self._is_remote_descendant(source_path, destination_path):
            raise ValueError(
                f"Cannot paste '{posixpath.basename(posixpath.normpath(source_path))}' into itself"
            )
        if self._remote_path_exists(sftp, destination_path):
            raise FileExistsError(
                f"{posixpath.basename(posixpath.normpath(destination_path))} already exists"
            )
        sftp.mkdir(destination_path)

        for entry in sftp.listdir_attr(source_path):
            child_source = posixpath.join(source_path, entry.filename)
            child_destination = posixpath.join(destination_path, entry.filename)
            if stat_isdir(entry):
                self._copy_remote_directory(sftp, child_source, child_destination)
            else:
                self._copy_remote_file(sftp, child_source, child_destination)

    def _on_progress_dialog_closed(self, dialog) -> None:
        """Drop our reference when the user dismisses the progress dialog."""
        if getattr(self, "_progress_dialog", None) is dialog:
            self._progress_dialog = None

    def _show_progress_dialog(self, operation_type: str, filename: str, future: Future,
                               total_files: int = 1,
                               source_path: Optional[str] = None,
                               destination_path: Optional[str] = None) -> None:
        """Show and manage the progress dialog for a file operation."""
        try:
            logger.debug("_show_progress_dialog called for %s %s", operation_type, filename)

            # Reuse only when the existing dialog is still open and active.
            # A dismissed/completed dialog keeps its Python wrapper around until
            # the user starts another transfer; reusing that object skips
            # present() and nothing appears on screen.
            reuse_dialog = False
            if (
                hasattr(self, "_progress_dialog")
                and self._progress_dialog
                and self._progress_dialog.is_reusable()
                and self._progress_dialog.operation_type == operation_type
            ):
                reuse_dialog = True
                logger.debug("Reusing existing progress dialog for %s", operation_type)
            else:
                # Dismiss any stale or different-operation dialog.
                if hasattr(self, "_progress_dialog") and self._progress_dialog:
                    try:
                        self._progress_dialog.close()
                    except (AttributeError, RuntimeError):
                        pass
                    self._progress_dialog = None

            if not reuse_dialog:
                # Create new progress dialog
                logger.debug("Creating progress dialog")
                dialog_parent = self
                if self._embedded_parent is not None:
                    dialog_parent = self._embedded_parent
                else:
                    try:
                        transient = self.get_transient_for()
                        if transient is not None:
                            dialog_parent = transient
                    except Exception:
                        pass

                self._progress_dialog = SFTPProgressDialog(parent=dialog_parent, operation_type=operation_type)
                self._progress_dialog.connect("closed", self._on_progress_dialog_closed)
                self._progress_dialog.set_operation_details(total_files=total_files, filename=filename)
                # AlertDialog.present takes a parent widget; MessageDialog
                # already had it set via set_transient_for in the dialog ctor.
                if _HAS_ALERT_DIALOG:
                    self._progress_dialog.present(dialog_parent)
                else:
                    self._progress_dialog.present()
                logger.debug("Progress dialog created and shown successfully")
            elif not self._progress_dialog.get_visible():
                # Same batch, dialog object reused but not visible — re-present.
                dialog_parent = self
                if self._embedded_parent is not None:
                    dialog_parent = self._embedded_parent
                else:
                    try:
                        transient = self.get_transient_for()
                        if transient is not None:
                            dialog_parent = transient
                    except Exception:
                        pass
                try:
                    if _HAS_ALERT_DIALOG:
                        self._progress_dialog.present(dialog_parent)
                    else:
                        self._progress_dialog.present()
                except Exception as exc:
                    logger.debug("Failed to re-present progress dialog: %s", exc)
            
            # Add future to dialog (will update total_files if needed). Real
            # byte counts arrive via the manager's progress-bytes signal — no
            # need to pre-set total_bytes here.
            self._progress_dialog.set_operation_details(total_files=total_files, filename=filename)
            self._progress_dialog.set_future(future)

            # Surface source and destination paths so the user can see where
            # the file is going / coming from. Both labels stay hidden until
            # one is provided.
            if source_path or destination_path:
                self._progress_dialog.set_paths(source_path, destination_path)

        except Exception as exc:
            logger.error("Error in _show_progress_dialog: %s", exc, exc_info=True)
            return
        
        # Only connect progress signal handler when creating a new dialog
        # When reusing, the handler is already connected
        if not reuse_dialog:
            # Store references for cleanup
            self._progress_handler_id = None
            self._progress_bytes_handler_id = None
            self._active_futures = []  # Track all active futures for multi-file transfers
            self._future_to_filename = {}  # Map futures to filenames for progress tracking
            
            # Connect progress signal. We compute the overall-progress
            # fraction exactly once here — the dialog stores and renders it
            # verbatim without re-applying multi-file math.
            def _on_progress(manager, progress: float, message: str) -> None:
                if not (self._progress_dialog and
                        not self._progress_dialog.is_cancelled and
                        getattr(self, '_active_futures', None)):
                    return
                active_count = sum(
                    1 for f in self._active_futures
                    if f and not f.done() and not f.cancelled()
                )
                if active_count == 0:
                    return
                try:
                    if self._progress_dialog.total_files > 1:
                        # Single-file directory transfers emit per-file
                        # fractions; flatten to one overall progress value.
                        completed = self._progress_dialog.files_completed
                        total = self._progress_dialog.total_files
                        overall_progress = (completed + progress) / total
                        # Don't claim 100% while files are still active.
                        if completed < total:
                            cap = (total - 1) / total
                            overall_progress = min(overall_progress, cap)
                        self._progress_dialog.update_progress(overall_progress, message)
                    else:
                        self._progress_dialog.update_progress(progress, message)
                except (AttributeError, RuntimeError, GLib.GError):
                    # Dialog may have been destroyed mid-emit.
                    pass

            def _on_progress_bytes(manager, transferred, total) -> None:
                if not (self._progress_dialog and not self._progress_dialog.is_cancelled):
                    return
                try:
                    GLib.idle_add(self._progress_dialog.on_bytes, transferred, total)
                except (AttributeError, RuntimeError, GLib.GError):
                    pass

            self._progress_handler_id = self._manager.connect("progress", _on_progress)
            self._progress_bytes_handler_id = self._manager.connect(
                "progress-bytes", _on_progress_bytes
            )
        
        # Add this future to the active futures list
        if not hasattr(self, '_active_futures'):
            self._active_futures = []
        if future not in self._active_futures:
            self._active_futures.append(future)
        
        # Map future to filename for progress tracking
        if not hasattr(self, '_future_to_filename'):
            self._future_to_filename = {}
        self._future_to_filename[future] = filename
        
        # Also update current_future for backward compatibility
        self._current_future = future
        
        def _on_complete(future_result) -> None:
            # Use GLib.idle_add to ensure we're on the main thread
            def _cleanup():
                # Remove this future from active futures list
                if hasattr(self, '_active_futures') and future_result in self._active_futures:
                    self._active_futures.remove(future_result)
                
                # Only disconnect progress signals if all futures are done
                active_count = sum(1 for f in getattr(self, '_active_futures', [])
                                 if f and not f.done())
                if active_count == 0:
                    if (hasattr(self, '_progress_handler_id') and self._progress_handler_id and
                        hasattr(self, '_manager') and self._manager is not None):
                        try:
                            self._manager.disconnect(self._progress_handler_id)
                        except (TypeError, RuntimeError, AttributeError):
                            pass
                        self._progress_handler_id = None
                    if (hasattr(self, '_progress_bytes_handler_id') and self._progress_bytes_handler_id and
                        hasattr(self, '_manager') and self._manager is not None):
                        try:
                            self._manager.disconnect(self._progress_bytes_handler_id)
                        except (TypeError, RuntimeError, AttributeError):
                            pass
                        self._progress_bytes_handler_id = None
                
                # Update dialog to show completion
                if self._progress_dialog:
                    try:
                        # Check if the future was cancelled first
                        if future_result.cancelled():
                            # Operation was cancelled, don't show completion
                            # The dialog will be closed by the cancel handler
                            pass
                        else:
                            # Check for exceptions
                            try:
                                exception = future_result.exception()
                                if exception:
                                    error_msg = str(exception)
                                    # Get filename for this future
                                    filename = self._future_to_filename.get(future_result, "unknown file")
                                    # Track failed file
                                    if hasattr(self._progress_dialog, '_failed_files'):
                                        self._progress_dialog._failed_files.append((filename, error_msg))
                                    logger.error(f"Upload failed for {filename}: {error_msg}")
                                    
                                    # For multi-file operations, don't show completion until all files are done
                                    active_count = sum(1 for f in getattr(self, '_active_futures', []) 
                                                     if f and not f.done())
                                    if active_count == 0:
                                        # All files are done (some may have failed)
                                        # Show completion with summary
                                        if hasattr(self._progress_dialog, '_failed_files') and self._progress_dialog._failed_files:
                                            # Some files failed
                                            failed_count = len(self._progress_dialog._failed_files)
                                            if failed_count == self._progress_dialog.total_files:
                                                # All files failed
                                                error_summary = self._progress_dialog._failed_files[0][1] if self._progress_dialog._failed_files else "Unknown error"
                                                self._progress_dialog.show_completion(success=False, error_message=error_summary)
                                            else:
                                                # Some succeeded, some failed
                                                error_msg = f"{failed_count} of {self._progress_dialog.total_files} files failed"
                                                self._progress_dialog.show_completion(success=False, error_message=error_msg)
                                        else:
                                            # All files succeeded (shouldn't happen if we're here, but handle it)
                                            self._progress_dialog.show_completion(success=True)
                                else:
                                    # File completed successfully
                                    self._progress_dialog.increment_file_count()
                                    
                                    # Only show completion dialog when ALL files are done
                                    active_count = sum(1 for f in getattr(self, '_active_futures', []) 
                                                     if f and not f.done())
                                    if active_count == 0:
                                        # All files completed successfully
                                        self._progress_dialog.show_completion(success=True)
                            except CancelledError:
                                # Future was cancelled, ignore
                                pass
                    except (AttributeError, RuntimeError, GLib.GError):
                        # Dialog may have been destroyed
                        pass
                
                # Only clear current_future when all transfers are done
                active_count = sum(1 for f in getattr(self, '_active_futures', []) 
                                 if f and not f.done())
                if active_count == 0:
                    self._current_future = None
            
            GLib.idle_add(_cleanup)
        
        # Connect future completion
        future.add_done_callback(_on_complete)

    @staticmethod
    def _normalize_local_path(path: Optional[str]) -> str:
        expanded = os.path.expanduser(path or "/")
        return os.path.abspath(expanded)


def launch_file_manager_window(
    *,
    host: str,
    username: str,
    port: int = 22,
    path: str = "~",
    parent: Optional[Gtk.Window] = None,
    transient_for_parent: bool = True,
    nickname: Optional[str] = None,
    connection: Any = None,
    connection_manager: Any = None,
    ssh_config: Optional[Dict[str, Any]] = None,
) -> FileManagerWindow:
    """Create and present the :class:`FileManagerWindow`.

    The function obtains the default application instance (``Gtk.Application``)
    if available; otherwise the caller must ensure the returned window remains
    referenced for the duration of its lifetime.

    Parameters
    ----------
    host, username, port, path
        Connection details used by :class:`FileManagerWindow`.
    parent
        Optional window that should act as the logical parent for stacking
        purposes.  When provided the new window may be set as transient for
        this parent depending on ``transient_for_parent``.
    transient_for_parent
        Set to ``False`` to avoid establishing a transient relationship with
        ``parent``.  This allows callers to request a free-floating window even
        when a parent reference is supplied.
    nickname, connection, connection_manager, ssh_config
        Optional context propagated to :class:`FileManagerWindow` so it can
        reuse saved credentials and SSH preferences.
    """

    app = Gtk.Application.get_default()
    if app is None:
        raise RuntimeError("An application instance is required to show the window")

    window = FileManagerWindow(
        application=app,
        host=host,
        username=username,
        port=port,
        initial_path=path,
        nickname=nickname,
        connection=connection,
        connection_manager=connection_manager,
        ssh_config=ssh_config,
    )
    if parent is not None and transient_for_parent:
        window.set_transient_for(parent)
    window.present()
    return window


__all__ = [
    "FileEntry",
    "FileManagerWindow",
    "SFTPProgressDialog",
    "launch_file_manager_window",
]

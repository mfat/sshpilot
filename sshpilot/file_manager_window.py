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

import collections
import dataclasses
import errno
import json
import mimetypes
import os
import pathlib
import posixpath
import shutil
import stat
import threading
import weakref
import time
import re
import tempfile
from datetime import datetime
from concurrent.futures import Future, ThreadPoolExecutor, CancelledError
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


import paramiko
from gi.repository import Adw, Gio, GLib, GObject, Gdk, Gtk, Pango

# Try to import GtkSourceView for syntax highlighting
try:
    import gi
    gi.require_version('GtkSource', '5')
    from gi.repository import GtkSource
    _HAS_GTKSOURCE = True
except (ImportError, ValueError, AttributeError):
    _HAS_GTKSOURCE = False
    GtkSource = None

from .platform_utils import is_flatpak, is_macos
from .text_editor import RemoteFileEditorWindow
from .file_manager import (
    AsyncSFTPManager,
    create_file_manager_backend,
    DOCS_JSON,
    FileEntry,
    FilePane,
    PaneControls,
    PaneToolbar,
    PathEntry,
    PropertiesDialog,
    SFTPProgressDialog,
    TransferCancelledException,
    _HAS_ALERT_DIALOG,
    _MainThreadDispatcher,
    _PROGRESS_DIALOG_BASE,
    _ensure_cfg_dir,
    _get_docs_json_path,
    _grant_persistent_access,
    _human_size,
    _human_time,
    _load_doc_config,
    _load_first_doc_path,
    _lookup_doc_entry,
    _lookup_document_path,
    _lookup_path_from_config,
    _mode_to_octal,
    _mode_to_str,
    _portal_doc_path,
    _pretty_path_for_display,
    _save_doc,
    _sftp_path_exists,
    stat_isdir,
    walk_remote,
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
        panes.set_end_child(self._right_pane)

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
            
            # Try each identifier until we find a password
            for lookup_host in lookup_hosts:
                try:
                    retrieved = connection_manager.get_password(lookup_host, lookup_user)
                    if retrieved:
                        logger.debug(
                            "Built-in file manager: Found password for %s@%s using identifier '%s'",
                            lookup_user,
                            lookup_host,
                            lookup_host
                        )
                        initial_password = retrieved
                        break
                except Exception as exc:
                    logger.debug(
                        "Built-in file manager: Password lookup failed for %s@%s (identifier '%s'): %s",
                        lookup_user,
                        lookup_host,
                        lookup_host,
                        exc
                    )

        self._manager = create_file_manager_backend(
            host,
            username,
            port,
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
        except Exception as exc:
            logger.exception("Error connecting signals: %s", exc)
        
        # Connect close-request and destroy handlers to clean up resources
        self.connect("close-request", self._on_close_request)
        self.connect("destroy", self._on_destroy)
        
        # Show initial progress before connecting
        try:
            self._show_progress(0.1, "Connecting…")
        except Exception as exc:
            logger.exception("Error showing progress: %s", exc)
        
        # Show loading toast in remote pane (infinite timeout until manually dismissed)
        try:
            self._right_pane.show_toast("Loading remote directory...", timeout=0)
        except (AttributeError, RuntimeError, GLib.GError):
            # Overlay might be destroyed or invalid, ignore
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
            self._manager.connect_to_server()
        except Exception as exc:
            logger.exception("Error connecting to server: %s", exc)
    
    def _on_connected(self, _manager) -> None:
        """Handle successful connection - reset password dialog state."""
        self._password_dialog_shown = False
        self._password_retry_count = 0
        logger.debug("Built-in file manager: Connection successful, reset password dialog state")
    
    def _on_connection_error(self, _manager, error_message: str) -> None:
        """Handle connection error - reset password dialog state if not authentication error."""
        # Only reset if this is not an authentication error (which would trigger authentication-required)
        # Authentication errors are handled by _on_authentication_required
        if "authentication" not in error_message.lower() and "password" not in error_message.lower():
            self._password_dialog_shown = False
            self._password_retry_count = 0
            logger.debug("Built-in file manager: Non-authentication connection error, reset password dialog state")
        
        # Show error toast
        self._clear_progress_toast()
        self._right_pane.show_toast(f"Connection error: {error_message}")

    def _on_close_request(self, window) -> bool:
        """Handle window close request - clean up resources."""
        logger.debug("FileManagerWindow close-request received, cleaning up")
        try:
            if hasattr(self, '_manager') and self._manager is not None:
                logger.debug("Closing AsyncSFTPManager")
                self._manager.close()
                self._manager = None
        except Exception as exc:
            logger.error(f"Error closing AsyncSFTPManager: {exc}", exc_info=True)
        
        # Clear progress dialog if it exists
        self._clear_progress_toast()
        
        # Allow the window to close
        return False

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
        self._show_progress(0.4, "Connected")
        
        # Trigger directory loads for all panes that have a pending initial path
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
        
        try:
            toast = Adw.Toast.new(message)
            toast.set_priority(Adw.ToastPriority.HIGH)
            self._toast_overlay.add_toast(toast)
        except (AttributeError, RuntimeError, GLib.GError):
            # Overlay might be destroyed or invalid, ignore
            pass

    def _on_connection_error(self, _manager, message: str) -> None:
        """Handle connection error with toast."""
        # Use GLib.idle_add to ensure we're on the main thread
        def show_error():
            try:
                self._clear_progress_toast()
                
                if getattr(self, '_connection_error_reported', False):
                    return
                
                # Try to show toast on right pane (remote pane) which is more reliable
                if hasattr(self, '_right_pane') and self._right_pane:
                    self._right_pane.show_toast(message or "Connection failed", timeout=5000)
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
        """Close the AsyncSFTPManager and clear UI state."""
        manager = getattr(self, "_manager", None)
        if manager is not None:
            try:
                logger.info("Cleaning up AsyncSFTPManager resources")
                manager.close()
            except Exception as exc:
                logger.error(f"Error closing AsyncSFTPManager: {exc}", exc_info=True)
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
        """Show password dialog before attempting connection.
        
        Returns the password if user provided it, None if cancelled.
        Uses GLib main loop to handle dialog interaction properly.
        """
        password_result = [None]  # Use list to allow modification in nested function
        main_loop = GLib.MainLoop()
        
        # Get display name
        nickname = getattr(connection, 'nickname', None) if connection else None
        display_name = nickname or f"{user}@{host}"
        
        # Get the correct parent window (handles both embedded tab and separate window cases)
        # Adw.MessageDialog.transient_for requires a Gtk.Window, so we need to get the root window
        dialog_parent_window: Optional[Gtk.Window] = None
        try:
            if self._embedded_parent is not None:
                # If embedded as a tab, get the root window from the parent widget
                root_window = self._embedded_parent.get_root()
                if root_window is not None and isinstance(root_window, Gtk.Window):
                    dialog_parent_window = root_window
            else:
                # If standalone window, try to get transient parent if any
                transient = self.get_transient_for()
                if transient is not None:
                    dialog_parent_window = transient
            
            # Fallback: try to get application's active window
            if dialog_parent_window is None:
                try:
                    app = self.get_application()
                    if app is not None:
                        active_window = app.get_active_window()
                        if active_window is not None and isinstance(active_window, Gtk.Window):
                            dialog_parent_window = active_window
                except Exception:
                    pass
            
            # Final fallback: use self if it's a window
            if dialog_parent_window is None:
                try:
                    self_root = self.get_root()
                    if self_root is not None and isinstance(self_root, Gtk.Window):
                        dialog_parent_window = self_root
                    else:
                        dialog_parent_window = self
                except Exception:
                    dialog_parent_window = self
        except Exception as e:
            logger.error(f"Error determining password dialog parent: {e}", exc_info=True)
            # Final fallback to self
            dialog_parent_window = self
        
        # Log the parent window for debugging
        logger.debug(f"Password dialog parent window: {dialog_parent_window}, type: {type(dialog_parent_window)}, embedded: {self._embedded_parent is not None}")
        
        # Create password dialog
        dialog = Adw.MessageDialog(
            transient_for=dialog_parent_window,
            modal=True,
            heading="Password Required",
            body=f"Please enter your password for {display_name}:",
        )
        
        # Ensure transient_for is set (in case it wasn't set in constructor)
        if dialog_parent_window is not None:
            try:
                dialog.set_transient_for(dialog_parent_window)
            except Exception:
                pass
        
        # Create a container box for entry and checkbox
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content_box.set_margin_top(12)
        content_box.set_margin_bottom(12)
        content_box.set_margin_start(12)
        content_box.set_margin_end(12)
        
        # Add password entry
        password_entry = Gtk.PasswordEntry()
        password_entry.set_property("placeholder-text", "Password")
        content_box.append(password_entry)
        
        # Add checkbox to store password
        store_checkbox = Gtk.CheckButton(label="Store password")
        store_checkbox.set_active(False)
        content_box.append(store_checkbox)
        
        # Add container to dialog's extra child area
        dialog.set_extra_child(content_box)
        
        # Add responses
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("connect", "Connect")
        dialog.set_default_response("connect")
        dialog.set_close_response("cancel")
        
        # Handle Enter key - try multiple approaches for maximum compatibility
        def on_entry_activate(_entry):
            """Handle Enter key press in password entry"""
            dialog.emit("response", "connect")
        
        # Try to set activates-default property (works for Gtk.Entry)
        try:
            password_entry.set_property("activates-default", True)
        except (TypeError, AttributeError):
            pass
        
        # Also connect to activate signal as fallback
        try:
            password_entry.connect("activate", on_entry_activate)
        except (TypeError, AttributeError):
            # Fallback to key controller if activate signal is not available
            key_controller = Gtk.EventControllerKey()
            def on_key_pressed(_controller, keyval, _keycode, _state):
                if keyval == Gdk.KEY_Return or keyval == Gdk.KEY_KP_Enter:
                    dialog.emit("response", "connect")
                    return True
                return False
            key_controller.connect("key-pressed", on_key_pressed)
            password_entry.add_controller(key_controller)
        
        # Focus password entry when dialog is shown
        def on_dialog_shown(_dialog):
            password_entry.grab_focus()
        dialog.connect("notify::visible", lambda d, _: on_dialog_shown(d) if d.get_visible() else None)
        
        def on_response(_dialog, response: str) -> None:
            if response == "connect":
                entered_password = password_entry.get_text()
                if entered_password:
                    password_result[0] = entered_password
                    
                    # Store password if checkbox is checked
                    if store_checkbox.get_active() and hasattr(self, '_connection_manager') and self._connection_manager:
                        try:
                            self._connection_manager.store_password(host, user, entered_password)
                        except Exception as e:
                            logger.debug(f"Failed to store password: {e}")
                else:
                    password_result[0] = None  # Empty password treated as cancel
            else:
                password_result[0] = None  # User cancelled
            dialog.destroy()
            main_loop.quit()
        
        dialog.connect("response", on_response)
        dialog.present()
        
        # Run main loop to wait for dialog response
        # This blocks until the dialog is closed
        main_loop.run()
        
        return password_result[0]

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
                logger.debug(f"Built-in file manager: Showing password dialog (attempt {self._password_retry_count}/{self._max_password_retries})")
                
                # Get connection info for dialog
                username = self._manager._username
                host = self._manager._host
                nickname = getattr(self._connection, 'nickname', None) if self._connection else None
                display_name = nickname or f"{username}@{host}"
                
                # Get the correct parent window (handles both embedded tab and separate window cases)
                # Adw.MessageDialog.transient_for requires a Gtk.Window, so we need to get the root window
                dialog_parent_window: Optional[Gtk.Window] = None
                try:
                    if self._embedded_parent is not None:
                        # If embedded as a tab, get the root window from the parent widget
                        root_window = self._embedded_parent.get_root()
                        if root_window is not None and isinstance(root_window, Gtk.Window):
                            dialog_parent_window = root_window
                    else:
                        # If standalone window, try to get transient parent if any
                        transient = self.get_transient_for()
                        if transient is not None:
                            dialog_parent_window = transient
                    
                    # Fallback: try to get application's active window
                    if dialog_parent_window is None:
                        try:
                            app = self.get_application()
                            if app is not None:
                                active_window = app.get_active_window()
                                if active_window is not None and isinstance(active_window, Gtk.Window):
                                    dialog_parent_window = active_window
                        except Exception:
                            pass
                    
                    # Final fallback: use self if it's a window
                    if dialog_parent_window is None:
                        try:
                            self_root = self.get_root()
                            if self_root is not None and isinstance(self_root, Gtk.Window):
                                dialog_parent_window = self_root
                            else:
                                dialog_parent_window = self
                        except Exception:
                            dialog_parent_window = self
                except Exception as e:
                    logger.error(f"Error determining password dialog parent: {e}", exc_info=True)
                    # Final fallback to self
                    dialog_parent_window = self
                
                # Log the parent window for debugging
                logger.debug(f"Password dialog parent window: {dialog_parent_window}, type: {type(dialog_parent_window)}, embedded: {self._embedded_parent is not None}")
                
                # Create password dialog
                dialog = Adw.MessageDialog(
                    transient_for=dialog_parent_window,
                    modal=True,
                    heading="Password Required",
                    body=f"Authentication failed for {display_name}.\n\nPlease enter your password:",
                )
                
                # Ensure transient_for is set (in case it wasn't set in constructor)
                if dialog_parent_window is not None:
                    try:
                        dialog.set_transient_for(dialog_parent_window)
                    except Exception:
                        pass
                
                # Create a container box for entry and checkbox
                content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
                content_box.set_margin_top(12)
                content_box.set_margin_bottom(12)
                content_box.set_margin_start(12)
                content_box.set_margin_end(12)
                
                # Add password entry
                password_entry = Gtk.PasswordEntry()
                password_entry.set_property("placeholder-text", "Password")
                content_box.append(password_entry)
                
                # Add checkbox to store password
                store_checkbox = Gtk.CheckButton(label="Store password")
                store_checkbox.set_active(False)
                content_box.append(store_checkbox)
                
                # Add container to dialog's extra child area
                dialog.set_extra_child(content_box)
                
                # Add responses
                dialog.add_response("cancel", "Cancel")
                dialog.add_response("connect", "Connect")
                dialog.set_default_response("connect")
                dialog.set_close_response("cancel")
                
                # Handle Enter key - try multiple approaches for maximum compatibility
                def on_entry_activate(_entry):
                    """Handle Enter key press in password entry"""
                    dialog.emit("response", "connect")
                
                # Try to set activates-default property (works for Gtk.Entry)
                try:
                    password_entry.set_property("activates-default", True)
                except (TypeError, AttributeError):
                    pass
                
                # Also connect to activate signal as fallback
                try:
                    password_entry.connect("activate", on_entry_activate)
                except (TypeError, AttributeError):
                    # Fallback to key controller if activate signal is not available
                    key_controller = Gtk.EventControllerKey()
                    def on_key_pressed(_controller, keyval, _keycode, _state):
                        if keyval == Gdk.KEY_Return or keyval == Gdk.KEY_KP_Enter:
                            dialog.emit("response", "connect")
                            return True
                        return False
                    key_controller.connect("key-pressed", on_key_pressed)
                    password_entry.add_controller(key_controller)
                
                # Focus password entry when dialog is shown
                def on_dialog_shown(_dialog):
                    password_entry.grab_focus()
                dialog.connect("notify::visible", lambda d, _: on_dialog_shown(d) if d.get_visible() else None)
                
                def on_response(_dialog, response: str) -> None:
                    # Get password and checkbox state before destroying dialog
                    entered_password = password_entry.get_text() if response == "connect" else None
                    should_store = store_checkbox.get_active() if response == "connect" else False
                    
                    # Destroy dialog first
                    dialog.destroy()
                    
                    # Reset the flag when dialog is closed so it can be shown again if authentication fails
                    # This allows the dialog to be shown again if the password is wrong
                    self._password_dialog_shown = False
                    
                    # Use GLib.idle_add to ensure UI operations happen on main thread
                    # and avoid race conditions with dialog destruction
                    def handle_response():
                        if response == "connect":
                            if entered_password:
                                # Store password in connection manager if checkbox is checked
                                if should_store and self._connection_manager is not None:
                                    try:
                                        lookup_host = host
                                        if self._connection is not None:
                                            hostname = getattr(self._connection, "hostname", None)
                                            host_attr = getattr(self._connection, "host", None)
                                            nickname_attr = getattr(self._connection, "nickname", None)
                                            lookup_host = hostname or host_attr or nickname_attr or lookup_host
                                        
                                        lookup_user = username
                                        if self._connection is not None:
                                            lookup_user = getattr(self._connection, "username", None) or lookup_user
                                        
                                        # Store password if checkbox was checked
                                        self._connection_manager.store_password(lookup_host, lookup_user, entered_password)
                                        logger.debug("Using password from dialog for connection")
                                    except Exception as exc:
                                        logger.debug(f"Failed to process password: {exc}")
                                
                                # Retry connection with password
                                # If authentication fails again, _on_authentication_required will be called
                                # and can show the dialog again since _password_dialog_shown is now False
                                try:
                                    self._manager.connect_to_server(password=entered_password)
                                except Exception as exc:
                                    logger.error(f"Error calling connect_to_server: {exc}")
                                    self._on_connection_error(self._manager, f"Failed to connect: {exc}")
                            else:
                                # Empty password, show error
                                self._on_connection_error(self._manager, "Password cannot be empty")
                        else:
                            # User cancelled - reset retry count
                            self._password_retry_count = 0
                            self._on_connection_error(self._manager, "Authentication cancelled")
                        return False  # Don't repeat
                    
                    GLib.idle_add(handle_response)
                
                dialog.connect("response", on_response)
                dialog.present()
                logger.debug("Built-in file manager: Password dialog presented")
                
                return False  # Don't repeat
            except Exception as exc:
                logger.error(f"Built-in file manager: Error in show_password_dialog: {exc}", exc_info=True)
                self._password_dialog_shown = False  # Reset flag on error
                return False
        
        # Schedule dialog creation on main thread
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
            logger.debug(f"_on_directory_loaded: fallback target pane: {target == self._right_pane and 'remote' or 'local'}")
        
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
            # Remote pane: use SFTP manager
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
        """Restore Flatpak folder access after window initialization is complete."""
        try:
            portal_result = _load_first_doc_path()
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
                            self._right_pane.show_toast(f"Connection error: {str(e)}")
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
                                error_msg = f"Failed to delete {entry_name}: {str(e)}"
                                logger.error(f"Delete failed for '{entry_name}': {error_msg}", exc_info=True)
                                errors.append(error_msg)
                            
                            # Continue with next deletion on the main loop
                            GLib.idle_add(lambda: _delete_next(index + 1))
                        
                        try:
                            future = self._manager.remove(target_path)
                            future.add_done_callback(_on_delete_done)
                        except Exception as exc:
                            logger.error(f"Failed to create remove future for {entry_name}: {exc}", exc_info=True)
                            errors.append(f"Failed to delete {entry_name}: {str(exc)}")
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
                    pane.show_toast(f"Upload failed: {str(e)}")
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
                        pane.show_toast(f"Error downloading {entry_name}: {str(e)}")

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
                if "socket is closed" in error_str or "connection" in error_str and "closed" in error_str:
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
        scheduled_any = False

        for entry in entries:
            source_path = self._resolve_remote_entry_path(source_dir, entry)
            destination_path = self._resolve_remote_entry_path(destination_dir, entry)

            if entry.is_dir and self._is_remote_descendant(source_path, destination_path):
                skipped.append(
                    f"Cannot paste '{entry.name}' into its own subdirectory"
                )
                continue


            def _impl(src=source_path, dest=destination_path, is_dir=entry.is_dir):
                sftp = getattr(manager, "_sftp", None)
                if sftp is None:
                    raise RuntimeError("SFTP session is not connected")
                self._ensure_remote_directory(sftp, posixpath.dirname(dest))
                if is_dir:
                    self._copy_remote_directory(sftp, src, dest)
                else:
                    self._copy_remote_file(sftp, src, dest)

            future = manager._submit(_impl)
            self._attach_refresh(
                future,
                refresh_remote=self._right_pane,
                highlight_name=entry.name,
            )
            if move:
                self._schedule_remote_move_cleanup(future, source_path, self._right_pane)
            scheduled_any = True

        if scheduled_any:
            self._right_pane.show_toast(
                "Moving items…" if move else "Copying items…"
            )
        elif skipped:
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
    def _ensure_remote_directory(sftp: paramiko.SFTPClient, path: str) -> None:
        if not path:
            return
        components = []
        while path and path not in {"/", ""}:
            components.append(path)
            path = posixpath.dirname(path)
        for component in reversed(components):
            try:
                sftp.mkdir(component)
            except IOError:
                continue

    @staticmethod
    def _remote_path_exists(sftp: paramiko.SFTPClient, path: str) -> bool:
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
        self, sftp: paramiko.SFTPClient, source_path: str, destination_path: str
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
        self, sftp: paramiko.SFTPClient, source_path: str, destination_path: str
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

    def _show_progress_dialog(self, operation_type: str, filename: str, future: Future,
                               total_files: int = 1,
                               source_path: Optional[str] = None,
                               destination_path: Optional[str] = None) -> None:
        """Show and manage the progress dialog for a file operation."""
        try:
            logger.debug("_show_progress_dialog called for %s %s", operation_type, filename)

            # Check if we can reuse an existing dialog for the same operation type
            reuse_dialog = False
            if (hasattr(self, '_progress_dialog') and self._progress_dialog and
                self._progress_dialog.operation_type == operation_type and
                not self._progress_dialog.is_cancelled):
                reuse_dialog = True
                logger.debug("Reusing existing progress dialog for %s", operation_type)
            else:
                # Dismiss any existing progress dialog for different operation type
                if hasattr(self, '_progress_dialog') and self._progress_dialog:
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
                self._progress_dialog.set_operation_details(total_files=total_files, filename=filename)
                # AlertDialog.present takes a parent widget; MessageDialog
                # already had it set via set_transient_for in the dialog ctor.
                if _HAS_ALERT_DIALOG:
                    self._progress_dialog.present(dialog_parent)
                else:
                    self._progress_dialog.present()
                logger.debug("Progress dialog created and shown successfully")
            
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
    "AsyncSFTPManager",
    "FileEntry",
    "FileManagerWindow",
    "SFTPProgressDialog",
    "launch_file_manager_window",
]

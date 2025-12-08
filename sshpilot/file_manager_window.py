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

import logging


logger = logging.getLogger(__name__)


def _sftp_path_exists(sftp: paramiko.SFTPClient, path: str) -> bool:
    """Return ``True`` if *path* exists on the remote SFTP server."""

    try:
        sftp.stat(path)
    except FileNotFoundError:
        return False
    except IOError as exc:
        error_code = getattr(exc, "errno", None)
        if error_code is None and exc.args:
            first_arg = exc.args[0]
            if isinstance(first_arg, int):
                error_code = first_arg
        if error_code in {errno.ENOENT, errno.EINVAL}:
            return False
        raise
    return True


def _get_docs_json_path():
    """Get the path to the granted folders config file."""
    from .platform_utils import get_config_dir
    try:
        base_dir = get_config_dir()
    except TypeError:
        # Some tests monkeypatch GLib with lightweight stubs that do not
        # implement get_user_config_dir(). Fall back to a sensible default.
        base_dir = os.path.join(os.path.expanduser("~"), ".config", "sshpilot")
    return os.path.join(base_dir, "granted-folders.json")


DOCS_JSON = _get_docs_json_path()


def _ensure_cfg_dir():
    """Ensure the config directory exists."""
    cfg_dir = os.path.dirname(DOCS_JSON)
    os.makedirs(cfg_dir, exist_ok=True)


def _save_doc(folder_path: str, doc_id: str):
    """Save document ID, display name, and actual path to JSON config."""
    _ensure_cfg_dir()
    data = {}
    if os.path.exists(DOCS_JSON):
        try:
            with open(DOCS_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    data[doc_id] = {
        "display": Gio.File.new_for_path(folder_path).get_parse_name(),
        "path": folder_path  # Store the actual path for non-Flatpak lookup
    }
    with open(DOCS_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _grant_persistent_access(gfile):
    """Grant persistent access to a file via the Document portal (Flatpak only)."""
    if not is_flatpak():
        # In non-Flatpak environments, generate a simple ID from the path
        path = gfile.get_path()
        import hashlib
        doc_id = hashlib.md5(path.encode()).hexdigest()[:16]
        logger.debug(f"Generated simple doc ID for non-Flatpak: {doc_id}")
        return doc_id

    path = gfile.get_path()
    if not path:
        logger.warning("Cannot grant persistent access without a path")
        return None

    try:
        # Get the Document portal (only in Flatpak)
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        proxy = Gio.DBusProxy.new_sync(
            bus,
            Gio.DBusProxyFlags.NONE,
            None,
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/documents",
            "org.freedesktop.portal.Documents",
            None
        )

        fd_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY") and os.path.isdir(path):
            fd_flags |= os.O_DIRECTORY

        fd = os.open(path, fd_flags)
        try:
            fd_list = Gio.UnixFDList.new()
            fd_index = fd_list.append(fd)

            is_directory = os.path.isdir(path)
            flags = 1 | 2  # reuse_existing | persistent
            if is_directory:
                flags |= 8  # export-directory

            app_id = os.environ.get("FLATPAK_ID", "")
            basename = gfile.get_basename() or os.path.basename(path)

            permissions: List[str] = ["read"]
            if os.access(path, os.W_OK):
                permissions.append("write")

            # AddFull method signature according to the docs: (ah, u, s, as)
            parameters = GLib.Variant(
                "(ahusas)",
                ([fd_index], flags, app_id, permissions)
            )

            result = proxy.call_with_unix_fd_list_sync(
                "AddFull",
                parameters,
                Gio.DBusCallFlags.NONE,
                -1,
                fd_list,
                None
            )
        finally:
            os.close(fd)

        if result:
            doc_ids = result.get_child_value(0).unpack()
            doc_id = doc_ids[0] if doc_ids else None
            if doc_id:
                logger.info(
                    "Granted persistent access via Document portal, doc_id: %s", doc_id
                )
                return doc_id

    except Exception as e:
        logger.warning(f"Failed to grant persistent access via Document portal: {e}")

    # Fallback to simple ID generation
    path = gfile.get_path()
    if path:
        import hashlib
        doc_id = hashlib.md5(path.encode()).hexdigest()[:16]
        logger.debug(f"Using fallback doc ID: {doc_id}")
        return doc_id
    return None


def _lookup_document_path(doc_id: str):
    """Look up the current path for a document ID."""
    # Always try config lookup first since Document portal seems unreliable
    config_path = _lookup_path_from_config(doc_id)
    if config_path and os.path.exists(config_path):
        logger.debug(f"Found valid path from config for {doc_id}: {config_path}")
        return config_path
    
    # Only try Document portal in Flatpak and if config lookup failed
    if not is_flatpak():
        return None
    
    try:
        # Get the Document portal (only in Flatpak)
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        proxy = Gio.DBusProxy.new_sync(
            bus,
            Gio.DBusProxyFlags.NONE,
            None,
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/documents",
            "org.freedesktop.portal.Documents",
            None
        )
        
        # Call Lookup to get the current path
        # Lookup(IN s doc_id, OUT ay path, OUT a{s(say)} out_info)
        result = proxy.call_sync(
            "Lookup",
            GLib.Variant("(s)", (doc_id,)),
            Gio.DBusCallFlags.NONE,
            -1,
            None
        )
        
        if result:
            path_bytes = result.get_child_value(0).get_bytestring()
            path = path_bytes.decode('utf-8')
            logger.debug(f"Document portal lookup for {doc_id}: {path}")
            return path
        
    except Exception as e:
        logger.debug(f"Document portal lookup failed for {doc_id}: {e}")
    
    return None


def _lookup_path_from_config(doc_id: str):
    """Look up the original path from our config."""
    try:
        entry = _lookup_doc_entry(doc_id)
        if entry:

            # First try the 'path' field (new format)
            if 'path' in entry:
                path = entry['path']
                if os.path.exists(path):
                    return path
            
            # Fallback to 'display' field (old format)
            display = entry.get('display', '')
            if display:
                # If it's a portal path, try it directly
                if '/doc/' in display:
                    if os.path.exists(display):
                        return display
                # If it starts with ~, expand it
                elif display.startswith('~'):
                    expanded = os.path.expanduser(display)
                    if os.path.exists(expanded):
                        return expanded
                # Try as-is
                elif os.path.exists(display):
                    return display
            
            # Last resort: try to construct portal path from doc_id
            if is_flatpak():
                portal_path = f"/run/user/{os.getuid()}/doc/{doc_id}"
                if os.path.isdir(portal_path):
                    return portal_path

    except Exception as e:
        logger.debug(f"Failed to lookup path from config: {e}")
    return None


def _portal_doc_path(doc_id: str) -> str:
    """Get the portal mount path for a document ID."""
    return f"/run/user/{os.getuid()}/doc/{doc_id}"


def _load_doc_config() -> Dict[str, Dict[str, str]]:
    """Load the granted folders configuration file."""

    if not os.path.exists(DOCS_JSON):
        return {}

    try:
        with open(DOCS_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                # Ensure we only keep dictionary entries
                return {
                    key: value
                    for key, value in data.items()
                    if isinstance(value, dict)
                }
    except Exception as exc:  # pragma: no cover - config parsing errors are non-fatal
        logger.debug(f"Failed to load granted folders config: {exc}")

    return {}


def _lookup_doc_entry(doc_id: str) -> Optional[Dict[str, str]]:
    """Return the stored configuration entry for the given document ID."""

    config = _load_doc_config()
    entry = config.get(doc_id)
    if isinstance(entry, dict):
        return entry
    return None


def _load_first_doc_path():
    """Load the first valid document portal path from saved config."""
    logger.debug(f"Looking for config file: {DOCS_JSON}")

    config = _load_doc_config()
    if not config:
        logger.debug("Config file does not exist or is empty")
        return None

    for doc_id, entry in config.items():
        logger.debug(f"Looking up document ID: {doc_id}")
        portal_path = _lookup_document_path(doc_id)
        if portal_path and os.path.isdir(portal_path):
            logger.debug(f"Found valid portal path: {portal_path}")
            return portal_path, doc_id, entry

        logger.debug(f"Document ID {doc_id} is no longer valid")

    logger.debug("No valid portal paths found")
    return None


def _pretty_path_for_display(path: str) -> str:
    """Convert a filesystem path to a human-friendly display string.

    Uses GFile's parse_name for human-readable presentation (often shows "~" etc.).
    For document portal paths, shows just the folder name instead of the full mount path.
    """
    try:
        gfile = Gio.File.new_for_path(path)
        parse_name = gfile.get_parse_name()
        
        # If it's a doc mount, show a human-friendly version
        if "/doc/" in path and parse_name.startswith("/run/"):
            # Extract the final directory name from the portal path
            basename = gfile.get_basename()
            if basename:
                # For home directory, show it as ~/username
                if basename == os.path.basename(os.path.expanduser("~")):
                    return f"~/{basename}"
                # For other directories, just show the basename
                return basename
            # Fall back to the full path if basename fails
            return parse_name
        return parse_name
    except Exception:
        # Fallback to original path if GFile operations fail
        return path




class TransferCancelledException(Exception):
    """Exception raised when a transfer is cancelled"""
    pass


# ---------------------------------------------------------------------------
# Utility data structures


class SFTPProgressDialog(Adw.MessageDialog):
    """Modern GNOME HIG-compliant SFTP file transfer progress dialog"""

    _LABEL_WIDTH_CHARS = 48

    def __init__(self, parent=None, operation_type="transfer"):
        # Set appropriate title based on operation
        title = "Downloading Files" if operation_type == "download" else "Uploading Files"

        super().__init__(
            title=title,
            body="Transferring files…",
            default_response="cancel"
        )
        
        # Dialog properties
        self.set_modal(True)
        if parent:
            self.set_transient_for(parent)
        
        # Transfer state
        self.is_cancelled = False
        self.current_file = ""
        self.transferred_bytes = 0
        self.total_bytes = 0
        self.files_completed = 0
        self.total_files = 0
        self.start_time = time.time()
        self.operation_type = operation_type
        self._current_future = None
        self._futures = []  # Track all futures for multi-file operations
        self._completion_shown = False  # Flag to prevent multiple completion dialogs
        self._file_progress = {}  # Track progress per file: {future_id: (progress, filename)}
        self._failed_files = []  # Track failed files: [(filename, error_message), ...]
        
        self._build_ui()
        
    def _build_ui(self):
        """Build the modern GNOME HIG-compliant UI"""
        
        # Add response buttons
        self.add_response("cancel", "Cancel")
        self.set_default_response("cancel")
        self.set_close_response("cancel")
        
        # Connect response signal
        self.connect("response", self._on_response)
        
        # Create progress content area
        progress_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=12,
            margin_bottom=12
        )
        
        def _configure_progress_label(label: Gtk.Label) -> None:
            label.set_ellipsize(Pango.EllipsizeMode.END)
            label.set_justify(Gtk.Justification.CENTER)
            label.set_width_chars(self._LABEL_WIDTH_CHARS)
            label.set_max_width_chars(self._LABEL_WIDTH_CHARS)

        # Current file label (primary info)
        self.file_label = Gtk.Label()
        self.file_label.set_text("—")
        _configure_progress_label(self.file_label)
        progress_box.append(self.file_label)

        # Status label for detailed progress messages
        self.status_label = Gtk.Label()
        self.status_label.set_text("Preparing transfer…")
        _configure_progress_label(self.status_label)
        progress_box.append(self.status_label)
        
        # Main progress bar
        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_show_text(True)
        self.progress_bar.set_text("0%")
        progress_box.append(self.progress_bar)
        
        # Transfer details
        details_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6
        )
        progress_box.append(details_box)
        
        # Speed and time info
        info_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        details_box.append(info_box)
        
        self.speed_label = Gtk.Label()
        self.speed_label.set_text("—")
        self.speed_label.set_halign(Gtk.Align.START)
        self.speed_label.add_css_class("caption")
        info_box.append(self.speed_label)
        
        # Spacer
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        info_box.append(spacer)
        
        self.time_label = Gtk.Label()
        self.time_label.set_text("—")
        self.time_label.set_halign(Gtk.Align.END)
        self.time_label.add_css_class("caption")
        info_box.append(self.time_label)
        
        # File counter
        self.counter_label = Gtk.Label()
        self.counter_label.set_text("0 of 0 files")
        self.counter_label.set_halign(Gtk.Align.CENTER)
        self.counter_label.add_css_class("caption")
        details_box.append(self.counter_label)
        
        # Set the progress content as extra child
        self.set_extra_child(progress_box)
    
    def set_operation_details(self, total_files, filename=None):
        """Set the operation details"""
        # Only update total_files if it's larger (for adding more files to existing dialog)
        if total_files > self.total_files:
            self.total_files = total_files
            self.counter_label.set_text(f"{self.files_completed} of {total_files} files")
        
        if filename:
            self.current_file = filename
            self.file_label.set_text(filename)
    
    def _on_response(self, dialog, response):
        """Handle dialog response"""
        if response == "cancel":
            self.is_cancelled = True
            # Cancel all tracked futures
            if self._current_future and hasattr(self._current_future, 'cancel'):
                self._current_future.cancel()
            for future in self._futures:
                if future and hasattr(future, 'cancel') and not future.done():
                    future.cancel()
            self.close()
        elif response == "done":
            self.close()
    
    def update_progress(self, fraction, message=None, current_file=None):
        """Update progress bar and status"""
        GLib.idle_add(self._update_progress_ui, fraction, message, current_file)
    
    def _update_progress_ui(self, fraction, message, current_file):
        """Update UI elements (must be called from main thread)"""
        
        # For multi-file operations, calculate overall progress
        if self.total_files > 1:
            # Calculate overall progress: (completed_files + current_file_progress) / total_files
            # We use the fraction passed in as the current file's progress
            overall_progress = (self.files_completed + fraction) / self.total_files
            
            # Ensure we never show 100% until all files are completed
            # Maximum progress = (total_files - 1) / total_files when at least one file is still active
            if self.files_completed < self.total_files:
                max_progress = (self.total_files - 1) / self.total_files
                overall_progress = min(overall_progress, max_progress)
            
            percentage = int(overall_progress * 100)
            self.progress_bar.set_fraction(overall_progress)
            self.progress_bar.set_text(f"{percentage}%")
        else:
            # Single file - use progress directly
            percentage = int(fraction * 100)
            self.progress_bar.set_fraction(fraction)
            self.progress_bar.set_text(f"{percentage}%")
        
        # Update status label with status message
        if message:
            self.status_label.set_text(message)
        
        # Update current file
        if current_file:
            self.current_file = current_file
            self.file_label.set_text(current_file)
        
        # Calculate and update speed/time estimates
        elapsed = time.time() - self.start_time
        if elapsed > 1.0 and fraction > 0:  # Wait at least 1 second for meaningful estimates
            # Calculate transferred bytes and speed
            if self.total_bytes > 0:
                transferred_bytes = int(self.total_bytes * fraction)
                bytes_per_second = transferred_bytes / elapsed
                
                # Update speed display
                if bytes_per_second > 1024 * 1024:  # MB/s
                    speed_text = f"{bytes_per_second / (1024 * 1024):.1f} MB/s"
                elif bytes_per_second > 1024:  # KB/s
                    speed_text = f"{bytes_per_second / 1024:.1f} KB/s"
                else:
                    speed_text = f"{bytes_per_second:.0f} B/s"
                
                self.speed_label.set_text(speed_text)
                
                # Show size information
                transferred_size = self._format_size(transferred_bytes)
                total_size = self._format_size(self.total_bytes)
                size_info = f"{transferred_size} of {total_size}"
                
                # Update file label to show size info
                if current_file:
                    self.file_label.set_text(f"{current_file} ({size_info})")
            
            # Estimate total time and remaining time
            estimated_total_time = elapsed / fraction
            remaining_time = estimated_total_time - elapsed
            
            if remaining_time > 0:
                self.time_label.set_text(self._format_time(remaining_time))
            else:
                self.time_label.set_text("Almost done…")
        
        return False
    
    def increment_file_count(self):
        """Increment completed file counter"""
        GLib.idle_add(self._increment_file_count_ui)
    
    def _increment_file_count_ui(self):
        """Update file counter (must be called from main thread)"""
        self.files_completed += 1
        self.counter_label.set_text(f"{self.files_completed} of {self.total_files} files")
        return False
    
    def set_future(self, future):
        """Set the current operation future for cancellation"""
        self._current_future = future
        if future not in self._futures:
            self._futures.append(future)
    
    def set_total_bytes(self, total_bytes):
        """Set the total bytes for the operation"""
        self.total_bytes = total_bytes
    
    def _format_time(self, seconds):
        """Format time remaining for display"""
        if seconds > 3600:
            hours = int(seconds // 3600)
            minutes = int((seconds % 3600) // 60)
            return f"{hours}h {minutes}m remaining"
        elif seconds > 60:
            minutes = int(seconds // 60)
            return f"{minutes}m remaining"
        else:
            return f"{int(seconds)}s remaining"
    
    def _format_size(self, size_bytes):
        """Format file size for display"""
        if size_bytes >= 1024 * 1024 * 1024:  # GB
            return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"
        elif size_bytes >= 1024 * 1024:  # MB
            return f"{size_bytes / (1024 * 1024):.1f} MB"
        elif size_bytes >= 1024:  # KB
            return f"{size_bytes / 1024:.1f} KB"
        else:
            return f"{size_bytes} bytes"
    
    def show_completion(self, success=True, error_message=None):
        """Show completion state"""
        GLib.idle_add(self._show_completion_ui, success, error_message)
    
    def _show_completion_ui(self, success, error_message):
        """Update UI to show completion state (idempotent - safe to call multiple times)"""
        # Prevent multiple completion dialogs from being shown
        if self._completion_shown:
            return False
        self._completion_shown = True
        
        if success:
            self.set_title("Transfer Complete")
            self.status_label.set_text("Transfer completed successfully")
            self.file_label.set_text(f"Successfully transferred {self.files_completed} files")
            self.progress_bar.set_fraction(1.0)
            self.progress_bar.set_text("100%")
        else:
            self.set_title("Transfer Failed")
            self.status_label.set_text("Transfer failed")
            if error_message:
                self.file_label.set_text(f"Error: {error_message}")
            else:
                self.file_label.set_text("An error occurred during transfer")
        
        # Switch to Done button (handle errors gracefully)
        try:
            self.remove_response("cancel")
        except (AttributeError, RuntimeError, GLib.GError):
            # Cancel button may already be removed, ignore
            pass
        
        try:
            self.add_response("done", "Done")
            self.set_default_response("done")
            self.set_close_response("done")
        except (AttributeError, RuntimeError, GLib.GError):
            # Done button may already exist, ignore
            pass
        
        return False


@dataclasses.dataclass
class FileEntry:
    """Light weight description of a directory entry."""

    name: str
    is_dir: bool
    size: int
    modified: float
    item_count: Optional[int] = None  # Number of items in directory (for folders only)


class _MainThreadDispatcher:
    """Helper that marshals callbacks back to the GTK main loop."""

    @staticmethod
    def dispatch(func: Callable, *args, **kwargs) -> None:
        GLib.idle_add(lambda: func(*args, **kwargs))


# ---------------------------------------------------------------------------
# Asynchronous SFTP layer


class AsyncSFTPManager(GObject.GObject):
    """Small wrapper around :mod:`paramiko` that performs operations in
    worker threads.

    The class exposes a queue of operations and emits signals when important
    events happen.  Tests can monkeypatch :class:`paramiko.SSHClient` to avoid
    talking to a real server.
    """

    __gsignals__ = {
        "connected": (GObject.SignalFlags.RUN_FIRST, None, tuple()),
        "connection-error": (
            GObject.SignalFlags.RUN_FIRST,
            None,
            (str,),
        ),
        "authentication-required": (
            GObject.SignalFlags.RUN_FIRST,
            None,
            (str,),
        ),
        "progress": (GObject.SignalFlags.RUN_FIRST, None, (float, str)),
        "operation-error": (
            GObject.SignalFlags.RUN_FIRST,
            None,
            (str,),
        ),
        "directory-loaded": (
            GObject.SignalFlags.RUN_FIRST,
            None,
            (str, object),
        ),
    }

    def __init__(
        self,
        host: str,
        username: str,
        port: int = 22,
        password: Optional[str] = None,
        *,
        dispatcher: Callable[[Callable, tuple, dict], None] | None = None,
        connection: Any = None,
        connection_manager: Any = None,
        ssh_config: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__()
        self._host = host
        self._username = username
        self._password = password
        self._port = port or 22
        self._client: Optional[paramiko.SSHClient] = None
        self._sftp: Optional[paramiko.SFTPClient] = None
        # Use single worker to serialize SFTP operations - SFTP connections are not thread-safe
        # Operations will be queued and executed one at a time
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._dispatcher = dispatcher or (
            lambda cb, args=(), kwargs=None: _MainThreadDispatcher.dispatch(
                cb, *args, **(kwargs or {})
            )
        )
        self._lock = threading.Lock()
        self._cancelled_operations = set()  # Track cancelled operation IDs
        self._connection = connection
        self._connection_manager = connection_manager
        self._ssh_config = dict(ssh_config) if ssh_config else None
        self._proxy_sock: Optional[Any] = None
        self._jump_clients: List[paramiko.SSHClient] = []
        self._keepalive_interval: int = 0
        self._keepalive_count_max: int = 0
        self._keepalive_thread: Optional[threading.Thread] = None
        self._keepalive_stop_event: Optional[threading.Event] = None
        self._keepalive_failures: int = 0

    
    def _format_size(self, size_bytes):
        """Format file size for display"""
        if size_bytes >= 1024 * 1024 * 1024:  # GB
            return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"
        elif size_bytes >= 1024 * 1024:  # MB
            return f"{size_bytes / (1024 * 1024):.1f} MB"
        elif size_bytes >= 1024:  # KB
            return f"{size_bytes / 1024:.1f} KB"
        else:
            return f"{size_bytes} bytes"

    # -- connection -----------------------------------------------------

    def connect_to_server(self, password: Optional[str] = None) -> None:
        """Connect to server, optionally with a password for authentication."""
        if password is not None:
            self._password = password
        self._submit(
            self._connect_impl,
            on_success=lambda *_: self.emit("connected"),
            on_error=lambda exc: self.emit("connection-error", str(exc)),
        )

    def close(self) -> None:
        logger.info("AsyncSFTPManager.close() called")
        # Stop keepalive worker first
        self._stop_keepalive_worker()
        with self._lock:
            if self._sftp is not None:
                try:
                    self._sftp.close()
                except Exception as exc:
                    logger.debug(f"Error closing SFTP client: {exc}")
                finally:
                    self._sftp = None
            if self._client is not None:
                try:
                    self._client.close()
                except Exception as exc:
                    logger.debug(f"Error closing SSH client: {exc}")
                finally:
                    self._client = None
            if self._jump_clients:
                for jump_client in self._jump_clients:
                    try:
                        jump_client.close()
                    except Exception as exc:  # pragma: no cover - defensive cleanup
                        logger.debug("Error closing jump client: %s", exc)
                self._jump_clients.clear()

            if self._proxy_sock is not None:
                try:
                    self._proxy_sock.close()
                except Exception as exc:  # pragma: no cover - defensive cleanup
                    logger.debug("Error closing proxy socket: %s", exc)
                finally:
                    self._proxy_sock = None
        # Shutdown executor - use wait=False since connections are closed and will interrupt operations
        # This prevents hanging if there are stuck operations (like hanging listdir calls)
        logger.debug("Shutting down executor (connections closed, operations should be interrupted)")
        try:
            # Use wait=False to avoid hanging on stuck operations
            # The connections are already closed, so operations should fail quickly
            # Threads are daemon threads, so they'll be cleaned up when process exits
            self._executor.shutdown(wait=False)
            logger.debug("Executor shutdown initiated (non-blocking)")
        except Exception as exc:
            logger.debug(f"Error shutting down executor: {exc}")
        logger.info("AsyncSFTPManager.close() completed")

    def _start_keepalive_worker(self) -> None:
        with self._lock:
            if self._keepalive_interval <= 0 or self._sftp is None:
                return
            if self._keepalive_thread is not None and self._keepalive_thread.is_alive():
                return

            stop_event = threading.Event()
            interval = self._keepalive_interval
            self._keepalive_stop_event = stop_event
            self._keepalive_failures = 0

        def _worker() -> None:
            logger.debug("SFTP keepalive worker started for %s", self._host)
            try:
                while not stop_event.wait(interval):
                    with self._lock:
                        sftp = self._sftp
                        count_max = self._keepalive_count_max
                    if sftp is None:
                        logger.debug("Keepalive worker exiting because SFTP client is gone")
                        break

                    try:
                        sftp.stat(".")
                    except Exception as exc:  # pragma: no cover - defensive logging
                        with self._lock:
                            self._keepalive_failures += 1
                            failures = self._keepalive_failures
                            count_max = self._keepalive_count_max
                        logger.debug(
                            "SFTP keepalive attempt failed (%s/%s): %s",
                            failures,
                            count_max,
                            exc,
                        )
                        if count_max >= 0 and failures > count_max:
                            message = (
                                "SFTP keepalive failed too many times; connection may be down"
                            )
                            logger.warning(message)
                            self._dispatcher(
                                self.emit,
                                ("operation-error", message),
                                {},
                            )
                            break
                    else:
                        with self._lock:
                            self._keepalive_failures = 0
            finally:
                logger.debug("SFTP keepalive worker exiting for %s", self._host)
                with self._lock:
                    if self._keepalive_thread is threading.current_thread():
                        self._keepalive_thread = None
                        self._keepalive_stop_event = None
                        self._keepalive_failures = 0

        thread = threading.Thread(
            target=_worker,
            name=f"SFTPKeepalive-{self._host}",
            daemon=True,
        )
        with self._lock:
            self._keepalive_thread = thread
        thread.start()

    def _stop_keepalive_worker(self) -> None:
        thread: Optional[threading.Thread]
        event: Optional[threading.Event]
        with self._lock:
            thread = self._keepalive_thread
            event = self._keepalive_stop_event
        if event is not None:
            event.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        with self._lock:
            if thread is None or not thread.is_alive():
                self._keepalive_thread = None
                self._keepalive_stop_event = None
                self._keepalive_failures = 0

    # -- helpers --------------------------------------------------------

    def _submit(
        self,
        func: Callable[[], object],
        *,
        on_success: Optional[Callable[[object], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> Future:
        future = self._executor.submit(func)

        def _done(fut: Future) -> None:
            try:
                result = fut.result()
            except Exception as exc:  # pragma: no cover - errors handled uniformly
                if on_error:
                    self._dispatcher(on_error, (exc,), {})
                else:
                    self._dispatcher(self.emit, ("operation-error", str(exc)), {})
            else:
                if on_success:
                    self._dispatcher(on_success, (result,), {})

        future.add_done_callback(_done)
        return future

    # -- actual work ----------------------------------------------------

    @staticmethod
    def _select_host_key_policy(strict_host: str, auto_add: bool) -> paramiko.MissingHostKeyPolicy:
        """Return an appropriate Paramiko host key policy based on settings."""

        normalized = (strict_host or "").strip().lower()
        try:
            if normalized in {"yes", "always"}:
                return paramiko.RejectPolicy()
            if normalized in {"no", "off", "accept-new", "accept_new"}:
                return paramiko.AutoAddPolicy()
            if normalized in {"ask", "accept-new-once", "ask-new"}:
                return paramiko.WarningPolicy()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Failed to create host key policy for '%s': %s", normalized, exc)

        return paramiko.AutoAddPolicy() if auto_add else paramiko.RejectPolicy()

    @staticmethod
    def _parse_proxy_jump_entry(entry: str) -> Tuple[str, Optional[str], Optional[int]]:
        """Parse a ``ProxyJump`` token into host, optional user, and port."""

        token = entry.strip()
        if not token:
            return entry, None, None

        username: Optional[str] = None
        host_segment = token
        if "@" in token:
            username, host_segment = token.split("@", 1)

        port: Optional[int] = None
        hostname = host_segment

        if host_segment.startswith("[") and "]" in host_segment:
            bracket_end = host_segment.index("]")
            hostname = host_segment[1:bracket_end]
            remainder = host_segment[bracket_end + 1 :]
            if remainder.startswith(":"):
                try:
                    port = int(remainder[1:])
                except ValueError:
                    port = None
        elif ":" in host_segment:
            host_part, port_str = host_segment.rsplit(":", 1)
            hostname = host_part or host_segment
            try:
                port = int(port_str)
            except ValueError:
                port = None

        return hostname or entry, username, port

    def _create_proxy_jump_socket(
        self,
        jump_entries: List[str],
        *,
        config_override: Optional[str],
        policy: paramiko.MissingHostKeyPolicy,
        known_hosts_path: Optional[str],
        allow_agent: bool,
        look_for_keys: bool,
        key_filename: Optional[str],
        passphrase: Optional[str],
        resolved_host: str,
        resolved_port: int,
        base_username: str,
        connect_timeout: Optional[int] = None,
    ) -> Tuple[Any, List[paramiko.SSHClient]]:
        """Create a socket by chaining SSH connections through jump hosts."""

        from .ssh_config_utils import get_effective_ssh_config

        def _coerce_port(value: Any, default: int) -> int:
            try:
                return int(str(value))
            except (TypeError, ValueError):
                return default

        resolved_hops: List[Dict[str, Any]] = []
        for raw_entry in jump_entries:
            host_token, explicit_user, explicit_port = self._parse_proxy_jump_entry(raw_entry)
            try:
                hop_cfg = get_effective_ssh_config(host_token, config_file=config_override)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug(
                    "Failed to resolve effective SSH config for ProxyJump host %s: %s",
                    host_token,
                    exc,
                )
                hop_cfg = {}

            hostname = str(hop_cfg.get("hostname", host_token) or host_token)
            username = str(explicit_user or hop_cfg.get("user", base_username) or base_username)
            port = explicit_port
            if port is None:
                port = _coerce_port(hop_cfg.get("port", 22), 22)
            resolved_hops.append(
                {
                    "raw": raw_entry,
                    "alias": host_token,
                    "hostname": hostname,
                    "username": username,
                    "port": port,
                    "config": hop_cfg,
                }
            )

        jump_clients: List[paramiko.SSHClient] = []
        upstream_sock: Optional[Any] = None

        for index, hop in enumerate(resolved_hops):
            jump_client = paramiko.SSHClient()
            try:
                jump_client.load_system_host_keys()
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("Unable to load system host keys for jump host: %s", exc)
            jump_client.set_missing_host_key_policy(policy)

            if known_hosts_path:
                try:
                    if os.path.exists(known_hosts_path):
                        jump_client.load_host_keys(known_hosts_path)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug(
                        "Failed to load known hosts for ProxyJump host %s: %s",
                        hop["alias"],
                        exc,
                    )

            hop_kwargs: Dict[str, Any] = {
                "hostname": hop["hostname"],
                "username": hop["username"],
                "port": hop["port"],
                "allow_agent": allow_agent,
                "look_for_keys": look_for_keys,
            }

            if connect_timeout is not None:
                hop_kwargs["timeout"] = connect_timeout

            if upstream_sock is not None:
                hop_kwargs["sock"] = upstream_sock

            if key_filename:
                hop_kwargs["key_filename"] = key_filename
            if passphrase:
                hop_kwargs["passphrase"] = passphrase

            hop_password: Optional[str] = None
            if self._connection_manager is not None and hasattr(
                self._connection_manager, "get_password"
            ):
                try:
                    hop_password = self._connection_manager.get_password(
                        hop["alias"], hop["username"]
                    )
                    if not hop_password:
                        hop_password = self._connection_manager.get_password(
                            hop["hostname"], hop["username"]
                        )
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug(
                        "Password lookup for ProxyJump host %s failed: %s",
                        hop["alias"],
                        exc,
                    )

            if hop_password:
                hop_kwargs["password"] = hop_password

            jump_client.connect(**hop_kwargs)
            jump_clients.append(jump_client)

            transport = jump_client.get_transport()
            if transport is None:
                raise RuntimeError(
                    f"ProxyJump host {hop['alias']} did not provide a transport"
                )

            if index + 1 < len(resolved_hops):
                next_hop = resolved_hops[index + 1]
                dest = (next_hop["hostname"], next_hop["port"])
            else:
                dest = (resolved_host, resolved_port)

            upstream_sock = transport.open_channel(
                "direct-tcpip",
                dest,
                ("127.0.0.1", 0),
            )

            logger.debug(
                "ProxyJump hop %s connected to %s:%s, chaining towards %s:%s",
                hop["alias"],
                hop["hostname"],
                hop["port"],
                dest[0],
                dest[1],
            )

        if upstream_sock is None:
            raise RuntimeError("ProxyJump chain failed to produce a socket")

        return upstream_sock, jump_clients

    def _connect_impl(self) -> None:
        self._stop_keepalive_worker()
        client = paramiko.SSHClient()

        try:
            client.load_system_host_keys()
        except Exception as exc:
            logger.debug("Unable to load system host keys: %s", exc)

        ssh_cfg: Dict[str, Any] = {}
        file_manager_cfg: Dict[str, Any] = {}
        cfg = None
        try:
            from .config import Config  # Lazy import to avoid circular dependency

            cfg = Config()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Failed to initialise configuration for file manager: %s", exc)

        if self._ssh_config is not None:
            ssh_cfg = dict(self._ssh_config)
        elif cfg is not None:
            try:
                ssh_cfg = cfg.get_ssh_config() or {}
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("Failed to load SSH configuration for file manager: %s", exc)
                ssh_cfg = {}
        else:
            ssh_cfg = {}

        if cfg is not None and hasattr(cfg, 'get_file_manager_config'):
            try:
                file_manager_cfg = cfg.get_file_manager_config() or {}
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("Failed to load file manager configuration: %s", exc)
                file_manager_cfg = {}
        elif isinstance(self._ssh_config, dict):
            potential_cfg = self._ssh_config.get('file_manager')  # type: ignore[attr-defined]
            if isinstance(potential_cfg, dict):
                file_manager_cfg = dict(potential_cfg)

        def _coerce_int(value: Any, default: int) -> int:
            try:
                coerced = int(str(value))
                return coerced if coerced > 0 else default
            except (TypeError, ValueError):
                return default

        keepalive_interval = max(0, _coerce_int(ssh_cfg.get("keepalive_interval"), 0))
        keepalive_count_max = max(0, _coerce_int(ssh_cfg.get("keepalive_count_max"), 0))

        if isinstance(file_manager_cfg, dict):
            fm_interval = file_manager_cfg.get("sftp_keepalive_interval")
            if isinstance(fm_interval, int) and fm_interval >= 0:
                keepalive_interval = fm_interval

            fm_count = file_manager_cfg.get("sftp_keepalive_count_max")
            if isinstance(fm_count, int) and fm_count >= 0:
                keepalive_count_max = fm_count

            fm_timeout = file_manager_cfg.get("sftp_connect_timeout")
        else:
            fm_timeout = None

        connect_timeout: Optional[int]
        if isinstance(fm_timeout, int) and fm_timeout > 0:
            connect_timeout = fm_timeout
        else:
            connect_timeout = None

        connection_timeout_override = max(0, _coerce_int(ssh_cfg.get("connection_timeout"), 0))
        if connect_timeout is None and connection_timeout_override > 0:
            connect_timeout = connection_timeout_override

        with self._lock:
            self._keepalive_interval = keepalive_interval
            self._keepalive_count_max = keepalive_count_max
            self._keepalive_failures = 0

        strict_host = str(ssh_cfg.get("strict_host_key_checking", "") or "").strip()
        auto_add = bool(ssh_cfg.get("auto_add_host_keys", True))
        policy = self._select_host_key_policy(strict_host, auto_add)
        client.set_missing_host_key_policy(policy)

        known_hosts_path = None
        if self._connection_manager is not None:
            known_hosts_path = getattr(self._connection_manager, "known_hosts_path", None)

        if known_hosts_path:
            try:
                if os.path.exists(known_hosts_path):
                    client.load_host_keys(known_hosts_path)
                else:
                    logger.debug("Known hosts file not found at %s", known_hosts_path)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("Failed to load known hosts from %s: %s", known_hosts_path, exc)

        password = self._password or None
        connection = self._connection
        if not password and connection is not None:
            password = getattr(connection, "password", None) or None

        if not password and self._connection_manager is not None:
            lookup_user = self._username
            if connection is not None:
                lookup_user = getattr(connection, "username", None) or self._username

            # Try multiple host identifiers to match storage logic
            # Storage uses: hostname -> host -> nickname
            # We should try all of them to ensure we find the password
            lookup_hosts = []
            if connection is not None:
                # Collect all possible host identifiers
                hostname = getattr(connection, "hostname", None)
                host = getattr(connection, "host", None)
                nickname = getattr(connection, "nickname", None)
                
                # Add in storage priority order: hostname -> host -> nickname
                if hostname:
                    lookup_hosts.append(hostname)
                if host and host not in lookup_hosts:
                    lookup_hosts.append(host)
                if nickname and nickname not in lookup_hosts:
                    lookup_hosts.append(nickname)
            
            # Fallback to self._host if no connection or no identifiers found
            if not lookup_hosts:
                lookup_hosts = [self._host]
            
            logger.debug(
                "File manager: Attempting password lookup for %s@%s (trying identifiers: %s)",
                lookup_user,
                self._host,
                lookup_hosts
            )
            
            # Try each identifier until we find a password
            for lookup_host in lookup_hosts:
                try:
                    retrieved = self._connection_manager.get_password(lookup_host, lookup_user)
                    if retrieved:
                        logger.debug(
                            "File manager: Password found for %s@%s using identifier '%s'",
                            lookup_user,
                            lookup_host,
                            lookup_host
                        )
                        password = retrieved
                        break
                    else:
                        logger.debug(
                            "File manager: No password found for %s@%s using identifier '%s'",
                            lookup_user,
                            lookup_host,
                            lookup_host
                        )
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug(
                        "Password lookup failed for %s@%s (identifier '%s'): %s",
                        lookup_user,
                        lookup_host,
                        lookup_host,
                        exc
                    )

        allow_agent = True
        look_for_keys = True
        key_filename: Optional[str] = None
        passphrase: Optional[str] = None
        auth_method = 0
        key_mode = 0

        logger.debug("File manager: connection object is %s", "None" if connection is None else "present")
        if connection is not None:
            try:
                auth_method = int(getattr(connection, "auth_method", 0) or 0)
            except Exception:
                auth_method = 0

            try:
                key_mode = int(getattr(connection, "key_select_mode", 0) or 0)
            except Exception:
                key_mode = 0

            raw_keyfile = getattr(connection, "keyfile", "") or ""
            keyfile = raw_keyfile.strip()
            if keyfile.lower().startswith("select key file"):
                keyfile = ""
            
            logger.debug("File manager: connection nickname='%s', hostname='%s', key_mode=%d, keyfile='%s', auth_method=%d", 
                        getattr(connection, 'nickname', 'None'), 
                        getattr(connection, 'hostname', 'None'), 
                        key_mode, keyfile, auth_method)
        else:
            logger.debug("File manager: No connection object provided")

        identity_agent_disabled = False
        if connection is not None:
            identity_agent_disabled = bool(
                getattr(connection, "identity_agent_disabled", False)
            )

        if (
            connection is not None
            and key_mode in (1, 2)
            and keyfile
            and os.path.isfile(keyfile)
        ):
                key_filename = keyfile
                look_for_keys = False
                logger.debug("File manager: Using specific key file: %s", keyfile)
                # Prepare key for connection (add to ssh-agent if needed)
                key_prepared = False
                if identity_agent_disabled:
                    logger.debug(
                        "File manager: IdentityAgent disabled; skipping key preparation"
                    )
                elif not getattr(connection, "add_keys_to_agent_enabled", False):
                    logger.debug(
                        "File manager: AddKeysToAgent not enabled; skipping key preparation"
                    )
                elif (
                    self._connection_manager is not None
                    and hasattr(self._connection_manager, "prepare_key_for_connection")
                ):
                    try:
                        key_prepared = self._connection_manager.prepare_key_for_connection(keyfile)
                        if key_prepared:
                            logger.debug(
                                "Successfully prepared key for file manager: %s", keyfile
                            )
                        else:
                            logger.warning(
                                "Failed to prepare key for file manager: %s", keyfile
                            )
                    except Exception as exc:  # pragma: no cover - defensive
                        logger.warning("Error preparing key for file manager %s: %s", keyfile, exc)
                        key_prepared = False
                
                # If key preparation failed, we still try to connect but may prompt for passphrase
                if not key_prepared:
                    logger.info("Key preparation failed for %s, connection may prompt for passphrase", keyfile)

                passphrase = getattr(connection, "key_passphrase", None) or None
                if (
                    not passphrase
                    and self._connection_manager is not None
                    and hasattr(self._connection_manager, "get_key_passphrase")
                ):
                    try:
                        passphrase = self._connection_manager.get_key_passphrase(keyfile)
                    except Exception as exc:  # pragma: no cover - defensive
                        logger.debug("Failed to load key passphrase for %s: %s", keyfile, exc)

                # Only disable agent if explicitly configured to do so
                if getattr(connection, "pubkey_auth_no", False):
                    allow_agent = False
                    look_for_keys = False
                elif key_prepared:
                    # If we successfully prepared a key, ensure agent is enabled
                    allow_agent = True
                    look_for_keys = True
                    logger.debug("Key was prepared successfully, enabling SSH agent usage")

                # Only disable agent for password auth method
                if auth_method == 1:
                    allow_agent = False
                    look_for_keys = False

        if connection is not None:
            if auth_method == 1:
                allow_agent = False
                look_for_keys = False
            if getattr(connection, "pubkey_auth_no", False):
                allow_agent = False
                look_for_keys = False

        effective_cfg: Dict[str, Any] = {}
        proxy_command: str = ""
        proxy_jump: List[str] = []

        target_alias: Optional[str] = None
        config_override: Optional[str] = None

        if connection is not None:
            try:
                proxy_command = str(getattr(connection, "proxy_command", "") or "")
            except Exception:
                proxy_command = ""
            try:
                raw_jump = getattr(connection, "proxy_jump", []) or []
            except Exception:
                raw_jump = []
            if isinstance(raw_jump, str):
                proxy_jump = [token.strip() for token in raw_jump.split(",") if token.strip()]
            elif isinstance(raw_jump, (list, tuple, set)):
                proxy_jump = [str(token).strip() for token in raw_jump if str(token).strip()]

            source_path = str(getattr(connection, "source", "") or "")
            if source_path:
                expanded_source = os.path.abspath(
                    os.path.expanduser(os.path.expandvars(source_path))
                )
                if os.path.exists(expanded_source):
                    config_override = expanded_source

            if not config_override and getattr(connection, "isolated_config", False):
                root_candidate = str(getattr(connection, "config_root", "") or "")
                if root_candidate:
                    expanded_root = os.path.abspath(
                        os.path.expanduser(os.path.expandvars(root_candidate))
                    )
                    if os.path.exists(expanded_root):
                        config_override = expanded_root

            target_alias = (
                getattr(connection, "nickname", "")
                or getattr(connection, "hostname", "")
                or getattr(connection, "host", "")
                or None
            )

        if not target_alias:
            target_alias = self._host

        alias_for_config: Optional[str] = None

        if target_alias:
            try:
                from .ssh_config_utils import get_effective_ssh_config

                effective_cfg = get_effective_ssh_config(
                    target_alias, config_file=config_override
                )
                alias_for_config = target_alias

            except Exception as exc:  # pragma: no cover - defensive
                logger.debug(
                    "Failed to resolve effective SSH config for %s: %s",
                    target_alias,
                    exc,
                )
                effective_cfg = {}

        if not proxy_command:
            proxy_command = str(effective_cfg.get("proxycommand", "") or "")

        if not proxy_jump:
            raw_cfg_jump = effective_cfg.get("proxyjump", [])
            if isinstance(raw_cfg_jump, str):
                proxy_jump = [token.strip() for token in re.split(r"[\s,]+", raw_cfg_jump) if token.strip()]
            elif isinstance(raw_cfg_jump, (list, tuple, set)):
                proxy_jump = [
                    str(token).strip()
                    for token in raw_cfg_jump
                    if str(token).strip()
                ]

        alias_for_substitution = alias_for_config or target_alias or self._host

        def _coerce_port(value: Any, default: int) -> int:
            try:
                return int(str(value))
            except (TypeError, ValueError):
                return default

        resolved_host = str(effective_cfg.get("hostname", self._host) or self._host)
        resolved_port = _coerce_port(effective_cfg.get("port", self._port), self._port)
        resolved_username = str(effective_cfg.get("user", self._username) or self._username)


        def _expand_proxy_tokens(raw_command: str) -> str:
            if not raw_command:
                return raw_command

            substitution_host = str(resolved_host)
            substitution_port = str(resolved_port)
            substitution_user = str(resolved_username) if resolved_username else ""

            substitution_alias = str(alias_for_substitution) if alias_for_substitution else substitution_host

            token_pattern = re.compile(r"%(?:%|h|p|r|n)")

            def _replace(match: re.Match[str]) -> str:
                token = match.group(0)
                if token == "%%":
                    return "%"
                if token == "%h":
                    return substitution_host
                if token == "%p":
                    return substitution_port
                if token == "%r":
                    return substitution_user
                if token == "%n":
                    return substitution_alias
                return token

            return token_pattern.sub(_replace, raw_command)


        proxy_sock: Optional[Any] = None
        jump_clients: List[paramiko.SSHClient] = []
        proxy_command = proxy_command.strip()
        if proxy_command:
            try:
                from paramiko.proxy import ProxyCommand as ParamikoProxyCommand

                expanded_command = _expand_proxy_tokens(proxy_command)
                proxy_sock = ParamikoProxyCommand(expanded_command)
                logger.debug(
                    "File manager: using ProxyCommand '%s' (expanded from '%s')",
                    expanded_command,
                    proxy_command,
                )

            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Failed to set up ProxyCommand '%s': %s", proxy_command, exc)
                proxy_sock = None
        elif proxy_jump:
            try:
                proxy_sock, jump_clients = self._create_proxy_jump_socket(
                    proxy_jump,
                    config_override=config_override,
                    policy=policy,
                    known_hosts_path=known_hosts_path,
                    allow_agent=allow_agent,
                    look_for_keys=look_for_keys,
                    key_filename=key_filename,
                    passphrase=passphrase,
                    resolved_host=resolved_host,
                    resolved_port=resolved_port,
                    base_username=resolved_username,
                    connect_timeout=connect_timeout,
                )
                logger.debug(
                    "File manager: using Paramiko ProxyJump chain via %s",
                    ", ".join(proxy_jump),
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Failed to set up ProxyJump chain %s: %s", proxy_jump, exc)
                proxy_sock = None
                jump_clients = []
        else:
            jump_clients = []

        if not proxy_jump:
            jump_clients = []


        connect_kwargs: Dict[str, Any] = {
            "hostname": resolved_host,
            "username": resolved_username,
            "port": resolved_port,
            "allow_agent": allow_agent,
            "look_for_keys": look_for_keys,
        }

        if connect_timeout is not None:
            connect_kwargs["timeout"] = connect_timeout

        if password:
            connect_kwargs["password"] = password

        if key_filename:
            connect_kwargs["key_filename"] = key_filename

        if passphrase:
            connect_kwargs["passphrase"] = passphrase

        if proxy_sock is not None:
            connect_kwargs["sock"] = proxy_sock

        transport: Optional[Any] = None
        try:
            client.connect(**connect_kwargs)
            sftp = client.open_sftp()
            transport = client.get_transport()
            interval = 0
            with self._lock:
                interval = self._keepalive_interval
            if (
                transport is not None
                and hasattr(transport, "set_keepalive")
                and interval > 0
            ):
                try:
                    transport.set_keepalive(interval)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug("Failed to configure SSH keepalive: %s", exc)
        except paramiko.AuthenticationException as auth_exc:
            # Authentication failed - close connections and request password from UI
            if proxy_sock is not None:
                try:
                    proxy_sock.close()
                except Exception:  # pragma: no cover - defensive cleanup
                    pass
            if proxy_jump:
                for jump_client in jump_clients:
                    try:
                        jump_client.close()
                    except Exception:  # pragma: no cover - defensive cleanup
                        pass
            if client is not None:
                try:
                    client.close()
                except Exception:  # pragma: no cover - defensive cleanup
                    pass
            
            # Emit a special signal for authentication failure that will trigger password dialog
            logger.debug("Authentication failed, requesting password from user")
            self.emit("authentication-required", str(auth_exc))
            return  # Don't raise, let the UI handle it
        except Exception:
            if proxy_sock is not None:
                try:
                    proxy_sock.close()
                except Exception:  # pragma: no cover - defensive cleanup
                    pass
            if proxy_jump:
                for jump_client in jump_clients:
                    try:
                        jump_client.close()
                    except Exception:  # pragma: no cover - defensive cleanup
                        pass

            raise

        with self._lock:
            self._client = client
            self._sftp = sftp
            self._password = password
            self._proxy_sock = proxy_sock
            self._jump_clients = jump_clients

        self._start_keepalive_worker()


    # -- public operations ----------------------------------------------

    def listdir(self, path: str) -> None:
        logger.debug(f"AsyncSFTPManager.listdir called for path: {path}")
        def _impl() -> Tuple[str, List[FileEntry]]:
            entries: List[FileEntry] = []
            
            # Serialize all SFTP operations
            with self._lock:
                if self._sftp is None:
                    raise IOError("SFTP connection is not available")
                
                # Expand ~ to user's home directory
                expanded_path = path
                if path == "~" or path.startswith("~/"):
                    # Use the most reliable method to get home directory
                    # The SFTP normalize method with "." should give us the initial directory
                    # which is typically the user's home directory
                    try:
                        if path == "~":
                            # For just ~, resolve to the absolute home directory
                            expanded_path = self._sftp.normalize(".")
                        else:
                            # For ~/subpath, we need to resolve the home directory first
                            # Try to get the actual home directory path
                            home_path = self._sftp.normalize(".")
                            expanded_path = home_path + path[1:]  # Replace ~ with home_path
                    except Exception:
                        # If normalize fails, try common patterns
                        try:
                            possible_homes = [
                                f"/home/{self._username}",
                                f"/Users/{self._username}",  # macOS
                                f"/export/home/{self._username}",  # Solaris
                            ]
                            for possible_home in possible_homes:
                                try:
                                    # Test if this directory exists
                                    self._sftp.listdir_attr(possible_home)
                                    if path == "~":
                                        expanded_path = possible_home
                                    else:
                                        expanded_path = possible_home + path[1:]
                                    break
                                except Exception:
                                    continue
                            else:
                                # Final fallback
                                expanded_path = f"/home/{self._username}" + (path[1:] if path.startswith("~/") else "")
                        except Exception:
                            # Ultimate fallback
                            expanded_path = f"/home/{self._username}" + (path[1:] if path.startswith("~/") else "")
                
                for attr in self._sftp.listdir_attr(expanded_path):
                    is_dir = stat_isdir(attr)
                    item_count = None
                    
                    # Count items in directory
                    if is_dir:
                        try:
                            dir_path = os.path.join(expanded_path, attr.filename)
                            dir_attrs = self._sftp.listdir_attr(dir_path)
                            item_count = len(dir_attrs)
                        except Exception:
                            # If we can't read the directory, set count to None
                            item_count = None
                    
                    entries.append(
                        FileEntry(
                            name=attr.filename,
                            is_dir=is_dir,
                            size=attr.st_size,
                            modified=attr.st_mtime,
                            item_count=item_count,
                        )
                    )
            return expanded_path, entries

        self._submit(
            _impl,
            on_success=lambda result: (logger.debug(f"listdir success for {result[0]}, emitting directory-loaded with {len(result[1])} entries"), self.emit("directory-loaded", *result))[1],
            on_error=lambda exc: (logger.debug(f"listdir error: {exc}"), self.emit("operation-error", str(exc)))[1],
        )

    def mkdir(self, path: str) -> Future:
        logger.debug(f"Creating directory: {path}")
        return self._submit(
            lambda: self._sftp.mkdir(path),
            # Don't call listdir from callback - let the UI handle refresh
        )

    def path_exists(self, path: str) -> Future:
        """Return a future that resolves to whether *path* exists remotely."""

        def _impl() -> bool:
            # Serialize SFTP operations
            with self._lock:
                if self._sftp is None:
                    raise IOError("SFTP connection is not available")
                return _sftp_path_exists(self._sftp, path)

        return self._submit(_impl)

    def remove(self, path: str) -> Future:
        def _remove_recursive(target_path: str) -> None:
            """Recursively remove a file or directory synchronously."""
            logger.info(f"Attempting to remove: {target_path}")
            
            # Serialize SFTP operations - try to remove as file first
            try:
                with self._lock:
                    if self._sftp is None:
                        raise RuntimeError("SFTP connection is not available")
                    logger.info(f"Calling _sftp.remove('{target_path}')")
                    self._sftp.remove(target_path)
                logger.info(f"_sftp.remove() returned successfully for {target_path}")
                logger.info(f"Successfully removed file {target_path}")
                return
            except (IOError, OSError) as file_error:
                # If remove() fails, it's likely a directory - remove contents recursively
                logger.info(f"Path {target_path} is a directory (remove failed: {file_error}), removing recursively")
            except paramiko.ssh_exception.SSHException as ssh_error:
                # Handle paramiko-specific exceptions
                logger.error(f"SSH error calling remove() on {target_path}: {ssh_error}", exc_info=True)
                raise
            except Exception as unexpected_error:
                # Catch any other exceptions from remove()
                logger.error(f"Unexpected error calling remove() on {target_path}: {unexpected_error}", exc_info=True)
                raise
            
            # Handle directory removal
            try:
                logger.info(f"Calling listdir on {target_path}")
                
                # Serialize SFTP operations for directory operations
                entries = None
                with self._lock:
                    if self._sftp is None:
                        raise RuntimeError("SFTP connection is not available")
                    
                    # First check if directory still exists (might have been deleted by parallel operation)
                    try:
                        stat_result = self._sftp.stat(target_path)
                        if not stat.S_ISDIR(stat_result.st_mode):
                            logger.info(f"Path {target_path} is not a directory, skipping recursive removal")
                            return
                    except (IOError, OSError) as stat_error:
                        error_code = getattr(stat_error, "errno", None)
                        if error_code in {errno.ENOENT, errno.EINVAL}:
                            logger.info(f"Directory {target_path} no longer exists (deleted by parallel operation?), skipping")
                            return
                        # If stat fails for other reasons, continue with listdir attempt
                        logger.debug(f"stat() failed for {target_path}: {stat_error}, continuing with listdir")
                    
                    # Try to get directory contents
                    try:
                        logger.info(f"About to call _sftp.listdir('{target_path}')")
                        entries = self._sftp.listdir(target_path)
                        logger.info(f"listdir call completed, checking result...")
                        if entries is not None:
                            logger.debug(f"listdir returned {len(entries)} entries for {target_path}")
                            if entries:
                                logger.debug(f"Entries to delete: {entries[:5]}{'...' if len(entries) > 5 else ''}")
                            else:
                                logger.debug(f"Directory {target_path} is empty")
                        else:
                            logger.error(f"listdir returned None for {target_path}")
                    except IOError as listdir_io_error:
                        # Check if directory no longer exists (might have been deleted by parallel operation)
                        error_code = getattr(listdir_io_error, "errno", None)
                        if error_code in {errno.ENOENT, errno.EINVAL}:
                            logger.debug(f"Directory {target_path} no longer exists, skipping")
                            return
                        logger.error(f"listdir IOError for {target_path}: {listdir_io_error}", exc_info=True)
                        raise
                    except OSError as listdir_os_error:
                        error_code = getattr(listdir_os_error, "errno", None)
                        if error_code in {errno.ENOENT, errno.EINVAL}:
                            logger.debug(f"Directory {target_path} no longer exists, skipping")
                            return
                        logger.error(f"listdir OSError for {target_path}: {listdir_os_error}", exc_info=True)
                        raise
                    except Exception as listdir_error:
                        logger.error(f"listdir failed for {target_path}: {listdir_error}", exc_info=True)
                        raise
                
                if entries is None:
                    logger.error(f"listdir returned None for {target_path}")
                    raise RuntimeError(f"listdir returned None for {target_path}")
                
                logger.debug(f"Directory {target_path} contains {len(entries)} entries")
                for entry in entries:
                    entry_path = posixpath.join(target_path, entry)
                    logger.debug(f"Recursively removing entry: {entry_path}")
                    _remove_recursive(entry_path)  # Recursive call (lock released, will re-acquire)
                
                # After all children are removed, remove the directory itself
                logger.debug(f"Removing empty directory: {target_path}")
                with self._lock:
                    if self._sftp is None:
                        raise RuntimeError("SFTP connection is not available")
                    try:
                        self._sftp.rmdir(target_path)
                        logger.debug(f"Successfully removed directory {target_path}")
                    except IOError as rmdir_error:
                        # Check if directory was already deleted
                        error_code = getattr(rmdir_error, "errno", None)
                        if error_code in {errno.ENOENT, errno.EINVAL}:
                            logger.debug(f"Directory {target_path} already removed")
                            return
                        raise
            except (IOError, OSError) as dir_error:
                # If listdir or rmdir fails, log and re-raise
                logger.error(f"Failed to remove directory {target_path}: {dir_error}", exc_info=True)
                raise
            except Exception as e:
                # Catch any other unexpected errors from directory operations
                logger.error(f"Unexpected error removing directory {target_path}: {e}", exc_info=True)
                raise

        def _impl() -> None:
            try:
                logger.info(f"Starting remove operation for {path}")
                _remove_recursive(path)
                logger.info(f"Successfully completed remove operation for {path}")
            except Exception as e:
                logger.error(f"Error in remove operation for {path}: {e}", exc_info=True)
                # Re-raise so the future will have the exception
                raise

        parent = os.path.dirname(path) or "/"
        return self._submit(_impl)  # Don't call listdir from callback - let the UI handle refresh

    def rename(self, source: str, target: str) -> Future:
        logger.debug(f"Renaming {source} to {target}")
        return self._submit(
            lambda: self._sftp.rename(source, target),
            # Don't call listdir from callback - let the UI handle refresh
        )

    def download(self, source: str, destination: pathlib.Path) -> Future:
        try:
            # Ensure parent directory exists, with special handling for portal paths
            parent_dir = destination.parent
            logger.debug(f"Download: ensuring parent directory exists: {parent_dir}")
            
            if not parent_dir.exists():
                logger.debug(f"Download: creating parent directory: {parent_dir}")
                parent_dir.mkdir(parents=True, exist_ok=True)
            else:
                logger.debug(f"Download: parent directory already exists: {parent_dir}")
                
            # Verify we can write to the destination
            if not os.access(str(parent_dir), os.W_OK):
                logger.warning(f"Download: no write access to destination directory: {parent_dir}")
            else:
                logger.debug(f"Download: write access confirmed for: {parent_dir}")
                
        except Exception as e:
            logger.error(f"Download: failed to prepare destination directory {destination.parent}: {e}")
            # Continue anyway - maybe the directory already exists or will be created by the SFTP operation
        
        operation_id = f"download_{id(self)}_{time.time()}"

        def _impl() -> None:
            assert self._sftp is not None
            self.emit("progress", 0.0, "Starting download…")
            
            def progress_callback(transferred: int, total: int) -> None:
                # Check if this operation was cancelled
                if operation_id in self._cancelled_operations:
                    raise TransferCancelledException("Download was cancelled")
                    
                if total > 0:
                    progress = transferred / total
                    transferred_size = self._format_size(transferred)
                    total_size = self._format_size(total)
                    self.emit("progress", progress, f"Downloaded {transferred_size} of {total_size}")
                else:
                    transferred_size = self._format_size(transferred)
                    self.emit("progress", 0.0, f"Downloaded {transferred_size}")
            
            try:
                logger.debug(f"Download: starting SFTP get from {source} to {destination}")
                # Serialize SFTP operations - must hold lock during file transfer
                with self._lock:
                    if self._sftp is None:
                        raise IOError("SFTP connection closed before download could start")
                    self._sftp.get(source, str(destination), callback=progress_callback)
                
                # Only emit completion if not cancelled
                if operation_id not in self._cancelled_operations:
                    # Verify the file was actually created
                    if destination.exists():
                        file_size = destination.stat().st_size
                        logger.info(f"Download: successfully saved {source} to {destination} ({file_size} bytes)")
                    else:
                        logger.error(f"Download: file not found after transfer: {destination}")
                    self.emit("progress", 1.0, "Download complete")
            except TransferCancelledException:
                # Clean up partial download on cancellation
                try:
                    if destination.exists():
                        destination.unlink()
                        print(f"DEBUG: Cleaned up partial download: {destination}")
                except Exception:
                    pass
                self.emit("progress", 0.0, "Download cancelled")
                print(f"DEBUG: Download operation {operation_id} was cancelled")
            finally:
                # Clean up the cancellation flag
                self._cancelled_operations.discard(operation_id)

        future = self._submit(_impl)
        
        # Store the operation ID so we can cancel it
        original_cancel = future.cancel
        def cancel_with_cleanup():
            print(f"DEBUG: Cancelling download operation {operation_id}")
            self._cancelled_operations.add(operation_id)
            return original_cancel()
        future.cancel = cancel_with_cleanup
        
        return future

    def upload(self, source: pathlib.Path, destination: str) -> Future:
        operation_id = f"upload_{id(self)}_{time.time()}"
        
        def _impl() -> None:
            # Check connection before starting
            with self._lock:
                if self._sftp is None:
                    raise IOError("SFTP connection is not available. Connection may have been closed.")
            
            self.emit("progress", 0.0, "Starting upload…")
            
            def progress_callback(transferred: int, total: int) -> None:
                # Check if this operation was cancelled
                if operation_id in self._cancelled_operations:
                    raise TransferCancelledException("Upload was cancelled")
                    
                if total > 0:
                    progress = transferred / total
                    transferred_size = self._format_size(transferred)
                    total_size = self._format_size(total)
                    self.emit("progress", progress, f"Uploaded {transferred_size} of {total_size}")
                else:
                    transferred_size = self._format_size(transferred)
                    self.emit("progress", 0.0, f"Uploaded {transferred_size}")
            
            try:
                # Re-check connection before actual upload and serialize SFTP operations
                # SFTP connections are not thread-safe - must serialize file operations
                with self._lock:
                    if self._sftp is None:
                        raise IOError("SFTP connection closed before upload could start")
                    
                    # Perform the actual upload while holding the lock to serialize operations
                    self._sftp.put(str(source), destination, callback=progress_callback)
                
                # Only emit completion if not cancelled
                if operation_id not in self._cancelled_operations:
                    self.emit("progress", 1.0, "Upload complete")
            except TransferCancelledException:
                self.emit("progress", 0.0, "Upload cancelled")
                print(f"DEBUG: Upload operation {operation_id} was cancelled")
            except Exception as e:
                # Re-raise to ensure it's propagated to the future
                logger.error(f"Upload failed for {destination}: {e}", exc_info=True)
                raise
            finally:
                # Clean up the cancellation flag
                self._cancelled_operations.discard(operation_id)

        future = self._submit(_impl)
        
        # Store the operation ID so we can cancel it
        original_cancel = future.cancel
        def cancel_with_cleanup():
            print(f"DEBUG: Cancelling upload operation {operation_id}")
            self._cancelled_operations.add(operation_id)
            return original_cancel()
        future.cancel = cancel_with_cleanup
        
        return future

    # Helpers for directory recursion – these are intentionally simplistic
    # and rely on Paramiko's high level API.

    def download_directory(self, source: str, destination: pathlib.Path) -> Future:
        def _impl() -> None:
            assert self._sftp is not None
            self.emit("progress", 0.0, "Preparing download…")
            
            # First, collect all files to get total count
            all_files = []
            for root, dirs, files in walk_remote(self._sftp, source):
                rel_root = os.path.relpath(root, source)
                target_root = destination / rel_root
                target_root.mkdir(parents=True, exist_ok=True)
                for name in files:
                    all_files.append((os.path.join(root, name), str(target_root / name)))
            
            total_files = len(all_files)
            if total_files == 0:
                self.emit("progress", 1.0, "Directory downloaded (no files)")
                return
            
            # Download files with progress tracking
            for i, (remote_path, local_path) in enumerate(all_files):
                file_progress = i / total_files
                self.emit("progress", file_progress, f"Downloading {os.path.basename(remote_path)}...")
                
                def progress_callback(transferred: int, total: int) -> None:
                    if total > 0:
                        file_progress = transferred / total
                        overall_progress = (i + file_progress) / total_files
                        self.emit("progress", overall_progress, 
                                f"Downloading {os.path.basename(remote_path)} ({transferred:,}/{total:,} bytes)")
                
                # Serialize SFTP operations
                with self._lock:
                    if self._sftp is None:
                        raise IOError("SFTP connection closed during directory download")
                    self._sftp.get(remote_path, local_path, callback=progress_callback)
            
            self.emit("progress", 1.0, "Directory downloaded")

        return self._submit(_impl)

    def upload_directory(self, source: pathlib.Path, destination: str) -> Future:
        def _impl() -> None:
            assert self._sftp is not None
            self.emit("progress", 0.0, "Preparing upload…")
            
            # First, collect all files to get total count
            all_files = []
            for root, dirs, files in os.walk(source):
                rel_root = os.path.relpath(root, str(source))
                remote_root = (
                    destination if rel_root == "." else os.path.join(destination, rel_root)
                )
                try:
                    # Serialize SFTP operations
                    with self._lock:
                        if self._sftp is None:
                            raise IOError("SFTP connection closed during directory upload")
                        self._sftp.mkdir(remote_root)
                except IOError:
                    pass
                for name in files:
                    local_path = os.path.join(root, name)
                    remote_path = os.path.join(remote_root, name)
                    all_files.append((local_path, remote_path))
            
            total_files = len(all_files)
            if total_files == 0:
                self.emit("progress", 1.0, "Directory uploaded (no files)")
                return
            
            # Upload files with progress tracking
            for i, (local_path, remote_path) in enumerate(all_files):
                file_progress = i / total_files
                self.emit("progress", file_progress, f"Uploading {os.path.basename(local_path)}...")
                
                def progress_callback(transferred: int, total: int) -> None:
                    if total > 0:
                        file_progress = transferred / total
                        overall_progress = (i + file_progress) / total_files
                        self.emit("progress", overall_progress, 
                                f"Uploading {os.path.basename(local_path)} ({transferred:,}/{total:,} bytes)")
                
                # Serialize SFTP operations
                with self._lock:
                    if self._sftp is None:
                        raise IOError("SFTP connection closed during directory upload")
                    self._sftp.put(local_path, remote_path, callback=progress_callback)
            
            self.emit("progress", 1.0, "Directory uploaded")

        return self._submit(_impl)


def stat_isdir(attr: paramiko.SFTPAttributes) -> bool:
    """Return ``True`` when the attribute represents a directory."""

    return bool(attr.st_mode & 0o40000)


def walk_remote(sftp: paramiko.SFTPClient, root: str) -> Iterable[Tuple[str, List[str], List[str]]]:
    """Yield a remote directory tree similar to :func:`os.walk`."""

    dirs: List[str] = []
    files: List[str] = []
    for entry in sftp.listdir_attr(root):
        if stat_isdir(entry):
            dirs.append(entry.filename)
        else:
            files.append(entry.filename)
    yield root, dirs, files
    for directory in dirs:
        new_root = os.path.join(root, directory)
        yield from walk_remote(sftp, new_root)


# ---------------------------------------------------------------------------
# UI widgets


class PathEntry(Gtk.Entry):
    """Simple entry used for the editable pathbar."""

    def __init__(self) -> None:
        super().__init__()
        # Don't set hexpand here - we'll set it explicitly in the toolbar
        # self.set_hexpand(True)
        self.set_placeholder_text("/remote/path")
        # Remove minimum width constraint to allow full expansion
        # self.set_size_request(200, -1)  # Commented out to allow full width


class PaneControls(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.set_valign(Gtk.Align.CENTER)
        from sshpilot import icon_utils
        self.back_button = icon_utils.new_button_from_icon_name("go-previous-symbolic")
        self.up_button = icon_utils.new_button_from_icon_name("go-up-symbolic")
        self.refresh_button = icon_utils.new_button_from_icon_name("view-refresh-symbolic")
        self.new_folder_button = icon_utils.new_button_from_icon_name("folder-new-symbolic")
        for widget in (
            self.back_button,
            self.up_button,
            self.refresh_button,
            self.new_folder_button,
        ):
            widget.set_valign(Gtk.Align.CENTER)
        for widget in (self.back_button, self.up_button, self.refresh_button, self.new_folder_button):
            widget.add_css_class("flat")
        self.append(self.back_button)
        self.append(self.up_button)
        self.append(self.refresh_button)
        self.append(self.new_folder_button)


class PaneToolbar(Gtk.Box):
    __gsignals__ = {
        "view-changed": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "show-hidden-toggled": (GObject.SignalFlags.RUN_LAST, None, (bool,)),
    }

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)

        # Build a custom top bar: WindowHandle -> Box [ left | ENTRY (expands) | right ]
        handle = Gtk.WindowHandle()                    # gives draggable area like a headerbar
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        handle.set_child(bar)

        # Left side (compact)
        self._pane_label = Gtk.Label()
        self._pane_label.set_css_classes(["title"])
        self.controls = PaneControls()
        left = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        left.set_margin_start(12)  # Add margin before Remote/Local labels
        left.append(self._pane_label)
        left.append(self.controls)
        bar.append(left)

        # Entry (fills all remaining space)
        self.path_entry = PathEntry()
        self.path_entry.set_hexpand(True)
        self.path_entry.set_halign(Gtk.Align.FILL)
        self.path_entry.set_width_chars(0)
        self.path_entry.set_max_width_chars(0)
        bar.append(self.path_entry)

        # Right side (compact, flush-right)
        right = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._current_view = "list"

        # Toggle for showing hidden files within this pane
        self._show_hidden_button = Gtk.ToggleButton()
        self._show_hidden_button.set_valign(Gtk.Align.CENTER)
        self._show_hidden_button.add_css_class("flat")
        from sshpilot import icon_utils
        self._show_hidden_image = icon_utils.new_image_from_icon_name("view-conceal-symbolic")
        self._show_hidden_button.set_child(self._show_hidden_image)
        self._show_hidden_handler_id = self._show_hidden_button.connect(
            "toggled", self._on_show_hidden_toggled
        )
        self._update_show_hidden_icon(False)
        right.append(self._show_hidden_button)

        self.sort_split_button = self._create_sort_split_button()
        right.append(self.sort_split_button)
        bar.append(right)

        # Don't wrap in ToolbarView to avoid nested ToolbarView theme issues
        # Instead, add toolbar styling via CSS class and append handle directly
        handle.add_css_class('toolbar')
        self.append(handle)

    # Keep your factory
    def _create_sort_split_button(self) -> Adw.SplitButton:
        menu_model = Gio.Menu()
        sort_section = Gio.Menu()
        sort_section.append("Name", "pane.sort-by-name")
        sort_section.append("Size", "pane.sort-by-size")
        sort_section.append("Modified", "pane.sort-by-modified")
        menu_model.append_section("Sort by", sort_section)
        direction_section = Gio.Menu()
        direction_section.append("Ascending", "pane.sort-direction-asc")
        direction_section.append("Descending", "pane.sort-direction-desc")
        menu_model.append_section("Order", direction_section)
        split_button = Adw.SplitButton()
        split_button.set_menu_model(menu_model)
        split_button.set_tooltip_text("Toggle view mode")
        split_button.set_dropdown_tooltip("Sort files and folders")
        from sshpilot import icon_utils
        # Adw.SplitButton doesn't have set_icon(), use set_icon_name() with bundled icon
        # We'll need to use set_icon_name() but ensure our resource path is checked first
        # For now, use set_icon_name() - the icon theme should find our bundled icon
        split_button.set_icon_name("view-list-symbolic")
        split_button.connect("clicked", self._on_view_toggle_clicked)
        return split_button

    # Example handler
    def _on_view_toggle_clicked(self, *_):
        from sshpilot import icon_utils
        self._current_view = "grid" if self._current_view == "list" else "list"
        icon_name = "view-grid-symbolic" if self._current_view == "grid" else "view-list-symbolic"
        # Adw.SplitButton uses set_icon_name()
        self.sort_split_button.set_icon_name(icon_name)
        self.emit("view-changed", self._current_view)

    def _on_show_hidden_toggled(self, button: Gtk.ToggleButton) -> None:
        state = button.get_active()
        self._update_show_hidden_icon(state)
        self.emit("show-hidden-toggled", state)

    def set_show_hidden_state(self, show_hidden: bool) -> None:
        if not hasattr(self, "_show_hidden_button"):
            return
        current = self._show_hidden_button.get_active()
        if current == show_hidden:
            self._update_show_hidden_icon(show_hidden)
            return
        if self._show_hidden_handler_id is not None:
            self._show_hidden_button.handler_block(self._show_hidden_handler_id)
        try:
            self._show_hidden_button.set_active(show_hidden)
        finally:
            if self._show_hidden_handler_id is not None:
                self._show_hidden_button.handler_unblock(self._show_hidden_handler_id)
        self._update_show_hidden_icon(show_hidden)

    def _update_show_hidden_icon(self, show_hidden: bool) -> None:
        from sshpilot import icon_utils
        icon_name = "view-reveal-symbolic" if show_hidden else "view-conceal-symbolic"
        tooltip = "Hide Hidden Files" if show_hidden else "Show Hidden Files"
        icon_utils.set_icon_from_name(self._show_hidden_image, icon_name)
        self._show_hidden_button.set_tooltip_text(tooltip)

    def get_header_bar(self):
        """Get the actual header bar for toolbar view."""
        return None  # No longer using Adw.HeaderBar


# ---------- Helper functions for properties dialog ----------
def _human_size(n: int) -> str:
    """Convert bytes to human readable format."""
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or unit == "PB":
            return f"{n:.0f} {unit}" if n >= 10 or unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return "0 B"


def _human_time(ts: float) -> str:
    """Convert timestamp to human readable format."""
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "—"


def _mode_to_str(mode: int) -> str:
    """Convert file mode to string representation like -rw-r--r--."""
    is_dir = "d" if stat.S_ISDIR(mode) else "-"
    perm = ""
    for who, shift in (("USR", 6), ("GRP", 3), ("OTH", 0)):
        r = "r" if mode & (4 << shift) else "-"
        w = "w" if mode & (2 << shift) else "-"
        x = "x" if mode & (1 << shift) else "-"
        perm += r + w + x
    return is_dir + perm


def _mode_to_octal(mode: int) -> str:
    """Convert file mode to octal representation like 755."""
    # Extract only the permission bits (last 9 bits)
    perm_bits = mode & 0o777
    return oct(perm_bits)[2:]  # Remove '0o' prefix


class PropertiesDialog(Adw.Window):
    """Nautilus-style properties dialog using card-based design."""

    def __init__(self, entry: "FileEntry", current_path: str, parent: Gtk.Window, sftp_manager: Optional["AsyncSFTPManager"] = None):
        super().__init__()
        self._entry = entry
        self._current_path = current_path
        self._parent_window = parent
        self._sftp_manager = sftp_manager
        self.set_title("Properties")
        
        # Set window properties
        self.set_default_size(400, 500)
        self.set_resizable(True)
        self.set_modal(True)
        self.set_transient_for(parent)
        
        # Position window relative to parent
        if parent:
            try:
                # Get parent window position and size
                parent_alloc = parent.get_allocation()
                parent_width = parent_alloc.width
                parent_height = parent_alloc.height
                
                # Center the dialog on the parent window
                # For GTK4, we'll let the window manager handle positioning
                # The modal and transient_for properties should handle this
            except Exception:
                # Fallback: let window manager handle positioning
                pass

        # Build the dialog content
        self._build_dialog()

    def _build_dialog(self) -> None:
        """Build the Nautilus-style properties dialog content."""
        # Create AdwToolbarView as the main content (proper Adw.Window structure)
        toolbar_view = Adw.ToolbarView()
        
        # Create proper header bar for dragging
        header_bar = Adw.HeaderBar()
        header_bar.set_title_widget(Gtk.Label(label="Properties"))
        
        # Add header bar to toolbar view
        toolbar_view.add_top_bar(header_bar)
        
        # Main content box
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                         margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)
        
        # Header with icon and name
        content.append(self._create_header_block())
        
        # Parent folder row
        content.append(self._create_parent_folder_row())
        
        # Size row
        content.append(self._create_size_row())
        
        # Modified and Created rows
        content.append(self._create_modified_row())
        content.append(self._create_created_row())
        
        # Permissions row
        content.append(self._create_permissions_row())
        
        # Set content in toolbar view
        toolbar_view.set_content(content)
        
        # Set the toolbar view as the window content
        self.set_content(toolbar_view)


    def _create_header_block(self) -> Gtk.Widget:
        """Create the header block with icon, name, and summary."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, halign=Gtk.Align.CENTER)
        
        # Icon
        from sshpilot import icon_utils
        if self._entry.is_dir:
            icon = icon_utils.new_image_from_icon_name("folder-symbolic")
        else:
            icon = icon_utils.new_image_from_icon_name("text-x-generic-symbolic")
        # Set a larger custom size instead of using predefined sizes
        icon.set_pixel_size(64)
        icon.add_css_class("icon-dropshadow")
        icon.add_css_class("card")
        box.append(icon)
        
        # Name (centered, bold)
        name_label = Gtk.Label(label=self._entry.name)
        name_label.add_css_class("title-3")
        box.append(name_label)
        
        # Summary
        summary_parts = []
        if self._entry.is_dir:
            if self._entry.item_count is not None:
                summary_parts.append(f"{self._entry.item_count} item{'s' if self._entry.item_count != 1 else ''}")
            else:
                summary_parts.append("Folder")
        else:
            if self._entry.size:
                summary_parts.append(_human_size(self._entry.size))
        
        # Add free space for local files
        if not self._is_remote_file():
            try:
                path = os.path.join(self._current_path, self._entry.name)
                if os.path.exists(path):
                    stat = os.statvfs(path)
                    free = stat.f_bavail * stat.f_frsize
                    summary_parts.append(f"{_human_size(free)} Free")
            except Exception:
                pass
        
        summary_text = " — ".join(summary_parts) if summary_parts else ""
        summary_label = Gtk.Label(label=summary_text)
        summary_label.add_css_class("dim-label")
        box.append(summary_label)
        
        return box

    def _create_size_row(self) -> Gtk.Widget:
        """Create the size row."""
        if self._entry.is_dir:
            if self._entry.item_count is not None:
                size_text = f"{self._entry.item_count} item{'s' if self._entry.item_count != 1 else ''}"
                # For local folders, start calculating actual size
                if not self._is_remote_file():
                    size_text += " (calculating size...)"
                    self._start_folder_size_calculation()
            else:
                size_text = "—"
        else:
            size_text = _human_size(self._entry.size) if self._entry.size else "—"
        
        # Store reference to size row for updating
        self._size_row = Adw.ActionRow(title="Size", subtitle=size_text)
        self._size_row.add_css_class("card")
        return self._size_row

    def _create_parent_folder_row(self) -> Gtk.Widget:
        """Create the parent folder row."""
        parent_path = os.path.dirname(os.path.join(self._current_path, self._entry.name))
        if not parent_path:
            parent_path = "/"
        
        row = Adw.ActionRow(title="Parent Folder", subtitle=parent_path)
        row.add_css_class("card")
        
        # Add folder open button for local files
        if not self._is_remote_file():
            from sshpilot import icon_utils
            btn = icon_utils.new_button_from_icon_name("folder-open-symbolic")
            btn.add_css_class("flat")
            btn.connect("clicked", self._on_open_parent)
            row.add_suffix(btn)
            row.set_activatable_widget(btn)
        
        return row

    def _create_modified_row(self) -> Gtk.Widget:
        """Create the modified date row."""
        modified_time = _human_time(self._entry.modified) if self._entry.modified else "—"
        row = Adw.ActionRow(title="Modified", subtitle=modified_time)
        row.add_css_class("card")
        return row

    def _create_created_row(self) -> Gtk.Widget:
        """Create the created date row (if available)."""
        # For remote files, we typically don't have creation time
        if self._is_remote_file():
            return Gtk.Box()  # Empty box widget
        
        # Try to get creation time for local files
        try:
            path = os.path.join(self._current_path, self._entry.name)
            if os.path.exists(path):
                stat_result = os.stat(path)
                if hasattr(stat_result, 'st_birthtime'):  # macOS
                    created_time = _human_time(stat_result.st_birthtime)
                elif hasattr(stat_result, 'st_ctime'):  # Linux
                    created_time = _human_time(stat_result.st_ctime)
                else:
                    return Gtk.Box()  # Empty box widget
            else:
                return Gtk.Box()  # Empty box widget
        except Exception:
            return Gtk.Box()  # Empty box widget
        
        row = Adw.ActionRow(title="Created", subtitle=created_time)
        row.add_css_class("card")
        return row

    def _create_permissions_row(self) -> Gtk.Widget:
        """Create the permissions row."""
        perms_text = "—"
        
        # Get actual permissions for local files
        if not self._is_remote_file():
            try:
                path = os.path.join(self._current_path, self._entry.name)
                if os.path.exists(path):
                    stat_result = os.stat(path)
                    mode = stat_result.st_mode
                    # Show both letter format and numeric format
                    letter_format = _mode_to_str(mode)
                    numeric_format = _mode_to_octal(mode)
                    perms_text = f"{letter_format} ({numeric_format})"
                else:
                    perms_text = "—"
            except Exception:
                perms_text = "—"
        else:
            # For remote files, try to get permissions from SFTP asynchronously
            # Start with a placeholder, will be updated when stat completes
            perms_text = "Loading…"
            
            logger.debug(f"PropertiesDialog: Checking SFTP manager for remote file permissions")
            logger.debug(f"PropertiesDialog: _sftp_manager={self._sftp_manager}")
            
            if self._sftp_manager:
                has_sftp = hasattr(self._sftp_manager, '_sftp')
                logger.debug(f"PropertiesDialog: hasattr(_sftp_manager, '_sftp')={has_sftp}")
                if has_sftp:
                    sftp_client = getattr(self._sftp_manager, '_sftp', None)
                    logger.debug(f"PropertiesDialog: _sftp_manager._sftp={sftp_client}")
                
                if has_sftp and sftp_client:
                    # Build the full remote path
                    if self._current_path.endswith('/'):
                        remote_path = self._current_path + self._entry.name
                    else:
                        remote_path = posixpath.join(self._current_path, self._entry.name)
                    
                    logger.debug(f"PropertiesDialog: Fetching permissions for remote path: {remote_path}")
                    
                    # Get file attributes from SFTP asynchronously
                    def _get_attr():
                        assert self._sftp_manager._sftp is not None
                        logger.debug(f"PropertiesDialog: Background thread calling stat({remote_path})")
                        attr = self._sftp_manager._sftp.stat(remote_path)
                        logger.debug(f"PropertiesDialog: stat() returned: {attr}, st_mode={getattr(attr, 'st_mode', None)}")
                        return attr
                    
                    def _update_permissions(future):
                        try:
                            logger.debug(f"PropertiesDialog: Future completed, getting result")
                            attr = future.result()
                            logger.debug(f"PropertiesDialog: Got attr: {attr}, has st_mode: {hasattr(attr, 'st_mode')}")
                            if attr and hasattr(attr, 'st_mode'):
                                mode = attr.st_mode
                                # Show both letter format and numeric format
                                letter_format = _mode_to_str(mode)
                                numeric_format = _mode_to_octal(mode)
                                new_text = f"{letter_format} ({numeric_format})"
                                logger.debug(f"PropertiesDialog: Setting permissions to: {new_text}")
                            else:
                                # Fallback to simplified permissions
                                if self._entry.is_dir:
                                    new_text = "Create and Delete Files"
                                else:
                                    new_text = "Read and Write"
                                logger.debug(f"PropertiesDialog: No st_mode, using fallback: {new_text}")
                        except Exception as e:
                            logger.debug(f"Failed to get remote file permissions: {e}", exc_info=True)
                            # Fallback to simplified permissions if we can't get mode
                            if self._entry.is_dir:
                                new_text = "Create and Delete Files"
                            else:
                                new_text = "Read and Write"
                        
                        # Update the row subtitle on the main thread
                        GLib.idle_add(lambda: self._update_permissions_row(new_text))
                    
                    # Submit stat operation to background thread
                    logger.debug(f"PropertiesDialog: Submitting stat operation to background thread")
                    future = self._sftp_manager._submit(_get_attr)
                    future.add_done_callback(_update_permissions)
                    logger.debug(f"PropertiesDialog: Future submitted, callback added")
                else:
                    logger.debug(f"PropertiesDialog: No SFTP client available")
                    # No SFTP manager available, show simplified permissions
                    if self._entry.is_dir:
                        perms_text = "Create and Delete Files"
                    else:
                        perms_text = "Read and Write"
            else:
                logger.debug(f"PropertiesDialog: No SFTP manager available")
                # No SFTP manager available, show simplified permissions
                if self._entry.is_dir:
                    perms_text = "Create and Delete Files"
                else:
                    perms_text = "Read and Write"
        
        row = Adw.ActionRow(title="Permissions", subtitle=perms_text)
        row.add_css_class("card")
        # Store reference to row for async updates
        self._permissions_row = row
        
        return row
    
    def _update_permissions_row(self, text: str) -> None:
        """Update the permissions row subtitle."""
        if hasattr(self, '_permissions_row'):
            self._permissions_row.set_subtitle(text)

    def _is_remote_file(self) -> bool:
        """Check if this is a remote file (from SFTP)."""
        # Check if we have an SFTP manager - that's the most reliable indicator
        if self._sftp_manager is not None:
            logger.debug(f"PropertiesDialog: Detected remote file (has SFTP manager)")
            return True
        
        # Fallback heuristic - in a real implementation, you'd pass connection info
        is_remote = "://" in self._current_path or (self._current_path.startswith("/") and 
                not os.path.exists(os.path.join(self._current_path, self._entry.name)))
        logger.debug(f"PropertiesDialog: _is_remote_file()={is_remote}, path={self._current_path}, has_sftp_manager={self._sftp_manager is not None}")
        return is_remote

    def _on_open_parent(self, *_) -> None:
        """Open parent directory in system file manager."""
        try:
            if not self._is_remote_file():
                parent_dir = os.path.dirname(os.path.join(self._current_path, self._entry.name))
                if os.path.exists(parent_dir):
                    Gio.AppInfo.launch_default_for_uri(f"file://{parent_dir}", None)
        except Exception:
            pass

    def _start_folder_size_calculation(self):
        """Start calculating folder size in background thread."""
        import threading
        
        folder_path = os.path.join(self._current_path, self._entry.name)
        
        # Create and start the background thread
        thread = threading.Thread(target=self._calculate_folder_size, args=(folder_path,))
        thread.daemon = True  # Allows main program to exit even if thread is running
        thread.start()

    def _calculate_folder_size(self, path):
        """
        Recursively calculates the size of a folder.
        THIS RUNS ON A BACKGROUND THREAD.
        """
        total_size = 0
        try:
            for dirpath, dirnames, filenames in os.walk(path):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    # Skip if it is a symlink or file doesn't exist
                    if not os.path.islink(fp):
                        try:
                            total_size += os.path.getsize(fp)
                        except FileNotFoundError:
                            # File might have been deleted while scanning
                            pass
                        except OSError:
                            # Permissions error, etc.
                            pass

        except Exception:
            total_size = -1  # Use a negative value to signal an error

        # When done, schedule the UI update on the main GTK thread
        GLib.idle_add(self._update_folder_size_ui, total_size)
        
    def _update_folder_size_ui(self, total_size):
        """
        Updates the size row with the final folder size.
        THIS RUNS ON THE MAIN GTK THREAD.
        """
        if hasattr(self, '_size_row') and self._size_row:
            if total_size >= 0:
                human_readable_size = _human_size(total_size)
                if self._entry.item_count is not None:
                    size_text = f"{self._entry.item_count} item{'s' if self._entry.item_count != 1 else ''} ({human_readable_size})"
                else:
                    size_text = human_readable_size
            else:
                if self._entry.item_count is not None:
                    size_text = f"{self._entry.item_count} item{'s' if self._entry.item_count != 1 else ''} (size unavailable)"
                else:
                    size_text = "Size unavailable"
            
            self._size_row.set_subtitle(size_text)
            
        # Returning GLib.SOURCE_REMOVE ensures this function only runs once
        return GLib.SOURCE_REMOVE


class FilePane(Gtk.Box):
    """Represents a single pane in the manager."""

    _TYPEAHEAD_TIMEOUT = 1.0

    __gsignals__ = {
        "path-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "request-operation": (
            GObject.SignalFlags.RUN_FIRST,
            None,
            (str, object),
        ),
    }

    def __init__(self, label: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.toolbar = PaneToolbar()
        self.toolbar._pane_label.set_text(label)
        self.append(self.toolbar)

        self._is_remote = label.lower() == "remote"
        self._window: Optional["FileManagerWindow"] = None

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_hexpand(True)
        self._stack.set_vexpand(True)

        self._list_store = Gio.ListStore(item_type=Gtk.StringObject)
        self._selection_model = Gtk.MultiSelection.new(self._list_store)
        self._selection_anchor: Optional[int] = None

        self._suppress_next_context_menu: bool = False

        list_factory = Gtk.SignalListItemFactory()
        list_factory.connect("setup", self._on_list_setup)
        list_factory.connect("bind", self._on_list_bind)
        list_factory.connect("unbind", self._on_list_unbind)
        list_view = Gtk.ListView(model=self._selection_model, factory=list_factory)
        list_view.add_css_class("rich-list")
        list_view.set_can_focus(True)  # Enable keyboard focus for typeahead
        # Navigate on row activation (double click / Enter)
        self._list_view = list_view
        list_view.connect("activate", self._on_list_activate)

        # Wrap list view in a scrolled window for proper scrolling
        list_scrolled = Gtk.ScrolledWindow()
        list_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        list_scrolled.set_child(list_view)

        grid_factory = Gtk.SignalListItemFactory()
        grid_factory.connect("setup", self._on_grid_setup)
        grid_factory.connect("bind", self._on_grid_bind)
        grid_factory.connect("unbind", self._on_grid_unbind)
        grid_view = Gtk.GridView(
            model=self._selection_model,
            factory=grid_factory,
            max_columns=6,
        )
        grid_view.set_enable_rubberband(True)
        grid_view.set_can_focus(True)  # Enable keyboard focus for typeahead
        self._grid_view = grid_view
        # Navigate on grid item activation (double click / Enter)
        grid_view.connect("activate", self._on_grid_activate)

        # Wrap grid view in a scrolled window for proper scrolling
        grid_scrolled = Gtk.ScrolledWindow()
        grid_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        grid_scrolled.set_child(grid_view)

        self._stack.add_named(list_scrolled, "list")
        self._stack.add_named(grid_scrolled, "grid")


        overlay = Adw.ToastOverlay()
        self._overlay = overlay
        self._current_toast = None  # Keep reference to current toast for dismissal

        content_overlay = Gtk.Overlay()
        content_overlay.set_child(self._stack)

        overlay.set_child(content_overlay)
        self.append(overlay)

        # Add drop target for file operations - use string type for better compatibility
        drop_target = Gtk.DropTarget.new(type=GObject.TYPE_STRING, actions=Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        drop_target.connect("drop", self._on_drop_string)
        drop_target.connect("enter", self._on_drop_enter)
        drop_target.connect("leave", self._on_drop_leave)
        self.add_controller(drop_target)
        
        logger.debug(f"Added drop target to pane: {self._is_remote}")

        self._partner_pane: Optional["FilePane"] = None

        self._action_buttons: Dict[str, Gtk.Button] = {}
        action_bar = Gtk.ActionBar()
        action_bar.add_css_class("inline-toolbar")

        def _create_action_button(
            name: str,
            icon_name: str,
            label: str,
            callback: Callable[[Gtk.Button], None],
        ) -> Gtk.Button:
            from sshpilot import icon_utils
            button = Gtk.Button()
            
            # Only upload and download buttons get text labels
            if name in ["upload", "download"]:
                # ButtonContent only supports set_icon_name, but we want bundled icons
                # So we'll use an Image widget with a label
                image = icon_utils.new_image_from_icon_name(icon_name)
                # Create a box to hold both icon and label
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                box.append(image)
                label_widget = Gtk.Label(label=label)
                box.append(label_widget)
                button.set_child(box)
            else:
                # Icon-only buttons for other actions - use Image widget as child
                image = icon_utils.new_image_from_icon_name(icon_name)
                button.set_child(image)
                button.set_tooltip_text(label)
            
            # Improve button alignment and styling
            button.set_valign(Gtk.Align.CENTER)
            button.set_has_frame(False)
            button.add_css_class("flat")
            
            button.connect("clicked", callback)
            self._action_buttons[name] = button
            return button

        download_button = _create_action_button(
            "download",
            "document-save-symbolic",
            "Download",
            lambda _button: self._on_download_clicked(_button),
        )
        upload_button = _create_action_button(
            "upload",
            "document-send-symbolic",
            "Upload",
            lambda _button: self._on_upload_clicked(_button),
        )
        copy_button = _create_action_button(
            "copy",
            "edit-copy-symbolic",
            "Copy",
            lambda _button: self._emit_entry_operation("copy"),
        )
        cut_button = _create_action_button(
            "cut",
            "edit-cut-symbolic",
            "Cut",
            lambda _button: self._emit_entry_operation("cut"),
        )
        paste_button = _create_action_button(
            "paste",
            "edit-paste-symbolic",
            "Paste",
            lambda _button: self._emit_paste_operation(),
        )
        edit_button = _create_action_button(
            "edit",
            "text-editor-symbolic",
            "Edit",
            lambda _button: self._on_menu_edit(),
        )
        rename_button = _create_action_button(
            "rename",
            "document-edit-symbolic",
            "Rename",
            lambda _button: self._emit_entry_operation("rename"),
        )
        delete_button = _create_action_button(
            "delete",
            "user-trash-symbolic",
            "Delete",
            lambda _button: self._emit_entry_operation("delete"),
        )
        download_button.set_visible(self._is_remote)
        upload_button.set_visible(not self._is_remote)
        edit_button.set_visible(False)  # Initially hidden, shown when text file is selected

        # Add Request Access button for local pane in Flatpak (always show when in Flatpak)
        request_access_button = None
        if not self._is_remote and is_flatpak():
            request_access_button = _create_action_button(
                "request_access",
                "folder-open-symbolic",
                "Request Access",
                lambda _button: self._on_request_access_clicked(),
            )
            # Use ButtonContent for this special button to make it more prominent
            content = Adw.ButtonContent()
            content.set_icon_name("folder-open-symbolic")
            content.set_label("Request Access")
            request_access_button.set_child(content)
            # Ensure the button uses the suggested-action styling with visible background
            request_access_button.set_has_frame(True)
            request_access_button.remove_css_class("flat")
            request_access_button.add_css_class("suggested-action")
            # Store reference to the button so we can hide it later
            self._request_access_button = request_access_button
        else:
            self._request_access_button = None

        action_bar.pack_start(upload_button)
        action_bar.pack_start(download_button)
        if request_access_button:
            action_bar.pack_start(request_access_button)
        action_bar.pack_end(delete_button)
        action_bar.pack_end(rename_button)
        action_bar.pack_end(edit_button)
        action_bar.pack_end(cut_button)
        action_bar.pack_end(copy_button)
        action_bar.pack_end(paste_button)

        self._action_bar = action_bar
        self.append(action_bar)

        self._can_paste: bool = False

        # Connect to view-changed signal from toolbar
        self.toolbar.connect("view-changed", self._on_view_toggle)
        self.toolbar.connect("show-hidden-toggled", self._on_toolbar_show_hidden_toggled)
        self.toolbar.path_entry.connect("activate", self._on_path_entry)
        # Wire navigation buttons
        self.toolbar.controls.up_button.connect("clicked", self._on_up_clicked)
        self.toolbar.controls.back_button.connect("clicked", self._on_back_clicked)
        self.toolbar.controls.refresh_button.connect("clicked", self._on_refresh_clicked)
        self.toolbar.controls.new_folder_button.connect(
            "clicked", lambda *_: self.emit("request-operation", "mkdir", None)
        )
        # Upload/download functionality is now available through action bar and context menu only

        self._history: List[str] = []
        self._current_path = "/"
        self._entries: List[FileEntry] = []
        self._cached_entries: List[FileEntry] = []
        self._raw_entries: List[FileEntry] = []
        self._show_hidden = False
        self.toolbar.set_show_hidden_state(self._show_hidden)
        self._sort_key = "name"  # Default sort by name
        self._sort_descending = False  # Default ascending order

        self._suppress_history_push: bool = False
        self._selection_model.connect("selection-changed", self._on_selection_changed)

        self._menu_actions: Dict[str, Gio.SimpleAction] = {}
        self._menu_action_callbacks: Dict[str, Callable[[], None]] = {}  # Store callbacks for direct access
        self._menu_action_group = Gio.SimpleActionGroup()
        self.insert_action_group("pane", self._menu_action_group)
        self._menu_popover: Gtk.Popover = self._create_menu_model()
        self._add_context_controller(list_view)
        self._add_context_controller(grid_view)

        for view in (list_view, grid_view):
            controller = Gtk.EventControllerKey.new()
            controller.connect("key-pressed", self._on_typeahead_key_pressed)
            view.add_controller(controller)
            self._attach_shortcuts(view)


        self._update_menu_state()
        # Set up sorting actions for the split button
        self._setup_sorting_actions()
        
        # Initialize view button icon and direction states
        self._update_view_button_icon()
        self._update_sort_direction_states()

        self._typeahead_buffer: str = ""
        self._typeahead_last_time: float = 0.0

    # -- drop zone & drag support -------------------------------------

    def set_partner_pane(self, partner: Optional["FilePane"]) -> None:
        self._partner_pane = partner






    # -- callbacks ------------------------------------------------------

    def _attach_shortcuts(self, view: Gtk.Widget) -> None:
        controller = Gtk.ShortcutController()
        controller.set_scope(Gtk.ShortcutScope.LOCAL)

        def add_shortcut(trigger: Gtk.ShortcutTrigger, handler: Callable[[], bool]) -> None:
            if trigger is None:
                return
            action = Gtk.CallbackAction.new(lambda _widget, _args: handler())
            controller.add_shortcut(Gtk.Shortcut.new(trigger, action))

        def add_trigger_string(trigger_str: str, handler: Callable[[], bool]) -> None:
            if not trigger_str:
                return
            trigger = Gtk.ShortcutTrigger.parse_string(trigger_str)
            add_shortcut(trigger, handler)

        add_trigger_string("<primary>l", self._shortcut_focus_path_entry)
        add_trigger_string("<primary>r", self._shortcut_refresh)
        add_shortcut(Gtk.KeyvalTrigger.new(Gdk.KEY_F5, Gdk.ModifierType(0)), self._shortcut_refresh)
        add_trigger_string("<primary>c", lambda: self._shortcut_operation("copy"))
        add_trigger_string("<primary>x", lambda: self._shortcut_operation("cut"))
        add_trigger_string("<primary>v", lambda: self._shortcut_operation("paste"))
        add_trigger_string(
            "<shift><primary>v",
            lambda: self._shortcut_operation("paste", force_move=True),
        )

        delete_triggers = [
            Gtk.KeyvalTrigger.new(Gdk.KEY_Delete, Gdk.ModifierType(0)),
            Gtk.KeyvalTrigger.new(Gdk.KEY_KP_Delete, Gdk.ModifierType(0)),
            Gtk.KeyvalTrigger.new(Gdk.KEY_Delete, Gdk.ModifierType.SHIFT_MASK),
            Gtk.KeyvalTrigger.new(Gdk.KEY_KP_Delete, Gdk.ModifierType.SHIFT_MASK),
        ]
        for trigger in delete_triggers:
            add_shortcut(trigger, self._shortcut_delete)

        view.add_controller(controller)

    def _shortcut_focus_path_entry(self) -> bool:
        entry = getattr(self.toolbar, "path_entry", None)
        if isinstance(entry, Gtk.Entry):
            try:
                entry.grab_focus()
                entry.select_region(0, -1)
            except Exception:
                pass
        return True

    def _shortcut_refresh(self) -> bool:
        self._on_refresh_clicked(None)
        return True

    def _shortcut_delete(self) -> bool:
        self._emit_entry_operation("delete")
        return True

    def _on_view_toggle(self, toolbar, view_name: str) -> None:
        self._stack.set_visible_child_name(view_name)
        # Update the split button icon to reflect current view
        self._update_view_button_icon()

    def _on_toolbar_show_hidden_toggled(self, _toolbar, show_hidden: bool) -> None:
        self.set_show_hidden(show_hidden)

    def _on_path_entry(self, entry: Gtk.Entry) -> None:
        self.emit("path-changed", entry.get_text() or "/")

    def _on_list_setup(self, factory: Gtk.SignalListItemFactory, item):
        from .icon_utils import new_image_from_icon_name
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        icon = new_image_from_icon_name("folder-symbolic")
        icon.set_valign(Gtk.Align.CENTER)
        name_label = Gtk.Label(xalign=0)
        name_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        name_label.set_max_width_chars(40)
        name_label.set_hexpand(True)
        metadata_label = Gtk.Label(xalign=1)
        metadata_label.set_halign(Gtk.Align.END)
        metadata_label.set_ellipsize(Pango.EllipsizeMode.END)
        metadata_label.add_css_class("dim-label")
        box.append(icon)
        box.append(name_label)
        box.append(metadata_label)
        box.set_hexpand(True)
        # Store references as Python attributes instead of deprecated set_data
        box.icon = icon
        box.name_label = name_label
        box.metadata_label = metadata_label
        
        # Add drag source for file operations
        drag_source = Gtk.DragSource()
        drag_source.set_actions(Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        drag_source.connect("prepare", self._on_drag_prepare)
        drag_source.connect("drag-begin", self._on_drag_begin)
        drag_source.connect("drag-end", self._on_drag_end)
        box.add_controller(drag_source)
        
        # Add right-click gesture to select item and show context menu
        right_click_gesture = Gtk.GestureClick()
        right_click_gesture.set_button(Gdk.BUTTON_SECONDARY)
        right_click_gesture.connect("pressed", self._on_list_item_right_click, item)
        box.add_controller(right_click_gesture)
        
        item.set_child(box)

    def _on_list_bind(self, factory: Gtk.SignalListItemFactory, item):
        box = item.get_child()
        # Access references as Python attributes instead of deprecated get_data
        icon: Gtk.Image = box.icon
        name_label: Gtk.Label = box.name_label
        metadata_label: Gtk.Label = box.metadata_label

        position = item.get_position()
        entry: Optional[FileEntry] = None
        if position is not None and 0 <= position < len(self._entries):
            entry = self._entries[position]
            
        # Store position in the box for drag operations
        box.drag_position = position

        if entry is None:
            value = item.get_item().get_string()
            name_label.set_text(value)
            name_label.set_tooltip_text(value)
            metadata_label.set_text("—")
            metadata_label.set_tooltip_text(None)
            from .icon_utils import set_icon_from_name
            set_icon_from_name(icon, "folder-symbolic" if value.endswith('/') else "text-x-generic-symbolic")
            return

        display_name = entry.name + ("/" if entry.is_dir else "")
        name_label.set_text(display_name)
        name_label.set_tooltip_text(display_name)

        if entry.is_dir:
            if entry.item_count is not None:
                count_text = f"{entry.item_count} items"
                metadata_label.set_text(count_text)
                metadata_label.set_tooltip_text(count_text)
            else:
                metadata_label.set_text("—")
                metadata_label.set_tooltip_text(None)
        else:
            size_text = self._format_size(entry.size)
            metadata_label.set_text(size_text)
            metadata_label.set_tooltip_text(size_text)

        from .icon_utils import set_icon_from_name
        if entry.is_dir:
            set_icon_from_name(icon, "folder-symbolic")
        else:
            set_icon_from_name(icon, "text-x-generic-symbolic")

        box._pane_entry = entry
        box._pane_index = position

    def _on_list_unbind(self, factory: Gtk.SignalListItemFactory, item):
        box = item.get_child()
        if box is None:
            return

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        if size_bytes < 1024:
            return f"{size_bytes} B"
        units = ["KB", "MB", "GB", "TB", "PB"]
        value = float(size_bytes)
        for unit in units:
            value /= 1024.0
            if value < 1024.0:
                return f"{value:.1f} {unit}"
        return f"{value:.1f} EB"

    def _on_grid_setup(self, factory: Gtk.SignalListItemFactory, item):
        button = Gtk.Button()
        button.set_has_frame(False)
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
        )
        content.set_halign(Gtk.Align.CENTER)
        content.set_valign(Gtk.Align.CENTER)

        from .icon_utils import new_image_from_icon_name
        image = new_image_from_icon_name("folder-symbolic", size=64)
        image.set_halign(Gtk.Align.CENTER)
        content.append(image)

        label = Gtk.Label()
        label.set_halign(Gtk.Align.CENTER)
        label.set_justify(Gtk.Justification.CENTER)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_wrap(True)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_lines(2)
        # Set normal font weight to override any bold styling
        label.add_css_class("caption")
        content.append(label)

        button.set_child(content)

        # GestureClick allows inspecting modifier state and click counts before
        # the button consumes the event. Use this to keep selection behaviour in
        # sync with Gtk.GridView expectations.
        click_gesture = Gtk.GestureClick()
        click_gesture.set_button(Gdk.BUTTON_PRIMARY)
        if hasattr(click_gesture, "set_exclusive"):
            try:
                click_gesture.set_exclusive(True)
            except Exception:
                pass
        propagation_phase = getattr(Gtk, "PropagationPhase", None)
        if propagation_phase is not None and hasattr(click_gesture, "set_propagation_phase"):
            try:
                click_gesture.set_propagation_phase(propagation_phase.CAPTURE)
            except Exception:
                pass
        click_gesture.connect("pressed", self._on_grid_cell_pressed, button)
        button.add_controller(click_gesture)
        
        # Add right-click gesture to select item and show context menu
        right_click_gesture = Gtk.GestureClick()
        right_click_gesture.set_button(Gdk.BUTTON_SECONDARY)
        right_click_gesture.connect("pressed", self._on_grid_item_right_click, item)
        button.add_controller(right_click_gesture)
        
        # Add drag source for file operations
        drag_source = Gtk.DragSource()
        drag_source.set_actions(Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        drag_source.connect("prepare", self._on_drag_prepare)
        drag_source.connect("drag-begin", self._on_drag_begin)
        drag_source.connect("drag-end", self._on_drag_end)
        button.add_controller(drag_source)
        
        item.set_child(button)

    def _on_grid_bind(self, factory: Gtk.SignalListItemFactory, item):
        # Grid view uses the same icon for now but honours the entry name as
        # tooltip so users can differentiate.
        button = item.get_child()
        content = button.get_child()
        image = content.get_first_child()
        label = content.get_last_child()

        value = item.get_item().get_string()
        display_text = value[:-1] if value.endswith('/') else value

        label.set_text(display_text)
        label.set_tooltip_text(display_text)
        button.set_tooltip_text(display_text)

        # Update the image icon based on type
        from .icon_utils import set_icon_from_name
        if value.endswith('/'):
            set_icon_from_name(image, "folder-symbolic")
        else:
            set_icon_from_name(image, "text-x-generic-symbolic")

        entry: Optional[FileEntry] = None
        position = item.get_position()
        if position is not None and 0 <= position < len(self._entries):
            entry = self._entries[position]
            
        # Store position in the button for drag operations
        button.drag_position = position


    def _on_grid_unbind(self, factory: Gtk.SignalListItemFactory, item):
        button = item.get_child()
        if button is None:
            return

    def _on_grid_cell_pressed(
        self,
        gesture: Gtk.GestureClick,
        n_press: int,
        _x: float,
        _y: float,
        button: Gtk.Button,
    ) -> None:
        position = getattr(button, "drag_position", None)
        if position is None or not (0 <= position < len(self._entries)):
            return

        if n_press == 1:
            self._update_grid_selection_for_press(position, gesture)
            return

        if n_press >= 2:
            try:
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            except Exception:
                pass
            self._suppress_next_context_menu = True
            self._navigate_to_entry(position)

    def _update_grid_selection_for_press(
        self, position: int, gesture: Gtk.GestureClick
    ) -> None:
        state = gesture.get_current_event_state()
        if state is None:
            state = Gdk.ModifierType(0)

        primary_mask = getattr(Gdk.ModifierType, "CONTROL_MASK", 0)
        if is_macos():
            primary_mask |= (
                getattr(Gdk.ModifierType, "META_MASK", 0)
                | getattr(Gdk.ModifierType, "SUPER_MASK", 0)
            )

        has_primary = bool(state & primary_mask)
        has_shift = bool(state & getattr(Gdk.ModifierType, "SHIFT_MASK", 0))

        if has_shift and self._selection_anchor is not None:
            start = min(self._selection_anchor, position)
            end = max(self._selection_anchor, position)
            self._selection_model.unselect_all()
            for index in range(start, end + 1):
                self._selection_model.select_item(index, False)
            self._selection_anchor = position
        elif has_shift:
            self._selection_model.select_item(position, True)
            self._selection_anchor = position
        elif has_primary:
            is_selected = False
            if hasattr(self._selection_model, "is_selected"):
                try:
                    is_selected = self._selection_model.is_selected(position)
                except Exception:
                    is_selected = False
            if is_selected:
                self._selection_model.unselect_item(position)
            else:
                self._selection_model.select_item(position, False)
            self._selection_anchor = position
        else:
            is_selected = False
            if hasattr(self._selection_model, "is_selected"):
                try:
                    is_selected = self._selection_model.is_selected(position)
                except Exception:
                    is_selected = False
            if not is_selected or self._selection_model is None:
                try:
                    self._selection_model.unselect_all()
                except Exception:
                    pass
                self._selection_model.select_item(position, False)
            self._selection_anchor = position

    def _on_selection_changed(self, model, position, n_items):
        self._update_menu_state()

    def _setup_sorting_actions(self) -> None:
        """Set up sorting actions for the split button menu."""
        # Create actions for sorting
        self._menu_actions["sort-by-name"] = Gio.SimpleAction.new("sort-by-name", None)
        self._menu_actions["sort-by-size"] = Gio.SimpleAction.new("sort-by-size", None)
        self._menu_actions["sort-by-modified"] = Gio.SimpleAction.new("sort-by-modified", None)
        
        # Create stateful actions for sort direction (radio buttons)
        self._menu_actions["sort-direction-asc"] = Gio.SimpleAction.new_stateful(
            "sort-direction-asc", None, GLib.Variant.new_boolean(not self._sort_descending)
        )
        self._menu_actions["sort-direction-desc"] = Gio.SimpleAction.new_stateful(
            "sort-direction-desc", None, GLib.Variant.new_boolean(self._sort_descending)
        )
        
        # Connect action handlers
        self._menu_actions["sort-by-name"].connect("activate", lambda *_: self._on_sort_by("name"))
        self._menu_actions["sort-by-size"].connect("activate", lambda *_: self._on_sort_by("size"))
        self._menu_actions["sort-by-modified"].connect("activate", lambda *_: self._on_sort_by("modified"))
        self._menu_actions["sort-direction-asc"].connect("activate", lambda *_: self._on_sort_direction(False))
        self._menu_actions["sort-direction-desc"].connect("activate", lambda *_: self._on_sort_direction(True))
        
        # Add actions to action group
        for action in self._menu_actions.values():
            self._menu_action_group.add_action(action)

    def _on_sort_by(self, sort_key: str) -> None:
        """Handle sort by selection from menu."""
        if self._sort_key != sort_key:
            self._sort_key = sort_key
            self._refresh_sorted_entries(preserve_selection=True)

    def _on_sort_direction(self, descending: bool) -> None:
        """Handle sort direction selection from menu."""
        if self._sort_descending != descending:
            self._sort_descending = descending
            self._refresh_sorted_entries(preserve_selection=True)
            self._update_sort_direction_states()

    def _update_view_button_icon(self) -> None:
        """Update the split button icon based on current view mode."""
        # Check which view is currently active
        if hasattr(self.toolbar, '_current_view') and self.toolbar._current_view == "list":
            icon_name = "view-list-symbolic"
        else:
            icon_name = "view-grid-symbolic"
        
        # Adw.SplitButton uses set_icon_name()
        self.toolbar.sort_split_button.set_icon_name(icon_name)

    def _update_sort_direction_states(self) -> None:
        """Update the radio button states for sort direction."""
        asc_action = self._menu_actions["sort-direction-asc"]
        desc_action = self._menu_actions["sort-direction-desc"]
        
        asc_action.set_state(GLib.Variant.new_boolean(not self._sort_descending))
        desc_action.set_state(GLib.Variant.new_boolean(self._sort_descending))

    def _create_menu_model(self) -> Gtk.Popover:
        # Create menu actions first
        def _add_action(name: str, callback: Callable[[], None]) -> None:
            if name not in self._menu_actions:
                action = Gio.SimpleAction.new(name, None)
                # Store callback for direct access
                self._menu_action_callbacks[name] = callback

                def _on_activate(_action: Gio.SimpleAction, _param: Optional[GLib.Variant]) -> None:
                    try:
                        logger.debug(f"_add_action: _on_activate called for action '{name}'")
                        callback()
                        logger.debug(f"_add_action: callback for '{name}' completed successfully")
                    except Exception as e:
                        logger.error(f"_add_action: Error in callback for '{name}': {e}", exc_info=True)

                action.connect("activate", _on_activate)
                self._menu_action_group.add_action(action)
                self._menu_actions[name] = action

        _add_action("download", self._on_menu_download)
        _add_action("upload", self._on_menu_upload)
        _add_action("edit", self._on_menu_edit)
        _add_action("copy", lambda: self._emit_entry_operation("copy"))
        _add_action("cut", lambda: self._emit_entry_operation("cut"))
        _add_action("paste", self._emit_paste_operation)
        _add_action("rename", lambda: self._emit_entry_operation("rename"))
        _add_action("delete", lambda: self._emit_entry_operation("delete"))
        _add_action("new_folder", lambda: self.emit("request-operation", "mkdir", None))
        _add_action("properties", self._on_menu_properties)

        # Create popover with listbox (same style as connection list)
        popover = Gtk.Popover.new()
        popover.set_has_arrow(True)
        
        # Create listbox for menu items (same margins as connection list)
        listbox = Gtk.ListBox(margin_top=2, margin_bottom=2, margin_start=2, margin_end=2)
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        popover.set_child(listbox)
        
        return popover


    def _on_list_item_right_click(self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float, list_item: Gtk.ListItem) -> None:
        """Handle right-click on a list item: select it and show context menu."""
        position = list_item.get_position()
        if position is not None and 0 <= position < len(self._entries):
            # Check if the clicked item is already selected
            is_selected = False
            if hasattr(self._selection_model, "is_selected"):
                try:
                    is_selected = self._selection_model.is_selected(position)
                except Exception:
                    is_selected = False
            
            # If the clicked item is not already selected, clear selection and select only this item
            # If it is already selected, preserve the current selection
            if not is_selected:
                self._selection_model.unselect_all()
                self._selection_model.select_item(position, False)
            else:
                # Ensure the clicked item is selected (should already be, but be safe)
                self._selection_model.select_item(position, False)
            self._selection_anchor = position
        
        # Show context menu at click position
        box = list_item.get_child()
        if box:
            # Convert coordinates to the view widget's coordinate space
            view_widget = self._list_view
            widget_x, widget_y = box.translate_coordinates(view_widget, x, y)
            if widget_x is not None and widget_y is not None:
                self._show_context_menu(view_widget, widget_x, widget_y)
            else:
                # Fallback: use the box coordinates
                self._show_context_menu(box, x, y)

    def _on_grid_item_right_click(self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float, list_item: Gtk.ListItem) -> None:
        """Handle right-click on a grid item: select it and show context menu."""
        position = list_item.get_position()
        if position is not None and 0 <= position < len(self._entries):
            # Check if the clicked item is already selected
            is_selected = False
            if hasattr(self._selection_model, "is_selected"):
                try:
                    is_selected = self._selection_model.is_selected(position)
                except Exception:
                    is_selected = False
            
            # If the clicked item is not already selected, clear selection and select only this item
            # If it is already selected, preserve the current selection
            if not is_selected:
                self._selection_model.unselect_all()
                self._selection_model.select_item(position, False)
            else:
                # Ensure the clicked item is selected (should already be, but be safe)
                self._selection_model.select_item(position, False)
            self._selection_anchor = position
        
        # Show context menu at click position
        button = list_item.get_child()
        if button:
            # Convert coordinates to the view widget's coordinate space
            view_widget = self._grid_view
            widget_x, widget_y = button.translate_coordinates(view_widget, x, y)
            if widget_x is not None and widget_y is not None:
                self._show_context_menu(view_widget, widget_x, widget_y)
            else:
                # Fallback: use the button coordinates
                self._show_context_menu(button, x, y)

    def _add_context_controller(self, widget: Gtk.Widget) -> None:
        gesture = Gtk.GestureClick()
        gesture.set_button(Gdk.BUTTON_SECONDARY)

        def _on_pressed(_gesture: Gtk.GestureClick, n_press: int, x: float, y: float) -> None:
            # Check if click is on an item or empty space
            # If on empty space, clear selection before showing menu
            if self._is_click_on_empty_space(widget, x, y):
                self._selection_model.unselect_all()
                self._selection_anchor = None
            self._show_context_menu(widget, x, y)

        gesture.connect("pressed", _on_pressed)
        widget.add_controller(gesture)

        long_press = Gtk.GestureLongPress()

        def _on_long_press(_gesture: Gtk.GestureLongPress, x: float, y: float) -> None:
            # Check if click is on an item or empty space
            if self._is_click_on_empty_space(widget, x, y):
                self._selection_model.unselect_all()
                self._selection_anchor = None
            self._show_context_menu(widget, x, y)

        long_press.connect("pressed", _on_long_press)
        widget.add_controller(long_press)




    def _show_context_menu(self, widget: Gtk.Widget, x: float, y: float) -> None:
        if getattr(self, '_suppress_next_context_menu', False):
            self._suppress_next_context_menu = False
            return
        # Selection is now handled by item-level gestures or cleared for empty space
        # No need to update selection here
        self._update_menu_state()
        try:
            widget.grab_focus()
        except Exception:
            pass
        
        # Get the listbox from the popover
        listbox = self._menu_popover.get_child()
        if not isinstance(listbox, Gtk.ListBox):
            return
        
        # Clear existing items
        while listbox.get_first_child() is not None:
            listbox.remove(listbox.get_first_child())
        
        # Check if items are selected
        try:
            if not hasattr(self, '_entries') or not self._entries:
                has_selection = False
            else:
                selected_entries = self.get_selected_entries()
                has_selection = len(selected_entries) > 0
        except AttributeError:
            has_selection = False
        
        # Build menu items using Adw.ActionRow (same style as connection list)
        def _add_menu_item(title: str, icon_name: str, action_name: str) -> None:
            row = Adw.ActionRow(title=title)
            # Use our helper function to prefer bundled icons
            from sshpilot import icon_utils
            icon = icon_utils.new_image_from_icon_name(icon_name)
            row.add_prefix(icon)
            row.set_activatable(True)
            def _on_activated(*_):
                try:
                    logger.debug(f"_show_context_menu: Menu item '{title}' (action '{action_name}') activated")
                    # Get the callback and call it directly
                    callback = self._menu_action_callbacks.get(action_name)
                    if callback:
                        logger.debug(f"_show_context_menu: Found callback for '{action_name}', calling directly")
                        callback()
                        logger.debug(f"_show_context_menu: Callback for '{action_name}' completed")
                    else:
                        logger.error(f"_show_context_menu: Callback for '{action_name}' not found. Available callbacks: {list(self._menu_action_callbacks.keys())}")
                        # Fallback: try to activate the action
                        action = self._menu_actions.get(action_name)
                        if action:
                            logger.debug(f"_show_context_menu: Falling back to action.activate() for '{action_name}'")
                            action.activate(None)
                except Exception as e:
                    logger.error(f"_show_context_menu: Failed to execute action '{action_name}': {e}", exc_info=True)
                finally:
                    self._menu_popover.popdown()
            row.connect('activated', _on_activated)
            listbox.append(row)
        
        # Add Download/Upload based on pane type and selection
        if self._is_remote and has_selection:
            _add_menu_item("Download", "document-save-symbolic", "download")
        elif not self._is_remote and has_selection:
            _add_menu_item("Upload…", "document-send-symbolic", "upload")
        
        # Add Edit for any single file (both local and remote)
        if has_selection:
            selected_entries = self.get_selected_entries()
            if len(selected_entries) == 1 and not selected_entries[0].is_dir:
                _add_menu_item("Edit", "text-editor-symbolic", "edit")
        
        # Add clipboard operations if items are selected
        if has_selection:
            _add_menu_item("Copy", "edit-copy-symbolic", "copy")
            _add_menu_item("Cut", "edit-cut-symbolic", "cut")
        
        # Add Paste if clipboard has items
        if getattr(self, "_can_paste", False):
            _add_menu_item("Paste", "edit-paste-symbolic", "paste")
        
        # Add management operations if items are selected
        if has_selection:
            _add_menu_item("Rename…", "document-edit-symbolic", "rename")
            _add_menu_item("Delete", "user-trash-symbolic", "delete")
        
        # Add New Folder only if no items are selected (before Properties)
        if not has_selection:
            _add_menu_item("New Folder", "folder-new-symbolic", "new_folder")
        
        # Always add Properties (at the end)
        _add_menu_item("Properties…", "document-properties-symbolic", "properties")
        
        # Create a rectangle for the popover positioning
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1
        
        # Set parent and show popover
        if self._menu_popover.get_parent() != widget:
            self._menu_popover.set_parent(widget)
        
        self._menu_popover.set_pointing_to(rect)
        self._menu_popover.popup()

    def _is_click_on_empty_space(self, widget: Gtk.Widget, x: float, y: float) -> bool:
        """Check if the click is on empty space (not on an item)."""
        # Determine which view is active
        visible_child = self._stack.get_visible_child()
        if visible_child is None:
            return True
        
        # Find the actual view widget (list or grid) in the scrolled window
        view_widget = None
        for child in visible_child:
            if isinstance(child, Gtk.ScrolledWindow):
                scrolled_child = child.get_child()
                if scrolled_child == self._list_view:
                    view_widget = self._list_view
                elif scrolled_child == self._grid_view:
                    view_widget = self._grid_view
                break
        
        if view_widget is None:
            return True
        
        try:
            # Convert coordinates to view widget's coordinate space
            widget_x, widget_y = widget.translate_coordinates(view_widget, x, y)
            if widget_x is None or widget_y is None:
                return True
            
            # Use pick() to find which child widget is at the coordinates
            picked = view_widget.pick(widget_x, widget_y, Gtk.PickFlags.DEFAULT)
            if picked is None:
                return True
            
            # Check if we picked an actual item (not just the view widget itself)
            # For ListView: check if we picked a list item or its child
            if isinstance(view_widget, Gtk.ListView):
                # Walk up the widget tree to see if we hit a list item
                current = picked
                while current and current != view_widget:
                    # If we find a widget that has the drag_position attribute, it's an item
                    if hasattr(current, 'drag_position'):
                        return False
                    # If we find a box that's a list item child, it's an item
                    if isinstance(current, Gtk.Box) and hasattr(current, '_pane_entry'):
                        return False
                    current = current.get_parent()
                # If we only hit the view widget itself, it's empty space
                return picked == view_widget
            
            # For GridView: check if we picked a button (grid item)
            elif isinstance(view_widget, Gtk.GridView):
                # Walk up the widget tree to see if we hit a button
                current = picked
                while current and current != view_widget:
                    # If we find a button with drag_position, it's an item
                    if isinstance(current, Gtk.Button) and hasattr(current, 'drag_position'):
                        return False
                    current = current.get_parent()
                # If we only hit the view widget itself, it's empty space
                return picked == view_widget
            
            return True
        except Exception as e:
            logger.debug(f"Error checking if click is on empty space: {e}")
            return True

    def _get_selected_indices(self) -> List[int]:
        indices: List[int] = []
        total = len(self._entries)
        if hasattr(self._selection_model, "is_selected"):
            for index in range(total):
                try:
                    if self._selection_model.is_selected(index):
                        indices.append(index)
                except AttributeError:
                    break
        else:
            getter = getattr(self._selection_model, "get_selected", None)
            if callable(getter):
                try:
                    selected_index = getter()
                except Exception:
                    selected_index = None
                if isinstance(selected_index, int) and 0 <= selected_index < total:
                    indices.append(selected_index)
        return indices

    def _get_primary_selection_index(self) -> Optional[int]:
        indices = self._get_selected_indices()
        return indices[0] if indices else None

    def get_selected_entries(self) -> List[FileEntry]:
        return [self._entries[index] for index in self._get_selected_indices()]


    def _update_menu_state(self) -> None:
        selected_entries = self.get_selected_entries()
        selection_count = len(selected_entries)
        has_selection = selection_count > 0
        single_selection = selection_count == 1

        def _set_enabled(name: str, enabled: bool) -> None:
            action = self._menu_actions.get(name)
            if action is not None:
                action.set_enabled(enabled)

        def _set_button(name: str, enabled: bool) -> None:
            button = self._action_buttons.get(name)
            if button is not None:
                button.set_sensitive(enabled)


        # For context menu, actions are always enabled since menu items are shown/hidden dynamically
        can_paste = bool(getattr(self, "_can_paste", False))

        _set_enabled("download", self._is_remote and has_selection)
        _set_enabled("upload", (not self._is_remote) and has_selection)
        _set_enabled("copy", has_selection)
        _set_enabled("cut", has_selection)
        _set_enabled("paste", can_paste)
        # Edit is enabled for single file selection (any file type)
        can_edit = single_selection and not selected_entries[0].is_dir if single_selection else False
        
        _set_enabled("edit", can_edit)
        _set_enabled("rename", single_selection)
        _set_enabled("delete", has_selection)
        _set_enabled("properties", single_selection)
        # new_folder is available in context menu only now

        # Action bar buttons still use the old logic
        _set_button("download", self._is_remote and has_selection)
        _set_button("upload", (not self._is_remote) and has_selection)
        _set_button("copy", has_selection)
        _set_button("cut", has_selection)
        _set_button("paste", can_paste)
        _set_button("rename", single_selection)
        _set_button("edit", can_edit)
        
        # Show/hide edit button based on selection (always show for single file)
        edit_button = self._action_buttons.get("edit")
        if edit_button is not None:
            edit_button.set_visible(can_edit)
        _set_button("delete", has_selection)

    def _emit_entry_operation(self, action: str) -> None:
        entries = self.get_selected_entries()
        if not entries:
            self.show_toast("Select at least one item first")
            return
        if action == "rename" and len(entries) != 1:
            self.show_toast("Select a single item to rename")
            return
        payload = {"entries": entries, "directory": self._current_path}
        self.emit("request-operation", action, payload)

    def _emit_paste_operation(self, *, force_move: bool = False) -> None:
        payload = {"directory": self._current_path}
        if force_move:
            payload["force_move"] = True
        self.emit("request-operation", "paste", payload)

    def _shortcut_operation(self, action: str, *, force_move: bool = False) -> bool:
        if action in {"copy", "cut", "rename", "delete"}:
            self._emit_entry_operation(action)
            return True
        if action == "paste":
            self._emit_paste_operation(force_move=force_move)
            return True
        return False

    def set_can_paste(self, can_paste: bool) -> None:
        current = bool(getattr(self, "_can_paste", False))
        if current == can_paste:
            return
        self._can_paste = can_paste
        self._update_menu_state()

    def set_show_hidden(self, show_hidden: bool, *, preserve_selection: bool = True) -> None:
        """Update the hidden file visibility state and refresh entries."""

        self.toolbar.set_show_hidden_state(show_hidden)
        if self._show_hidden == show_hidden:
            return

        self._show_hidden = show_hidden
        self._apply_entry_filter(preserve_selection=preserve_selection)

    def _on_menu_download(self) -> None:
        if not self._is_remote:
            return
        entries = self.get_selected_entries()
        if not entries:
            self.show_toast("Select items to download first")
            return
        self._on_download_clicked(None)

    def _on_menu_upload(self) -> None:
        if self._is_remote:
            return
        entries = self.get_selected_entries()
        if not entries:
            self.show_toast("Select items to upload first")
            return
        self._on_upload_clicked(None)


    def get_selected_entry(self) -> Optional[FileEntry]:
        selected_entries = self.get_selected_entries()
        if not selected_entries:
            return None
        return selected_entries[0]

    def set_file_manager_window(self, window: "FileManagerWindow") -> None:
        """Associate this pane with its owning file manager window."""

        self._window = window

    def _get_file_manager_window(self) -> Optional["FileManagerWindow"]:
        """Return the controlling FileManagerWindow if available."""

        window = getattr(self, "_window", None)
        if window is not None:
            return window

        root = self.get_root()
        if isinstance(root, FileManagerWindow):
            self._window = root
            return root

        # When embedded as a tab, traverse up the widget tree to find FileManagerWindow
        parent = self.get_parent()
        while parent is not None:
            if isinstance(parent, FileManagerWindow):
                self._window = parent
                return parent
            parent = parent.get_parent()

        return None

    def _on_upload_clicked(self, _button: Gtk.Button) -> None:
        window = self._get_file_manager_window()
        if not isinstance(window, FileManagerWindow):
            self.show_toast("File manager is not available")
            return

        local_pane = getattr(window, "_left_pane", None)
        if not isinstance(local_pane, FilePane):
            self.show_toast("Local pane is unavailable")
            return

        destination_pane: Optional[FilePane]
        if self._is_remote:
            destination_pane = self
        else:
            destination_pane = getattr(window, "_right_pane", None)
            if not isinstance(destination_pane, FilePane) or not destination_pane._is_remote:
                destination_pane = None

        if destination_pane is None:
            self.show_toast("Remote pane is unavailable")
            return

        entries = local_pane.get_selected_entries()
        if not entries:
            self.show_toast("Select items in the local pane to upload")
            return

        # Use the actual current path instead of the display path from path entry
        # This handles Flatpak portal paths correctly
        base_dir = getattr(local_pane, '_current_path', None)
        if not base_dir:
            # Fallback to normalized path entry text for non-portal paths
            base_dir = window._normalize_local_path(local_pane.toolbar.path_entry.get_text())
        source_paths = [pathlib.Path(os.path.join(base_dir, entry.name)) for entry in entries]

        destination = destination_pane.toolbar.path_entry.get_text() or "/"
        payload = {"paths": source_paths, "destination": destination}
        self.emit("request-operation", "upload", payload)
        if len(entries) == 1:
            self.show_toast(f"Uploading {entries[0].name}…")
        else:
            self.show_toast(f"Uploading {len(entries)} items…")


    def _on_download_clicked(self, _button: Gtk.Button) -> None:
        entries = self.get_selected_entries()
        if not entries:
            self.show_toast("Select items to download")
            return

        window = self._get_file_manager_window()
        if not isinstance(window, FileManagerWindow):
            self.show_toast("File manager is not available")
            return

        local_pane = getattr(window, "_left_pane", None)
        if local_pane is None:
            self.show_toast("Local pane is unavailable")
            return

        # Use the actual current path instead of the display path from path entry
        # This handles Flatpak portal paths correctly
        destination_root = getattr(local_pane, '_current_path', None)
        if not destination_root:
            # Fallback to normalized path entry text for non-portal paths
            destination_root = window._normalize_local_path(local_pane.toolbar.path_entry.get_text())
        
        if not os.path.isdir(destination_root):
            self.show_toast("Local destination is not accessible")
            return
        payload = {
            "entries": entries,
            "directory": self._current_path,
            "destination": pathlib.Path(destination_root),
        }
        self.emit("request-operation", "download", payload)
        if len(entries) == 1:
            self.show_toast(f"Downloading {entries[0].name}…")
        else:
            self.show_toast(f"Downloading {len(entries)} items…")

    def _on_request_access_clicked(self) -> None:
        """Handle Request Access button click in Flatpak environment."""
        # Create a confirmation dialog
        window = self.get_root()
        dialog = Adw.MessageDialog.new(
            window,
            "Request Folder Access",
            "You are using the app in a sandbox. Please grant access to your home folder to use the File Manager."
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ok", "OK")
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")
        dialog.set_close_response("cancel")
        
        def on_response(dialog, response):
            if response == "ok":
                self._show_folder_picker()
        
        dialog.connect("response", on_response)
        dialog.present()

    def _hide_request_access_button(self) -> None:
        """Hide the Request Access button after access has been granted.
        In Flatpak, always keep the button visible as requested."""
        if hasattr(self, '_request_access_button') and self._request_access_button:
            # Don't hide the button in Flatpak - always keep it visible
            if not is_flatpak():
                self._request_access_button.set_visible(False)
                logger.debug("Hid Request Access button after granting access")
            else:
                logger.debug("Keeping Request Access button visible in Flatpak")

    def _show_folder_picker(self) -> None:
        """Show a portal-aware folder picker for Flatpak with persistent access."""
        dlg = Gtk.FileChooserNative(
            title="Select Folder to Grant Access",
            action=Gtk.FileChooserAction.SELECT_FOLDER,
            transient_for=self.get_root(),
            modal=True,
        )
        
        def _resp(_dlg, resp):
            if resp == Gtk.ResponseType.ACCEPT:
                gfile = dlg.get_file()
                if gfile:
                    try:
                        path = gfile.get_path()
                        logger.debug(f"FileChooserNative returned path: {path}")
                        
                        # Grant persistent access via Document portal
                        doc_id = _grant_persistent_access(gfile)
                        if doc_id:
                            _save_doc(path, doc_id)
                            logger.info(f"Persisted access to {path} (ID={doc_id})")
                        else:
                            logger.warning(f"Could not grant persistent access to: {path}")
                        
                        # Switch to it immediately
                        self.toolbar.path_entry.set_text(path)
                        self.toolbar.path_entry.emit("activate")
                        self.show_toast(f"Access granted to: {_pretty_path_for_display(path)}")
                        
                        # Hide the Request Access button since access is now granted
                        self._hide_request_access_button()
                    except Exception as e:
                        logger.warning(f"Failed to persist folder access: {e}")
                        # Still navigate to folder even if persistence fails
                        path = gfile.get_path()
                        if path:
                            self.toolbar.path_entry.set_text(path)
                            self.toolbar.path_entry.emit("activate")
                            self.show_toast(f"Access granted to: {_pretty_path_for_display(path)}")
                            # Hide the Request Access button since access is now granted
                            self._hide_request_access_button()
            dlg.destroy()
        
        dlg.connect("response", _resp)
        dlg.show()

    def restore_persisted_folder(self) -> None:
        """Restore access to a previously granted folder on app launch (Flatpak only)."""
        if not is_flatpak():
            return
            
        logger.debug("Attempting to restore persisted folder...")
        portal_result = _load_first_doc_path()
        if portal_result:
            portal_path, doc_id, entry = portal_result
            logger.debug(f"Found persisted path: {portal_path} (doc_id={doc_id})")
            try:
                # Directly trigger the path change instead of relying on path entry activation
                self.emit("path-changed", portal_path)
                logger.info(f"Restored access to folder: {portal_path}")
            except Exception as e:
                logger.warning(f"Failed to restore folder access: {e}")
        else:
            logger.debug("No persisted path found")

    def _set_current_pathbar_text(self, path: str) -> None:
        """Set the path bar text with human-friendly display formatting."""
        display_path = _pretty_path_for_display(path)
        self.toolbar.path_entry.set_text(display_path)

    @staticmethod
    def _dialog_dismissed(error: GLib.Error) -> bool:
        dialog_error = getattr(Gtk, "DialogError", None)
        if dialog_error is not None and error.matches(dialog_error, dialog_error.DISMISSED):
            return True
        return error.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED)

    def _build_properties_details(self, entry: FileEntry, is_current_directory: bool = False) -> Dict[str, str]:
        base_path = self._current_path or "/"
        if is_current_directory:
            # For current directory, use the base path as location
            location = base_path
        else:
            location = os.path.join(base_path, entry.name)

        entry_type = "Folder" if entry.is_dir else "File"
        if entry.is_dir:
            size_text = "—"
        else:
            size_text = self._format_size(entry.size)

        try:
            modified_dt = datetime.fromtimestamp(entry.modified)
            modified_text = modified_dt.strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, OverflowError, ValueError, TypeError):
            modified_text = "Unknown"

        return {
            "name": entry.name,
            "type": entry_type,
            "size": size_text,
            "modified": modified_text,
            "location": location,
        }

    def _is_text_file(self, entry: FileEntry) -> bool:
        """Check if a file is likely a text file based on name/extension."""
        if entry.is_dir:
            return False
        
        # Check mimetype
        mimetype, _ = mimetypes.guess_type(entry.name)
        if mimetype:
            if mimetype.startswith('text/'):
                return True
            # Also allow common code file types
            text_mimes = [
                'application/json',
                'application/javascript',
                'application/xml',
                'application/x-sh',
                'application/x-python',
            ]
            if mimetype in text_mimes:
                return True
        
        # Check by extension
        _, ext = os.path.splitext(entry.name.lower())
        text_extensions = {
            '.txt', '.md', '.rst', '.log',
            '.py', '.pyw', '.pyx', '.pyi',
            '.js', '.jsx', '.ts', '.tsx',
            '.html', '.htm', '.xhtml', '.xml', '.css', '.scss', '.sass',
            '.json', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf',
            '.sh', '.bash', '.zsh', '.fish', '.ps1',
            '.c', '.cpp', '.cc', '.cxx', '.h', '.hpp', '.hxx',
            '.java', '.kt', '.scala', '.go', '.rs', '.rb', '.pl', '.pm',
            '.php', '.php3', '.php4', '.php5', '.phtml',
            '.sql', '.lua', '.vim', '.vimrc',
            '.dockerfile', '.makefile', '.cmake',
            '.properties', '.env', '.gitignore', '.gitattributes',
        }
        if ext in text_extensions:
            return True
        
        # Check if filename suggests a text file
        text_patterns = ['readme', 'license', 'changelog', 'authors', 'contributors', 'makefile']
        name_lower = entry.name.lower()
        for pattern in text_patterns:
            if pattern in name_lower:
                return True
        
        return False
    
    def _on_menu_edit(self) -> None:
        """Handle Edit menu action - open file in editor."""
        entry = self.get_selected_entry()
        if entry is None:
            self.show_toast("No file selected")
            return
        
        if entry.is_dir:
            self.show_toast("Cannot edit directories")
            return
        
        # Get file manager window
        window = self._get_file_manager_window()
        if window is None or not isinstance(window, FileManagerWindow):
            self.show_toast("Cannot edit file - window not available")
            return
        
        if self._is_remote:
            # Remote file editing
            sftp_manager = getattr(window, '_manager', None)
            if sftp_manager is None:
                self.show_toast("Cannot edit file - connection not available")
                return
            
            # Build remote path
            file_path = posixpath.join(self._current_path or "/", entry.name)
            
            # Create editor window for remote file
            try:
                editor = RemoteFileEditorWindow(
                    parent=window,
                    file_path=file_path,
                    file_name=entry.name,
                    is_local=False,
                    sftp_manager=sftp_manager,
                    file_manager_window=window,
                )
                editor.present()
            except Exception as e:
                logger.error(f"Failed to open editor: {e}", exc_info=True)
                self.show_toast(f"Failed to open editor: {e}")
        else:
            # Local file editing
            file_path = os.path.join(self._current_path or os.path.expanduser("~"), entry.name)
            file_path = os.path.abspath(os.path.expanduser(file_path))
            
            # Create editor window for local file
            try:
                editor = RemoteFileEditorWindow(
                    parent=window,
                    file_path=file_path,
                    file_name=entry.name,
                    is_local=True,
                    sftp_manager=None,
                    file_manager_window=window,
                )
                editor.present()
            except Exception as e:
                logger.error(f"Failed to open editor: {e}", exc_info=True)
                self.show_toast(f"Failed to open editor: {e}")

    def _on_menu_properties(self) -> None:
        entry = self.get_selected_entry()
        if entry is None:
            # No item selected - show properties for current directory
            current_path = self._current_path or "/"
            logger.debug(f"_on_menu_properties: No selection, showing properties for current directory: {current_path}")
            
            # Get directory name and parent path
            # PropertiesDialog expects entry.name and current_path where entry is located
            if current_path == "/":
                # Special case for root directory
                dir_name = "/"
                parent_path = "/"
            else:
                # Normalize path (remove trailing slash)
                normalized_path = current_path.rstrip("/")
                dir_name = os.path.basename(normalized_path) or normalized_path
                parent_path = os.path.dirname(normalized_path) or "/"
                # If parent_path is empty after dirname, use "/"
                if not parent_path:
                    parent_path = "/"
            
            logger.debug(f"_on_menu_properties: dir_name={dir_name}, parent_path={parent_path}, is_remote={self._is_remote}")
            
            # Create a FileEntry for the current directory
            try:
                if self._is_remote:
                    # For remote, create a basic entry with item count
                    if hasattr(self, '_entries') and self._entries is not None:
                        item_count = len(self._entries)
                    else:
                        item_count = None
                    logger.debug(f"_on_menu_properties: Creating remote directory entry with item_count={item_count}")
                    entry = FileEntry(
                        name=dir_name,
                        is_dir=True,
                        size=0,
                        modified=0.0,
                        item_count=item_count
                    )
                else:
                    # For local, get actual directory stats
                    if os.path.isdir(current_path):
                        stat_info = os.stat(current_path)
                        # Count items in directory
                        try:
                            item_count = len(list(os.scandir(current_path)))
                        except Exception:
                            item_count = None
                        
                        entry = FileEntry(
                            name=dir_name,
                            is_dir=True,
                            size=0,  # Directories don't have a meaningful size
                            modified=stat_info.st_mtime,
                            item_count=item_count
                        )
                        logger.debug(f"_on_menu_properties: Created local directory entry with modified={stat_info.st_mtime}, item_count={item_count}")
                    else:
                        logger.warning(f"_on_menu_properties: Current directory is not accessible: {current_path}")
                        self.show_toast("Current directory is not accessible")
                        return
            except Exception as e:
                logger.error(f"Error creating directory entry for properties: {e}", exc_info=True)
                self.show_toast("Unable to get directory properties")
                return
            
            # Mark that this is the current directory
            # Use parent path for PropertiesDialog so it can construct the full path correctly
            is_current_dir = True
            properties_path = parent_path
            logger.debug(f"_on_menu_properties: Using properties_path={properties_path} for current directory")
        else:
            is_current_dir = False
            properties_path = None
            logger.debug(f"_on_menu_properties: Showing properties for selected entry: {entry.name}")
        
        try:
            details = self._build_properties_details(entry, is_current_directory=is_current_dir)
            logger.debug(f"_on_menu_properties: Built properties details: {details}")
            self._show_properties_dialog(entry, details, properties_path=properties_path)
        except Exception as e:
            logger.error(f"Error showing properties dialog: {e}", exc_info=True)
            self.show_toast(f"Failed to show properties: {e}")

    def _show_properties_dialog(self, entry: FileEntry, details: Dict[str, str], properties_path: Optional[str] = None) -> None:
        """Show modern properties dialog.
        
        Args:
            entry: The file entry to show properties for
            details: Properties details dictionary
            properties_path: Optional path to use instead of self._current_path (for current directory)
        """
        window = self.get_root()
        if window is None:
            logger.error("FilePane: Cannot show properties dialog - window is None")
            self.show_toast("Cannot show properties - window not available")
            return
        
        try:
            # Get SFTP manager if this is a remote pane
            # Use _get_file_manager_window() to find the FileManagerWindow even when embedded as a tab
            sftp_manager = None
            if self._is_remote:
                file_manager_window = self._get_file_manager_window()
                if file_manager_window is not None:
                    sftp_manager = getattr(file_manager_window, '_manager', None)
                    logger.debug(f"FilePane: Getting SFTP manager for properties dialog: is_remote={self._is_remote}, file_manager_window={file_manager_window}, manager={sftp_manager}")
                else:
                    logger.debug(f"FilePane: Could not find FileManagerWindow for remote pane")
            else:
                logger.debug(f"FilePane: Not getting SFTP manager: is_remote={self._is_remote}")
            
            # Use provided path or fall back to current path
            path_for_dialog = properties_path if properties_path is not None else self._current_path
            
            logger.debug(f"FilePane: Creating PropertiesDialog with entry.name={entry.name}, path={path_for_dialog}, is_remote={self._is_remote}")
            
            # Create and show the modern properties dialog
            dialog = PropertiesDialog(entry, path_for_dialog, window, sftp_manager)
            logger.debug(f"FilePane: Created PropertiesDialog with sftp_manager={sftp_manager}, path={path_for_dialog}")
            dialog.present()
            logger.debug(f"FilePane: PropertiesDialog presented successfully")
        except Exception as e:
            logger.error(f"FilePane: Failed to show properties dialog: {e}", exc_info=True)
            # Fallback to simple message dialog if modern dialog fails
            try:
                self._show_fallback_properties_dialog(entry, details, window)
            except Exception as fallback_error:
                logger.error(f"FilePane: Fallback properties dialog also failed: {fallback_error}", exc_info=True)
                self.show_toast(f"Failed to show properties: {e}")

    def _show_fallback_properties_dialog(self, entry: FileEntry, details: Dict[str, str], window: Gtk.Window) -> None:
        """Fallback to simple properties dialog if modern dialog fails."""
        heading = f"{entry.name} Properties" if entry.name else "Properties"
        body_lines = [
            f"Name: {details['name']}",
            f"Type: {details['type']}",
            f"Size: {details['size']}",
            f"Modified: {details['modified']}",
            f"Location: {details['location']}",
        ]
        body_text = "\n".join(body_lines)

        try:
            dialog = Adw.MessageDialog(
                transient_for=window,
                modal=True,
                heading=heading,
                body=body_text
            )
            dialog.add_response("ok", "OK")
            dialog.set_default_response("ok")
            dialog.connect("response", lambda d, *_: d.destroy())
            dialog.present()
        except Exception:
            # Final fallback to basic Gtk dialog
            dialog = Gtk.MessageDialog(
                transient_for=window,
                modal=True,
                message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.OK,
                text=heading,
                secondary_text=body_text
            )
            dialog.connect("response", lambda d, *_: d.destroy())
            dialog.present()

    # -- public API -----------------------------------------------------

    def show_entries(self, path: str, entries: Iterable[FileEntry]) -> None:
        entries_list = list(entries)
        pane_type = "remote" if self._is_remote else "local"
        logger.debug(f"FilePane.show_entries: {pane_type} pane updating with {len(entries_list)} entries for path {path}")
        
        self._current_path = path
        self._set_current_pathbar_text(path)
        self._cached_entries = entries_list
        self._apply_entry_filter(preserve_selection=False)
        
        logger.debug(f"FilePane.show_entries: {pane_type} pane update completed")

    def highlight_entry(self, name: str) -> None:
        if not name:
            return
        match: Optional[int] = None
        for index, entry in enumerate(self._entries):
            if entry.name == name:
                match = index
                break
        if match is None:
            return
        self._selection_model.unselect_all()
        self._selection_anchor = None
        self._selection_model.select_item(match, False)
        self._selection_anchor = match
        self._scroll_to_position(match)

    def _apply_entry_filter(self, *, preserve_selection: bool) -> None:
        selected_names: set[str] = set()
        if preserve_selection:
            for entry in self.get_selected_entries():
                selected_names.add(entry.name)

        # Filter for hidden files and store as raw entries
        self._raw_entries = [
            entry
            for entry in self._cached_entries
            if self._show_hidden or not entry.name.startswith(".")
        ]

        # Apply sorting to get final entries
        self._entries = self._sort_entries(self._raw_entries)
        
        # Update the list store
        self._list_store.remove_all()
        restored_selection: List[int] = []
        for idx, entry in enumerate(self._entries):
            suffix = "/" if entry.is_dir else ""
            self._list_store.append(Gtk.StringObject.new(entry.name + suffix))
            if preserve_selection and entry.name in selected_names:
                restored_selection.append(idx)

        self._selection_model.unselect_all()
        self._selection_anchor = None
        for index in restored_selection:
            self._selection_model.select_item(index, False)
        if restored_selection:
            self._selection_anchor = restored_selection[-1]

        self._update_menu_state()



    # -- navigation helpers --------------------------------------------

    def _navigate_to_entry(self, position: Optional[int]) -> None:
        if position is None or not (0 <= position < len(self._entries)):
            return

        try:
            entry = self._entries[position]
        except IndexError:
            return

        if not getattr(entry, "is_dir", False):
            return

        base_path = self._current_path or ""
        target_path = os.path.join(base_path, entry.name)
        self.emit("path-changed", target_path)

    def _on_list_activate(self, _list_view: Gtk.ListView, position: int) -> None:
        self._navigate_to_entry(position)

    def _sort_entries(self, entries: Iterable[FileEntry]) -> List[FileEntry]:
        def key_func(item: FileEntry):
            if self._sort_key == "size":
                return item.size
            if self._sort_key == "modified":
                return item.modified
            return item.name.casefold()

        dirs = [entry for entry in entries if entry.is_dir]
        files = [entry for entry in entries if not entry.is_dir]

        dirs_sorted = sorted(dirs, key=key_func, reverse=self._sort_descending)
        files_sorted = sorted(files, key=key_func, reverse=self._sort_descending)
        return dirs_sorted + files_sorted

    def _refresh_sorted_entries(self, *, preserve_selection: bool) -> None:
        # Simply re-apply the filter which now includes sorting
        self._apply_entry_filter(preserve_selection=preserve_selection)

    def _on_grid_activate(self, _grid_view: Gtk.GridView, position: int) -> None:
        if position is not None and 0 <= position < len(self._entries):
            entry = self._entries[position]
            if entry.is_dir:
                self.emit("path-changed", os.path.join(self._current_path, entry.name))

    def _on_drag_prepare(self, drag_source: Gtk.DragSource, x: float, y: float) -> Gdk.ContentProvider:
        """Prepare drag data when drag operation starts."""
        # Get the widget that initiated the drag
        widget = drag_source.get_widget()
        
        # Get the position that was stored during binding
        position = getattr(widget, 'drag_position', None)
        
        logger.debug(f"Drag prepare: position={position}, entries_count={len(self._entries)}")
        
        # Create a JSON representation of the drag data so arbitrary characters
        # in paths or filenames are preserved without relying on delimiter
        # parsing.  Consumers expect the payload under the "payload" key.
        payload_dict: Optional[Dict[str, Any]] = None
        if position is not None and 0 <= position < len(self._entries):
            entry = self._entries[position]
            entry_path = os.path.join(self._current_path or "", entry.name)
            payload_dict = {
                "pane_id": id(self),
                "path": self._current_path,
                "position": position,
                "entry_name": entry.name,
                "entry_path": entry_path,
            }

        drag_data = {
            "format": "sshpilot_drag",
            "payload": payload_dict,
        }
        drag_data_string = json.dumps(drag_data, separators=(",", ":"), sort_keys=True)

        return Gdk.ContentProvider.new_for_value(drag_data_string)

    def _on_drag_begin(self, drag_source: Gtk.DragSource, drag: Gdk.Drag) -> None:
        """Called when drag operation begins - set drag icon."""
        logger.debug(f"Drag begin: pane={self._is_remote}")
        if is_macos():
            # macOS provides its own drag preview; avoid setting a custom icon.
            return
        # Create a simple icon for the drag operation
        widget = drag_source.get_widget()
        if widget:
            # Create a paintable from the widget to use as drag icon
            paintable = Gtk.WidgetPaintable.new(widget)
            drag_source.set_icon(paintable, 0, 0)

    def _on_drag_end(self, drag_source: Gtk.DragSource, drag: Gdk.Drag, delete_data: bool) -> None:
        """Called when drag operation ends."""
        # Clean up any drag-related state if needed
        pass

    def _on_drop_string(self, drop_target: Gtk.DropTarget, value: str, x: float, y: float) -> bool:
        """Handle dropped files from string data."""
        logger.debug(f"Drop received: value={value}")

        if not isinstance(value, str):
            logger.debug("Drop rejected: non-string drag data")
            return False

        try:
            container = json.loads(value)
        except json.JSONDecodeError as exc:
            logger.debug("Drop rejected: invalid JSON drag data (%s)", exc)
            return False

        if not isinstance(container, dict):
            logger.debug("Drop rejected: drag container is not a mapping")
            return False

        if container.get("format") != "sshpilot_drag":
            logger.debug("Drop rejected: unexpected drag format")
            return False

        payload = container.get("payload")
        if not isinstance(payload, dict):
            logger.debug("Drop rejected: payload missing or invalid")
            return False

        missing_keys = [key for key in ("pane_id", "entry_name") if key not in payload]
        if missing_keys:
            logger.debug("Drop rejected: payload missing keys %s", missing_keys)
            return False

        try:
            source_pane_id = int(payload.get("pane_id"))
        except (TypeError, ValueError):
            logger.debug("Drop rejected: missing source pane id")
            return False

        position_raw = payload.get("position")
        try:
            position = int(position_raw)
        except (TypeError, ValueError):
            position = -1

        entry_name = payload.get("entry_name")
        if not isinstance(entry_name, str):
            logger.debug("Drop rejected: invalid entry name in payload")
            return False

        source_path = payload.get("path")
        if source_path is not None and not isinstance(source_path, str):
            logger.debug("Drop rejected: invalid source path in payload")
            return False

        stored_entry_path = payload.get("entry_path")
        if stored_entry_path is not None and not isinstance(stored_entry_path, str):
            stored_entry_path = None

        # Find the source pane by ID
        source_pane = None
        window = self._get_file_manager_window()
        if isinstance(window, FileManagerWindow):
            if id(window._left_pane) == source_pane_id:
                source_pane = window._left_pane
            elif id(window._right_pane) == source_pane_id:
                source_pane = window._right_pane
        
        if source_pane is None:
            logger.debug("Drop rejected: source pane not found")
            return False
            
        logger.debug(
            "Drop data: source_pane=%s, target_pane=%s, position=%s",
            source_pane._is_remote,
            self._is_remote,
            position,
        )

        # Don't allow dropping on the same pane
        if source_pane == self:
            logger.debug("Drop rejected: same pane")
            return False

        def _normalise_path(path: Optional[str]) -> Optional[str]:
            if not isinstance(path, str):
                return None
            if path == "":
                return ""
            return os.path.normpath(path)

        expected_source_path = source_path
        if expected_source_path is None and stored_entry_path:
            expected_source_path = os.path.dirname(stored_entry_path)

        current_source_path = getattr(source_pane, "_current_path", None)
        expected_source_path_norm = _normalise_path(expected_source_path)
        current_source_path_norm = _normalise_path(current_source_path)

        if (
            expected_source_path_norm is not None
            and current_source_path_norm is not None
            and expected_source_path_norm != current_source_path_norm
        ):
            logger.debug(
                "Drop rejected: source path changed (expected=%s, current=%s)",
                expected_source_path_norm,
                current_source_path_norm,
            )
            self.show_toast("Dragged item is no longer available")
            return False

        entry = next(
            (item for item in getattr(source_pane, "_entries", []) if item.name == entry_name),
            None,
        )
        if entry is None:
            logger.debug("Drop rejected: entry %s not found in source pane", entry_name)
            self.show_toast("Dragged item is no longer available")
            return False

        current_entry_path = os.path.join(current_source_path or "", entry.name)
        stored_entry_path_norm = _normalise_path(stored_entry_path)
        current_entry_path_norm = _normalise_path(current_entry_path)
        if (
            stored_entry_path_norm is not None
            and current_entry_path_norm is not None
            and stored_entry_path_norm != current_entry_path_norm
        ):
            logger.debug(
                "Drop rejected: entry path changed (expected=%s, current=%s)",
                stored_entry_path_norm,
                current_entry_path_norm,
            )
            self.show_toast("Dragged item is no longer available")
            return False

        if stored_entry_path is not None:
            source_file_path = stored_entry_path
        elif expected_source_path is not None:
            source_file_path = os.path.join(expected_source_path, entry.name)
        else:
            source_file_path = current_entry_path

        logger.debug(f"Drop operation: {entry.name} from {source_file_path}")
        
        # Determine operation type based on source and target panes
        if self._is_remote and not source_pane._is_remote:
            # Local to remote - upload
            logger.debug("Starting upload operation")
            self._handle_upload_from_drag(source_file_path, entry)
        elif not self._is_remote and source_pane._is_remote:
            # Remote to local - download
            logger.debug("Starting download operation")
            self._handle_download_from_drag(source_file_path, entry)
        else:
            # Same type of pane - not supported for now
            logger.debug("Drop rejected: same pane type")
            return False
        
        return True

    def _handle_upload_from_drag(self, source_path: str, entry: FileEntry) -> None:
        """Handle upload operation from drag and drop."""
        try:
            import pathlib
            source_path_obj = pathlib.Path(source_path)
            destination_path = posixpath.join(self._current_path, entry.name)
            
            # Get the file manager window to access the SFTP manager
            window = self._get_file_manager_window()
            if not isinstance(window, FileManagerWindow):
                self.show_toast("Upload failed: Invalid window context")
                return
                
            manager = window._manager
            
            # Check for file conflicts first
            files_to_transfer = [(str(source_path_obj), destination_path)]
            
            def _proceed_with_upload(resolved_files: List[Tuple[str, str]]) -> None:
                for local_path_str, dest_path in resolved_files:
                    path_obj = pathlib.Path(local_path_str)
                    
                    if entry.is_dir:
                        # Upload directory
                        future = manager.upload_directory(path_obj, dest_path)
                    else:
                        # Upload file
                        future = manager.upload(path_obj, dest_path)
                    
                    # Show progress dialog for upload
                    window._show_progress_dialog("upload", entry.name, future)
                    window._attach_refresh(
                        future,
                        refresh_remote=self,
                        highlight_name=entry.name,
                    )
            
            window._check_file_conflicts(files_to_transfer, "upload", _proceed_with_upload)
            
        except Exception as e:
            self.show_toast(f"Upload failed: {str(e)}")

    def _handle_download_from_drag(self, source_path: str, entry: FileEntry) -> None:
        """Handle download operation from drag and drop."""
        try:
            import pathlib
            destination_path = pathlib.Path(self._current_path) / entry.name
            
            # Get the file manager window to access the SFTP manager
            window = self._get_file_manager_window()
            if not isinstance(window, FileManagerWindow):
                self.show_toast("Download failed: Invalid window context")
                return
                
            manager = window._manager
            
            # Check for file conflicts first
            files_to_transfer = [(source_path, str(destination_path))]
            
            def _proceed_with_download(resolved_files: List[Tuple[str, str]]) -> None:
                for source, target_path_str in resolved_files:
                    target_path = pathlib.Path(target_path_str)
                    
                    if entry.is_dir:
                        # Download directory
                        future = manager.download_directory(source, target_path)
                    else:
                        # Download file
                        future = manager.download(source, target_path)
                    
                    # Show progress dialog for download
                    window._show_progress_dialog("download", entry.name, future)
                    window._attach_refresh(
                        future,
                        refresh_local_path=str(self._current_path),
                        highlight_name=entry.name,
                    )
            
            window._check_file_conflicts(files_to_transfer, "download", _proceed_with_download)
            
        except Exception as e:
            self.show_toast(f"Download failed: {str(e)}")

    def _on_drop_enter(self, drop_target: Gtk.DropTarget, x: float, y: float) -> Gdk.DragAction:
        """Called when drag enters drop target."""
        logger.debug(f"Drop enter: pane={self._is_remote}")
        # Add visual feedback - could highlight the drop area
        self.add_css_class("drop-target-active")
        return Gdk.DragAction.COPY

    def _on_drop_leave(self, drop_target: Gtk.DropTarget) -> None:
        """Called when drag leaves drop target."""
        logger.debug(f"Drop leave: pane={self._is_remote}")
        # Remove visual feedback
        self.remove_css_class("drop-target-active")

    def _on_up_clicked(self, _button) -> None:
        parent = os.path.dirname(self._current_path.rstrip('/')) or '/'
        # Avoid navigating past root repeatedly
        if parent != self._current_path:
            self.emit("path-changed", parent)

    def _on_back_clicked(self, _button) -> None:
        prev = self.pop_history()
        if prev:
            # Suppress history push for back navigation
            self._suppress_history_push = True
            self.emit("path-changed", prev)

    def _on_refresh_clicked(self, _button) -> None:
        # Refresh the current directory
        current_path = self._current_path or "/"
        self.emit("path-changed", current_path)

    def push_history(self, path: str) -> None:
        if self._history and self._history[-1] == path:
            return
        self._history.append(path)

    def pop_history(self) -> Optional[str]:
        if len(self._history) > 1:
            self._history.pop()
            return self._history[-1]
        return None

    def show_toast(self, text: str, timeout: int = -1) -> None:
        """Show a toast message safely."""
        try:
            # Dismiss any existing toast first
            if self._current_toast:
                self._current_toast.dismiss()
                self._current_toast = None
            
            toast = Adw.Toast.new(text)
            if timeout >= 0:
                toast.set_timeout(timeout)
            self._overlay.add_toast(toast)
            self._current_toast = toast  # Keep reference for dismissal
        except (AttributeError, RuntimeError, GLib.GError):
            # Overlay might be destroyed or invalid, ignore
            pass

    def dismiss_toasts(self) -> None:
        """Dismiss all toasts from the overlay."""
        try:
            # Dismiss the current toast if it exists
            if self._current_toast:
                self._current_toast.dismiss()
                self._current_toast = None
        except (AttributeError, RuntimeError, GLib.GError):
            # Overlay might be destroyed or invalid, ignore
            pass

    # -- type-ahead search ----------------------------------------------

    def _current_time(self) -> float:
        getter = getattr(GLib, "get_monotonic_time", None)
        if callable(getter):
            try:
                return getter() / 1_000_000
            except Exception:
                pass
        return time.monotonic()

    def _find_prefix_match(self, prefix: str, start_index: int) -> Optional[int]:
        if not prefix or not self._entries:
            return None

        total = len(self._entries)
        if total <= 0:
            return None

        start = 0 if start_index is None else start_index
        if start < 0:
            start = 0

        prefix_casefold = prefix.casefold()
        for offset in range(total):
            index = (start + offset) % total
            if self._entries[index].name.casefold().startswith(prefix_casefold):
                return index
        return None

    def _scroll_to_position(self, position: int) -> None:
        visible = self._stack.get_visible_child_name()
        view: Optional[Gtk.Widget] = None
        if visible == "list":
            view = self._list_view
        elif visible == "grid":
            view = self._grid_view

        if view is None:
            return

        scroll_to = getattr(view, "scroll_to", None)
        if callable(scroll_to):
            flags = getattr(Gtk, "ListScrollFlags", None)
            focus_flag = getattr(flags, "FOCUS", 1) if flags is not None else 1
            try:
                scroll_to(position, focus_flag)
            except Exception:
                pass

    def _on_typeahead_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        if not self._entries:
            return False

        if state & (
            Gdk.ModifierType.CONTROL_MASK
            | Gdk.ModifierType.ALT_MASK
            | getattr(Gdk.ModifierType, "ALT_MASK", 0)
            | getattr(Gdk.ModifierType, "SUPER_MASK", 0)
        ):
            return False

        char_code = Gdk.keyval_to_unicode(keyval)
        if not char_code:
            return False

        char = chr(char_code)
        if not char or not char.isprintable():
            return False

        now = self._current_time()
        if now - self._typeahead_last_time > self._TYPEAHEAD_TIMEOUT:
            self._typeahead_buffer = ""

        self._typeahead_last_time = now

        repeat_cycle = (
            bool(self._typeahead_buffer)
            and len(self._typeahead_buffer) == 1
            and char.casefold() == self._typeahead_buffer.casefold()
        )

        selected = self._get_primary_selection_index()
        if selected is None or selected < 0:
            selected_index = 0
        else:
            selected_index = selected

        start_index = selected_index
        match: Optional[int] = None
        prefix: Optional[str] = None

        if repeat_cycle:
            candidate = self._typeahead_buffer + char
            match = self._find_prefix_match(candidate, start_index)
            if match is not None:
                self._typeahead_buffer = candidate
            else:
                start_index += 1
                prefix = self._typeahead_buffer
        else:
            self._typeahead_buffer += char
            prefix = self._typeahead_buffer

        if match is None and prefix is not None:
            match = self._find_prefix_match(prefix, start_index)

        if match is None and not repeat_cycle:
            self._typeahead_buffer = char
            match = self._find_prefix_match(self._typeahead_buffer, selected_index)

        if match is None:
            return False

        setter = getattr(self._selection_model, "select_item", None)
        if callable(setter):
            setter(match, True)
        else:
            fallback = getattr(self._selection_model, "set_selected", None)
            if callable(fallback):
                fallback(match)

        self._scroll_to_position(match)
        return True

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

        self._manager = AsyncSFTPManager(
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
            print(f"Error connecting signals: {exc}")
        
        # Connect close-request and destroy handlers to clean up resources
        self.connect("close-request", self._on_close_request)
        self.connect("destroy", self._on_destroy)
        
        # Show initial progress before connecting
        try:
            self._show_progress(0.1, "Connecting…")
        except Exception as exc:
            print(f"Error showing progress: {exc}")
        
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
            print(f"Error connecting to server: {exc}")
    
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
        
        # Add password entry
        password_entry = Gtk.PasswordEntry()
        password_entry.set_property("placeholder-text", "Password")
        password_entry.set_margin_top(12)
        password_entry.set_margin_bottom(12)
        password_entry.set_margin_start(12)
        password_entry.set_margin_end(12)
        
        # Add entry to dialog's extra child area
        dialog.set_extra_child(password_entry)
        
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
                
                # Add password entry
                password_entry = Gtk.PasswordEntry()
                password_entry.set_property("placeholder-text", "Password")
                password_entry.set_margin_top(12)
                password_entry.set_margin_bottom(12)
                password_entry.set_margin_start(12)
                password_entry.set_margin_end(12)
                
                # Add entry to dialog's extra child area
                dialog.set_extra_child(password_entry)
                
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
                    # Get password before destroying dialog
                    entered_password = password_entry.get_text() if response == "connect" else None
                    
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
                                # Store password in connection manager if available
                                if self._connection_manager is not None:
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
                                        
                                        # Optionally save password (user can choose in future)
                                        # For now, just use it for this connection
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
        print(f"=== CHECKING FILE CONFLICTS ===")
        print(f"Operation type: {operation_type}")
        print(f"Files to transfer: {files_to_transfer}")

        def _finalize_conflicts(conflicts: List[Tuple[str, str]]) -> None:
            print(f"Total conflicts found: {len(conflicts)}")

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
                
                print("No conflicts, proceeding with transfers")
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
                print(f"Checking: {source} -> {dest}")
                exists = os.path.exists(dest)
                print(f"  Local file exists: {exists}")
                if exists:
                    conflicts.append((source, dest))
                    print(f"  CONFLICT DETECTED: {dest}")

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
                print(f"Checking: {source} -> {dest}")

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

                    print(f"  Remote file exists: {exists}")
                    if exists:
                        conflicts.append(pair)
                        print(f"  CONFLICT DETECTED: {pair[1]}")

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
                # Remote to local clipboard operation not supported
                pane.show_toast("Remote to local clipboard operation not supported")
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
                logger.info(f"_proceed_with_upload: Starting upload of {total_files} files")
                
                for local_path_str, destination in resolved_files:
                    path_obj = pathlib.Path(local_path_str)

                    try:
                        logger.debug(f"_proceed_with_upload: Starting upload of {path_obj.name}")
                        if path_obj.is_dir():
                            future = self._manager.upload_directory(path_obj, destination)
                        else:
                            future = self._manager.upload(path_obj, destination)

                        # Show progress dialog for upload (pass total_files for multi-file support)
                        self._show_progress_dialog("upload", path_obj.name, future, total_files=total_files)
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
            print(f"=== DOWNLOAD OPERATION CALLED ===")
            print(f"Payload: {payload}")

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
            print(f"Entries to download: {[e.name for e in entries]}")
            print(f"Directory: {directory}")
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
                        self._show_progress_dialog("download", entry_name, future, total_files=total_files)
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
            socket_closed_error = False
            try:
                completed.result()
                operation_succeeded = True
                logger.debug("_attach_refresh: operation completed successfully")
            except Exception as e:
                error_str = str(e).lower()
                # Check if it's a socket/connection closed error - upload might still have succeeded
                if "socket is closed" in error_str or "connection" in error_str and "closed" in error_str:
                    socket_closed_error = True
                    logger.debug(f"_attach_refresh: operation completed but socket closed: {e}")
                    # Still try to refresh - the upload might have succeeded before socket closed
                    operation_succeeded = True
                else:
                    logger.debug(f"_attach_refresh: operation failed with {e}")
                    # For other errors, don't refresh
                    return
            
            # Only refresh if operation succeeded (or socket closed, which might mean success)
            if operation_succeeded:
                if highlight_name:
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

    def _show_progress_dialog(self, operation_type: str, filename: str, future: Future, total_files: int = 1) -> None:
        """Show and manage the progress dialog for a file operation."""
        try:
            print(f"DEBUG: _show_progress_dialog called for {operation_type} {filename}")
            
            # Check if we can reuse an existing dialog for the same operation type
            reuse_dialog = False
            if (hasattr(self, '_progress_dialog') and self._progress_dialog and 
                self._progress_dialog.operation_type == operation_type and
                not self._progress_dialog.is_cancelled):
                reuse_dialog = True
                print(f"DEBUG: Reusing existing progress dialog for {operation_type}")
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
                print(f"DEBUG: Creating progress dialog")
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
                self._progress_dialog.present()
                print(f"DEBUG: Progress dialog created and shown successfully")
            
            # Add future to dialog (will update total_files if needed)
            self._progress_dialog.set_operation_details(total_files=total_files, filename=filename)
            self._progress_dialog.set_future(future)
            
            # Try to get file size for better progress display (only for new dialogs)
            if not reuse_dialog:
                try:
                    if operation_type == "download":
                        # For downloads, we'll get the size from the SFTP manager if available
                        # This is a rough estimate, the actual implementation would need to
                        # query the remote file size
                        self._progress_dialog.total_bytes = 1024 * 1024  # Default to 1MB estimate
                    else:  # upload
                        # For uploads, we could get the local file size
                        self._progress_dialog.total_bytes = 1024 * 1024  # Default to 1MB estimate
                except Exception:
                    self._progress_dialog.total_bytes = 0
            
        except Exception as exc:
            print(f"DEBUG: Error in _show_progress_dialog: {exc}")
            import traceback
            traceback.print_exc()
            return
        
        # Only connect progress signal handler when creating a new dialog
        # When reusing, the handler is already connected
        if not reuse_dialog:
            # Store references for cleanup
            self._progress_handler_id = None
            self._active_futures = []  # Track all active futures for multi-file transfers
            self._future_to_filename = {}  # Map futures to filenames for progress tracking
            
            # Connect progress signal
            def _on_progress(manager, progress: float, message: str) -> None:
                # Check if dialog exists and operation hasn't been cancelled
                # Accept progress from any active future
                if (self._progress_dialog and 
                    not self._progress_dialog.is_cancelled and
                    hasattr(self, '_active_futures') and self._active_futures):
                    # Check if any active future is still running
                    active_count = sum(1 for f in self._active_futures 
                                     if f and not f.done() and not f.cancelled())
                    if active_count > 0:
                        try:
                            # For multi-file operations, calculate overall progress
                            if self._progress_dialog.total_files > 1:
                                # Calculate overall progress: (completed_files + current_file_progress) / total_files
                                completed = self._progress_dialog.files_completed
                                total = self._progress_dialog.total_files
                                
                                # Calculate overall progress
                                # Each file contributes 1/total to the overall progress
                                # Completed files contribute: completed / total
                                # Current file contributes: progress / total
                                # Total: (completed + progress) / total
                                overall_progress = (completed + progress) / total
                                
                                # Clamp to ensure we never show 100% until all files are done
                                # When there are active files, the maximum progress is:
                                # (total - active_count) completed files + 1.0 for the current file
                                # = (total - active_count + 1.0) / total
                                # But we should cap at (total - 1) / total to be safe (when 1 file is active)
                                if active_count > 0 and completed < total:
                                    # Maximum is when all but one file is completed and current is at 100%
                                    max_progress = (total - 1) / total
                                    overall_progress = min(overall_progress, max_progress)
                                
                                self._progress_dialog.update_progress(overall_progress, message)
                            else:
                                # Single file - use progress directly
                                self._progress_dialog.update_progress(progress, message)
                        except (AttributeError, RuntimeError, GLib.GError):
                            # Dialog may have been destroyed
                            pass
            
            # Connect progress signal and store handler ID
            self._progress_handler_id = self._manager.connect("progress", _on_progress)
        
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
                
                # Only disconnect progress signal if all futures are done
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

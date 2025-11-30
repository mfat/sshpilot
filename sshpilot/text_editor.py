"""Text editor window for editing remote and local files.

This module provides a text editor window that supports both remote (SFTP) and
local file editing with syntax highlighting, search/replace, and undo/redo
functionality.
"""

from __future__ import annotations

import os
import pathlib
import tempfile
from concurrent.futures import Future
from typing import TYPE_CHECKING, Optional

from gi.repository import Adw, Gio, GLib, GObject, Gdk, Gtk

# Try to import GtkSourceView for syntax highlighting
try:
    import gi
    gi.require_version('GtkSource', '5')
    from gi.repository import GtkSource
    _HAS_GTKSOURCE = True
except (ImportError, ValueError, AttributeError):
    _HAS_GTKSOURCE = False
    GtkSource = None

import logging

if TYPE_CHECKING:
    from .file_manager_window import AsyncSFTPManager, FileManagerWindow

logger = logging.getLogger(__name__)


class RemoteFileEditorWindow(Adw.Window):
    """FileZilla-style text editor window using GTK SourceView.
    
    Supports both remote (SFTP) and local file editing:
    - Remote: Downloads to temp location, edits, uploads back when saved
    - Local: Edits file directly, saves locally
    """
    
    def __init__(
        self,
        parent: Adw.Window,
        file_path: str,
        file_name: str,
        is_local: bool = False,
        sftp_manager: Optional["AsyncSFTPManager"] = None,
        file_manager_window: Optional["FileManagerWindow"] = None,
    ) -> None:
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(False)  # Allow multiple editors
        self.set_default_size(900, 600)
        self.set_title(f"Edit {file_name}")
        
        self._is_local = is_local
        self._file_path = file_path  # Can be remote path or local path
        self._file_name = file_name
        self._sftp_manager = sftp_manager
        self._file_manager_window = file_manager_window
        self._temp_file: Optional[pathlib.Path] = None
        self._file_monitor: Optional[Gio.FileMonitor] = None
        self._file_modified_time: float = 0.0
        self._has_unsaved_changes = False
        self._upload_dialog: Optional[Adw.AlertDialog] = None
        self._is_closing = False
        self._is_loading = True  # Flag to track initial file loading
        
        # Search/Replace state
        self._search_entry: Optional[Gtk.Entry] = None
        self._replace_entry: Optional[Gtk.Entry] = None
        self._search_settings: Optional[GtkSource.SearchSettings] = None
        self._search_context: Optional[GtkSource.SearchContext] = None
        
        if self._is_local:
            # For local files, use the file path directly
            self._temp_file = pathlib.Path(file_path)
        else:
            # For remote files, create temp file using Python's tempfile module
            # Use NamedTemporaryFile with delete=False for manual cleanup
            # This is the recommended pattern per Python docs for persistent temp files
            temp_file_obj = tempfile.NamedTemporaryFile(
                mode='w+b',
                prefix=f"sshpilot_edit_{os.getpid()}_",
                suffix=f"_{file_name}",
                delete=False  # Don't auto-delete, we'll clean up manually in _do_close()
            )
            temp_file_obj.close()  # Close the file handle, we'll open it later when needed
            self._temp_file = pathlib.Path(temp_file_obj.name)
        
        # Create UI
        self._setup_ui()
        
        # Load file (download if remote, direct load if local)
        if self._is_local:
            self._load_file_content()
        else:
            self._download_and_load()
    
    def _setup_ui(self) -> None:
        """Set up the editor UI."""
        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)
        
        # Header bar
        header_bar = Adw.HeaderBar()
        self._title_label = Gtk.Label(label=f"Edit {self._file_name}")
        header_bar.set_title_widget(self._title_label)
        
        # Save button - label depends on local vs remote
        save_label = "Save" if self._is_local else "Save"
        self._save_button = Gtk.Button(label=save_label)
        self._save_button.add_css_class("suggested-action")
        self._save_button.set_sensitive(False)
        self._save_button.connect("clicked", self._on_save_clicked)
        header_bar.pack_end(self._save_button)
        
        # Undo/Redo buttons
        self._undo_button = Gtk.Button.new_from_icon_name("edit-undo-symbolic")
        self._undo_button.set_tooltip_text("Undo")
        self._undo_button.set_sensitive(False)
        self._undo_button.connect("clicked", self._on_undo_clicked)
        header_bar.pack_start(self._undo_button)
        
        self._redo_button = Gtk.Button.new_from_icon_name("edit-redo-symbolic")
        self._redo_button.set_tooltip_text("Redo")
        self._redo_button.set_sensitive(False)
        self._redo_button.connect("clicked", self._on_redo_clicked)
        header_bar.pack_start(self._redo_button)
        
        # Search button to toggle search bar
        self._search_button = Gtk.Button.new_from_icon_name("system-search-symbolic")
        self._search_button.set_tooltip_text("Search")
        self._search_button.connect("clicked", self._on_search_button_clicked)
        header_bar.pack_start(self._search_button)
        
        # Cancel/Close button
        cancel_button = Gtk.Button(label="Close")
        cancel_button.connect("clicked", self._on_close_clicked)
        header_bar.pack_start(cancel_button)
        
        toolbar_view.add_top_bar(header_bar)
        
        # Toolbar for search/replace
        self._search_toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar = self._search_toolbar
        toolbar.set_margin_start(6)
        toolbar.set_margin_end(6)
        toolbar.set_margin_top(6)
        toolbar.set_margin_bottom(6)
        
        # Search section
        search_label = Gtk.Label(label="Search:")
        self._search_entry = Gtk.Entry()
        self._search_entry.set_placeholder_text("Search...")
        self._search_entry.set_width_chars(20)
        
        search_prev_btn = Gtk.Button(label="Prev")
        search_prev_btn.connect("clicked", self._on_search_prev_clicked)
        
        search_next_btn = Gtk.Button(label="Next")
        search_next_btn.connect("clicked", self._on_search_next_clicked)
        
        # Replace section
        replace_label = Gtk.Label(label="Replace:")
        self._replace_entry = Gtk.Entry()
        self._replace_entry.set_placeholder_text("Replace with...")
        self._replace_entry.set_width_chars(20)
        
        replace_btn = Gtk.Button(label="Replace")
        replace_btn.connect("clicked", self._on_replace_clicked)
        
        replace_all_btn = Gtk.Button(label="Replace All")
        replace_all_btn.connect("clicked", self._on_replace_all_clicked)
        
        # Pack toolbar
        toolbar.append(search_label)
        toolbar.append(self._search_entry)
        toolbar.append(search_prev_btn)
        toolbar.append(search_next_btn)
        toolbar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        toolbar.append(replace_label)
        toolbar.append(self._replace_entry)
        toolbar.append(replace_btn)
        toolbar.append(replace_all_btn)
        
        # Connect search entry signals
        self._search_entry.connect("changed", self._on_search_changed)
        self._search_entry.connect("activate", self._on_search_activate)
        
        # Editor area
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        
        # Create editor widget (SourceView if available, otherwise TextView)
        if _HAS_GTKSOURCE:
            self._source_view = GtkSource.View()
            self._source_view.set_show_line_numbers(True)
            self._source_view.set_highlight_current_line(False)  # Disable to only highlight search string, not entire line
            self._source_view.set_auto_indent(True)
            self._source_view.set_indent_width(4)
            self._source_view.set_tab_width(4)
            self._source_view.set_insert_spaces_instead_of_tabs(False)
            self._source_view.set_monospace(True)  # Ensure monospace font
            self._source_view.set_wrap_mode(Gtk.WrapMode.WORD)  # Enable word wrap
            
            # Detect language from file extension
            language_manager = GtkSource.LanguageManager.get_default()
            _, ext = os.path.splitext(self._file_name)
            language = language_manager.guess_language(self._file_name, None)
            if language:
                self._buffer = GtkSource.Buffer.new_with_language(language)
                self._source_view.set_buffer(self._buffer)
            else:
                self._buffer = GtkSource.Buffer()
                self._source_view.set_buffer(self._buffer)
            
            # Set up search context for SourceView
            self._search_settings = GtkSource.SearchSettings()
            self._search_context = GtkSource.SearchContext.new(self._buffer, self._search_settings)
            self._search_context.set_highlight(True)
        else:
            # Fallback to regular TextView
            self._source_view = Gtk.TextView()
            self._source_view.set_monospace(True)
            self._source_view.set_wrap_mode(Gtk.WrapMode.WORD)  # Enable word wrap
            self._buffer = Gtk.TextBuffer()
            self._source_view.set_buffer(self._buffer)
            self._search_settings = None
            self._search_context = None
        
        # Connect to buffer changes
        self._buffer.connect("modified-changed", self._on_buffer_modified_changed)
        
        # Connect undo/redo state changes if using GtkSource
        if _HAS_GTKSOURCE and isinstance(self._buffer, GtkSource.Buffer):
            self._buffer.connect("notify::can-undo", self._on_undo_state_changed)
            self._buffer.connect("notify::can-redo", self._on_redo_state_changed)
        
        # Create a vertical box to hold toolbar and scrolled window
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content_box.append(toolbar)
        content_box.append(scrolled)
        
        # Hide search toolbar by default
        self._search_toolbar.set_visible(False)
        
        scrolled.set_child(self._source_view)
        
        # Wrap editor in toast overlay for status messages (same style as file manager)
        toast_overlay = Adw.ToastOverlay()
        toast_overlay.set_child(content_box)
        self._toast_overlay = toast_overlay
        self._current_toast = None  # Keep reference for dismissal
        
        toolbar_view.set_content(toast_overlay)
        
        # Add keyboard controller for shortcuts
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_controller)
        
        # Apply same toast CSS styling as file manager
        self._apply_toast_css()
        
        # Connect close request
        self.connect("close-request", self._on_close_request)
        
        # Show initial loading toast
        self._show_toast("Loading…" if self._is_local else "Downloading…", timeout=2)
    
    def _apply_toast_css(self) -> None:
        """Apply the same toast CSS styling as file manager."""
        try:
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
            """
            css_provider.load_from_data(toast_css.encode())
            display = Gdk.Display.get_default()
            if display:
                Gtk.StyleContext.add_provider_for_display(
                    display, css_provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
                )
        except Exception as e:
            logger.debug(f"Failed to apply toast CSS: {e}")
    
    def _show_toast(self, text: str, timeout: int = 3) -> None:
        """Show a toast message safely (same style as FilePane)."""
        try:
            # Dismiss any existing toast first
            if self._current_toast:
                self._current_toast.dismiss()
                self._current_toast = None
            
            toast = Adw.Toast.new(text)
            if timeout >= 0:
                toast.set_timeout(timeout)
            self._toast_overlay.add_toast(toast)
            self._current_toast = toast  # Keep reference for dismissal
        except (AttributeError, RuntimeError, GLib.GError):
            # Overlay might be destroyed or invalid, ignore
            pass
    
    def _download_and_load(self) -> None:
        """Download the remote file and load it into the editor."""
        def download_complete(future: Future) -> None:
            try:
                future.result()  # Wait for download to complete
                GLib.idle_add(self._load_file_content)
            except Exception as e:
                logger.error(f"Failed to download file for editing: {e}", exc_info=True)
                GLib.idle_add(self._show_error, f"Failed to download file: {e}")
        
        # Download file (use _file_path which contains the remote path for remote files)
        future = self._sftp_manager.download(self._file_path, self._temp_file)
        future.add_done_callback(download_complete)
    
    def _load_file_content(self) -> None:
        """Load the file content into the editor (downloaded for remote, direct for local)."""
        try:
            if not self._temp_file.exists():
                error_msg = "File not found" if self._is_local else "Downloaded file not found"
                self._show_error(error_msg)
                return
            
            # Read file content
            with open(self._temp_file, 'rb') as f:
                content = f.read()
            
            # Try to decode as UTF-8, fall back to latin-1 if needed
            try:
                text = content.decode('utf-8')
            except UnicodeDecodeError:
                try:
                    text = content.decode('latin-1')
                except UnicodeDecodeError:
                    text = content.decode('utf-8', errors='replace')
            
            # Set content in buffer
            self._buffer.set_text(text)
            self._buffer.set_modified(False)
            
            # Reset undo/redo state after loading
            if _HAS_GTKSOURCE and isinstance(self._buffer, GtkSource.Buffer):
                try:
                    if hasattr(self._buffer, 'begin_not_undoable_action'):
                        self._buffer.begin_not_undoable_action()
                        self._buffer.end_not_undoable_action()
                except (AttributeError, TypeError):
                    pass
                # Update undo/redo button states
                if hasattr(self, '_undo_button'):
                    self._undo_button.set_sensitive(False)
                if hasattr(self, '_redo_button'):
                    self._redo_button.set_sensitive(False)
            
            # Get initial modification time
            self._file_modified_time = self._temp_file.stat().st_mtime
            
            # Set up file monitoring
            self._setup_file_monitoring()
            
            # Mark loading as complete - now modification events will be handled
            self._is_loading = False
            
        except Exception as e:
            logger.error(f"Failed to load file content: {e}", exc_info=True)
            self._show_error(f"Failed to load file: {e}")
            # Ensure loading flag is reset even on error
            self._is_loading = False
    
    def _setup_file_monitoring(self) -> None:
        """Set up file monitoring to detect external saves."""
        try:
            gfile = Gio.File.new_for_path(str(self._temp_file))
            self._file_monitor = gfile.monitor_file(Gio.FileMonitorFlags.WATCH_MOVES, None)
            self._file_monitor.connect("changed", self._on_file_changed)
        except Exception as e:
            logger.warning(f"Failed to set up file monitoring: {e}")
    
    def _on_file_changed(
        self,
        monitor: Gio.FileMonitor,
        file: Gio.File,
        other_file: Optional[Gio.File],
        event_type: Gio.FileMonitorEvent,
    ) -> None:
        """Handle file system changes to the temp file."""
        if self._is_closing:
            return
        
        # Only care about changes (not moves/deletes)
        if event_type in (Gio.FileMonitorEvent.CHANGED, Gio.FileMonitorEvent.CHANGES_DONE_HINT):
            try:
                if self._temp_file.exists():
                    new_mtime = self._temp_file.stat().st_mtime
                    if new_mtime > self._file_modified_time:
                        self._file_modified_time = new_mtime
                        GLib.idle_add(self._on_file_saved_externally)
            except Exception:
                pass
    
    def _update_title(self, modified: bool = None) -> None:
        """Update the headerbar title to show modified state."""
        if modified is None:
            modified = self._buffer.get_modified() if self._buffer else False
        
        if modified:
            self._title_label.set_label(f"* (modified) Edit {self._file_name}")
        else:
            self._title_label.set_label(f"Edit {self._file_name}")
    
    def _on_file_saved_externally(self) -> None:
        """Handle when the file is saved externally (from the editor)."""
        self._has_unsaved_changes = True
        self._save_button.set_sensitive(True)
        self._update_title(True)
    
    def _on_buffer_modified_changed(self, buffer: Gtk.TextBuffer) -> None:
        """Handle buffer modification state changes."""
        # Ignore modification events during initial file loading
        if self._is_loading:
            return
        
        modified = buffer.get_modified()
        if modified:
            if not self._has_unsaved_changes:
                self._has_unsaved_changes = True
                self._save_button.set_sensitive(True)
            self._update_title(True)
        else:
            # Buffer is no longer modified
            self._update_title(False)
    
    def _on_undo_state_changed(self, buffer: GtkSource.Buffer, pspec: GObject.ParamSpec) -> None:
        """Handle undo state changes."""
        if hasattr(self, '_undo_button'):
            try:
                if isinstance(buffer, GtkSource.Buffer) and hasattr(buffer, 'can_undo'):
                    self._undo_button.set_sensitive(buffer.can_undo())
            except (AttributeError, TypeError):
                pass
    
    def _on_redo_state_changed(self, buffer: GtkSource.Buffer, pspec: GObject.ParamSpec) -> None:
        """Handle redo state changes."""
        if hasattr(self, '_redo_button'):
            try:
                if isinstance(buffer, GtkSource.Buffer) and hasattr(buffer, 'can_redo'):
                    self._redo_button.set_sensitive(buffer.can_redo())
            except (AttributeError, TypeError):
                pass
    
    def _on_save_clicked(self, _button: Gtk.Button) -> None:
        """Handle save button click - save buffer content locally, upload if remote."""
        if not self._temp_file:
            self._show_error("File not found")
            return
        
        # Always save buffer content to file
        start, end = self._buffer.get_bounds()
        text = self._buffer.get_text(start, end, False)
        try:
            with open(self._temp_file, 'w', encoding='utf-8') as f:
                f.write(text)
            self._buffer.set_modified(False)
            
            # Reset undo stack after save
            if _HAS_GTKSOURCE and isinstance(self._buffer, GtkSource.Buffer):
                try:
                    if hasattr(self._buffer, 'begin_not_undoable_action'):
                        self._buffer.begin_not_undoable_action()
                        self._buffer.end_not_undoable_action()
                except (AttributeError, TypeError):
                    pass
                # Update undo/redo button states
                if hasattr(self, '_undo_button'):
                    self._undo_button.set_sensitive(False)
                if hasattr(self, '_redo_button'):
                    self._redo_button.set_sensitive(False)
            self._file_modified_time = self._temp_file.stat().st_mtime
            self._has_unsaved_changes = False
            self._update_title(False)
            
            if self._is_local:
                # For local files, just save and refresh the pane
                self._show_toast("Saved", timeout=2)
                self._save_button.set_sensitive(False)
                # Refresh the file manager to show updated file
                if self._file_manager_window:
                    pane = self._file_manager_window._left_pane
                    if pane:
                        GLib.idle_add(lambda: pane.emit("path-changed", pane._current_path))
                return
        except Exception as e:
            self._show_error(f"Failed to save file: {e}")
            return
        
        # For remote files, upload after saving
        if not self._is_local:
            self._upload_file()
    
    def _upload_file(self) -> None:
        """Upload the modified file back to the remote server."""
        self._show_toast("Uploading…", timeout=-1)  # Show until upload completes
        self._save_button.set_sensitive(False)
        
        def upload_complete(future: Future) -> None:
            try:
                future.result()
                GLib.idle_add(self._on_upload_success)
            except Exception as e:
                logger.error(f"Failed to upload file: {e}", exc_info=True)
                GLib.idle_add(self._on_upload_error, str(e))
        
        future = self._sftp_manager.upload(self._temp_file, self._file_path)
        future.add_done_callback(upload_complete)
    
    def _on_upload_success(self) -> None:
        """Handle successful upload."""
        self._has_unsaved_changes = False
        self._save_button.set_sensitive(False)
        self._update_title(False)
        self._show_toast("Uploaded successfully", timeout=2)
        
        # Reset buffer modified flag since changes have been saved
        self._buffer.set_modified(False)
        
        # Reset undo stack after save
        if _HAS_GTKSOURCE and isinstance(self._buffer, GtkSource.Buffer):
            self._buffer.begin_not_undoable_action()
            self._buffer.end_not_undoable_action()
            # Update undo/redo button states
            if hasattr(self, '_undo_button'):
                self._undo_button.set_sensitive(False)
            if hasattr(self, '_redo_button'):
                self._redo_button.set_sensitive(False)
        
        # Refresh the file manager to show updated file
        if self._file_manager_window:
            pane = self._file_manager_window._right_pane
            if pane:
                pane.emit("path-changed", pane._current_path)
    
    def _on_upload_error(self, error: str) -> None:
        """Handle upload error."""
        self._show_toast(f"Upload failed: {error}", timeout=4)
        self._save_button.set_sensitive(True)
        self._show_error(f"Failed to upload file: {error}")
    
    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        """Handle close button click."""
        self._check_and_close()
    
    def _on_close_request(self, _window: Adw.Window) -> bool:
        """Handle window close request."""
        self._check_and_close()
        return True  # Prevent default close
    
    def _check_and_close(self) -> None:
        """Check for unsaved changes and close if okay."""
        # Only check if the buffer has been modified (user made actual changes)
        has_changes = self._buffer.get_modified()
        
        if has_changes:
            # Show confirmation dialog - text differs for local vs remote
            if self._is_local:
                dialog_text = f"You have unsaved changes to {self._file_name}. Save changes before closing?"
                save_label = "Save"
            else:
                dialog_text = f"You have unsaved changes to {self._file_name}. Upload changes before closing?"
                save_label = "Save & Upload"
            
            dialog = Adw.AlertDialog.new(
                "Unsaved Changes",
                dialog_text
            )
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("discard", "Discard Changes")
            dialog.add_response("save", save_label)
            dialog.set_default_response("save")
            dialog.set_close_response("cancel")
            
            def on_response(_dialog: Adw.AlertDialog, response: str) -> None:
                if response == "save":
                    self._on_save_clicked(None)
                    # Wait a bit then close
                    GLib.timeout_add(500, self._do_close)
                elif response == "discard":
                    self._do_close()
            
            dialog.connect("response", on_response)
            dialog.present(self)
        else:
            self._do_close()
    
    def _do_close(self) -> None:
        """Actually close the window and clean up."""
        self._is_closing = True
        
        # Stop monitoring
        if self._file_monitor:
            self._file_monitor.cancel()
            self._file_monitor = None
        
        # Clean up temp file (only for remote files - local files shouldn't be deleted)
        if not self._is_local and self._temp_file and self._temp_file.exists():
            try:
                self._temp_file.unlink()
            except Exception:
                pass
        
        self.destroy()
    
    def _show_error(self, message: str) -> None:
        """Show an error dialog."""
        dialog = Adw.AlertDialog.new("Error", message)
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present(self)
    
    # ---------- Search/Replace methods ----------
    
    def _update_search_settings(self) -> None:
        """Update search settings from search entry."""
        logger.debug("_update_search_settings: called")
        
        if not _HAS_GTKSOURCE:
            logger.debug("_update_search_settings: _HAS_GTKSOURCE is False")
            return
        
        if not self._search_settings:
            logger.debug("_update_search_settings: _search_settings is None")
            return
        
        if not self._search_entry:
            logger.debug("_update_search_settings: _search_entry is None")
            return
        
        text = self._search_entry.get_text()
        logger.debug(f"_update_search_settings: text='{text}'")
        
        if text:
            self._search_settings.set_search_text(text)
            logger.debug(f"_update_search_settings: set_search_text('{text}')")
        else:
            self._search_settings.set_search_text(None)
            logger.debug("_update_search_settings: set_search_text(None)")
        
        # Configure search behavior
        self._search_settings.set_case_sensitive(False)
        self._search_settings.set_wrap_around(True)
        logger.debug("_update_search_settings: configured case_sensitive=False, wrap_around=True")
    
    def _search_next(self) -> None:
        """Search for next occurrence."""
        logger.debug("_search_next: called")
        
        if not _HAS_GTKSOURCE:
            logger.debug("_search_next: _HAS_GTKSOURCE is False")
            return
        
        if not self._search_context:
            logger.debug("_search_next: _search_context is None")
            return
        
        if not self._search_entry:
            logger.debug("_search_next: _search_entry is None")
            return
        
        # Ensure search settings are up to date
        self._update_search_settings()
        
        # Check search text
        search_text = self._search_entry.get_text() if self._search_entry else None
        logger.debug(f"_search_next: search_text='{search_text}'")
        
        if not search_text:
            logger.debug("_search_next: no search text, returning")
            return
        
        # Check search settings
        if self._search_settings:
            settings_text = self._search_settings.get_search_text()
            logger.debug(f"_search_next: search_settings.search_text='{settings_text}'")
        
        # If there's a selection, start from just after the end of the selection
        # Otherwise, start from the insert mark
        try:
            bounds_result = self._buffer.get_selection_bounds()
            logger.debug(f"_search_next: get_selection_bounds() returned: {bounds_result}, len={len(bounds_result) if hasattr(bounds_result, '__len__') else 'N/A'}")
            
            # get_selection_bounds() returns (bool, start_iter, end_iter) or just bool
            if isinstance(bounds_result, tuple) and len(bounds_result) >= 1:
                has_selection = bounds_result[0]
                if has_selection and len(bounds_result) >= 3:
                    start = bounds_result[1]
                    end = bounds_result[2]
                else:
                    start = None
                    end = None
            else:
                has_selection = bool(bounds_result) if bounds_result else False
                start = None
                end = None
            
            logger.debug(f"_search_next: has_selection={has_selection}, start={start}, end={end}")
            
            if has_selection and start is not None and end is not None:
                # Start searching from just after the end of the current selection
                iter_ = end.copy()
                start_offset = iter_.get_offset()
                # Advance by one character to skip the current match
                if not iter_.is_end():
                    iter_.forward_char()
                end_offset = iter_.get_offset()
                logger.debug(f"_search_next: using selection end, start_offset={start_offset}, end_offset={end_offset}")
            else:
                # No selection, start from insert mark (cursor position)
                insert_mark = self._buffer.get_insert()
                iter_ = self._buffer.get_iter_at_mark(insert_mark)
                offset = iter_.get_offset()
                logger.debug(f"_search_next: using cursor position, offset={offset}")
        except (ValueError, TypeError) as e:
            # Fallback to insert mark
            logger.debug(f"_search_next: exception getting selection bounds: {e}")
            insert_mark = self._buffer.get_insert()
            iter_ = self._buffer.get_iter_at_mark(insert_mark)
            offset = iter_.get_offset()
            logger.debug(f"_search_next: fallback to cursor, offset={offset}")
        
        # Log iterator position before search
        iter_offset = iter_.get_offset()
        buffer_size = self._buffer.get_char_count()
        logger.debug(f"_search_next: calling forward() with iter_offset={iter_offset}, buffer_size={buffer_size}")
        
        ok, match_start, match_end, wrapped = self._search_context.forward(iter_)
        logger.debug(f"_search_next: forward() returned ok={ok}, wrapped={wrapped}")
        
        if ok:
            match_start_offset = match_start.get_offset()
            match_end_offset = match_end.get_offset()
            logger.debug(f"_search_next: match found at start_offset={match_start_offset}, end_offset={match_end_offset}")
            self._buffer.select_range(match_start, match_end)
            # Move cursor to the end of the match so next search starts from after it
            insert_mark = self._buffer.get_insert()
            self._buffer.move_mark(insert_mark, match_end)
            logger.debug(f"_search_next: moved cursor to end of match at offset={match_end_offset}")
            self._source_view.scroll_to_iter(match_start, 0.1, True, 0.0, 0.0)
        else:
            logger.debug("_search_next: no match found")
    
    def _search_prev(self) -> None:
        """Search for previous occurrence."""
        if not _HAS_GTKSOURCE or not self._search_context:
            return
        
        insert_mark = self._buffer.get_insert()
        iter_ = self._buffer.get_iter_at_mark(insert_mark)
        
        ok, match_start, match_end, wrapped = self._search_context.backward(iter_)
        if ok:
            self._buffer.select_range(match_start, match_end)
            self._source_view.scroll_to_iter(match_start, 0.1, True, 0.0, 0.0)
    
    def _on_search_button_clicked(self, button: Gtk.Button) -> None:
        """Handle search button click - toggle search bar visibility."""
        visible = self._search_toolbar.get_visible()
        self._search_toolbar.set_visible(not visible)
        if not visible and self._search_entry:
            # Show and focus search entry when opening
            self._search_entry.grab_focus()
    
    def _on_search_changed(self, editable: Gtk.Editable) -> None:
        """Handle search entry text change."""
        self._update_search_settings()
    
    def _on_search_activate(self, entry: Gtk.Entry) -> None:
        """Handle Enter key in search entry."""
        self._update_search_settings()
        self._search_next()
    
    def _on_search_next_clicked(self, button: Gtk.Button) -> None:
        """Handle search next button click."""
        logger.debug("_on_search_next_clicked: button clicked")
        self._update_search_settings()
        self._search_next()
    
    def _on_search_prev_clicked(self, button: Gtk.Button) -> None:
        """Handle search previous button click."""
        self._update_search_settings()
        self._search_prev()
    
    def _on_replace_clicked(self, button: Gtk.Button) -> None:
        """Replace current match and move to next."""
        if not _HAS_GTKSOURCE or not self._search_context or not self._replace_entry:
            return
        
        self._update_search_settings()
        replace_text = self._replace_entry.get_text()
        
        # Check if there's a selection that might be a match
        try:
            has_selection, sel_start, sel_end = self._buffer.get_selection_bounds()
        except (ValueError, TypeError):
            has_selection = False
            sel_start = None
            sel_end = None
        
        # Get starting position for search
        if has_selection and sel_start is not None:
            # Start from the beginning of the selection
            search_start = sel_start.copy()
        else:
            # Start from cursor position
            insert_mark = self._buffer.get_insert()
            search_start = self._buffer.get_iter_at_mark(insert_mark)
        
        # Find the match using the search context
        # According to docs, replace() requires iterators from a valid search match
        ok, match_start, match_end, wrapped = self._search_context.forward(search_start)
        
        if not ok:
            # No match found
            return
        
        # Verify the match is at the expected position
        # If we had a selection, the match should start at or before the selection end
        if has_selection and sel_end is not None:
            # Check if match overlaps with selection (match should start before or at selection end)
            if match_start.compare(sel_end) > 0:
                # Match is after selection, which means selection wasn't a match
                # Use the found match
                pass
            elif match_end.compare(sel_start) < 0:
                # Match is before selection, shouldn't happen with forward search
                # Use the found match
                pass
        
        # Replace the match using the iterators from the search context
        # replace() requires: match_start, match_end, replace_text, replace_length
        # According to docs, after replace(), the iterators are revalidated to point to the replaced text
        ok = self._search_context.replace(match_start, match_end, replace_text, len(replace_text))
        if not ok:
            # Replace failed - the iterators might not correspond to a valid match
            return
        
        # After replace(), match_start and match_end iterators are revalidated to point to the replaced text
        # Select the replaced text and scroll to it
        self._buffer.select_range(match_start, match_end)
        self._source_view.scroll_to_iter(match_start, 0.1, True, 0.0, 0.0)
        
        # Move to next occurrence automatically
        # The iterators now point to the replaced text, so we can search from the end
        self._search_next()
    
    def _on_replace_all_clicked(self, button: Gtk.Button) -> None:
        """Replace all matches."""
        if not _HAS_GTKSOURCE or not self._search_context or not self._replace_entry:
            return
        
        self._update_search_settings()
        replace_text = self._replace_entry.get_text()
        if not replace_text:
            return
        
        # replace_all() needs the replace text and its length (in characters)
        replace_length = len(replace_text)
        self._search_context.replace_all(replace_text, replace_length)
    
    # ---------- Undo/Redo methods ----------
    
    def _on_undo_clicked(self, button: Gtk.Button) -> None:
        """Handle undo button click."""
        if _HAS_GTKSOURCE and isinstance(self._buffer, GtkSource.Buffer):
            if self._buffer.can_undo():
                self._buffer.undo()
    
    def _on_redo_clicked(self, button: Gtk.Button) -> None:
        """Handle redo button click."""
        if _HAS_GTKSOURCE and isinstance(self._buffer, GtkSource.Buffer):
            if self._buffer.can_redo():
                self._buffer.redo()
    
    # ---------- Keyboard shortcuts ----------
    
    def _on_key_pressed(self, controller: Gtk.EventControllerKey, keyval: int, keycode: int, state: Gdk.ModifierType) -> bool:
        """Handle keyboard shortcuts."""
        ctrl = state & Gdk.ModifierType.CONTROL_MASK
        shift = state & Gdk.ModifierType.SHIFT_MASK
        
        # Ctrl+F -> show search bar and focus search
        if ctrl and keyval == Gdk.KEY_f:
            # Show search bar if hidden
            if not self._search_toolbar.get_visible():
                self._search_toolbar.set_visible(True)
            if self._search_entry:
                self._search_entry.grab_focus()
            return True
        
        # Ctrl+Z -> undo
        if ctrl and keyval == Gdk.KEY_z and not shift:
            if _HAS_GTKSOURCE and isinstance(self._buffer, GtkSource.Buffer):
                if self._buffer.can_undo():
                    self._buffer.undo()
                    return True
        
        # Ctrl+Shift+Z OR Ctrl+Y -> redo
        if (ctrl and shift and keyval == Gdk.KEY_z) or (ctrl and keyval == Gdk.KEY_y):
            if _HAS_GTKSOURCE and isinstance(self._buffer, GtkSource.Buffer):
                if self._buffer.can_redo():
                    self._buffer.redo()
                    return True
        
        return False


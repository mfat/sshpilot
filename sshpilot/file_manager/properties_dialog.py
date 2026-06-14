"""Nautilus-style properties dialog for SFTP entries."""

from __future__ import annotations

import os
import posixpath
import threading
from typing import TYPE_CHECKING, Optional

from gi.repository import Adw, Gio, GLib, Gtk

from .format_utils import _human_size, _human_time, _mode_to_octal, _mode_to_str

if TYPE_CHECKING:
    # Forward refs only — keeps the file_manager_window mega-module from
    # being imported eagerly when this module is loaded.
    from ..file_manager_window import AsyncSFTPManager, FileEntry


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
        
        # Icon — use the file-type-aware icon resolved from the entry's name.
        from sshpilot import icon_utils
        from .file_type_icons import get_icon_for_name
        icon_name = get_icon_for_name(self._entry.name, self._entry.is_dir)
        icon = icon_utils.new_image_from_icon_name(icon_name)
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



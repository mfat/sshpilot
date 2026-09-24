"""Nautilus-style properties dialog for SFTP entries."""

from __future__ import annotations

import logging
import os
import posixpath
import threading
from gettext import gettext as _
from gettext import ngettext
from typing import TYPE_CHECKING, Any, Optional, Sequence, Union

from gi.repository import Adw, Gio, GLib, Gtk

from .format_utils import (
    _human_size,
    _human_time,
    _item_count_text,
    _mode_to_octal,
    _mode_to_str,
    safe_display_text,
)
from ..shortcut_utils import install_esc_to_close

logger = logging.getLogger(__name__)

# Nautilus lists individual names up to this count, then falls back to "N Selected Items".
_MAX_LISTED_NAMES = 5


def _folder_label(item_count: Optional[int]) -> str:
    if item_count is None:
        return _("Folder")
    return _item_count_text(item_count)


def _folder_calculating_text(item_count: Optional[int]) -> str:
    return _("{folder} (calculating size...)").format(
        folder=_folder_label(item_count)
    )


def _folder_size_text(item_count: Optional[int], total_size: int) -> str:
    if total_size >= 0:
        size = _human_size(total_size)
        if item_count is None:
            return size
        return _("{items} ({size})").format(
            items=_item_count_text(item_count),
            size=size,
        )
    if item_count is None:
        return _("Size unavailable")
    return _("{items} (size unavailable)").format(
        items=_item_count_text(item_count)
    )


def _free_space_text(size: int) -> str:
    return _("{size} Free").format(size=_human_size(size))


def _selection_title(entries: Sequence["FileEntry"]) -> str:
    """Nautilus-style header name for one or more selected entries."""
    count = len(entries)
    if count > _MAX_LISTED_NAMES:
        return ngettext(
            "{count} Selected Item",
            "{count} Selected Items",
            count,
        ).format(count=count)
    if count > 1:
        return ", ".join(safe_display_text(entry.name) for entry in entries)
    return safe_display_text(entries[0].name)


def _selection_size_text(item_count: int, total_size: int) -> str:
    """Nautilus-style size line for a multi-item selection."""
    if total_size < 0:
        return _("Size unavailable")
    size = _human_size(total_size)
    return ngettext(
        "{count} item, with size {size}",
        "{count} items, totalling {size}",
        item_count,
    ).format(count=item_count, size=size)


def _common_value(values: Sequence[Optional[str]]) -> Optional[str]:
    """Return the shared value when every entry agrees, else ``None``."""
    present = [value for value in values if value is not None]
    if not present:
        return None
    first = present[0]
    if all(value == first for value in present[1:]):
        return first
    return None


if TYPE_CHECKING:
    # Forward refs only — keeps the file_manager_window mega-module from
    # being imported eagerly when this module is loaded.
    from .common import FileEntry


@Gtk.Template(resource_path="/io/github/mfat/sshpilot/ui/properties_dialog.ui")
class PropertiesDialog(Adw.Window):
    """Nautilus-style properties dialog using card-based design."""

    __gtype_name__ = "SshPilotPropertiesDialog"

    content_box = Gtk.Template.Child()

    def __init__(
        self,
        entry: Union["FileEntry", Sequence["FileEntry"]],
        current_path: str,
        parent: Gtk.Window,
        sftp_manager: Optional[Any] = None,
    ):
        super().__init__()
        from .common import FileEntry as _FileEntry

        if isinstance(entry, _FileEntry):
            entries = [entry]
        else:
            entries = list(entry)
        if not entries:
            raise ValueError("PropertiesDialog requires at least one entry")

        self._entries = entries
        self._entry = entries[0]
        self._current_path = current_path
        self._parent_window = parent
        self._sftp_manager = sftp_manager
        self.set_transient_for(parent)
        install_esc_to_close(self)

        # Positioning is delegated to the window manager via the modal /
        # transient_for properties; GTK4 has no manual window placement.

        # Build the dialog content
        self._build_dialog()

    @property
    def _is_multi(self) -> bool:
        return len(self._all_entries) > 1

    @property
    def _all_entries(self) -> list["FileEntry"]:
        entries = getattr(self, "_entries", None)
        if entries:
            return list(entries)
        return [self._entry]

    def _build_dialog(self) -> None:
        """Build the Nautilus-style properties dialog content."""
        # Static shell (toolbar view + "Properties" header) is in the template;
        # the property rows are appended into the template content box.
        content = self.content_box

        # Header with icon and name
        content.append(self._create_header_block())

        # Parent folder row
        content.append(self._create_parent_folder_row())

        # Size row
        content.append(self._create_size_row())

        # Modified and Created rows
        content.append(self._create_modified_row())
        content.append(self._create_created_row())

        # Owner / group row
        content.append(self._create_owner_row())

        # Permissions row
        content.append(self._create_permissions_row())

    def _create_header_block(self) -> Gtk.Widget:
        """Create the header block with icon, name, and summary."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, halign=Gtk.Align.CENTER)

        # Icon — single entry uses its type icon; multi uses a generic stack cue.
        from sshpilot import icon_utils
        from ..file_type_icons import get_icon_for_name

        if self._is_multi:
            all_dirs = all(entry.is_dir for entry in self._all_entries)
            all_files = all(not entry.is_dir for entry in self._all_entries)
            if all_dirs:
                icon_name = "folder-symbolic"
            elif all_files:
                icon_name = get_icon_for_name(self._entry.name, False)
            else:
                icon_name = "folder-documents-symbolic"
        else:
            icon_name = get_icon_for_name(self._entry.name, self._entry.is_dir)
        icon = icon_utils.new_image_from_icon_name(icon_name)
        # Set a larger custom size instead of using predefined sizes
        icon.set_pixel_size(64)
        icon.add_css_class("icon-dropshadow")
        icon.add_css_class("card")
        box.append(icon)

        # Name (centered, bold). GTK cannot encode the lone surrogates a
        # filename whose bytes are not valid UTF-8 arrives with.
        name_label = Gtk.Label(label=_selection_title(self._all_entries))
        name_label.add_css_class("title-3")
        name_label.set_wrap(True)
        name_label.set_justify(Gtk.Justification.CENTER)
        box.append(name_label)

        # Summary
        summary_parts = []
        if self._is_multi:
            summary_parts.append(_item_count_text(len(self._all_entries)))
        elif self._entry.is_dir:
            summary_parts.append(_folder_label(self._entry.item_count))
        else:
            if self._entry.size:
                summary_parts.append(_human_size(self._entry.size))

        # Add free space for local files
        if not self._is_remote_file():
            try:
                path = self._local_path(self._entry)
                if os.path.exists(path):
                    stat = os.statvfs(path)
                    free = stat.f_bavail * stat.f_frsize
                    summary_parts.append(_free_space_text(free))
            except Exception:
                pass

        summary_text = " — ".join(summary_parts) if summary_parts else ""
        summary_label = Gtk.Label(label=summary_text)
        summary_label.add_css_class("dim-label")
        box.append(summary_label)

        if self._is_remote_file():
            self._start_remote_free_space(summary_label, summary_parts)

        return box

    def _start_remote_free_space(self, label: Gtk.Label, summary_parts: list) -> None:
        """Append the remote filesystem's free space once the daemon answers.

        Servers without ``statvfs@openssh.com`` just leave it out.
        """
        if self._sftp_manager is None or not hasattr(self._sftp_manager, "filesystem_usage"):
            return
        future = self._sftp_manager.filesystem_usage(self._remote_path(self._entry))

        def _done(fut) -> None:
            try:
                usage = fut.result()
            except Exception as exc:
                logger.debug("Remote free space unavailable: %s", exc)
                return
            parts = [*summary_parts, _free_space_text(usage.available_bytes)]

            def _apply():
                label.set_label(" — ".join(parts))
                return GLib.SOURCE_REMOVE

            GLib.idle_add(_apply)

        future.add_done_callback(_done)

    def _create_size_row(self) -> Gtk.Widget:
        """Create the size row."""
        if self._is_multi:
            size_text = self._start_multi_size_calculation()
        elif self._entry.is_dir:
            base = _folder_label(self._entry.item_count)
            size_text = base
            if not self._is_remote_file():
                # Local folders: recurse with os.walk in the background.
                size_text = _folder_calculating_text(self._entry.item_count)
                self._start_folder_size_calculation()
            elif self._sftp_manager is not None and hasattr(
                self._sftp_manager, "directory_size"
            ):
                # Remote folders: recurse over SFTP in the background.
                size_text = _folder_calculating_text(self._entry.item_count)
                self._start_remote_folder_size_calculation()
        else:
            size_text = _human_size(self._entry.size) if self._entry.size else "—"

        # Store reference to size row for updating
        self._size_row = Adw.ActionRow(title=_("Size"), subtitle=size_text)
        self._size_row.add_css_class("card")
        return self._size_row

    def _start_multi_size_calculation(self) -> str:
        """Sum sizes for a multi-item selection (Nautilus-style)."""
        file_total = sum(entry.size for entry in self._entries if not entry.is_dir)
        folders = [entry for entry in self._entries if entry.is_dir]
        # Until folders finish, treat each selected item as one (not deep count).
        self._multi_size_bytes = file_total
        self._multi_size_failed = False
        self._multi_pending_folders = len(folders)

        if not folders:
            return _selection_size_text(len(self._entries), file_total)

        if self._is_remote_file():
            if self._sftp_manager is None or not hasattr(self._sftp_manager, "directory_size"):
                return _selection_size_text(len(self._entries), file_total)
            for folder in folders:
                self._request_remote_folder_size(folder)
        else:
            for folder in folders:
                path = self._local_path(folder)
                thread = threading.Thread(
                    target=self._calculate_multi_folder_size, args=(path,)
                )
                thread.daemon = True
                thread.start()
        return _selection_size_text(len(self._entries), file_total) + "…"

    def _request_remote_folder_size(self, entry: "FileEntry") -> None:
        try:
            future = self._sftp_manager.directory_size(self._remote_path(entry))
        except Exception as exc:
            logger.debug("Remote folder size request failed: %s", exc)
            self._on_multi_folder_size(-1)
            return

        def _done(fut) -> None:
            try:
                total = fut.result()
            except Exception as exc:
                logger.debug("Remote folder size failed: %s", exc)
                total = -1
            GLib.idle_add(self._on_multi_folder_size, total)

        future.add_done_callback(_done)

    def _calculate_multi_folder_size(self, path: str) -> None:
        total_size = 0
        try:
            for dirpath, _dirnames, filenames in os.walk(path):
                for name in filenames:
                    fp = os.path.join(dirpath, name)
                    if os.path.islink(fp):
                        continue
                    try:
                        total_size += os.path.getsize(fp)
                    except OSError:
                        pass
        except Exception:
            total_size = -1
        GLib.idle_add(self._on_multi_folder_size, total_size)

    def _on_multi_folder_size(self, folder_size: int) -> bool:
        if folder_size < 0:
            self._multi_size_failed = True
        else:
            self._multi_size_bytes += folder_size
        self._multi_pending_folders = max(0, self._multi_pending_folders - 1)
        if self._multi_pending_folders == 0:
            total = -1 if self._multi_size_failed else self._multi_size_bytes
            if hasattr(self, "_size_row") and self._size_row:
                self._size_row.set_subtitle(
                    _selection_size_text(len(self._entries), total)
                )
        return GLib.SOURCE_REMOVE

    def _create_parent_folder_row(self) -> Gtk.Widget:
        """Create the parent folder row."""
        parent_path = os.path.dirname(self._local_path(self._entry))
        if not parent_path:
            parent_path = "/"

        row = Adw.ActionRow(
            title=_("Parent Folder"), subtitle=safe_display_text(parent_path)
        )
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
        if self._is_multi:
            times = [
                _human_time(entry.modified) if entry.modified else None
                for entry in self._entries
            ]
            modified_time = _common_value(times) or "—"
        else:
            modified_time = _human_time(self._entry.modified) if self._entry.modified else "—"
        row = Adw.ActionRow(title=_("Modified"), subtitle=modified_time)
        row.add_css_class("card")
        # Stored so the async remote stat can refresh it with the precise mtime.
        self._modified_row = row
        return row

    def _create_owner_row(self) -> Gtk.Widget:
        """Create the owner/group row."""
        owner_text = "—"
        if not self._is_remote_file():
            owners = []
            for entry in self._entries:
                try:
                    st = os.stat(self._local_path(entry))
                    owners.append(self._format_owner(st.st_uid, st.st_gid))
                except Exception:
                    owners.append(None)
            owner_text = _common_value(owners) or "—"
        elif self._sftp_manager is not None:
            owner_text = _("Loading…")  # filled by the async remote stat
        row = Adw.ActionRow(title=_("Owner"), subtitle=owner_text)
        row.add_css_class("card")
        self._owner_row = row
        return row

    @staticmethod
    def _format_owner(uid, gid) -> str:
        """Format uid/gid, resolving to names on the local machine."""
        if uid is None or gid is None:
            return "—"
        user, group = str(uid), str(gid)
        try:
            import pwd

            user = pwd.getpwuid(int(uid)).pw_name
        except Exception:
            pass
        try:
            import grp

            group = grp.getgrgid(int(gid)).gr_name
        except Exception:
            pass
        return f"{user} : {group}"

    @staticmethod
    def _format_remote_owner(uid, gid) -> str:
        """Format remote uid/gid numerically until a daemon name-resolution op exists."""
        if uid is None or gid is None:
            return "—"
        return f"{int(uid)} : {int(gid)}"

    def _create_created_row(self) -> Gtk.Widget:
        """Create the created date row (if available)."""
        # Creation time is per-file and not meaningful for a multi selection.
        if self._is_remote_file() or self._is_multi:
            return Gtk.Box()  # Empty box widget

        # Try to get creation time for local files
        try:
            path = self._local_path(self._entry)
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

        row = Adw.ActionRow(title=_("Created"), subtitle=created_time)
        row.add_css_class("card")
        return row

    def _create_permissions_row(self) -> Gtk.Widget:
        """Create the permissions row."""
        perms_text = "—"

        # Get actual permissions for local files
        if not self._is_remote_file():
            perms = []
            for entry in self._entries:
                try:
                    path = self._local_path(entry)
                    if os.path.exists(path):
                        mode = os.stat(path).st_mode
                        perms.append(f"{_mode_to_str(mode)} ({_mode_to_octal(mode)})")
                    else:
                        perms.append(None)
                except Exception:
                    perms.append(None)
            perms_text = _common_value(perms) or "—"
        else:
            # For remote files, fetch typed daemon metadata (mode, uid/gid,
            # mtime) asynchronously; the manager owns all remote I/O.
            perms_text = _("Loading…")

            if self._sftp_manager is not None and hasattr(self._sftp_manager, "stat"):
                self._start_remote_metadata_fetch()
            else:
                logger.debug("PropertiesDialog: No SFTP manager available")
                if self._is_multi:
                    perms_text = "—"
                elif self._entry.is_dir:
                    perms_text = _("Create and Delete Files")
                else:
                    perms_text = _("Read and Write")

        row = Adw.ActionRow(title=_("Permissions"), subtitle=perms_text)
        row.add_css_class("card")
        # Store reference to row for async updates
        self._permissions_row = row

        return row

    def _start_remote_metadata_fetch(self) -> None:
        """Stat every selected remote entry, then show common values."""
        results: list[Optional[Any]] = [None] * len(self._entries)
        pending = len(self._entries)

        def _finish() -> None:
            nonlocal pending
            pending -= 1
            if pending > 0:
                return

            owners: list[Optional[str]] = []
            perms: list[Optional[str]] = []
            modified: list[Optional[str]] = []
            for index, entry in enumerate(self._entries):
                remote = results[index]
                if remote is None:
                    owners.append(None)
                    perms.append(None)
                    modified.append(None)
                    continue
                if remote.uid is not None and remote.gid is not None:
                    owners.append(self._format_remote_owner(remote.uid, remote.gid))
                else:
                    owners.append(None)
                if remote.mode:
                    perms.append(
                        f"{_mode_to_str(remote.mode, remote.file_type)} ({_mode_to_octal(remote.mode)})"
                    )
                else:
                    perms.append(None)
                if remote.modified_at is not None:
                    modified.append(_human_time(remote.modified_at.timestamp()))
                else:
                    modified.append(None)

            owner_text = _common_value(owners) or "—"
            perm_text = _common_value(perms)
            if perm_text is None:
                if self._is_multi:
                    perm_text = "—"
                elif self._entry.is_dir:
                    perm_text = _("Create and Delete Files")
                else:
                    perm_text = _("Read and Write")
            modified_text = _common_value(modified)

            def _apply():
                self._update_permissions_row(perm_text)
                if hasattr(self, "_owner_row"):
                    self._owner_row.set_subtitle(owner_text)
                if modified_text is not None and hasattr(self, "_modified_row"):
                    self._modified_row.set_subtitle(modified_text)
                return GLib.SOURCE_REMOVE

            GLib.idle_add(_apply)

        for index, entry in enumerate(self._entries):
            remote_path = self._remote_path(entry)
            logger.debug("PropertiesDialog: Fetching remote metadata for %s", remote_path)
            future = self._sftp_manager.stat(remote_path)

            def _on_done(fut, slot=index) -> None:
                try:
                    results[slot] = fut.result()
                except Exception as exc:
                    logger.debug("Failed to get remote file attributes: %s", exc)
                    results[slot] = None
                _finish()

            future.add_done_callback(_on_done)

    def _update_permissions_row(self, text: str) -> None:
        """Update the permissions row subtitle."""
        if hasattr(self, '_permissions_row'):
            self._permissions_row.set_subtitle(text)

    def _is_remote_file(self) -> bool:
        """Check if this is a remote file (from SFTP)."""
        # Check if we have an SFTP manager - that's the most reliable indicator
        if self._sftp_manager is not None:
            logger.debug("PropertiesDialog: Detected remote file (has SFTP manager)")
            return True

        # Fallback heuristic - in a real implementation, you'd pass connection info
        is_remote = "://" in self._current_path or (
            self._current_path.startswith("/")
            and not os.path.exists(self._local_path(self._entry))
        )
        logger.debug(
            "PropertiesDialog: _is_remote_file()=%s, path=%s, has_sftp_manager=%s",
            is_remote,
            self._current_path,
            self._sftp_manager is not None,
        )
        return is_remote

    def _on_open_parent(self, *_) -> None:
        """Open parent directory in system file manager."""
        try:
            if not self._is_remote_file():
                parent_dir = os.path.dirname(self._local_path(self._entry))
                if os.path.exists(parent_dir):
                    Gio.AppInfo.launch_default_for_uri(f"file://{parent_dir}", None)
        except Exception:
            pass

    def _local_path(self, entry: "FileEntry") -> str:
        return os.path.join(self._current_path, entry.name)

    def _start_folder_size_calculation(self):
        """Start calculating folder size in background thread."""
        folder_path = self._local_path(self._entry)

        # Create and start the background thread
        thread = threading.Thread(target=self._calculate_folder_size, args=(folder_path,))
        thread.daemon = True  # Allows main program to exit even if thread is running
        thread.start()

    def _remote_path(self, entry: Optional["FileEntry"] = None) -> str:
        target = entry if entry is not None else self._entry
        if self._current_path.endswith("/"):
            return self._current_path + target.name
        return posixpath.join(self._current_path, target.name)

    def _start_remote_folder_size_calculation(self) -> None:
        """Recursively size a remote folder over SFTP, then update the row."""
        try:
            future = self._sftp_manager.directory_size(self._remote_path())
        except Exception as exc:
            logger.debug("Remote folder size request failed: %s", exc)
            return

        def _done(fut) -> None:
            try:
                total = fut.result()
            except Exception as exc:
                logger.debug("Remote folder size failed: %s", exc)
                total = -1
            GLib.idle_add(self._update_folder_size_ui, total)

        future.add_done_callback(_done)

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
            size_text = _folder_size_text(self._entry.item_count, total_size)

            self._size_row.set_subtitle(size_text)

        # Returning GLib.SOURCE_REMOVE ensures this function only runs once
        return GLib.SOURCE_REMOVE

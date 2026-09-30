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

from . import permissions as perms
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
    """Placeholder while a folder's deep size is still being measured.

    Nautilus shows a spinner beside a partial count; we approximate with an
    ellipsis until the final ``items, totalling size`` line is ready.
    """
    if item_count is None or item_count == 0:
        return "…"
    return _item_count_text(item_count)


def _folder_size_text(item_count: Optional[int], total_size: int) -> str:
    """Nautilus-style contents line for a folder (or deep-counted selection)."""
    if total_size < 0:
        if item_count is None:
            return _("Size unavailable")
        if item_count == 0:
            return _("Empty folder")
        return _("{items} (size unavailable)").format(
            items=_item_count_text(item_count)
        )
    if item_count is not None and item_count == 0 and total_size == 0:
        return _("Empty folder")
    if item_count is None:
        return _human_size(total_size)
    return _selection_size_text(item_count, total_size)


def _free_space_text(size: int) -> str:
    # Nautilus: "%s Free" (e.g. "100 MB Free")
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
    """Nautilus-style size line for a multi-item / deep-counted selection."""
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

    navigation_view = Gtk.Template.Child()
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
        # Per-entry permission state, filled by the local or remote stat.
        self._modes: list[Optional[int]] = [None] * len(entries)
        self._initial_modes: list[Optional[int]] = [None] * len(entries)
        self._file_types: list[Optional[Any]] = [None] * len(entries)
        self._uids: list[Optional[int]] = [None] * len(entries)
        self._gids: list[Optional[int]] = [None] * len(entries)
        self._access_rows: list[tuple[Adw.ComboRow, perms.PermissionClass, bool, list[int]]] = []
        self._permissions_page: Optional[Adw.NavigationPage] = None
        self._syncing_permission_widgets = False
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
        if not self._is_remote_file():
            self._load_local_stats()

        # Header with icon, name, size/contents, and free space (folders only)
        content.append(self._create_header_block())

        # Rows are grouped in boxed lists like Nautilus's preference groups.
        # An Adw row needs a ListBox parent: it emits "activated" and owns the
        # focus a row grabs (a bare row asserts in gtk_list_box_row_grab_focus).
        content.append(self._row_list(self._create_parent_folder_row()))
        content.append(
            self._row_list(self._create_modified_row(), self._create_created_row())
        )
        content.append(self._row_list(self._create_owner_row()))
        # Permissions row (opens "Set Custom Permissions") and exec switch
        content.append(
            self._row_list(self._create_permissions_row(), self._create_execution_row())
        )
        if not self._is_remote_file():
            self._on_modes_known()

    @staticmethod
    def _row_list(*rows: Gtk.Widget) -> Gtk.ListBox:
        box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.add_css_class("boxed-list")
        for row in rows:
            # _create_created_row returns a bare placeholder when there is no date.
            if isinstance(row, Gtk.ListBoxRow):
                box.append(row)
        return box

    def _load_local_stats(self) -> None:
        for index, entry in enumerate(self._entries):
            try:
                st = os.stat(self._local_path(entry))
            except OSError:
                continue
            self._modes[index] = st.st_mode
            self._uids[index] = st.st_uid
            self._gids[index] = st.st_gid

    @property
    def _all_folders(self) -> bool:
        return all(entry.is_dir for entry in self._all_entries)

    def _header_icon_name(self) -> str:
        """Colored Adwaita mimetype icon, matching the file-manager listing."""
        from ..file_type_icons import ICON_FOLDER, ICON_GENERIC, get_icon_for_name

        entries = self._all_entries
        if len(entries) == 1:
            return get_icon_for_name(entries[0].name, entries[0].is_dir)
        if self._all_folders:
            return ICON_FOLDER
        if all(not entry.is_dir for entry in entries):
            icons = {get_icon_for_name(entry.name, False) for entry in entries}
            return icons.pop() if len(icons) == 1 else ICON_GENERIC
        return ICON_GENERIC

    def _create_header_block(self) -> Gtk.Widget:
        """Create the header block with icon, name, size, and free space.

        Layout mirrors Nautilus: colored icon, title, size/contents caption,
        then a separate free-space caption when every selected item is a folder.
        """
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, halign=Gtk.Align.CENTER)

        from sshpilot import icon_utils

        icon = icon_utils.new_image_from_icon_name(self._header_icon_name())
        icon.set_pixel_size(64)
        icon.add_css_class("icon-dropshadow")
        box.append(icon)

        # Name (centered, bold). GTK cannot encode the lone surrogates a
        # filename whose bytes are not valid UTF-8 arrives with.
        name_label = Gtk.Label(label=_selection_title(self._all_entries))
        name_label.add_css_class("title-3")
        name_label.set_wrap(True)
        name_label.set_justify(Gtk.Justification.CENTER)
        box.append(name_label)

        # Size / contents (caption under the name, like Nautilus)
        size_text = self._initial_size_text()
        self._size_label = Gtk.Label(label=size_text)
        self._size_label.add_css_class("caption")
        self._size_label.set_wrap(True)
        self._size_label.set_justify(Gtk.Justification.CENTER)
        box.append(self._size_label)
        self._start_size_measurement()

        # Free space: separate caption, folders only (Nautilus should_show_free_space)
        self._free_space_label = Gtk.Label(label="")
        self._free_space_label.add_css_class("caption")
        self._free_space_label.set_visible(False)
        box.append(self._free_space_label)
        if self._all_folders:
            self._fill_free_space()

        return box

    def _initial_size_text(self) -> str:
        if self._is_multi:
            file_total = sum(
                entry.size for entry in self._all_entries if not entry.is_dir
            )
            if any(entry.is_dir for entry in self._all_entries):
                return _selection_size_text(len(self._all_entries), file_total) + "…"
            return _selection_size_text(len(self._all_entries), file_total)
        if self._entry.is_dir:
            return _folder_calculating_text(self._entry.item_count)
        return _human_size(self._entry.size) if self._entry.size else "—"

    def _start_size_measurement(self) -> None:
        """Kick off folder / multi deep-size work that updates the header label."""
        if self._is_multi:
            self._start_multi_size_calculation()
            return
        if not self._entry.is_dir:
            return
        if not self._is_remote_file():
            self._start_folder_size_calculation()
        elif self._sftp_manager is not None and hasattr(self._sftp_manager, "directory_size"):
            self._start_remote_folder_size_calculation()
        else:
            # No deep-size backend: fall back to the shallow item count.
            self._set_size_text(_folder_label(self._entry.item_count))

    def _set_size_text(self, text: str) -> None:
        label = getattr(self, "_size_label", None)
        if label is not None:
            label.set_label(text)

    def _fill_free_space(self) -> None:
        """Show free space under the size line when the volume can report it."""
        if not self._is_remote_file():
            try:
                path = self._local_path(self._entry)
                if os.path.exists(path):
                    stat = os.statvfs(path)
                    free = stat.f_bavail * stat.f_frsize
                    self._show_free_space(free)
            except Exception:
                pass
            return
        self._start_remote_free_space()

    def _show_free_space(self, available_bytes: int) -> None:
        label = getattr(self, "_free_space_label", None)
        if label is None:
            return
        label.set_label(_free_space_text(available_bytes))
        label.set_visible(True)

    def _start_remote_free_space(self) -> None:
        """Fill the free-space caption once the daemon answers.

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

            def _apply():
                self._show_free_space(usage.available_bytes)
                return GLib.SOURCE_REMOVE

            GLib.idle_add(_apply)

        future.add_done_callback(_done)

    def _start_multi_size_calculation(self) -> None:
        """Sum sizes for a multi-item selection (Nautilus-style)."""
        file_total = sum(entry.size for entry in self._all_entries if not entry.is_dir)
        folders = [entry for entry in self._all_entries if entry.is_dir]
        self._multi_size_bytes = file_total
        self._multi_size_failed = False
        self._multi_pending_folders = len(folders)

        if not folders:
            return

        if self._is_remote_file():
            if self._sftp_manager is None or not hasattr(self._sftp_manager, "directory_size"):
                return
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
            self._set_size_text(_selection_size_text(len(self._all_entries), total))
        return GLib.SOURCE_REMOVE

    def _create_parent_folder_row(self) -> Gtk.Widget:
        """Create the parent folder row."""
        parent_path = os.path.dirname(self._local_path(self._entry))
        if not parent_path:
            parent_path = "/"

        row = Adw.ActionRow(
            title=_("Parent Folder"), subtitle=safe_display_text(parent_path)
        )
        row.set_activatable(False)

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
        row.set_activatable(False)
        # Stored so the async remote stat can refresh it with the precise mtime.
        self._modified_row = row
        return row

    def _create_owner_row(self) -> Gtk.Widget:
        """Create the owner/group row."""
        owner_text = "—"
        if not self._is_remote_file():
            owners = [
                self._format_owner(uid, gid) if uid is not None else None
                for uid, gid in zip(self._uids, self._gids)
            ]
            owner_text = _common_value(owners) or "—"
        elif self._sftp_manager is not None:
            owner_text = _("Loading…")  # filled by the async remote stat
        row = Adw.ActionRow(title=_("Owner"), subtitle=owner_text)
        row.set_activatable(False)
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
        row.set_activatable(False)
        return row

    def _create_permissions_row(self) -> Gtk.Widget:
        """Create the permissions row."""
        perms_text = "—"

        # Get actual permissions for local files
        if not self._is_remote_file():
            perms_text = self._mode_summary_text() or "—"
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
        row.set_activatable(False)  # until every selected mode is known
        from sshpilot import icon_utils

        self._permissions_arrow = icon_utils.new_image_from_icon_name("go-next-symbolic")
        self._permissions_arrow.set_visible(False)
        row.add_suffix(self._permissions_arrow)
        row.connect("activated", self._on_permissions_row_activated)
        # Store reference to row for async updates
        self._permissions_row = row

        return row

    def _create_execution_row(self) -> Gtk.Widget:
        """Nautilus "Executable as Program" switch, single regular files only."""
        row = Adw.SwitchRow(title=_("Executable as Program"))
        row.set_visible(False)
        row.connect("notify::active", self._on_execution_toggled)
        self._execution_row = row
        return row

    def _should_show_execution_switch(self) -> bool:
        if self._is_multi or self._entry.is_dir:
            return False
        content_type, uncertain = Gio.content_type_guess(self._entry.name, None)
        return (
            uncertain
            or content_type == "application/octet-stream"
            or Gio.content_type_can_be_executable(content_type)
        )

    def _mode_summary_text(self) -> Optional[str]:
        texts = [
            f"{_mode_to_str(mode, file_type)} ({_mode_to_octal(mode)})"
            if mode is not None
            else None
            for mode, file_type in zip(self._modes, self._file_types)
        ]
        return _common_value(texts)

    @property
    def _permissions_known(self) -> bool:
        return all(mode is not None for mode in self._modes)

    def _can_set_permissions(self) -> bool:
        """Locally only the owner (or root) may chmod; remote servers decide."""
        if self._is_remote_file():
            return True
        euid = os.geteuid() if hasattr(os, "geteuid") else None
        if euid is None or euid == 0:
            return True
        return all(uid == euid for uid in self._uids)

    def _on_modes_known(self) -> None:
        """Enable editing once every selected entry's mode is known."""
        if not self._permissions_known:
            return
        self._initial_modes = list(self._modes)
        self._permissions_row.set_activatable(True)
        self._permissions_arrow.set_visible(True)
        self._execution_row.set_visible(self._should_show_execution_switch())
        self._sync_permission_widgets()

    def _permission_items(self) -> list[tuple[bool, int]]:
        return [
            (entry.is_dir, mode)
            for entry, mode in zip(self._entries, self._modes)
            if mode is not None
        ]

    def _sync_permission_widgets(self) -> None:
        """Show the current modes in the summary, switch and access combos."""
        summary = perms.summarize(self._permission_items())
        text = self._mode_summary_text()
        if text is not None:
            self._update_permissions_row(text)
        can_set = self._can_set_permissions()
        self._syncing_permission_widgets = True
        try:
            self._execution_row.set_active(summary.file_exec == perms.EXEC)
            self._execution_row.set_sensitive(can_set)
            for row, who, is_folder, values in self._access_rows:
                current = (summary.folder if is_folder else summary.file).get(who, 0)
                if current not in values:
                    # Nautilus lists an odd or mixed value as an extra choice.
                    values.append(current)
                    row.get_model().append(perms.permission_label(current, is_folder))
                row.set_selected(values.index(current))
                row.set_sensitive(can_set)
        finally:
            self._syncing_permission_widgets = False

    def _on_permissions_row_activated(self, _row) -> None:
        if not self._permissions_known:
            return
        if self._permissions_page is None:
            self._permissions_page = self._build_permissions_page()
            self._sync_permission_widgets()
        self.navigation_view.push(self._permissions_page)

    def _common_owner_parts(self) -> tuple[str, str]:
        users: list[Optional[str]] = []
        groups: list[Optional[str]] = []
        remote = self._is_remote_file()
        for uid, gid in zip(self._uids, self._gids):
            if uid is None or gid is None:
                users.append(None)
                groups.append(None)
                continue
            text = (
                self._format_remote_owner(uid, gid)
                if remote
                else self._format_owner(uid, gid)
            )
            user, _sep, group = text.partition(" : ")
            users.append(user)
            groups.append(group)
        return _common_value(users) or "—", _common_value(groups) or "—"

    def _build_permissions_page(self) -> Adw.NavigationPage:
        """Nautilus "Set Custom Permissions" page."""
        items = self._permission_items()
        has_folders = any(is_dir for is_dir, _mode in items)
        has_files = any(not is_dir for is_dir, _mode in items)
        user, group = self._common_owner_parts()

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        banner = Adw.Banner(title=_("Only the owner can edit these permissions"))
        banner.set_revealed(not self._can_set_permissions())
        box.append(banner)

        page = Adw.PreferencesPage()
        page.set_vexpand(True)
        box.append(page)

        sections = (
            (perms.PermissionClass.OWNER, None, _("Owner"), user),
            (perms.PermissionClass.GROUP, None, _("Group"), group),
            (perms.PermissionClass.OTHERS, _("Other Users"), None, None),
        )
        for who, group_title, name_title, name in sections:
            pref_group = Adw.PreferencesGroup()
            if group_title:
                pref_group.set_title(group_title)
            if name_title:
                name_row = Adw.ActionRow(title=name_title, subtitle=safe_display_text(name))
                name_row.add_css_class("property")
                pref_group.add(name_row)
            if has_folders and has_files:
                pref_group.add(self._create_access_row(who, True, _("Folder Access")))
                pref_group.add(self._create_access_row(who, False, _("File Access")))
            else:
                pref_group.add(self._create_access_row(who, has_folders, _("Access")))
            page.add(pref_group)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(box)
        return Adw.NavigationPage(
            title=_("Set Custom Permissions"), tag="permissions", child=toolbar
        )

    def _create_access_row(
        self, who: perms.PermissionClass, is_folder: bool, title: str
    ) -> Adw.ComboRow:
        values = perms.permission_choices(who, is_folder)
        model = Gtk.StringList.new(
            [perms.permission_label(value, is_folder) for value in values]
        )
        row = Adw.ComboRow(title=title, model=model)
        self._access_rows.append((row, who, is_folder, values))
        row.connect("notify::selected", self._on_access_selected, who, is_folder, values)
        return row

    def _on_access_selected(self, row, _pspec, who, is_folder, values) -> None:
        if self._syncing_permission_widgets:
            return
        position = row.get_selected()
        if position == Gtk.INVALID_LIST_POSITION or position >= len(values):
            return
        value = values[position]
        mask = perms.class_mask(who, is_folder)
        new_modes = {}
        for index, entry in enumerate(self._entries):
            mode = self._modes[index]
            if mode is None or entry.is_dir != is_folder:
                continue
            if value & perms.INCONSISTENT:
                # The mixed "---" choice puts back what each item started with.
                source = self._initial_modes[index]
                bits = source if source is not None else mode
            else:
                bits = perms.permission_to_mode(who, value)
            new_modes[index] = perms.apply_bits(mode, bits, mask)
        self._change_modes(new_modes)

    def _on_execution_toggled(self, row, _pspec) -> None:
        if self._syncing_permission_widgets:
            return
        executable = row.get_active()
        new_modes = {
            index: perms.set_executable(mode, executable)
            for index, (entry, mode) in enumerate(zip(self._entries, self._modes))
            if mode is not None and not entry.is_dir
        }
        self._change_modes(new_modes)

    def _change_modes(self, new_modes: dict[int, int]) -> None:
        """chmod each changed entry; failures revert and are reported once."""
        changes = {
            index: mode
            for index, mode in new_modes.items()
            if self._modes[index] is not None
            and (self._modes[index] & 0o7777) != mode
        }
        if not changes:
            return
        previous = {index: self._modes[index] for index in changes}
        for index, mode in changes.items():
            # Keep the file-type bits so the summary still shows "d" / "l".
            self._modes[index] = (previous[index] & ~0o7777) | mode

        failures: list[tuple[int, BaseException]] = []
        pending = len(changes)

        def _finished_one() -> None:
            nonlocal pending
            pending -= 1
            if pending:
                return
            for index, _exc in failures:
                self._modes[index] = previous[index]
            self._sync_permission_widgets()
            if failures:
                index, exc = failures[0]
                self._report_permission_error(self._entries[index], exc)

        remote = self._is_remote_file()
        for index, mode in changes.items():
            entry = self._entries[index]
            if not remote:
                try:
                    os.chmod(self._local_path(entry), mode)
                except OSError as exc:
                    failures.append((index, exc))
                _finished_one()
                continue
            try:
                future = self._sftp_manager.chmod(self._remote_path(entry), mode)
            except Exception as exc:
                failures.append((index, exc))
                _finished_one()
                continue

            def _done(fut, slot=index) -> None:
                exc = fut.exception()

                def _apply():
                    if exc is not None:
                        failures.append((slot, exc))
                    _finished_one()
                    return GLib.SOURCE_REMOVE

                GLib.idle_add(_apply)

            future.add_done_callback(_done)

    def _report_permission_error(self, entry: "FileEntry", exc: BaseException) -> None:
        logger.debug("Changing permissions of %s failed: %s", entry.name, exc)
        detail = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
        text = _("Could not change the permissions of “{name}”: {error}").format(
            name=entry.name, error=detail
        )
        from .pane import present_error_alert

        present_error_alert(self, text)

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
            mode_texts: list[Optional[str]] = []
            modified: list[Optional[str]] = []
            for index, entry in enumerate(self._entries):
                remote = results[index]
                if remote is None:
                    owners.append(None)
                    mode_texts.append(None)
                    modified.append(None)
                    continue
                if remote.uid is not None and remote.gid is not None:
                    owners.append(self._format_remote_owner(remote.uid, remote.gid))
                else:
                    owners.append(None)
                if remote.mode:
                    mode_texts.append(
                        f"{_mode_to_str(remote.mode, remote.file_type)} ({_mode_to_octal(remote.mode)})"
                    )
                else:
                    mode_texts.append(None)
                if remote.modified_at is not None:
                    modified.append(_human_time(remote.modified_at.timestamp()))
                else:
                    modified.append(None)

            owner_text = _common_value(owners) or "—"
            perm_text = _common_value(mode_texts)
            if perm_text is None:
                if self._is_multi:
                    perm_text = "—"
                elif self._entry.is_dir:
                    perm_text = _("Create and Delete Files")
                else:
                    perm_text = _("Read and Write")
            modified_text = _common_value(modified)

            def _apply():
                for index, remote in enumerate(results):
                    if remote is None:
                        continue
                    self._uids[index] = remote.uid
                    self._gids[index] = remote.gid
                    self._file_types[index] = remote.file_type
                    self._modes[index] = remote.mode or None
                self._update_permissions_row(perm_text)
                if hasattr(self, "_owner_row"):
                    self._owner_row.set_subtitle(owner_text)
                if modified_text is not None and hasattr(self, "_modified_row"):
                    self._modified_row.set_subtitle(modified_text)
                self._on_modes_known()
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
        Updates the size caption with the final folder size.
        THIS RUNS ON THE MAIN GTK THREAD.
        """
        self._set_size_text(_folder_size_text(self._entry.item_count, total_size))

        # Returning GLib.SOURCE_REMOVE ensures this function only runs once
        return GLib.SOURCE_REMOVE

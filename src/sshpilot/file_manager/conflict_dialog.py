"""Nautilus-style file conflict dialog for pre-transfer resolution."""

from __future__ import annotations

import logging
from gettext import gettext as _
from typing import Callable, Optional

from gi.repository import Adw, GLib, Gtk, Pango

from ..shortcut_utils import install_esc_to_close
from .conflict_resolution import (
    ConflictAction,
    ConflictItem,
    ConflictResponse,
    ConflictSideInfo,
)
from .format_utils import _human_size, _human_time, filename_extension_offset, safe_display_text

logger = logging.getLogger(__name__)

# Match Nautilus BUTTON_ACTIVATION_DELAY_IN_SECONDS — avoid accidental clicks
# when a transfer dialog appears under the cursor.
_BUTTON_ACTIVATION_DELAY_MS = 2000


class FileConflictDialog(Adw.Window):
    """Ask how to resolve one destination conflict before a transfer starts."""

    def __init__(
        self,
        parent: Optional[Gtk.Widget],
        item: ConflictItem,
        *,
        suggested_name: str,
        remaining_count: int = 1,
        on_response: Optional[Callable[[ConflictResponse], None]] = None,
    ) -> None:
        super().__init__()
        self._item = item
        self._suggested_name = suggested_name
        self._conflict_name = item.conflict_name
        self._remaining_count = max(1, int(remaining_count))
        self._on_response = on_response
        self._response_emitted = False
        self._activation_timeout_id = 0

        self.set_title(self._window_title())
        self.set_modal(True)
        self.set_resizable(False)
        self.set_default_size(480, -1)
        if parent is not None:
            transient = parent
            if not isinstance(parent, Gtk.Window):
                root = parent.get_root() if hasattr(parent, "get_root") else None
                if isinstance(root, Gtk.Window):
                    transient = root
            if isinstance(transient, Gtk.Window):
                try:
                    self.set_transient_for(transient)
                except Exception:
                    pass

        install_esc_to_close(self)
        self.connect("close-request", self._on_close_request)

        self._build_ui()
        self._populate()
        self._delay_buttons()

    def present_for(self, parent: Optional[Gtk.Widget] = None) -> None:
        try:
            if parent is not None and hasattr(self, "present"):
                # Adw.Window.present() takes no parent; already transient_for.
                self.present()
            else:
                self.present()
        except Exception:
            logger.debug("FileConflictDialog present failed", exc_info=True)
            self.present()

    def _window_title(self) -> str:
        src_dir = self._item.source.is_directory
        dst_dir = self._item.destination.is_directory
        if src_dir and dst_dir:
            return _("Merge Folder")
        if src_dir or dst_dir:
            return _("File and Folder Conflict")
        return _("File Conflict")

    def _build_ui(self) -> None:
        toolbar = Adw.ToolbarView()
        self.set_content(toolbar)

        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)

        self._cancel_button = Gtk.Button(label=_("_Cancel"), use_underline=True)
        self._cancel_button.connect("clicked", lambda *_: self._finish(ConflictAction.CANCEL))
        header.pack_start(self._cancel_button)

        self._skip_button = Gtk.Button(label=_("_Skip"), use_underline=True)
        self._skip_button.connect("clicked", lambda *_: self._finish(ConflictAction.SKIP))
        header.pack_end(self._skip_button)

        self._rename_button = Gtk.Button(label=_("_Rename"), use_underline=True)
        self._rename_button.add_css_class("suggested-action")
        self._rename_button.set_visible(False)
        self._rename_button.set_sensitive(False)
        self._rename_button.connect("clicked", lambda *_: self._finish(ConflictAction.RENAME))
        header.pack_end(self._rename_button)

        replace_label = _("_Merge") if self._item.is_merge else _("_Replace")
        self._replace_button = Gtk.Button(label=replace_label, use_underline=True)
        self._replace_button.add_css_class("suggested-action")
        self._replace_button.connect("clicked", lambda *_: self._finish(ConflictAction.REPLACE))
        # Block replacing a folder with a symlink (Nautilus).
        if (
            self._item.destination.is_directory
            and self._item.source.is_symlink
            and not self._item.destination.is_symlink
        ):
            self._replace_button.set_sensitive(False)
        header.pack_end(self._replace_button)

        toolbar.add_top_bar(header)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.set_margin_top(18)
        body.set_margin_bottom(18)
        body.set_margin_start(18)
        body.set_margin_end(18)

        self._primary_label = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self._primary_label.add_css_class("title-2")
        self._primary_label.set_max_width_chars(50)
        body.append(self._primary_label)

        self._secondary_label = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self._secondary_label.set_max_width_chars(50)
        body.append(self._secondary_label)

        comparison = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        comparison.set_halign(Gtk.Align.START)

        self._dest_row = self._make_side_row()
        self._src_row = self._make_side_row()
        comparison.append(self._dest_row["box"])
        comparison.append(self._src_row["box"])

        self._expander = Gtk.Expander(label=_("Select a _new name for the destination"), use_underline=True)
        self._expander.connect("notify::expanded", self._on_expanded)

        rename_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        rename_box.add_css_class("linked")
        rename_box.set_margin_top(6)

        self._name_entry = Gtk.Entry()
        self._name_entry.set_hexpand(True)
        self._name_entry.set_activates_default(True)
        self._name_entry.connect("changed", self._on_entry_changed)
        rename_box.append(self._name_entry)

        reset = Gtk.Button(label=_("R_eset"), use_underline=True)
        reset.connect("clicked", self._on_reset)
        rename_box.append(reset)

        self._expander.set_child(rename_box)
        comparison.append(self._expander)
        body.append(comparison)

        self._apply_all = Gtk.CheckButton(
            label=_("_Apply this action to all files and folders"),
            use_underline=True,
        )
        self._apply_all.connect("toggled", self._on_apply_all_toggled)
        if self._remaining_count <= 1:
            self._apply_all.set_visible(False)
        body.append(self._apply_all)

        toolbar.set_content(body)
        self.set_default_widget(self._replace_button)

    def _make_side_row(self) -> dict:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        icon = Gtk.Image()
        icon.set_pixel_size(48)
        icon.set_valign(Gtk.Align.CENTER)
        label = Gtk.Label(xalign=0.0, use_markup=True)
        label.set_wrap(True)
        label.set_max_width_chars(42)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(icon)
        box.append(label)
        return {"box": box, "icon": icon, "label": label}

    def _populate(self) -> None:
        primary, secondary = self._dialog_text()
        self._primary_label.set_text(primary)
        self._secondary_label.set_text(secondary)

        dest = self._item.destination
        src = self._item.source
        self._set_side(
            self._dest_row,
            dest,
            heading=_("Original Folder") if dest.is_directory else _("Original File"),
            size_label=_("Contents:") if dest.is_directory else _("Size:"),
        )
        replace_heading = _("Merge With") if self._item.is_merge else _("Replace With")
        self._set_side(
            self._src_row,
            src,
            heading=replace_heading,
            size_label=_("Contents:") if src.is_directory else _("Size:"),
        )

        self._name_entry.set_text(self._suggested_name)
        self._on_entry_changed(self._name_entry)

        if (
            self._item.destination.is_directory
            and self._item.source.is_symlink
            and not self._item.destination.is_symlink
        ):
            self._apply_all.set_sensitive(False)

    def _set_side(
        self,
        row: dict,
        side: ConflictSideInfo,
        *,
        heading: str,
        size_label: str,
    ) -> None:
        icon_name = "folder-symbolic" if side.is_directory else "text-x-generic-symbolic"
        if side.is_symlink:
            icon_name = "emblem-symbolic-link"
        row["icon"].set_from_icon_name(icon_name)

        size_text = _("Unknown")
        if side.is_directory:
            size_text = _("folder")
        elif side.size is not None:
            size_text = _human_size(int(side.size))

        mtime_text = _human_time(side.mtime) if side.mtime is not None else _("Unknown")
        markup = (
            f"<b>{safe_display_text(heading)}</b>\n"
            f"{safe_display_text(size_label)} {safe_display_text(size_text)}\n"
            f"{safe_display_text(_('Last modified:'))} {safe_display_text(mtime_text)}"
        )
        row["label"].set_markup(markup)

    def _dialog_text(self) -> tuple[str, str]:
        name = safe_display_text(self._conflict_name)
        directory = safe_display_text(self._item.destination_directory_name or _("the destination"))
        src = self._item.source
        dest = self._item.destination

        if dest.is_directory and src.is_symlink and not dest.is_symlink:
            primary = _(
                "You are trying to replace the destination folder “{name}” with a symbolic link."
            ).format(name=name)
            extra = _("Please rename the symbolic link or press the skip button.")
            message = _(
                "This is not allowed in order to avoid the deletion of the destination folder’s contents."
            )
            return primary, f"{message}\n{extra}"

        if dest.is_directory and src.is_directory:
            primary = _("Merge folder “{name}”?").format(name=name)
            extra = _(
                "Merging will ask for confirmation before replacing any files in "
                "the folder that conflict with the files being copied."
            )
            message = self._age_message(
                _("An older folder with the same name already exists in “{directory}”."),
                _("A newer folder with the same name already exists in “{directory}”."),
                _("Another folder with the same name already exists in “{directory}”."),
                directory,
            )
            return primary, f"{message}\n{extra}"

        if dest.is_directory and not src.is_directory:
            primary = _("Replace folder “{name}”?").format(name=name)
            extra = _("Replacing it will remove all files in the folder.")
            message = _("A folder with the same name already exists in “{directory}”.").format(
                directory=directory
            )
            return primary, f"{message}\n{extra}"

        primary = _("Replace file “{name}”?").format(name=name)
        extra = _("Replacing it will overwrite its content.")
        message = self._age_message(
            _("An older file with the same name already exists in “{directory}”."),
            _("A newer file with the same name already exists in “{directory}”."),
            _("Another file with the same name already exists in “{directory}”."),
            directory,
        )
        return primary, f"{message}\n{extra}"

    def _age_message(self, older: str, newer: str, same: str, directory: str) -> str:
        src_m = self._item.source.mtime
        dst_m = self._item.destination.mtime
        if src_m is not None and dst_m is not None:
            if src_m > dst_m:
                return older.format(directory=directory)
            if src_m < dst_m:
                return newer.format(directory=directory)
        return same.format(directory=directory)

    def _delay_buttons(self) -> None:
        for button in (
            self._cancel_button,
            self._skip_button,
            self._replace_button,
            self._rename_button,
        ):
            button.set_sensitive(False)

        def _enable() -> bool:
            self._activation_timeout_id = 0
            self._cancel_button.set_sensitive(True)
            self._skip_button.set_sensitive(True)
            # Replace may stay disabled for symlink→folder.
            if not (
                self._item.destination.is_directory
                and self._item.source.is_symlink
                and not self._item.destination.is_symlink
            ):
                self._replace_button.set_sensitive(True)
            self._on_entry_changed(self._name_entry)
            return False

        self._activation_timeout_id = GLib.timeout_add(_BUTTON_ACTIVATION_DELAY_MS, _enable)

    def _on_expanded(self, expander: Gtk.Expander, *_args) -> None:
        expanded = expander.get_expanded()
        self._replace_button.set_visible(not expanded)
        self._rename_button.set_visible(expanded)
        if expanded:
            self.set_default_widget(self._rename_button)
            self._apply_all.set_sensitive(False)
            self._name_entry.grab_focus()
            text = self._name_entry.get_text()
            if text == self._suggested_name:
                start = filename_extension_offset(self._conflict_name)
                end = filename_extension_offset(self._suggested_name)
                self._name_entry.select_region(start, end)
        else:
            self.set_default_widget(self._replace_button)
            self._apply_all.set_sensitive(self._remaining_count > 1)

    def _on_apply_all_toggled(self, button: Gtk.CheckButton) -> None:
        self._expander.set_sensitive(not button.get_active())

    def _on_entry_changed(self, entry: Gtk.Entry) -> None:
        text = (entry.get_text() or "").strip()
        valid = bool(text) and text != self._conflict_name and "/" not in text and "\\" not in text
        if self._rename_button.get_visible():
            self._rename_button.set_sensitive(valid and self._cancel_button.get_sensitive())

    def _on_reset(self, *_args) -> None:
        self._name_entry.set_text(self._conflict_name)
        self._name_entry.grab_focus()
        end = filename_extension_offset(self._conflict_name)
        self._name_entry.select_region(0, end)

    def _on_close_request(self, *_args) -> bool:
        self._finish(ConflictAction.CANCEL)
        return False

    def _finish(self, action: ConflictAction) -> None:
        if self._response_emitted:
            return
        self._response_emitted = True
        if self._activation_timeout_id:
            GLib.source_remove(self._activation_timeout_id)
            self._activation_timeout_id = 0

        apply_to_all = False
        new_name: Optional[str] = None
        if action is ConflictAction.RENAME:
            new_name = (self._name_entry.get_text() or "").strip()
        elif action is not ConflictAction.CANCEL:
            apply_to_all = bool(self._apply_all.get_active())

        response = ConflictResponse(
            action=action,
            apply_to_all=apply_to_all,
            new_name=new_name,
        )
        callback = self._on_response
        self._on_response = None
        try:
            self.close()
        except Exception:
            pass
        if callback is not None:
            GLib.idle_add(lambda: (callback(response), False)[1])

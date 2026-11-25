"""Advanced Configuration, sidebar: the blocks of the configuration.

The blocks are listed in file order, which is the order OpenSSH reads them
in: the first value it obtains for an option is the one it keeps. They are
grouped under the file they are in; the file on screen lists its blocks, and
another file of the configuration (one an ``Include`` line reads) is a row
that opens it.

A block is reordered by dragging it within its file. The global options
stay first (the model refuses any other order, and says why). Where a drop
would land is a line above or under the row the pointer is on: nothing is
inserted into the list during a drag, so nothing moves under the pointer.

The search field keeps the blocks whose name or address (HostName) holds
what is typed, and selects the best of them.
"""
from __future__ import annotations

from gettext import gettext as _
from typing import Dict, Optional, Sequence

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, Gtk, Pango  # noqa: E402

from .advanced_config_block_row import BlockRow  # noqa: E402
from .advanced_config_widgets import DragItem, set_class  # noqa: E402
from .gtk import advanced_config_messages as messages  # noqa: E402
from .ssh_config_blocks import GLOBAL, HOST, MATCH, Block  # noqa: E402


def _block_detail(block: Block) -> str:
    """``user@hostname`` as the block itself sets them; empty when it sets
    neither."""
    values: Dict[str, str] = {}
    for option in block.options:
        keyword = option.keyword
        if keyword in ("user", "hostname") and keyword not in values:
            values[keyword] = option.value.strip().strip('"')
    user, host = values.get("user", ""), values.get("hostname", "")
    if user and host:
        return f"{user}@{host}"
    # Without an address of its own, what the block is says more than a
    # user name alone.
    return host


class FileRow(Gtk.ListBoxRow):
    """A file of the configuration: the heading of its blocks when it is the
    one on screen, a row that opens it otherwise."""

    def __init__(self, file: str, name: str) -> None:
        super().__init__()
        self.file = file
        self.set_selectable(False)
        self.add_css_class("advcfg-file-row")
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_top(10)
        box.set_margin_bottom(4)
        self.icon = Gtk.Image.new_from_icon_name("text-x-generic-symbolic")
        self.icon.add_css_class("dim-label")
        box.append(self.icon)
        self.label = Gtk.Label(label=name, xalign=0)
        self.label.set_hexpand(True)
        self.label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.label.add_css_class("caption-heading")
        self.label.add_css_class("dim-label")
        box.append(self.label)
        self.arrow = Gtk.Image.new_from_icon_name("go-next-symbolic")
        self.arrow.add_css_class("dim-label")
        box.append(self.arrow)
        self.set_child(box)
        self.set_tooltip_text(name)

    def set_current(self, current: bool, name: str) -> None:
        if self.label.get_label() != name:
            self.label.set_label(name)
            self.set_tooltip_text(name)
        self.set_activatable(not current)
        self.arrow.set_visible(not current)
        self.icon.set_from_icon_name(
            "document-open-symbolic" if current else "text-x-generic-symbolic"
        )


class BlockSidebar(Gtk.Box):
    """The list of blocks. *controller* supplies ``select_block``,
    ``open_file``, ``drop_block``, ``add_block``, ``search_changed`` and
    ``editor`` (for ``has_global``)."""

    def __init__(self, controller) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.controller = controller
        self.add_css_class("advcfg-sidebar")
        #: True while the view itself moves the selection.
        self._syncing = False
        self._rows: Dict[int, BlockRow] = {}
        self._file_rows: Dict[str, FileRow] = {}
        self._blocks: Sequence[Block] = ()
        self._current_file = ""
        #: The row a drop would land beside, and on which side.
        self._drop_row: Optional[Gtk.ListBoxRow] = None
        self._dragging: Optional[int] = None

        self.target_entry = Gtk.SearchEntry()
        self.target_entry.set_placeholder_text(_("Search blocks"))
        self.target_entry.set_tooltip_text(_("Find blocks by name, hostname or IP address"))
        for side in ("top", "bottom", "start", "end"):
            getattr(self.target_entry, f"set_margin_{side}")(8)
        self.target_entry.connect("search-changed", lambda *_a: controller.search_changed())
        self.append(self.target_entry)

        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list.add_css_class("navigation-sidebar")
        self.list.add_css_class("advcfg-block-list")
        self.list.connect("row-selected", self._on_row_selected)
        self.list.connect("row-activated", self._on_row_activated)
        self.list.set_filter_func(self._row_shown)
        nothing = Gtk.Label(label=_("No block matches."), wrap=True, xalign=0)
        nothing.add_css_class("dim-label")
        for side in ("top", "bottom", "start", "end"):
            getattr(nothing, f"set_margin_{side}")(12)
        self.list.set_placeholder(nothing)
        target = Gtk.DropTarget.new(DragItem, Gdk.DragAction.MOVE)
        target.connect("motion", self._on_drag_motion)
        target.connect("leave", lambda *_a: self._mark_drop(None, False))
        target.connect("drop", self._on_drop)
        self.list.add_controller(target)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_child(self.list)
        self.append(scroller)

        menu = Gio.Menu()
        # Host and Match are OpenSSH's keywords: not translated.
        menu.append(_("Host Block"), "advcfg-sidebar.add-host")
        menu.append(_("Match Block"), "advcfg-sidebar.add-match")
        menu.append(_("Global Options"), "advcfg-sidebar.add-global")
        actions = Gio.SimpleActionGroup()
        for name, kind in (("add-host", HOST), ("add-match", MATCH), ("add-global", GLOBAL)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a, k=kind: controller.add_block(k))
            actions.add_action(action)
        self._add_global = actions.lookup_action("add-global")
        self.insert_action_group("advcfg-sidebar", actions)
        self.new_button = Gtk.MenuButton(menu_model=menu)
        self.new_button.set_child(Adw.ButtonContent(
            icon_name="list-add-symbolic", label=_("New Block"),
        ))
        self.new_button.set_tooltip_text(_("Add a block to this file"))
        self.new_button.set_direction(Gtk.ArrowType.UP)
        self.new_button.set_margin_top(6)
        self.new_button.set_margin_bottom(8)
        self.new_button.set_margin_start(8)
        self.new_button.set_margin_end(8)
        self.append(Gtk.Separator())
        self.append(self.new_button)

    # -- the rows ------------------------------------------------------------

    def sync(self, blocks: Sequence[Block], files: Sequence, current_file: str,
             current_name: str) -> None:
        """Make the rows match *blocks* (of *current_file*) and *files*, in
        place: a row lives as long as its block, and is only relabelled."""
        self._blocks = blocks
        self._current_file = current_file
        # The files: the one on screen, or every file of the configuration.
        wanted_files = [(ref.file, ref.display_name) for ref in files] or [
            (current_file, current_name)
        ]
        names = dict(wanted_files)
        names.setdefault(current_file, current_name)
        self._syncing = True
        try:
            for file in [f for f in self._file_rows if f not in names]:
                self.list.remove(self._file_rows.pop(file))
            wanted = {block.uid for block in blocks}
            for uid in [uid for uid in self._rows if uid not in wanted]:
                self.list.remove(self._rows.pop(uid))
            index = 0
            for file, name in wanted_files:
                row = self._file_rows.get(file)
                if row is None:
                    row = self._file_rows[file] = FileRow(file, name)
                    self.list.insert(row, index)
                elif row.get_index() != index:
                    self.list.remove(row)
                    self.list.insert(row, index)
                row.set_current(file == current_file, name)
                index += 1
                if file != current_file:
                    continue
                for block in blocks:
                    block_row = self._rows.get(block.uid)
                    if block_row is None:
                        block_row = self._rows[block.uid] = self._new_row(block.uid)
                        self.list.insert(block_row, index)
                    elif block_row.get_index() != index:
                        self.list.remove(block_row)
                        self.list.insert(block_row, index)
                    block_row.update(
                        block.kind, block.patterns, block.disabled,
                        "" if block.kind == GLOBAL else _block_detail(block),
                    )
                    block_row._movable = block.kind != GLOBAL
                    block_row.set_movable(block_row._movable)
                    index += 1
            self.list.invalidate_filter()
        finally:
            self._syncing = False
        has_global = any(block.kind == GLOBAL for block in blocks)
        self._add_global.set_enabled(not has_global)

    def _new_row(self, uid: int) -> BlockRow:
        row = BlockRow()
        row._uid = uid
        row._movable = True
        source = Gtk.DragSource()
        source.set_actions(Gdk.DragAction.MOVE)
        source.connect("prepare", self._on_drag_prepare, row)
        source.connect("drag-begin", self._on_drag_begin, row)
        source.connect("drag-end", lambda *_a: self._end_drag())
        source.connect("drag-cancel", lambda *_a: (self._end_drag(), False)[1])
        row.add_controller(source)
        return row

    def rows(self) -> Dict[int, BlockRow]:
        return dict(self._rows)

    def select(self, uid: Optional[int]) -> None:
        """Show *uid*'s row as the selected one (None: none)."""
        row = self._rows.get(uid) if uid is not None else None
        if self.list.get_selected_row() is row:
            return
        self._syncing = True
        try:
            if row is None:
                self.list.unselect_all()
            else:
                self.list.select_row(row)
        finally:
            self._syncing = False

    def show_dimmed(self, dimmed: set) -> None:
        """Dim the rows of the blocks in *dimmed* (blocks that do not apply
        to the host being checked), in place."""
        for uid, row in self._rows.items():
            set_class(row, "advcfg-skipped", uid in dimmed)

    def _on_row_selected(self, _list, row) -> None:
        if self._syncing or row is None or not isinstance(row, BlockRow):
            return
        self.controller.select_block(row._uid)

    def _on_row_activated(self, _list, row) -> None:
        if isinstance(row, FileRow) and row.file != self._current_file:
            self.controller.open_file(row.file)

    # -- search --------------------------------------------------------------

    def search_words(self) -> list:
        return self.target_entry.get_text().casefold().split()

    def _row_shown(self, row) -> bool:
        if isinstance(row, FileRow):
            return True
        words = self.search_words()
        if not words:
            return True
        block = next((b for b in self._blocks if b.uid == getattr(row, "_uid", None)), None)
        return block is not None and block_matches(block, words)

    def refilter(self) -> None:
        self.list.invalidate_filter()

    # -- dragging a block ----------------------------------------------------

    def _on_drag_prepare(self, _source, _x, _y, row):
        if not row._movable or self.controller.raw_pending:
            return None
        return Gdk.ContentProvider.new_for_value(DragItem(row._uid))

    def _on_drag_begin(self, source, _drag, row) -> None:
        self._dragging = row._uid
        try:
            source.set_icon(Gtk.WidgetPaintable.new(row), 0, 0)
        except Exception:
            pass

    def _end_drag(self) -> None:
        self._dragging = None
        self._mark_drop(None, False)

    def _drop_place(self, y: float):
        """``(row, before)`` a drop at *y* would land beside, or None: a
        file's heading, or below the last block of another file, is no
        place for a block of this one."""
        row = self.list.get_row_at_y(int(y))
        if not isinstance(row, BlockRow):
            return None
        found, bounds = row.compute_bounds(self.list)
        before = found and y < bounds.get_y() + bounds.get_height() / 2
        return row, before

    def _on_drag_motion(self, _target, _x, y):
        place = self._drop_place(y)
        if place is None or place[0]._uid == self._dragging:
            self._mark_drop(None, False)
            return Gdk.DragAction.MOVE if place is not None else 0
        self._mark_drop(*place)
        return Gdk.DragAction.MOVE

    def _mark_drop(self, row, before: bool) -> None:
        previous = self._drop_row
        if previous is not None and previous is not row:
            previous.remove_css_class("advcfg-drop-above")
            previous.remove_css_class("advcfg-drop-below")
        self._drop_row = row
        if row is not None:
            set_class(row, "advcfg-drop-above", before)
            set_class(row, "advcfg-drop-below", not before)

    def _on_drop(self, _target, value, _x, y) -> bool:
        self._mark_drop(None, False)
        uid = value.uid if isinstance(value, DragItem) else None
        place = self._drop_place(y)
        if uid is None or place is None or place[0]._uid == uid:
            return False
        row, before = place
        return bool(self.controller.drop_block(uid, row._uid, before))


def block_matches(block: Block, words) -> bool:
    """Whether *block* is found by every word: in its name (what its row in
    the list reads) or in its address (HostName). Its other options are not
    searched: a host is not found by a directive that happens to mention it."""
    text = " ".join([
        messages.block_list_title(block.kind, block.patterns),
        *(option.value for option in block.options if option.keyword == "hostname"),
    ]).casefold()
    return all(word in text for word in words)

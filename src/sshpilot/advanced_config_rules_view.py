"""Advanced Configuration, left pane: the rules as cards.

One card per block: a header band (the handle it is dragged by, with the
block's patterns and its menu) and under it the block's options, indented.

The view owns widgets only. It never edits the draft: every gesture is
handed to the *controller* (the window), which edits through
:class:`~sshpilot.advanced_config_model.DraftEditor` and calls :meth:`sync`.

``sync`` updates what is on screen in place. A card or a row is created
once, when its block or option first appears, kept for as long as it
exists, and moved rather than rebuilt when the order changes; text typed
into a field is left alone. Rebuilding the whole list on every edit is what
made the window slow, and it also threw away focus and scroll position.
Menus are built when they are first opened, for the same reason.
"""
from __future__ import annotations

from gettext import gettext as _
from typing import Dict, List, Optional, Set

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from .api.models.connections import (  # noqa: E402
    SshConfigDraftEvaluation,
    SshConfigDraftLineState as State,
)
from .gtk import advanced_config_messages as messages  # noqa: E402
from .ssh_config_blocks import GLOBAL, HOST, Block, LineMap, Option  # noqa: E402

#: Options offered by "Common Option", with a starting value.
COMMON_OPTIONS = (
    ("HostName", ""),
    ("User", ""),
    ("Port", "22"),
    ("IdentityFile", "~/.ssh/id_ed25519"),
    ("ProxyJump", ""),
    ("ForwardAgent", "yes"),
    ("IdentitiesOnly", "yes"),
    ("ServerAliveInterval", "60"),
)

#: Where a block gone to rests in the list: this far down it, as a part of
#: its height. Between the top and the middle.
_CARD_REST = 0.25

#: How long a card takes to fold or open.
_FOLD_MS = 150

_BLOCK_DRAG = "block"
_OPTION_DRAG = "option"


class DragItem(GObject.Object):
    """What is being dragged: a block or an option of this window.

    A type of its own, not text: a text payload dropped on one of the many
    entries would be taken by the entry and typed into it.
    """

    def __init__(self, kind: str, uid: int) -> None:
        super().__init__()
        self.kind = kind
        self.uid = uid


def pill(text: str = "", style: str = "muted") -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.set_valign(Gtk.Align.CENTER)
    label.add_css_class("advcfg-pill")
    label.add_css_class(style)
    label._style = style
    # The tooltip is handed over when the pointer asks for it: setting one
    # costs a hundred times what the rest of a pill does, and a selection
    # changes dozens of pills that nobody will hover.
    label._tooltip = ""
    label.set_has_tooltip(True)
    label.connect("query-tooltip", _pill_tooltip)
    label.set_visible(bool(text))
    return label


def _pill_tooltip(label, _x, _y, _keyboard, tooltip) -> bool:
    if not label._tooltip:
        return False
    tooltip.set_text(label._tooltip)
    return True


def set_pill(label: Gtk.Label, text: str, style: str, tooltip: str = "") -> None:
    # A selection repaints every pill of every card: one that already says
    # this is left alone (asking GTK to set what is set is not free).
    shown = (text, style, tooltip)
    if getattr(label, "_shown", None) == shown:
        return
    label._shown = shown
    if label.get_label() != text:
        label.set_label(text)
    if label._style != style:
        label.remove_css_class(label._style)
        label.add_css_class(style)
        label._style = style
    label._tooltip = tooltip
    label.set_visible(bool(text))


def _set_class(widget, name: str, on: bool) -> None:
    if on:
        widget.add_css_class(name)
    else:
        widget.remove_css_class(name)


def _drag_payload(value) -> tuple:
    if isinstance(value, DragItem):
        return value.kind, value.uid
    return "", 0


class OptionRow:
    """One option line: grip, keyword, value, state, remove."""

    def __init__(self, view: "RulesView", option: Option) -> None:
        self.view = view
        self.uid = option.uid
        #: What :meth:`show` last put on the line.
        self._said: Optional[tuple] = None
        self.box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        grip = Gtk.Image.new_from_icon_name("list-drag-handle-symbolic")
        grip.add_css_class("dim-label")
        grip.set_tooltip_text(_("Drag to reorder or to another block"))
        self.box.append(grip)
        self.key = Gtk.Entry(text=option.key)
        self.key.add_css_class("monospace")
        self.key.set_width_chars(22)
        self.key.set_placeholder_text(_("Option"))
        self.key.connect("changed", self._on_changed, "key")
        self.box.append(self.key)
        self.value = Gtk.Entry(text=option.value)
        self.value.add_css_class("monospace")
        self.value.set_hexpand(True)
        self.value.set_placeholder_text(_("Value"))
        self.value.connect("changed", self._on_changed, "value")
        self.box.append(self.value)
        self.status = pill()
        self.box.append(self.status)
        remove = Gtk.Button(icon_name="edit-delete-symbolic", tooltip_text=_("Remove option"))
        remove.add_css_class("flat")
        remove.connect("clicked", lambda *_a: view.controller.delete_option(self.uid))
        self.box.append(remove)

        source = Gtk.DragSource()
        source.set_actions(Gdk.DragAction.MOVE)
        source.connect("prepare", lambda *_a: Gdk.ContentProvider.new_for_value(
            DragItem(_OPTION_DRAG, self.uid)
        ))
        source.connect("drag-begin", lambda src, _drag: view.drag_icon(src, self.box))
        grip.add_controller(source)
        target = Gtk.DropTarget.new(DragItem, Gdk.DragAction.MOVE)
        target.connect("motion", view.on_drop_motion, self.box)
        target.connect("leave", lambda *_a: view.clear_drop(self.box))
        target.connect("drop", self._on_drop)
        self.box.add_controller(target)

    def _on_changed(self, entry, field: str) -> None:
        if not self.view.syncing:
            self.view.controller.option_typed(self.uid, field, entry.get_text())

    def _on_drop(self, _target, value, _x, y) -> bool:
        self.view.clear_drop(self.box)
        kind, uid = _drag_payload(value)
        if kind != _OPTION_DRAG or uid == self.uid:
            return False
        return self.view.controller.drop_option(uid, self.uid, y < self.box.get_height() / 2)

    def sync(self, option: Option) -> None:
        for entry, text in ((self.key, option.key), (self.value, option.value)):
            if entry.get_text() != text:
                entry.set_text(text)

    def show(self, item, error) -> None:
        """OpenSSH's word on this line: a state, a rejection, or nothing."""
        # The same word as last time (most lines, on most selections):
        # nothing to do. The items are values, compared as such.
        if self._said == (item, error) and self._said is not None:
            return
        self._said = (item, error)
        for entry in (self.key, self.value):
            _set_class(entry, "error", error is not None)
        if error is not None:
            set_pill(self.status, _("Rejected"), "warning", messages.error_text(error))
            unused = False
        elif item is None:
            set_pill(self.status, "", "muted")
            unused = False
        else:
            set_pill(
                self.status, messages.state_label(item.state),
                messages.STATE_STYLES[item.state], messages.state_tooltip(item),
            )
            unused = item.state in (State.SHADOWED, State.FORCED, State.INACTIVE)
        _set_class(self.box, "advcfg-unused", unused)


class BlockCard:
    """One block: header band, optional note, option rows, add buttons."""

    def __init__(self, view: "RulesView", block: Block) -> None:
        self.view = view
        self.uid = block.uid
        self.kind = block.kind
        self.rows: Dict[int, OptionRow] = {}
        #: The last answer does not apply this block: it is dimmed while it
        #: is not selected.
        self.dims = False
        #: Whether the card is dimmed on screen now.
        self.dimmed = False
        controller = view.controller
        movable = block.kind != GLOBAL

        self.card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.card.add_css_class("card")
        self.card.add_css_class("advcfg-block")
        # A click anywhere on the card, or focus entering one of its fields,
        # selects the block. The click is watched, never taken: the fields
        # and buttons inside still receive it.
        click = Gtk.GestureClick()
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", lambda *_a: controller.select_block(self.uid))
        self.card.add_controller(click)
        self.click = click
        focus = Gtk.EventControllerFocus()
        focus.connect("enter", lambda *_a: controller.select_block(self.uid))
        self.card.add_controller(focus)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.add_css_class("advcfg-header")
        if movable:
            header.add_css_class("movable")
            header.set_cursor_from_name("grab")
            header.set_tooltip_text(_("Drag to reorder"))
        grip = Gtk.Image.new_from_icon_name("list-drag-handle-symbolic")
        grip.add_css_class("dim-label")
        grip.set_opacity(1.0 if movable else 0.3)
        header.append(grip)
        # Folding is the user's: nothing folds or opens a block on its own,
        # except a drag (the others fold while it lasts) and going to a
        # line inside a folded block (it opens).
        self.fold_button = Gtk.Button(icon_name="pan-down-symbolic")
        self.fold_button.add_css_class("flat")
        self.fold_button.set_tooltip_text(_("Collapse"))
        self.fold_button.connect("clicked", lambda *_a: view.set_folded(self.uid, not self.folded))
        header.append(self.fold_button)
        self.number = Gtk.Label()
        self.number.add_css_class("dim-label")
        self.number.add_css_class("numeric")
        header.append(self.number)
        # Host and Match are OpenSSH's keywords: not translated.
        kind_label = {GLOBAL: _("Global"), HOST: "Host"}.get(block.kind, "Match")
        header.append(pill(kind_label, "muted" if block.kind == GLOBAL else "accent"))
        self.patterns: Optional[Gtk.Entry] = None
        if block.kind == GLOBAL:
            text = Gtk.Label(label=_("Options for every target"), xalign=0)
            text.add_css_class("dim-label")
            text.set_hexpand(True)
            header.append(text)
        else:
            self.patterns = Gtk.Entry(text=block.patterns)
            self.patterns.add_css_class("monospace")
            self.patterns.set_hexpand(True)
            self.patterns.set_placeholder_text(
                "*.example.com !bastion" if block.kind == HOST else "host *.prod user deploy"
            )
            self.patterns.update_property(
                [Gtk.AccessibleProperty.LABEL],
                [_("Host patterns") if block.kind == HOST else _("Match criteria")],
            )
            self.patterns.connect("changed", self._on_patterns_changed)
            header.append(self.patterns)
        self.status = pill()
        header.append(self.status)
        self.up = self.down = None
        if movable:
            self.up = Gtk.Button(icon_name="go-up-symbolic", tooltip_text=_("Move up"))
            self.down = Gtk.Button(icon_name="go-down-symbolic", tooltip_text=_("Move down"))
            for button, delta in ((self.up, -1), (self.down, 1)):
                button.add_css_class("flat")
                button.connect(
                    "clicked", lambda *_a, d=delta: controller.move_block_by(self.uid, d)
                )
                header.append(button)
        menu = Gtk.MenuButton(icon_name="view-more-symbolic")
        menu.add_css_class("flat")
        menu.set_tooltip_text(_("Block options"))
        menu.set_create_popup_func(self._build_block_menu)
        header.append(menu)
        self.card.append(header)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.add_css_class("advcfg-body")
        self.note: Optional[Gtk.Entry] = None
        self._note_slot = Gtk.Box()
        body.append(self._note_slot)
        self.warning = Gtk.Label(xalign=0, wrap=True)
        self.warning.add_css_class("error")
        self.warning.set_visible(False)
        body.append(self.warning)
        self.rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        if block.kind != GLOBAL:
            self.rows_box.add_css_class("advcfg-options")   # global options: not indented
        body.append(self.rows_box)
        self.footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.footer.add_css_class("advcfg-footer")
        add = Gtk.Button()
        add.set_child(Adw.ButtonContent(icon_name="list-add-symbolic", label=_("Add Option")))
        add.connect("clicked", lambda *_a: controller.add_option(self.uid))
        self.footer.append(add)
        presets = Gtk.MenuButton(label=_("Common Option"))
        presets.set_create_popup_func(self._build_preset_menu)
        self.footer.append(presets)
        self.rows_box.append(self.footer)
        #: Folded to its header line by the user (the arrow, Collapse All).
        self.folded = False
        self.revealer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
            transition_duration=_FOLD_MS,
            reveal_child=True,
        )
        self.revealer.set_child(body)
        self.card.append(self.revealer)

        if movable:
            source = Gtk.DragSource()
            source.set_actions(Gdk.DragAction.MOVE)
            source.connect("prepare", lambda *_a: Gdk.ContentProvider.new_for_value(
                DragItem(_BLOCK_DRAG, self.uid)
            ))

            def begun(src, _drag) -> None:
                view.drag_icon(src, self.card)
                view.begin_block_drag(self.uid)

            source.connect("drag-begin", begun)
            source.connect("drag-end", lambda *_a: view.end_block_drag())
            source.connect("drag-cancel", lambda *_a: (view.end_block_drag(), False)[1])
            header.add_controller(source)       # the whole band, not only the grip
        target = Gtk.DropTarget.new(DragItem, Gdk.DragAction.MOVE)
        target.connect("motion", view.on_drop_motion, self.card)
        target.connect("leave", lambda *_a: view.clear_drop(self.card))
        target.connect("drop", self._on_drop)
        self.card.add_controller(target)

    def show_folded(self, folded: bool, *, animate: bool = True) -> None:
        """Fold the card to its header line or open it, as shown: what the
        user chose (``folded``) is not touched."""
        _set_class(self.card, "folded", folded)
        if self.revealer.get_reveal_child() == folded:
            self.revealer.set_transition_duration(_FOLD_MS if animate else 0)
            self.revealer.set_reveal_child(not folded)
        self.fold_button.set_icon_name("pan-end-symbolic" if folded else "pan-down-symbolic")
        self.fold_button.set_tooltip_text(_("Expand") if folded else _("Collapse"))

    # -- menus: built when first opened ------------------------------------

    def _actions(self, prefix: str, entries) -> None:
        group = Gio.SimpleActionGroup()
        for name, callback in entries:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a, c=callback: c())
            group.add_action(action)
        self.card.insert_action_group(prefix, group)

    def _build_block_menu(self, button: Gtk.MenuButton) -> None:
        controller = self.view.controller
        block = controller.editor.find_block(self.uid)
        self._actions("block", (
            ("toggle", lambda: controller.toggle_block(self.uid)),
            ("note", lambda: controller.open_note(self.uid)),
            ("duplicate", lambda: controller.duplicate_block(self.uid)),
            ("delete", lambda: controller.delete_block(self.uid)),
        ))
        menu = Gio.Menu()
        disabled = bool(block and block.disabled)
        menu.append(_("Enable Block") if disabled else _("Disable Block"), "block.toggle")
        if self.kind != GLOBAL:
            # (Global options have no header line to put a note above.)
            menu.append(_("Add or Edit Note"), "block.note")
            menu.append(_("Duplicate Block"), "block.duplicate")
        menu.append(_("Delete Block"), "block.delete")
        button.set_menu_model(menu)

    def _build_preset_menu(self, button: Gtk.MenuButton) -> None:
        controller = self.view.controller
        self._actions("preset", tuple(
            (f"p{number}", lambda k=key, v=value: controller.add_option(self.uid, k, v))
            for number, (key, value) in enumerate(COMMON_OPTIONS)
        ))
        button.set_menu_model(self.view.preset_menu)

    # -- gestures -----------------------------------------------------------

    def _on_patterns_changed(self, entry) -> None:
        if not self.view.syncing:
            self.view.controller.patterns_typed(self.uid, entry.get_text())

    def _on_note_changed(self, entry) -> None:
        if not self.view.syncing:
            self.view.controller.note_typed(self.uid, entry.get_text())

    def _on_drop(self, _target, value, _x, y) -> bool:
        self.view.clear_drop(self.card)
        kind, uid = _drag_payload(value)
        if kind == _OPTION_DRAG:
            return self.view.controller.drop_option_on_block(uid, self.uid)
        if kind != _BLOCK_DRAG or uid == self.uid:
            return False
        return self.view.controller.drop_block(uid, self.uid, y < self.card.get_height() / 2)

    # -- showing the block ---------------------------------------------------

    def sync(self, block: Block, index: int, total: int, note_open: bool) -> None:
        label = f"#{index + 1}"
        if self.number.get_label() != label:
            self.number.set_label(label)
        _set_class(self.card, "disabled", block.disabled)
        if self.patterns is not None and self.patterns.get_text() != block.patterns:
            self.patterns.set_text(block.patterns)
        if self.up is not None:
            self.up.set_sensitive(index > 0)
            self.down.set_sensitive(index < total - 1)
        self._sync_note(block, note_open)
        self._sync_rows(block)

    def _sync_note(self, block: Block, note_open: bool) -> None:
        wanted = block.kind != GLOBAL and (bool(block.note) or note_open)
        if wanted and self.note is None:
            self.note = Gtk.Entry(text=block.note)
            self.note.set_placeholder_text(_("Note (written as a comment above the block)"))
            self.note.add_css_class("dim-label")
            self.note.set_hexpand(True)
            self.note.connect("changed", self._on_note_changed)
            self._note_slot.append(self.note)
        elif not wanted and self.note is not None:
            self._note_slot.remove(self.note)
            self.note = None
        elif self.note is not None and self.note.get_text() != block.note:
            self.note.set_text(block.note)

    def _sync_rows(self, block: Block) -> None:
        wanted = [option.uid for option in block.options]
        for uid in [uid for uid in self.rows if uid not in wanted]:
            self.rows_box.remove(self.rows.pop(uid).box)
        previous = None
        for option in block.options:
            row = self.rows.get(option.uid)
            if row is None:
                row = self.rows[option.uid] = OptionRow(self.view, option)
                self.rows_box.insert_child_after(row.box, previous)
            else:
                row.sync(option)
                after = previous.get_next_sibling() if previous else self.rows_box.get_first_child()
                if after is not row.box:
                    self.rows_box.reorder_child_after(row.box, previous)
            previous = row.box
        # The add buttons stay last.
        if self.footer.get_next_sibling() is not None:
            self.rows_box.reorder_child_after(self.footer, previous)


class RulesView(Gtk.Box):
    """The scrolling list of block cards."""

    def __init__(self, controller) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.controller = controller
        #: True while :meth:`sync` writes into fields: their "changed" is
        #: then the view's own doing, not typing.
        self.syncing = False
        self.cards: Dict[int, BlockCard] = {}
        self.selected_uid: Optional[int] = None
        #: The block being dragged, while one is.
        self._dragging: Optional[int] = None
        self.set_size_request(420, -1)
        self.preset_menu = Gio.Menu()
        for number, (key, _value) in enumerate(COMMON_OPTIONS):
            self.preset_menu.append(key, f"preset.p{number}")

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for side in ("top", "bottom"):
            getattr(header, f"set_margin_{side}")(8)
        header.set_margin_start(16)
        header.set_margin_end(16)
        title = Gtk.Label(label=_("Configuration rules"))
        title.add_css_class("heading")
        header.append(title)
        self.block_count = pill()
        header.append(self.block_count)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        header.append(spacer)
        self.add_global_button = Gtk.Button(label=_("Global"))
        self.add_global_button.set_tooltip_text(_("Add options above every Host and Match line"))
        self.add_global_button.connect("clicked", lambda *_a: controller.add_block(GLOBAL))
        header.append(self.add_global_button)
        for label, kind in (("Host", HOST), ("Match", "match")):
            button = Gtk.Button(label=label)
            button.set_tooltip_text(_("Add a block at the end"))
            button.connect("clicked", lambda *_a, k=kind: controller.add_block(k))
            header.append(button)
        self.append(header)
        self.append(Gtk.Separator())
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_vexpand(True)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(self.box, f"set_margin_{side}")(14)
        self.empty = Adw.StatusPage(
            icon_name="document-properties-symbolic",
            title=_("No Configuration Rules"),
            description=_("Add a Host or Match block to start."),
        )
        self.empty.set_visible(False)
        self.box.append(self.empty)
        self.scroller.set_child(self.box)
        self.scroller.get_vadjustment().connect("notify::page-size", self._sync_scroll_room)
        self.append(self.scroller)

    # -- drag feedback -------------------------------------------------------

    @staticmethod
    def drag_icon(source, widget) -> None:
        try:
            source.set_icon(Gtk.WidgetPaintable.new(widget), 0, 0)
        except Exception:
            pass

    @staticmethod
    def clear_drop(widget) -> None:
        widget.remove_css_class("advcfg-drop-before")
        widget.remove_css_class("advcfg-drop-after")

    def on_drop_motion(self, _target, _x, y, widget):
        self.clear_drop(widget)
        widget.add_css_class(
            "advcfg-drop-before" if y < widget.get_height() / 2 else "advcfg-drop-after"
        )
        return Gdk.DragAction.MOVE

    # -- the draft on screen -------------------------------------------------

    def sync(self, blocks: List[Block], notes_open, has_global: bool) -> None:
        """Make the cards match *blocks*, changing only what differs."""
        self.syncing = True
        try:
            wanted = {block.uid for block in blocks}
            for uid in [uid for uid in self.cards if uid not in wanted]:
                self.box.remove(self.cards.pop(uid).card)
            previous = self.empty
            for index, block in enumerate(blocks):
                card = self.cards.get(block.uid)
                if card is None:
                    card = self.cards[block.uid] = BlockCard(self, block)
                    self.box.insert_child_after(card.card, previous)
                elif previous.get_next_sibling() is not card.card:
                    self.box.reorder_child_after(card.card, previous)
                card.sync(block, index, len(blocks), block.uid in notes_open)
                previous = card.card
            self.empty.set_visible(not blocks)
            self.add_global_button.set_sensitive(not has_global)
            set_pill(self.block_count, messages.block_count(len(blocks)), "muted")
            if self.selected_uid not in self.cards:
                self.selected_uid = None
        finally:
            self.syncing = False

    def select(self, uid: Optional[int]) -> None:
        previous = self.cards.get(self.selected_uid)
        if previous is not None:
            previous.card.remove_css_class("selected")
        self.selected_uid = uid if uid in self.cards else None
        current = self.cards.get(self.selected_uid)
        if previous is not None and previous is not current:
            self.dim(previous, previous.dims)       # left: dimmed again
        if current is not None:
            current.card.add_css_class("selected")
            self.dim(current, False)        # the block in hand is at full strength

    # -- folding ---------------------------------------------------------------

    def set_folded(self, uid: int, folded: bool) -> None:
        """Fold one block to its header line, or open it: the user's doing."""
        card = self.cards.get(uid)
        if card is not None and card.folded != folded:
            card.folded = folded
            card.show_folded(folded)

    def fold_all(self, folded: bool) -> None:
        for card in self.cards.values():
            card.folded = folded
            card.show_folded(folded)

    def begin_block_drag(self, uid: int) -> None:
        """A block is being dragged: every other one folds to its header
        line while that lasts, so the whole order is short enough to drop
        into. The dragged block stays where the pointer has it."""
        if self._dragging is not None:
            return
        self._dragging = uid
        top = self.card_top(uid)
        for other, card in self.cards.items():
            if other != uid:
                card.show_folded(True, animate=False)
        if top is not None:
            self._after_layout(lambda: self.put_card_at(uid, top))

    def end_block_drag(self) -> None:
        """The drag is over, dropped or not: the blocks are as they were."""
        uid, self._dragging = self._dragging, None
        if uid is None:
            return
        for card in self.cards.values():
            card.show_folded(card.folded, animate=False)
        if uid in self.cards:
            self._after_layout(lambda: self.scroll_to(uid))

    def _after_layout(self, action) -> None:
        """Run *action* once the list has been laid out again. Sizes change
        in the frame's layout step, which comes after its tick: the frame
        after the next one is the first to see them."""
        frames = [0]

        def tick(_widget, _clock) -> bool:
            frames[0] += 1
            if frames[0] < 2:
                return True
            action()
            return False

        self.scroller.add_tick_callback(tick)

    @staticmethod
    def dim(card: "BlockCard", dimmed: bool) -> None:
        """Dim a block that does not apply, or bring it back. It stays as
        it is otherwise: whole, in its place, and editable."""
        if card.dimmed != dimmed:
            card.dimmed = dimmed
            _set_class(card.card, "skipped", dimmed)

    def show_answer(
        self,
        blocks: List[Block],
        evaluation: Optional[SshConfigDraftEvaluation],
        lines: LineMap,
        *,
        keep_open,
        related: Optional[Set[int]] = None,
    ) -> int:
        """Put OpenSSH's answer on the cards. Returns how many blocks apply.

        Every block stays on screen, whole, in the file's order. One that
        does not apply to the host in question is dimmed, unless it is
        selected or in *keep_open* (being worked on).

        *related* stands in for an evaluation while the block picked names
        no host of its own: the header lines of the blocks that apply to a
        host it applies to. The blocks are marked; the option lines are
        not, since what a line does is told per host.
        """
        answered = bool(evaluation and evaluation.evaluated)
        matched = set(evaluation.matched_blocks) if answered else set()
        states = {item.line: item for item in evaluation.lines} if answered else {}
        status = messages.block_status
        if related and not answered:
            answered, matched, status = True, related, messages.related_status
        errors = (
            {error.line: error for error in evaluation.errors if not error.file}
            if evaluation else {}
        )
        count = 0
        for block in blocks:
            card = self.cards.get(block.uid)
            if card is None:
                continue
            header_line = lines.headers.get(block.uid, 0)
            applies = None
            if answered and not block.disabled:
                applies = block.kind == GLOBAL or header_line in matched
            label, style, tooltip = status(block.disabled, applies, block.kind == GLOBAL)
            set_pill(card.status, label, style, tooltip)
            count += bool(applies)
            card.dims = applies is False and block.uid not in keep_open
            self.dim(card, card.dims and block.uid != self.selected_uid)
            error = errors.get(header_line) if header_line else None
            if card.warning.get_visible() != (error is not None):
                card.warning.set_visible(error is not None)
            card.warning.set_label(messages.error_text(error) if error else "")
            for option in block.options:
                row = card.rows.get(option.uid)
                if row is not None:
                    line = lines.options.get(option.uid, 0)
                    row.show(states.get(line) if line else None,
                             errors.get(line) if line else None)
        return count

    # -- focus ---------------------------------------------------------------

    def focus_block(self, uid: int) -> None:
        card = self.cards.get(uid)
        if card is not None:
            (card.patterns or card.card).grab_focus()

    def focus_note(self, uid: int) -> None:
        card = self.cards.get(uid)
        if card is not None and card.note is not None:
            card.note.grab_focus()

    def focus_option(self, uid: int, value: bool = False) -> None:
        for card in self.cards.values():
            row = card.rows.get(uid)
            if row is not None:
                (row.value if value else row.key).grab_focus()
                return

    def scroll_to(self, uid: int) -> None:
        """Bring a block's card to where the eye rests in the list: a
        quarter of the way down, with what comes before it in view above
        and most of the list left for it and what follows. A card too tall
        for that is shown from the top. Focus stays where it is (the
        sidebar's list is being walked)."""
        card = self.cards.get(uid)
        if card is None:
            return
        found, bounds = card.card.compute_bounds(self.box)
        if not found:
            return
        adjustment = self.scroller.get_vadjustment()
        page, height = adjustment.get_page_size(), bounds.get_height()
        gap = self.box.get_margin_top()
        top = bounds.get_y() + gap          # the card's top, as the list scrolls
        rest = page * _CARD_REST
        if height + gap > page - rest:
            rest = gap                      # no room for it there: from the top
        adjustment.set_value(max(0.0, top - rest))

    def card_top(self, uid: int) -> Optional[float]:
        """How far under the top edge of the list a block's card starts, as
        scrolled now (negative: above it). None: no such card, or no layout
        yet."""
        card = self.cards.get(uid)
        if card is None:
            return None
        found, bounds = card.card.compute_bounds(self.box)
        if not found:
            return None
        return (
            bounds.get_y() + self.box.get_margin_top()
            - self.scroller.get_vadjustment().get_value()
        )

    def put_card_at(self, uid: int, offset: float) -> None:
        """Scroll the list so that a block's card starts *offset* under its
        top edge (as near as the list allows)."""
        card = self.cards.get(uid)
        if card is None:
            return
        found, bounds = card.card.compute_bounds(self.box)
        if not found:
            return
        self.scroller.get_vadjustment().set_value(
            max(0.0, bounds.get_y() + self.box.get_margin_top() - offset)
        )

    def _sync_scroll_room(self, *_args) -> None:
        """Leave room under the last block for it to come up to where the
        others rest: without it the list ends where the last card does,
        and the blocks at the end of the file could never be brought up."""
        page = self.scroller.get_vadjustment().get_page_size()
        room = max(14, int(page * (1 - _CARD_REST)) - 60)
        if self.box.get_margin_bottom() != room:
            # Not during the size allocation that told us the page size.
            GLib.idle_add(lambda: (self.box.set_margin_bottom(room), False)[1])

    def reveal(self, uid: int) -> None:
        """Scroll to and flash the option or block with *uid*. A folded
        block opens for an option inside it: it is what was asked for."""
        widget = None
        for card in self.cards.values():
            if uid in card.rows:
                self.set_folded(card.uid, False)
                widget = card.rows[uid].box
                card.rows[uid].value.grab_focus()
                break
        if widget is None and uid in self.cards:
            widget = self.cards[uid].card
            self.focus_block(uid)
        if widget is None:
            return
        widget.add_css_class("advcfg-flash")
        GLib.timeout_add(900, lambda: (widget.remove_css_class("advcfg-flash"), False)[1])

"""Advanced Configuration, Directives view: one block, line by line.

Its title card (name, kind, file, note, on/off, the hosts it applies to,
its problems: shown by the window above all three views), then its lines
in the block's order, as the file has them, each a keyword and a value.
A line an earlier block (or Settings) overrides says so beside it; it is
never refused or rewritten.

Everything is updated in place: the widgets of a block are made when it is
selected and only relabelled after that, so typing never loses the focus.
"""
from __future__ import annotations

from gettext import gettext as _
from typing import Callable, Dict, List, Optional, Sequence

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gtk, Pango  # noqa: E402

from . import advanced_config_fields as fields  # noqa: E402
from .advanced_config_widgets import (  # noqa: E402
    LineDragItem,
    gap_row,
    lift_row,
    mark_unused,
    set_class,
    show_verdict,
    verdict_pill,
)
from .gtk import advanced_config_messages as messages  # noqa: E402
from .connection_dialog_auth import canonical_keyword  # noqa: E402
from .ssh_config_answer import (  # noqa: E402
    NO_EFFECT,
    OVERRIDDEN,
    REJECTED,
    ConfigAnswer,
    LineVerdict,
)
from .ssh_config_blocks import GLOBAL, HOST, Block, LineMap, block_category  # noqa: E402
from .ssh_option_list import OPENSSH_KEYWORDS  # noqa: E402


class AnswerView(ConfigAnswer):
    """What OpenSSH answered, as the Structured view reads it: the shared
    reader (:class:`ConfigAnswer`), plus where each of the draft's lines is."""

    def __init__(self, evaluation=None, lines: Optional[LineMap] = None, *,
                 host: str = "", applies=None, related_hosts=None,
                 current: bool = True) -> None:
        super().__init__(evaluation)
        self.lines = lines or LineMap({}, {})
        self.host = host
        self.applies = applies
        self.related_hosts = related_hosts
        #: The answer is about the text as it is now (not one edit behind).
        self.current = current

    def line_verdict(self, uid: int, keyword: str = "") -> LineVerdict:
        """The mark of an option line, by its uid."""
        return self.verdict(self.lines.options.get(uid, 0), keyword)


def _state_pill(view: "StructuredView") -> Gtk.Label:
    """The pill that says what OpenSSH does with a line. A "No effect" one
    is a link: a click shows the line that is used instead, as a source
    does in Effective."""
    return verdict_pill(lambda target: _follow(view, target))


def _follow(view: "StructuredView", target) -> None:
    file, line = target
    if file:
        view.controller.reveal_source(file, line)
    else:
        view.controller.reveal_line(line)


class _Syncing:
    """Text put into a field by the view is not typing."""

    def __init__(self) -> None:
        self.on = False

    def set_text(self, entry: Gtk.Editable, text: str) -> None:
        if entry.get_text() == text:
            return
        self.on = True
        try:
            entry.set_text(text)
        finally:
            self.on = False


def _entry(placeholder: str = "", width: int = 0, mono: bool = True) -> Gtk.Entry:
    entry = Gtk.Entry()
    entry.set_valign(Gtk.Align.CENTER)
    _escape_leaves(entry)
    if placeholder:
        entry.set_placeholder_text(placeholder)
    if width:
        entry.set_width_chars(width)
    if mono:
        entry.add_css_class("monospace")
    return entry


def _leave_field(widget: Gtk.Widget) -> None:
    """Take the keyboard focus out of a field: what was typed stays."""
    root = widget.get_root()
    if root is not None:
        root.set_focus(None)


def _escape_leaves(entry: Gtk.Entry) -> None:
    """Esc leaves the field, keeping what was typed, as in other GNOME
    apps. (A keyword field's suggestions, open, take the Esc first: their
    controller is added later, so it runs before.)"""

    def pressed(_controller, keyval, _code, _state) -> bool:
        if keyval != Gdk.KEY_Escape:
            return False
        _leave_field(entry)
        return True

    keys = Gtk.EventControllerKey()
    keys.connect("key-pressed", pressed)
    entry.add_controller(keys)


def _icon_button(icon: str, tooltip: str, callback: Callable[[], None]) -> Gtk.Button:
    button = Gtk.Button(icon_name=icon, tooltip_text=tooltip)
    button.add_css_class("flat")
    button.set_valign(Gtk.Align.CENTER)
    button.connect("clicked", lambda *_a: callback())
    return button


class _Suggestions:
    """Known keywords under a keyword field, as it is typed. Never takes
    the focus: typing carries on in the field while the list follows it."""

    def __init__(self, entry: Gtk.Entry, accept: Callable[[str], None]) -> None:
        self.entry = entry
        self.accept = accept
        self.popover: Optional[Gtk.Popover] = None
        self.names: Optional[Gtk.ListBox] = None
        keys = Gtk.EventControllerKey()
        # Ahead of the field's own text widget, which takes Enter (activate)
        # and Tab before a controller of the entry would see them.
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        entry.add_controller(keys)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *_a: self.hide())
        entry.add_controller(focus)

    @staticmethod
    def matches(text: str) -> List[str]:
        typed = text.strip().lower()
        if not typed:
            return []
        starts = [name for name in OPENSSH_KEYWORDS if name.lower().startswith(typed)]
        inside = [n for n in OPENSSH_KEYWORDS if typed in n.lower() and n not in starts]
        found = starts + inside
        if len(found) == 1 and found[0].lower() == typed:
            return []
        return found[:40]

    def typed(self) -> None:
        if not self.entry.get_delegate().has_focus():
            return
        self.show(self.matches(self.entry.get_text()))

    def show(self, names: Sequence[str]) -> None:
        if not names or not self.entry.get_mapped():
            self.hide()
            return
        if self.popover is None:
            popover = Gtk.Popover()
            popover.set_autohide(False)
            popover.set_has_arrow(False)
            popover.set_can_focus(False)
            popover.set_position(Gtk.PositionType.BOTTOM)
            popover.set_halign(Gtk.Align.START)
            popover.add_css_class("menu")
            box = Gtk.ListBox()
            box.set_can_focus(False)
            box.connect("row-activated", lambda _b, row: self._take(row))
            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroller.set_propagate_natural_height(True)
            scroller.set_max_content_height(220)
            scroller.set_min_content_width(220)
            scroller.set_child(box)
            popover.set_child(scroller)
            popover.set_parent(self.entry)
            self.popover, self.names = popover, box
        self.names.remove_all()
        for name in names:
            label = Gtk.Label(label=name, xalign=0)
            for side in ("top", "bottom", "start", "end"):
                getattr(label, f"set_margin_{side}")(6 if side in ("top", "bottom") else 8)
            item = Gtk.ListBoxRow()
            item.set_can_focus(False)
            item.set_child(label)
            self.names.append(item)
        self.popover.popup()

    def hide(self) -> None:
        if self.popover is not None:
            self.popover.popdown()

    def visible(self) -> bool:
        return self.popover is not None and self.popover.get_visible()

    def _take(self, row) -> None:
        self.hide()
        self.accept(row.get_child().get_label())

    def _on_key(self, _controller, keyval, _code, _state) -> bool:
        """Down/Up walk the suggestions (kept in view), Enter or Tab take
        the selected one, Esc closes the list: as the connection editor's
        option list does (ssh_option_list._on_keyword_key)."""
        if not self.visible():
            return False
        selected = self.names.get_selected_row()
        if keyval in (Gdk.KEY_Down, Gdk.KEY_Up):
            step = 1 if keyval == Gdk.KEY_Down else -1
            index = (selected.get_index() if selected is not None else -1 if step > 0 else 0) + step
            target = self.names.get_row_at_index(index)
            if target is not None:
                self.names.select_row(target)
                # Keep the selection in view without moving the focus.
                adjustment = self.popover.get_child().get_vadjustment()
                height = max(target.get_height(), 1)
                top = index * height
                if top < adjustment.get_value():
                    adjustment.set_value(top)
                elif top + height > adjustment.get_value() + adjustment.get_page_size():
                    adjustment.set_value(top + height - adjustment.get_page_size())
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter, Gdk.KEY_Tab):
            if selected is not None:
                self._take(selected)
                return True
            self.hide()                     # nothing chosen: Enter/Tab go on as usual
            return False
        if keyval == Gdk.KEY_Escape:
            self.hide()
            return True
        return False

    def destroy(self) -> None:
        if self.popover is not None:
            self.popover.unparent()
            self.popover = None


def _directive_fields() -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    box.set_margin_top(6)
    box.set_margin_bottom(6)
    box.set_margin_start(6)
    box.set_margin_end(6)
    return box


class LineRow:
    """One line of the block, as every line is shown: its keyword and value
    in two fields, what OpenSSH does with it, and (while it goes unused)
    what is used instead. An empty value shows the value the host checked
    gets from elsewhere, greyed."""

    def __init__(self, view: "StructuredView", uid: int, _keyword: str = "") -> None:
        self.view = view
        self.uid = uid
        self.row = Gtk.ListBoxRow()
        self.row.set_activatable(False)
        box = _directive_fields()
        # The grip: drag the line to another place in the block.
        self.handle = Gtk.Image.new_from_icon_name("list-drag-handle-symbolic")
        self.handle.add_css_class("advcfg-drag-handle")
        self.handle.set_valign(Gtk.Align.CENTER)
        self.handle.set_tooltip_text(_("Drag to reorder"))
        self.handle.set_cursor_from_name("grab")
        source = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        source.connect("prepare", self._on_drag_prepare)
        source.connect("drag-begin", self._on_drag_begin)
        source.connect("drag-end", lambda *_a: view.line_drag_ended())
        source.connect("drag-cancel", lambda *_a: (view.line_drag_ended(), False)[1])
        self.handle.add_controller(source)
        box.append(self.handle)
        self.key = _entry(_("Option"), 24)
        self.key.update_property([Gtk.AccessibleProperty.LABEL], [_("Option")])
        self.key.connect("changed", self._on_changed, "key")
        # Enter in the keyword goes on to the value, as in the connection
        # editor's option list.
        self.key.connect("activate", lambda *_a: self.value.grab_focus())
        self.value = _entry()
        self.value.set_hexpand(True)
        self.value.update_property([Gtk.AccessibleProperty.LABEL], [_("Value")])
        self.value.connect("changed", self._on_changed, "value")
        self.state = _state_pill(view)
        # A key or certificate line: choose its file (see _check_key).
        self.browse = _icon_button("document-open-symbolic", _("Choose a key file"),
                                   lambda: view.controller.browse_identity(self.uid))
        self.browse.set_visible(False)
        # Up and down: the line one place earlier or later in the block.
        self.up = _icon_button("go-up-symbolic", _("Move Up"), lambda: view.move_line(self.uid, -1))
        self.down = _icon_button("go-down-symbolic", _("Move Down"), lambda: view.move_line(self.uid, 1))
        remove = _icon_button("user-trash-symbolic", _("Remove option"),
                              lambda: view.controller.delete_option(self.uid))
        for widget in (self.key, self.value, self.browse, self.state, self.up, self.down, remove):
            box.append(widget)
        # Under the fields, while the line goes unused: what is used instead.
        self.instead = Gtk.Label(xalign=0, wrap=True, use_markup=True, visible=False)
        self.instead.add_css_class("caption")
        self.instead.add_css_class("dim-label")
        self.instead.set_margin_start(14)
        self.instead.set_margin_bottom(6)
        # Under the fields, for a key file OpenSSH will not use: what is
        # wrong with it, and for a private key others can read, Fix.
        self.key_line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, visible=False)
        self.key_line.set_margin_start(14)
        self.key_line.set_margin_bottom(6)
        self.key_status = Gtk.Label(xalign=0)
        self.key_status.set_ellipsize(Pango.EllipsizeMode.END)
        self.key_status.add_css_class("caption")
        self.key_status.add_css_class("error")
        self.key_line.append(self.key_status)
        self.fix = Gtk.Button(label=_("Fix"), valign=Gtk.Align.CENTER, visible=False)
        self.fix.add_css_class("advcfg-fix")
        self.fix.set_tooltip_text(_("Make the key readable by you only (0600), as OpenSSH requires"))
        self.fix.connect("clicked", lambda *_a: self._on_fix())
        self.key_line.append(self.fix)
        self._key_file = None
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        column.append(box)
        column.append(self.key_line)
        column.append(self.instead)
        self.row.set_child(column)
        self.suggestions = _Suggestions(self.key, self._take_keyword)

    def bind(self, option) -> None:
        self.view.syncing.set_text(self.key, option.key)
        self.view.syncing.set_text(self.value, option.value)
        self._check_key(option.key, option.value)

    def _check_key(self, key: str, value: str) -> None:
        """For a key or certificate line: Browse, and what the file on disk
        says about itself (missing; a private key others can read)."""
        private = fields.KEY_FILE_KEYWORDS.get(key.strip().lower())
        self.browse.set_visible(private is not None)
        status = fields.key_file_status(value) if private is not None and value.strip() else None
        problem = status is not None and (
            status.state == fields.KEY_MISSING or (private and status.state == fields.KEY_OPEN)
        )
        self._key_file = status.path if problem else None
        self.key_line.set_visible(problem)
        if problem:
            self.key_status.set_label(messages.key_status_text(status.state, status.mode))
        self.fix.set_visible(problem and status.state == fields.KEY_OPEN)

    def _on_fix(self) -> None:
        if self._key_file:
            self.view.controller.fix_key(self._key_file)
            self._check_key(self.key.get_text(), self.value.get_text())

    def show(self, answer: AnswerView) -> None:
        option = self.view.controller.editor.find_option(self.uid)[1]
        keyword = canonical_keyword(option.keyword) if option else ""
        verdict = answer.line_verdict(self.uid, keyword)
        show_verdict(self.state, verdict)
        # A line that does not take effect is struck through and tinted
        # yellow, as overridden declarations are in browser developer tools:
        # valid, just not used. Red (error) is for a line OpenSSH rejects.
        for entry in (self.key, self.value):
            set_class(entry, "error", verdict.state == REJECTED)
            mark_unused(entry, verdict.unused)
        # Empty: what the host gets from elsewhere, greyed in the field.
        inherited = answer.inherited(keyword)[0] if keyword else ""
        self.value.set_placeholder_text(inherited or _("Value"))
        # Under the fields, while the line goes unused: what is used
        # instead ("" for a pattern block: no one host's answer says it).
        text = verdict.note_markup if option and verdict.unused else ""
        if self.instead.get_label() != text:
            self.instead.set_label(text)
        self.instead.set_visible(bool(text))

    def set_place(self, first: bool, last: bool) -> None:
        """Where the line stands in its block: no Up for the first, no
        Down for the last."""
        self.up.set_sensitive(not first)
        self.down.set_sensitive(not last)

    def _on_drag_prepare(self, _source, x, y):
        # Where the row was grabbed (the grip's point, in the row): the
        # carried copy is held there.
        found, point = self.handle.compute_point(self.row, Graphene.Point().init(x, y))
        self._grab = (point.x, point.y) if found else (x, y)
        return Gdk.ContentProvider.new_for_value(LineDragItem(self.uid))

    def _on_drag_begin(self, source, drag) -> None:
        """GNOME's way (Settings, Nautilus): a solid copy of the row is
        carried where it was grabbed, and the row itself is hidden while
        the list makes room for it where it would land."""
        lift_row(source, drag, self.row, *getattr(self, "_grab", (0, 0)))
        self.view.line_drag_began(self.uid, self.row.get_height())
        self.row.set_visible(False)

    def _on_changed(self, entry, field: str) -> None:
        if self.view.syncing.on:
            return
        self.view.controller.option_typed(self.uid, field, entry.get_text())
        self._check_key(self.key.get_text(), self.value.get_text())
        if field == "key":
            self.suggestions.typed()

    def _take_keyword(self, name: str) -> None:
        self.key.set_text(name)
        self.key.set_position(-1)
        self.suggestions.hide()
        self.value.grab_focus()

    def destroy(self) -> None:
        self.suggestions.destroy()


#: The keywords Add Option offers at a click, in the order it lists them.
#: The common options' menu, in sections: the connection, keys and agent,
#: forwarding.
_COMMON = (
    ("hostname", "user", "port", "proxyjump"),
    ("identityfile", "identitiesonly", "forwardagent"),
    ("localforward", "remoteforward", "dynamicforward"),
)


class _Page(Gtk.ScrolledWindow):
    """The block's groups, one under the other, the full width of the view
    with the Effective view's margins, so the lines line up with the title
    card above them as Effective's list does."""

    def __init__(self) -> None:
        super().__init__()
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.set_vexpand(True)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        for side in ("top", "bottom", "start", "end"):
            getattr(self.box, f"set_margin_{side}")(16)
        self.set_child(self.box)

    def add(self, widget: Gtk.Widget) -> None:
        self.box.append(widget)

    def scroll_to_top(self) -> None:
        self.get_vadjustment().set_value(0)


def _boxed_list() -> Gtk.ListBox:
    listbox = Gtk.ListBox()
    listbox.set_selection_mode(Gtk.SelectionMode.NONE)
    listbox.add_css_class("boxed-list")
    return listbox


#: The kinds of block (ssh_config_blocks.block_category) the title card's
#: type line is coloured for.
_KIND_CLASSES = ("connection", "pattern", "match_rule", "all_hosts")


def _card_tile(icon: str, title: str) -> tuple:
    """A tile of the title card: ``(tile, content box, severity dot)``."""
    tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    tile.add_css_class("advcfg-tile")
    heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    image = Gtk.Image.new_from_icon_name(icon)
    image.add_css_class("advcfg-tile-icon")
    heading.append(image)
    label = Gtk.Label(label=title, xalign=0)
    label.add_css_class("caption-heading")
    label.add_css_class("advcfg-tile-title")
    heading.append(label)
    dot = Gtk.Box(valign=Gtk.Align.CENTER, visible=False)
    dot.add_css_class("advcfg-dot")
    heading.append(dot)
    tile.append(heading)
    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    tile.append(content)
    return tile, content, dot


class StructuredView(Gtk.Box):
    """One block, line by line. *controller* supplies the edits (see the
    calls)."""

    def __init__(self, controller) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.controller = controller
        self.syncing = _Syncing()
        self.uid: Optional[int] = None
        self.kind = ""
        #: Lines added with Add Option: they stay plain lines (see
        #: advanced_config_fields).
        self._answer = AnswerView()
        #: The row of each line of the block on screen, by uid.
        self._rows: Dict[int, object] = {}

        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.append(self.stack)
        empty = Adw.StatusPage(
            icon_name="document-properties-symbolic",
            title=_("No Block Selected"),
            description=_("Choose a block in the sidebar, or add one."),
        )
        self.stack.add_named(empty, "empty")
        self.page = _Page()
        # A click on the page around the fields (not on one, nor on a
        # button: those take the click) leaves the field being edited.
        click = Gtk.GestureClick()
        click.connect("pressed", lambda *_a: _leave_field(self.page))
        self.page.add_controller(click)
        self.stack.add_named(self.page, "block")

        # -- the block itself: the title card --------------------------------------
        # What the block is (its name, read only: it follows the Host or
        # Match row as it is typed; what kind of block; its file), Note,
        # on/off and the actions; the note as the comment it is; and tiles:
        # what it applies to, how many lines, and its problems when it has
        # any. The window shows the card above all three views.
        self.block_group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.block_group.add_css_class("advcfg-title-card")
        # Shown with a block (see show): nothing to head before there is one.
        self.block_group.set_visible(False)
        # A card in libadwaita's OSD style, whole (dimmed while the block is off),
        # holds the name and the actions; the note, notices and tiles sit
        # under it on the page.
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        line.add_css_class("advcfg-card-band")
        line.add_css_class("osd")
        self.band = line
        # The name, and under it what kind of block and its file.
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        names.set_valign(Gtk.Align.CENTER)
        self.head_title = Gtk.Label(xalign=0)
        self.head_title.add_css_class("monospace")
        self.head_title.add_css_class("advcfg-card-title")
        self.head_title.set_ellipsize(Pango.EllipsizeMode.END)
        names.append(self.head_title)
        self.head_caption = Gtk.Label(xalign=0)
        self.head_caption.add_css_class("dim-label")
        self.head_caption.add_css_class("caption")
        self.head_caption.set_ellipsize(Pango.EllipsizeMode.END)
        names.append(self.head_caption)
        # The file the block is in, on a line of its own.
        file_line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.head_file = Gtk.Label(xalign=0)
        self.head_file.add_css_class("dim-label")
        self.head_file.add_css_class("caption")
        self.head_file.add_css_class("monospace")
        self.head_file.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        file_line.append(self.head_file)
        names.append(file_line)
        self.head_file_line = file_line
        line.append(names)
        self.note_button = Gtk.MenuButton(
            icon_name="document-edit-symbolic", popover=self._build_note_popover(),
        )
        self.note_button.add_css_class("flat")
        self.note_button.set_valign(Gtk.Align.CENTER)
        line.append(self.note_button)
        self.enabled_row = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.enabled_row.set_tooltip_text(
            _("On: OpenSSH reads this block. Off: it is written as comments, which OpenSSH ignores.")
        )
        self.enabled_row.update_property([Gtk.AccessibleProperty.LABEL], [_("Enabled")])
        self.enabled_row.connect("notify::active", self._on_enabled)
        line.append(self.enabled_row)
        self.duplicate_button = Gtk.Button(icon_name="edit-copy-symbolic",
                                           tooltip_text=_("Duplicate Block"))
        self.duplicate_button.add_css_class("flat")
        self.duplicate_button.set_valign(Gtk.Align.CENTER)
        self.duplicate_button.connect("clicked", lambda *_a: controller.duplicate_block(self.uid))
        self.delete_button = Gtk.Button(icon_name="user-trash-symbolic",
                                        tooltip_text=_("Delete Block"))
        self.delete_button.add_css_class("flat")
        self.delete_button.set_valign(Gtk.Align.CENTER)
        self.delete_button.connect("clicked", lambda *_a: controller.delete_block(self.uid))
        line.append(self.duplicate_button)
        line.append(self.delete_button)
        self.block_group.append(line)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        body.add_css_class("advcfg-card-body")
        self.block_group.append(body)

        # The note, read only, as the comment it is in the file.
        self.note_label = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.note_label.add_css_class("advcfg-comment")
        body.append(self.note_label)
        # Off: one line that says so (the tiles and lines are dimmed with it).
        self.disabled_label = Gtk.Label(
            label=_("Disabled: written as comments, which OpenSSH ignores."), xalign=0,
        )
        self.disabled_label.add_css_class("advcfg-disabled-note")
        body.append(self.disabled_label)
        #: What is wrong with the block's own line, or that it does not
        #: apply to a host kept in question.
        self.error_row = Gtk.Label(xalign=0, wrap=True)
        self.error_row.add_css_class("error")
        body.append(self.error_row)

        # The tiles share the full width: one alone spans it, two halve it.
        self.tiles = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, homogeneous=True)
        # Applies To: the hosts of a pattern, Match or wildcard block, each
        # a link (none for a block that names its own hosts).
        self.applies_box, content, _dot = _card_tile("network-server-symbolic", _("Applies To"))
        self.applies_box.set_hexpand(True)
        self.applies_label = Gtk.Label(xalign=0)
        self.applies_label.add_css_class("dim-label")
        self.applies_label.set_ellipsize(Pango.EllipsizeMode.END)
        content.append(self.applies_label)
        # The flow box needs the tile width to measure its wrapped height.
        self.applies_chips = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                                         max_children_per_line=12)
        self.applies_chips.set_column_spacing(6)
        self.applies_chips.set_row_spacing(6)
        content.append(self.applies_chips)
        self.tiles.append(self.applies_box)
        # Problems: shown only when there are some; a click shows the first.
        self.problems_tile, content, self.problems_dot = _card_tile(
            "dialog-warning-symbolic", _("Problems"),
        )
        self.problems_tile.set_hexpand(True)
        self.problems_label = Gtk.Label(xalign=0, wrap=True)
        content.append(self.problems_label)
        self.problems_tile.set_cursor_from_name("pointer")
        self.problems_tile.set_tooltip_text(_("Show the first problem"))
        click = Gtk.GestureClick()
        click.connect("released", lambda *_a: self._show_first_problem())
        self.problems_tile.add_controller(click)
        self._first_problem: Optional[int] = None
        self.tiles.append(self.problems_tile)
        body.append(self.tiles)
        self._file_name = ""

        # -- the lines, in the block's order ------------------------------------
        group = Adw.PreferencesGroup()
        #: The lines, dimmed while the block is off.
        self.options_group = group
        self.options_list = _boxed_list()
        # Lines dragged by their grip: a gap opens where one would land, the
        # connection list's own placeholder (as the list of blocks does).
        self._line_drag: Optional[int] = None
        self._gap: Optional[Gtk.ListBoxRow] = None
        self._gap_at: Optional[tuple] = None
        self._gap_clear = 0
        target = Gtk.DropTarget.new(LineDragItem, Gdk.DragAction.MOVE)
        target.connect("motion", self._on_line_motion)
        target.connect("leave", lambda *_a: self._close_gap_later())
        target.connect("drop", self._on_line_drop)
        self.options_list.add_controller(target)
        # The block's Host or Match line: a row like the others, first.
        self.header_row = Gtk.ListBoxRow(activatable=False)
        header_fields = _directive_fields()
        # Reserve the grip column without offering to reorder the block header.
        grip_space = Gtk.Image(icon_name="list-drag-handle-symbolic",
                              accessible_role=Gtk.AccessibleRole.PRESENTATION)
        grip_space.add_css_class("advcfg-drag-handle")
        grip_space.set_opacity(0)
        header_fields.append(grip_space)
        self.header_keyword = _entry(width=24)
        self.header_keyword.set_editable(False)
        self.header_keyword.update_property([Gtk.AccessibleProperty.LABEL], [_("Option")])
        header_fields.append(self.header_keyword)
        self.patterns_row = _entry()
        self.patterns_row.set_hexpand(True)
        self.patterns_row.connect("changed", self._on_patterns_changed)
        header_fields.append(self.patterns_row)
        self.header_row.set_child(header_fields)
        self.options_list.append(self.header_row)
        self.add_option_row = Adw.ActionRow(title=_("Add Option"))
        self.add_option_row.add_prefix(Gtk.Image.new_from_icon_name("list-add-symbolic"))
        self.add_option_row.set_activatable(True)
        self.add_option_row.connect(
            "activated", lambda *_a: controller.add_option(self.uid)
        )
        self.add_option_row.add_suffix(self._common_menu())
        self.options_list.append(self.add_option_row)
        group.add(self.options_list)
        self.page.add(group)

    def _common_menu(self) -> Gtk.MenuButton:
        """The options most blocks set, a click away: a menu model, so it
        looks and behaves as the app's other menus (GtkPopoverMenu), in
        sections of options that go together."""
        actions = Gio.SimpleActionGroup()
        add = Gio.SimpleAction.new("add-common", GLib.VariantType.new("s"))
        add.connect("activate", lambda _a, keyword: self._add_common(keyword.get_string()))
        actions.add_action(add)
        self.insert_action_group("directives", actions)
        menu = Gio.Menu()
        for section in _COMMON:
            items = Gio.Menu()
            for keyword in section:
                item = Gio.MenuItem.new(fields.SPELLING[keyword], None)
                item.set_action_and_target_value(
                    "directives.add-common", GLib.Variant.new_string(keyword)
                )
                items.append_item(item)
            menu.append_section(None, items)
        button = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu,
                                tooltip_text=_("Add a Common Option"))
        button.add_css_class("flat")
        button.set_valign(Gtk.Align.CENTER)
        return button

    def _add_common(self, keyword: str) -> None:
        if self.uid is None:
            return
        value = "yes" if keyword in fields.SWITCHES else ""
        self.controller.add_option(self.uid, fields.SPELLING[keyword], value)

    # -- moving lines ---------------------------------------------------------------

    def move_line(self, uid: int, delta: int) -> None:
        """Up (-1) or Down (+1): the line one place earlier or later."""
        block = self.controller.editor.find_block(self.uid)
        uids = [option.uid for option in block.options] if block is not None else []
        if uid not in uids:
            return
        index = uids.index(uid) + delta
        if not 0 <= index < len(uids):
            return
        # move_option takes the place as the list stands before the move.
        self.controller.move_option(uid, index + 1 if delta > 0 else index)
        row = self._rows.get(uid)
        if row is not None:
            (row.up if delta < 0 else row.down).grab_focus()

    def line_drag_began(self, uid: int, height: int = 0) -> None:
        self._line_drag = uid
        self._open_gap(None)                  # an earlier gap, still waiting to close
        # Room of the carried row's height (Nautilus's placeholder row).
        self._gap = gap_row(height)

    def line_drag_ended(self) -> None:
        carried, self._line_drag = self._line_drag, None
        row = self._rows.get(carried)
        if row is not None:
            row.row.set_visible(True)
        self._close_gap_later()

    def _gap_row(self) -> Gtk.ListBoxRow:
        if self._gap is None:
            self._gap = gap_row(0)
        return self._gap

    def _line_place(self, y: float):
        """``(uid, before)`` of the line a drop at *y* lands beside."""
        row = self.options_list.get_row_at_y(int(y))
        if row is None:
            return None
        if row is self._gap:
            return self._gap_at                    # over the gap: it stays
        uids = {line.row: uid for uid, line in self._rows.items()}
        order = [option.uid for option in
                 (self.controller.editor.find_block(self.uid).options if self.uid else [])]
        if not order:
            return None
        if row is self.header_row:
            return (order[0], True)
        if row is self.add_option_row:
            return (order[-1], False)
        uid = uids.get(row)
        if uid is None:
            return None
        found, bounds = row.compute_bounds(self.options_list)
        return (uid, found and y < bounds.get_y() + bounds.get_height() / 2)

    def _open_gap(self, place) -> None:
        if self._gap_clear:
            GLib.source_remove(self._gap_clear)
            self._gap_clear = 0
        gap = self._gap_row()
        if place is None:
            self._gap_at = None
            if gap.get_parent() is not None:
                self.options_list.remove(gap)
            return
        if gap.get_parent() is not None:
            if self._gap_at == place:
                return
            self.options_list.remove(gap)
        self._gap_at = place
        uid, before = place
        index = self._rows[uid].row.get_index()
        self.options_list.insert(gap, index if before else index + 1)

    def _close_gap_later(self) -> None:
        """Close the gap once GTK has finished the crossing it is in (see
        the sidebar's _clear_drop_later and the connection list's #1334)."""
        if not self._gap_clear:
            self._gap_clear = GLib.idle_add(self._close_gap_now)

    def _close_gap_now(self) -> bool:
        self._gap_clear = 0
        self._open_gap(None)
        return GLib.SOURCE_REMOVE

    def _on_line_motion(self, _target, _x, y):
        place = self._line_place(y)
        if place is None or place[0] == self._line_drag:
            self._open_gap(None)
            return Gdk.DragAction.MOVE if place is not None else 0
        self._open_gap(place)
        return Gdk.DragAction.MOVE

    def _on_line_drop(self, _target, value, _x, y) -> bool:
        uid = value.uid if isinstance(value, LineDragItem) else None
        place = self._gap_at or self._line_place(y)
        # Out now: the lines are put in order by index straight after.
        self._open_gap(None)
        block = self.controller.editor.find_block(self.uid)
        if uid is None or place is None or block is None or place[0] == uid:
            return False
        order = [option.uid for option in block.options]
        target, before = place
        if target not in order or uid not in order:
            return False
        self.controller.move_option(uid, order.index(target) + (0 if before else 1))
        return True

    # -- the rows (for the controller and the tests) ----------------------------

    @property
    def line_rows(self) -> Dict[int, "LineRow"]:
        """The row of each line of the block on screen, by uid."""
        return dict(self._rows)

    def _drop_row(self, uid: int) -> None:
        row = self._rows.pop(uid)
        self.options_list.remove(row.row)
        if hasattr(row, "destroy"):
            row.destroy()

    def show(self, block: Optional[Block]) -> None:
        """Show *block* (None: none), in place when it is the one shown."""
        if block is None:
            self.uid = None
            self.stack.set_visible_child_name("empty")
            self.block_group.set_visible(False)
            return
        self.block_group.set_visible(True)
        if block.uid != self.uid:
            self.uid = block.uid
            for uid in list(self._rows):
                self._drop_row(uid)
            self.page.scroll_to_top()
        self.stack.set_visible_child_name("block")
        self._sync(block)
        self.show_answer(self._answer)

    def _sync(self, block: Block) -> None:
        self.kind = block.kind
        options = {option.uid: option for option in block.options}
        is_global = block.kind == GLOBAL
        # Global options have no Host or Match line.
        self.header_row.set_visible(not is_global)
        keyword = "Host" if block.kind == HOST else "Match"
        if self.header_keyword.get_text() != keyword:
            self.header_keyword.set_text(keyword)
            self.patterns_row.set_tooltip_text(
                _("The names and patterns this block is for") if block.kind == HOST
                else _("The conditions this block is for")
            )
        self.patterns_row.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Host patterns") if block.kind == HOST else _("Match conditions")],
        )
        self._show_heading(block)
        self.note_button.set_visible(not is_global)
        self.duplicate_button.set_visible(not is_global)
        self.syncing.set_text(self.patterns_row, block.patterns)
        self._show_note(block.note if not is_global else "")
        if self.enabled_row.get_active() == block.disabled:
            self.syncing.on = True
            try:
                self.enabled_row.set_active(not block.disabled)
            finally:
                self.syncing.on = False
        self.disabled_label.set_visible(block.disabled)
        set_class(self.band, "advcfg-band-off", block.disabled)
        set_class(self.options_group, "advcfg-dimmed", block.disabled)
        set_class(self.tiles, "advcfg-dimmed", block.disabled)

        # Every line in the block's order, each a keyword and a value.
        for uid in [uid for uid in self._rows if uid not in options]:
            self._drop_row(uid)
        for index, option in enumerate(block.options, start=1):  # after the Host line
            uid = option.uid
            row = self._rows.get(uid)
            if row is None:
                row = self._rows[uid] = LineRow(self, uid)
                self.options_list.insert(row.row, index)
            elif row.row.get_index() != index:
                self.options_list.remove(row.row)
                self.options_list.insert(row.row, index)
            row.bind(option)
            row.set_place(index == 1, index == len(block.options))

    # -- OpenSSH's answer -------------------------------------------------------

    def show_answer(self, answer: AnswerView) -> None:
        self._answer = answer
        if self.uid is None:
            return
        for row in self._rows.values():
            row.show(answer)
        self._show_status(answer)

    def set_file_name(self, name: str) -> None:
        """The file the block is in, as the title card says it."""
        self._file_name = name
        block = self.controller.editor.find_block(self.uid) if self.uid is not None else None
        if block is not None:
            self._show_heading(block)

    def _show_heading(self, block: Block) -> None:
        title = messages.block_list_title(block.kind, block.patterns)
        set_class(self.head_title, "monospace", block.kind != GLOBAL)
        if self.head_title.get_label() != title:
            self.head_title.set_label(title)
            self.head_title.set_tooltip_text(messages.block_title(block.kind, block.patterns))
        category = block_category(block.kind, block.patterns)
        kind = messages.block_category_label(
            category, len(block.patterns.split()) or 1, block.kind,
        )
        # The type line takes a pale colour per kind of block (the card is
        # dark in both themes, so libadwaita's fixed light shades read on it).
        for name in _KIND_CLASSES:
            set_class(self.head_caption, f"advcfg-kind-{name}", name == category)
        if self.head_caption.get_label() != kind:
            self.head_caption.set_label(kind)
        if self.head_file.get_label() != self._file_name:
            self.head_file.set_label(self._file_name)
            self.head_file.set_tooltip_text(self._file_name or None)
        self.head_file_line.set_visible(bool(self._file_name))

    def _show_status(self, answer: AnswerView) -> None:
        """Only what says something: the hosts a pattern or Match block
        applies to, that the block does not apply to a host kept in
        question, what OpenSSH rejects in its line, and the problems of
        its lines."""
        block = self.controller.editor.find_block(self.uid)
        disabled = block is not None and block.disabled
        self._show_applies(block, None if disabled else answer.related_hosts)
        header = self.controller.editor.lines.headers.get(self.uid, 0)
        error = answer.errors.get(header) if header else None
        if error is not None:
            text = messages.error_text(error)
        elif (not disabled and answer.evaluation is not None and answer.applies is False
              and block is not None and block.kind != GLOBAL):
            text = messages.applies_text(False, answer.host)
        else:
            text = ""
        self.error_row.set_label(text)
        self.error_row.set_visible(bool(text))
        self._show_problems(block, answer, header_error=error is not None)

    def _show_problems(self, block, answer: AnswerView, *, header_error: bool) -> None:
        unused = overridden = rejected = 0
        first = None
        for option in (block.options if block is not None and not block.disabled else ()):
            state = answer.line_verdict(option.uid).state
            if state == REJECTED:
                rejected += 1
            elif state == NO_EFFECT:
                unused += 1
            elif state == OVERRIDDEN:
                overridden += 1
            else:
                continue
            first = option.uid if first is None else first
        rejected += int(header_error)
        self._first_problem = first
        parts = []
        if rejected:
            parts.append(messages.rejected_problem(rejected))
        if unused:
            parts.append(messages.precedence_problem(unused))
        if overridden:
            parts.append(messages.settings_problem(overridden))
        self.problems_tile.set_visible(bool(parts))
        # One sentence a line: each says what is wrong and why.
        self.problems_label.set_label("\n".join(parts))
        style = "error" if (rejected or unused) else "purple"
        for name in ("error", "purple"):
            on = bool(parts) and name == style
            set_class(self.problems_tile, f"advcfg-attn-{name}", on)
            set_class(self.problems_dot, f"advcfg-attn-{name}", on)
        self.problems_dot.set_visible(bool(parts))

    def _show_first_problem(self) -> None:
        if self._first_problem is None:
            return
        self.controller.reveal_option(self._first_problem)

    #: Hosts named before the rest are counted ("+3").
    _APPLIES_SHOWN = 8

    def _show_applies(self, block, hosts) -> None:
        """A pattern, Match or wildcard block: the hosts it applies to, as
        chips. A block that names its hosts (or the global options) has no
        such tile: its title already says what it applies to."""
        if block is None:
            return
        self.applies_box.set_visible(hosts is not None)
        text = messages.applies_to_none() if hosts is not None and not hosts else ""
        self.applies_label.set_label(text)
        self.applies_label.set_visible(bool(text))
        shown = tuple(hosts or ())
        if getattr(self, "_applies", None) == (hosts is None, shown):
            return
        self._applies = (hosts is None, shown)
        self.applies_chips.set_visible(bool(shown))
        child = self.applies_chips.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.applies_chips.remove(child)
            child = following
        for name in shown[:self._APPLIES_SHOWN]:
            # A plain button with the theme's own look: no custom styling,
            # which drew unreadable pills under some bundled themes (AppImage).
            chip = Gtk.Button(label=name, halign=Gtk.Align.START,
                              tooltip_text=messages.show_host_block(name))
            chip.connect("clicked", lambda *_a, n=name: self.controller.follow_host(n))
            self.applies_chips.append(chip)
        if len(shown) > self._APPLIES_SHOWN:
            rest = shown[self._APPLIES_SHOWN:]
            more = Gtk.Label(label=f"+{len(rest)}", halign=Gtk.Align.START, tooltip_text=", ".join(rest))
            more.add_css_class("dim-label")
            self.applies_chips.append(more)

    # -- gestures ----------------------------------------------------------------

    def _on_patterns_changed(self, row) -> None:
        if not self.syncing.on and self.uid is not None:
            self.controller.patterns_typed(self.uid, row.get_text())
            # The name above follows the typing (the view is not shown
            # again for an edit of its own).
            block = self.controller.editor.find_block(self.uid)
            if block is not None:
                self._show_heading(block)

    # -- the note -------------------------------------------------------------

    def _build_note_popover(self) -> Gtk.Popover:
        """Write the note, then Save it; Cancel leaves it as it was, and
        Remove takes it away."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(6)
        self.note_row = Gtk.Entry(width_chars=36, placeholder_text=_("A comment above the block"))
        self.note_row.update_property([Gtk.AccessibleProperty.LABEL], [_("Note")])
        self.note_row.connect("activate", lambda *_a: self._save_note())
        box.append(self.note_row)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.remove_note_button = Gtk.Button(label=_("Remove"))
        self.remove_note_button.add_css_class("destructive-action")
        self.remove_note_button.connect("clicked", lambda *_a: self._save_note(""))
        buttons.append(self.remove_note_button)
        spacer = Gtk.Box(hexpand=True)
        buttons.append(spacer)
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda *_a: self.note_popover.popdown())
        buttons.append(cancel)
        save = Gtk.Button(label=_("Save"))
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda *_a: self._save_note())
        buttons.append(save)
        box.append(buttons)
        self.note_popover = Gtk.Popover(child=box)
        # Opened: the note as it is (what was typed and cancelled is gone).
        self.note_popover.connect("show", lambda *_a: self._open_note())
        return self.note_popover

    def _note(self) -> str:
        block = self.controller.editor.find_block(self.uid) if self.uid is not None else None
        return block.note if block is not None else ""

    def _open_note(self) -> None:
        note = self._note()
        self.note_row.set_text(note)
        self.remove_note_button.set_visible(bool(note))
        self.note_row.grab_focus()

    def _save_note(self, text: Optional[str] = None) -> None:
        text = self.note_row.get_text() if text is None else text
        self.note_popover.popdown()
        if self.uid is not None and text.strip() != self._note():
            self.controller.note_typed(self.uid, text.strip())
            self._show_note(self._note())

    def _show_note(self, note: str) -> None:
        shown = f"# {note}" if note else ""
        if self.note_label.get_label() != shown:
            self.note_label.set_label(shown)
        self.note_label.set_visible(bool(note))
        self.note_button.set_tooltip_text(_("Edit Note") if note else _("Add Note"))

    def _on_enabled(self, row, _pspec) -> None:
        if self.syncing.on or self.uid is None:
            return
        block = self.controller.editor.find_block(self.uid)
        if block is not None and block.disabled == row.get_active():
            self.controller.toggle_block(self.uid)

    # -- focus, for the controller -------------------------------------------------

    def focus_patterns(self) -> None:
        if self.header_row.get_visible():
            self.patterns_row.grab_focus()

    def option_widget(self, uid: int) -> Optional[Gtk.Widget]:
        """The field showing option *uid*, to focus or flash."""
        row = self._rows.get(uid)
        return row.value if row is not None else None

    def line_status(self, uid: int) -> Optional[str]:
        """What is said beside option *uid* (No effect, Overridden,
        Rejected), or None: nothing needs saying, or it is not shown."""
        row = self._rows.get(uid)
        if row is None:
            return None
        return row.state.get_label() if row.state.get_visible() else None

    def focus_option(self, uid: int, *, key: bool = False) -> None:
        if key and uid in self._rows:
            self._rows[uid].key.grab_focus()
            return
        widget = self.option_widget(uid)
        if widget is not None:
            widget.grab_focus()

    def reveal(self, uid: int) -> None:
        """Bring the field of option or block *uid* into view and flash it."""
        widget = self.option_widget(uid)
        if widget is None:
            widget = self.header_row if self.header_row.get_visible() else self.head_title
            self.page.scroll_to_top()
        else:
            # The row holding the field: the one a boxed list scrolls to.
            row = widget
            while row is not None and not isinstance(row, Gtk.ListBoxRow):
                row = row.get_parent()
            target = row or widget
            target.grab_focus()
        widget.add_css_class("advcfg-flash")
        GLib.timeout_add(900, lambda: (widget.remove_css_class("advcfg-flash"), False)[1])

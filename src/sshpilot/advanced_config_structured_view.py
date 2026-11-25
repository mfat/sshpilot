"""Advanced Configuration, Structured view: one block, as fields.

The block's header (its Host patterns or Match conditions), its note and
whether it is on; then its lines, in the block's order, as the file has
them. A line gets a control that suits it
(HostName, User, Port, ProxyJump as fields, IdentitiesOnly and ForwardAgent
as yes/no, identity files with their key check, forwards); every other
line is a keyword and a value. Which control a line gets is decided in
:mod:`advanced_config_fields`: no line is dropped or rewritten to fit one.
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
from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402

from . import advanced_config_fields as fields  # noqa: E402
from .advanced_config_widgets import pill, set_class, set_pill  # noqa: E402
from .api.models.connections import SshConfigDraftLineState as State  # noqa: E402
from .gtk import advanced_config_messages as messages  # noqa: E402
from .ssh_config_blocks import GLOBAL, HOST, Block, LineMap, block_category  # noqa: E402
from .ssh_option_list import OPENSSH_KEYWORDS  # noqa: E402


def _descriptions() -> Dict[str, str]:
    return {
        "hostname": _("The real host name or IP address to connect to"),
        "user": _("The user name to log in as"),
        "port": _("The port the SSH server listens on"),
        "proxyjump": _("Connect through these jump hosts, in order"),
        "identitiesonly": _("Offer only the identity files set in the configuration"),
        "forwardagent": _("Let the remote host use your local SSH agent"),
    }


class AnswerView:
    """What OpenSSH answered, as the Structured view reads it."""

    def __init__(self, evaluation=None, lines: Optional[LineMap] = None, *,
                 host: str = "", applies=None, related_hosts=None,
                 current: bool = True) -> None:
        #: The answer when OpenSSH evaluated the draft; None otherwise (no
        #: host, a rejected draft, a command waiting for consent).
        self.evaluation = evaluation if evaluation is not None and evaluation.evaluated else None
        self.lines = lines or LineMap({}, {})
        self.host = host
        self.applies = applies
        self.related_hosts = related_hosts
        #: The answer is about the text as it is now (not one edit behind).
        self.current = current
        self.values: Dict[str, List[str]] = {}
        self.sources: Dict[str, object] = {}
        self.forced: set = set()
        self.defaults: set = set()
        self.states: Dict[int, object] = {}
        # A rejected line is said whether or not the rest was evaluated.
        self.errors: Dict[int, object] = {
            error.line: error for error in getattr(evaluation, "errors", ()) or ()
            if not error.file
        }
        if self.evaluation is not None:
            for item in self.evaluation.values:
                self.values.setdefault(item.key, []).append(item.value)
            self.sources = {source.key: source for source in self.evaluation.sources}
            self.forced = {item.key for item in self.evaluation.forced}
            self.defaults = set(self.evaluation.defaults)
            self.states = {item.line: item for item in self.evaluation.lines}

    def inherited(self, keyword: str) -> tuple:
        """``(value, where it comes from)`` for a field this block leaves
        empty. Empty strings when there is no answer."""
        values = self.values.get(keyword)
        if not values:
            return "", ""
        source = messages.source_label(
            self.sources.get(keyword),
            forced=keyword in self.forced, default=keyword in self.defaults,
        )
        return ", ".join(values), source

    def line_state(self, uid: int) -> tuple:
        """``(state item, error)`` of an option line, by its uid."""
        line = self.lines.options.get(uid, 0)
        if not line:
            return None, None
        return self.states.get(line), self.errors.get(line)


def _state_pill(view: "StructuredView") -> Gtk.Label:
    """The pill that says what OpenSSH does with a line. A "Not used" one
    is a link: a click shows the line that is used instead, as a source
    does in Effective."""
    label = pill(style="warning")
    label._target = None
    click = Gtk.GestureClick()
    click.connect("released", lambda *_a: _follow_pill(view, label))
    label.add_controller(click)
    return label


def _follow_pill(view: "StructuredView", label: Gtk.Label) -> None:
    target = label._target
    if target is None:
        return
    file, line = target
    if file:
        view.controller.reveal_source(file, line)
    else:
        view.controller.reveal_line(line)


def _set_pill_target(label: Gtk.Label, target) -> None:
    if getattr(label, "_target", None) == target:
        return
    label._target = target
    label.set_cursor_from_name("pointer" if target else None)
    set_class(label, "advcfg-pill-link", target is not None)


def _line_pill(label: Gtk.Label, item, error) -> bool:
    """Say what OpenSSH does with a line: only what needs saying (not used,
    overridden, rejected). Returns whether the line is unused."""
    if error is not None:
        set_pill(label, _("Rejected"), "warning", messages.error_text(error))
        _set_pill_target(label, None)
        return False
    if item is not None and item.state in (State.SHADOWED, State.FORCED):
        text = _("Not used") if item.state is State.SHADOWED else messages.state_label(item.state)
        tooltip = messages.state_tooltip(item)
        target = None
        if item.state is State.SHADOWED and item.decided_line:
            target = (item.decided_file, item.decided_line)
            tooltip += "\n" + messages.show_line_hint()
        set_pill(label, text, messages.STATE_STYLES[item.state], tooltip)
        _set_pill_target(label, target)
        return True
    set_pill(label, "", "muted")
    _set_pill_target(label, None)
    return False


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
    if placeholder:
        entry.set_placeholder_text(placeholder)
    if width:
        entry.set_width_chars(width)
    if mono:
        entry.add_css_class("monospace")
    return entry


def _icon_button(icon: str, tooltip: str, callback: Callable[[], None]) -> Gtk.Button:
    button = Gtk.Button(icon_name=icon, tooltip_text=tooltip)
    button.add_css_class("flat")
    button.set_valign(Gtk.Align.CENTER)
    button.connect("clicked", lambda *_a: callback())
    return button


class SingleRow:
    """The first line of a keyword of one value (HostName, User, Port,
    ProxyJump), edited in a field. Emptied, the line stays (OpenSSH says
    it is missing its value) until it is removed: the row never vanishes
    under the pointer."""

    kind = fields.SINGLE_KIND

    def __init__(self, view: "StructuredView", uid: int, keyword: str) -> None:
        self.view = view
        self.uid = uid
        self.keyword = keyword
        self.row = Adw.ActionRow(title=fields.SPELLING[keyword],
                                 subtitle=_descriptions().get(keyword, ""))
        self.row.set_title_selectable(False)
        self.entry = _entry(width=34)
        self.entry.set_hexpand(False)
        self.entry.update_property([Gtk.AccessibleProperty.LABEL], [fields.SPELLING[keyword]])
        self.entry.connect("changed", self._on_changed)
        self.state = _state_pill(view)
        self.reset = _icon_button(
            "user-trash-symbolic", _("Remove this line from the block"),
            lambda: view.controller.delete_option(self.uid),
        )
        for widget in (self.state, self.entry, self.reset):
            self.row.add_suffix(widget)

    def bind(self, option) -> None:
        self.view.syncing.set_text(self.entry, option.value)

    def show(self, answer: AnswerView) -> None:
        value, _source = answer.inherited(self.keyword)
        self.entry.set_placeholder_text(value)
        item, error = answer.line_state(self.uid)
        unused = _line_pill(self.state, item, error)
        set_class(self.entry, "error", error is not None)
        set_class(self.entry, "dim-label", unused)

    def _on_changed(self, entry) -> None:
        if not self.view.syncing.on:
            self.view.controller.option_typed(self.uid, "value", entry.get_text())


class SwitchRow:
    """The first line of a yes/no keyword (IdentitiesOnly, ForwardAgent)."""

    kind = fields.SWITCH_KIND
    _VALUES = ("yes", "no")

    def __init__(self, view: "StructuredView", uid: int, keyword: str) -> None:
        self.view = view
        self.uid = uid
        self.keyword = keyword
        self.row = Adw.ComboRow(title=fields.SPELLING[keyword],
                                subtitle=_descriptions().get(keyword, ""))
        self.row.set_model(Gtk.StringList.new(list(self._VALUES)))
        self.row.connect("notify::selected", self._on_selected)
        self.state = _state_pill(view)
        self.row.add_suffix(self.state)
        self.row.add_suffix(_icon_button(
            "user-trash-symbolic", _("Remove this line from the block"),
            lambda: view.controller.delete_option(self.uid),
        ))

    def bind(self, option) -> None:
        value = option.value.strip().strip('"').lower()
        index = self._VALUES.index(value) if value in self._VALUES else 0
        if self.row.get_selected() != index:
            self.view.syncing.on = True
            try:
                self.row.set_selected(index)
            finally:
                self.view.syncing.on = False

    def show(self, answer: AnswerView) -> None:
        item, error = answer.line_state(self.uid)
        _line_pill(self.state, item, error)

    def _on_selected(self, *_args) -> None:
        if self.view.syncing.on:
            return
        value = self._VALUES[self.row.get_selected()]
        self.view.controller.choice_made(self.view.uid, self.keyword, self.uid, value)


class IdentityRow:
    """One IdentityFile line: the path, what the file on disk says about
    itself, Browse, and a fix for a key others can read."""

    kind = fields.IDENTITY_KIND

    def __init__(self, view: "StructuredView", uid: int, _keyword: str = "") -> None:
        self.view = view
        self.uid = uid
        self.status = None
        self.row = Adw.ActionRow(title="IdentityFile")
        self.row.set_subtitle_lines(1)
        self.entry = _entry(width=38)
        self.entry.update_property([Gtk.AccessibleProperty.LABEL], ["IdentityFile"])
        self.entry.connect("changed", self._on_changed)
        self.state = _state_pill(view)
        self.fix = Gtk.Button(label=_("Fix"))
        self.fix.set_valign(Gtk.Align.CENTER)
        self.fix.set_tooltip_text(_("Make the key readable by you only (0600), as OpenSSH requires"))
        self.fix.connect("clicked", lambda *_a: self._on_fix())
        self.fix.set_visible(False)
        browse = _icon_button("document-open-symbolic", _("Choose a key file"),
                              lambda: view.controller.browse_identity(self.uid))
        remove = _icon_button("user-trash-symbolic", _("Remove this line from the block"),
                              lambda: view.controller.delete_option(self.uid))
        for widget in (self.state, self.entry, self.fix, browse, remove):
            self.row.add_suffix(widget)

    def bind(self, option) -> None:
        self.view.syncing.set_text(self.entry, option.value)
        self._check(option.value)

    def _check(self, value: str) -> None:
        status = fields.key_file_status(value)
        self.status = status
        text = messages.key_status_text(status.state, status.mode)
        self.row.set_subtitle(GLib.markup_escape_text(text))
        self.fix.set_visible(status.state == fields.KEY_OPEN)
        set_class(self.entry, "warning", status.state in (fields.KEY_MISSING, fields.KEY_OPEN))

    def show(self, answer: AnswerView) -> None:
        item, error = answer.line_state(self.uid)
        _line_pill(self.state, item, error)
        set_class(self.entry, "error", error is not None)

    def _on_changed(self, entry) -> None:
        if not self.view.syncing.on:
            self.view.controller.option_typed(self.uid, "value", entry.get_text())
            self._check(entry.get_text())

    def _on_fix(self) -> None:
        if self.status is not None and self.status.path:
            self.view.controller.fix_key(self.status.path)
            self._check(self.entry.get_text())


class ForwardRow:
    """One LocalForward, RemoteForward or DynamicForward line."""

    kind = fields.FORWARD_KIND

    def __init__(self, view: "StructuredView", uid: int, _keyword: str = "") -> None:
        self.view = view
        self.uid = uid
        self.row = Adw.ActionRow()
        self.entry = _entry(width=34)
        self.entry.connect("changed", self._on_changed)
        self.state = _state_pill(view)
        remove = _icon_button("user-trash-symbolic", _("Remove this forward"),
                              lambda: view.controller.delete_option(self.uid))
        for widget in (self.state, self.entry, remove):
            self.row.add_suffix(widget)

    def bind(self, option) -> None:
        title = fields.SPELLING.get(option.keyword, option.key)
        if self.row.get_title() != title:
            self.row.set_title(title)
        self.entry.update_property([Gtk.AccessibleProperty.LABEL], [title])
        self.entry.set_placeholder_text(_FORWARD_HINTS.get(option.keyword, ""))
        self.view.syncing.set_text(self.entry, option.value)

    def show(self, answer: AnswerView) -> None:
        item, error = answer.line_state(self.uid)
        _line_pill(self.state, item, error)
        set_class(self.entry, "error", error is not None)

    def _on_changed(self, entry) -> None:
        if not self.view.syncing.on:
            self.view.controller.option_typed(self.uid, "value", entry.get_text())


_FORWARD_HINTS = {
    "localforward": "8080 localhost:80",
    "remoteforward": "9000 localhost:9000",
    "dynamicforward": "1080",
}

_ROWS = {
    fields.SINGLE_KIND: SingleRow,
    fields.SWITCH_KIND: SwitchRow,
}


class _Suggestions:
    """Known keywords under a keyword field, as it is typed. Never takes
    the focus: typing carries on in the field while the list follows it."""

    def __init__(self, entry: Gtk.Entry, accept: Callable[[str], None]) -> None:
        self.entry = entry
        self.accept = accept
        self.popover: Optional[Gtk.Popover] = None
        self.names: Optional[Gtk.ListBox] = None
        keys = Gtk.EventControllerKey()
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
        if not self.visible():
            return False
        selected = self.names.get_selected_row()
        if keyval in (Gdk.KEY_Down, Gdk.KEY_Up):
            index = selected.get_index() if selected is not None else -1
            index += 1 if keyval == Gdk.KEY_Down else -1
            target = self.names.get_row_at_index(max(0, index))
            if target is not None:
                self.names.select_row(target)
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_Tab) and selected is not None:
            self._take(selected)
            return True
        if keyval == Gdk.KEY_Escape:
            self.hide()
            return True
        return False

    def destroy(self) -> None:
        if self.popover is not None:
            self.popover.unparent()
            self.popover = None


class OtherRow:
    """A line no field shows: keyword, value, what OpenSSH does with it."""

    kind = fields.OTHER_KIND

    def __init__(self, view: "StructuredView", uid: int, _keyword: str = "") -> None:
        self.view = view
        self.uid = uid
        self.row = Gtk.ListBoxRow()
        self.row.set_activatable(False)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for side in ("top", "bottom"):
            getattr(box, f"set_margin_{side}")(6)
        box.set_margin_start(12)
        box.set_margin_end(6)
        self.key = _entry(_("Option"), 24)
        self.key.update_property([Gtk.AccessibleProperty.LABEL], [_("Option")])
        self.key.connect("changed", self._on_changed, "key")
        self.value = _entry(_("Value"))
        self.value.set_hexpand(True)
        self.value.update_property([Gtk.AccessibleProperty.LABEL], [_("Value")])
        self.value.connect("changed", self._on_changed, "value")
        self.state = _state_pill(view)
        remove = _icon_button("user-trash-symbolic", _("Remove option"),
                              lambda: view.controller.delete_option(self.uid))
        for widget in (self.key, self.value, self.state, remove):
            box.append(widget)
        self.row.set_child(box)
        self.suggestions = _Suggestions(self.key, self._take_keyword)

    def bind(self, option) -> None:
        self.view.syncing.set_text(self.key, option.key)
        self.view.syncing.set_text(self.value, option.value)

    def show(self, answer: AnswerView) -> None:
        item, error = answer.line_state(self.uid)
        unused = _line_pill(self.state, item, error)
        for entry in (self.key, self.value):
            set_class(entry, "error", error is not None)
            set_class(entry, "dim-label", unused)

    def _on_changed(self, entry, field: str) -> None:
        if self.view.syncing.on:
            return
        self.view.controller.option_typed(self.uid, field, entry.get_text())
        if field == "key":
            self.suggestions.typed()

    def _take_keyword(self, name: str) -> None:
        self.key.set_text(name)
        self.key.set_position(-1)
        self.suggestions.hide()
        self.value.grab_focus()

    def destroy(self) -> None:
        self.suggestions.destroy()


_ROWS.update({
    fields.IDENTITY_KIND: IdentityRow,
    fields.FORWARD_KIND: ForwardRow,
    fields.OTHER_KIND: OtherRow,
})

#: The keywords Add Option offers at a click, in the order it lists them.
_COMMON = (
    "hostname", "user", "port", "identityfile", "identitiesonly", "proxyjump",
    "forwardagent", "localforward", "remoteforward", "dynamicforward",
)


class _Page(Gtk.ScrolledWindow):
    """The block's groups, one under the other, as wide as reads well: a
    preferences page keeps to a narrow column that leaves most of a wide
    window empty."""

    def __init__(self) -> None:
        super().__init__()
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.set_vexpand(True)
        clamp = Adw.Clamp(maximum_size=1100, tightening_threshold=900)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        for side in ("top", "bottom"):
            getattr(self.box, f"set_margin_{side}")(24)
        for side in ("start", "end"):
            getattr(self.box, f"set_margin_{side}")(12)
        clamp.set_child(self.box)
        self.set_child(clamp)

    def add(self, widget: Gtk.Widget) -> None:
        self.box.append(widget)

    def scroll_to_top(self) -> None:
        self.get_vadjustment().set_value(0)


def _boxed_list() -> Gtk.ListBox:
    listbox = Gtk.ListBox()
    listbox.set_selection_mode(Gtk.SelectionMode.NONE)
    listbox.add_css_class("boxed-list")
    return listbox


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
    """One block as fields. *controller* supplies the edits (see the
    calls) and ``browse_identity(uid)``."""

    def __init__(self, controller) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.controller = controller
        self.syncing = _Syncing()
        self.uid: Optional[int] = None
        self.kind = ""
        #: Lines added with Add Option: they stay plain lines (see
        #: advanced_config_fields).
        self._keep: set = set()
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
        self.stack.add_named(self.page, "block")

        # -- the block itself: the title card --------------------------------------
        # What the block is (its name, read only: it follows the Host or
        # Match row as it is typed; what kind of block; its file), Note,
        # on/off and the actions; the note as the comment it is; and tiles:
        # what it applies to, how many lines, and its problems when it has
        # any. The window shows the card above all three views.
        self.block_group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.block_group.add_css_class("card")
        self.block_group.add_css_class("advcfg-title-card")
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.head_title = Gtk.Label(xalign=0)
        self.head_title.add_css_class("monospace")
        self.head_title.add_css_class("advcfg-card-title")
        self.head_title.set_ellipsize(Pango.EllipsizeMode.END)
        line.append(self.head_title)
        self.head_caption = Gtk.Label(xalign=0, hexpand=True)
        self.head_caption.add_css_class("dim-label")
        self.head_caption.add_css_class("caption")
        self.head_caption.set_ellipsize(Pango.EllipsizeMode.END)
        self.head_caption.set_valign(Gtk.Align.CENTER)
        line.append(self.head_caption)
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

        # The note, read only, as the comment it is in the file.
        self.note_label = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.note_label.add_css_class("advcfg-comment")
        self.block_group.append(self.note_label)
        # Off: one line that says so (the tiles and lines are dimmed with it).
        self.disabled_label = Gtk.Label(
            label=_("Disabled: written as comments, which OpenSSH ignores."), xalign=0,
        )
        self.disabled_label.add_css_class("advcfg-disabled-note")
        self.block_group.append(self.disabled_label)
        #: What is wrong with the block's own line, or that it does not
        #: apply to a host kept in question.
        self.error_row = Gtk.Label(xalign=0, wrap=True)
        self.error_row.add_css_class("error")
        self.block_group.append(self.error_row)

        self.tiles = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        # Applies To: the hosts of a pattern or Match block, each a link;
        # for a host, its own user@address.
        self.applies_box, content, _dot = _card_tile("network-server-symbolic", _("Applies To"))
        self.applies_box.set_hexpand(True)
        self.applies_label = Gtk.Label(xalign=0)
        self.applies_label.set_ellipsize(Pango.EllipsizeMode.END)
        content.append(self.applies_label)
        # Chips as wide as their names, not stretched across the line.
        self.applies_chips = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                                         halign=Gtk.Align.START, max_children_per_line=12)
        self.applies_chips.set_column_spacing(6)
        self.applies_chips.set_row_spacing(6)
        content.append(self.applies_chips)
        self.tiles.append(self.applies_box)
        self.lines_tile, content, _dot = _card_tile("document-properties-symbolic", _("Directives"))
        self.lines_label = Gtk.Label(xalign=0)
        content.append(self.lines_label)
        self.tiles.append(self.lines_tile)
        # Problems: shown only when there are some; a click shows the first.
        self.problems_tile, content, self.problems_dot = _card_tile(
            "dialog-warning-symbolic", _("Problems"),
        )
        self.problems_label = Gtk.Label(xalign=0)
        content.append(self.problems_label)
        self.problems_tile.set_cursor_from_name("pointer")
        self.problems_tile.set_tooltip_text(_("Show the first problem"))
        click = Gtk.GestureClick()
        click.connect("released", lambda *_a: self._show_first_problem())
        self.problems_tile.add_controller(click)
        self._first_problem: Optional[int] = None
        self.tiles.append(self.problems_tile)
        self.block_group.append(self.tiles)
        self._file_name = ""

        # -- the lines, in the block's order ------------------------------------
        group = Adw.PreferencesGroup()
        #: The lines, dimmed while the block is off.
        self.options_group = group
        self.options_list = _boxed_list()
        # The block's Host or Match line: a row like the others, first.
        self.header_row = Adw.ActionRow()
        self.header_row.set_title_selectable(False)
        self.patterns_row = _entry(width=34)
        self.patterns_row.set_hexpand(False)
        self.patterns_row.connect("changed", self._on_patterns_changed)
        self.header_row.add_suffix(self.patterns_row)
        # Where the others have Remove: kept empty, so the fields line up.
        spacer = _icon_button("user-trash-symbolic", "", lambda: None)
        spacer.set_opacity(0)
        spacer.set_can_target(False)
        spacer.set_can_focus(False)
        self.header_row.add_suffix(spacer)
        self.options_list.append(self.header_row)
        self.add_option_row = Adw.ActionRow(title=_("Add Option"))
        self.add_option_row.add_prefix(Gtk.Image.new_from_icon_name("list-add-symbolic"))
        self.add_option_row.set_activatable(True)
        self.add_option_row.connect(
            "activated", lambda *_a: controller.add_option(self.uid, keep=True)
        )
        self.add_option_row.add_suffix(self._common_menu())
        self.options_list.append(self.add_option_row)
        group.add(self.options_list)
        self.page.add(group)

    def _common_menu(self) -> Gtk.MenuButton:
        """The options most blocks set, a click away."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        popover = Gtk.Popover(child=box)
        popover.add_css_class("menu")
        for keyword in _COMMON:
            button = Gtk.Button(label=fields.SPELLING[keyword])
            button.add_css_class("flat")
            button.get_child().set_xalign(0)
            button.connect("clicked", lambda *_a, k=keyword: (
                popover.popdown(), self._add_common(k)
            ))
            box.append(button)
        menu = Gtk.MenuButton(icon_name="view-more-symbolic", popover=popover,
                              tooltip_text=_("Add a Common Option"))
        menu.add_css_class("flat")
        menu.set_valign(Gtk.Align.CENTER)
        return menu

    def _add_common(self, keyword: str) -> None:
        if self.uid is None:
            return
        value = "yes" if keyword in fields.SWITCHES else ""
        self.controller.add_option(self.uid, fields.SPELLING[keyword], value)

    # -- the rows, by kind (for the controller and the tests) --------------------

    def _of_kind(self, kind: str) -> Dict[int, object]:
        return {uid: row for uid, row in self._rows.items() if row.kind == kind}

    @property
    def typed(self) -> Dict[str, SingleRow]:
        """The field of each keyword of one value the block sets."""
        return {row.keyword: row for row in self._of_kind(fields.SINGLE_KIND).values()}

    @property
    def switches(self) -> Dict[str, SwitchRow]:
        return {row.keyword: row for row in self._of_kind(fields.SWITCH_KIND).values()}

    @property
    def _identity_rows(self) -> Dict[int, IdentityRow]:
        return self._of_kind(fields.IDENTITY_KIND)

    @property
    def _forward_rows(self) -> Dict[int, ForwardRow]:
        return self._of_kind(fields.FORWARD_KIND)

    @property
    def _other_rows(self) -> Dict[int, OtherRow]:
        return self._of_kind(fields.OTHER_KIND)

    # -- showing a block -------------------------------------------------------

    def keep(self, uid: int) -> None:
        """Keep a line added with Add Option a plain line."""
        self._keep.add(uid)

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
            self._keep = set()
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
        if self.header_row.get_title() != keyword:
            self.header_row.set_title(keyword)
            self.header_row.set_subtitle(
                _("The names and patterns this block is for") if block.kind == HOST
                else _("The conditions this block is for")
            )
        self.patterns_row.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Host patterns") if block.kind == HOST else _("Match conditions")],
        )
        self._show_heading(block)
        self.lines_label.set_label(messages.line_count(len(block.options)))
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
        set_class(self.options_group, "advcfg-dimmed", block.disabled)
        set_class(self.tiles, "advcfg-dimmed", block.disabled)

        # Every line in the block's order, each with its control. A row is
        # made again only when its line changes kind (a line above went).
        kinds = fields.line_kinds(block.options, self._keep)
        wanted = {uid: kind for uid, kind in kinds}
        for uid in [uid for uid, row in self._rows.items()
                    if wanted.get(uid) != row.kind
                    or getattr(row, "keyword", options[uid].keyword) != options[uid].keyword]:
            self._drop_row(uid)
        for index, (uid, kind) in enumerate(kinds, start=1):     # after the Host line
            option = options[uid]
            row = self._rows.get(uid)
            if row is None:
                row = self._rows[uid] = _ROWS[kind](self, uid, option.keyword)
                self.options_list.insert(row.row, index)
            elif row.row.get_index() != index:
                self.options_list.remove(row.row)
                self.options_list.insert(row.row, index)
            row.bind(option)

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
        kind = messages.block_category_label(
            block_category(block.kind, block.patterns), len(block.patterns.split()) or 1
        )
        caption = " · ".join(part for part in (kind, self._file_name) if part)
        if self.head_caption.get_label() != caption:
            self.head_caption.set_label(caption)

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
            item, error = answer.line_state(option.uid)
            if error is not None:
                rejected += 1
            elif item is not None and item.state is State.SHADOWED:
                unused += 1
            elif item is not None and item.state is State.FORCED:
                overridden += 1
            else:
                continue
            first = option.uid if first is None else first
        rejected += int(header_error)
        self._first_problem = first
        parts = []
        if rejected:
            parts.append(messages.rejected_count(rejected))
        if unused:
            parts.append(messages.shadowed_count(unused))
        if overridden:
            parts.append(messages.overridden_count(overridden))
        self.problems_tile.set_visible(bool(parts))
        self.problems_label.set_label(" · ".join(parts))
        style = "error" if (rejected or unused) else "purple"
        for name in ("error", "purple"):
            on = bool(parts) and name == style
            set_class(self.problems_tile, f"advcfg-attn-{name}", on)
            set_class(self.problems_dot, f"advcfg-attn-{name}", on)
        self.problems_dot.set_visible(bool(parts))

    def _show_first_problem(self) -> None:
        if self._first_problem is None:
            return
        self.controller.show_page("structured")
        self.reveal(self._first_problem)

    #: Hosts named before the rest are counted ("+3").
    _APPLIES_SHOWN = 8

    def _show_applies(self, block, hosts) -> None:
        """A pattern or Match block: the hosts it applies to, as chips. A
        host: its own user@address (or its names). Global options: every
        host."""
        if block is None:
            return
        names = False                  # a sentence, or names (monospace)
        if hosts is None:
            if block.kind == GLOBAL or block.patterns.split() == ["*"]:
                text = messages.every_host()
            else:
                text, names = fields.block_detail(block) or block.patterns.strip(), True
        else:
            text = "" if hosts else messages.applies_to_none()
        set_class(self.applies_label, "monospace", names)
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
            chip = Gtk.Button(label=name, tooltip_text=messages.show_host_block(name))
            chip.add_css_class("advcfg-host-chip")
            chip.connect("clicked", lambda *_a, n=name: self.controller.go_to(host=n))
            self.applies_chips.append(chip)
        if len(shown) > self._APPLIES_SHOWN:
            rest = shown[self._APPLIES_SHOWN:]
            more = Gtk.Label(label=f"+{len(rest)}", tooltip_text=", ".join(rest))
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
        if row is None:
            return None
        if row.kind == fields.SWITCH_KIND:
            return row.row
        if row.kind == fields.OTHER_KIND:
            return row.value
        return row.entry

    def line_status(self, uid: int) -> Optional[str]:
        """What is said beside option *uid* (Not used, Overridden,
        Rejected), or None: nothing needs saying, or it is not shown."""
        row = self._rows.get(uid)
        if row is None:
            return None
        return row.state.get_label() if row.state.get_visible() else None

    def focus_option(self, uid: int, *, key: bool = False) -> None:
        if key and uid in self._other_rows:
            self._other_rows[uid].key.grab_focus()
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

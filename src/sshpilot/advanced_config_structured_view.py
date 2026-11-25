"""Advanced Configuration, Structured view: one block, as fields.

The block's header (its Host patterns or Match conditions), its note and
whether it is on; then the options as fields where a field suits them
(HostName, User, Port, the identity files, ProxyJump, ForwardAgent,
IdentitiesOnly, the forwards) and every other line in a table. Which lines
the fields show is decided in :mod:`advanced_config_fields`: no line is
dropped or rewritten to fit a field.

A field the block leaves empty shows, in grey, what OpenSSH will use for
the host being checked and where that comes from ("22 · from Host *",
"OpenSSH default"): the answer comes from OpenSSH (``ssh -G``) like every
other answer here. A line an earlier block (or Settings) overrides says so
beside it; it is never refused or rewritten.

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
from .i18n import N_  # noqa: E402
from .ssh_config_blocks import GLOBAL, HOST, Block, LineMap  # noqa: E402
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


def _line_pill(label: Gtk.Label, item, error) -> bool:
    """Say what OpenSSH does with a line: only what needs saying (not used,
    overridden, rejected). Returns whether the line is unused."""
    if error is not None:
        set_pill(label, _("Rejected"), "warning", messages.error_text(error))
        return False
    if item is not None and item.state in (State.SHADOWED, State.FORCED):
        text = _("Not used") if item.state is State.SHADOWED else messages.state_label(item.state)
        set_pill(label, text, messages.STATE_STYLES[item.state], messages.state_tooltip(item))
        return True
    set_pill(label, "", "muted")
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


class TypedField:
    """A keyword of one value (HostName, User, Port, ProxyJump): its first
    line in the block, edited in a field. Empty when the block does not set
    it, with OpenSSH's value for the host being checked in grey."""

    def __init__(self, view: "StructuredView", keyword: str, description: str) -> None:
        self.view = view
        self.keyword = keyword
        self.uid: Optional[int] = None
        self.row = Adw.ActionRow(title=fields.SPELLING[keyword], subtitle=description)
        self.row.set_title_selectable(False)
        self.entry = _entry(width=34)
        self.entry.set_hexpand(False)
        self.entry.update_property([Gtk.AccessibleProperty.LABEL], [fields.SPELLING[keyword]])
        self.entry.connect("changed", self._on_changed)
        self.source = pill(style="accent")
        self.state = pill(style="warning")
        self.reset = _icon_button(
            "edit-clear-symbolic", _("Remove this line from the block"), self._on_reset
        )
        for widget in (self.state, self.source, self.entry, self.reset):
            self.row.add_suffix(widget)

    def bind(self, option) -> None:
        self.uid = option.uid if option is not None else None
        self.view.syncing.set_text(self.entry, option.value if option is not None else "")
        self.reset.set_visible(option is not None)

    def show(self, answer: AnswerView) -> None:
        value, source = answer.inherited(self.keyword)
        self.entry.set_placeholder_text(value)
        set_pill(self.source, source if self.uid is None else "", "accent")
        item, error = answer.line_state(self.uid) if self.uid is not None else (None, None)
        unused = _line_pill(self.state, item, error)
        set_class(self.entry, "error", error is not None)
        set_class(self.entry, "dim-label", unused)

    def _on_changed(self, entry) -> None:
        if not self.view.syncing.on:
            self.view.controller.field_typed(self.view.uid, self.keyword, self.uid, entry.get_text())

    def _on_reset(self) -> None:
        if self.uid is not None:
            self.view.controller.delete_option(self.uid)


class SwitchField:
    """A yes/no keyword (IdentitiesOnly, ForwardAgent): not set, yes or no.
    "Not set" says what OpenSSH will use instead."""

    _VALUES = (None, "yes", "no")

    def __init__(self, view: "StructuredView", keyword: str, description: str) -> None:
        self.view = view
        self.keyword = keyword
        self.uid: Optional[int] = None
        self._not_set = ""
        self.model = Gtk.StringList.new(["", "yes", "no"])
        self.row = Adw.ComboRow(title=fields.SPELLING[keyword], subtitle=description)
        self.row.set_model(self.model)
        self.row.connect("notify::selected", self._on_selected)
        self.state = pill(style="warning")
        self.row.add_suffix(self.state)
        self._set_not_set(_("Not set"))

    def _set_not_set(self, text: str) -> None:
        if text == self._not_set:
            return
        self._not_set = text
        selected = self.row.get_selected()
        self.view.syncing.on = True
        try:
            self.model.splice(0, 1, [text])
            self.row.set_selected(selected)
        finally:
            self.view.syncing.on = False

    def bind(self, option) -> None:
        self.uid = option.uid if option is not None else None
        value = option.value.strip().strip('"').lower() if option is not None else None
        index = self._VALUES.index(value) if value in self._VALUES else 0
        if self.row.get_selected() != index:
            self.view.syncing.on = True
            try:
                self.row.set_selected(index)
            finally:
                self.view.syncing.on = False

    def show(self, answer: AnswerView) -> None:
        value, source = answer.inherited(self.keyword)
        if value:
            # Translators: the "not set" choice of a yes/no option, with what
            # OpenSSH uses instead, e.g. "Not set (no, OpenSSH default)".
            text = _("Not set ({value}, {source})").format(value=value, source=source) \
                if source else _("Not set ({value})").format(value=value)
        else:
            text = _("Not set")
        self._set_not_set(text)
        item, error = answer.line_state(self.uid) if self.uid is not None else (None, None)
        _line_pill(self.state, item, error)

    def _on_selected(self, *_args) -> None:
        if self.view.syncing.on:
            return
        value = self._VALUES[self.row.get_selected()]
        self.view.controller.choice_made(self.view.uid, self.keyword, self.uid, value)


class IdentityRow:
    """One IdentityFile line: the path, what the file on disk says about
    itself, Browse, and a fix for a key others can read."""

    def __init__(self, view: "StructuredView", uid: int) -> None:
        self.view = view
        self.uid = uid
        self.status = None
        self.row = Adw.ActionRow(title="IdentityFile")
        self.row.set_subtitle_lines(1)
        self.entry = _entry(width=38)
        self.entry.update_property([Gtk.AccessibleProperty.LABEL], ["IdentityFile"])
        self.entry.connect("changed", self._on_changed)
        self.state = pill(style="warning")
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

    def __init__(self, view: "StructuredView", uid: int) -> None:
        self.view = view
        self.uid = uid
        self.row = Adw.ActionRow()
        self.entry = _entry(width=34)
        self.entry.connect("changed", self._on_changed)
        self.state = pill(style="warning")
        remove = _icon_button("user-trash-symbolic", _("Remove this forward"),
                              lambda: view.controller.delete_option(self.uid))
        for widget in (self.state, self.entry, remove):
            self.row.add_suffix(widget)

    def bind(self, option) -> None:
        title = fields.SPELLING.get(option.keyword, option.key)
        if self.row.get_title() != title:
            self.row.set_title(title)
        self.entry.update_property([Gtk.AccessibleProperty.LABEL], [title])
        self.view.syncing.set_text(self.entry, option.value)

    def show(self, answer: AnswerView) -> None:
        item, error = answer.line_state(self.uid)
        _line_pill(self.state, item, error)
        set_class(self.entry, "error", error is not None)

    def _on_changed(self, entry) -> None:
        if not self.view.syncing.on:
            self.view.controller.option_typed(self.uid, "value", entry.get_text())


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

    def __init__(self, view: "StructuredView", uid: int) -> None:
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
        self.state = pill(style="warning")
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


_FORWARD_KINDS = {
    # Translators: kinds of SSH port forward, shown on a tunnel's chip.
    "localforward": N_("Local"),
    "remoteforward": N_("Remote"),
    "dynamicforward": N_("SOCKS"),
}


class RoutingCard:
    """Routing and tunnels at a glance: this computer → each jump host →
    the host, as OpenSSH resolves them for the host checked, and the port
    forwards that open with it. The block's own forwards can be removed
    here; forwards other blocks add are shown dimmed, with where they come
    from. A jump host is a button that shows its block."""

    def __init__(self, view: "StructuredView") -> None:
        self.view = view
        self.group = Adw.PreferencesGroup(title=_("Routing and Tunnels"))
        add = Gtk.Button()
        add.set_child(Adw.ButtonContent(icon_name="list-add-symbolic", label=_("Add Tunnel")))
        add.add_css_class("flat")
        add.connect("clicked", lambda *_a: view.focus_add_forward())
        self.group.set_header_suffix(add)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("card")
        card.add_css_class("advcfg-routing")
        card.append(self._caption(_("Route")))
        self.route = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE)
        self.route.set_max_children_per_line(24)
        self.route.set_column_spacing(4)
        self.route.set_row_spacing(6)
        card.append(self.route)
        card.append(self._caption(_("Port forwards")))
        self.tunnels = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE)
        self.tunnels.set_max_children_per_line(12)
        self.tunnels.set_column_spacing(6)
        self.tunnels.set_row_spacing(6)
        card.append(self.tunnels)
        self.group.add(card)
        self._shown: Optional[tuple] = None

    @staticmethod
    def _caption(text: str) -> Gtk.Label:
        label = Gtk.Label(label=text, xalign=0)
        label.add_css_class("caption-heading")
        label.add_css_class("dim-label")
        return label

    def show(self, stops: Sequence[tuple], tunnels: Sequence[tuple]) -> None:
        """*stops*: ``(name, endpoint, is_jump)`` from the first jump host
        to the host itself (none: no host is checked). *tunnels*:
        ``(keyword, value, uid or None, source)``; a uid is a line of this
        block, which can be removed."""
        shown = (tuple(stops), tuple(tunnels))
        if shown == self._shown:
            return
        self._shown = shown
        self.route.remove_all()
        if not stops:
            self.route.append(self._note(_("Type a host to check to see its route.")))
        else:
            entries = [(_("This computer"), "", False)] + list(stops)
            for index, (name, endpoint, jump) in enumerate(entries):
                if index:
                    arrow = Gtk.Image.new_from_icon_name("go-next-symbolic")
                    arrow.add_css_class("dim-label")
                    arrow.set_valign(Gtk.Align.CENTER)
                    self.route.append(arrow)
                self.route.append(self._stop(name, endpoint, index, len(entries), jump))
        self.tunnels.remove_all()
        if not tunnels:
            self.tunnels.append(self._note(_("No port forwards.")))
        for keyword, value, uid, source in tunnels:
            self.tunnels.append(self._tunnel(keyword, value, uid, source))

    @staticmethod
    def _note(text: str) -> Gtk.Label:
        label = Gtk.Label(label=text, xalign=0)
        label.add_css_class("dim-label")
        return label

    def _stop(self, name: str, endpoint: str, index: int, count: int, jump: bool):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        title = Gtk.Label(label=name, xalign=0)
        if index:
            title.add_css_class("monospace")
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_max_width_chars(24)
        box.append(title)
        if endpoint:
            where = Gtk.Label(label=endpoint, xalign=0)
            for name_ in ("caption", "dim-label", "monospace"):
                where.add_css_class(name_)
            where.set_ellipsize(Pango.EllipsizeMode.END)
            where.set_max_width_chars(32)
            box.append(where)
        box.set_valign(Gtk.Align.CENTER)
        if jump:
            # A jump host: its own block is a click away.
            button = Gtk.Button(child=box, tooltip_text=_("Show this host's block"))
            button.add_css_class("advcfg-hop")
            button.set_valign(Gtk.Align.CENTER)
            button.connect("clicked", lambda *_a, n=name: self.view.controller.show_host(
                fields.hop_host(n)
            ))
            return button
        box.add_css_class("advcfg-hop")
        if index == count - 1 and index:
            box.add_css_class("advcfg-hop-target")
        return box

    def _tunnel(self, keyword: str, value: str, uid: Optional[int], source: str):
        listen, target = fields.forward_parts(keyword, value)
        chip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        chip.add_css_class("advcfg-tunnel")
        kind = pill(_(_FORWARD_KINDS.get(keyword, keyword)), "success" if uid else "muted")
        chip.append(kind)
        text = f"{listen} → {target}" if target else listen
        label = Gtk.Label(label=text)
        label.add_css_class("monospace")
        chip.append(label)
        if uid is not None:
            remove = Gtk.Button(icon_name="window-close-symbolic",
                                tooltip_text=_("Remove this forward"))
            remove.add_css_class("flat")
            remove.add_css_class("circular")
            remove.connect("clicked", lambda *_a: self.view.controller.delete_option(uid))
            chip.append(remove)
        else:
            chip.add_css_class("dim-label")
            chip.set_tooltip_text(source or None)
        return chip


def _boxed_list() -> Gtk.ListBox:
    listbox = Gtk.ListBox()
    listbox.set_selection_mode(Gtk.SelectionMode.NONE)
    listbox.add_css_class("boxed-list")
    return listbox


class StructuredView(Gtk.Box):
    """One block as fields. *controller* supplies the edits (see the
    calls), ``hop_endpoint(name)`` and ``browse_identity(uid)``."""

    def __init__(self, controller) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.controller = controller
        self.syncing = _Syncing()
        self.uid: Optional[int] = None
        self.kind = ""
        #: Lines added from the table: they stay there (see advanced_config_fields).
        self._keep: set = set()
        self._answer = AnswerView()
        self._identity_rows: Dict[int, IdentityRow] = {}
        self._forward_rows: Dict[int, ForwardRow] = {}
        self._other_rows: Dict[int, OtherRow] = {}
        descriptions = _descriptions()

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

        # -- the block itself ---------------------------------------------
        self.block_group = Adw.PreferencesGroup()
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.duplicate_button = Gtk.Button(icon_name="edit-copy-symbolic",
                                           tooltip_text=_("Duplicate Block"))
        self.duplicate_button.add_css_class("flat")
        self.duplicate_button.connect("clicked", lambda *_a: controller.duplicate_block(self.uid))
        self.delete_button = Gtk.Button(icon_name="user-trash-symbolic",
                                        tooltip_text=_("Delete Block"))
        self.delete_button.add_css_class("flat")
        self.delete_button.connect("clicked", lambda *_a: controller.delete_block(self.uid))
        actions.append(self.duplicate_button)
        actions.append(self.delete_button)
        self.block_group.set_header_suffix(actions)
        block_list = _boxed_list()
        self.patterns_row = Adw.EntryRow()
        self.patterns_row.add_css_class("monospace")
        self.patterns_row.connect("changed", self._on_patterns_changed)
        block_list.append(self.patterns_row)
        self.note_row = Adw.EntryRow(title=_("Note (a comment above the block)"))
        self.note_row.connect("changed", self._on_note_changed)
        block_list.append(self.note_row)
        self.enabled_row = Adw.SwitchRow(
            title=_("Enabled"),
            subtitle=_("Off, the block is written as comments, which OpenSSH ignores"),
        )
        self.enabled_row.connect("notify::active", self._on_enabled)
        block_list.append(self.enabled_row)
        self.status_row = Adw.ActionRow(title=_("Host checked"))
        self.status_row.set_activatable(False)
        self.status_row.set_subtitle_lines(4)
        block_list.append(self.status_row)
        self.error_row = Adw.ActionRow()
        self.error_row.add_css_class("error")
        self.error_row.set_subtitle_lines(4)
        self.error_row.set_visible(False)
        block_list.append(self.error_row)
        self.block_group.add(block_list)
        self.page.add(self.block_group)
        self.routing = RoutingCard(self)
        self.page.add(self.routing.group)

        # -- connection ------------------------------------------------------
        group = Adw.PreferencesGroup(title=_("Connection"))
        connection = _boxed_list()
        self.typed: Dict[str, TypedField] = {}
        for keyword in ("hostname", "user", "port"):
            field = self.typed[keyword] = TypedField(self, keyword, descriptions[keyword])
            connection.append(field.row)
        group.add(connection)
        self.page.add(group)

        # -- authentication -------------------------------------------------
        group = Adw.PreferencesGroup(title=_("Authentication"))
        self.auth_list = _boxed_list()
        self.identity_hint = Adw.ActionRow(title="IdentityFile")
        self.identity_hint.set_subtitle_lines(3)
        self.identity_hint.set_activatable(False)
        self.auth_list.append(self.identity_hint)
        self.add_identity_row = Adw.ActionRow(title=_("Add Identity File"))
        self.add_identity_row.add_prefix(Gtk.Image.new_from_icon_name("list-add-symbolic"))
        self.add_identity_row.set_activatable(True)
        self.add_identity_row.connect("activated", lambda *_a: controller.add_identity(self.uid))
        self.auth_list.append(self.add_identity_row)
        self.switches: Dict[str, SwitchField] = {}
        field = self.switches["identitiesonly"] = SwitchField(
            self, "identitiesonly", descriptions["identitiesonly"]
        )
        self.auth_list.append(field.row)
        group.add(self.auth_list)
        self.page.add(group)

        # -- networking ------------------------------------------------------
        group = Adw.PreferencesGroup(title=_("Networking"))
        network = _boxed_list()
        field = self.typed["proxyjump"] = TypedField(self, "proxyjump", descriptions["proxyjump"])
        network.append(field.row)
        field = self.switches["forwardagent"] = SwitchField(
            self, "forwardagent", descriptions["forwardagent"]
        )
        network.append(field.row)
        group.add(network)
        self.page.add(group)

        group = Adw.PreferencesGroup(
            title=_("Port Forwards"),
            description=_("Tunnels opened with every connection this block applies to"),
        )
        self.forward_list = _boxed_list()
        self.add_forward_row = Adw.ActionRow(title=_("Add Forward"))
        self.forward_kind = Gtk.DropDown.new_from_strings(
            ["LocalForward", "RemoteForward", "DynamicForward"]
        )
        self.forward_kind.set_valign(Gtk.Align.CENTER)
        self.forward_kind.connect("notify::selected", lambda *_a: self._sync_forward_hint())
        self.forward_spec = _entry(width=22)
        self.forward_spec.connect("activate", lambda *_a: self._on_add_forward())
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text=_("Add Forward"))
        add.set_valign(Gtk.Align.CENTER)
        add.connect("clicked", lambda *_a: self._on_add_forward())
        for widget in (self.forward_kind, self.forward_spec, add):
            self.add_forward_row.add_suffix(widget)
        self.forward_list.append(self.add_forward_row)
        self._sync_forward_hint()
        group.add(self.forward_list)
        self.page.add(group)

        # -- everything else ----------------------------------------------------
        self.other_group = Adw.PreferencesGroup(
            title=_("Other Options"),
            description=_("Lines no field above shows, in the block's order"),
        )
        add_option = Gtk.Button()
        add_option.set_child(Adw.ButtonContent(icon_name="list-add-symbolic", label=_("Add Option")))
        add_option.add_css_class("flat")
        add_option.connect("clicked", lambda *_a: controller.add_option(self.uid, keep=True))
        self.other_group.set_header_suffix(add_option)
        self.other_list = _boxed_list()
        none = Gtk.Label(label=_("No other options."), xalign=0)
        none.add_css_class("dim-label")
        for side in ("top", "bottom", "start", "end"):
            getattr(none, f"set_margin_{side}")(12)
        self.other_list.set_placeholder(none)
        self.other_group.add(self.other_list)
        self.page.add(self.other_group)

    # -- showing a block -------------------------------------------------------

    def keep(self, uid: int) -> None:
        """Keep a line added from the table in the table."""
        self._keep.add(uid)

    def show(self, block: Optional[Block]) -> None:
        """Show *block* (None: none), in place when it is the one shown."""
        if block is None:
            self.uid = None
            self.stack.set_visible_child_name("empty")
            return
        if block.uid != self.uid:
            self.uid = block.uid
            self._keep = set()
            for rows, listbox in (
                (self._identity_rows, self.auth_list),
                (self._forward_rows, self.forward_list),
                (self._other_rows, self.other_list),
            ):
                for row in rows.values():
                    listbox.remove(row.row)
                    if hasattr(row, "destroy"):
                        row.destroy()
                rows.clear()
            self.page.scroll_to_top()
        self.stack.set_visible_child_name("block")
        self._sync(block)
        self.show_answer(self._answer)

    def _sync(self, block: Block) -> None:
        self.kind = block.kind
        options = {option.uid: option for option in block.options}
        claims = fields.claim(block.options, self._keep)
        is_global = block.kind == GLOBAL
        self.block_group.set_title(GLib.markup_escape_text(
            messages.block_title(block.kind, block.patterns)
        ))
        self.patterns_row.set_visible(not is_global)
        self.note_row.set_visible(not is_global)
        self.duplicate_button.set_visible(not is_global)
        title = _("Host patterns") if block.kind == HOST else _("Match conditions")
        if self.patterns_row.get_title() != title:
            self.patterns_row.set_title(title)
        self.syncing.set_text(self.patterns_row, block.patterns)
        self.syncing.set_text(self.note_row, block.note)
        if self.enabled_row.get_active() == block.disabled:
            self.syncing.on = True
            try:
                self.enabled_row.set_active(not block.disabled)
            finally:
                self.syncing.on = False

        for keyword, field in self.typed.items():
            uid = claims.single.get(keyword)
            field.bind(options.get(uid) if uid is not None else None)
        for keyword, field in self.switches.items():
            uid = claims.switches.get(keyword)
            field.bind(options.get(uid) if uid is not None else None)

        self._sync_rows(self._identity_rows, claims.identities, options, IdentityRow,
                        self.auth_list, first_index=0)
        self.identity_hint.set_visible(not claims.identities)
        self._sync_rows(self._forward_rows, claims.forwards, options, ForwardRow,
                        self.forward_list, first_index=0)
        self._sync_rows(self._other_rows, claims.other, options, OtherRow,
                        self.other_list, first_index=0)

    def _sync_rows(self, rows: Dict[int, object], uids: List[int], options, factory,
                   listbox: Gtk.ListBox, first_index: int) -> None:
        for uid in [uid for uid in rows if uid not in uids]:
            row = rows.pop(uid)
            listbox.remove(row.row)
            if hasattr(row, "destroy"):
                row.destroy()
        for position, uid in enumerate(uids):
            row = rows.get(uid)
            index = first_index + position
            if row is None:
                row = rows[uid] = factory(self, uid)
                listbox.insert(row.row, index)
            elif row.row.get_index() != index:
                listbox.remove(row.row)
                listbox.insert(row.row, index)
            row.bind(options[uid])

    def _sync_forward_hint(self) -> None:
        hints = {
            0: "8080 localhost:80",
            1: "9000 localhost:9000",
            2: "1080",
        }
        self.forward_spec.set_placeholder_text(hints.get(self.forward_kind.get_selected(), ""))

    # -- OpenSSH's answer -------------------------------------------------------

    def show_answer(self, answer: AnswerView) -> None:
        self._answer = answer
        if self.uid is None:
            return
        for field in (*self.typed.values(), *self.switches.values()):
            field.show(answer)
        for rows in (self._identity_rows, self._forward_rows, self._other_rows):
            for row in rows.values():
                row.show(answer)
        # The identity files OpenSSH will offer when this block sets none.
        value, source = answer.inherited("identityfile")
        if value:
            hint = _("Not set here. OpenSSH tries: {files} ({source})").format(
                files=value, source=source
            ) if source else _("Not set here. OpenSSH tries: {files}").format(files=value)
        else:
            hint = _("Not set here.")
        self.identity_hint.set_subtitle(GLib.markup_escape_text(hint))
        self._show_routing(answer)
        self._show_status(answer)

    def _show_routing(self, answer: AnswerView) -> None:
        """The route and the tunnels, as OpenSSH resolves them for the host
        checked; this block's own forwards even without an answer."""
        stops = []
        if answer.evaluation is not None and answer.host:
            for hop in fields.jump_hops((answer.values.get("proxyjump") or [""])[0]):
                stops.append((hop, self.controller.hop_endpoint(fields.hop_host(hop)), True))
            endpoint = ""
            if answer.values.get("hostname"):
                endpoint = "{host}:{port}".format(
                    host=answer.values["hostname"][0],
                    port=(answer.values.get("port") or ["22"])[0],
                )
            stops.append((answer.host, endpoint, False))
        block = self.controller.editor.find_block(self.uid)
        own = [
            (option.keyword, option.value, option.uid)
            for option in (block.options if block is not None else ())
            if option.uid in self._forward_rows
        ]
        tunnels = [(keyword, value, uid, "") for keyword, value, uid in own]
        if answer.evaluation is not None and answer.applies is not False:
            # OpenSSH names no source for a keyword it collects: the line
            # behind a forward is the other block's line with that value
            # that it read.
            elsewhere = []
            for other in self.controller.editor.blocks:
                if other.uid == self.uid:
                    continue
                for option in other.options:
                    if option.keyword in fields.FORWARDS:
                        item, _error = answer.line_state(option.uid)
                        if item is not None and item.state in (State.APPLIED, State.ACCUMULATED):
                            elsewhere.append((option.keyword, option.value, other))
            for keyword in fields.FORWARDS:
                for value in answer.values.get(keyword, ()):
                    if any(k == keyword and fields.same_forward(keyword, v, value)
                           for k, v, _uid in own):
                        continue
                    found = next((block for k, v, block in elsewhere
                                  if k == keyword and fields.same_forward(keyword, v, value)), None)
                    if found is not None:
                        source = _("from {block}").format(
                            block=messages.block_title(found.kind, found.patterns)
                        )
                    elif answer.current:
                        # Another file of the configuration, or Settings.
                        source = messages.source_label(answer.sources.get(keyword))
                    else:
                        continue            # an answer one edit behind
                    tunnels.append((keyword, value, None, source))
        self.routing.show(stops, tunnels)

    def focus_add_forward(self) -> None:
        self.forward_spec.grab_focus()

    def _show_status(self, answer: AnswerView) -> None:
        block = self.controller.editor.find_block(self.uid)
        disabled = block is not None and block.disabled
        if disabled:
            text = _("Off: OpenSSH does not read this block.")
        elif answer.related_hosts is not None:
            text = messages.related_note(answer.related_hosts)
        elif answer.evaluation is not None:
            applies = answer.applies if block is None or block.kind != GLOBAL else True
            text = messages.applies_text(applies, answer.host)
        elif answer.host:
            text = _("Asking OpenSSH about {host}…").format(host=answer.host)
        else:
            text = _("Type a host to check above to see what OpenSSH would use for it.")
        self.status_row.set_subtitle(GLib.markup_escape_text(text))
        header = self.controller.editor.lines.headers.get(self.uid, 0)
        error = answer.errors.get(header) if header else None
        self.error_row.set_visible(error is not None)
        if error is not None:
            self.error_row.set_title(GLib.markup_escape_text(messages.error_text(error)))

    # -- gestures ----------------------------------------------------------------

    def _on_patterns_changed(self, row) -> None:
        if not self.syncing.on and self.uid is not None:
            self.controller.patterns_typed(self.uid, row.get_text())

    def _on_note_changed(self, row) -> None:
        if not self.syncing.on and self.uid is not None:
            self.controller.note_typed(self.uid, row.get_text())

    def _on_enabled(self, row, _pspec) -> None:
        if self.syncing.on or self.uid is None:
            return
        block = self.controller.editor.find_block(self.uid)
        if block is not None and block.disabled == row.get_active():
            self.controller.toggle_block(self.uid)

    def _on_add_forward(self) -> None:
        spec = self.forward_spec.get_text().strip()
        if not spec or self.uid is None:
            return
        keyword = ("localforward", "remoteforward", "dynamicforward")[
            self.forward_kind.get_selected()
        ]
        self.syncing.set_text(self.forward_spec, "")
        self.controller.add_forward(self.uid, keyword, spec)

    # -- focus, for the controller -------------------------------------------------

    def focus_patterns(self) -> None:
        if self.patterns_row.get_visible():
            self.patterns_row.grab_focus()

    def option_widget(self, uid: int) -> Optional[Gtk.Widget]:
        """The field showing option *uid*, to focus or flash."""
        for field in self.typed.values():
            if field.uid == uid:
                return field.entry
        for field in self.switches.values():
            if field.uid == uid:
                return field.row
        for rows in (self._identity_rows, self._forward_rows):
            if uid in rows:
                return rows[uid].entry
        if uid in self._other_rows:
            return self._other_rows[uid].value
        return None

    def line_status(self, uid: int) -> Optional[str]:
        """What is said beside option *uid* (Not used, Overridden,
        Rejected), or None: nothing needs saying, or it is not shown."""
        pills = [field.state for field in (*self.typed.values(), *self.switches.values())
                 if field.uid == uid]
        for rows in (self._identity_rows, self._forward_rows, self._other_rows):
            if uid in rows:
                pills.append(rows[uid].state)
        for label in pills:
            return label.get_label() if label.get_visible() else None
        return None

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
            widget = self.patterns_row if self.patterns_row.get_visible() else self.block_group
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

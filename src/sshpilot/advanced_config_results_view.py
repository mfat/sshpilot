"""Advanced Configuration, right pane: what OpenSSH resolves.

Four pages: the effective values with the line behind each, the order the
configuration is read in, the text itself, and the files ``Include`` lines
read.

Showing an answer has to be cheap, because one arrives after every click
and every pause in typing:

* only the page on screen is filled; the others are marked stale and filled
  when they are opened;
* the effective values keep one list item per option for the life of the
  window and change it in place. ``ssh -G`` prints the same eighty-odd
  options every time; building a row of widgets for each, for every
  answer, was the bulk of the delay.
"""
from __future__ import annotations

from dataclasses import dataclass
from gettext import gettext as _
from typing import Callable, Dict, List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from .advanced_config_rules_view import pill, set_pill  # noqa: E402
from .api.models.connections import (  # noqa: E402
    SshConfigDraftEvaluation,
    SshConfigDraftLineState as State,
)
from .connection_dialog_auth import canonical_keyword  # noqa: E402
from .gtk import advanced_config_messages as messages  # noqa: E402
from .ssh_config_blocks import GLOBAL, HOST, Block, LineMap  # noqa: E402

_PAGES = ("effective", "trace", "raw", "includes")


@dataclass
class Answer:
    """One evaluation and the draft it was about."""

    evaluation: Optional[SshConfigDraftEvaluation]
    blocks: List[Block]
    lines: LineMap
    user: Optional[str] = None
    host: str = ""

    @property
    def answered(self) -> bool:
        return bool(self.evaluation and self.evaluation.evaluated)


class EffectiveItem(GObject.Object):
    """One option of ``ssh -G``: what the Effective list shows for it."""

    def __init__(self, key: str) -> None:
        super().__init__()
        self.key = key
        #: The row showing this item, while it has one.
        self.cell: Optional["_EffectiveCell"] = None
        #: The last answer has this option.
        self.present = False
        self.set([], None, forced=False, default=True, unused=0)

    def set(self, values: List[str], source, *, forced: bool, default: bool,
            unused: int) -> None:
        """Take a new answer; the row showing the item follows."""
        self.values = "\n".join(values)
        self.forced = forced
        self.unused = unused
        self.configured = forced or source is not None or not default
        #: The draft's line that supplies the value (0: none of its lines).
        self.line = 0
        self.tooltip = ""
        if forced:
            self.source = ""
        elif source is None:
            self.source = messages.default_source() if default else messages.unknown_source()
        else:
            self.source = messages.where(source.file, source.line, source.block)
            if source.file:
                self.tooltip = self.source
            else:
                self.line = source.line
                self.tooltip = _("Show this line in the rules")
        if self.cell is not None:
            self.cell.bind(self)


class _EffectiveCell:
    """The widgets of one visible row; bound to whichever item scrolls in."""

    def __init__(self, on_jump: Callable[[int], None]) -> None:
        self.item: Optional[EffectiveItem] = None
        self.box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.set_hexpand(True)
        self.key = Gtk.Label(xalign=0)
        self.key.add_css_class("caption")
        self.key.add_css_class("dim-label")
        text.append(self.key)
        self.value = Gtk.Label(xalign=0, selectable=True, wrap=True)
        self.value.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        text.append(self.value)
        self.box.append(text)
        self.unused = pill(style="warning")
        self.box.append(self.unused)
        self.forced = pill(messages.forced_source(), "purple")
        self.box.append(self.forced)
        # A source in this configuration is a button that shows the line; a
        # source elsewhere is the same button, inert.
        self.source = Gtk.Button()
        self.source.add_css_class("flat")
        self.source.add_css_class("caption")
        self.source.set_valign(Gtk.Align.CENTER)
        self.label = Gtk.Label()
        self.label.set_ellipsize(Pango.EllipsizeMode.START)
        self.label.set_max_width_chars(34)
        self.source.set_child(self.label)
        self.source.connect(
            "clicked", lambda *_a: self.item and self.item.line and on_jump(self.item.line)
        )
        self.box.append(self.source)

    def bind(self, item: EffectiveItem) -> None:
        if self.item is not None and self.item is not item and self.item.cell is self:
            self.item.cell = None
        self.item = item
        item.cell = self
        self.key.set_label(item.key)
        self.value.set_label(item.values or "—")
        (self.value.remove_css_class if item.configured else self.value.add_css_class)("dim-label")
        set_pill(self.unused, messages.shadowed_count(item.unused) if item.unused else "", "warning")
        self.forced.set_visible(item.forced)
        self.source.set_visible(not item.forced)
        self.label.set_label(item.source)
        self.source.set_tooltip_text(item.tooltip or None)
        self.source.set_sensitive(bool(item.line))
        (self.label.remove_css_class if item.line else self.label.add_css_class)("dim-label")


class ResultsView(Gtk.Box):
    """The stack of result pages.

    *controller* supplies ``reveal_line(line)``, ``raw_text_changed()`` and
    ``apply_raw()``.
    """

    def __init__(self, controller) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.controller = controller
        self.set_size_request(380, -1)
        self._answer = Answer(None, [], LineMap({}, {}))
        self._items: Dict[str, EffectiveItem] = {}
        self._stale = set(_PAGES)
        self._text = ""
        self._raw_syncing = False

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for side in ("top", "bottom"):
            getattr(header, f"set_margin_{side}")(6)
        header.set_margin_start(12)
        header.set_margin_end(16)
        self.stack = Adw.ViewStack()
        self.stack.set_vexpand(True)
        switcher = Adw.ViewSwitcher(stack=self.stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        switcher.set_hexpand(True)
        header.append(switcher)
        self.matched_count = pill(style="success")
        header.append(self.matched_count)
        self.append(header)
        self.append(Gtk.Separator())
        self.append(self.stack)
        self.stack.add_titled_with_icon(
            self._build_effective(), "effective", _("Effective"), "emblem-ok-symbolic")
        self.stack.add_titled_with_icon(
            self._build_list("trace_list", _(
                "The order OpenSSH reads the configuration in for this target. "
                "The first value it obtains for an option is the one it keeps."
            )), "trace", _("Order"), "view-list-symbolic")
        self.stack.add_titled_with_icon(
            self._build_raw(), "raw", _("Text"), "text-x-generic-symbolic")
        self.stack.add_titled_with_icon(
            self._build_list("include_list", _(
                "Files OpenSSH reads for this target besides this one. They are "
                "read where their Include line stands, and are not edited here."
            )), "includes", _("Includes"), "folder-documents-symbolic")
        self.stack.connect("notify::visible-child-name", lambda *_a: self._fill_visible())

    # -- construction --------------------------------------------------------

    @staticmethod
    def _page() -> tuple:
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(14)
        scroller.set_child(box)
        return scroller, box

    def _build_effective(self) -> Gtk.Widget:
        # The summary and the filters stay put; the list scrolls on its own.
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(14)
        self.summary = Adw.PreferencesGroup(title=_("Connection"))
        self.summary_rows: Dict[str, Adw.ActionRow] = {}
        for key, title in (
            ("command", _("Command")),
            ("host", _("Host name and port")),
            ("user", _("User")),
            ("proxy", _("Proxy")),
            ("keys", _("Identity files")),
        ):
            row = Adw.ActionRow(title=title)
            row.set_subtitle_selectable(True)
            row.add_css_class("property")
            self.summary.add(row)
            self.summary_rows[key] = row
        self.summary.set_visible(False)
        box.append(self.summary)
        self.filters = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.search = Gtk.SearchEntry(placeholder_text=_("Filter options or values"))
        self.search.set_hexpand(True)
        self.search.connect("search-changed", lambda *_a: self._filter_effective())
        self.filters.append(self.search)
        self.only_configured = Gtk.ToggleButton(label=_("Configured"))
        self.only_configured.set_tooltip_text(_("Hide options left at OpenSSH's default"))
        self.only_shadowed = Gtk.ToggleButton(label=_("Shadowed"))
        self.only_shadowed.set_tooltip_text(_("Only options with lines that are not used"))
        for toggle in (self.only_configured, self.only_shadowed):
            toggle.connect("toggled", lambda *_a: self._filter_effective())
            self.filters.append(toggle)
        self.filters.set_visible(False)
        box.append(self.filters)

        self.effective_store = Gio.ListStore(item_type=EffectiveItem)
        self._effective_filter = Gtk.CustomFilter.new(self._effective_match)
        self.effective_model = Gtk.FilterListModel(
            model=self.effective_store, filter=self._effective_filter
        )
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_effective_row)
        factory.connect("bind", lambda _f, list_item: list_item._cell.bind(list_item.get_item()))
        self.effective_list = Gtk.ListView(
            model=Gtk.NoSelection(model=self.effective_model), factory=factory
        )
        self.effective_list.add_css_class("rich-list")
        self.effective_scroller = Gtk.ScrolledWindow()
        self.effective_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.effective_scroller.set_vexpand(True)
        self.effective_scroller.add_css_class("card")
        self.effective_scroller.set_child(self.effective_list)
        self.effective_scroller.set_visible(False)
        box.append(self.effective_scroller)
        self.note = Gtk.Label(xalign=0, yalign=0, wrap=True)
        self.note.add_css_class("dim-label")
        self.note.set_vexpand(True)
        box.append(self.note)
        return box

    def _setup_effective_row(self, _factory, list_item) -> None:
        cell = _EffectiveCell(self.controller.reveal_line)
        list_item._cell = cell
        list_item.set_child(cell.box)

    def _effective_match(self, item: EffectiveItem) -> bool:
        if not item.present:
            return False
        if self.only_configured.get_active() and not item.configured:
            return False
        if self.only_shadowed.get_active() and not item.unused:
            return False
        needle = self.search.get_text().strip().lower()
        return not needle or needle in item.key or needle in item.values.lower()

    def _filters_active(self) -> bool:
        return bool(
            self.only_configured.get_active() or self.only_shadowed.get_active()
            or self.search.get_text().strip()
        )

    def _filter_effective(self) -> None:
        self._effective_filter.changed(Gtk.FilterChange.DIFFERENT)

    def effective_items(self) -> List[EffectiveItem]:
        """The options listed now, after the filters."""
        model = self.effective_model
        return [model.get_item(index) for index in range(model.get_n_items())]

    def _build_list(self, attribute: str, hint: str) -> Gtk.Widget:
        scroller, box = self._page()
        label = Gtk.Label(label=hint, xalign=0, wrap=True)
        label.add_css_class("dim-label")
        box.append(label)
        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        box.append(listbox)
        setattr(self, attribute, listbox)
        return scroller

    def _build_raw(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(14)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hint = Gtk.Label(
            label=_("The configuration as it will be saved. Edit it and apply to "
                    "read it into the rules again."),
            xalign=0, wrap=True,
        )
        hint.add_css_class("dim-label")
        hint.set_hexpand(True)
        top.append(hint)
        self.revert_raw_button = Gtk.Button(label=_("Discard"))
        self.revert_raw_button.set_valign(Gtk.Align.CENTER)
        self.revert_raw_button.set_sensitive(False)
        self.revert_raw_button.set_tooltip_text(_("Drop the text typed here"))
        self.revert_raw_button.connect("clicked", lambda *_a: self.controller.discard_raw())
        top.append(self.revert_raw_button)
        self.apply_raw_button = Gtk.Button(label=_("Apply"))
        self.apply_raw_button.set_valign(Gtk.Align.CENTER)
        self.apply_raw_button.set_sensitive(False)
        self.apply_raw_button.connect("clicked", lambda *_a: self.controller.apply_raw())
        top.append(self.apply_raw_button)
        box.append(top)
        frame = Gtk.Frame()
        frame.set_vexpand(True)
        scroller = Gtk.ScrolledWindow()
        self.raw_view = Gtk.TextView(monospace=True)
        for side in ("top", "bottom", "left", "right"):
            getattr(self.raw_view, f"set_{side}_margin")(10)
        self.raw_view.get_buffer().connect("changed", self._on_raw_changed)
        scroller.set_child(self.raw_view)
        frame.set_child(scroller)
        box.append(frame)
        return box

    # -- the Text page -------------------------------------------------------

    @property
    def raw_pending(self) -> bool:
        """Text was typed here and not applied to the rules."""
        return self.apply_raw_button.get_sensitive()

    def raw_text(self) -> str:
        buffer = self.raw_view.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)

    def discard_raw(self) -> None:
        self.apply_raw_button.set_sensitive(False)
        self.revert_raw_button.set_sensitive(False)
        self._stale.add("raw")
        self._fill_visible()

    def set_text(self, text: str) -> None:
        """The draft's text changed; the page follows unless it is being
        edited itself."""
        self._text = text
        self._stale.add("raw")
        self._fill_visible()

    def _on_raw_changed(self, _buffer) -> None:
        if self._raw_syncing:
            return
        pending = self.raw_text() != self._text
        self.apply_raw_button.set_sensitive(pending)
        self.revert_raw_button.set_sensitive(pending)
        self.controller.raw_text_changed()

    def _fill_raw(self) -> None:
        if self.raw_pending:
            return
        if self.raw_text() != self._text:
            self._raw_syncing = True
            self.raw_view.get_buffer().set_text(self._text)
            self._raw_syncing = False

    # -- answers -------------------------------------------------------------

    def show(self, answer: Answer, matched: int) -> None:
        """Take a new answer; fill the page on screen now, the rest later."""
        self._answer = answer
        self._stale.update(("effective", "trace", "includes"))
        set_pill(
            self.matched_count,
            messages.matched_count(matched) if answer.answered else "", "success",
        )
        self._fill_visible()

    def _fill_visible(self) -> None:
        name = self.stack.get_visible_child_name()
        if name not in self._stale:
            return
        self._stale.discard(name)
        getattr(self, f"_fill_{name}")()

    def fill_all(self) -> None:
        """Fill every page now (tests look at pages that are not open)."""
        for name in list(self._stale):
            self._stale.discard(name)
            getattr(self, f"_fill_{name}")()

    def _fill_effective(self) -> None:
        answer = self._answer
        evaluation = answer.evaluation
        self.summary.set_visible(answer.answered)
        self.filters.set_visible(answer.answered)
        self.effective_scroller.set_visible(answer.answered)
        self.note.set_visible(not answer.answered)
        if not answer.answered:
            if evaluation is None:
                note = _(
                    "Select a block that names a host, or filter by a host, to "
                    "see what OpenSSH would use for it."
                )
            elif evaluation.pending_exec:
                note = messages.pending_exec_text(evaluation)
            else:
                note = "\n".join(messages.error_text(e) for e in evaluation.errors)
            self.note.set_label(note)
            return
        values: Dict[str, List[str]] = {}
        for item in evaluation.values:
            values.setdefault(item.key, []).append(item.value)

        def first(key, fallback=""):
            return (values.get(key) or [fallback])[0]

        proxy = first("proxyjump") or first("proxycommand")
        command = "ssh " + (f"-l {answer.user} " if answer.user else "") + answer.host
        for key, text in (
            ("command", command),
            ("host", f"{first('hostname', answer.host)} : {first('port', '22')}"),
            ("user", first("user")),
            ("proxy", proxy if proxy and proxy != "none" else "—"),
            ("keys", "\n".join(values.get("identityfile", ())) or "—"),
        ):
            escaped = GLib.markup_escape_text(text or "—")
            if self.summary_rows[key].get_subtitle() != escaped:
                self.summary_rows[key].set_subtitle(escaped)

        sources = {source.key: source for source in evaluation.sources}
        forced = {item.key for item in evaluation.forced}
        defaults = set(evaluation.defaults)
        unused: Dict[str, int] = {}
        option_at = {number: uid for uid, number in answer.lines.options.items()}
        keywords = {
            option.uid: canonical_keyword(option.keyword)
            for block in answer.blocks for option in block.options
        }
        for item in evaluation.lines:
            if item.state in (State.SHADOWED, State.FORCED):
                name = keywords.get(option_at.get(item.line))
                if name:
                    unused[name] = unused.get(name, 0) + 1
        # One item per option, kept for the life of the window. Which
        # options ssh -G prints varies a little from host to host (ProxyJump
        # only where one is set), so an item is added the first time its
        # option appears and merely hidden when an answer lacks it: the rows
        # already made are never thrown away.
        presence_changed = False
        added = [key for key in sorted(values) if key not in self._items]
        for key in added:
            self._items[key] = EffectiveItem(key)
        for key, item in self._items.items():
            present = key in values
            presence_changed = presence_changed or (present != item.present and key not in added)
            item.present = present
            if present:
                item.set(
                    values[key], sources.get(key), forced=key in forced,
                    default=key in defaults, unused=unused.get(key, 0),
                )
        self._add_items(added)
        if presence_changed or self._filters_active():
            # Which options are listed may have changed with the answer.
            # Otherwise the list is left alone and only its texts change.
            self._filter_effective()

    def _add_items(self, keys: List[str]) -> None:
        """List new options. The first answer brings some eighty at once:
        the ones that fit on screen are listed now and the rest a few per
        idle turn, so the answer is on screen without waiting for them."""
        if not keys:
            return
        if self.effective_store.get_n_items():
            order = sorted(self._items)
            for key in keys:                      # a late-comer, in its place
                self.effective_store.insert(
                    min(order.index(key), self.effective_store.get_n_items()), self._items[key]
                )
            return
        pending = list(keys)

        def step() -> bool:
            for key in pending[:16]:
                self.effective_store.append(self._items[key])
            del pending[:16]
            return bool(pending)

        if step():
            GLib.idle_add(step, priority=GLib.PRIORITY_DEFAULT_IDLE)

    @staticmethod
    def _clear(listbox: Gtk.ListBox) -> None:
        while (child := listbox.get_first_child()) is not None:
            listbox.remove(child)

    def _fill_trace(self) -> None:
        answer = self._answer
        self._clear(self.trace_list)
        if not answer.answered:
            return
        evaluation = answer.evaluation
        states = {item.line: item.state for item in evaluation.lines}
        matched = set(evaluation.matched_blocks)

        def add(title, subtitle, suffix=None):
            row = Adw.ActionRow(title=GLib.markup_escape_text(title))
            row.set_subtitle(GLib.markup_escape_text(subtitle))
            if suffix is not None:
                row.add_suffix(suffix)
            self.trace_list.append(row)

        if evaluation.forced:
            add(
                messages.forced_source(),
                ", ".join(f"{item.key}={item.value}" for item in evaluation.forced),
                pill(_("First"), "purple"),
            )
        for index, block in enumerate(answer.blocks):
            header = answer.lines.headers.get(block.uid, 0)
            applies = None if block.disabled else (
                block.kind == GLOBAL or header in matched
            )
            label, style, _tip = messages.block_status(
                block.disabled, applies, block.kind == GLOBAL
            )
            name = {GLOBAL: _("Global"), HOST: "Host"}.get(block.kind, "Match")
            counts: Dict[State, int] = {}
            for option in block.options:
                state = states.get(answer.lines.options.get(option.uid, 0))
                if state is not None:
                    counts[state] = counts.get(state, 0) + 1
            parts = [
                f"{messages.state_label(state)}: {counts[state]}"
                for state in (State.APPLIED, State.ACCUMULATED, State.INCLUDE,
                              State.SHADOWED, State.FORCED)
                if counts.get(state)
            ]
            add(f"#{index + 1}  {name} {block.patterns}".rstrip(),
                " · ".join(parts), pill(label, style))
        if evaluation.system_config:
            supplied = sum(1 for s in evaluation.sources if s.file == evaluation.system_config)
            add(
                _("System-wide configuration"),
                f"{evaluation.system_config} · {messages.state_label(State.APPLIED)}: {supplied}",
                pill(_("Last"), "muted"),
            )

    def _fill_includes(self) -> None:
        answer = self._answer
        self._clear(self.include_list)
        if not answer.answered:
            return
        evaluation = answer.evaluation
        option_at = {number: uid for uid, number in answer.lines.options.items()}
        patterns = {
            option.uid: option.value for block in answer.blocks for option in block.options
        }
        for include in evaluation.includes:
            pattern = patterns.get(option_at.get(include.line), "")
            row = Adw.ActionRow(title=GLib.markup_escape_text(f"Include {pattern}"))
            row.set_subtitle(GLib.markup_escape_text(
                "\n".join(include.files) or _("No file matches.")
            ))
            row.set_subtitle_selectable(True)
            self.include_list.append(row)
        if evaluation.system_config:
            row = Adw.ActionRow(title=_("System-wide configuration"))
            row.set_subtitle(GLib.markup_escape_text(evaluation.system_config))
            row.set_subtitle_selectable(True)
            self.include_list.append(row)
        if not evaluation.includes and not evaluation.system_config:
            self.include_list.append(
                Adw.ActionRow(title=_("No other file is read for this target."))
            )

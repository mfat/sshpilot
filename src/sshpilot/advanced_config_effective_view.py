"""Advanced Configuration, Effective view: what OpenSSH uses for the host
being checked (``ssh -G``), and where each value comes from.

Each value is counted as set by the block on screen, by another block or
file (Settings ▸ SSH Options included), or left at OpenSSH's default; a
chip per kind shows how many there are and filters the list. A source in
this configuration is a button that shows its line. A value with a problem
(lines of the option that go unused, a key file that is missing or that
other users can read) is marked with a warning and says what is wrong.

Showing an answer has to be cheap: one arrives after every click and every
pause in typing. The list keeps one item per option for the life of the
view and changes it in place; ``ssh -G`` prints the same eighty-odd options
every time, and building a row of widgets for each, for every answer, was
the bulk of the delay.
"""
from __future__ import annotations

from dataclasses import dataclass
from gettext import gettext as _
from typing import Dict, List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from . import advanced_config_fields as fields  # noqa: E402
from .advanced_config_widgets import pill, set_pill  # noqa: E402
from .api.models.connections import (  # noqa: E402
    SshConfigDraftEvaluation,
    SshConfigDraftLineState as State,
)
from .connection_dialog_auth import canonical_keyword  # noqa: E402
from .gtk import advanced_config_messages as messages  # noqa: E402
from .ssh_config_blocks import Block, LineMap  # noqa: E402

ALL, THIS_BLOCK, OTHER, DEFAULT = "all", "this", "other", "default"

def value_problems(key: str, values: List[str], *, default: bool) -> List[str]:
    """What is wrong with the files an option's values name, worded."""
    return [
        messages.value_problem_text(
            fields.home_relative(status.path),
            messages.key_status_text(status.state, status.mode),
        )
        for status in fields.value_file_problems(key, values, default=default)
    ]


@dataclass
class Answer:
    """One evaluation and the draft it was about."""

    evaluation: Optional[SshConfigDraftEvaluation]
    blocks: List[Block]
    lines: LineMap
    user: Optional[str] = None
    host: str = ""
    #: Said in place of the usual hint while there is no evaluation.
    note: str = ""
    #: The lines of the block on screen (header and options).
    block_lines: frozenset = frozenset()

    @property
    def answered(self) -> bool:
        return bool(self.evaluation and self.evaluation.evaluated)


class EffectiveItem(GObject.Object):
    """One option of ``ssh -G``: what the list shows for it."""

    def __init__(self, key: str) -> None:
        super().__init__()
        self.key = key
        #: The row showing this item, while it has one.
        self.cell: Optional["_EffectiveCell"] = None
        #: The last answer has this option.
        self.present = False
        self.kind = DEFAULT
        self.set([], None, forced=False, default=True, unused=0, block_lines=frozenset())

    def set(self, values: List[str], source, *, forced: bool, default: bool,
            unused: int, block_lines: frozenset) -> None:
        """Take a new answer; the row showing the item follows."""
        told = (tuple(values), source, forced, default, unused, block_lines)
        if told == getattr(self, "_told", None):
            return
        self._told = told
        self.values = "\n".join(values)
        self.forced = forced
        self.unused = unused
        #: What is wrong with the value, worded; empty when nothing is.
        self.problems = ([messages.shadowed_count(unused)] if unused else []) + \
            value_problems(self.key, values, default=default)
        #: The draft's line that supplies the value (0: none of its lines),
        #: or the other file and its line.
        self.line = 0
        self.file, self.file_line = "", 0
        self.tooltip = ""
        if forced:
            self.source = messages.forced_source()
            self.kind = OTHER
        elif source is None:
            self.source = messages.default_source() if default else messages.unknown_source()
            self.kind = DEFAULT if default else OTHER
        else:
            self.source = messages.where(source.file, source.line, source.block)
            if source.file:
                self.file, self.file_line = source.file, source.line
                self.tooltip = self.source
                self.kind = OTHER
            else:
                self.line = source.line
                self.tooltip = _("Show this line")
                self.kind = THIS_BLOCK if source.line in block_lines else OTHER
        if self.cell is not None:
            self.cell.bind(self)


#: Widths of the fixed columns, in characters, shared by the rows and the
#: header above them so the titles sit over their columns.
_KEY_CHARS = 24
_SOURCE_CHARS = 34


def _header_over(template: Gtk.Widget, title: str, *, in_button: bool = False) -> Gtk.Widget:
    """*title* in a column as wide as *template*, a widget built like the
    rows' (never shown: a stack is as wide as its widest page). In a flat
    button when the rows' text is in one, so the title starts where it does."""
    stack = Gtk.Stack(hhomogeneous=True)
    stack.add_child(template)
    label = Gtk.Label(label=title, xalign=0)
    label.add_css_class("heading")
    shown: Gtk.Widget = label
    if in_button:
        shown = Gtk.Button(child=label, can_target=False, focusable=False)
        shown.add_css_class("flat")
    stack.add_child(shown)
    stack.set_visible_child(shown)
    return stack


class _EffectiveCell:
    """The widgets of one visible row; bound to whichever item scrolls in."""

    def __init__(self, controller) -> None:
        self.controller = controller
        self.item: Optional[EffectiveItem] = None
        self.box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        # Kept in place when hidden, so the keys stay in one column.
        self.warning = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        self.warning.add_css_class("warning")
        self.warning.set_valign(Gtk.Align.START)
        self.warning.set_opacity(0)
        self.box.append(self.warning)
        self.key = Gtk.Label(xalign=0)
        self.key.add_css_class("monospace")
        self.key.set_width_chars(_KEY_CHARS)
        self.key.set_max_width_chars(_KEY_CHARS)
        self.key.set_ellipsize(Pango.EllipsizeMode.END)
        self.key.set_valign(Gtk.Align.START)
        self.box.append(self.key)
        self.value = Gtk.Label(xalign=0, selectable=True, wrap=True)
        self.value.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.value.add_css_class("monospace")
        self.value.set_hexpand(True)
        self.value.set_valign(self.key.get_valign())
        self.box.append(self.value)
        self.problem = pill(style="warning")
        self.box.append(self.problem)
        # A source in this configuration is a button that shows the line,
        # in this file or in another of its files; a source elsewhere (the
        # system-wide file) is the same button, inert.
        self.source = Gtk.Button()
        self.source.add_css_class("flat")
        self.source.add_css_class("caption")
        self.source.set_valign(Gtk.Align.CENTER)
        self.label = Gtk.Label(xalign=0)
        self.label.set_ellipsize(Pango.EllipsizeMode.START)
        self.label.set_width_chars(_SOURCE_CHARS)
        self.label.set_max_width_chars(_SOURCE_CHARS)
        self.source.set_child(self.label)
        self.source.connect("clicked", self._on_source_clicked)
        self.box.append(self.source)

    def _on_source_clicked(self, *_args) -> None:
        item = self.item
        if item is None:
            return
        if item.line:
            self.controller.reveal_line(item.line)
        elif item.file:
            self.controller.reveal_source(item.file, item.file_line)

    def bind(self, item: EffectiveItem) -> None:
        if self.item is not None and self.item is not item and self.item.cell is self:
            self.item.cell = None
        self.item = item
        item.cell = self
        self.key.set_label(item.key)
        self.key.set_tooltip_text(item.key)
        self.value.set_label(item.values or "—")
        problems = item.problems
        self.warning.set_opacity(1 if problems else 0)
        (self.key.add_css_class if problems else self.key.remove_css_class)("warning")
        # One problem is said in full; several are counted, and listed in
        # the tooltip.
        text = problems[0] if len(problems) == 1 else (
            messages.problem_count(len(problems)) if problems else ""
        )
        set_pill(self.problem, text, "warning", "\n".join(problems))
        self.label.set_label(item.source)
        self.source.set_tooltip_text(item.tooltip or None)
        reachable = bool(item.line) or (
            bool(item.file) and self.controller.source_file(item.file) is not None
        )
        # A source that cannot be shown (OpenSSH's default, the system-wide
        # file) is plain text: not clickable, but not greyed out either, as
        # an insensitive button would be.
        self.source.set_can_target(reachable)
        self.source.set_focusable(reachable)


class EffectiveView(Gtk.Box):
    """*controller* supplies ``reveal_line``, ``reveal_source`` and
    ``source_file``."""

    def __init__(self, controller) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.controller = controller
        for side in ("top", "bottom", "start", "end"):
            getattr(self, f"set_margin_{side}")(16)
        self._answer = Answer(None, [], LineMap({}, {}))
        self._items: Dict[str, EffectiveItem] = {}
        self._stale = True
        self._shown = False

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        bar.add_css_class("card")
        bar.add_css_class("advcfg-command-bar")
        self.command = Gtk.Label(xalign=0, selectable=True)
        self.command.add_css_class("monospace")
        self.command.set_hexpand(True)
        self.command.set_ellipsize(Pango.EllipsizeMode.END)
        bar.append(self.command)
        self.search = Gtk.SearchEntry(placeholder_text=_("Search options or values"))
        self.search.set_width_chars(24)
        self.search.connect("search-changed", lambda *_a: self._refilter())
        bar.append(self.search)
        self.append(bar)

        self._filter_kind = ALL
        self._filter = Gtk.CustomFilter.new(self._match)
        self.chips = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._chips: Dict[str, Gtk.ToggleButton] = {}
        group = None
        for kind in (ALL, THIS_BLOCK, OTHER, DEFAULT):
            chip = Gtk.ToggleButton(group=group)
            chip.add_css_class("advcfg-chip")
            chip.connect("toggled", self._on_chip)
            group = group or chip
            self._chips[kind] = chip
            self.chips.append(chip)
        self._chips[ALL].set_active(True)
        self._filter_kind = ALL
        self.append(self.chips)

        self.store = Gio.ListStore(item_type=EffectiveItem)
        self.model = Gtk.FilterListModel(model=self.store, filter=self._filter)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_row)
        factory.connect("bind", lambda _f, list_item: list_item._cell.bind(list_item.get_item()))
        self.list = Gtk.ListView(model=Gtk.NoSelection(model=self.model), factory=factory)
        self.list.add_css_class("rich-list")
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_vexpand(True)
        self.scroller.set_child(self.list)
        # The table: column titles over the list, in one card.
        self.table = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.table.add_css_class("card")
        self.table.set_vexpand(True)
        self.table.append(self._build_header())
        self.table.append(Gtk.Separator())
        self.table.append(self.scroller)
        self.append(self.table)
        self.note = Adw.StatusPage(icon_name="system-search-symbolic")
        self.note.set_vexpand(True)
        self.append(self.note)
        self._set_chip_labels({ALL: 0, THIS_BLOCK: 0, OTHER: 0, DEFAULT: 0})

    def _build_header(self) -> Gtk.Widget:
        """Titles over the columns of the rows: each laid out as a row's is."""
        cell = _EffectiveCell(self.controller)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        cell.box.remove(cell.warning)
        cell.warning.set_opacity(0)
        header.append(cell.warning)                     # the icon's column
        cell.box.remove(cell.key)
        header.append(_header_over(cell.key, _("Directive")))
        value = Gtk.Label(label=_("Effective Value"), xalign=0, hexpand=True)
        value.add_css_class("heading")
        header.append(value)
        cell.box.remove(cell.source)
        cell.source.set_can_target(False)
        header.append(_header_over(cell.source, _("Comes From"), in_button=True))
        # In a list styled as the rows' list, so the theme spaces and pads
        # the titles exactly as it does the columns under them.
        holder = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        holder.add_css_class("rich-list")
        holder.add_css_class("advcfg-table-header")
        holder.set_can_target(False)
        row = Gtk.ListBoxRow(activatable=False, selectable=False, focusable=False)
        row.set_child(header)
        holder.append(row)
        return holder

    def _setup_row(self, _factory, list_item) -> None:
        cell = _EffectiveCell(self.controller)
        list_item._cell = cell
        list_item.set_child(cell.box)

    # -- filters ---------------------------------------------------------------

    def _on_chip(self, chip) -> None:
        if not chip.get_active():
            return
        self._filter_kind = next(kind for kind, button in self._chips.items() if button is chip)
        self._refilter()

    def _match(self, item: EffectiveItem) -> bool:
        if not item.present:
            return False
        if self._filter_kind != ALL and item.kind != self._filter_kind:
            return False
        needle = self.search.get_text().strip().lower()
        return not needle or needle in item.key or needle in item.values.lower()

    def _refilter(self) -> None:
        self._filter.changed(Gtk.FilterChange.DIFFERENT)

    def _set_chip_labels(self, counts: Dict[str, int]) -> None:
        names = {
            ALL: _("All"),
            # Translators: a filter: values this block sets itself.
            THIS_BLOCK: _("Explicit"),
            # Translators: a filter: values set by another block or file
            # (Host *, an included file, Settings), not by this block.
            OTHER: _("Inherited"),
            DEFAULT: _("OpenSSH default"),
        }
        for kind, chip in self._chips.items():
            # Translators: a filter and how many options it keeps, e.g. "All 84".
            label = _("{name} {count}").format(name=names[kind], count=counts.get(kind, 0))
            if chip.get_label() != label:
                chip.set_label(label)

    def items(self) -> List[EffectiveItem]:
        """The options listed now, after the filters."""
        return [self.model.get_item(index) for index in range(self.model.get_n_items())]

    def select_filter(self, kind: str) -> None:
        self._chips[kind].set_active(True)

    # -- answers -----------------------------------------------------------------

    def set_shown(self, shown: bool) -> None:
        """The view is on screen (or not): it is filled only while it is."""
        self._shown = shown
        if shown and self._stale:
            self.fill()

    def show(self, answer: Answer) -> None:
        self._answer = answer
        self._stale = True
        if self._shown:
            self.fill()

    def fill(self) -> None:
        self._stale = False
        answer = self._answer
        evaluation = answer.evaluation
        command = "ssh -G " + (f"-l {answer.user} " if answer.user else "") + answer.host
        self.command.set_label(command if answer.host else "ssh -G")
        for widget in (self.chips, self.table):
            widget.set_visible(answer.answered)
        self.note.set_visible(not answer.answered)
        if not answer.answered:
            if evaluation is None:
                self.note.set_title(_("No Host Checked"))
                self.note.set_description(answer.note or _(
                    "Choose a block that names a host, or type a host to check, "
                    "to see what OpenSSH would use for it."
                ))
            elif evaluation.pending_exec:
                self.note.set_title(_("Waiting for Consent"))
                self.note.set_description(GLib.markup_escape_text(
                    messages.pending_exec_text(evaluation, answer.host)
                ))
            else:
                self.note.set_title(_("OpenSSH Rejects This Configuration"))
                self.note.set_description(GLib.markup_escape_text(
                    "\n".join(messages.error_text(e) for e in evaluation.errors)
                ))
            return
        values: Dict[str, List[str]] = {}
        for item in evaluation.values:
            values.setdefault(item.key, []).append(item.value)
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
        # One item per option, kept for the life of the view: an item is
        # added the first time its option appears, and merely hidden when
        # an answer lacks it.
        added = [key for key in sorted(values) if key not in self._items]
        for key in added:
            self._items[key] = EffectiveItem(key)
        counts = {ALL: 0, THIS_BLOCK: 0, OTHER: 0, DEFAULT: 0}
        presence_changed = kind_changed = False
        for key, item in self._items.items():
            kind = item.kind
            present = key in values
            presence_changed = presence_changed or (present != item.present and key not in added)
            item.present = present
            if item.present:
                item.set(
                    values[key], sources.get(key), forced=key in forced,
                    default=key in defaults, unused=unused.get(key, 0),
                    block_lines=answer.block_lines,
                )
                counts[ALL] += 1
                counts[item.kind] += 1
                kind_changed = kind_changed or item.kind != kind
        self._set_chip_labels(counts)
        self._add_items(added)
        # Refiltering makes every row anew: only when what is listed may
        # have changed. Otherwise only the texts of the rows change.
        filtering = self._filter_kind != ALL or bool(self.search.get_text().strip())
        if presence_changed or (kind_changed and filtering) or (added and filtering):
            self._refilter()

    def _add_items(self, keys: List[str]) -> None:
        if not keys:
            return
        if self.store.get_n_items():
            order = sorted(self._items)
            for key in keys:                      # a late-comer, in its place
                self.store.insert(min(order.index(key), self.store.get_n_items()), self._items[key])
            return
        for key in keys:
            self.store.append(self._items[key])

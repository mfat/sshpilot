"""Search in the connection editor's sidebar, as in the Settings window.

The search button (or Ctrl+F) opens a search bar above the list of sections;
what is typed is matched against the fields on every page shown for the
selected protocol, and the results take the place of the section list.
Choosing one opens its page, scrolls to the field and flashes it.

The rows are read from the live pages, so a field added to a page is found
without a table to keep in step. A row that writes an OpenSSH option is also
found by the option's keyword (``ControlMaster``, ``ProxyJump``), as a match
in its subtitle: the keyword the override marks already know it by
(``OVERRIDABLE_ROWS``), and any a row lists in ``_sshpilot_search_keywords``. ``rank_matches`` is the matching itself,
free of widgets.
"""

from __future__ import annotations

import logging
import unicodedata
from gettext import gettext as _
from typing import Dict, List, Optional, Sequence, Tuple

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk

logger = logging.getLogger(__name__)


def fold(text: Optional[str]) -> str:
    """Case- and accent-insensitive form of *text*."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def rank_matches(
    query: str,
    pages: Sequence[Tuple[str, str]],
    rows: Sequence[Tuple[str, str, str, str]],
) -> List[Tuple[str, Optional[int]]]:
    """What *query* finds, best first.

    *pages* is ``(page id, title)`` in sidebar order and *rows* is
    ``(page id, group title, row title, row subtitle)``. Every word of the
    query must be found. The result is ``(page id, index into rows)``, the
    index ``None`` for a page matched by its own name; those come first,
    then rows whose title starts with the query, rows whose title holds it,
    and rows matched through their subtitle, group or page.
    """
    tokens = fold(query).split()
    if not tokens:
        return []
    order = {page_id: index for index, (page_id, _title) in enumerate(pages)}
    titles = dict(pages)
    matches = []
    for page_id, title in pages:
        folded = fold(title)
        if folded and all(token in folded for token in tokens):
            matches.append((-1, order[page_id], 0, page_id, None))
    for index, (page_id, group, title, subtitle) in enumerate(rows):
        if page_id not in order:
            continue
        folded_title = fold(title)
        fields = (folded_title, fold(subtitle), fold(f"{group} {titles[page_id]}"))
        if not all(any(token in field for field in fields) for token in tokens):
            continue
        if folded_title.startswith(" ".join(tokens)):
            rank = 0
        elif all(token in folded_title for token in tokens):
            rank = 1
        else:
            rank = 2
        matches.append((rank, order[page_id], index, page_id, index))
    matches.sort(key=lambda match: match[:3])
    return [(page_id, index) for *_rank, page_id, index in matches]


class ConnectionDialogSearchMixin:
    """The sidebar search of ``ConnectionDialog``."""

    def _setup_sidebar_search(self) -> None:
        self._search_highlight_id = None
        self._search_highlight_row = None
        self.search_bar.connect_entry(self.search_entry)
        self.search_bar.connect("notify::search-mode-enabled", self._on_search_mode_changed)
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.search_entry.connect("activate", self._on_search_activate)
        self.search_entry.connect("stop-search", self._on_search_stop)
        self.search_results.connect("row-activated", self._on_search_result_activated)
        # Down from the entry moves into the results, as in a list search.
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_search_entry_key)
        self.search_entry.add_controller(keys)

    def start_search(self) -> bool:
        """Open the search bar and focus it (Ctrl+F).

        Not while a single page is shown without a sidebar: there is one
        short page to look at.
        """
        if getattr(self, "_single_page_layout", False):
            return False
        try:
            if self.split_view.get_collapsed():
                self.split_view.set_show_content(False)
        except Exception:
            pass
        self.search_bar.set_search_mode(True)
        self.search_entry.grab_focus()
        return True

    def _end_search(self) -> None:
        self.search_bar.set_search_mode(False)

    def _on_search_mode_changed(self, *_args) -> None:
        if not self.search_bar.get_search_mode():
            if self.search_entry.get_text():
                self.search_entry.set_text("")
            self.sidebar_stack.set_visible_child_name("pages")

    def _on_search_stop(self, _entry) -> None:
        # Escape leaves the search first; a second one cancels the dialog.
        self._end_search()
        selected = self.sidebar_list.get_selected_row()
        if selected is not None:
            selected.grab_focus()

    def _on_search_entry_key(self, _controller, keyval, _keycode, _state) -> bool:
        if keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
            first = self.search_results.get_row_at_index(0)
            if first is not None and self.sidebar_stack.get_visible_child_name() == "results":
                first.grab_focus()
                return True
        return False

    def _on_search_activate(self, _entry) -> None:
        # Enter opens the best match and ends the search; clicking a result
        # keeps the results up for the next one.
        first = self.search_results.get_row_at_index(0)
        if first is not None and self.sidebar_stack.get_visible_child_name() == "results":
            self._on_search_result_activated(self.search_results, first)
            self._end_search()

    # -- what there is to find ---------------------------------------------

    def _searchable_pages(self) -> List[Tuple[str, str]]:
        """``(page id, title)`` of the pages the sidebar shows, in its order."""
        rows = getattr(self, "_sidebar_rows", None) or {}
        titles = getattr(self, "_page_titles", None) or {}
        return [
            (name, titles.get(name, ""))
            for name, row in rows.items()
            if row.get_visible()
        ]

    def _iter_page_rows(self):
        """Yield ``(page id, group title, row)`` for every shown, titled row."""
        widgets = getattr(self, "_page_widgets", None) or {}
        for page_id, _title in self._searchable_pages():
            page = widgets.get(page_id)
            if page is None:
                continue
            stack = [(page, "", None)]
            while stack:
                widget, group, expander = stack.pop()
                # The page itself is hidden while another one is shown.
                if widget is not page and not widget.get_visible():
                    continue
                if isinstance(widget, Adw.PreferencesGroup):
                    group = widget.get_title() or ""
                if isinstance(widget, Adw.PreferencesRow):
                    title = widget.get_title() or ""
                    # An ExpanderRow draws its header with an inner ActionRow.
                    header = expander is not None and title == expander.get_title()
                    if title and not header:
                        yield page_id, group, widget
                    if isinstance(widget, Adw.ExpanderRow):
                        expander = widget
                children = []
                child = widget.get_first_child()
                while child is not None:
                    children.append((child, group, expander))
                    child = child.get_next_sibling()
                stack.extend(reversed(children))

    def search_fields(self, query: str):
        """Fields matching *query*, best first: ``[(page id, group, row)]``.

        ``row`` is ``None`` for a page matched by its name.
        """
        found = list(self._iter_page_rows())
        keywords = self._option_keywords()
        described = []
        for page_id, group, row in found:
            subtitle = ""
            get_subtitle = getattr(row, "get_subtitle", None)
            if callable(get_subtitle):
                subtitle = get_subtitle() or ""
            # Matched as the subtitle is, never shown.
            subtitle = " ".join(filter(None, (
                subtitle,
                keywords.get(id(row), ""),
                getattr(row, "_sshpilot_search_keywords", ""),
            )))
            described.append((page_id, group, row.get_title() or "", subtitle))
        results = []
        for page_id, index in rank_matches(query, self._searchable_pages(), described):
            if index is None:
                results.append((page_id, "", None))
            else:
                results.append((page_id, found[index][1], found[index][2]))
        return results

    def _option_keywords(self) -> Dict[int, str]:
        """The OpenSSH keyword of each row that writes one, by ``id(row)``."""
        from .connection_dialog_overrides import OVERRIDABLE_ROWS

        keywords = {}
        for row_name, keyword in OVERRIDABLE_ROWS:
            row = getattr(self, row_name, None)
            if row is not None:
                keywords[id(row)] = keyword
        return keywords

    def _on_search_changed(self, entry) -> None:
        query = entry.get_text()
        while (child := self.search_results.get_first_child()) is not None:
            self.search_results.remove(child)
        if not query.strip():
            self.sidebar_stack.set_visible_child_name("pages")
            return
        titles = getattr(self, "_page_titles", None) or {}
        results = self.search_fields(query)
        for page_id, group, target in results:
            row = Adw.ActionRow()
            try:
                row.set_use_markup(False)
            except Exception:
                pass
            row.set_activatable(True)
            page_title = titles.get(page_id, "")
            row.set_title(target.get_title() if target is not None else page_title)
            if target is None:
                row.set_subtitle(_("Page"))
            elif group and group != page_title:
                # Translators: where a search result lives, e.g. "Terminal › Mouse".
                row.set_subtitle(_("{page} › {group}").format(page=page_title, group=group))
            else:
                row.set_subtitle(page_title)
            icon = self._sidebar_page_icon(page_id)
            if icon is not None:
                row.add_prefix(icon)
            row._search_target = (page_id, target)
            self.search_results.append(row)
        self.sidebar_stack.set_visible_child_name("results" if results else "empty")

    def _on_search_result_activated(self, _listbox, row) -> None:
        target = getattr(row, "_search_target", None)
        if target is not None:
            self.reveal_field_row(*target)

    def reveal_field_row(self, page_id, target) -> None:
        """Show *target* on its page: open the page, scroll to it, flash it."""
        if not self.select_page(page_id) or target is None:
            return
        parent = target.get_parent()
        while parent is not None:
            if isinstance(parent, Adw.ExpanderRow) and not parent.get_expanded():
                parent.set_expanded(True)
            parent = parent.get_parent()

        def _focus():
            # Focusing scrolls the page's viewport to the row.
            try:
                target.grab_focus()
                self._flash_field_row(target)
            except Exception:
                logger.debug("Could not reveal a search result", exc_info=True)
            return GLib.SOURCE_REMOVE

        # After the page switch, so the page is mapped and allocated.
        GLib.timeout_add(150, _focus)

    def _flash_field_row(self, row) -> None:
        # The Settings search's highlight (style.css).
        row.add_css_class("settings-search-highlight")
        if self._search_highlight_id:
            GLib.source_remove(self._search_highlight_id)
            previous = self._search_highlight_row
            if previous is not None and previous is not row:
                previous.remove_css_class("settings-search-highlight")

        def _clear():
            self._search_highlight_id = None
            row.remove_css_class("settings-search-highlight")
            return GLib.SOURCE_REMOVE

        self._search_highlight_row = row
        self._search_highlight_id = GLib.timeout_add(1500, _clear)

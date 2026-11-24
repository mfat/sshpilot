"""The sidebar's views: Connections, and the Rules of the SSH configuration.

A dropdown at the top of the sidebar changes what the list below shows. The
connection list is as it was. The Rules view lists what the configuration
holds besides connections (``sidebar_rules``), in file order; a rule opens in
Advanced Configuration, where rules are edited and ordered. Nothing is
edited, moved or deleted from this list.
"""

from __future__ import annotations

import logging
import threading
from gettext import gettext as _

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

from .sidebar_rules import matches, read_rules, summary
from .ssh_config_blocks import GLOBAL, MATCH

logger = logging.getLogger(__name__)

CONNECTIONS = "connections"
RULES = "rules"


class SidebarRulesView(Gtk.Stack):
    """The list of rules, or a line saying there are none."""

    def __init__(self, window):
        super().__init__()
        self._window = window
        self._rules = []
        self._query = ""
        self._generation = 0
        self._loaded = False

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.listbox.add_css_class("navigation-sidebar")
        self.listbox.connect("row-activated", self._on_row_activated)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_min_content_width(0)
        scrolled.set_child(self.listbox)
        self.add_named(scrolled, "list")

        self.empty = Adw.StatusPage()
        self.empty.add_css_class("compact")
        self.empty.set_icon_name("document-properties-symbolic")
        self.add_named(self.empty, "empty")
        self._show_empty(loading=True)

    # -- reading ----------------------------------------------------------

    def refresh(self) -> None:
        """Read the configuration again, off the main loop."""
        client = getattr(self._window, "client", None)
        if client is None or not hasattr(client, "get_ssh_config_text"):
            self._set_rules([], failed=True)
            return
        self._generation += 1
        generation = self._generation

        def work():
            try:
                root = client.get_ssh_config_text()
                rules = read_rules(root.text)
                for ref in tuple(getattr(root, "files", ()) or ())[1:]:
                    part = client.get_ssh_config_text(ref.file)
                    rules.extend(read_rules(part.text, ref.file, ref.display_name))
                failed = False
            except Exception:
                logger.debug("Could not read the rules of the configuration", exc_info=True)
                rules, failed = [], True

            def done():
                if generation == self._generation:
                    self._set_rules(rules, failed=failed)
                return GLib.SOURCE_REMOVE

            GLib.idle_add(done)

        threading.Thread(target=work, name="sidebar-rules", daemon=True).start()

    def set_filter(self, query: str) -> None:
        query = query or ""
        if query != self._query:
            self._query = query
            if self._loaded:
                self._fill()

    @property
    def rules(self):
        return tuple(self._rules)

    def _set_rules(self, rules, failed: bool = False) -> None:
        self._rules = list(rules)
        self._loaded = True
        self._failed = failed
        self._fill()

    # -- drawing ----------------------------------------------------------

    def _show_empty(self, loading=False, failed=False, filtered=False) -> None:
        if loading:
            self.empty.set_title(_("Reading the Configuration…"))
            self.empty.set_description("")
        elif failed:
            self.empty.set_title(_("Rules Unavailable"))
            self.empty.set_description(_("The SSH configuration could not be read."))
        elif filtered:
            self.empty.set_title(_("No Results"))
            self.empty.set_description(_("Try a different search."))
        else:
            self.empty.set_title(_("No Rules"))
            self.empty.set_description(_(
                "A rule sets options for every host it matches, such as "
                "“Host *.example.com”. Add one from the + menu."
            ))
        self.set_visible_child_name("empty")

    def _fill(self) -> None:
        while (child := self.listbox.get_first_child()) is not None:
            self.listbox.remove(child)
        shown = [rule for rule in self._rules if matches(rule, self._query)]
        if not shown:
            self._show_empty(
                failed=getattr(self, "_failed", False),
                filtered=bool(self._rules),
            )
            return
        for rule in shown:
            self.listbox.append(self._row(rule))
        self.set_visible_child_name("list")

    def _row(self, rule):
        row = Adw.ActionRow()
        try:
            row.set_use_markup(False)
        except Exception:
            pass
        if rule.kind == GLOBAL:
            title = _("Global Options")
            icon = "preferences-system-symbolic"
        elif rule.kind == MATCH:
            # "Match" is the OpenSSH keyword, as written in the file.
            title = "Match " + rule.patterns
            icon = "system-search-symbolic"
        else:
            title = rule.patterns
            icon = "document-properties-symbolic"
        row.set_title(title)
        row.set_title_lines(1)
        row.set_subtitle_lines(1)
        details = summary(rule)
        if rule.file_label:
            details = f"{rule.file_label} · {details}" if details else rule.file_label
        row.set_subtitle(details)
        row.set_tooltip_text(_("Open in Advanced Configuration"))
        row.add_prefix(Gtk.Image.new_from_icon_name(icon))
        row.set_activatable(True)
        row._rule = rule
        return row

    def _on_row_activated(self, _listbox, row) -> None:
        rule = getattr(row, "_rule", None)
        if rule is not None:
            self.open_rule(rule)

    def open_rule(self, rule) -> None:
        opener = getattr(self._window, "open_advanced_config", None)
        if not callable(opener):
            return
        advanced = opener(line=rule.line, file=rule.file)
        if advanced is None:
            return
        watch_saves(self._window, advanced)


def watch_saves(window, advanced) -> None:
    """Read the rules again after each save of *advanced*, once per window."""
    if advanced is None or getattr(advanced, "_sidebar_rules_watched", False):
        return
    advanced._sidebar_rules_watched = True
    advanced.add_saved_listener(lambda *_a: refresh_rules(window))


def refresh_rules(window) -> None:
    """Read the rules again if the Rules view is the one shown."""
    view = getattr(window, "_sidebar_rules_view", None)
    if view is not None and current_view(window) == RULES:
        view.refresh()


def current_view(window) -> str:
    stack = getattr(window, "_sidebar_view_stack", None)
    return (stack.get_visible_child_name() or CONNECTIONS) if stack else CONNECTIONS


def show_view(window, name: str) -> None:
    """Show the Connections or the Rules view of the sidebar."""
    stack = getattr(window, "_sidebar_view_stack", None)
    if stack is None or name not in (CONNECTIONS, RULES):
        return
    stack.set_visible_child_name(name)
    switcher = getattr(window, "_sidebar_view_switcher", None)
    if switcher is not None and switcher.get_selected() != _VIEWS.index(name):
        switcher.set_selected(_VIEWS.index(name))
    rules = name == RULES
    # The buttons under the list act on connections.
    toolbar = getattr(window, "_sidebar_toolbar_clip", None)
    if toolbar is not None:
        toolbar.set_visible(not rules)
    entry = getattr(window, "search_entry", None)
    if entry is not None:
        entry.set_placeholder_text(_("Search rules") if rules else _("Search connections"))
    if rules:
        window._sidebar_rules_view.refresh()


_VIEWS = (CONNECTIONS, RULES)

#: The dropdown's height until a connection row has been measured.
_SWITCHER_HEIGHT = 46

_SWITCHER_CSS = """
dropdown.sidebar-view-switcher > button {
  background-color: @accent_bg_color;
  color: @accent_fg_color;
  font-weight: bold;
  border-radius: 8px;
  box-shadow: none;
}
dropdown.sidebar-view-switcher > button:hover {
  background-image: image(alpha(currentColor, 0.1));
}
dropdown.sidebar-view-switcher > button:active,
dropdown.sidebar-view-switcher > button:checked {
  background-image: image(alpha(black, 0.15));
}
"""


def _install_switcher_css() -> None:
    from gi.repository import Gdk

    display = Gdk.Display.get_default()
    if display is None or getattr(display, "_sidebar_switcher_css", False):
        return
    provider = Gtk.CssProvider()
    try:
        provider.load_from_string(_SWITCHER_CSS)
    except (AttributeError, TypeError):         # GTK before 4.12
        provider.load_from_data(_SWITCHER_CSS.encode("utf-8"))
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    display._sidebar_switcher_css = True


def sync_switcher_height(window) -> None:
    """Make the dropdown as tall as a connection row.

    The rows' height follows the sidebar mode and the font, so it is
    measured from the first connection row there is.
    """
    switcher = getattr(window, "_sidebar_view_switcher", None)
    listbox = getattr(window, "connection_list", None)
    if switcher is None or listbox is None:
        return
    row = listbox.get_first_child()
    while row is not None:
        if getattr(row, "connection", None) is not None and row.get_visible():
            # As drawn, once it has a size; what it asks for before that.
            height = row.get_height()
            if height <= 0:
                try:
                    height = row.measure(Gtk.Orientation.VERTICAL, -1)[1]
                except Exception:
                    return
            if height > 0:
                switcher.set_size_request(-1, height)
            return
        row = row.get_next_sibling()


def build_sidebar_switcher(window, sidebar_box) -> None:
    """The dropdown at the top of the sidebar that picks what it lists."""
    _install_switcher_css()
    dropdown = Gtk.DropDown.new_from_strings([_("Connections"), _("Rules")])
    # Filled with the accent colour, as a suggested action is.
    dropdown.add_css_class("sidebar-view-switcher")
    dropdown.set_size_request(-1, _SWITCHER_HEIGHT)
    dropdown.set_hexpand(True)
    dropdown.set_margin_start(6)
    dropdown.set_margin_end(6)
    dropdown.set_margin_bottom(6)
    dropdown.set_tooltip_text(_("What the sidebar lists"))
    dropdown.connect(
        "notify::selected",
        lambda d, _p: show_view(window, _VIEWS[min(d.get_selected(), len(_VIEWS) - 1)]),
    )
    window._sidebar_view_switcher = dropdown
    sidebar_box.append(dropdown)


def build_sidebar_view_stack(window, sidebar_box) -> None:
    """Put the connection list and the Rules view where the list was."""
    stack = Gtk.Stack()
    stack.set_vexpand(True)
    stack.set_hexpand(True)
    stack.add_named(window.connection_scrolled, CONNECTIONS)
    window._sidebar_rules_view = SidebarRulesView(window)
    stack.add_named(window._sidebar_rules_view, RULES)
    window._sidebar_view_stack = stack
    sidebar_box.append(stack)
    # Once the first rows are on screen; later rebuilds keep it in step.
    GLib.timeout_add(1500, lambda: (sync_switcher_height(window), False)[1])
    entry = getattr(window, "search_entry", None)
    if entry is not None:
        entry.connect(
            "search-changed",
            lambda e: window._sidebar_rules_view.set_filter(e.get_text()),
        )
    if window.lookup_action("new-host-rule") is None:
        action = Gio.SimpleAction.new("new-host-rule", None)
        action.connect("activate", lambda *_a: new_host_rule(window))
        window.add_action(action)


def new_host_rule(window) -> None:
    """Begin a new, unsaved Host block in Advanced Configuration."""
    opener = getattr(window, "open_advanced_config", None)
    if not callable(opener):
        return
    advanced = opener(new_block=("", ()))
    watch_saves(window, advanced)

"""In-app commands for the search popup.

The search popup (``search_popup.py``) surfaces in-app utilities and settings —
SSH Config Editor, Known Hosts, Preferences, Sessions, Docker Console, and so
on — alongside connection results. Rather than maintain a second catalogue,
commands are harvested from the one that already exists: the primary menu
(``MainWindow.create_menu()``). Every labelled ``app.``/``win.`` action becomes a
command, including plugin pages (they fold themselves into that menu's Tools
section — see ``plugins/host.py``), so new utilities appear here for free.

A match becomes a :class:`CommandRow` appended to the results list; activating it
dismisses the popup and runs the action via ``Gtk.Widget.activate_action``.
"""

from __future__ import annotations

import gettext
from typing import List, Tuple

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib

_ = gettext.gettext


class CommandRow(Gtk.ListBoxRow):
    """A search result that runs an in-app command instead of a connection."""

    def __init__(self, title: str, subtitle: str, icon_name: str, activate):
        super().__init__()
        # on_connection_activated dispatches on this attribute (window.py).
        self.command_activate = activate
        self.add_css_class("command-row")

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(12)
        box.set_margin_end(12)

        box.append(Gtk.Image.new_from_icon_name(
            icon_name or "application-x-executable-symbolic"))

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        label = Gtk.Label(label=title, xalign=0)
        label.add_css_class("body")
        text.append(label)
        if subtitle:
            sub = Gtk.Label(label=subtitle, xalign=0)
            sub.add_css_class("caption")
            sub.add_css_class("dim-label")
            text.append(sub)
        box.append(text)

        self.set_child(box)


def _menu_string(model, index, attr):
    value = model.get_item_attribute_value(index, attr, None)
    return value.get_string() if value is not None else None


def _walk_menu(model, out: List[Tuple[str, str, object]]) -> None:
    """Flatten a Gio.MenuModel into (label, detailed_action, target) triples,
    descending through sections and submenus."""
    for i in range(model.get_n_items()):
        label = _menu_string(model, i, "label")
        action = _menu_string(model, i, "action")
        if label and action:
            target = model.get_item_attribute_value(i, "target", None)
            out.append((label, action, target))
        links = model.iterate_item_links(i)
        while links.next():
            _walk_menu(links.get_value(), out)


def _matches(title: str, query: str) -> bool:
    """Every whitespace-separated keyword must be a substring of the title."""
    title = title.lower()
    return all(word in title for word in query.lower().split())


def _make_activator(window, action: str, target):
    def run():
        # Close the popup first (restores the sidebar), then run the command so
        # the dialog/page it opens isn't fighting the popup for the overlay.
        window._dismiss_search_popup()
        Gtk.Widget.activate_action(window, action, target)
    return run


def append_command_rows(window, query: str) -> int:
    """Append CommandRows matching ``query`` to ``window.connection_list``.

    Returns the number appended. Harvests fresh each call so pages that appear
    after a plugin is enabled show up without a restart.
    """
    if not query.strip():
        return 0

    pairs: List[Tuple[str, str, object]] = []
    try:
        _walk_menu(window.create_menu(), pairs)
    except Exception:
        return 0

    seen = set()
    count = 0
    for label, action, target in pairs:
        if action in seen:
            continue
        seen.add(action)
        title = label.replace("_", "")  # strip any mnemonic marker
        if not _matches(title, query):
            continue
        row = CommandRow(
            title,
            _("Command"),
            "application-x-executable-symbolic",
            _make_activator(window, action, target),
        )
        window.connection_list.append(row)
        count += 1
    return count

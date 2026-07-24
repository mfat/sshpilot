"""In-app commands for the search popup.

The search popup (``search_popup.py``) surfaces in-app utilities and settings —
SSH Config Editor, Known Hosts, Preferences, Sessions, Docker Console, and so
on — alongside connection results. Rather than maintain a second catalogue,
commands are harvested from the one that already exists: the primary menu
(``MainWindow.create_menu()``). Every labelled ``app.``/``win.`` action becomes a
command, including plugin pages (they fold themselves into that menu's Tools
section — see ``plugins/host.py``), so new utilities appear here for free.

A match becomes a :class:`CommandRow` appended to the results list; the row
carries only the action name + target (plain data, never a ``window`` reference),
so it forms no cycle that would wedge finalization. ``MainWindow`` runs the
action from ``on_connection_activated`` via ``Gtk.Widget.activate_action``.
"""

from __future__ import annotations

import gettext

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from .omni_search import collect_commands

_ = gettext.gettext


class CommandRow(Gtk.ListBoxRow):
    """A search result that runs an in-app command instead of a connection."""

    def __init__(self, title: str, subtitle: str, icon_name: str,
                 action: str, target):
        super().__init__()
        # on_connection_activated dispatches on these. Plain data only — no
        # window/closure reference, so the row forms no finalization cycle.
        self.command_action = action
        self.command_target = target
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


def _matches(title: str, query: str) -> bool:
    """Every whitespace-separated keyword must be a substring of the title."""
    title = title.lower()
    return all(word in title for word in query.lower().split())


def append_command_rows(window, query: str) -> int:
    """Append CommandRows matching ``query`` to ``window.connection_list``.

    Returns the number appended. Harvests fresh each call so pages that appear
    after a plugin is enabled show up without a restart.
    """
    if not query.strip():
        return 0

    count = 0
    for command in collect_commands(window):
        if not _matches(command.title, query):
            continue
        row = CommandRow(
            command.title,
            _("Command"),
            command.icon_name,
            command.action,
            command.target,
        )
        window.connection_list.append(row)
        count += 1
    return count

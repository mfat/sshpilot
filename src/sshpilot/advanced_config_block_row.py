"""A block of the SSH configuration as a row of a list.

Laid out like a connection row of the main sidebar: an icon, a name, and a
dimmed caption under it that says what the block is: a host, a host pattern,
a Match rule, or options for all hosts.
"""
from __future__ import annotations

from gettext import gettext as _

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Pango  # noqa: E402

from .gtk import advanced_config_messages as messages  # noqa: E402
from .ssh_config_blocks import (  # noqa: E402
    ALL_HOSTS,
    CONNECTION,
    MATCH_RULE,
    PATTERN,
    block_category,
)

ICONS = {
    CONNECTION: "computer-symbolic",
    # Several machines: a pattern is options for all it matches.
    PATTERN: "network-workgroup-symbolic",
    MATCH_RULE: "system-search-symbolic",
    ALL_HOSTS: "preferences-system-symbolic",
}

# The main sidebar's full-mode row: margins above and below, icon to text.
_MARGIN = 6
_SPACING = 12


class BlockRow(Gtk.ListBoxRow):
    """One block. ``update`` relabels it in place."""

    def __init__(self) -> None:
        super().__init__()
        self._shown = None
        #: The block as its own first line ("Host web1"); "" for a row
        #: that is not a block.
        self._text = ""
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=_SPACING)
        content.set_margin_top(_MARGIN)
        content.set_margin_bottom(_MARGIN)
        # The grip of a block that can be dragged to another place (every
        # block but the global options, which stay first).
        self.handle = Gtk.Image.new_from_icon_name("list-drag-handle-symbolic")
        self.handle.add_css_class("advcfg-drag-handle")
        self.handle.set_valign(Gtk.Align.CENTER)
        self.handle.set_tooltip_text(_("Drag to reorder"))
        self.handle.set_cursor_from_name("grab")
        self.handle.set_opacity(0)
        content.append(self.handle)
        content.set_spacing(8)
        self.icon = Gtk.Image()
        self.icon.set_icon_size(Gtk.IconSize.NORMAL)
        self.icon.set_valign(Gtk.Align.CENTER)
        content.append(self.icon)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.set_hexpand(True)
        text.set_valign(Gtk.Align.CENTER)
        self.title = Gtk.Label(xalign=0)
        self.title.set_ellipsize(Pango.EllipsizeMode.END)
        text.append(self.title)
        self.caption = Gtk.Label(xalign=0)
        self.caption.add_css_class("dim-label")
        self.caption.add_css_class("caption")
        self.caption.set_ellipsize(Pango.EllipsizeMode.END)
        text.append(self.caption)
        content.append(text)
        self.set_child(content)

    def update(self, kind: str, patterns: str, disabled: bool = False,
               detail: str = "") -> None:
        """Show a block. *detail* (``user@hostname``, the block's own) is
        the caption when given; else the caption says what the block is."""
        shown = (kind, patterns, bool(disabled), detail)
        if shown == self._shown:
            return
        self._shown = shown
        category = block_category(kind, patterns)
        self.category = category
        title = messages.block_list_title(kind, patterns)
        caption = detail or messages.block_category_label(
            category, len(patterns.split()) or 1, kind
        )
        if disabled:
            # Translators: a block that is switched off, e.g. "Host pattern · off".
            caption = _("{kind} · off").format(kind=caption)
        self.title.set_label(title)
        self.caption.set_label(caption)
        self.icon.set_from_icon_name(ICONS[category])
        self._text = messages.block_title(kind, patterns)
        self.set_tooltip_text(self._text)
        if disabled:
            self.title.add_css_class("dim-label")
        else:
            self.title.remove_css_class("dim-label")

    def set_movable(self, movable: bool) -> None:
        """Show the grip, or keep its room empty so the icons line up."""
        self.movable = movable
        self.handle.set_opacity(1 if movable else 0)
        self.handle.set_tooltip_text(_("Drag to reorder") if movable else None)

    def set_plain(self, title: str, icon_name: str) -> None:
        """A row that is not a block ("All Blocks"): a name on one line."""
        self.title.set_label(title)
        self.caption.set_visible(False)
        self.icon.set_from_icon_name(icon_name)

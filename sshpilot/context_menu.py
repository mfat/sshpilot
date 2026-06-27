"""Shared Gtk.PopoverMenu builder for sidebar-style context menus."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Gio, GLib, Gdk, Pango


class IconContextMenu:
    """Build and show a Gtk.PopoverMenu with flat icon+label buttons."""

    def __init__(self) -> None:
        self._menu = Gio.Menu()
        self._custom_widgets: list[tuple[str, Gtk.Button]] = []
        self._counter = 0
        self._popover: Gtk.PopoverMenu | None = None
        self._popover_ref: list[Gtk.PopoverMenu | None] = [None]

    def add_item(
        self,
        icon_name: str,
        label_text: str,
        callback: Callable[[], None] | None,
    ) -> Gio.MenuItem | None:
        if callback is None:
            return None

        wid = f'ctx-{self._counter}'
        self._counter += 1

        item = Gio.MenuItem.new(None, None)
        item.set_attribute_value('custom', GLib.Variant('s', wid))

        btn = Gtk.Button()
        btn.add_css_class('flat')
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.append(Gtk.Image.new_from_icon_name(icon_name))
        lbl = Gtk.Label(label=label_text)
        lbl.set_xalign(0)
        lbl.set_hexpand(True)
        attrs = Pango.AttrList.new()
        attrs.insert(Pango.attr_weight_new(Pango.Weight.NORMAL))
        lbl.set_attributes(attrs)
        box.append(lbl)
        btn.set_child(box)
        btn.connect(
            'clicked',
            lambda _b, cb=callback: (cb(), self._popover_ref[0] and self._popover_ref[0].popdown()),
        )

        self._custom_widgets.append((wid, btn))
        return item

    def add_section(self, *items: Gio.MenuItem | None) -> None:
        section = Gio.Menu()
        for item in items:
            if item is not None:
                section.append_item(item)
        if section.get_n_items():
            self._menu.append_section(None, section)

    def show(
        self,
        parent: Gtk.Widget,
        on_closed: Callable[..., None] | None = None,
    ) -> Gtk.PopoverMenu:
        pop = Gtk.PopoverMenu.new_from_model(self._menu)
        self._popover = pop
        self._popover_ref[0] = pop
        for wid, btn in self._custom_widgets:
            pop.add_child(btn, wid)
        if on_closed:
            pop.connect('closed', on_closed)
        pop.set_parent(parent)
        GLib.idle_add(lambda: (pop.popup(), False)[-1])
        return pop


# --- GNOME Quick-Settings-style tile menu ----------------------------------

_TILE_CSS = b"""
.quick-tile {
    border-radius: 14px;
    padding: 10px 12px;
    min-width: 132px;
    min-height: 44px;
    background-color: alpha(currentColor, 0.07);
}
.quick-tile:hover {
    background-color: alpha(currentColor, 0.12);
}
.quick-tile:active {
    background-color: alpha(currentColor, 0.16);
}
.quick-tile.destructive {
    color: @error_color;
}
"""

_tile_css_loaded = False


def _ensure_tile_css() -> None:
    """Load the .quick-tile CSS once onto the default display."""
    global _tile_css_loaded
    if _tile_css_loaded:
        return
    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(_TILE_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _tile_css_loaded = True


@dataclass
class _Tile:
    icon_name: str
    label: str
    callback: Callable[[], None]
    destructive: bool = False


class IconTileMenu:
    """Build and show a Gtk.Popover laid out as a grid of quick-settings tiles.

    Exposes the same ``add_item`` / ``add_section`` / ``show`` API as
    :class:`IconContextMenu` so it is a drop-in replacement, but renders items
    as a 2-column grid of rounded tile buttons (GNOME Quick Settings look).
    """

    def __init__(self) -> None:
        self._sections: list[list[_Tile]] = []
        self._popover: Gtk.Popover | None = None
        self._popover_ref: list[Gtk.Popover | None] = [None]

    def add_item(
        self,
        icon_name: str,
        label_text: str,
        callback: Callable[[], None] | None,
    ) -> _Tile | None:
        if callback is None:
            return None
        destructive = icon_name == 'user-trash-symbolic'
        return _Tile(icon_name, label_text, callback, destructive)

    def add_section(self, *items: _Tile | None) -> None:
        tiles = [it for it in items if it is not None]
        if tiles:
            self._sections.append(tiles)

    def _build_tile(self, tile: _Tile) -> Gtk.Button:
        btn = Gtk.Button()
        btn.add_css_class('quick-tile')
        if tile.destructive:
            btn.add_css_class('destructive')
        btn.set_hexpand(True)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.append(Gtk.Image.new_from_icon_name(tile.icon_name))
        lbl = Gtk.Label(label=tile.label)
        lbl.set_xalign(0)
        lbl.set_wrap(True)
        lbl.set_hexpand(True)
        box.append(lbl)
        btn.set_child(box)
        btn.connect(
            'clicked',
            lambda _b, cb=tile.callback: (
                cb(),
                self._popover_ref[0] and self._popover_ref[0].popdown(),
            ),
        )
        return btn

    def show(
        self,
        parent: Gtk.Widget,
        on_closed: Callable[..., None] | None = None,
    ) -> Gtk.Popover:
        _ensure_tile_css()
        pop = Gtk.Popover()
        # Open to the right of the row, into the window content (the sidebar is
        # on the left). GTK flips automatically if there is no room that side.
        pop.set_position(Gtk.PositionType.RIGHT)
        self._popover = pop
        self._popover_ref[0] = pop

        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        container.set_margin_top(12)
        container.set_margin_bottom(12)
        container.set_margin_start(12)
        container.set_margin_end(12)

        for tiles in self._sections:
            grid = Gtk.Grid(column_spacing=6, row_spacing=6, column_homogeneous=True)
            lone = len(tiles) == 1
            for i, tile in enumerate(tiles):
                btn = self._build_tile(tile)
                col = i % 2
                row = i // 2
                # A section with a single tile spans both columns so it reads
                # as an intentional full-width row (like GNOME's Dark Style row).
                width = 2 if lone else 1
                grid.attach(btn, col, row, width, 1)
            container.append(grid)

        pop.set_child(container)
        if on_closed:
            pop.connect('closed', on_closed)
        pop.set_parent(parent)
        GLib.idle_add(lambda: (pop.popup(), False)[-1])
        return pop

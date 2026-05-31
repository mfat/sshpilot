"""Shared Gtk.PopoverMenu builder for sidebar-style context menus."""

from __future__ import annotations

from typing import Callable

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Gio, GLib, Pango


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

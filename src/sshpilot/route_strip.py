"""The route a connection takes, as a picture: this computer → each jump
host → the destination.

Shared by Advanced Configuration (the route OpenSSH resolves for the host
being checked) and the connection editor's Routing page (the route its
fields describe). Each stop is a name over its address; every stop is as
tall as the tallest. A jump host can be a button (*on_hop*), when the
window showing the picture has somewhere to take it; a route through a
ProxyCommand is one stop of its own; a stop the connection inherits rather
than sets is dimmed, with where it comes from in its tooltip.

Where the window can rewrite the jump hosts (*on_reorder*), their cards can
be dragged into another order. A drag carries a :class:`_HopDrag` of this
strip, never text, so nothing dropped from elsewhere is taken for a hop.
"""
from __future__ import annotations

from dataclasses import dataclass
from gettext import gettext as _
from typing import Callable, List, Optional, Sequence

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, GObject, Gtk, Pango  # noqa: E402

#: Kinds of stop.
JUMP = "jump"
COMMAND = "command"
TARGET = "target"


@dataclass(frozen=True)
class Stop:
    name: str
    endpoint: str = ""
    kind: str = JUMP
    #: Set by another block (or OpenSSH's default), not by this connection.
    inherited: bool = False
    #: Where an inherited stop comes from, for its tooltip.
    source: str = ""


_CSS = """
.route-stop { padding: 4px 10px; border-radius: 8px; background-color: alpha(currentColor, 0.07); }
.route-stop.route-jump { background-color: alpha(@accent_color, 0.12); }
button.route-stop.route-jump:hover { background-color: alpha(@accent_color, 0.2); }
.route-stop.route-target { background-color: alpha(@success_color, 0.14); }
.route-stop.route-command { background-color: alpha(@warning_color, 0.14); }
.route-stop.route-inherited { opacity: 0.6; }
.route-stop.route-dragged { opacity: 0.35; }
.route-stop button.route-remove { min-width: 22px; min-height: 22px; padding: 0; margin-right: -4px; }
.route-stop.route-drop-before { box-shadow: inset 3px 0 0 0 @accent_bg_color; }
.route-stop.route-drop-after { box-shadow: inset -3px 0 0 0 @accent_bg_color; }
"""
_css_installed = False


def _ensure_css() -> None:
    global _css_installed
    display = Gdk.Display.get_default()
    if _css_installed or display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(_CSS.encode("utf-8"))
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _css_installed = True


class _HopDrag(GObject.Object):
    """What a dragged jump host card carries: its strip and its place."""

    def __init__(self, strip: "RouteStrip", index: int) -> None:
        super().__init__()
        self.strip = strip
        self.index = index


class _FlowLayout(Gtk.LayoutManager):
    """Children left to right at their natural width, wrapping onto a new
    line like words. An arrow is glued to the stop after it, so a line
    never ends on an arrow. (Gtk.FlowBox lines its children up in
    columns; Adw.WrapBox needs libadwaita 1.7.)"""

    HSPACING = 6
    VSPACING = 6

    def do_get_request_mode(self, _widget):
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    @staticmethod
    def _units(widget) -> List[List[Gtk.Widget]]:
        units: List[List[Gtk.Widget]] = []
        glued: List[Gtk.Widget] = []
        child = widget.get_first_child()
        while child is not None:
            if child.should_layout():
                if getattr(child, "_route_glue", False):
                    glued.append(child)
                else:
                    units.append(glued + [child])
                    glued = []
            child = child.get_next_sibling()
        if glued:
            units.append(glued)
        return units

    def _lines(self, widget, width: int):
        """``[(height, [(child, child width), ...]), ...]``."""
        lines, line, used = [], [], 0
        for unit in self._units(widget):
            sizes = [(child, child.measure(Gtk.Orientation.HORIZONTAL, -1)[1]) for child in unit]
            unit_width = sum(size for _child, size in sizes) + self.HSPACING * (len(sizes) - 1)
            if line and used + self.HSPACING + unit_width > width:
                lines.append(line)
                line, used = [], 0
            used += (self.HSPACING if line else 0) + unit_width
            line.extend(sizes)
        if line:
            lines.append(line)
        return [
            (max(child.measure(Gtk.Orientation.VERTICAL, size)[1] for child, size in line), line)
            for line in lines
        ]

    def do_measure(self, widget, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            units = self._units(widget)
            widths = [
                sum(child.measure(orientation, -1)[1] for child in unit)
                + self.HSPACING * (len(unit) - 1)
                for unit in units
            ]
            minimum = max(widths, default=0)
            natural = sum(widths) + self.HSPACING * max(0, len(widths) - 1)
            return minimum, natural, -1, -1
        width = for_size if for_size >= 0 else self.do_measure(
            widget, Gtk.Orientation.HORIZONTAL, -1)[1]
        lines = self._lines(widget, width)
        height = sum(h for h, _line in lines) + self.VSPACING * max(0, len(lines) - 1)
        return height, height, -1, -1

    def do_allocate(self, widget, width, height, _baseline):
        rtl = widget.get_direction() == Gtk.TextDirection.RTL
        y = 0
        for line_height, line in self._lines(widget, width):
            x = 0
            for child, size in line:
                size = min(size, width)
                left = width - x - size if rtl else x
                child.size_allocate(_rect(left, y, size, line_height), -1)
                x += size + self.HSPACING
            y += line_height + self.VSPACING


def _rect(x: int, y: int, width: int, height: int) -> Gdk.Rectangle:
    rect = Gdk.Rectangle()
    rect.x, rect.y, rect.width, rect.height = int(x), int(y), int(width), int(height)
    return rect


class RouteStrip(Gtk.Widget):
    """The stops of a route, left to right, wrapping when narrow."""

    __gtype_name__ = "SshpilotRouteStrip"

    def __init__(
        self,
        on_hop: Optional[Callable[[str], None]] = None,
        on_reorder: Optional[Callable[[int, int], None]] = None,
        on_remove: Optional[Callable[[int], None]] = None,
    ) -> None:
        """*on_hop(name)*: a jump host card was clicked. *on_reorder(index,
        position)*: the jump host at *index* was dropped at *position* (0:
        first; the count of jump hosts: last), counted before it moves.
        *on_remove(index)*: the jump host at *index* is to go (its card
        then has a remove button)."""
        super().__init__()
        _ensure_css()
        self.set_layout_manager(_FlowLayout())
        self.on_hop = on_hop
        self.on_reorder = on_reorder
        self.on_remove = on_remove
        self._shown: Optional[tuple] = None
        self._heights = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.VERTICAL)

    def show_route(self, stops: Sequence[Stop], note: str = "") -> None:
        """*stops* from the first jump host to the destination; none: *note*
        is said instead."""
        shown = (tuple(stops), note)
        if shown == self._shown:
            return
        self._shown = shown
        self.remove_all()
        # Every stop as tall as the tallest, and no taller: a size group,
        # not filling the line (the line grows when the window does).
        self._heights = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.VERTICAL)
        if not stops:
            label = Gtk.Label(label=note, xalign=0)
            label.add_css_class("dim-label")
            self.append(label)
            return
        local = Stop(_("This computer"), GLib.get_host_name(), kind="local")
        movable = self.on_reorder is not None and sum(s.kind == JUMP for s in stops) > 1
        hop = 0
        for index, stop in enumerate([local, *stops]):
            if index:
                arrow = Gtk.Image.new_from_icon_name("go-next-symbolic")
                arrow.add_css_class("dim-label")
                arrow.set_valign(Gtk.Align.CENTER)
                arrow._route_glue = True        # stays with the stop after it
                self.append(arrow)
            widget = self._stop(stop, hop if stop.kind == JUMP else None)
            if stop.kind == JUMP:
                if movable:
                    self._make_movable(widget, hop)
                hop += 1
            self.append(widget)

    # -- children ------------------------------------------------------------------

    def append(self, child: Gtk.Widget) -> None:
        child.set_parent(self)

    def remove_all(self) -> None:
        child = self.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            child.unparent()
            child = following

    def do_dispose(self) -> None:
        self.remove_all()
        Gtk.Widget.do_dispose(self)

    # -- reordering the jump hosts ---------------------------------------------

    def _make_movable(self, widget: Gtk.Widget, index: int) -> None:
        widget.set_tooltip_text(_("Drag to change the order of the jump hosts"))
        widget.set_cursor(Gdk.Cursor.new_from_name("grab", None))
        source = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        source.connect("prepare", lambda *_a: Gdk.ContentProvider.new_for_value(
            _HopDrag(self, index)
        ))
        source.connect("drag-begin", lambda src, _drag: (
            src.set_icon(Gtk.WidgetPaintable.new(widget), 0, 0),
            widget.add_css_class("route-dragged"),
        ))
        source.connect("drag-end", lambda *_a: widget.remove_css_class("route-dragged"))
        widget.add_controller(source)
        target = Gtk.DropTarget.new(_HopDrag, Gdk.DragAction.MOVE)
        target.connect("motion", lambda _t, x, _y: self._mark(widget, x))
        target.connect("leave", lambda *_a: self._mark(widget, None))
        target.connect("drop", lambda _t, value, x, _y: self._drop(widget, index, value, x))
        widget.add_controller(target)

    @staticmethod
    def _after(widget: Gtk.Widget, x: float) -> bool:
        after = x > widget.get_width() / 2
        if widget.get_direction() == Gtk.TextDirection.RTL:
            after = not after
        return after

    def _mark(self, widget: Gtk.Widget, x: Optional[float]):
        after = x is not None and self._after(widget, x)
        for name, on in (("route-drop-before", x is not None and not after),
                         ("route-drop-after", after)):
            if on:
                widget.add_css_class(name)
            else:
                widget.remove_css_class(name)
        return Gdk.DragAction.MOVE

    def _drop(self, widget: Gtk.Widget, index: int, value, x: float) -> bool:
        self._mark(widget, None)
        if not isinstance(value, _HopDrag) or value.strip is not self:
            return False
        position = index + (1 if self._after(widget, x) else 0)
        if position in (value.index, value.index + 1):
            return False                    # dropped where it already is
        # After the drop the strip is drawn anew: call back once GTK is done
        # with the drag, not from inside it.
        GLib.idle_add(lambda: (self.on_reorder(value.index, position), False)[1])
        return True

    def stops(self) -> tuple:
        """The stops on screen (for tests)."""
        return self._shown[0] if self._shown else ()

    def _stop(self, stop: Stop, hop: Optional[int] = None) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.set_valign(Gtk.Align.CENTER)
        title = Gtk.Label(label=stop.name, xalign=0)
        if stop.kind != "local":
            title.add_css_class("monospace")
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_max_width_chars(24)
        box.append(title)
        if stop.endpoint:
            where = Gtk.Label(label=stop.endpoint, xalign=0)
            for name in ("caption", "dim-label", "monospace"):
                where.add_css_class(name)
            where.set_ellipsize(Pango.EllipsizeMode.END)
            where.set_max_width_chars(32)
            box.append(where)
        if stop.kind == JUMP and self.on_hop is not None:
            # A jump host: its own block is a click away.
            widget = Gtk.Button(child=box, tooltip_text=_("Show this host's block"))
            widget.connect("clicked", lambda *_a, n=stop.name: self.on_hop(n))
        else:
            widget = Gtk.CenterBox(orientation=Gtk.Orientation.VERTICAL)
            if hop is not None and self.on_remove is not None:
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                row.append(box)
                remove = Gtk.Button(icon_name="window-close-symbolic",
                                    tooltip_text=_("Remove this jump host"))
                remove.update_property([Gtk.AccessibleProperty.LABEL],
                                       [_("Remove jump host {name}").format(name=stop.name)])
                for name in ("flat", "circular", "route-remove"):
                    remove.add_css_class(name)
                remove.set_valign(Gtk.Align.CENTER)
                remove.connect("clicked", lambda *_a, i=hop: GLib.idle_add(
                    lambda: (self.on_remove(i), False)[1]
                ))
                row.append(remove)
                box = row
            widget.set_center_widget(box)
            if stop.kind == COMMAND:
                widget.set_tooltip_text(stop.endpoint or None)
        widget.set_valign(Gtk.Align.CENTER)
        widget.add_css_class("route-stop")
        if stop.kind in (JUMP, TARGET, COMMAND):
            widget.add_css_class(f"route-{stop.kind}")
        if stop.inherited:
            widget.add_css_class("route-inherited")
            if stop.source:
                widget.set_tooltip_text(stop.source)
        self._heights.add_widget(widget)
        return widget

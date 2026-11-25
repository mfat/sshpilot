"""The route a connection takes, as a picture: this computer → each jump
host → the destination.

Shared by Advanced Configuration (the route OpenSSH resolves for the host
being checked) and the connection editor's Routing page (the route its
fields describe). Each stop is a name over its address; every stop is as
tall as the tallest. A jump host can be a button (*on_hop*), when the
window showing the picture has somewhere to take it; a route through a
ProxyCommand is one stop of its own; a stop the connection inherits rather
than sets is dimmed, with where it comes from in its tooltip.
"""
from __future__ import annotations

from dataclasses import dataclass
from gettext import gettext as _
from typing import Callable, Optional, Sequence

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

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


class RouteStrip(Gtk.FlowBox):
    """The stops of a route, left to right, wrapping when narrow."""

    def __init__(self, on_hop: Optional[Callable[[str], None]] = None) -> None:
        super().__init__(selection_mode=Gtk.SelectionMode.NONE)
        _ensure_css()
        self.on_hop = on_hop
        self.set_max_children_per_line(24)
        self.set_column_spacing(4)
        self.set_row_spacing(6)
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
        for index, stop in enumerate([local, *stops]):
            if index:
                arrow = Gtk.Image.new_from_icon_name("go-next-symbolic")
                arrow.add_css_class("dim-label")
                arrow.set_valign(Gtk.Align.CENTER)
                self.append(arrow)
            self.append(self._stop(stop))

    def stops(self) -> tuple:
        """The stops on screen (for tests)."""
        return self._shown[0] if self._shown else ()

    def _stop(self, stop: Stop) -> Gtk.Widget:
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

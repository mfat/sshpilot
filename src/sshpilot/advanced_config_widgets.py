"""Small widgets shared by the parts of Advanced Configuration: the status
pill, and what a drag in the sidebar carries."""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GObject, Gtk  # noqa: E402


class DragItem(GObject.Object):
    """A block being dragged in the sidebar.

    A type of its own, not text: a text payload dropped on a field would be
    taken by the field and typed into it.
    """

    def __init__(self, uid: int) -> None:
        super().__init__()
        self.uid = uid


class LineDragItem(GObject.Object):
    """A line of the block on screen being dragged to another place in it.
    A type of its own, so it is never taken for a dragged block."""

    def __init__(self, uid: int) -> None:
        super().__init__()
        self.uid = uid


def pill(text: str = "", style: str = "muted") -> Gtk.Label:
    """A small rounded label: a state, a source, a count."""
    label = Gtk.Label(label=text)
    label.add_css_class("advcfg-pill")
    label.add_css_class(style)
    label._style = style
    label._tooltip = ""
    label.set_valign(Gtk.Align.CENTER)
    label.set_visible(bool(text))
    # Tooltips are answered when asked for: setting one costs about a
    # millisecond, and pills are relabelled on every answer.
    label.set_has_tooltip(True)
    label.connect("query-tooltip", _pill_tooltip)
    return label


def _pill_tooltip(label, _x, _y, _keyboard, tooltip) -> bool:
    if not label._tooltip:
        return False
    tooltip.set_text(label._tooltip)
    return True


def set_pill(label: Gtk.Label, text: str, style: str, tooltip: str = "") -> None:
    if label.get_label() != text:
        label.set_label(text)
    if label.get_visible() != bool(text):
        label.set_visible(bool(text))
    if label._style != style:
        label.remove_css_class(label._style)
        label.add_css_class(style)
        label._style = style
    label._tooltip = tooltip


def set_class(widget: Gtk.Widget, name: str, on: bool) -> None:
    if on:
        widget.add_css_class(name)
    else:
        widget.remove_css_class(name)

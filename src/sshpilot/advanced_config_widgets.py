"""Small widgets shared by the parts of Advanced Configuration: the status
pill, and what a drag in the sidebar carries."""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GObject, Gtk  # noqa: E402


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


def lift_row(source: Gtk.DragSource, drag: Gdk.Drag, row: Gtk.Widget,
             grab_x: float, grab_y: float) -> None:
    """The drag image of a list row, as GNOME's own reorderable lists make
    it (Settings' search and input-source rows, Nautilus's sidebar): a solid
    copy of the row at its size, in a list of its own, raised by a shadow
    (no drag-highlight outline: the user found the blue edge noisy), held
    at the point where it was grabbed (not by a corner).

    The copy is a picture of the row as it is now, so the row itself can be
    hidden while it is carried. On macOS it is set with DragSource.set_icon:
    Gtk.DragIcon has tripped AppKit there (see dnd_payload)."""
    from .platform_utils import is_macos

    width, height = row.get_width(), row.get_height()
    try:
        image = Gtk.WidgetPaintable.new(row).get_current_image()
    except Exception:
        return
    if is_macos():
        source.set_icon(image, int(grab_x), int(grab_y))
        return
    picture = Gtk.Picture.new_for_paintable(image)
    picture.set_can_shrink(False)
    lifted = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
    lifted.add_css_class("advcfg-lifted-row")
    lifted.set_size_request(width, height)
    item = Gtk.ListBoxRow(child=picture, activatable=False)
    lifted.append(item)
    Gtk.DragIcon.get_for_drag(drag).set_child(lifted)
    drag.set_hotspot(int(grab_x), int(grab_y))


def gap_row(height: int) -> Gtk.ListBoxRow:
    """Where a carried row would land: empty space of its height, as
    Nautilus's sidebar opens it, so the list makes room for the row."""
    gap = Gtk.ListBoxRow(selectable=False, activatable=False, focusable=False)
    gap.add_css_class("advcfg-drop-gap")
    gap.set_size_request(-1, max(height, 1))
    gap.drop_placeholder = True
    return gap


#: The look Advanced Configuration and the connection editor share: the
#: status pills, and a typed value that does not take effect, struck
#: through and tinted (:func:`mark_unused`).
_SHARED_CSS = """
.advcfg-pill { font-size: 0.82em; font-weight: 600; padding: 2px 9px; border-radius: 999px; }
.advcfg-pill.success { background-color: alpha(@success_color, 0.16); color: @success_color; }
.advcfg-pill.warning { background-color: alpha(@warning_color, 0.18); color: @warning_color; }
.advcfg-pill.error { background-color: alpha(@error_color, 0.16); color: @error_color; }
.advcfg-pill.accent { background-color: alpha(@accent_color, 0.15); color: @accent_color; }
.advcfg-pill.purple { background-color: alpha(@purple_3, 0.18); color: @purple_3; }
.advcfg-pill.advcfg-pill-link:hover { text-decoration: underline; }
.advcfg-pill.muted { background-color: alpha(currentColor, 0.08); color: alpha(currentColor, 0.65); }
row.warning button, row.warning menubutton, row.warning image.edit-icon { color: @view_fg_color; }
"""
_shared_css_installed = False


def ensure_shared_css() -> None:
    global _shared_css_installed
    display = Gdk.Display.get_default()
    if _shared_css_installed or display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(_SHARED_CSS.encode("utf-8"))
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _shared_css_installed = True


_STRUCK: list = []


def struck_attributes(struck: bool = True):
    """The text attributes of a value that does not take effect (an empty
    list when *struck* is false: the binding refuses None)."""
    from gi.repository import Pango

    if not struck:
        return Pango.AttrList()
    if not _STRUCK:
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_strikethrough_new(True))
        _STRUCK.append(attrs)
    return _STRUCK[0]


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


# -- a line's mark (ssh_config_answer.LineVerdict), wherever it is shown ------


def verdict_pill(on_follow) -> Gtk.Label:
    """The pill beside a line or field that says what OpenSSH does with it.
    A "No effect" one is a link: a click calls *on_follow* with the line
    used instead, ``(file, line)``."""
    ensure_shared_css()
    label = pill(style="warning")
    label._target = None
    click = Gtk.GestureClick()
    # The click is the pill's: a row it sits in must not take it too (a
    # row whose activatable widget is a checkbox would toggle it).
    click.connect(
        "pressed",
        lambda gesture, *_a: gesture.set_state(Gtk.EventSequenceState.CLAIMED),
    )
    click.connect(
        "released",
        lambda *_a: label._target is not None and on_follow(label._target),
    )
    label.add_controller(click)
    return label


def show_verdict(label: Gtk.Label, verdict) -> None:
    """Show *verdict* (a ``LineVerdict``) on a :func:`verdict_pill`."""
    set_pill(label, verdict.tag, verdict.style, verdict.tooltip)
    target = verdict.target if verdict.tag else None
    if getattr(label, "_target", None) == target:
        return
    label._target = target
    label.set_cursor_from_name("pointer" if target else None)
    set_class(label, "advcfg-pill-link", target is not None)


def mark_unused(widget: Gtk.Widget, unused: bool) -> None:
    """A typed value that does not take effect: Adwaita's warning tint on
    *widget*, and its text struck through. Only typed text is struck; a
    label or a checkbox is only tinted, by the caller's own row.

    GtkText ignores CSS text-decoration, so the text gets a Pango attribute:
    on the entry itself, or on an entry row's inner text (its delegate).
    """
    set_class(widget, "warning", unused)
    delegate = getattr(widget, "get_delegate", None)
    text = delegate() if callable(delegate) else widget
    if text is None or not hasattr(text, "set_attributes"):
        return
    if getattr(text, "_sshpilot_struck", False) != unused:
        text._sshpilot_struck = unused
        text.set_attributes(struck_attributes(unused))

"""Reveal the hidden sidebar as a floating popup when the pointer hits the edge.

While the sidebar is hidden (F9), resting the pointer on the window's leading
edge, or on an owner-defined hot zone (the header bar's sidebar toggle), for a
moment shows the sidebar in the same detachable ``SearchPopup`` that
Ctrl+F uses, so the terminal never resizes. Moving the pointer off the panel
hides it again, unless something has since claimed it (search was opened, or a
popover such as a row's context menu holds focus inside it).

A single motion controller on the content overlay drives both directions. It
runs in CAPTURE so it sees motion over the terminal and the popup alike, and
it never claims events, so nothing underneath loses them.
"""

from __future__ import annotations

from typing import Callable, Optional

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk

# How close to the edge counts as "on" it, and how long the pointer must rest
# there: long enough that sweeping past the edge toward another window doesn't
# pop the sidebar open.
EDGE_PX = 3
REVEAL_DELAY_MS = 150
# Grace before hiding, so a brief overshoot past the panel edge is forgiven.
HIDE_DELAY_MS = 300
# Extra room around the panel that still counts as "on" it.
HIDE_MARGIN_PX = 16

_BUTTONS_MASK = (
    Gdk.ModifierType.BUTTON1_MASK
    | Gdk.ModifierType.BUTTON2_MASK
    | Gdk.ModifierType.BUTTON3_MASK
)


class SidebarEdgeReveal:
    """Show ``popup_func()`` on a leading-edge hover while ``is_hidden()``."""

    def __init__(
        self,
        overlay: Gtk.Widget,
        popup_func: Callable[[], Optional[object]],
        is_hidden: Callable[[], bool],
        keep_open: Callable[[], bool],
        in_hot_zone: Optional[Callable[[float, float], bool]] = None,
    ):
        self._overlay = overlay
        self._popup_func = popup_func
        self._is_hidden = is_hidden
        self._keep_open = keep_open
        self._in_hot_zone = in_hot_zone or (lambda _x, _y: False)
        self._revealed = False
        self._reveal_id = 0
        self._hide_id = 0

        motion = Gtk.EventControllerMotion()
        motion.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        motion.connect("motion", self._on_motion)
        motion.connect("leave", self._on_leave)
        overlay.add_controller(motion)
        self._motion = motion

    @property
    def revealed(self) -> bool:
        """True while the popup is up because of an edge hover."""
        popup = self._popup_func()
        if self._revealed and (popup is None or not popup.visible):
            self._revealed = False
        return self._revealed

    # -- pointer ----------------------------------------------------------

    def _on_motion(self, controller, x, y):
        if self.revealed:
            if self._over_panel(x, y):
                self._cancel_hide()
            else:
                self._schedule_hide()
            return

        popup = self._popup_func()
        if (
            popup is None
            or popup.visible
            or not self._is_hidden()
            or controller.get_current_event_state() & _BUTTONS_MASK
            or not (self._on_edge(x) or self._in_hot_zone(x, y))
        ):
            self._cancel_reveal()
            return
        if not self._reveal_id:
            self._reveal_id = GLib.timeout_add(REVEAL_DELAY_MS, self._reveal)

    def _on_leave(self, _controller):
        # Leaving the overlay entirely: off the window, or onto a popover's
        # own surface (a row menu). Neither should hide or reveal anything.
        self._cancel_reveal()
        self._cancel_hide()

    def _on_edge(self, x: float) -> bool:
        if self._overlay.get_direction() == Gtk.TextDirection.RTL:
            return x >= self._overlay.get_width() - EDGE_PX
        return x <= EDGE_PX

    def _over_panel(self, x: float, y: float) -> bool:
        panel = getattr(self._popup_func(), "panel", None)
        if panel is None:
            return False
        ok, bounds = panel.compute_bounds(self._overlay)
        if not ok:
            return False
        left = bounds.get_x() - HIDE_MARGIN_PX
        right = bounds.get_x() + bounds.get_width() + HIDE_MARGIN_PX
        return left <= x <= right

    # -- show / hide ------------------------------------------------------

    def _reveal(self):
        self._reveal_id = 0
        popup = self._popup_func()
        if popup is not None and not popup.visible and self._is_hidden():
            popup.show()
            self._revealed = True
        return GLib.SOURCE_REMOVE

    def _hide(self):
        self._hide_id = 0
        popup = self._popup_func()
        if (
            self._revealed
            and popup is not None
            and popup.visible
            and not self._keep_open()
            and not self._popover_open(popup)
        ):
            popup.hide()
        if popup is None or not popup.visible:
            self._revealed = False
        return GLib.SOURCE_REMOVE

    def _popover_open(self, popup) -> bool:
        """A popover opened from inside the panel (a row menu) holds focus."""
        panel = getattr(popup, "panel", None)
        root = self._overlay.get_root()
        focus = root.get_focus() if root is not None else None
        if panel is None or focus is None or not focus.is_ancestor(panel):
            return False
        widget = focus
        while widget is not None and widget is not panel:
            if isinstance(widget, Gtk.Popover):
                return True
            widget = widget.get_parent()
        return False

    def _schedule_hide(self):
        if not self._hide_id:
            self._hide_id = GLib.timeout_add(HIDE_DELAY_MS, self._hide)

    def _cancel_hide(self):
        if self._hide_id:
            GLib.source_remove(self._hide_id)
            self._hide_id = 0

    def _cancel_reveal(self):
        if self._reveal_id:
            GLib.source_remove(self._reveal_id)
            self._reveal_id = 0

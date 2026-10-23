"""Keep macOS clicks from being cancelled by a stale pointer motion.

GTK's macOS backend delivers a motion event just before the button release
whenever the pointer moved during the click, even by a pixel, which a trackpad
press nearly always does. That motion carries a modifier state in which the
button is already up. ``GtkGestureSingle`` reads "motion without the pressed
button" as a lost release and resets itself, so every click gesture on the
pick stack is cancelled and the release that follows activates nothing. Seen
from the user's side, buttons only respond to perfectly still clicks.

A capture-phase controller on each toplevel swallows those motions while a
press is in progress, so the gestures never see them. The real release still
arrives and finishes the click. Only a few such motions are dropped per press:
if the release really was lost (the button let go outside the window), GTK
gets the next ones and resets as it normally would.
"""

from __future__ import annotations

import logging

from gi.repository import Gdk, Gtk

from .platform_utils import is_macos

logger = logging.getLogger(__name__)

# The stale motion is a single event in practice; a couple more are tolerated
# before GTK is left to treat the release as lost.
MAX_STALE_MOTIONS_PER_PRESS = 3

_BUTTON_MASKS = {
    1: Gdk.ModifierType.BUTTON1_MASK,
    2: Gdk.ModifierType.BUTTON2_MASK,
    3: Gdk.ModifierType.BUTTON3_MASK,
}


class _ClickGuard:
    """Per-toplevel state: which button is down and how many motions were eaten."""

    def __init__(self) -> None:
        self.button_mask = None
        self.swallowed = 0

    def on_event(self, controller, _event) -> bool:
        # PyGObject hands the legacy controller's event over as None on some
        # versions; the controller's current event is always available.
        event = controller.get_current_event()
        if event is None:
            return False
        event_type = event.get_event_type()

        if event_type == Gdk.EventType.BUTTON_PRESS:
            self.button_mask = _BUTTON_MASKS.get(event.get_button())
            self.swallowed = 0
            return False

        if event_type in (Gdk.EventType.BUTTON_RELEASE, Gdk.EventType.GRAB_BROKEN):
            self.button_mask = None
            return False

        if event_type != Gdk.EventType.MOTION_NOTIFY or self.button_mask is None:
            return False
        if event.get_modifier_state() & self.button_mask:
            return False
        if self.swallowed >= MAX_STALE_MOTIONS_PER_PRESS:
            self.button_mask = None
            return False
        self.swallowed += 1
        return True


def guard_window(window) -> None:
    """Install the click guard on one toplevel, once."""
    if getattr(window, "_sshpilot_click_guard", None) is not None:
        return
    guard = _ClickGuard()
    controller = Gtk.EventControllerLegacy()
    controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    controller.connect("event", guard.on_event)
    window.add_controller(controller)
    window._sshpilot_click_guard = guard


def install_click_guard() -> None:
    """Guard every current and future toplevel. No-op outside macOS."""
    if not is_macos():
        return
    toplevels = Gtk.Window.get_toplevels()

    def _guard_range(model, position, _removed, added):
        for index in range(position, position + added):
            window = model.get_item(index)
            if window is None:
                continue
            try:
                guard_window(window)
            except Exception:
                logger.debug("Could not install the click guard", exc_info=True)

    _guard_range(toplevels, 0, 0, toplevels.get_n_items())
    toplevels.connect("items-changed", _guard_range)
    # The list model only lives as long as someone holds it.
    install_click_guard._toplevels = toplevels

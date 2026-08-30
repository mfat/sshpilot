"""Pause emulator feed while the user is selecting (PTY-less VTE).

VTE disconnects its PTY reader for the duration of a drag-select so child
output cannot rewrite the highlighted cells and call ``deselect_all``.
Daemon-backed tabs have no VTE PTY: the daemon pushes bytes with
``feed()`` instead, so that pause is a no-op and busy TUIs (htop, top)
wipe the selection mid-drag.

This buffer is the Ptyxis-equivalent pause for the feed path. Only arm it
when VTE itself would start a local selection on the primary press
(mouse tracking off, or Shift held while tracking is on), and always end
it on the matching release -- see ``SelectionFeedPauseController`` for why
the pointer has to be observed rather than gestured at.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def selection_press_owns_pointer(
    *,
    mouse_tracking_active: bool,
    shift_held: bool,
) -> bool:
    """True when VTE would begin a local selection on this primary press."""
    if mouse_tracking_active and not shift_held:
        return False
    return True


class DeferredDisplayFeed:
    """Accumulate display bytes while a local selection gesture is active."""

    def __init__(self) -> None:
        self._paused = False
        self._buf = bytearray()

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def buffered_length(self) -> int:
        return len(self._buf)

    def begin(self) -> None:
        self._paused = True

    def accept(self, data: bytes) -> bytes | None:
        """Return *data* to paint now, or ``None`` when it was deferred."""
        if not data:
            return b""
        if self._paused:
            self._buf.extend(data)
            return None
        return data

    def end(self) -> bytes:
        """Leave the pause and return every deferred byte (may be empty)."""
        self._paused = False
        data = bytes(self._buf)
        self._buf.clear()
        return data

    def reset(self) -> None:
        """Drop any deferred bytes without painting them (session teardown)."""
        self._paused = False
        self._buf.clear()


class SelectionFeedPauseController:
    """Drive a :class:`DeferredDisplayFeed` from the pointer's press/release.

    Installed as a ``Gtk.EventControllerLegacy``, deliberately *not* as a
    ``Gtk.GestureClick``: VTE claims the pointer sequence for its own
    drag-select as soon as the button goes down, so a click gesture on the
    same widget only ever sees ``pressed`` followed by ``cancel`` -- the
    ``released`` half never arrives. Pausing the feed on a signal whose
    partner never fires froze the display permanently on the first press.
    A legacy controller is not a gesture: it never competes for the
    sequence, it still sees the release (including one delivered outside
    the window, via the implicit pointer grab), and VTE's mouse reports
    reach the remote unchanged.
    """

    # Belt-and-braces: a drag whose release is never delivered (a foreign
    # pointer grab, a compositor taking over) must not leave the terminal
    # dark forever. Long enough that a deliberate selection is never cut
    # short in practice.
    MAX_PAUSE_MS = 15000

    def __init__(
        self,
        pause: DeferredDisplayFeed,
        *,
        mouse_tracking_active,
        flush,
    ) -> None:
        self._pause = pause
        self._mouse_tracking_active = mouse_tracking_active
        self._flush = flush
        self._widget = None
        self._controller = None
        self._watchdog = None

    def install(self, widget) -> bool:
        """Attach to *widget*; returns whether the observer is now live."""
        from gi.repository import Gtk

        self.uninstall()
        if widget is None or not hasattr(widget, "add_controller"):
            return False
        controller = Gtk.EventControllerLegacy()
        # CAPTURE so the press is seen before VTE decides what to do with
        # it, and so a backend that wraps its terminal in a container still
        # observes events VTE stops from bubbling.
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        controller.connect("event", self._on_event)
        widget.add_controller(controller)
        self._widget = widget
        self._controller = controller
        return True

    def uninstall(self) -> None:
        self._cancel_watchdog()
        controller = self._controller
        widget = self._widget
        self._controller = None
        self._widget = None
        if controller is None or widget is None:
            return
        try:
            widget.remove_controller(controller)
        except Exception:
            pass

    def _on_event(self, controller, event=None) -> bool:
        from gi.repository import Gdk

        if event is None:
            # PyGObject hands the "event" signal a NULL box on some
            # versions; the controller still knows the current event.
            try:
                event = controller.get_current_event()
            except Exception:
                event = None
        if event is None:
            return False
        try:
            event_type = event.get_event_type()
            if event_type == Gdk.EventType.BUTTON_PRESS:
                if event.get_button() == Gdk.BUTTON_PRIMARY:
                    self._begin(
                        shift_held=bool(
                            event.get_modifier_state()
                            & Gdk.ModifierType.SHIFT_MASK
                        )
                    )
            elif event_type == Gdk.EventType.BUTTON_RELEASE:
                if event.get_button() == Gdk.BUTTON_PRIMARY:
                    self.end()
            elif event_type == Gdk.EventType.GRAB_BROKEN:
                self.end()
        except Exception:
            # Never let an observer error swallow a pointer event, and never
            # leave the feed stuck paused because of one.
            self.end()
        return False  # Gdk.EVENT_PROPAGATE: purely an observer.

    def _begin(self, *, shift_held: bool) -> None:
        try:
            mouse_tracking = bool(self._mouse_tracking_active())
        except Exception:
            mouse_tracking = False
        if not selection_press_owns_pointer(
            mouse_tracking_active=mouse_tracking,
            shift_held=shift_held,
        ):
            return
        self._pause.begin()
        logger.debug(
            "Terminal display feed paused for selection "
            "mouse_tracking=%s shift=%s",
            mouse_tracking,
            shift_held,
        )
        self._arm_watchdog()

    def end(self) -> None:
        """Leave the pause and paint whatever was deferred."""
        self._cancel_watchdog()
        self._flush()

    def _arm_watchdog(self) -> None:
        self._cancel_watchdog()
        try:
            from gi.repository import GLib
        except Exception:
            return

        def _expire():
            self._watchdog = None
            if self._pause.paused:
                logger.warning(
                    "Selection feed pause expired after %sms without a "
                    "button release; flushing %s buffered bytes",
                    self.MAX_PAUSE_MS,
                    self._pause.buffered_length,
                )
                self._flush()
            return False

        try:
            self._watchdog = GLib.timeout_add(self.MAX_PAUSE_MS, _expire)
        except Exception:
            self._watchdog = None

    def _cancel_watchdog(self) -> None:
        source = self._watchdog
        self._watchdog = None
        if source is None:
            return
        try:
            from gi.repository import GLib

            GLib.source_remove(source)
        except Exception:
            pass

"""Make a confirmation dialog's default response visible.

GTK only draws the focus ring around a focused button while its window is in
"focus visible" mode, which GTK turns on the first time the user navigates
with the keyboard. A confirmation raised from a mouse click therefore opens
with its default response focused but completely unmarked: nothing on screen
says that Enter confirms (GH #1231). Turning focus-visible on as we present a
confirmation restores the ring. GTK clears the flag again on the next pointer
interaction, so this brightens the dialog the user is looking at rather than
leaving focus rings switched on for good.

Everything here is best effort: a dialog that cannot be marked still works,
it just looks the way it did before.
"""

import logging

from gi.repository import GLib, Gtk

logger = logging.getLogger(__name__)

__all__ = [
    "mark_default_response_visible",
    "capture_toplevels",
    "mark_new_dialog_default_visible",
]


def _resolve_window(widget):
    """Return the Gtk.Window that owns ``widget``, or None.

    ``Adw.Dialog`` is presented *inside* a window, so the flag belongs to the
    window it was presented on; ``Adw.MessageDialog`` and friends are windows
    themselves and ``get_root()`` returns them unchanged.
    """
    candidate = widget
    getter = getattr(candidate, "get_root", None)
    if callable(getter):
        try:
            root = getter()
        except Exception:
            root = None
        if root is not None:
            candidate = root
    if callable(getattr(candidate, "set_focus_visible", None)):
        return candidate
    return None


def mark_default_response_visible(widget) -> bool:
    """Show the focus ring on ``widget``'s window. Call it after present()."""
    if widget is None:
        return False
    window = _resolve_window(widget)
    if window is None:
        return False
    try:
        window.set_focus_visible(True)
    except Exception as exc:
        logger.debug("Could not mark the default response visible: %s", exc)
        return False
    return True


def capture_toplevels():
    """Snapshot the current toplevels, to diff against after choose()."""
    try:
        return set(Gtk.Window.get_toplevels())
    except Exception as exc:
        logger.debug("Could not list toplevels: %s", exc)
        return set()


def mark_new_dialog_default_visible(before) -> None:
    """Mark whichever toplevel appeared since ``before`` was captured.

    ``Gtk.AlertDialog`` builds its own window and never hands it to us, so the
    only way to reach it is to spot the toplevel it added. Deferred to an idle
    callback so the window exists by the time we look.
    """
    known = set(before or ())

    def _arm():
        try:
            for window in Gtk.Window.get_toplevels():
                if window not in known:
                    mark_default_response_visible(window)
        except Exception as exc:
            logger.debug("Could not mark the new dialog's default: %s", exc)
        return False

    GLib.idle_add(_arm)

"""Give activation back to the parent when a transient window closes.

``Gtk.Window.close()`` hides the window straight away. Nothing hands activation
to its transient parent: GDK does that only when Windows sends ``WM_CLOSE``,
which a button, the client-side title bar or Escape never produce. Windows
then picks the next window from its activation history, and that is the
parent only if the parent was active just before this window. Otherwise
another application comes forward and the main window stays behind it (GH
#1339). Two common routes break the chain: an item in the sidebar's row menu,
whose non-autohide popover takes activation when clicked, and coming back to
an open dialog from another application.

Doing what GDK's ``WM_CLOSE`` handler does, for every close, puts the parent
back in the chain. A modal window is still mapped at this point, so GDK passes
activation straight back to it, which is fine: Windows now remembers the
parent. A close that a handler then refuses therefore changes nothing.

Windows only, and best effort: a window that cannot hand over still closes.
"""
from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)

__all__ = ["hand_activation_to_parent", "install_transient_close_handoff"]

_installed = False


def hand_activation_to_parent(window) -> None:
    """Present ``window``'s transient parent if ``window`` holds activation.

    A window closing in the background leaves activation alone, so it cannot
    pull the application over whatever the user moved on to. An unmapped parent
    is skipped too: presenting it would show a window that was hidden on purpose.
    """
    parent = window.get_transient_for()
    if parent is None or not parent.get_mapped() or not window.is_active():
        return
    parent.present()


def _on_close_request(window, *_args) -> bool:
    try:
        hand_activation_to_parent(window)
    except Exception as exc:
        logger.debug("Could not hand activation to the parent: %s", exc)
    return True  # keep the hook installed


def install_transient_close_handoff() -> bool:
    """Watch every window's close. Returns True once the hook is in place.

    Call after the first window exists: GTK registers ``close-request`` when
    ``Gtk.Window`` is first instantiated, and the hook cannot attach before.
    Safe to call again.

    An emission hook rather than a handler on each window: the dialogs are
    many, ``Gtk.Window.get_toplevels()`` announces a window from inside its
    construction, and this way none can be missed.
    """
    global _installed
    if sys.platform != "win32":
        return False
    if not _installed:
        from gi.repository import GObject, Gtk

        GObject.add_emission_hook(Gtk.Window, "close-request", _on_close_request)
        _installed = True
    return True

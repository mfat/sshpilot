"""Keep the ibus input method away from text views whose window is gone.

GTK 4.24 crashes the app on X11 when a window holding a text view closes
while ibus is the input method (GH #1374, seen on closing the SSH config
editor). A text view updates the input method's cursor position from an idle
callback. Unrealizing the view does not remove that callback; only disposing
it does, and the app keeps its windows' widgets alive past ``destroy()``. The
callback then runs after the window's surface is gone, and ibus 1.5.35 looks
the surface up without checking it and dereferences NULL on X11.

Pointing an unrealized view at GTK's built-in input method stops that: the
multicontext drops its ibus delegate at once, so the late callback reaches the
built-in one, which ignores cursor positions. The view gets its own input
method back when it is realized again.

An emission hook rather than a handler per view: text views come from many
dialogs and plugins, and this way none is missed.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["install_text_view_im_guard"]

# GTK's own input method; it ignores the cursor location it is given.
PARKED_IM_MODULE = "gtk-im-context-simple"
# Set on views this guard parked, holding the input method they had before.
_SAVED_ATTR = "_sshpilot_parked_im_module"

_installed = False


def _on_unrealize(widget, *_args) -> bool:
    from gi.repository import Gtk

    if isinstance(widget, Gtk.TextView):
        try:
            current = widget.get_property("im-module")
            if current != PARKED_IM_MODULE:
                setattr(widget, _SAVED_ATTR, current)
                widget.set_property("im-module", PARKED_IM_MODULE)
        except Exception as exc:
            logger.debug("Could not park a text view's input method: %s", exc)
    return True  # keep the hook installed


def _on_realize(widget, *_args) -> bool:
    from gi.repository import Gtk

    if isinstance(widget, Gtk.TextView) and hasattr(widget, _SAVED_ATTR):
        try:
            widget.set_property("im-module", getattr(widget, _SAVED_ATTR))
            delattr(widget, _SAVED_ATTR)
        except Exception as exc:
            logger.debug("Could not restore a text view's input method: %s", exc)
    return True


def install_text_view_im_guard() -> bool:
    """Watch every text view's realize/unrealize. Safe to call again.

    Returns True once the hooks are in place.
    """
    global _installed
    if not _installed:
        from gi.repository import GObject, Gtk

        # The signals belong to GtkWidget, whose class must exist first.
        GObject.type_class_ref(Gtk.TextView)
        GObject.add_emission_hook(Gtk.TextView, "unrealize", _on_unrealize)
        GObject.add_emission_hook(Gtk.TextView, "realize", _on_realize)
        _installed = True
    return True

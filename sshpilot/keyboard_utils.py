"""Keyboard layout utilities for GTK4 shortcut handling."""
from __future__ import annotations

from gi.repository import Gdk


def get_latin_keyval(keycode: int, state) -> int | None:
    """Return the keyval for *keycode* as if the Latin (group-0) layout were active.

    GTK4 passes the layout-specific keyval to key handlers, so pressing 'C' with
    a Russian layout yields KEY_Cyrillic_es instead of KEY_c.  Translating the
    hardware keycode to group 0 gives the Latin equivalent and lets shortcuts work
    regardless of the active keyboard layout.

    Returns None when translation fails; callers should fall back to the original
    keyval in that case.
    """
    display = Gdk.Display.get_default()
    if display is None:
        return None
    ok, keyval, *_ = display.translate_key(keycode, state, 0)
    return keyval if ok else None

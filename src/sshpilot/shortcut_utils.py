from __future__ import annotations

import sys


DOUBLE_SHIFT_SHORTCUT = "<double-shift>"

#: Canonical name of the window action every fullscreen accelerator reaches.
TOGGLE_FULLSCREEN_ACTION = "toggle-fullscreen"


def default_fullscreen_shortcuts(mac: bool) -> list[str]:
    """Platform default accelerators for Toggle Fullscreen.

    macOS deliberately does not take F11. The OS binds it itself, and SSH
    Pilot defers to native fullscreen there, so the default is the standard
    macOS Control+Command+F. Everywhere else F11 is the expected key.
    """
    return ["<Control><Meta>f"] if mac else ["F11"]


class DoubleShiftDetector:
    """Recognize two clean Shift presses within a short interval."""

    def __init__(self, interval_seconds: float = 0.5) -> None:
        self.interval_seconds = interval_seconds
        self._last_release_at: float | None = None
        self._shift_down = False
        self._current_press_tainted = False
        self._completes_double_shift = False

    def reset(self) -> None:
        self._last_release_at = None
        self._shift_down = False
        self._current_press_tainted = False
        self._completes_double_shift = False

    def key_pressed(self, is_shift: bool, now: float) -> bool:
        """Track a key press; completion is reported on the second release."""
        if not is_shift:
            self._last_release_at = None
            if self._shift_down:
                self._current_press_tainted = True
            return False

        # Ignore key-repeat while either Shift key remains held.
        if self._shift_down:
            return False

        self._shift_down = True
        self._current_press_tainted = False
        elapsed = (
            now - self._last_release_at
            if self._last_release_at is not None
            else None
        )
        self._completes_double_shift = bool(
            elapsed is not None
            and 0 <= elapsed <= self.interval_seconds
        )
        if self._completes_double_shift:
            self._last_release_at = None
        return False

    def pointer_activity(self) -> None:
        """Cancel any pending double-tap: the pointer owns this Shift.

        Shift+drag is the only way to select terminal text while a remote
        application holds mouse tracking (issue #1178), so a Shift press that
        brackets a drag is a selection, never half of a double-tap. Without
        this, a second Shift+drag inside the interval opened Omnisearch and
        took focus from the terminal, which is what made copying appear to
        stop working after the first attempt or two.
        """
        self._last_release_at = None
        self._completes_double_shift = False
        if self._shift_down:
            self._current_press_tainted = True

    def key_released(self, is_shift: bool, now: float) -> bool:
        if not is_shift or not self._shift_down:
            return False
        self._shift_down = False
        activated = (
            self._completes_double_shift
            and not self._current_press_tainted
        )
        if activated or self._current_press_tainted:
            self._last_release_at = None
        else:
            self._last_release_at = now
        self._current_press_tainted = False
        self._completes_double_shift = False
        return activated


# ── non-Latin keyboard layouts ───────────────────────────────────────────────
#
# GTK cannot match a ``<Primary><Shift>`` letter accelerator while a non-Latin
# layout is active (GH #1249). ``gdk_key_event_matches()`` does carry a
# cross-layout fallback, but it looks the accelerator's *lowercase* keyval up in
# the keymap and then demands ``keys[i].level == level``: the only entry for
# "c" sits at level 0, while an event carrying Shift is at level 1, so the loop
# can never match and the shortcut silently becomes a plain keystroke -- VTE
# turns Ctrl+Shift+C into ^C. Unshifted accelerators are unaffected (level 0 on
# both sides), which is why Ctrl+C still works under Cyrillic and Ctrl+Shift+C
# does not. Ptyxis and GNOME Console inherit the same bug; Konsole escapes it
# because Qt resolves every event through the first Latin group itself.
#
# So do what Qt does: when the active layout gave the key a non-Latin keyval,
# ask the keymap what that *physical* key produces in the layout's Latin
# groups, and match the accelerator against that instead.


def _accel_modifier_mask():
    """The modifiers accelerator matching considers, as ``gdk_key_event_matches``."""
    from gi.repository import Gdk

    return (
        Gdk.ModifierType.CONTROL_MASK
        | Gdk.ModifierType.SHIFT_MASK
        | Gdk.ModifierType.ALT_MASK
        | Gdk.ModifierType.SUPER_MASK
        | Gdk.ModifierType.HYPER_MASK
        | Gdk.ModifierType.META_MASK
    )


def latin_fallback_keyvals(display, keyval: int, keycode: int) -> tuple:
    """Latin keyvals *keycode* also produces, when the active layout is not Latin.

    Returns an empty tuple whenever no fallback is wanted: for a key the active
    layout already resolved to ASCII (GTK matches those itself), for keys with
    no character at all (Page_Up, F1 -- their keyval is identical in every
    group), and for keymaps with no Latin group to fall back to.

    Only level 0 is collected: an accelerator names the unshifted letter, and
    that is the level GTK's own lookup uses.
    """
    from gi.repository import Gdk

    codepoint = Gdk.keyval_to_unicode(keyval)
    if codepoint == 0 or 0x20 < codepoint < 0x7F:
        return ()
    try:
        found, keys, keyvals = display.map_keycode(keycode)
    except Exception:
        return ()
    if not found:
        return ()
    candidates = []
    for key, candidate in zip(keys, keyvals):
        if key.level != 0:
            continue
        point = Gdk.keyval_to_unicode(candidate)
        if 0x20 < point < 0x7F and candidate not in candidates:
            candidates.append(candidate)
    return tuple(candidates)


def accel_matches_latin_fallback(event, accel: str, candidates, state) -> bool:
    """Whether *accel* names one of *candidates* with the event's modifiers.

    ``event`` is consulted first: GTK's own matching wins whenever it has any
    opinion at all. ``<Primary>c`` still partial-matches under a Cyrillic
    layout, and firing here as well would activate the action twice.
    """
    from gi.repository import Gdk, Gtk

    if not candidates or not accel or accel == DOUBLE_SHIFT_SHORTCUT:
        return False
    trigger = Gtk.ShortcutTrigger.parse_string(accel)
    if trigger is None:
        return False
    if event is not None and trigger.trigger(event, False) != Gdk.KeyMatch.NONE:
        return False
    parsed, accel_keyval, accel_mods = Gtk.accelerator_parse(accel)
    if not parsed or not accel_keyval:
        return False
    mask = _accel_modifier_mask()
    if (state & mask) != (accel_mods & mask):
        return False
    return Gdk.keyval_to_lower(accel_keyval) in candidates


def get_primary_modifier_label() -> str:
    """Return the label for the primary modifier key.

    Uses "⌘" on macOS and "Ctrl" on other platforms.
    """
    return "\u2318" if sys.platform == "darwin" else "Ctrl"


def install_esc_to_close(window) -> None:
    """Make Escape close *window*.

    Adw.Window lacks Adw.Dialog's built-in Esc-to-close. The shortcut runs in
    the bubble phase, so widgets that consume Esc themselves (e.g. a
    SearchEntry clearing its text) still win.
    """
    # Deferred so importing this module never requires GTK.
    from gi.repository import Gtk

    controller = Gtk.ShortcutController()
    controller.add_shortcut(
        Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("Escape"),
            Gtk.CallbackAction.new(lambda *_: (window.close(), True)[1]),
        )
    )
    window.add_controller(controller)


def install_search_esc(search_entry, window) -> None:
    """Esc in *search_entry* clears the filter, or closes *window* if empty.

    GtkSearchEntry consumes Escape itself (its stop-search binding), so the
    window-level install_esc_to_close() shortcut never fires while the entry
    has focus — which it often has by default.
    """

    def _on_stop_search(entry):
        if entry.get_text():
            entry.set_text("")
        else:
            window.close()

    search_entry.connect("stop-search", _on_stop_search)


__all__ = [
    "DOUBLE_SHIFT_SHORTCUT",
    "DoubleShiftDetector",
    "accel_matches_latin_fallback",
    "latin_fallback_keyvals",
    "get_primary_modifier_label",
    "install_esc_to_close",
    "install_search_esc",
]

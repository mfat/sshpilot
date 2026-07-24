from __future__ import annotations

import sys


DOUBLE_SHIFT_SHORTCUT = "<double-shift>"


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
    "get_primary_modifier_label",
    "install_esc_to_close",
    "install_search_esc",
]

from __future__ import annotations

import sys


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


__all__ = ["get_primary_modifier_label", "install_esc_to_close", "install_search_esc"]

"""Tests for the MonospaceFontDialog extraction.

MonospaceFontDialog was moved verbatim out of preferences.py into the leaf
module sshpilot/monospace_font_dialog.py to shrink that god-object. The dialog
is a GTK/Adw widget, so these tests do NOT exercise rendering or font
enumeration (the test suite stubs gi.repository — no real GTK). They guard the
*extraction contract*: the class is importable from its new home, still
re-exported from preferences for back-compat, and keeps its public method
surface intact.
"""

from sshpilot.monospace_font_dialog import MonospaceFontDialog


def test_importable_from_new_module():
    assert MonospaceFontDialog.__name__ == "MonospaceFontDialog"
    assert MonospaceFontDialog.__module__ == "sshpilot.monospace_font_dialog"


def test_reexported_from_preferences_for_back_compat():
    from sshpilot.preferences import MonospaceFontDialog as FromPrefs
    assert FromPrefs is MonospaceFontDialog


def test_public_method_surface_intact():
    expected = {
        "filter_fonts",
        "on_cancel",
        "on_search_changed",
        "on_select",
        "on_selection_changed",
        "on_size_changed",
        "populate_fonts",
        "select_current_font",
        "set_callback",
        "setup_ui",
        "update_preview",
    }
    actual = {n for n in vars(MonospaceFontDialog) if not n.startswith("__")}
    assert expected <= actual

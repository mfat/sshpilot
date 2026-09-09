"""Accelerator matching under non-Latin keyboard layouts (GH #1249).

GTK's own cross-layout fallback cannot reach a Shift-bearing letter
accelerator, so ``Ctrl+Shift+C`` under Cyrillic never matched and VTE turned it
into a plain ``^C``. The keyvals below are the real ones captured from a live
GTK4 session with the ``us``/``ir``/``ru`` layouts installed.

Needs the real GTK keyval tables, so it runs under the GUI opt-in:
SSHPILOT_GUI_TESTS=1 pytest -m gui
"""

from types import SimpleNamespace

import pytest

from gi.repository import Gdk, Gtk

from sshpilot.shortcut_utils import (
    DOUBLE_SHIFT_SHORTCUT,
    accel_matches_latin_fallback,
    latin_fallback_keyvals,
)
from tests._gui_harness import requires_gui  # the `gui` fixture comes from conftest

requires_gui()

pytestmark = pytest.mark.gui

CTRL = Gdk.ModifierType.CONTROL_MASK
SHIFT = Gdk.ModifierType.SHIFT_MASK

# keycode 54 (AB03) as the live keymap reports it: us, Persian, Cyrillic, us.
_AB03 = [
    (0, 0, Gdk.KEY_c), (0, 1, Gdk.KEY_C),
    (1, 0, Gdk.KEY_Arabic_zain), (1, 1, 0x1000698),
    (2, 0, Gdk.KEY_Cyrillic_es), (2, 1, Gdk.KEY_Cyrillic_ES),
    (3, 0, Gdk.KEY_c), (3, 1, Gdk.KEY_C),
]
_PAGE_UP = [(0, 0, Gdk.KEY_Page_Up)]


def _display(entries, found=True):
    keys = [SimpleNamespace(group=g, level=l) for g, l, _ in entries]
    keyvals = [kv for _, _, kv in entries]
    return SimpleNamespace(map_keycode=lambda _kc: (found, keys, keyvals))


def test_cyrillic_letter_falls_back_to_the_latin_group():
    display = _display(_AB03)

    candidates = latin_fallback_keyvals(display, Gdk.KEY_Cyrillic_ES, 54)

    assert Gdk.KEY_c in candidates
    assert accel_matches_latin_fallback(
        None, "<Primary><Shift>c", candidates, CTRL | SHIFT
    )


def test_fallback_does_not_match_a_different_letter():
    candidates = latin_fallback_keyvals(_display(_AB03), Gdk.KEY_Cyrillic_ES, 54)

    assert not accel_matches_latin_fallback(
        None, "<Primary><Shift>v", candidates, CTRL | SHIFT
    )


def test_fallback_requires_the_accelerator_modifiers():
    candidates = latin_fallback_keyvals(_display(_AB03), Gdk.KEY_Cyrillic_ES, 54)

    assert not accel_matches_latin_fallback(
        None, "<Primary><Shift>c", candidates, CTRL
    )


def test_latin_layouts_never_reach_the_fallback():
    """GTK matches these itself; a second opinion would double-activate."""
    assert latin_fallback_keyvals(_display(_AB03), Gdk.KEY_C, 54) == ()
    assert latin_fallback_keyvals(_display(_AB03), Gdk.KEY_c, 54) == ()


def test_keys_without_a_character_are_ignored():
    """Page_Up carries the same keyval in every group, so it needs no rescue."""
    assert latin_fallback_keyvals(_display(_PAGE_UP), Gdk.KEY_Page_Up, 112) == ()


def test_keymap_without_a_latin_group_yields_no_candidates():
    cyrillic_only = [(0, 0, Gdk.KEY_Cyrillic_es), (0, 1, Gdk.KEY_Cyrillic_ES)]

    candidates = latin_fallback_keyvals(
        _display(cyrillic_only), Gdk.KEY_Cyrillic_ES, 54
    )

    assert candidates == ()
    assert not accel_matches_latin_fallback(
        None, "<Primary><Shift>c", candidates, CTRL | SHIFT
    )


def test_unmapped_keycode_yields_no_candidates():
    display = _display(_AB03, found=False)

    assert latin_fallback_keyvals(display, Gdk.KEY_Cyrillic_ES, 54) == ()


def test_double_shift_sentinel_is_not_an_accelerator():
    candidates = latin_fallback_keyvals(_display(_AB03), Gdk.KEY_Cyrillic_ES, 54)

    assert not accel_matches_latin_fallback(
        None, DOUBLE_SHIFT_SHORTCUT, candidates, CTRL | SHIFT
    )


def test_gtk_still_cannot_match_the_shifted_accelerator_itself():
    """Pins the upstream behaviour this whole fallback exists to work around."""
    ok, keyval, mods = Gtk.accelerator_parse("<Primary><Shift>c")

    assert ok and keyval == Gdk.KEY_c and mods == (CTRL | SHIFT)
    # The accelerator names the lowercase keyval, which the keymap only holds
    # at level 0, while a Shift-bearing event arrives at level 1 -- the
    # mismatch that makes gdk_key_event_matches() return NONE.
    assert [level for _g, level, kv in _AB03 if kv == Gdk.KEY_c] == [0, 0]
    assert [level for _g, level, kv in _AB03 if kv == Gdk.KEY_C] == [1, 1]

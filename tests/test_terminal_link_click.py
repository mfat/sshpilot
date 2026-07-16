"""VTE link activation requires Ctrl+click (Cmd+click on macOS), like GNOME Terminal."""

from unittest.mock import patch

import sshpilot.terminal as terminal_mod
from sshpilot.terminal import TerminalWidget

# Stubbed gi.repository.Gdk.ModifierType values are not ints; pin real GDK masks.
_CONTROL = 4          # Gdk.ModifierType.CONTROL_MASK
_SHIFT = 1            # Gdk.ModifierType.SHIFT_MASK
_META = 1 << 28       # Gdk.ModifierType.META_MASK


def test_link_modifier_requires_ctrl_on_linux():
    with patch.object(terminal_mod, "is_macos", return_value=False), patch.object(
        terminal_mod.Gdk.ModifierType, "CONTROL_MASK", _CONTROL
    ), patch.object(terminal_mod.Gdk.ModifierType, "META_MASK", _META):
        assert TerminalWidget._click_has_link_modifier(_CONTROL)
        assert TerminalWidget._click_has_link_modifier(_CONTROL | _SHIFT)
        assert not TerminalWidget._click_has_link_modifier(0)
        assert not TerminalWidget._click_has_link_modifier(_SHIFT)
        assert not TerminalWidget._click_has_link_modifier(_META)


def test_link_modifier_requires_meta_on_macos():
    with patch.object(terminal_mod, "is_macos", return_value=True), patch.object(
        terminal_mod.Gdk.ModifierType, "CONTROL_MASK", _CONTROL
    ), patch.object(terminal_mod.Gdk.ModifierType, "META_MASK", _META):
        assert TerminalWidget._click_has_link_modifier(_META)
        assert not TerminalWidget._click_has_link_modifier(_CONTROL)
        assert not TerminalWidget._click_has_link_modifier(0)

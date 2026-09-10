"""Cursor presentation vocabulary shared by the terminal backends.

One vocabulary for both backends: the shape nicks are VTE's own
("block"/"ibeam"/"underline", matching Ptyxis' gsettings enum) and the blink
nicks are "system"/"on"/"off".  Each backend translates on the way out --
xterm.js spells the I-beam "bar" and has no "system", so the PyXterm side
resolves it against GtkSettings.

This module deliberately imports nothing from gi so Preferences and the tests
can share the vocabulary without pulling in Vte or WebKit.
"""

from __future__ import annotations

from typing import Any

DEFAULT_CURSOR_SHAPE = "block"
DEFAULT_CURSOR_BLINK = "system"

CURSOR_SHAPES = ("block", "ibeam", "underline")
CURSOR_BLINK_MODES = ("system", "on", "off")

# Names of the matching Vte enum members, resolved by the VTE backend.
VTE_CURSOR_SHAPES = {
    "block": "BLOCK",
    "ibeam": "IBEAM",
    "underline": "UNDERLINE",
}

VTE_CURSOR_BLINK_MODES = {
    "system": "SYSTEM",
    "on": "ON",
    "off": "OFF",
}

# xterm.js names the I-beam "bar"; the other two nicks match.
XTERM_CURSOR_STYLES = {
    "block": "block",
    "ibeam": "bar",
    "underline": "underline",
}


def normalize_cursor_shape(value: Any) -> str:
    """Return a known shape nick, falling back to the default."""
    nick = str(value or "").strip().lower()
    return nick if nick in VTE_CURSOR_SHAPES else DEFAULT_CURSOR_SHAPE


def normalize_cursor_blink(value: Any) -> str:
    """Return a known blink nick, falling back to the default.

    ``cursor_blink`` shipped as a boolean that nothing ever read: both call
    sites hardcoded ``CursorBlinkMode.ON``.  A ``True`` in an existing config
    is therefore the stale default rather than a choice anyone made, so it
    resolves to "system" like a fresh install; a hand-edited ``False`` is a
    real preference and becomes "off".
    """
    if isinstance(value, bool):
        return DEFAULT_CURSOR_BLINK if value else "off"
    nick = str(value or "").strip().lower()
    return nick if nick in VTE_CURSOR_BLINK_MODES else DEFAULT_CURSOR_BLINK


def xterm_cursor_style(shape: Any) -> str:
    """Translate a shape nick into an xterm.js ``cursorStyle``."""
    return XTERM_CURSOR_STYLES[normalize_cursor_shape(shape)]

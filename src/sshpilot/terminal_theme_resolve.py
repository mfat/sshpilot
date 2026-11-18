"""Terminal colours for the GTK-drawn backends, resolved from the config.

Shared by the built-in backend and the gtkghostty backend so a theme looks
the same in both: the profile's colours, the default 16-colour palette when
the profile has none, and the group-colour override.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, List, NamedTuple, Optional, Tuple

import gi

gi.require_version("Gdk", "4.0")
from gi.repository import Gdk  # noqa: E402

from .terminal_color_utils import get_contrast_color  # noqa: E402

logger = logging.getLogger(__name__)

Rgb = Tuple[float, float, float]

DEFAULT_PALETTE = (
    "#000000", "#CC0000", "#4E9A06", "#C4A000",
    "#3465A4", "#75507B", "#06989A", "#D3D7CF",
    "#555753", "#EF2929", "#8AE234", "#FCE94F",
    "#729FCF", "#AD7FA8", "#34E2E2", "#EEEEEC",
)


def daemon_output_is_rendered() -> bool:
    """True when daemon session output was already rendered by ConPTY.

    The daemon is always local (a Unix socket, or loopback TCP on Windows) and
    ``daemon/server.py`` runs sessions under ConPTY on Windows. conhost then
    sends repaints of its own screen rather than the remote's raw stream.
    """

    return sys.platform == "win32"


def parse_rgb(value: str, fallback: Rgb = (0.0, 0.0, 0.0)) -> Rgb:
    rgba = Gdk.RGBA()
    if value and rgba.parse(value):
        return (rgba.red, rgba.green, rgba.blue)
    return fallback


class ResolvedTheme(NamedTuple):
    foreground: Rgb
    background: Rgb
    cursor: Rgb
    palette: List[Rgb]
    font: Optional[str]


def resolve_theme(owner: Any, theme_name: Optional[str] = None) -> ResolvedTheme:
    """The colours a terminal owned by ``owner`` (a TerminalWidget) should use."""
    config = getattr(owner, "config", None)
    try:
        if theme_name is None and config:
            theme_name = config.get_setting("terminal.theme", "default")
        profile = config.get_terminal_profile(theme_name) if config else {}
    except Exception:
        logger.debug("Could not read terminal profile", exc_info=True)
        profile = {}
    fg = parse_rgb(profile.get("foreground", "#000000"))
    bg = parse_rgb(profile.get("background", "#FFFFFF"), (1.0, 1.0, 1.0))
    cursor = parse_rgb(profile.get("cursor_color", profile.get("foreground", "#000000")))
    try:
        use_group = bool(config.get_setting("ui.use_group_color_in_terminal", False)) if config else False
        override = owner._get_group_color_rgba() if hasattr(owner, "_get_group_color_rgba") else None
        if use_group and override is not None:
            bg = (override.red, override.green, override.blue)
            contrast = get_contrast_color(override)
            fg = cursor = (contrast.red, contrast.green, contrast.blue)
    except Exception:
        logger.debug("Group color override failed", exc_info=True)
    palette = [parse_rgb(c) for c in (profile.get("palette") or DEFAULT_PALETTE)]
    return ResolvedTheme(fg, bg, cursor, palette, profile.get("font") or None)

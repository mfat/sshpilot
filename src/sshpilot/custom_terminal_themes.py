"""User-defined terminal color schemes (GTK-free).

Custom themes live in config.json under ``terminal.custom_themes`` as
``{key: theme}``, with the same shape as the built-in themes in
``Config.load_builtin_themes()``. Entries are written by the theme editor but
can also be hand-written, so everything read back goes through
:func:`normalize_terminal_theme` and invalid entries are skipped.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Collection, Dict, Mapping, Optional

CUSTOM_THEMES_SETTING = "terminal.custom_themes"
CUSTOM_THEME_KEY_PREFIX = "custom_"
MAX_THEME_NAME_LENGTH = 64
PALETTE_SIZE = 16

logger = logging.getLogger(__name__)

# Neither backend draws these (see _reset_highlight() in terminal_backends),
# but every theme carries them so the shape matches the built-ins.
_DEFAULT_HIGHLIGHT_BACKGROUND = "#4A90E2"
_DEFAULT_HIGHLIGHT_FOREGROUND = "#FFFFFF"

_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def normalize_color(value: Any) -> Optional[str]:
    """Return *value* as ``#RRGGBB``, or ``None`` when it is not a hex color."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not _HEX_COLOR.match(text):
        return None
    digits = text[1:]
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    return "#" + digits.upper()


def normalize_terminal_theme(data: Any) -> Optional[Dict[str, Any]]:
    """Validate a theme and return it in the built-in shape, or ``None``.

    ``name``, ``foreground``, ``background`` and a 16-color ``palette`` are
    required. An 8-color palette is accepted and repeated for the bright
    colors. The cursor defaults to the foreground.
    """
    if not isinstance(data, Mapping):
        return None
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    foreground = normalize_color(data.get("foreground"))
    background = normalize_color(data.get("background"))
    if foreground is None or background is None:
        return None

    raw_palette = data.get("palette")
    if not isinstance(raw_palette, (list, tuple)) or len(raw_palette) not in (8, PALETTE_SIZE):
        return None
    palette = [normalize_color(color) for color in raw_palette]
    if any(color is None for color in palette):
        return None
    if len(palette) == 8:
        palette = palette + palette

    return {
        "name": name.strip()[:MAX_THEME_NAME_LENGTH],
        "foreground": foreground,
        "background": background,
        "cursor_color": normalize_color(data.get("cursor_color")) or foreground,
        "highlight_background": (
            normalize_color(data.get("highlight_background"))
            or _DEFAULT_HIGHLIGHT_BACKGROUND
        ),
        "highlight_foreground": (
            normalize_color(data.get("highlight_foreground"))
            or _DEFAULT_HIGHLIGHT_FOREGROUND
        ),
        "palette": palette,
    }


def load_custom_themes(
    stored: Any, reserved_keys: Collection[str]
) -> Dict[str, Dict[str, Any]]:
    """Valid custom themes from the stored setting, in stored order.

    Keys that collide with *reserved_keys* (the built-ins) are skipped so a
    hand-written entry cannot replace a built-in theme.
    """
    themes: Dict[str, Dict[str, Any]] = {}
    if not isinstance(stored, Mapping):
        return themes
    for key, data in stored.items():
        if not isinstance(key, str) or not key or key in reserved_keys:
            continue
        theme = normalize_terminal_theme(data)
        if theme is None:
            logger.warning(
                "Ignoring custom terminal theme %r: it needs a name, hex "
                "foreground/background colors and an 8- or 16-color palette",
                key,
            )
            continue
        themes[key] = theme
    return themes


def new_custom_theme_key(name: str, taken: Collection[str]) -> str:
    """A stable, unused key for a new theme called *name*."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "theme"
    base = CUSTOM_THEME_KEY_PREFIX + slug[:40]
    key = base
    suffix = 2
    while key in taken:
        key = f"{base}_{suffix}"
        suffix += 1
    return key

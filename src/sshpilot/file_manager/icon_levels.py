"""Icon-size level constants shared by file-manager pane and pane controls.

Kept as a leaf module (no intra-package imports) so both ``pane`` and
``pane_controls`` can import it without forming an import cycle.
"""

# Nautilus's zoom steps (nautilus-enums.h): list and grid zoom separately,
# each indexed by its own level; level 0 = smallest.
_LIST_ICON_SIZES = (16, 32, 64)
_GRID_ICON_SIZES = (48, 64, 96, 168, 256)
# Nautilus's defaults: list "medium" (32px), grid "medium" (96px).
_DEFAULT_LIST_LEVEL = 1
_DEFAULT_GRID_LEVEL = 2
_MIN_ICON_LEVEL = 0
_MAX_LIST_LEVEL = len(_LIST_ICON_SIZES) - 1
_MAX_GRID_LEVEL = len(_GRID_ICON_SIZES) - 1

# Per-view lookups keyed by the pane's view name ("list" / "grid").
_DEFAULT_ICON_LEVELS = {"list": _DEFAULT_LIST_LEVEL, "grid": _DEFAULT_GRID_LEVEL}
_MAX_ICON_LEVELS = {"list": _MAX_LIST_LEVEL, "grid": _MAX_GRID_LEVEL}


def clamp_icon_level(view: str, level: int) -> int:
    return max(_MIN_ICON_LEVEL, min(_MAX_ICON_LEVELS[view], int(level)))

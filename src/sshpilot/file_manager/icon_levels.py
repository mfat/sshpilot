"""Icon-size level constants shared by file-manager pane and pane controls.

Kept as a leaf module (no intra-package imports) so both ``pane`` and
``pane_controls`` can import it without forming an import cycle.
"""

# Both tuples are indexed by the same level; level 0 = smallest,
# len-1 = largest. Defaults match the GNOME Files / Adwaita HIG for
# rich list rows (24 px) and grid cells (72 px).
_LIST_ICON_SIZES = (16, 24, 32, 48, 64)
_GRID_ICON_SIZES = (48, 72, 96, 128, 192)
_DEFAULT_ICON_LEVEL = 1
_MIN_ICON_LEVEL = 0
_MAX_ICON_LEVEL = len(_LIST_ICON_SIZES) - 1

from __future__ import annotations

import sys


def get_primary_modifier_label() -> str:
    """Return the label for the primary modifier key.

    Uses "⌘" on macOS and "Ctrl" on other platforms.
    """
    return "\u2318" if sys.platform == "darwin" else "Ctrl"


__all__ = ["get_primary_modifier_label"]

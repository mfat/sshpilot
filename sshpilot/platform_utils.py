"""Platform-related utility functions."""

import platform


def is_macos() -> bool:
    """Return True if running on macOS."""
    return platform.system() == "Darwin"

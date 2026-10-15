"""Which platform this process runs on (no GLib).

Functions, not import-time constants, so tests can patch them. Modules that
re-export them (``platform_utils``, ``backup_manager``) keep their own names
as patch points.

Do not use these for ``fcntl`` / ``pty`` / ``termios`` import guards: those
must follow the real interpreter, not a patched answer.
"""
from __future__ import annotations

import os
import platform


def is_macos() -> bool:
    """Return True if running on macOS."""
    return platform.system() == "Darwin"


def is_windows() -> bool:
    """Return True if running on Windows."""
    return platform.system() == "Windows"


def is_flatpak() -> bool:
    """Return True if running inside a Flatpak sandbox."""
    return os.environ.get("FLATPAK_ID") is not None or os.path.exists("/.flatpak-info")

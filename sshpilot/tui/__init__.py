"""
Terminal UI package for sshPilot.

This module exposes the public entry point for running the curses-based TUI.
The actual TUI implementation lives in ``sshpilot.tui.app``. We import it
lazily so that running ``python -m sshpilot.tui.app`` does not emit the
``RuntimeWarning`` that occurs when the module is imported twice before being
executed.
"""

from __future__ import annotations

from typing import Any

__all__ = ["main"]


def main(*args: Any, **kwargs: Any) -> Any:
    """Entry point used by ``sshpilot-tui`` console script."""
    from .app import main as _app_main

    return _app_main(*args, **kwargs)

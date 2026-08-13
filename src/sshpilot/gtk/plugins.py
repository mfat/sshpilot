"""GTK plugin contribution surface.

Widgets, rows, dialogs, menus, and preferences pages belong here.
Headless plugin contracts live in ``sshpilot.core.plugins``.
"""
from __future__ import annotations

# Re-export the live UiHost implementation for clarity of ownership.
# ``sshpilot.plugins.host.UiHost`` remains the runtime class used by the app;
# new GTK-only plugin helpers should land in this package.
from sshpilot.plugins.host import UiHost  # noqa: F401

__all__ = ["UiHost"]

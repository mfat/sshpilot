"""GTK-free, UI-agnostic core for sshPilot.

Nothing under ``sshpilot.core`` may import ``gi`` / Gtk / Gdk / Adw / Vte /
GLib / Gio. See ``docs/architecture/core-boundary.md``.
"""
from .errors import CoreError, ErrorCode, FieldError

__all__ = ["CoreError", "ErrorCode", "FieldError"]

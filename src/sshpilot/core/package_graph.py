"""Package classification manifest for dependency-direction enforcement.

Prefer an explicit allowed graph over a growing blacklist of UI modules.
"""
from __future__ import annotations

# Packages that must remain free of GI / GTK.
BOUNDARY_PACKAGES = ("core", "api", "daemon")

# Absolute module prefixes that boundary packages must never import.
FORBIDDEN_UI_PREFIXES = (
    "sshpilot.gtk",
    "sshpilot.main",
    "sshpilot.window",
    "sshpilot.window_dialogs",
    "sshpilot.preferences",
    "sshpilot.terminal",
    "sshpilot.sidebar",
    "sshpilot.connection_dialog",
    "sshpilot.known_hosts_editor",
    "sshpilot.secret_unlock_dialog",
    "sshpilot.scp_window",
    "sshpilot.sshcopyid_window",
    "sshpilot.file_manager",
)

# Allowed edges: importer_package -> imported_prefix
ALLOWED_EDGES = {
    "core": (
        "sshpilot.core",
        "sshpilot.connection_identity",  # pure identity helpers
        "sshpilot.platform.paths",  # path helpers used by CLI
    ),
    "api": (
        "sshpilot.api",
        "sshpilot.core",
        "sshpilot.connection_identity",
    ),
    "daemon": (
        "sshpilot.daemon",
        "sshpilot.api",
        "sshpilot.core",
        "sshpilot.connection_identity",
        # Daemon may import selected top-level pure helpers; GTK is forbidden.
    ),
}

FORBIDDEN_GI_MODULES = frozenset({"gi", "gi.repository"})
FORBIDDEN_GI_NAMES = frozenset({"Gtk", "Gdk", "Adw", "Vte", "GLib", "Gio", "GObject"})

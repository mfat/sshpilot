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
        "sshpilot.api",  # API model layer is shared language (GTK-free leaf)
        "sshpilot.runtime_identity",
        "sshpilot.platform.paths",  # path helpers used by CLI
        "sshpilot.ssh_config_document",  # GI-free lossless config document
        "sshpilot.ssh_config_formatter",  # GI-free managed-option registry
        "sshpilot.identity",  # GI-free identity-provider contract
        "sshpilot.providers",  # GI-free identity provider definitions
    ),
    "api": (
        "sshpilot.api",
        "sshpilot.core",
        "sshpilot.runtime_identity",
    ),
    "daemon": (
        "sshpilot.daemon",
        "sshpilot.api",
        "sshpilot.core",
        "sshpilot.runtime_identity",
        "sshpilot.platform.paths",  # GI-free path helpers (headless composition)
        # Daemon may import selected top-level headless helpers; GTK and
        # GObject adapters are forbidden. Registered GObject-adapter debt is
        # tracked in tests/core/test_dependency_boundary.py::DAEMON_DEBT.
        "sshpilot.askpass_utils",
        "sshpilot.authorized_keys_parser",  # GI-free authorized_keys parser
        "sshpilot.ssh_config_utils",
        "sshpilot.transfer_scp",  # GTK-free native SCP operand helpers
        "sshpilot.sftp",
        "sshpilot.ssh_multiplex",  # GI-free connection-multiplex helpers
        "sshpilot.backup_archive",  # GI-free secret backup archive (daemon transfer)
        "sshpilot.backup_backends",  # GI-free secret backup destination adapters
        "sshpilot.logging_support",  # shared GTK-free policy and redaction
    ),
}

# Top-level modules that are GObject/GTK-facing adapters. The daemon must not
# import them (they pull GI transitively); current violations are registered as
# debt in tests/core/test_dependency_boundary.py::DAEMON_DEBT.
FORBIDDEN_GOBJECT_ADAPTERS = frozenset(
    {
        "sshpilot.config",
        "sshpilot.connection_manager",
        "sshpilot.groups",
        "sshpilot.plugins",
        "sshpilot.secret_storage",
        "sshpilot.terminal_manager",
        "sshpilot.key_manager",
        "sshpilot.known_hosts_editor",
        "sshpilot.backup_manager",
        "sshpilot.platform_utils",
    }
)

FORBIDDEN_GI_MODULES = frozenset({"gi", "gi.repository"})
FORBIDDEN_GI_NAMES = frozenset({"Gtk", "Gdk", "Adw", "Vte", "GLib", "Gio", "GObject"})

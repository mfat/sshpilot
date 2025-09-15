"""Platform-related utility functions."""

import os
import platform

from gi.repository import GLib

APP_ID = "io.github.mfat.sshpilot"
APP_NAME = "sshpilot"


def is_macos() -> bool:
    """Return True if running on macOS."""
    return platform.system() == "Darwin"


def is_flatpak() -> bool:
    """Return True if running inside a Flatpak sandbox."""
    return os.environ.get("FLATPAK_ID") is not None or os.path.exists("/.flatpak-info")


def get_config_dir() -> str:
    """Return the per-user configuration directory for sshPilot."""
    return os.path.join(GLib.get_user_config_dir(), APP_NAME)


def get_data_dir() -> str:
    """Return the per-user data directory for sshPilot."""
    return os.path.join(GLib.get_user_data_dir(), APP_NAME)


SSH_DIR_ENV = "SSHPILOT_SSH_DIR"


def get_ssh_dir() -> str:
    """Return the SSH directory path.

    Defaults to ``GLib.get_home_dir()/.ssh`` but can be overridden by the
    ``SSHPILOT_SSH_DIR`` environment variable.
    """
    override = os.environ.get(SSH_DIR_ENV)
    if override:
        return os.path.expanduser(override)
    return os.path.join(GLib.get_home_dir(), ".ssh")


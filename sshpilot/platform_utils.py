"""Platform-related utility functions."""

import os
import platform
import shutil

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


_sshpass_path_cache: str | None = None
_sshpass_checked: bool = False


def get_sshpass_path() -> str | None:
    """Return the path to the sshpass binary, or None if unavailable.

    The result is cached after the first call so repeated lookups are free.
    Checks the Flatpak-bundled location first, then falls back to PATH.
    """
    global _sshpass_path_cache, _sshpass_checked
    if _sshpass_checked:
        return _sshpass_path_cache

    flatpak_path = "/app/bin/sshpass"
    if os.path.exists(flatpak_path) and os.access(flatpak_path, os.X_OK):
        _sshpass_path_cache = flatpak_path
    else:
        _sshpass_path_cache = shutil.which("sshpass")

    _sshpass_checked = True
    return _sshpass_path_cache


def get_ssh_dir() -> str:
    """Return the user's SSH directory.

    By default this uses GLib's concept of the home directory and appends
    ``.ssh``. The location can be overridden by setting the
    ``SSHPILOT_SSH_DIR`` environment variable.
    """
    override = os.environ.get("SSHPILOT_SSH_DIR")
    if override:
        return os.path.expanduser(override)
    return os.path.join(GLib.get_home_dir(), ".ssh")


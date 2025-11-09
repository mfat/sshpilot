"""Platform-related utility functions."""

import logging
import os
import platform
from pathlib import Path

from gi.repository import GLib

APP_ID = "io.github.mfat.sshpilot"
APP_NAME = "sshpilot"

logger = logging.getLogger(__name__)


def _normalize_path(path: str) -> str:
    """Expand user references and return an absolute path."""
    return os.path.abspath(os.path.expanduser(path))


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


def get_ssh_dir() -> str:
    """Return the user's SSH directory.

    By default this uses GLib's concept of the home directory and appends
    ``.ssh``. The location can be overridden by setting the
    ``SSHPILOT_SSH_DIR`` environment variable. When GLib cannot determine
    the home directory (returns an empty string), we fall back to Python's
    ``expanduser``/``Path.home`` heuristics to ensure the returned path is
    absolute and points to the real home directory rather than the current
    working directory.
    """
    override = os.environ.get("SSHPILOT_SSH_DIR")
    if override:
        return _normalize_path(override)

    home_dir = GLib.get_home_dir()
    if not home_dir or not str(home_dir).strip():
        expanded = os.path.expanduser("~")
        if expanded and expanded != "~":
            home_dir = expanded
        else:
            try:
                home_dir = str(Path.home())
            except Exception:
                home_dir = ""

    if not home_dir or not str(home_dir).strip():
        logger.warning(
            "Unable to determine the user's home directory via GLib; "
            "falling back to the current working directory for SSH data."
        )
        home_dir = os.getcwd()

    return _normalize_path(os.path.join(home_dir, ".ssh"))


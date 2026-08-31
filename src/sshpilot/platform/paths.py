"""Headless XDG / SSH path helpers (no GLib).

GTK and GLib-backed paths remain in ``sshpilot.platform_utils``. Core and
CLI consumers should prefer these stdlib resolvers.
"""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "sshpilot"


def get_home_dir() -> Path:
    return Path.home()


def get_ssh_dir() -> Path:
    override = os.environ.get("SSHPILOT_SSH_DIR")
    if override:
        return Path(override)
    return get_home_dir() / ".ssh"


def known_hosts_path_for(isolated: bool) -> Path:
    """The known_hosts file that belongs to one operation mode.

    Isolated Mode owns its own host keys: sshPilot created that scope, so it
    may manage the trust store inside it. Default Mode uses the user's global
    ``~/.ssh/known_hosts``, which is shared TOFU state for every SSH tool on
    the machine and is never sshPilot's to relocate or rewrite.

    Single source of truth for every consumer -- the launch path, the
    known-hosts editor, backup/restore, and the Operation Mode file list --
    so the answer cannot drift between them again.
    """
    if isolated:
        return get_config_dir() / "known_hosts"
    return get_ssh_dir() / "known_hosts"


def get_config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else get_home_dir() / ".config"
    return base / APP_NAME


def get_data_dir() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else get_home_dir() / ".local" / "share"
    return base / APP_NAME


def get_state_dir() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else get_home_dir() / ".local" / "state"
    return base / APP_NAME

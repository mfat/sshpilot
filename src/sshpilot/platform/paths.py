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

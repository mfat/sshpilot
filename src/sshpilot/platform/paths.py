"""Headless XDG / SSH path helpers (no GLib).

GTK and GLib-backed paths remain in ``sshpilot.platform_utils``. Core and
CLI consumers should prefer these stdlib resolvers.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

APP_NAME = "sshpilot"
_SANDBOX_OVERFLOW_UID = 65534


def _windows_base(env_var: str, fallback: str) -> Path:
    """``%APPDATA%`` / ``%LOCALAPPDATA%`` with a profile-relative fallback."""
    value = os.environ.get(env_var)
    return Path(value) if value else get_home_dir() / fallback


def get_home_dir() -> Path:
    return Path.home()


def get_ssh_dir() -> Path:
    override = os.environ.get("SSHPILOT_SSH_DIR")
    if override:
        return Path(override)
    return get_home_dir() / ".ssh"


def openssh_user_dir() -> Path:
    """``~/.ssh`` as OpenSSH itself finds it.

    OpenSSH takes the home directory from the password database, never from
    ``$HOME`` (Windows: the user profile). This is where ``ssh`` reads its
    per-user ``config`` when no ``-F`` is given, and where it looks for a
    relative ``Include`` of any user configuration file -- even one named
    with ``-F`` somewhere else.
    """
    if os.name != "nt":
        try:
            import pwd

            home = pwd.getpwuid(os.getuid()).pw_dir
            if home:
                return Path(home) / ".ssh"
        except (ImportError, KeyError, OSError):
            pass
    return get_home_dir() / ".ssh"


def direct_f_workaround_for_unmapped_user_ssh_config(config_path: str) -> bool:
    """Whether to use direct ``-F`` on ``realpath`` for #1384 / Flatpak #6982.

    Only when ``stat()`` shows the sandbox overflow UID, the mode is not
    group/world-writable, the process runs inside Flatpak, and the resolved
    path lies under ``/nix/store/``.  Any other unexpected owner keeps plain
    ``ssh host`` so OpenSSH's own rejection still applies.
    """
    from .detect import is_flatpak

    try:
        st = os.stat(config_path)
    except OSError:
        return False
    if st.st_uid != _SANDBOX_OVERFLOW_UID:
        return False
    if stat.S_IMODE(st.st_mode) & (stat.S_IWGRP | stat.S_IWOTH):
        return False
    resolved = os.path.realpath(config_path)
    return is_flatpak() and resolved.startswith("/nix/store/")


def is_openssh_user_config(path) -> bool:
    """Whether *path* is the file plain ``ssh host`` reads (``~/.ssh/config``)."""
    if not path:
        return False
    try:
        candidate = os.path.abspath(os.path.expanduser(str(path)))
        default = os.path.abspath(str(openssh_user_dir() / "config"))
    except (OSError, ValueError):
        return False
    return os.path.normcase(candidate) == os.path.normcase(default)


def include_base_dir(root_path, including_file) -> str:
    """Directory a relative ``Include`` in *including_file* is resolved in.

    OpenSSH resolves it in ``~/.ssh``, whichever file holds the line. That is
    applied when *root_path* is the user's own ``~/.ssh/config``, so the app
    discovers exactly the files ``ssh`` reads.

    A root anywhere else (``SSHPILOT_SSH_DIR``, Isolated Mode) is a
    configuration ``ssh`` has no notion of: the app treats the root's folder
    as the SSH folder and resolves next to the including file, as it always
    has. :func:`sshpilot.core.ssh_config_effective.openssh_reachable_files`
    tells which of those files ``ssh -F <root>`` can really reach.
    """
    if is_openssh_user_config(root_path):
        return str(openssh_user_dir())
    return os.path.dirname(os.path.abspath(str(including_file)))


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
    if xdg:
        return Path(xdg) / APP_NAME
    if sys.platform == "win32":
        return _windows_base("APPDATA", "AppData/Roaming") / APP_NAME
    return get_home_dir() / ".config" / APP_NAME


def get_data_dir() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / APP_NAME
    if sys.platform == "win32":
        return _windows_base("LOCALAPPDATA", "AppData/Local") / APP_NAME
    return get_home_dir() / ".local" / "share" / APP_NAME


def get_state_dir() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / APP_NAME
    if sys.platform == "win32":
        return _windows_base("LOCALAPPDATA", "AppData/Local") / APP_NAME / "state"
    return get_home_dir() / ".local" / "state" / APP_NAME


def get_runtime_dir() -> Path:
    """Per-user directory for the daemon endpoint file on Windows.

    POSIX keeps using ``XDG_RUNTIME_DIR`` via ``daemon.lifecycle``; this is
    only consulted where Unix-domain sockets are unavailable.
    """
    return _windows_base("LOCALAPPDATA", "AppData/Local") / APP_NAME / "run"

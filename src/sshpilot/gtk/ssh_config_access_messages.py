"""Frontend-owned wording for an SSH config the daemon cannot read or write.

The daemon marks such connections (``ConnectionSummary.config_read_only``,
``ConnectionStoreSnapshot.ssh_config_read_only``) and rejects a write with
``ErrorCode.SSH_CONFIG_READ_ONLY``; it never sends a sentence. Every place
the GUI explains it -- the connection editor banner, the delete prompt, a
save that fails anyway, the raw config editor -- uses the text here, so a
NixOS Home Manager user reads one consistent explanation.
"""

from __future__ import annotations

import os
import shlex
from gettext import gettext as _
from pathlib import Path

from ..api.errors import ErrorCode
from ..api.models.connection_store import SshConfigUnavailableReason
from ..i18n import N_

_HEADING = N_("SSH config is read-only")
_BODY = N_(
    "SSH Pilot can't save changes to this SSH config file. It is probably "
    "managed by another tool, such as Home Manager, so make the change "
    "there instead."
)


_UNAVAILABLE_HEADING = N_("SSH config can't be read")
_UNAVAILABLE_BODY = N_(
    "SSH Pilot can't read your SSH config, so it can't save changes. The "
    "notice at the top of the window says why."
)


def read_only_heading(unavailable: bool = False) -> str:
    return _(_UNAVAILABLE_HEADING if unavailable else _HEADING)


def read_only_body(unavailable: bool = False) -> str:
    return _(_UNAVAILABLE_BODY if unavailable else _BODY)


def is_read_only_error(error: object) -> bool:
    """Whether a daemon rejection means the SSH config file is read-only."""
    return getattr(error, "code", None) is ErrorCode.SSH_CONFIG_READ_ONLY


def is_config_read_only(connection: object) -> bool:
    """Whether the daemon marked this connection's SSH config read-only."""
    return getattr(connection, "config_read_only", False) is True


# -- An SSH config the daemon cannot read -----------------------------------
# ``ConnectionStoreSnapshot.ssh_config_unavailable`` names why, and
# ``ssh_config_unavailable_path`` the path at fault. The list holds no SSH
# connections then, and nothing can be saved; the window's notice says why.

_Reason = SshConfigUnavailableReason

_BANNERS = {
    (_Reason.DANGLING_LINK, True): N_(
        "Your SSH config is a link that the Flatpak sandbox can't follow, so "
        "no connections are shown."
    ),
    (_Reason.DANGLING_LINK, False): N_(
        "Your SSH config is a link to a file that doesn't exist, so no "
        "connections are shown."
    ),
    _Reason.PERMISSION_DENIED: N_(
        "SSH Pilot isn't allowed to read your SSH config, so no connections "
        "are shown."
    ),
    _Reason.SANDBOX_HIDDEN: N_(
        "The Flatpak sandbox has no access to your SSH folder, so no "
        "connections are shown."
    ),
    _Reason.UNREADABLE: N_(
        "SSH Pilot can't read a file your SSH config includes, so no "
        "connections are shown."
    ),
    _Reason.INVALID: N_(
        "Your SSH config contains an error, so no connections are shown."
    ),
}

_HEADINGS = {
    _Reason.SANDBOX_HIDDEN: N_("SSH folder not shared"),
    _Reason.INVALID: N_("SSH config has an error"),
}

_BODIES = {
    (_Reason.DANGLING_LINK, True): N_(
        "Your SSH config links to {path}, which is outside the folders the "
        "Flatpak sandbox can see.\n\nTo load your connections, run this "
        "command in a terminal and restart SSH Pilot. It gives read-only "
        "access, so your file stays managed where it is:\n\n{command}"
    ),
    (_Reason.DANGLING_LINK, False): N_(
        "Your SSH config links to {path}, which doesn't exist. Fix the link "
        "or replace it with a file, then restart SSH Pilot."
    ),
    _Reason.PERMISSION_DENIED: N_(
        "Access to {path} was denied. Check that it belongs to you and that "
        "you can open it, then restart SSH Pilot.\n\nIf the owner and "
        "permissions are already right, a security policy such as AppArmor "
        "or SELinux may be blocking it."
    ),
    "permission_denied_fix": N_(
        "Access to {path} was denied. If it belongs to another user, for "
        "example because it was created with sudo, this command gives it "
        "back to you:\n\n{command}\n\nThen restart SSH Pilot. If the owner "
        "and permissions are already right, a security policy such as "
        "AppArmor or SELinux may be blocking it."
    ),
    _Reason.SANDBOX_HIDDEN: N_(
        "SSH Pilot needs access to {path} for your SSH config, keys and known "
        "hosts, but the Flatpak permission for it has been removed.\n\nTo "
        "restore it, run this command in a terminal and restart SSH "
        "Pilot:\n\n{command}"
    ),
    _Reason.UNREADABLE: N_(
        "SSH Pilot couldn't read {path}, which your SSH config includes. "
        "Check that it belongs to you, that you can open it and that it is a "
        "text file, then restart SSH Pilot."
    ),
    _Reason.INVALID: N_(
        "SSH Pilot couldn't understand {path}. Check it for a mistake, such "
        "as a Host name that starts with \"-\", fix it, then restart SSH "
        "Pilot."
    ),
}


def _reason(value) -> "SshConfigUnavailableReason | None":
    try:
        return _Reason(value) if value else None
    except ValueError:
        return None


def _app_id() -> str:
    return os.environ.get("FLATPAK_ID") or "io.github.mfat.sshpilot"


# Mirrors the daemon log's hint in core/connections/ssh_config_store.py.
def flatpak_override_command(target) -> str:
    """The command that lets the Flatpak sandbox read a linked SSH config.

    A NixOS Home Manager link names one file deep in ``/nix/store``; sharing
    the store read-only keeps working across rebuilds, which change the
    hash. Anything else shares the directory holding the target.
    """
    app_id = _app_id()
    text = str(target)
    shared = "/nix/store" if text.startswith("/nix/store/") else str(Path(text).parent)
    return (
        f"flatpak override --user --filesystem={shlex.quote(shared + ':ro')} "
        f"{app_id}"
    )


def _share_folder_command(folder: str) -> str:
    home = os.path.expanduser("~")
    shared = "~/.ssh" if folder == os.path.join(home, ".ssh") else shlex.quote(folder)
    return f"flatpak override --user --filesystem={shared}:create {_app_id()}"


def _reclaim_command(path: str) -> str:
    """Give an SSH folder or a file in it back to the user, or "".

    Offered only for ``.ssh`` itself or a file directly in it: a refused
    home directory or system folder is not something to ``chown -R``.
    """
    target = Path(path)
    quoted = shlex.quote(path)
    if target.name == ".ssh":
        return f'sudo chown -R "$USER": {quoted} && chmod 700 {quoted}'
    if target.parent.name == ".ssh":
        return f'sudo chown "$USER": {quoted} && chmod 600 {quoted}'
    return ""


def fix_command(reason, path: str, flatpak: bool) -> str:
    """A command that fixes *reason*, for the details dialog's Copy, or ""."""
    reason = _reason(reason)
    if reason is _Reason.DANGLING_LINK and flatpak:
        return flatpak_override_command(Path(path))
    if reason is _Reason.SANDBOX_HIDDEN:
        return _share_folder_command(path)
    if reason is _Reason.PERMISSION_DENIED:
        return _reclaim_command(path)
    return ""


def unavailable_banner_title(reason, flatpak: bool) -> str:
    reason = _reason(reason)
    if reason is None:
        return ""
    text = _BANNERS.get((reason, flatpak)) or _BANNERS[reason]
    return _(text)


def unavailable_details_heading(reason) -> str:
    return _(_HEADINGS.get(_reason(reason), _UNAVAILABLE_HEADING))


def unavailable_details_body(reason, path: str, flatpak: bool) -> str:
    reason = _reason(reason)
    if reason is None:
        return ""
    command = fix_command(reason, path, flatpak)
    if reason is _Reason.PERMISSION_DENIED and command:
        text = _BODIES["permission_denied_fix"]
    else:
        text = _BODIES.get((reason, flatpak)) or _BODIES[reason]
    return _(text).format(path=path, command=command)


def details_button_label() -> str:
    return _("_Details")


def copy_command_label() -> str:
    return _("_Copy Command")

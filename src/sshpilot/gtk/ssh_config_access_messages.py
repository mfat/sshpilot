"""Frontend-owned wording for an SSH config file the daemon cannot write.

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
from ..i18n import N_

_HEADING = N_("SSH config is read-only")
_BODY = N_(
    "SSH Pilot can't save changes to this SSH config file. It is probably "
    "managed by another tool, such as Home Manager, so make the change "
    "there instead."
)


def read_only_heading() -> str:
    return _(_HEADING)


def read_only_body() -> str:
    return _(_BODY)


def is_read_only_error(error: object) -> bool:
    """Whether a daemon rejection means the SSH config file is read-only."""
    return getattr(error, "code", None) is ErrorCode.SSH_CONFIG_READ_ONLY


def is_config_read_only(connection: object) -> bool:
    """Whether the daemon marked this connection's SSH config read-only."""
    return getattr(connection, "config_read_only", False) is True


# -- A linked SSH config whose target cannot be seen ------------------------
# ``ConnectionStoreSnapshot.ssh_config_missing_link_target`` is set when the
# root config is a link to a file the daemon cannot see. Inside the Flatpak
# that is usually a NixOS Home Manager link into /nix/store, outside the
# sandbox; elsewhere the link is simply broken. The list is empty either way.

_LINK_BANNER_FLATPAK = N_(
    "Your SSH config is a link that the Flatpak sandbox can't follow, so no "
    "connections are shown."
)
_LINK_BANNER_NATIVE = N_(
    "Your SSH config is a link to a file that doesn't exist, so no "
    "connections are shown."
)
_LINK_HEADING = N_("SSH config can't be read")
_LINK_BODY_FLATPAK = N_(
    "Your SSH config links to {target}, which is outside the folders the "
    "Flatpak sandbox can see.\n\nTo load your connections, run this command "
    "in a terminal and restart SSH Pilot. It gives read-only access, so your "
    "file stays managed where it is:\n\n{command}"
)
_LINK_BODY_NATIVE = N_(
    "Your SSH config links to {target}, which doesn't exist. Fix the link "
    "or replace it with a file, then restart SSH Pilot."
)


# Mirrors the daemon log's hint in core/connections/ssh_config_store.py.
def flatpak_override_command(target) -> str:
    """The command that lets the Flatpak sandbox read a linked SSH config.

    A NixOS Home Manager link names one file deep in ``/nix/store``; sharing
    the store read-only keeps working across rebuilds, which change the
    hash. Anything else shares the directory holding the target.
    """
    app_id = os.environ.get("FLATPAK_ID") or "io.github.mfat.sshpilot"
    text = str(target)
    shared = "/nix/store" if text.startswith("/nix/store/") else str(Path(text).parent)
    return (
        f"flatpak override --user --filesystem={shlex.quote(shared + ':ro')} "
        f"{app_id}"
    )


def link_banner_title(flatpak: bool) -> str:
    return _(_LINK_BANNER_FLATPAK if flatpak else _LINK_BANNER_NATIVE)


def link_details_heading() -> str:
    return _(_LINK_HEADING)


def link_details_body(target: str, command: str, flatpak: bool) -> str:
    if flatpak:
        return _(_LINK_BODY_FLATPAK).format(target=target, command=command)
    return _(_LINK_BODY_NATIVE).format(target=target)


def details_button_label() -> str:
    return _("_Details")


def copy_command_label() -> str:
    return _("_Copy Command")

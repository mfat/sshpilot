"""Frontend-owned wording for an SSH config file the daemon cannot write.

The daemon marks such connections (``ConnectionSummary.config_read_only``,
``ConnectionStoreSnapshot.ssh_config_read_only``) and rejects a write with
``ErrorCode.SSH_CONFIG_READ_ONLY``; it never sends a sentence. Every place
the GUI explains it -- the connection editor banner, the delete prompt, a
save that fails anyway, the raw config editor -- uses the text here, so a
NixOS Home Manager user reads one consistent explanation.
"""

from __future__ import annotations

from gettext import gettext as _

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

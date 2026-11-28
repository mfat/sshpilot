"""Frontend-owned presentation of connection-sharing notices.

The daemon publishes the outcome code and the socket path (see
:mod:`sshpilot.api.models.connection_sharing`); the wording lives here.

Only outcomes where sharing was meant to happen and did not raise a toast. A
launch that opened or joined the shared connection is the normal case, and a
toast for every tab would train people to ignore the one that matters. Every
outcome is in ``daemon.log`` either way.

A connection that logged in on its own looks like it worked, so the sentence
says it did not share, and what to change where that is known.
"""

from __future__ import annotations

from gettext import gettext as _

from ..api.models.connection_sharing import (
    ConnectionSharingNotice,
    ConnectionSharingOutcome,
)
from ..i18n import N_

_TEMPLATES = {
    #: The ``ControlMaster yes`` case: it only ever opens a shared connection,
    #: so with one already open every launch logs in again.
    ConnectionSharingOutcome.REFUSED: N_(
        "“{name}” logged in again instead of reusing its shared connection. "
        "Set ControlMaster to auto."
    ),
    #: The socket path is in daemon.log; a toast has no room for it.
    ConnectionSharingOutcome.FAILED: N_(
        "“{name}” could not create its shared connection's socket."
    ),
}


def sharing_needs_attention(notice: ConnectionSharingNotice) -> bool:
    """Whether *notice* is worth interrupting the user for."""
    return notice.outcome in _TEMPLATES


def format_sharing_problem(
    notice: ConnectionSharingNotice,
    *,
    display_name: str = "",
) -> str:
    """Translate one connection-sharing problem for a toast."""

    if not sharing_needs_attention(notice):
        raise ValueError("connection sharing notice is not a problem")
    name = display_name.strip() or str(notice.connection_id)
    return _(_TEMPLATES[notice.outcome]).format(name=name)

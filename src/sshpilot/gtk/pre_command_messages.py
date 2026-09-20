"""Frontend-owned presentation of pre-connection command notices.

The daemon publishes codes and numbers only (see
:mod:`sshpilot.api.models.pre_command`); the wording lives here, so it can be
translated and so the command text and its output -- which may carry a token or
a password -- never have to cross the wire to be rendered.

Every failure sentence ends by saying the connection continues. That is not
padding: the step deliberately never vetoes a launch, and a user who reads
"the pre-connection command failed" without that clause will reasonably assume
the connection was abandoned and stop waiting for it.
"""

from __future__ import annotations

from gettext import gettext as _

from ..api.models.pre_command import (
    PreCommandPhase,
    PreCommandReason,
    PreConnectionCommandNotice,
)
from ..i18n import N_


#: Shown while the command is still running, so a slow knock or VPN dial-up is
#: not a silent multi-second hang on connect.
PRE_COMMAND_RUNNING = N_("Running pre-connection command…")

_PRE_COMMAND_FAILURE_TEMPLATES = {
    PreCommandReason.NONZERO_EXIT: N_(
        "The pre-connection command for “{name}” failed (exit {code}). "
        "Connecting anyway."
    ),
    PreCommandReason.TIMED_OUT: N_(
        "The pre-connection command for “{name}” timed out. Connecting anyway."
    ),
    PreCommandReason.START_FAILED: N_(
        "The pre-connection command for “{name}” could not be started. "
        "Connecting anyway."
    ),
}


def pre_command_is_failure(notice: PreConnectionCommandNotice) -> bool:
    """Whether *notice* is a finished attempt the user should hear about.

    ``OK`` and ``COALESCED`` are finished too; neither is worth interrupting
    anyone for.
    """

    if type(notice) is not PreConnectionCommandNotice:
        raise ValueError("invalid pre-connection command notice")
    return (
        notice.phase is PreCommandPhase.FINISHED
        and notice.reason in _PRE_COMMAND_FAILURE_TEMPLATES
    )


def format_pre_command_failure(
    notice: PreConnectionCommandNotice,
    *,
    display_name: str = "",
) -> str:
    """Translate one failed pre-connection command notice.

    *display_name* is the connection's nickname when the caller has it; the
    connection id is a poor substitute but better than an unnamed host, and a
    toast with no subject at all is close to useless.
    """

    if not pre_command_is_failure(notice):
        raise ValueError("pre-connection command notice is not a failure")
    template = _PRE_COMMAND_FAILURE_TEMPLATES[notice.reason]
    name = display_name.strip() or str(notice.connection_id)
    return _(template).format(
        name=name,
        code=notice.exit_code if notice.exit_code is not None else "?",
    )


def format_pre_command_running() -> str:
    return _(PRE_COMMAND_RUNNING)


__all__ = [
    "PRE_COMMAND_RUNNING",
    "format_pre_command_failure",
    "format_pre_command_running",
    "pre_command_is_failure",
]

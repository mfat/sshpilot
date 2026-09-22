"""Frontend presentation of daemon session failures."""

from __future__ import annotations

from gettext import gettext as _

from ..api.models.sessions import SessionFailure, SessionFailureCode
from ..i18n import N_


_SESSION_FAILURE_TEMPLATES = {
    SessionFailureCode.START_FAILED: N_("The session process could not be started"),
    SessionFailureCode.AUTH_CANCELLED: N_(
        "The session authentication was cancelled"
    ),
    SessionFailureCode.AUTH_INCOMPLETE: N_(
        "The session did not complete authentication"
    ),
    SessionFailureCode.COMMAND_QUEUE_FULL: N_(
        "The daemon session command queue is full"
    ),
    SessionFailureCode.TERMINATION_FAILED: N_(
        "The session process could not be terminated"
    ),
    SessionFailureCode.ENDED_BEFORE_OUTPUT: N_(
        "The session ended before it produced any output"
    ),
    SessionFailureCode.SSH_EXITED: N_(
        "The SSH session exited with status {status}"
    ),
    SessionFailureCode.SSH_DIAGNOSTIC: N_("The SSH session failed."),
}


def format_session_failure(
    failure: SessionFailure, *, include_diagnostic: bool = False
) -> str:
    """Translate the reason, then optionally append the diagnostic unchanged."""

    if type(failure) is not SessionFailure:
        raise ValueError("invalid session failure")
    try:
        template = _SESSION_FAILURE_TEMPLATES[failure.code]
    except KeyError:
        raise ValueError("session failure code has no frontend presentation") from None
    reason = _(template).format(**failure.parameters)
    if include_diagnostic and failure.diagnostic:
        return f"{reason}\n\n{failure.diagnostic}"
    return reason


__all__ = ["format_session_failure"]

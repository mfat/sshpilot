"""Frontend presentation of private built-in protocol suggestion failures."""

from __future__ import annotations

from gettext import gettext as _

from ..i18n import N_
from ..plugins._command_failure import LocalCommandFailure, LocalCommandFailureReason


_REASON_TEMPLATES = {
    LocalCommandFailureReason.EMPTY_COMMAND: N_("No command was provided."),
    LocalCommandFailureReason.HOST_EXECUTOR_UNAVAILABLE: N_(
        "The '{program}' program is unavailable. Cannot run commands on the host."
    ),
    LocalCommandFailureReason.TIMED_OUT: N_("The command timed out."),
    LocalCommandFailureReason.START_FAILED: N_("The command could not be started."),
    LocalCommandFailureReason.EXITED: N_(
        "The '{program}' command exited with status {status}."
    ),
}


def format_protocol_suggestion_error(error: BaseException) -> str:
    """Translate known reasons; never translate plugin/tool diagnostics."""
    if isinstance(error, LocalCommandFailure):
        message = _(_REASON_TEMPLATES[error.reason]).format(
            program=error.program, status=error.status,
        )
        return f"{message}\n\n{error.diagnostic}" if error.diagnostic else message
    # Third-party suggestions still use the public contract. Their unknown
    # exception text is opaque, as it was before this internal correction.
    diagnostic = str(error) or type(error).__name__
    return _("Could not list suggestions: {error}").format(error=diagnostic)

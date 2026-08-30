"""Frontend-owned presentation of structured native SCP failures."""

from __future__ import annotations

from gettext import gettext as _

from ..api.models.operations import ScpFailure, ScpFailureCode
from ..i18n import N_


_SCP_FAILURE_TEMPLATES = {
    ScpFailureCode.UNAVAILABLE: N_(
        "Native SCP transfers are unavailable on the daemon."
    ),
    ScpFailureCode.COMMAND_QUEUE_FULL: N_(
        "The daemon transfer command queue is full."
    ),
    ScpFailureCode.CONNECTION_NOT_FOUND: N_(
        "The selected connection no longer exists in the daemon."
    ),
    ScpFailureCode.SSH_CONNECTION_REQUIRED: N_(
        "Native SCP transfers require an SSH connection."
    ),
    ScpFailureCode.HOST_IDENTIFIER_MISSING: N_(
        "The connection has no SSH host identifier."
    ),
    ScpFailureCode.TARGET_PREPARATION_FAILED: N_(
        "The native SCP target could not be prepared."
    ),
    ScpFailureCode.TRANSFER_PREPARATION_FAILED: N_(
        "The native SCP transfer could not be prepared."
    ),
    ScpFailureCode.PROCESS_START_FAILED: N_(
        "The SCP process could not be started."
    ),
    ScpFailureCode.REMOTE_SFTP_UNAVAILABLE: N_(
        "Could not start an SFTP session on the remote host. The remote SSH server "
        "may not have an SFTP server enabled."
    ),
    ScpFailureCode.TRANSFER_START_FAILED: N_(
        "The transfer could not be started"
    ),
    ScpFailureCode.TRANSFER_FAILED: N_("The SCP transfer failed."),
    ScpFailureCode.DAEMON_SHUTTING_DOWN: N_("The daemon is shutting down"),
    ScpFailureCode.UNEXPECTED_FAILURE: N_("The transfer failed unexpectedly."),
}


def format_scp_failure(failure: ScpFailure) -> str:
    """Translate one native SCP failure and append its diagnostic unchanged."""

    if type(failure) is not ScpFailure:
        raise ValueError("invalid SCP failure")
    try:
        template = _SCP_FAILURE_TEMPLATES[failure.code]
    except KeyError:
        raise ValueError("SCP failure code has no frontend presentation") from None
    message = _(template).format(**failure.parameters)
    return f"{message}\n\n{failure.diagnostic}" if failure.diagnostic else message

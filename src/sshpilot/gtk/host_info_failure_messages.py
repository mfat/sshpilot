"""Frontend-owned presentation of Host Info failures and RPC errors."""

from __future__ import annotations

from gettext import gettext as _

from ..api.errors import ErrorCode, SshPilotError
from ..api.models.host_info import HostInfoFailure, HostInfoFailureCode
from ..i18n import N_


_FAILURE_TEMPLATES = {
    HostInfoFailureCode.TIMED_OUT: N_("The host information probe timed out."),
    HostInfoFailureCode.REMOTE_COMMAND_FAILED: N_(
        "The host information command exited with status {exit_code}."
    ),
    HostInfoFailureCode.START_FAILED: N_(
        "The host information probe could not be started."
    ),
    HostInfoFailureCode.UNREADABLE_INFORMATION: N_(
        "The remote host returned unreadable system information."
    ),
    HostInfoFailureCode.PROBE_FAILED: N_("The host information probe failed."),
    HostInfoFailureCode.CANCELLED: N_("The host information probe was cancelled."),
}

_RPC_ERROR_TEMPLATES = {
    ErrorCode.OPERATION_NOT_FOUND: N_(
        "The requested host information probe does not exist."
    ),
    ErrorCode.CONNECTION_NOT_FOUND: N_(
        "The selected connection no longer exists in the daemon."
    ),
    ErrorCode.DAEMON_UNAVAILABLE: N_("Daemon connection unavailable."),
    ErrorCode.TRANSPORT_CLOSED: N_("Daemon connection unavailable."),
    ErrorCode.API_VERSION_MISMATCH: N_(
        "The daemon API version does not match this application."
    ),
    ErrorCode.UNSUPPORTED_CAPABILITY: N_(
        "Host information is unavailable on this daemon."
    ),
}


def format_host_info_failure(failure: HostInfoFailure) -> str:
    """Translate a stable reason, then format and append any raw diagnostic."""

    if type(failure) is not HostInfoFailure:
        raise ValueError("invalid Host Info failure")
    try:
        template = _FAILURE_TEMPLATES[failure.code]
    except KeyError:
        raise ValueError("Host Info failure code has no presentation") from None
    message = _(template).format(**failure.parameters)
    return f"{message}\n\n{failure.diagnostic}" if failure.diagnostic else message


def format_host_info_error(error: BaseException) -> str:
    """Use a stable RPC code where available; keep other details opaque."""

    if isinstance(error, SshPilotError):
        template = _RPC_ERROR_TEMPLATES.get(error.code)
        if template is not None:
            return _(template)
    message = _("Could not gather host information.")
    diagnostic = str(error).strip()
    return f"{message}\n\n{diagnostic}" if diagnostic else message

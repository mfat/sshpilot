"""Frontend-owned presentation of structured host-information failures."""

from __future__ import annotations

from gettext import gettext as _

from ..api.errors import ErrorCode, SshPilotError
from ..api.models.host_info import HostInfoFailure, HostInfoFailureCode
from ..i18n import N_


_HOST_INFO_FAILURE_TEMPLATES = {
    HostInfoFailureCode.PROBE_FAILED: N_("The host information probe failed"),
    HostInfoFailureCode.PROBE_TIMED_OUT: N_(
        "The host information probe timed out"
    ),
    HostInfoFailureCode.PROBE_START_FAILED: N_(
        "The host information probe could not be started"
    ),
    HostInfoFailureCode.CONNECTION_NOT_FOUND: N_(
        "The selected connection no longer exists in the daemon."
    ),
    HostInfoFailureCode.SSH_CONNECTION_REQUIRED: N_(
        "Host information requires an SSH connection."
    ),
    HostInfoFailureCode.UNREADABLE_SYSTEM_INFORMATION: N_(
        "The remote host returned unreadable system information"
    ),
}

_HOST_INFO_ERROR_TEMPLATES = {
    ErrorCode.UNSUPPORTED_CAPABILITY: N_("Host information is unavailable."),
    ErrorCode.CONNECTION_NOT_FOUND: N_(
        "The selected connection no longer exists in the daemon."
    ),
    ErrorCode.OPERATION_NOT_FOUND: N_(
        "The host information result is no longer available."
    ),
    ErrorCode.SERVER_BUSY: N_("The daemon is busy. Try again."),
    ErrorCode.DAEMON_UNAVAILABLE: N_("Daemon connection unavailable."),
    ErrorCode.DAEMON_SHUTTING_DOWN: N_("Daemon connection unavailable."),
    ErrorCode.TRANSPORT_CLOSED: N_("Daemon connection unavailable."),
    ErrorCode.TRANSPORT_TIMEOUT: N_("Daemon connection unavailable."),
}

_UNEXPECTED_HOST_INFO_ERROR = N_("The host information request failed.")


def _render(template: str, parameters, diagnostic: str) -> str:
    message = _(template).format(**parameters)
    return f"{message}\n\n{diagnostic}" if diagnostic else message


def format_host_info_failure(failure: HostInfoFailure) -> str:
    """Translate one summary failure and append its diagnostic unchanged."""

    if type(failure) is not HostInfoFailure:
        raise ValueError("invalid host info failure")
    try:
        template = _HOST_INFO_FAILURE_TEMPLATES[failure.code]
    except KeyError:
        raise ValueError(
            "host info failure code has no frontend presentation"
        ) from None
    return _render(template, failure.parameters, failure.diagnostic)


def format_host_info_error(error: BaseException) -> str:
    """Translate RPC failures; retain only unexpected text as a diagnostic."""

    if isinstance(error, SshPilotError):
        template = _HOST_INFO_ERROR_TEMPLATES.get(error.code)
        if template is not None:
            return _render(template, {}, "")
        return _render(_UNEXPECTED_HOST_INFO_ERROR, {}, str(error))
    return _render(_UNEXPECTED_HOST_INFO_ERROR, {}, str(error))


__all__ = ["format_host_info_error", "format_host_info_failure"]

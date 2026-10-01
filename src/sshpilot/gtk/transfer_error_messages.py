"""Frontend presentation of file-transfer start errors; no message parsing."""

from __future__ import annotations

from gettext import gettext as _

from ..api.errors import ErrorCode, SshPilotError
from ..i18n import N_
from .sftp_error_messages import format_direct_sftp_error, has_structured_sftp_failure


_TRANSFER_ERROR_TEMPLATES = {
    ErrorCode.INVALID_REQUEST: N_("The transfer could not be started"),
    ErrorCode.INTERNAL_ERROR: N_("The transfer could not be started"),
    ErrorCode.SERVER_BUSY: N_("The daemon transfer command queue is full"),
    ErrorCode.DAEMON_SHUTTING_DOWN: N_("The daemon is shutting down"),
    ErrorCode.DAEMON_UNAVAILABLE: N_("The daemon transfer service is unavailable."),
    ErrorCode.TRANSPORT_CLOSED: N_("The daemon connection was closed."),
    ErrorCode.TRANSPORT_TIMEOUT: N_("The daemon transfer request timed out."),
    ErrorCode.PROTOCOL_ERROR: N_("The transfer could not be started"),
    ErrorCode.API_VERSION_MISMATCH: N_(
        "The daemon API version does not match this application."
    ),
    ErrorCode.CONNECTION_NOT_FOUND: N_(
        "The selected connection no longer exists in the daemon."
    ),
    ErrorCode.MUTATION_AMBIGUOUS: N_(
        "The transfer may have started. Refresh before trying again."
    ),
}


def format_transfer_start_error(error: BaseException) -> str:
    """Select by code and details; keep server/unknown diagnostics opaque."""
    if isinstance(error, SshPilotError):
        if has_structured_sftp_failure(error):
            return format_direct_sftp_error(error)
        if error.code is ErrorCode.UNSUPPORTED_CAPABILITY:
            capability = error.details.get("capability")
            if type(capability) is str and capability:
                return _("The daemon does not support {capability}").format(
                    capability=capability
                )
            return _("The transfer could not be started")
        template = _TRANSFER_ERROR_TEMPLATES.get(error.code)
        if template is not None:
            return _(template)
    return format_direct_sftp_error(error)

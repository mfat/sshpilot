"""Frontend-owned presentation of direct SFTP RPC errors."""

from __future__ import annotations

from gettext import gettext as _

from ..api.errors import ErrorCode, SshPilotError
from ..i18n import N_


_DIRECT_SFTP_ERROR_TEMPLATES = {
    ErrorCode.REMOTE_PATH_NOT_FOUND: N_("The path was not found"),
    ErrorCode.REMOTE_PERMISSION_DENIED: N_("Permission denied"),
    ErrorCode.SFTP_PROTOCOL_LOST: N_("The SFTP connection was lost"),
    ErrorCode.REMOTE_UNSUPPORTED_OPERATION: N_(
        "The server does not support this operation"
    ),
    ErrorCode.SFTP_COMMAND_FAILED: N_("The SFTP command failed"),
    ErrorCode.SFTP_PROTOCOL_ERROR: N_("The SFTP command failed"),
}


def format_direct_sftp_error(error: BaseException) -> str:
    """Translate a direct SFTP error and append a specific server diagnostic."""

    if not isinstance(error, SshPilotError):
        return str(error)
    template = _DIRECT_SFTP_ERROR_TEMPLATES.get(error.code)
    if template is None:
        return str(error)

    message = _(template)
    details = error.details
    is_specific = details.get("server_message_is_specific", False)
    if type(is_specific) is not bool:
        raise ValueError("SFTP server diagnostic classification is invalid")
    if not is_specific:
        return message

    diagnostic = details.get("server_message")
    if type(diagnostic) is not str or not diagnostic:
        raise ValueError("specific SFTP server diagnostic is missing")
    return f"{message}\n\n{diagnostic}"

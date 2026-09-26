"""Frontend-owned presentation of direct SFTP RPC errors."""

from __future__ import annotations

from gettext import gettext as _

from ..api.errors import ErrorCode, SshPilotError
from ..api.models.operations import SFTP_FAILURE_CODE_DETAIL, SftpFailure, SftpFailureCode
from ..i18n import N_
from .sftp_failure_messages import format_sftp_failure


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
    structured = _structured_failure(error)
    if structured is not None:
        return format_sftp_failure(structured)
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


def has_structured_sftp_failure(error: BaseException) -> bool:
    """True when a direct SFTP error names a translatable ``SftpFailureCode``."""

    return isinstance(error, SshPilotError) and _structured_failure(error) is not None


def _structured_failure(error: SshPilotError) -> SftpFailure | None:
    """The parameterless SFTP failure a direct error names in its details, if any."""

    value = error.details.get(SFTP_FAILURE_CODE_DETAIL)
    if type(value) is not str:
        return None
    try:
        return SftpFailure(code=SftpFailureCode(value), error_code=error.code)
    except (TypeError, ValueError):
        # Unknown code (newer daemon) or one that needs parameters the
        # details do not carry: fall back to the generic presentation.
        return None

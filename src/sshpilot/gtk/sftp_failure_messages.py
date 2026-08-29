"""Frontend-owned presentation of structured SFTP summary failures."""

from __future__ import annotations

from gettext import gettext as _
from typing import Mapping

from ..api.models.operations import SftpFailure, SftpFailureCode
from ..i18n import N_


_SFTP_FAILURE_TEMPLATES = {
    SftpFailureCode.SESSION_ESTABLISHMENT_FAILED: N_(
        "The SFTP session could not be established"
    ),
    SftpFailureCode.CONNECTION_LOST: N_("The SFTP connection was lost"),
    SftpFailureCode.COMMAND_QUEUE_FULL: N_(
        "The daemon SFTP command queue is full"
    ),
    SftpFailureCode.PROCESS_TERMINATION_FAILED: N_(
        "The SFTP process could not be terminated"
    ),
    SftpFailureCode.SERVICE_NOT_FOUND: N_("The SFTP service was not found"),
    SftpFailureCode.SERVICE_NOT_READY: N_("The SFTP service is not ready"),
    SftpFailureCode.SERVICE_OWNER_REQUIRED: N_(
        "Only the originating client may mutate this SFTP service"
    ),
    SftpFailureCode.PATH_NOT_FOUND: N_("The path was not found"),
    SftpFailureCode.PERMISSION_DENIED: N_("Permission denied"),
    SftpFailureCode.UNSUPPORTED_OPERATION: N_(
        "The server does not support this operation"
    ),
    SftpFailureCode.COMMAND_FAILED: N_("The SFTP command failed"),
    SftpFailureCode.DIRECTORY_SIZE_REQUIRES_REAL_DIRECTORY: N_(
        "Directory size requires a real directory path, not a symbolic link"
    ),
    SftpFailureCode.DIRECTORY_CANNOT_BE_COPIED_INTO_ITSELF: N_(
        "A directory cannot be copied into itself"
    ),
    SftpFailureCode.RECURSIVE_COPY_SYMLINK_UNSUPPORTED: N_(
        "Recursive copy of a symbolic link is not supported"
    ),
    SftpFailureCode.DESTINATION_ALREADY_EXISTS: N_(
        "The destination already exists"
    ),
    SftpFailureCode.RECURSIVE_COPY_REQUIRED: N_(
        "Recursive copy is required for directories"
    ),
    SftpFailureCode.RECURSIVE_COPY_REQUIRES_DIRECTORY_SOURCE: N_(
        "Recursive copy requires a directory source"
    ),
    SftpFailureCode.OPERATION_FAILED_UNEXPECTEDLY: N_(
        "The operation failed unexpectedly"
    ),
    SftpFailureCode.TRANSFER_START_FAILED: N_(
        "The transfer could not be started"
    ),
    SftpFailureCode.TRANSFER_QUEUE_FULL: N_(
        "The daemon transfer command queue is full"
    ),
    SftpFailureCode.TRANSFER_FAILED: N_("The transfer failed"),
    SftpFailureCode.LOCAL_SOURCE_FILE_NOT_FOUND: N_(
        "The local source file was not found"
    ),
    SftpFailureCode.LOCAL_SOURCE_DIRECTORY_NOT_FOUND: N_(
        "The local source directory was not found"
    ),
    SftpFailureCode.RECURSIVE_UPLOAD_SYMLINK_UNSUPPORTED: N_(
        "Recursive upload requires a real directory path, not a symbolic link"
    ),
    SftpFailureCode.LOCAL_SOURCE_NOT_DIRECTORY: N_(
        "The local source is not a directory"
    ),
    SftpFailureCode.REMOTE_SOURCE_DIRECTORY_UNREADABLE: N_(
        "The remote source directory could not be read"
    ),
    SftpFailureCode.RECURSIVE_DOWNLOAD_SYMLINK_UNSUPPORTED: N_(
        "Recursive download requires a real directory path, not a symbolic link"
    ),
    SftpFailureCode.REMOTE_SOURCE_NOT_DIRECTORY: N_(
        "The remote source is not a directory"
    ),
    SftpFailureCode.LOCAL_DESTINATION_DIRECTORY_CREATION_FAILED: N_(
        "The local destination could not be created as a directory"
    ),
    SftpFailureCode.REMOTE_FILE_BLOCKS_DIRECTORY: N_(
        "A remote file is in the way of a directory: {remote_dir}"
    ),
    SftpFailureCode.REMOTE_DIRECTORY_CREATION_FAILED: N_(
        "The remote directory could not be created: {remote_dir}"
    ),
    SftpFailureCode.LOCAL_DESTINATION_EXISTS: N_(
        "The local destination already exists: {path}"
    ),
    SftpFailureCode.REMOTE_DESTINATION_EXISTS: N_(
        "The remote destination already exists: {path}"
    ),
    SftpFailureCode.NO_FREE_LOCAL_FILENAME: N_(
        "No free local filename could be found"
    ),
    SftpFailureCode.NO_FREE_REMOTE_FILENAME: N_(
        "No free remote filename could be found"
    ),
    SftpFailureCode.DAEMON_SHUTTING_DOWN: N_("The daemon is shutting down"),
}


def format_sftp_failure(failure: SftpFailure) -> str:
    """Translate one SFTP failure and append its diagnostic unchanged."""

    if type(failure) is not SftpFailure:
        raise ValueError("invalid SFTP failure")
    try:
        template = _SFTP_FAILURE_TEMPLATES[failure.code]
    except KeyError:
        raise ValueError("SFTP failure code has no frontend presentation") from None
    parameters: Mapping[str, str] = failure.parameters
    message = _(template).format(**parameters)
    return f"{message}\n\n{failure.diagnostic}" if failure.diagnostic else message

"""Stable, structured errors exposed by :mod:`sshpilot.api`."""

from enum import Enum
from typing import Any, Mapping, Optional

from ._safe_values import copy_safe_details
from .models.common import ConnectionId, RequestId, SessionId

CapabilityLike = Any


class ErrorCode(str, Enum):
    """Machine-readable protocol error codes."""

    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    API_VERSION_MISMATCH = "api_version_mismatch"
    INVALID_REQUEST = "invalid_request"
    VALIDATION_FAILED = "validation_failed"
    CONNECTION_ALREADY_EXISTS = "connection_already_exists"
    CONNECTION_NOT_FOUND = "connection_not_found"
    PERSISTENCE_FAILED = "persistence_failed"
    MUTATION_AMBIGUOUS = "mutation_ambiguous"
    SESSION_NOT_FOUND = "session_not_found"
    SESSION_ALREADY_CLOSED = "session_already_closed"
    SESSION_INVALID_STATE = "session_invalid_state"
    SESSION_STARTUP_FAILED = "session_startup_failed"
    SESSION_TERMINATION_FAILED = "session_termination_failed"
    UNSUPPORTED_SESSION_PROTOCOL = "unsupported_session_protocol"
    TERMINAL_ATTACHMENT_REQUIRED = "terminal_attachment_required"
    TERMINAL_INPUT_OWNER_REQUIRED = "terminal_input_owner_required"
    TERMINAL_INPUT_OWNER_EXISTS = "terminal_input_owner_exists"
    TERMINAL_INPUT_BACKPRESSURE = "terminal_input_backpressure"
    TERMINAL_INVALID_DIMENSIONS = "terminal_invalid_dimensions"
    TERMINAL_UNAVAILABLE = "terminal_unavailable"
    TERMINAL_REPLAY_UNAVAILABLE = "terminal_replay_unavailable"
    TERMINAL_SEQUENCE_OUT_OF_RANGE = "terminal_sequence_out_of_range"
    TERMINAL_CONTINUITY_LOST = "terminal_continuity_lost"
    PTY_ALLOCATION_FAILED = "pty_allocation_failed"
    SERVER_BUSY = "server_busy"
    INTERACTION_NOT_FOUND = "interaction_not_found"
    INTERACTION_EXPIRED = "interaction_expired"
    INTERACTION_ALREADY_ANSWERED = "interaction_already_answered"
    INTERACTION_CLAIM_CONFLICT = "interaction_claim_conflict"
    INTERACTION_RESPONDER_UNAUTHORIZED = "interaction_responder_unauthorized"
    INTERACTION_SECRET_EXPECTED = "interaction_secret_expected"
    INTERACTION_SECRET_DUPLICATE = "interaction_secret_duplicate"
    INTERACTION_TYPE_UNSUPPORTED = "interaction_type_unsupported"
    PROMPT_CLASSIFICATION_FAILED = "prompt_classification_failed"
    ASKPASS_HELPER_UNAVAILABLE = "askpass_helper_unavailable"
    SECRET_BACKEND_UNAVAILABLE = "secret_backend_unavailable"
    SECRET_STORAGE_FAILED = "secret_storage_failed"
    HOST_KEY_PERSISTENCE_FAILED = "host_key_persistence_failed"
    AUTHENTICATION_ATTEMPTS_EXHAUSTED = "authentication_attempts_exhausted"
    PERMISSION_DENIED = "permission_denied"
    OPERATION_CANCELLED = "operation_cancelled"
    OPERATION_TIMED_OUT = "operation_timed_out"
    OPERATION_NOT_FOUND = "operation_not_found"
    REMOTE_COMMAND_FAILED = "remote_command_failed"
    SFTP_SERVICE_NOT_FOUND = "sftp_service_not_found"
    SFTP_SERVICE_NOT_READY = "sftp_service_not_ready"
    SFTP_COMMAND_FAILED = "sftp_command_failed"
    SFTP_PROTOCOL_LOST = "sftp_protocol_lost"
    SFTP_PROTOCOL_ERROR = "sftp_protocol_error"
    REMOTE_PATH_NOT_FOUND = "remote_path_not_found"
    REMOTE_PATH_EXISTS = "remote_path_exists"
    REMOTE_PERMISSION_DENIED = "remote_permission_denied"
    REMOTE_NOT_DIRECTORY = "remote_not_directory"
    REMOTE_IS_DIRECTORY = "remote_is_directory"
    REMOTE_DIRECTORY_NOT_EMPTY = "remote_directory_not_empty"
    REMOTE_UNSUPPORTED_OPERATION = "remote_unsupported_operation"
    FILE_CONTENT_TOO_LARGE = "file_content_too_large"
    FILE_REVISION_CONFLICT = "file_revision_conflict"
    FILE_REPLACEMENT_FAILED = "file_replacement_failed"
    FILE_BACKUP_FAILED = "file_backup_failed"
    TRANSFER_NOT_FOUND = "transfer_not_found"
    TRANSFER_CONFLICT = "transfer_conflict"
    TRANSFER_CANCELLED = "transfer_cancelled"
    TRANSFER_IO_FAILED = "transfer_io_failed"
    TRANSFER_DISK_FULL = "transfer_disk_full"
    FORWARD_NOT_FOUND = "forward_not_found"
    FORWARD_BIND_FAILED = "forward_bind_failed"
    FORWARD_DESTINATION_INVALID = "forward_destination_invalid"
    FORWARD_STARTUP_FAILED = "forward_startup_failed"
    FORWARD_NOT_ACTIVE = "forward_not_active"
    SERVICE_OWNER_REQUIRED = "service_owner_required"
    INTERNAL_ERROR = "internal_error"
    DAEMON_UNAVAILABLE = "daemon_unavailable"
    STALE_EDITOR = "stale_editor"
    KEY_NOT_FOUND = "key_not_found"
    KEY_ALREADY_EXISTS = "key_already_exists"
    KEY_PUBLIC_UNAVAILABLE = "key_public_unavailable"
    KEY_GENERATION_FAILED = "key_generation_failed"
    KEY_VERIFICATION_FAILED = "key_verification_failed"
    TRANSPORT_CLOSED = "transport_closed"
    TRANSPORT_TIMEOUT = "transport_timeout"
    FRAME_TOO_LARGE = "frame_too_large"
    INVALID_FRAME = "invalid_frame"
    HANDSHAKE_REQUIRED = "handshake_required"
    HANDSHAKE_ALREADY_COMPLETED = "handshake_already_completed"
    PROTOCOL_VERSION_UNSUPPORTED = "protocol_version_unsupported"
    PROTOCOL_ERROR = "protocol_error"
    UNSUPPORTED_METHOD = "unsupported_method"
    DAEMON_SHUTTING_DOWN = "daemon_shutting_down"
    DAEMON_ACTIVE_RESOURCES = "daemon_active_resources"
    DAEMON_CONFIRMATION_REQUIRED = "daemon_confirmation_required"
    DAEMON_INCOMPATIBLE = "daemon_incompatible"
    DAEMON_RESTART_REQUIRED = "daemon_restart_required"


class SshPilotError(Exception):
    """Base exception carrying stable, frontend-safe error metadata."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: Optional[Mapping[str, Any]] = None,
        retryable: bool = False,
        request_id: Optional[RequestId] = None,
        connection_id: Optional[ConnectionId] = None,
        session_id: Optional[SessionId] = None,
    ) -> None:
        if not isinstance(code, ErrorCode):
            raise TypeError("error code must be an ErrorCode")
        if type(message) is not str or not message:
            raise TypeError("error message must be a non-empty string")
        if type(retryable) is not bool:
            raise TypeError("error retryable must be a boolean")
        for identifier_name, identifier in (
            ("request_id", request_id),
            ("connection_id", connection_id),
            ("session_id", session_id),
        ):
            if identifier is not None and (
                type(identifier) is not str or not identifier.strip()
            ):
                raise TypeError(
                    f"error {identifier_name} must be a non-empty string or None"
                )
        super().__init__(message)
        self.code = code
        self.message = message
        self._details = copy_safe_details(details)
        self.retryable = bool(retryable)
        self.request_id = request_id
        self.connection_id = connection_id
        self.session_id = session_id

    def __repr__(self) -> str:
        """Exclude structured details and identifiers from diagnostic repr."""

        return (
            f"{type(self).__name__}(code={self.code.value!r}, "
            f"message={self.message!r}, retryable={self.retryable!r})"
        )

    @property
    def details(self) -> dict:
        """Return a detached copy so callers cannot bypass validation."""

        return copy_safe_details(self._details)

    def to_dict(self) -> dict:
        """Return the public error envelope without implementation exceptions."""

        return {
            "code": self.code.value,
            "message": self.message,
            "details": copy_safe_details(self._details),
            "retryable": self.retryable,
            "request_id": self.request_id,
            "connection_id": self.connection_id,
            "session_id": self.session_id,
        }


class UnsupportedCapabilityError(SshPilotError):
    def __init__(self, message: str, capability: str) -> None:
        super().__init__(ErrorCode.UNSUPPORTED_CAPABILITY, message)
        self.capability = capability


class DaemonRestartRequiredError(SshPilotError):
    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.API_VERSION_MISMATCH, message)


class InvalidRequestError(SshPilotError):
    pass


def unsupported_capability(capability: CapabilityLike) -> SshPilotError:
    """Build the canonical unsupported-capability error."""

    capability_value = getattr(capability, "value", capability)
    return SshPilotError(
        ErrorCode.UNSUPPORTED_CAPABILITY,
        f"Capability '{capability_value}' is not supported by this client",
        details={"capability": str(capability_value)},
    )

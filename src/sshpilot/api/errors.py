"""Stable, structured errors exposed by :mod:`sshpilot.api`."""

from enum import Enum
from typing import Any, Mapping, Optional

from .models.common import ConnectionId, RequestId, SessionId

CapabilityLike = Any


class ErrorCode(str, Enum):
    """Machine-readable protocol error codes."""

    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    INVALID_REQUEST = "invalid_request"
    VALIDATION_FAILED = "validation_failed"
    CONNECTION_NOT_FOUND = "connection_not_found"
    SESSION_NOT_FOUND = "session_not_found"
    INTERACTION_NOT_FOUND = "interaction_not_found"
    INTERACTION_ALREADY_ANSWERED = "interaction_already_answered"
    PERMISSION_DENIED = "permission_denied"
    OPERATION_CANCELLED = "operation_cancelled"
    OPERATION_TIMED_OUT = "operation_timed_out"
    INTERNAL_ERROR = "internal_error"


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
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})
        self.retryable = bool(retryable)
        self.request_id = request_id
        self.connection_id = connection_id
        self.session_id = session_id

    def to_dict(self) -> dict:
        """Return the public error envelope without implementation exceptions."""

        return {
            "code": self.code.value,
            "message": self.message,
            "details": dict(self.details),
            "retryable": self.retryable,
            "request_id": self.request_id,
            "connection_id": self.connection_id,
            "session_id": self.session_id,
        }


def unsupported_capability(capability: CapabilityLike) -> SshPilotError:
    """Build the canonical unsupported-capability error."""

    capability_value = getattr(capability, "value", capability)
    return SshPilotError(
        ErrorCode.UNSUPPORTED_CAPABILITY,
        f"Capability '{capability_value}' is not supported by this client",
        details={"capability": str(capability_value)},
    )

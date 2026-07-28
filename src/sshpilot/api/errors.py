"""Stable, structured errors exposed by :mod:`sshpilot.api`."""

from enum import Enum
from typing import Any, Mapping, Optional

from ._safe_values import copy_safe_details
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


def unsupported_capability(capability: CapabilityLike) -> SshPilotError:
    """Build the canonical unsupported-capability error."""

    capability_value = getattr(capability, "value", capability)
    return SshPilotError(
        ErrorCode.UNSUPPORTED_CAPABILITY,
        f"Capability '{capability_value}' is not supported by this client",
        details={"capability": str(capability_value)},
    )

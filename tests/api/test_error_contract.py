from sshpilot.api import ErrorCode, SshPilotError
from sshpilot.api.models.common import ConnectionId, RequestId


def test_error_envelope_is_stable_and_safe():
    error = SshPilotError(
        ErrorCode.VALIDATION_FAILED,
        "Connection validation failed",
        details={"field": "hostname"},
        retryable=False,
        request_id=RequestId("request-1"),
        connection_id=ConnectionId("connection:v1:test"),
    )

    assert error.to_dict() == {
        "code": "validation_failed",
        "message": "Connection validation failed",
        "details": {"field": "hostname"},
        "retryable": False,
        "request_id": "request-1",
        "connection_id": "connection:v1:test",
        "session_id": None,
    }


def test_error_repr_does_not_require_raw_exception_details():
    error = SshPilotError(ErrorCode.INTERNAL_ERROR, "Operation failed")

    assert "Traceback" not in repr(error)
    assert error.details == {}


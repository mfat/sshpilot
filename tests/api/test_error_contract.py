import pytest

from sshpilot.api import ErrorCode, SshPilotError
from sshpilot.api.models.common import ConnectionId, RequestId


def test_error_envelope_is_stable_and_safe():
    error = SshPilotError(
        ErrorCode.VALIDATION_FAILED,
        "Connection validation failed",
        details={"field": "hostname"},
        retryable=False,
        request_id=RequestId("request-1"),
        connection_id=ConnectionId("test"),
    )

    assert error.to_dict() == {
        "code": "validation_failed",
        "message": "Connection validation failed",
        "details": {"field": "hostname"},
        "retryable": False,
        "request_id": "request-1",
        "connection_id": "test",
        "session_id": None,
    }


def test_error_repr_does_not_require_raw_exception_details():
    error = SshPilotError(
        ErrorCode.INTERNAL_ERROR,
        "Operation failed",
        details={"reason": "potentially-sensitive-context"},
    )

    assert "Traceback" not in repr(error)
    assert "potentially-sensitive-context" not in repr(error)
    assert str(error) == "Operation failed"


def test_error_details_are_deep_copied_and_frontend_safe():
    original = {
        "field": "hostname",
        "retry_after": 2.5,
        "problems": [{"code": "invalid"}],
        "optional": None,
    }
    error = SshPilotError(
        ErrorCode.VALIDATION_FAILED,
        "Connection validation failed",
        details=original,
    )
    original["problems"][0]["code"] = "mutated"

    assert error.details["problems"][0]["code"] == "invalid"
    assert error.to_dict()["details"] == {
        "field": "hostname",
        "retry_after": 2.5,
        "problems": [{"code": "invalid"}],
        "optional": None,
    }

    exposed = error.details
    exposed["problems"][0]["code"] = "mutated-again"
    assert error.details["problems"][0]["code"] == "invalid"


@pytest.mark.parametrize(
    "unsafe",
    [
        RuntimeError("secret-bearing exception"),
        object(),
        lambda: None,
    ],
)
def test_error_details_reject_unsafe_objects_without_rendering_them(unsafe):
    with pytest.raises(TypeError) as caught:
        SshPilotError(
            ErrorCode.INTERNAL_ERROR,
            "Operation failed",
            details={"cause": unsafe},
        )

    assert repr(unsafe) not in str(caught.value)


def test_error_details_reject_nested_unsafe_values():
    with pytest.raises(TypeError):
        SshPilotError(
            ErrorCode.INTERNAL_ERROR,
            "Operation failed",
            details={"problems": [{"cause": RuntimeError("private")}]},
        )


@pytest.mark.parametrize(
    "details",
    [
        {"password": "do-not-expose"},
        {"environment": {"SAFE": "value"}},
        {"argv": ["ssh", "-p", "do-not-expose"]},
        {"nested": {"access_token": "do-not-expose"}},
    ],
)
def test_error_details_reject_secret_environment_and_command_keys(details):
    with pytest.raises(ValueError) as caught:
        SshPilotError(
            ErrorCode.INTERNAL_ERROR,
            "Operation failed",
            details=details,
        )

    assert "do-not-expose" not in str(caught.value)


def test_internal_exception_is_logged_separately_from_public_error_details(caplog):
    try:
        raise RuntimeError("internal diagnostic")
    except RuntimeError:
        import logging

        logging.getLogger("sshpilot.test").exception("Internal operation failed")

    error = SshPilotError(ErrorCode.INTERNAL_ERROR, "Operation failed")

    assert "internal diagnostic" in caplog.text
    assert error.to_dict()["details"] == {}


def test_error_envelope_rejects_internal_objects_in_correlation_fields():
    with pytest.raises(TypeError):
        SshPilotError(
            ErrorCode.INTERNAL_ERROR,
            "Operation failed",
            connection_id=object(),
        )

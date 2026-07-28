import pytest

from sshpilot.api.models import (
    ConnectionValidationError,
    ConnectionValidationResult,
    CreateConnectionRequest,
    UpdateConnectionRequest,
)


def test_connection_request_validation_is_structured_at_model_boundary():
    with pytest.raises(ValueError, match="port"):
        CreateConnectionRequest(nickname="demo", hostname="host", port=0)
    with pytest.raises(ValueError, match="nickname"):
        UpdateConnectionRequest(nickname="")


def test_validation_result_cannot_be_valid_with_errors():
    error = ConnectionValidationError(
        field="hostname",
        code="required",
        message="Hostname is required",
    )

    with pytest.raises(ValueError):
        ConnectionValidationResult(valid=True, errors=(error,))

    result = ConnectionValidationResult(valid=False, errors=(error,))
    assert result.errors == (error,)


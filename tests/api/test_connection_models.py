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
    with pytest.raises(ValueError, match="at least one"):
        UpdateConnectionRequest()
    with pytest.raises((TypeError, ValueError), match="port"):
        CreateConnectionRequest(
            nickname="demo",
            hostname="host",
            port=True,
        )
    with pytest.raises((TypeError, ValueError), match="protocol"):
        CreateConnectionRequest(
            nickname="demo",
            hostname="host",
            protocol="",
        )


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


def test_update_connection_request_normalizes_string_proxy_jump():
    req = UpdateConnectionRequest(
        nickname="Oracle",
        config_patch={"proxy_jump": "ubuntu@150.230.27.23"},
    )
    assert req.config_patch["proxy_jump"] == ["ubuntu@150.230.27.23"]


def test_create_connection_request_accepts_and_validates_config_patch():
    req = CreateConnectionRequest(
        nickname="ProxyHost",
        hostname="target.internal",
        config_patch={"proxy_jump": ["jump.example.com"]},
    )
    assert req.config_patch["proxy_jump"] == ["jump.example.com"]


def test_ssh_host_aliases_cannot_begin_with_option_dash():
    with pytest.raises(ValueError, match="begin with"):
        CreateConnectionRequest(nickname="-G", hostname="example.com")
    with pytest.raises(ValueError, match="begin with"):
        UpdateConnectionRequest(nickname="--target")


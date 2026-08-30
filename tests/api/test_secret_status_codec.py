import pytest

from sshpilot.api.models.secrets import (
    BitwardenStatus,
    RbwStatus,
    SecretMessageCode,
    SecretOperationResult,
    SecretOperationState,
    SecretUnlockResult,
    UnlockResultKind,
)
from sshpilot.api.transport.codec import (
    bitwarden_status_from_wire,
    bitwarden_status_to_wire,
    rbw_status_from_wire,
    rbw_status_to_wire,
    secret_operation_result_from_wire,
    secret_operation_result_to_wire,
    secret_unlock_result_from_wire,
    secret_unlock_result_to_wire,
)


@pytest.mark.parametrize(
    ("value", "encoder", "decoder"),
    [
        (
            SecretUnlockResult(
                kind=UnlockResultKind.BACKEND_UNAVAILABLE,
                backend="rbw",
                message_code=SecretMessageCode.SECRET_BACKEND_UNAVAILABLE,
                message_parameters={"backend": "rbw"},
            ),
            secret_unlock_result_to_wire,
            secret_unlock_result_from_wire,
        ),
        (
            SecretOperationResult(
                state=SecretOperationState.FAILED,
                backend="keepassxc",
                message_code=SecretMessageCode.BACKEND_UNAVAILABLE,
                message_parameters={"backend": "keepassxc"},
                diagnostic="pykeepass is not installed",
            ),
            secret_operation_result_to_wire,
            secret_operation_result_from_wire,
        ),
        (
            BitwardenStatus(
                logged_in=False,
                unlocked=False,
                needs_login=True,
                email="alice@example.com",
                server_url="",
                profile="",
                message_code=SecretMessageCode.BITWARDEN_SIGN_IN_FAILED,
                diagnostic="Invalid credentials for alice@example.com",
            ),
            bitwarden_status_to_wire,
            bitwarden_status_from_wire,
        ),
        (
            RbwStatus(
                installed=True,
                configured=True,
                unlocked=True,
                email="alice@example.com",
                base_url="",
                message_code=SecretMessageCode.RBW_SYNC_FAILED,
            ),
            rbw_status_to_wire,
            rbw_status_from_wire,
        ),
    ],
)
def test_structured_secret_message_round_trip(value, encoder, decoder):
    wire = encoder(value)

    assert "message" not in wire
    assert decoder(wire) == value


def test_secret_message_decoder_rejects_unknown_code():
    wire = secret_unlock_result_to_wire(
        SecretUnlockResult(kind=UnlockResultKind.UNLOCKED, backend="bitwarden")
    )
    wire["message_code"] = "future_secret_message"

    with pytest.raises(ValueError, match="valid secret message code"):
        secret_unlock_result_from_wire(wire)


def test_secret_message_decoder_rejects_wrong_parameters():
    wire = secret_unlock_result_to_wire(
        SecretUnlockResult(
            kind=UnlockResultKind.BACKEND_UNAVAILABLE,
            backend="rbw",
            message_code=SecretMessageCode.SECRET_BACKEND_UNAVAILABLE,
            message_parameters={"backend": "rbw"},
        )
    )
    wire["message_parameters"] = {"backend": "rbw", "extra": "unsafe"}

    with pytest.raises(ValueError, match="do not match"):
        secret_unlock_result_from_wire(wire)


def test_secret_message_decoder_rejects_legacy_message_shape():
    wire = secret_unlock_result_to_wire(
        SecretUnlockResult(kind=UnlockResultKind.UNLOCKED, backend="bitwarden")
    )
    wire.pop("message_code")
    wire.pop("message_parameters")
    wire.pop("diagnostic")
    wire["message"] = "The vault could not be unlocked"

    with pytest.raises(ValueError, match="missing required fields"):
        secret_unlock_result_from_wire(wire)

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ConnectionId, RequestId
from sshpilot.api.transport.codec import (
    decode_envelope,
    encode_envelope,
    error_from_wire,
    error_to_wire,
)
from sshpilot.api.transport.envelopes import ErrorResponseEnvelope
from sshpilot.api.version import PROTOCOL_VERSION


def test_direct_sftp_error_round_trip_preserves_code_and_diagnostic():
    diagnostic = "server locale diagnostic"
    error = SshPilotError(
        ErrorCode.SFTP_COMMAND_FAILED,
        ErrorCode.SFTP_COMMAND_FAILED.value,
        details={
            "service_id": "sftp-7",
            "sftp_status": 4,
            "server_message": diagnostic,
            "server_message_is_specific": True,
        },
        connection_id=ConnectionId("demo"),
    )
    envelope = ErrorResponseEnvelope(
        PROTOCOL_VERSION,
        RequestId("request-1"),
        error_to_wire(error),
    )

    encoded = encode_envelope(envelope)
    decoded = decode_envelope(encoded)
    restored = error_from_wire(decoded.error)

    assert restored.code is ErrorCode.SFTP_COMMAND_FAILED
    assert restored.message == ErrorCode.SFTP_COMMAND_FAILED.value
    assert restored.details == error.details
    assert restored.details["server_message"] == diagnostic
    assert "The SFTP command failed" not in str(encoded)


def test_direct_sftp_error_codec_rejects_unknown_code():
    with pytest.raises(ValueError, match="unknown error code"):
        decode_envelope(
            {
                "type": "error",
                "protocol_version": PROTOCOL_VERSION,
                "request_id": "request-1",
                "error": {
                    "code": "english sentence as protocol code",
                    "message": "english sentence as protocol code",
                    "details": {},
                    "retryable": False,
                    "request_id": None,
                    "connection_id": None,
                    "session_id": None,
                },
            }
        )

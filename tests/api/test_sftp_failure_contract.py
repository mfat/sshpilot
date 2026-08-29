from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sshpilot.api.errors import ErrorCode
from sshpilot.api.models.operations import (
    OperationKind,
    OperationState,
    OperationSummary,
    ServiceFailure,
    SftpFailure,
    SftpFailureCode,
    SftpServiceState,
    SftpServiceSummary,
)
from sshpilot.api.models.transfers import (
    TransferBackend,
    TransferDirection,
    TransferState,
    TransferSummary,
)
from sshpilot.api.transport.codec import (
    operation_summary_from_wire,
    operation_summary_to_wire,
    sftp_service_summary_from_wire,
    sftp_service_summary_to_wire,
    transfer_summary_from_wire,
    transfer_summary_to_wire,
)


NOW = datetime(2026, 8, 29, tzinfo=timezone.utc)


def _failure() -> SftpFailure:
    return SftpFailure(
        code=SftpFailureCode.REMOTE_DIRECTORY_CREATION_FAILED,
        error_code=ErrorCode.TRANSFER_IO_FAILED,
        parameters={"remote_dir": "/home/alice/données {brutes}"},
        diagnostic="opaque server detail: mkdir status=4",
    )


def test_sftp_service_failure_round_trip_has_no_final_ui_text():
    summary = SftpServiceSummary(
        id="sftp-contract",
        connection_id="connection-contract",
        state=SftpServiceState.FAILED,
        created_at=NOW,
        failure=SftpFailure(
            SftpFailureCode.CONNECTION_LOST,
            ErrorCode.SFTP_PROTOCOL_LOST,
        ),
    )

    wire = sftp_service_summary_to_wire(summary)

    assert wire["failure"] == {
        "kind": "sftp",
        "code": "connection_lost",
        "error_code": "sftp_protocol_lost",
        "parameters": {},
        "diagnostic": "",
    }
    assert "The SFTP connection was lost" not in repr(wire)
    assert sftp_service_summary_from_wire(wire) == summary


def test_recursive_operation_failure_round_trips_exact_diagnostic():
    failure = SftpFailure(
        SftpFailureCode.PERMISSION_DENIED,
        ErrorCode.REMOTE_PERMISSION_DENIED,
        diagnostic="storage policy denied inode 42",
    )
    summary = OperationSummary(
        operation_id="operation-contract",
        kind=OperationKind.SFTP_COPY_TREE,
        state=OperationState.FAILED,
        message="",
        created_at=NOW,
        failure=failure,
    )

    wire = operation_summary_to_wire(summary)

    assert wire["message"] == ""
    assert wire["failure"]["diagnostic"] == "storage policy denied inode 42"
    assert operation_summary_from_wire(wire) == summary


def test_sftp_transfer_failure_round_trips_exact_parameters():
    summary = TransferSummary(
        id="transfer-contract",
        connection_id="connection-contract",
        sftp_service_id="sftp-contract",
        backend=TransferBackend.SFTP,
        direction=TransferDirection.UPLOAD,
        state=TransferState.FAILED,
        source_display="source",
        destination_display="destination",
        created_at=NOW,
        failure=_failure(),
    )

    wire = transfer_summary_to_wire(summary)

    assert wire["failure"]["parameters"] == {
        "remote_dir": "/home/alice/données {brutes}"
    }
    assert wire["failure"]["diagnostic"] == "opaque server detail: mkdir status=4"
    assert transfer_summary_from_wire(wire) == summary


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("kind", "other"),
        ("code", "unknown_sftp_failure"),
        ("error_code", "unknown_error"),
        ("parameters", []),
    ),
)
def test_sftp_failure_codec_rejects_unknown_or_invalid_payload(field, value):
    summary = SftpServiceSummary(
        id="sftp-contract",
        connection_id="connection-contract",
        state=SftpServiceState.FAILED,
        created_at=NOW,
        failure=SftpFailure(
            SftpFailureCode.CONNECTION_LOST,
            ErrorCode.SFTP_PROTOCOL_LOST,
        ),
    )
    wire = sftp_service_summary_to_wire(summary)
    wire["failure"][field] = value

    with pytest.raises((TypeError, ValueError)):
        sftp_service_summary_from_wire(wire)


def test_sftp_failure_rejects_wrong_parameter_shape():
    with pytest.raises(ValueError, match="parameters do not match"):
        SftpFailure(
            SftpFailureCode.REMOTE_DESTINATION_EXISTS,
            ErrorCode.TRANSFER_CONFLICT,
            parameters={"remote_dir": "/wrong-key"},
        )


def test_native_scp_service_failure_wire_contract_is_unchanged():
    failure = ServiceFailure("scp_transfer_failed", "scp exited with status 1")
    summary = TransferSummary(
        id="transfer-scp",
        connection_id="connection-contract",
        sftp_service_id=None,
        backend=TransferBackend.NATIVE_SCP,
        direction=TransferDirection.UPLOAD,
        state=TransferState.FAILED,
        source_display="source",
        destination_display="destination",
        created_at=NOW,
        failure=failure,
    )

    wire = transfer_summary_to_wire(summary)

    assert wire["failure"] == {
        "code": "scp_transfer_failed",
        "message": "scp exited with status 1",
    }
    assert transfer_summary_from_wire(wire) == summary


def test_failure_model_is_selected_by_operation_kind_and_transfer_backend():
    with pytest.raises(TypeError, match="operation kind"):
        OperationSummary(
            operation_id="operation-contract",
            kind=OperationKind.KEY_DEPLOYMENT,
            state=OperationState.FAILED,
            message="legacy text",
            created_at=NOW,
            failure=SftpFailure(
                SftpFailureCode.COMMAND_FAILED,
                ErrorCode.SFTP_COMMAND_FAILED,
            ),
        )
    with pytest.raises(TypeError, match="transfer backend"):
        TransferSummary(
            id="transfer-contract",
            connection_id="connection-contract",
            sftp_service_id="sftp-contract",
            backend=TransferBackend.SFTP,
            direction=TransferDirection.UPLOAD,
            state=TransferState.FAILED,
            source_display="source",
            destination_display="destination",
            created_at=NOW,
            failure=ServiceFailure("legacy", "legacy text"),
        )

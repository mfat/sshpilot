from datetime import datetime, timezone

import pytest

from sshpilot.api import Capability
from sshpilot.api.models import (
    ConnectionId,
    StartScpTransferRequest,
    TransferBackend,
    TransferConflictPolicy,
    TransferDirection,
    TransferState,
    TransferSummary,
)
from sshpilot.api.transport.codec import (
    scp_transfer_request_from_wire,
    scp_transfer_request_to_wire,
    transfer_summary_from_wire,
    transfer_summary_to_wire,
)


def test_scp_default_policy_is_overwrite_and_other_policies_are_rejected():
    default = StartScpTransferRequest(
        connection_id=ConnectionId("demo"),
        direction=TransferDirection.UPLOAD,
        sources=("/tmp/source",),
        destination="/remote/drop",
    )
    assert default.conflict_policy is TransferConflictPolicy.OVERWRITE
    for policy in (
        TransferConflictPolicy.FAIL,
        TransferConflictPolicy.SKIP,
        TransferConflictPolicy.RENAME,
    ):
        with pytest.raises(ValueError, match="overwrite only"):
            StartScpTransferRequest(
                connection_id=ConnectionId("demo"),
                direction=TransferDirection.UPLOAD,
                sources=("/tmp/source",),
                destination="/remote/drop",
                conflict_policy=policy,
            )


def test_scp_request_round_trips_multiple_sources_and_recursive():
    request = StartScpTransferRequest(
        connection_id=ConnectionId("demo"),
        direction=TransferDirection.UPLOAD,
        sources=("/tmp/one file", "/tmp/two"),
        destination="/var/tmp/drop",
        recursive=True,
        conflict_policy=TransferConflictPolicy.OVERWRITE,
    )

    wire = scp_transfer_request_to_wire(request)
    restored = scp_transfer_request_from_wire(wire)

    assert restored == request
    assert "password" not in repr(request)
    assert "environment" not in repr(request)


def test_scp_request_rejects_empty_nul_and_excessive_sources():
    with pytest.raises(ValueError):
        StartScpTransferRequest(
            connection_id=ConnectionId("demo"),
            direction=TransferDirection.DOWNLOAD,
            sources=(),
            destination="/tmp",
        )
    with pytest.raises(ValueError):
        StartScpTransferRequest(
            connection_id=ConnectionId("demo"),
            direction=TransferDirection.DOWNLOAD,
            sources=("/remote/a\x00",),
            destination="/tmp",
        )
    with pytest.raises(ValueError):
        StartScpTransferRequest(
            connection_id=ConnectionId("demo"),
            direction=TransferDirection.DOWNLOAD,
            sources=tuple(f"/remote/{index}" for index in range(65)),
            destination="/tmp",
        )


def test_native_scp_summary_round_trips_without_sftp_service():
    summary = TransferSummary(
        id="transfer-1",
        connection_id="demo",
        sftp_service_id=None,
        backend=TransferBackend.NATIVE_SCP,
        direction=TransferDirection.DOWNLOAD,
        state=TransferState.RUNNING,
        source_display="remote.log",
        destination_display="/tmp/remote.log",
        created_at=datetime.now(timezone.utc),
    )

    restored = transfer_summary_from_wire(transfer_summary_to_wire(summary))

    assert restored == summary
    assert restored.backend is TransferBackend.NATIVE_SCP
    assert restored.sftp_service_id is None


def test_native_scp_capability_is_declared():
    assert Capability.TRANSFERS_SCP.value == "transfers.scp"

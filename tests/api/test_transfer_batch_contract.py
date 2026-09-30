"""Wire contract for batch transfers (API 0.76)."""

from datetime import datetime, timezone

import pytest

from sshpilot.api.errors import ErrorCode
from sshpilot.api.models import (
    ConnectionId,
    StartTransferBatchRequest,
    TransferConflictPolicy,
    TransferDirection,
    TransferItem,
    TransferItemFailure,
    TransferState,
    TransferSummary,
)
from sshpilot.api.models.common import SftpServiceId, TransferId
from sshpilot.api.models.operations import SftpFailure, SftpFailureCode
from sshpilot.api.transport.codec import (
    start_transfer_batch_request_from_wire,
    start_transfer_batch_request_to_wire,
    transfer_summary_from_wire,
    transfer_summary_to_wire,
)

_FAILURE = SftpFailure(SftpFailureCode.PATH_NOT_FOUND, ErrorCode.REMOTE_PATH_NOT_FOUND)


def _request(items, **kwargs):
    return StartTransferBatchRequest(
        connection_id=ConnectionId("demo"),
        sftp_service_id=SftpServiceId("sftp-1"),
        direction=TransferDirection.UPLOAD,
        items=tuple(items),
        **kwargs,
    )


def _summary(**kwargs):
    return TransferSummary(
        id=TransferId("transfer-1"),
        connection_id=ConnectionId("demo"),
        sftp_service_id=SftpServiceId("sftp-1"),
        direction=TransferDirection.UPLOAD,
        state=TransferState.FAILED,
        source_display="a",
        destination_display="/remote",
        created_at=datetime(2026, 9, 30, tzinfo=timezone.utc),
        **kwargs,
    )


def test_batch_request_round_trips_and_defaults_to_overwrite():
    request = _request(
        [
            TransferItem("/tmp/a", "/remote/a"),
            TransferItem("/tmp/tree", "/remote/tree", recursive=True),
        ]
    )
    assert request.conflict_policy is TransferConflictPolicy.OVERWRITE
    assert start_transfer_batch_request_from_wire(
        start_transfer_batch_request_to_wire(request)
    ) == request


@pytest.mark.parametrize(
    "items",
    [
        [],
        [TransferItem("/tmp/a", "/remote/a")] * (StartTransferBatchRequest.MAX_ITEMS + 1),
    ],
)
def test_batch_request_rejects_empty_and_oversized(items):
    with pytest.raises(ValueError):
        _request(items)


def test_batch_request_decoder_rejects_unknown_item_fields():
    wire = start_transfer_batch_request_to_wire(_request([TransferItem("/tmp/a", "/remote/a")]))
    wire["items"][0]["mode"] = "0600"
    with pytest.raises(ValueError):
        start_transfer_batch_request_from_wire(wire)


def test_batch_summary_round_trips_with_item_failures():
    summary = _summary(
        items_total=3,
        items_done=3,
        item_failures=(TransferItemFailure(index=1, failure=_FAILURE),),
        failure=_FAILURE,
    )
    assert transfer_summary_from_wire(transfer_summary_to_wire(summary)) == summary


def test_single_transfer_summary_wire_is_unchanged():
    wire = transfer_summary_to_wire(_summary())
    assert not {"items_total", "items_done", "item_failures"} & set(wire)
    assert transfer_summary_from_wire(wire).items_total is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"items_done": 1},
        {"items_total": 2, "items_done": 3},
        {"items_total": 2, "item_failures": (TransferItemFailure(index=2, failure=_FAILURE),)},
    ],
)
def test_batch_summary_rejects_inconsistent_item_counts(kwargs):
    with pytest.raises(ValueError):
        _summary(**kwargs)

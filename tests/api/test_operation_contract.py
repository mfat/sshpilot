from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sshpilot.api.errors import ErrorCode
from sshpilot.api.events import CoreEvent, EventType
from sshpilot.api.models.operations import (
    OperationId,
    OperationKind,
    OperationState,
    OperationSummary,
    ServiceFailure,
    is_valid_operation_transition,
)
from sshpilot.api.transport.codec import (
    operation_id_request_from_wire,
    operation_id_request_to_wire,
    operation_summary_from_wire,
    operation_summary_to_wire,
    public_event_from_envelope,
    public_event_to_envelope,
)


def _summary(state=OperationState.RUNNING):
    return OperationSummary(
        operation_id=OperationId("operation-contract"),
        kind=OperationKind.KEY_DEPLOYMENT,
        state=state,
        message="safe progress",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        started_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
        finished_at=(
            datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc)
            if state is OperationState.SUCCEEDED
            else None
        ),
        progress=0.5,
        failure=(
            ServiceFailure(ErrorCode.REMOTE_COMMAND_FAILED.value, "safe failure")
            if state is OperationState.FAILED
            else None
        ),
    )


def test_operation_summary_and_id_round_trip():
    summary = _summary()
    assert operation_summary_from_wire(operation_summary_to_wire(summary)) == summary
    operation_id = OperationId("operation-contract")
    assert operation_id_request_from_wire(operation_id_request_to_wire(operation_id)) == operation_id


def test_operation_event_round_trip_uses_typed_snapshot():
    event = CoreEvent(
        type=EventType.OPERATION_STATE_CHANGED,
        payload=_summary(OperationState.SUCCEEDED),
        sequence=4,
    )
    envelope = public_event_to_envelope(
        event,
        sequence=event.sequence,
        protocol_version="1.0",
    )
    restored = public_event_from_envelope(envelope)
    assert restored.type is event.type
    assert restored.sequence == event.sequence
    assert restored.payload == event.payload


def test_operation_codec_rejects_unknown_state_and_unsafe_fields():
    wire = operation_summary_to_wire(_summary())
    wire["state"] = "not-a-state"
    with pytest.raises((TypeError, ValueError)):
        operation_summary_from_wire(wire)

    unsafe = operation_summary_to_wire(_summary())
    unsafe["state"] = "not-a-state"
    with pytest.raises((TypeError, ValueError)):
        operation_summary_from_wire(unsafe)


def test_operation_transition_contract_remains_minimal():
    assert is_valid_operation_transition(OperationState.QUEUED, OperationState.RUNNING)
    assert is_valid_operation_transition(OperationState.RUNNING, OperationState.CANCELLED)
    assert not is_valid_operation_transition(OperationState.QUEUED, OperationState.SUCCEEDED)
    assert not is_valid_operation_transition(OperationState.FAILED, OperationState.RUNNING)

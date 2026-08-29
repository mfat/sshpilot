from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sshpilot.api.errors import ErrorCode
from sshpilot.api.events import CoreEvent, EventType
from sshpilot.api.models.operations import (
    IdentityFailure,
    IdentityFailureCode,
    OperationId,
    OperationKind,
    OperationState,
    OperationSummary,
    is_valid_operation_transition,
)
from sshpilot.api.models.daemon import OperationMode, OperationModeFiles, OperationModeResult
from sshpilot.api.transport.codec import (
    operation_mode_files_from_wire,
    operation_mode_files_to_wire,
    operation_mode_result_from_wire,
    operation_mode_result_to_wire,
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
            IdentityFailure(
                IdentityFailureCode.INSTALLATION_FAILED,
                ErrorCode.REMOTE_COMMAND_FAILED,
            )
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


def test_recovery_mode_result_without_additive_rollback_flag_stays_truthful():
    wire = operation_mode_result_to_wire(
        OperationModeResult(
            accepted=False,
            active_mode=OperationMode.DEFAULT,
            generation=3,
            message="recovery required",
            target_description="Default user SSH configuration",
            recovery_required=True,
            rollback_completed=False,
        )
    )
    wire.pop("rollback_completed")

    restored = operation_mode_result_from_wire(wire)

    assert restored.recovery_required is True
    assert restored.rollback_completed is False


def test_successful_operation_mode_status_allows_empty_message():
    result = OperationModeResult(
        accepted=True,
        active_mode=OperationMode.DEFAULT,
        generation=4,
        target_description="Default user SSH configuration",
    )

    restored = operation_mode_result_from_wire(operation_mode_result_to_wire(result))

    assert restored.accepted is True
    assert restored.message == ""


def _files(root: str) -> OperationModeFiles:
    return OperationModeFiles(
        root_config_path=f"{root}/config",
        root_config_exists=True,
        known_hosts_path=f"{root}/known_hosts",
        known_hosts_exists=False,
        imported_fragment_path=f"{root}/sshpilot-imported.conf",
        imported_fragment_exists=False,
    )


def test_operation_mode_files_round_trip():
    files = _files("/home/user/.ssh")
    assert operation_mode_files_from_wire(operation_mode_files_to_wire(files)) == files


def test_operation_mode_result_round_trip_carries_resolved_files():
    result = OperationModeResult(
        accepted=True,
        active_mode=OperationMode.ISOLATED,
        generation=7,
        target_description="Isolated SSH configuration",
        default_files=_files("/home/user/.ssh"),
        isolated_files=_files("/home/user/.config/sshpilot"),
        app_config_path="/home/user/.config/sshpilot/config.json",
        app_config_exists=True,
    )

    restored = operation_mode_result_from_wire(operation_mode_result_to_wire(result))

    assert restored == result


def test_operation_mode_result_without_files_decodes_as_pre_0_41_payload():
    """A daemon predating API 0.41 omits the file-visibility fields entirely;
    the client must still decode a truthful (if less detailed) result rather
    than rejecting the whole response."""
    wire = operation_mode_result_to_wire(
        OperationModeResult(
            accepted=True,
            active_mode=OperationMode.DEFAULT,
            generation=1,
            target_description="Default user SSH configuration",
        )
    )
    for key in ("default_files", "isolated_files", "app_config_path", "app_config_exists"):
        wire.pop(key)

    restored = operation_mode_result_from_wire(wire)

    assert restored.default_files is None
    assert restored.isolated_files is None
    assert restored.app_config_path == ""
    assert restored.app_config_exists is False

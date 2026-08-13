from dataclasses import replace
from datetime import datetime, timezone

import pytest

from sshpilot.api.events import CoreEvent, EventType
from sshpilot.api.models.common import (
    AttachmentId,
    ClientId,
    ConnectionId,
    SessionId,
)
from sshpilot.api.models.sessions import (
    AttachSessionRequest,
    AttachSessionResult,
    AttachmentInfo,
    CloseSessionRequest,
    DetachSessionRequest,
    OpenSessionRequest,
    SessionCapabilities,
    SessionExitInfo,
    SessionFailure,
    SessionState,
    SessionSummary,
)
from sshpilot.api.transport.codec import (
    attach_session_request_from_wire,
    attach_session_request_to_wire,
    attach_session_result_from_wire,
    attach_session_result_to_wire,
    close_session_request_from_wire,
    close_session_request_to_wire,
    detach_session_request_from_wire,
    detach_session_request_to_wire,
    open_session_request_from_wire,
    open_session_request_to_wire,
    public_event_from_envelope,
    public_event_to_envelope,
    session_summary_from_wire,
    session_summary_to_wire,
)


def _summary(state=SessionState.RUNNING):
    return SessionSummary(
        id=SessionId("session-1"),
        connection_id=ConnectionId(
            "conn-2"
        ),
        state=state,
        created_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        capabilities=SessionCapabilities(),
    )


def test_session_request_and_result_codecs_round_trip_strictly():
    open_request = OpenSessionRequest(connection_id=_summary().connection_id)
    attach_request = AttachSessionRequest(session_id=_summary().id)
    detach_request = DetachSessionRequest(
        session_id=_summary().id,
        attachment_id=AttachmentId("attachment:test"),
    )
    close_request = CloseSessionRequest(session_id=_summary().id)
    attachment = AttachmentInfo(
        id=AttachmentId("attachment:test"),
        session_id=_summary().id,
        client_id=ClientId("client:test"),
        input_owner=False,
    )
    result = AttachSessionResult(session=_summary(), attachment=attachment)

    assert open_session_request_from_wire(
        open_session_request_to_wire(open_request)
    ) == open_request
    assert attach_session_request_from_wire(
        attach_session_request_to_wire(attach_request)
    ) == attach_request
    assert detach_session_request_from_wire(
        detach_session_request_to_wire(detach_request)
    ) == detach_request
    assert close_session_request_from_wire(
        close_session_request_to_wire(close_request)
    ) == close_request
    assert attach_session_result_from_wire(
        attach_session_result_to_wire(result)
    ) == result


def test_open_session_request_codec_round_trips_remote_command():
    request = OpenSessionRequest(
        connection_id=ConnectionId("test"),
        remote_command="docker exec -it web sh",
        force_tty=True,
    )
    encoded = open_session_request_to_wire(request)
    assert encoded["remote_command"] == "docker exec -it web sh"
    assert encoded["force_tty"] is True
    decoded = open_session_request_from_wire(encoded)
    assert decoded == request
    # absent field decodes as None
    plain = open_session_request_from_wire({"connection_id": "test"})
    assert plain.remote_command is None
    assert plain.force_tty is False


def test_session_summary_codec_round_trip_includes_safe_failure_and_exit():
    summary = replace(
        _summary(SessionState.FAILED),
        exit_info=SessionExitInfo(exit_code=2, reason="process_exit"),
        failure=SessionFailure(
            code="session_startup_failed",
            message="The session could not start",
        ),
    )

    decoded = session_summary_from_wire(session_summary_to_wire(summary))

    assert decoded == summary
    encoded = session_summary_to_wire(summary)
    assert set(encoded) == {
        "id",
        "connection_id",
        "state",
        "created_at",
        "input_owner",
        "capabilities",
        "exit_info",
        "failure",
        "attachment_count",
    }
    assert "command" not in repr(encoded)
    assert "environment" not in repr(encoded)


def test_session_response_codec_rejects_malformed_session_id():
    encoded = session_summary_to_wire(_summary())
    encoded["id"] = ""

    with pytest.raises(ValueError):
        session_summary_from_wire(encoded)


@pytest.mark.parametrize(
    "decoder,payload",
    [
        (open_session_request_from_wire, {"connection_id": "test", "x": 1}),
        (
            attach_session_request_from_wire,
            {"session_id": "session:test", "request_input": "yes"},
        ),
        (
            detach_session_request_from_wire,
            {"session_id": "session:test"},
        ),
        (
            close_session_request_from_wire,
            {"session_id": "session:test", "secret": "must-not-pass"},
        ),
    ],
)
def test_session_request_codecs_reject_missing_extra_and_wrong_fields(
    decoder,
    payload,
):
    with pytest.raises(ValueError):
        decoder(payload)


@pytest.mark.parametrize(
    "event_type,payload",
    [
        (EventType.SESSION_CREATED, _summary(SessionState.CREATED)),
        (EventType.SESSION_STATE_CHANGED, _summary(SessionState.RUNNING)),
        (EventType.SESSION_CLOSED, _summary(SessionState.CLOSED)),
        (
            EventType.SESSION_EXITED,
            SessionExitInfo(exit_code=0, reason="process_exit"),
        ),
    ],
)
def test_session_event_codec_round_trip_is_typed(event_type, payload):
    summary = _summary()
    event = CoreEvent(
        type=event_type,
        payload=payload,
        sequence=0,
        connection_id=summary.connection_id,
        session_id=summary.id,
    )

    envelope = public_event_to_envelope(
        event,
        sequence=41,
        protocol_version="1.0",
    )
    decoded = public_event_from_envelope(envelope)

    assert decoded.type is event_type
    assert decoded.payload == payload
    assert decoded.sequence == 41
    assert decoded.session_id == summary.id


def test_session_event_name_payload_mismatch_and_internal_object_are_rejected():
    event = CoreEvent(
        type=EventType.SESSION_CREATED,
        payload=_summary(SessionState.CREATED),
        sequence=0,
        session_id=_summary().id,
    )
    envelope = public_event_to_envelope(
        event,
        sequence=1,
        protocol_version="1.0",
    )
    mismatched = replace(envelope, event=EventType.SESSION_EXITED.value)

    with pytest.raises(ValueError):
        public_event_from_envelope(mismatched)
    with pytest.raises(TypeError):
        public_event_to_envelope(
            replace(event, payload=object()),
            sequence=1,
            protocol_version="1.0",
        )

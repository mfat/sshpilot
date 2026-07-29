from dataclasses import replace
from datetime import datetime

import pytest

from sshpilot.api.models.common import AttachmentId, ClientId, ConnectionId, SessionId
from sshpilot.api.models.sessions import (
    AttachSessionResult,
    AttachmentInfo,
    OpenSessionRequest,
    SessionCapabilities,
    SessionExitInfo,
    SessionState,
    SessionSummary,
)


def test_session_states_cover_protocol_v1_lifecycle():
    assert {state.value for state in SessionState} == {
        "created",
        "starting",
        "running",
        "exited",
        "failed",
        "closing",
        "closed",
    }


def test_session_model_requires_opaque_identifiers():
    with pytest.raises(ValueError):
        SessionSummary(
            id=SessionId(""),
            connection_id=ConnectionId("connection:v1:test"),
            state=SessionState.CREATED,
        )

    request = OpenSessionRequest(
        connection_id=ConnectionId("connection:v1:test"),
    )
    assert request.connection_id == "connection:v1:test"


def test_session_model_rejects_invalid_nested_values():
    summary = SessionSummary(
        id=SessionId("session:550e8400-e29b-41d4-a716-446655440000"),
        connection_id=ConnectionId("connection:550e8400-e29b-41d4-a716-446655440001"),
        state=SessionState.CREATED,
    )

    with pytest.raises(TypeError):
        replace(summary, state="running")
    with pytest.raises(ValueError):
        replace(summary, created_at=datetime(2030, 1, 1))
    with pytest.raises(TypeError):
        SessionCapabilities(supported={"terminal.input"})
    with pytest.raises(ValueError):
        SessionExitInfo(exit_code=0, signal=9)
    with pytest.raises(ValueError):
        SessionExitInfo(exit_code=-1)


def test_attach_result_must_reference_the_same_session():
    summary = SessionSummary(
        id=SessionId("session:550e8400-e29b-41d4-a716-446655440000"),
        connection_id=ConnectionId("connection:550e8400-e29b-41d4-a716-446655440001"),
        state=SessionState.RUNNING,
    )
    attachment = AttachmentInfo(
        id=AttachmentId("attachment:test"),
        session_id=SessionId("session:550e8400-e29b-41d4-a716-446655440002"),
        client_id=ClientId("client:test"),
        input_owner=False,
    )

    with pytest.raises(ValueError):
        AttachSessionResult(session=summary, attachment=attachment)

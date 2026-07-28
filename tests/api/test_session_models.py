import pytest

from sshpilot.api.models.common import ClientId, ConnectionId, SessionId
from sshpilot.api.models.sessions import OpenSessionRequest, SessionState, SessionSummary


def test_session_states_cover_protocol_v1_lifecycle():
    assert {state.value for state in SessionState} == {
        "creating",
        "connecting",
        "waiting_for_interaction",
        "connected",
        "reconnecting",
        "disconnected",
        "failed",
        "closing",
        "closed",
    }


def test_session_model_requires_opaque_identifiers():
    with pytest.raises(ValueError):
        SessionSummary(
            id=SessionId(""),
            connection_id=ConnectionId("connection:v1:test"),
            state=SessionState.CREATING,
        )

    request = OpenSessionRequest(
        connection_id=ConnectionId("connection:v1:test"),
        client_id=ClientId("gtk-1"),
    )
    assert request.client_id == "gtk-1"


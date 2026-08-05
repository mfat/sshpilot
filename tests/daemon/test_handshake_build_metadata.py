"""Daemon handshake build-identity metadata for stale-process detection."""

from sshpilot.api.transport.codec import (
    handshake_result_from_wire,
    handshake_result_to_wire,
)
from sshpilot.api.transport.envelopes import HandshakeResult
from sshpilot.api.capabilities import Capability
from sshpilot.api.version import API_IMPLEMENTATION_VERSION


def test_handshake_optional_build_metadata_round_trips():
    result = HandshakeResult(
        daemon_version="5.7.2",
        core_version="5.7.2",
        selected_protocol_version="1.0",
        daemon_capabilities=frozenset({Capability.CONNECTIONS_READ}),
        compatibility_status="compatible",
        server_instance_id="abc123",
        daemon_started_at="2026-07-29T12:00:00Z",
        development_revision="dev-phase-9.3",
        api_implementation_version=API_IMPLEMENTATION_VERSION,
    )
    wire = handshake_result_to_wire(result)
    assert wire["daemon_started_at"] == "2026-07-29T12:00:00Z"
    assert wire["development_revision"] == "dev-phase-9.3"
    assert wire["api_implementation_version"] == API_IMPLEMENTATION_VERSION
    restored = handshake_result_from_wire(wire)
    assert restored.daemon_started_at == result.daemon_started_at
    assert restored.development_revision == result.development_revision
    assert restored.api_implementation_version == result.api_implementation_version


def test_handshake_without_optional_build_metadata_still_parses():
    wire = {
        "daemon_version": "5.7.2",
        "core_version": "5.7.2",
        "selected_protocol_version": "1.0",
        "daemon_capabilities": ["connections.read"],
        "compatibility_status": "compatible",
        "server_instance_id": "abc123",
    }
    restored = handshake_result_from_wire(wire)
    assert restored.daemon_started_at == ""
    assert restored.development_revision == ""
    assert restored.api_implementation_version == ""


def test_dispatcher_includes_dev_revision(monkeypatch):
    from sshpilot.core.connection_application_service import ConnectionApplicationService
    from sshpilot.daemon.dispatch import RequestDispatcher
    from sshpilot.api.transport.envelopes import RequestEnvelope
    from sshpilot.daemon.dispatch import ClientProtocolState
    from tests.helpers.fake_connection_repository import make_test_repository
    
    monkeypatch.setenv("SSHPILOT_DEV_REVISION", "phase93-test")

    dispatcher = RequestDispatcher(ConnectionApplicationService(make_test_repository(), client_name="t"))
    assert dispatcher._development_revision == "phase93-test"
    state = ClientProtocolState()
    request = RequestEnvelope(
        protocol_version="1.0",
        request_id="request-1",
        method="system.handshake",
        params={
            "client_name": "pytest",
            "client_version": "1.0",
            "supported_protocol_versions": ["1.0"],
            "client_capabilities": [],
            "frontend_type": "test",
            "supported_frame_types": ["json-v1"],
        },
        client_id="client-1",
    )
    result = dispatcher._handle_handshake(request, state)
    assert result["development_revision"] == "phase93-test"
    assert result["api_implementation_version"] == API_IMPLEMENTATION_VERSION
    assert result["daemon_started_at"]

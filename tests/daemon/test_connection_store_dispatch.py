"""Connection-store dispatch propagation tests.

Proves ``MUTATION_AMBIGUOUS`` survives from the application service through the
daemon dispatcher as a structured ``SshPilotError`` with the stable contract
error code preserved.
"""

from unittest import mock

import pytest

from sshpilot.api.capabilities import Capability
from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ClientId, RequestId
from sshpilot.api.models.connections import CreateConnectionRequest
from sshpilot.api.transport.envelopes import HandshakeRequest, RequestEnvelope
from sshpilot.core.connection_application_service import (
    ConnectionApplicationService,
)
from sshpilot.core.errors import CoreError
from sshpilot.daemon.dispatch import (
    ClientProtocolState,
    DeferredResult,
    RequestDispatcher,
)

_request_counter = [0]


def _envelope(method, params):
    _request_counter[0] += 1
    return RequestEnvelope(
        protocol_version="1.0",
        request_id=RequestId(f"req-{_request_counter[0]}"),
        method=method,
        params=params,
        client_id=ClientId("client-1"),
    )


def _state() -> ClientProtocolState:
    state = ClientProtocolState()
    state.handshake_completed = True
    state.client_id = ClientId("client-1")
    state.client_info = HandshakeRequest(
        client_name="test",
        client_version="1.0",
        supported_protocol_versions=("1.0",),
        client_capabilities=frozenset(),
        frontend_type="cli",
        supported_frame_types=frozenset(),
    )
    state.selected_protocol_version = "1.0"
    return state


class _AmbiguousConnectionService:
    """A minimal service whose create_connection() always raises ambiguity.

    We avoid the real ``ConnectionApplicationService`` so the test exercises
    only the dispatch mapping/path, not the repository; the propagation under
    test is whether dispatch preserves the error code.
    """

    def __init__(self):
        self.create_calls = []

    # Capability negotiation surface used by the dispatcher.
    def _advertised_capabilities(self):
        return {Capability.CONNECTIONS_READ, Capability.CONNECTIONS_WRITE}

    def create_connection(self, request):
        assert isinstance(request, CreateConnectionRequest)
        self.create_calls.append(request)
        raise SshPilotError(
            ErrorCode.MUTATION_AMBIGUOUS,
            "The connection-store change may have completed",
        )


def _dispatcher(service=None):
    if service is None:
        service = _AmbiguousConnectionService()
    dispatcher = RequestDispatcher(service)
    return dispatcher, service


# ---------------------------------------------------------------------------
# Method mapping
# ---------------------------------------------------------------------------
def test_create_is_registered():
    dispatcher, _service = _dispatcher()
    assert "connections.create" in dispatcher.HANDLERS


def test_create_maps_to_write_capability():
    from sshpilot.daemon.dispatch import DAEMON_METHOD_CAPABILITIES
    assert (
        DAEMON_METHOD_CAPABILITIES["connections.create"]
        is Capability.CONNECTIONS_WRITE
    )


# ---------------------------------------------------------------------------
# Ambiguity propagation through dispatch
# ---------------------------------------------------------------------------
def test_create_dispatch_surfaces_mutation_ambiguous():
    dispatcher, service = _dispatcher()
    request = CreateConnectionRequest(
        nickname="new", hostname="example.test",
    )
    params = {
        "nickname": request.nickname,
        "hostname": request.hostname,
        "username": request.username,
        "port": request.port,
        "protocol": request.protocol,
        "config_patch": dict(request.config_patch),
        "plugin_data": dict(request.plugin_data),
    }
    result = dispatcher.dispatch(_envelope("connections.create", params), _state())
    assert isinstance(result, DeferredResult)

    with pytest.raises(SshPilotError) as excinfo:
        result.operation()
    assert excinfo.value.code is ErrorCode.MUTATION_AMBIGUOUS
    assert service.create_calls == [request]


def test_core_mutation_ambiguous_is_mapped_to_api_ambiguous():
    """When the application service raises a ``CoreError(MUTATION_AMBIGUOUS)``,
    the dispatcher surfaces it as ``SshPilotError(MUTATION_AMBIGUOUS)`` via
    ``_map_core_error`` (installed on ``ConnectionApplicationService``).
    """
    from sshpilot.core.connection_application_service import _map_core_error
    from sshpilot.core.errors import CoreError as _CoreError, ErrorCode as _CoreErrorCode
    api_error = _map_core_error(
        _CoreError(_CoreErrorCode.MUTATION_AMBIGUOUS, "ambiguous")
    )
    assert api_error.code is ErrorCode.MUTATION_AMBIGUOUS


def test_real_application_service_propagates_mutation_ambiguous(tmp_path):
    """End-to-end propagation through the real application service.

    Drive a real ``ConnectionApplicationService`` over a repository whose SSH
    file is swapped between writer completion and receipt verification, so the
    repository raises ``MUTATION_AMBIGUOUS``; the application service maps
    that to an API ``SshPilotError(MUTATION_AMBIGUOUS)``.
    """
    from pathlib import Path
    from sshpilot.core.connections.repository import ConnectionRepository
    from sshpilot.core.connections.ssh_config_store import SshConfigStore

    root = tmp_path / "ssh_config"
    root.write_text("Host web\n    HostName example.com\n")
    repo = ConnectionRepository(
        ssh_store=SshConfigStore(root),
        state_path=tmp_path / "connections.json",
        legacy_config_path=tmp_path / "config.json",
        isolated=False,
    )

    trigger = {"fired": False}
    original = repo._verify_write_receipt_locked

    def fail_after_ssh_write(captured, receipt):
        if not trigger["fired"] and receipt is not None and receipt.path == Path(root):
            trigger["fired"] = True
            raise CoreError(
                __import__(
                    "sshpilot.core.errors", fromlist=["ErrorCode"]
                ).ErrorCode.MUTATION_AMBIGUOUS,
                "ambiguous after write",
            )
        return original(captured, receipt)

    repo._verify_write_receipt_locked = fail_after_ssh_write

    daemon_service = ConnectionApplicationService(
        repo, client_name="test", allow_cross_thread_commands=True,
    )
    request = CreateConnectionRequest(nickname="new", hostname="example.test")
    with pytest.raises(SshPilotError) as excinfo:
        daemon_service.create_connection(request)
    assert excinfo.value.code is ErrorCode.MUTATION_AMBIGUOUS
    assert trigger["fired"]
from __future__ import annotations

from unittest import mock

import pytest

from sshpilot.api.capabilities import Capability
from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ClientId, RequestId
from sshpilot.api.transport.envelopes import HandshakeRequest, RequestEnvelope
from sshpilot.core.connection_application_service import ConnectionApplicationService
from sshpilot.daemon.dispatch import (
    ClientProtocolState,
    DAEMON_METHOD_CAPABILITIES,
    RequestDispatcher,
)
from sshpilot.daemon.operation_runtime import OperationRuntime


def _state() -> ClientProtocolState:
    state = ClientProtocolState()
    state.handshake_completed = True
    state.client_id = ClientId("client-operation")
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


def _request(method: str, params: dict) -> RequestEnvelope:
    return RequestEnvelope(
        protocol_version="1.0",
        request_id=RequestId("request-operation"),
        method=method,
        params=params,
        client_id=ClientId("client-operation"),
    )


def test_operation_dispatch_methods_use_identity_capabilities():
    assert DAEMON_METHOD_CAPABILITIES["operations.get"] is Capability.IDENTITY_READ
    assert DAEMON_METHOD_CAPABILITIES["operations.cancel"] is Capability.IDENTITY_OPERATE


def test_operation_dispatch_without_runtime_is_unsupported():
    dispatcher = RequestDispatcher(ConnectionApplicationService(mock.Mock(), client_name="test"))
    with pytest.raises(SshPilotError) as error:
        dispatcher.dispatch(_request("operations.get", {"operation_id": "operation-missing"}), _state())
    assert error.value.code is ErrorCode.UNSUPPORTED_CAPABILITY


def test_operation_dispatch_unknown_id_is_typed_not_found():
    dispatcher = RequestDispatcher(
        ConnectionApplicationService(mock.Mock(), client_name="test"),
        operation_runtime=OperationRuntime(),
    )
    try:
        with pytest.raises(SshPilotError) as error:
            dispatcher.dispatch(
                _request("operations.get", {"operation_id": "operation-missing"}),
                _state(),
            )
        assert error.value.code is ErrorCode.OPERATION_NOT_FOUND
    finally:
        dispatcher._operation_runtime.shutdown()

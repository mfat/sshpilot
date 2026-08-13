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
from sshpilot.api.models.operations import OperationKind
from sshpilot.daemon.operation_runtime import OperationRuntime


def _state(client_id: str = "client-operation") -> ClientProtocolState:
    state = ClientProtocolState()
    state.handshake_completed = True
    state.client_id = ClientId(client_id)
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


_REQUEST_SEQ = 0


def _request(method: str, params: dict, *, client_id: str = "client-operation") -> RequestEnvelope:
    global _REQUEST_SEQ
    _REQUEST_SEQ += 1
    return RequestEnvelope(
        protocol_version="1.0",
        request_id=RequestId(f"request-operation-{_REQUEST_SEQ}"),
        method=method,
        params=params,
        client_id=ClientId(client_id),
    )


def test_operation_dispatch_methods_use_generic_operation_capabilities():
    # Operations are shared infrastructure (SFTP directory-size/recursive
    # copy/move/remove use them too), so they must not be coupled to the
    # identity service -- an SFTP-only daemon has no identity service at all
    # but still creates and owns operations.
    assert DAEMON_METHOD_CAPABILITIES["operations.get"] is Capability.OPERATIONS_READ
    assert (
        DAEMON_METHOD_CAPABILITIES["operations.cancel"]
        is Capability.OPERATIONS_CONTROL
    )


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


def test_operation_owner_can_get_and_cancel_own_operation():
    runtime = OperationRuntime()
    dispatcher = RequestDispatcher(
        ConnectionApplicationService(mock.Mock(), client_name="test"),
        operation_runtime=runtime,
    )
    try:
        release = pytest.importorskip("threading").Event()
        started = runtime.start_operation(
            OperationKind.KEY_DEPLOYMENT,
            lambda _handle: (release.wait(2), "done")[1],
            owner_client_id=ClientId("client-operation"),
        )
        result = dispatcher.dispatch(
            _request("operations.get", {"operation_id": str(started.operation_id)}),
            _state("client-operation"),
        )
        assert result.value["operation_id"] == str(started.operation_id)

        result = dispatcher.dispatch(
            _request("operations.cancel", {"operation_id": str(started.operation_id)}),
            _state("client-operation"),
        )
        assert result.value["state"] in {"running", "cancelled"}
        release.set()
    finally:
        dispatcher._operation_runtime.shutdown()


def test_operation_get_and_cancel_are_owner_gated():
    runtime = OperationRuntime()
    dispatcher = RequestDispatcher(
        ConnectionApplicationService(mock.Mock(), client_name="test"),
        operation_runtime=runtime,
    )
    try:
        started = runtime.start_operation(
            OperationKind.KEY_DEPLOYMENT,
            lambda _handle: "done",
            owner_client_id=ClientId("client-operation"),
        )
        with pytest.raises(SshPilotError) as error:
            dispatcher.dispatch(
                _request(
                    "operations.get",
                    {"operation_id": str(started.operation_id)},
                    client_id="client-intruder",
                ),
                _state("client-intruder"),
            )
        assert error.value.code is ErrorCode.SERVICE_OWNER_REQUIRED

        with pytest.raises(SshPilotError) as error:
            dispatcher.dispatch(
                _request(
                    "operations.cancel",
                    {"operation_id": str(started.operation_id)},
                    client_id="client-intruder",
                ),
                _state("client-intruder"),
            )
        assert error.value.code is ErrorCode.SERVICE_OWNER_REQUIRED
    finally:
        dispatcher._operation_runtime.shutdown()


def test_operation_with_no_owner_is_not_visible_to_any_client():
    runtime = OperationRuntime()
    dispatcher = RequestDispatcher(
        ConnectionApplicationService(mock.Mock(), client_name="test"),
        operation_runtime=runtime,
    )
    try:
        started = runtime.start_operation(
            OperationKind.KEY_DEPLOYMENT,
            lambda _handle: "done",
        )
        with pytest.raises(SshPilotError) as error:
            dispatcher.dispatch(
                _request("operations.get", {"operation_id": str(started.operation_id)}),
                _state("client-operation"),
            )
        assert error.value.code is ErrorCode.SERVICE_OWNER_REQUIRED
    finally:
        dispatcher._operation_runtime.shutdown()

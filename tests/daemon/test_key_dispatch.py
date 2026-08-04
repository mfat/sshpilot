"""Daemon SSH-key dispatch RPC tests."""

from unittest import mock

import pytest

from sshpilot.api.capabilities import Capability
from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ClientId, RequestId
from sshpilot.api.models.keys import (
    KeyId,
    KeyList,
    KeyStoreScope,
    KeySummary,
    ListKeysRequest,
)
from sshpilot.api.transport.envelopes import HandshakeRequest, RequestEnvelope
from sshpilot.core.connection_application_service import ConnectionApplicationService
from sshpilot.daemon.dispatch import (
    ClientProtocolState,
    DeferredResult,
    RequestDispatcher,
    DAEMON_METHOD_CAPABILITIES,
    DEFERRED_DAEMON_METHODS,
    DRAIN_REJECTED_METHODS,
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


def _summary(key_id="key-1", name="id_ed25519"):
    return KeySummary(
        key_id=KeyId(key_id),
        name=name,
        private_path=f"/home/user/.ssh/{name}",
        public_path=f"/home/user/.ssh/{name}.pub",
        public_key_available=True,
    )


class _FakeKeyService:
    def __init__(self):
        self.list_calls: list[KeyStoreScope] = []
        self.result = KeyList(keys=(_summary(),))

    def list_keys(self, request: ListKeysRequest):
        self.list_calls.append(request.scope)
        return self.result


def _dispatcher(with_service=True):
    connections = ConnectionApplicationService(mock.Mock(), client_name="test")
    service = _FakeKeyService() if with_service else None
    return RequestDispatcher(connections, key_service=service), service


# ---------------------------------------------------------------------------
# Method mapping
# ---------------------------------------------------------------------------
def test_list_maps_to_read_capability():
    assert DAEMON_METHOD_CAPABILITIES["keys.list"] is Capability.KEYS_READ
    assert "keys.list" in DEFERRED_DAEMON_METHODS
    assert "keys.list" not in DRAIN_REJECTED_METHODS


def test_list_handler_is_registered():
    dispatcher, _service = _dispatcher()
    assert "keys.list" in dispatcher.HANDLERS


# ---------------------------------------------------------------------------
# Capability advertisement
# ---------------------------------------------------------------------------
def test_installed_service_advertises_read():
    dispatcher, _service = _dispatcher()
    capabilities = dispatcher._capabilities_for(_state())
    assert Capability.KEYS_READ in capabilities.supported
    # Write is not advertised until the generate RPC lands.
    assert Capability.KEYS_WRITE not in capabilities.supported


def test_missing_service_does_not_advertise_read():
    dispatcher, _service = _dispatcher(with_service=False)
    capabilities = dispatcher._capabilities_for(_state())
    assert Capability.KEYS_READ not in capabilities.supported
    assert Capability.KEYS_WRITE not in capabilities.supported


# ---------------------------------------------------------------------------
# Dispatch behaviour
# ---------------------------------------------------------------------------
def test_list_rejects_empty_params_with_malformed_scope():
    dispatcher, _service = _dispatcher()
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(_envelope("keys.list", {}), _state())
    assert excinfo.value.code is ErrorCode.INVALID_REQUEST


def test_list_rejects_extra_fields():
    dispatcher, _service = _dispatcher()
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(
            _envelope("keys.list", {"scope": "default", "extra": 1}),
            _state(),
        )
    assert excinfo.value.code is ErrorCode.INVALID_REQUEST


def test_list_rejects_malformed_scope_value():
    dispatcher, _service = _dispatcher()
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(_envelope("keys.list", {"scope": "weird"}), _state())
    assert excinfo.value.code is ErrorCode.INVALID_REQUEST


def test_list_missing_service_raises_unsupported_capability():
    dispatcher, _service = _dispatcher(with_service=False)
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(_envelope("keys.list", {"scope": "default"}), _state())
    assert excinfo.value.code is ErrorCode.UNSUPPORTED_CAPABILITY


def test_list_returns_deferred_execution_with_wire_result():
    dispatcher, service = _dispatcher()
    result = dispatcher.dispatch(
        _envelope("keys.list", {"scope": "default"}),
        _state(),
    )
    assert isinstance(result, DeferredResult)
    assert service.list_calls == []  # deferred: not executed yet
    wire = result.operation()
    assert service.list_calls == [KeyStoreScope.DEFAULT]
    assert wire == {
        "keys": [
            {
                "key_id": "key-1",
                "name": "id_ed25519",
                "private_path": "/home/user/.ssh/id_ed25519",
                "public_path": "/home/user/.ssh/id_ed25519.pub",
                "public_key_available": True,
            }
        ]
    }


def test_list_allowed_during_drain():
    dispatcher, _service = _dispatcher()
    dispatcher.begin_shutdown()
    result = dispatcher.dispatch(
        _envelope("keys.list", {"scope": "default"}),
        _state(),
    )
    assert isinstance(result, DeferredResult)


def test_same_scope_operations_share_command_key():
    dispatcher, _service = _dispatcher()
    first = dispatcher.dispatch(
        _envelope("keys.list", {"scope": "default"}),
        _state(),
    )
    second = dispatcher.dispatch(
        _envelope("keys.list", {"scope": "default"}),
        _state(),
    )
    assert first.command_key == second.command_key
    assert first.command_key == ("keys", "default")


def test_different_scopes_have_distinct_command_keys():
    dispatcher, _service = _dispatcher()
    default_result = dispatcher.dispatch(
        _envelope("keys.list", {"scope": "default"}),
        _state(),
    )
    isolated_result = dispatcher.dispatch(
        _envelope("keys.list", {"scope": "isolated"}),
        _state(),
    )
    assert default_result.command_key == ("keys", "default")
    assert isolated_result.command_key == ("keys", "isolated")
    assert default_result.command_key != isolated_result.command_key

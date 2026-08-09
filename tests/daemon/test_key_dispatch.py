"""Daemon SSH-key dispatch RPC tests."""

from unittest import mock

import pytest

from sshpilot.api.capabilities import Capability
from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ClientId, RequestId
from sshpilot.api.models.keys import (
    GenerateKeyRequest,
    GenerateKeyResult,
    KeyId,
    KeyList,
    KeyStoreScope,
    KeySummary,
    ListKeysRequest,
    PublicKeyResult,
    ReadPublicKeyRequest,
    VerifyKeyPassphraseRequest,
    VerifyKeyPassphraseResult,
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
        self.read_calls: list[ReadPublicKeyRequest] = []
        self.generate_calls: list[GenerateKeyRequest] = []
        self.verify_calls: list[VerifyKeyPassphraseRequest] = []
        self.result = KeyList(keys=(_summary(),))
        self.public_result = PublicKeyResult(
            key_id=KeyId("key-1"),
            text="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI public\n",
        )

    def list_keys(self, request: ListKeysRequest):
        self.list_calls.append(request.scope)
        return self.result

    def read_public_key(self, request: ReadPublicKeyRequest):
        self.read_calls.append(request)
        return self.public_result

    def generate_key(self, request: GenerateKeyRequest, *, owner_client_id):
        self.generate_calls.append((request, owner_client_id))
        return GenerateKeyResult(key=_summary())

    def verify_key_passphrase(self, request, *, owner_client_id):
        self.verify_calls.append((request, owner_client_id))
        return VerifyKeyPassphraseResult(valid=True)


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


def test_get_public_maps_to_read_capability():
    assert DAEMON_METHOD_CAPABILITIES["keys.get_public"] is Capability.KEYS_READ
    assert "keys.get_public" in DEFERRED_DAEMON_METHODS
    assert "keys.get_public" not in DRAIN_REJECTED_METHODS


def test_generate_maps_to_write_capability():
    assert DAEMON_METHOD_CAPABILITIES["keys.generate"] is Capability.KEYS_WRITE
    assert "keys.generate" in DEFERRED_DAEMON_METHODS
    assert "keys.generate" in DRAIN_REJECTED_METHODS


def test_verify_passphrase_maps_to_write_capability():
    assert DAEMON_METHOD_CAPABILITIES["keys.verify_passphrase"] is Capability.KEYS_WRITE
    assert "keys.verify_passphrase" in DEFERRED_DAEMON_METHODS
    assert "keys.verify_passphrase" in DRAIN_REJECTED_METHODS


def test_list_handler_is_registered():
    dispatcher, _service = _dispatcher()
    assert "keys.list" in dispatcher.HANDLERS
    assert "keys.get_public" in dispatcher.HANDLERS
    assert "keys.generate" in dispatcher.HANDLERS
    assert "keys.verify_passphrase" in dispatcher.HANDLERS


# ---------------------------------------------------------------------------
# Capability advertisement
# ---------------------------------------------------------------------------
def test_installed_service_advertises_read_and_write():
    dispatcher, _service = _dispatcher()
    capabilities = dispatcher._capabilities_for(_state())
    assert Capability.KEYS_READ in capabilities.supported
    assert Capability.KEYS_WRITE in capabilities.supported


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


# ---------------------------------------------------------------------------
# keys.get_public dispatch behaviour
# ---------------------------------------------------------------------------
def test_get_public_returns_deferred_execution_with_wire_result():
    dispatcher, service = _dispatcher()
    result = dispatcher.dispatch(
        _envelope("keys.get_public", {"key_id": "key-1", "scope": "default"}),
        _state(),
    )
    assert isinstance(result, DeferredResult)
    assert service.read_calls == []  # deferred: not executed yet
    wire = result.operation()
    assert service.read_calls == [
        ReadPublicKeyRequest(key_id=KeyId("key-1"), scope=KeyStoreScope.DEFAULT)
    ]
    assert wire == {
        "key_id": "key-1",
        "text": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI public\n",
    }


def test_get_public_rejects_missing_fields():
    dispatcher, _service = _dispatcher()
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(
            _envelope("keys.get_public", {"scope": "default"}),
            _state(),
        )
    assert excinfo.value.code is ErrorCode.INVALID_REQUEST


def test_get_public_rejects_extra_fields():
    dispatcher, _service = _dispatcher()
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(
            _envelope(
                "keys.get_public",
                {"key_id": "key-1", "scope": "default", "path": "/etc"},
            ),
            _state(),
        )
    assert excinfo.value.code is ErrorCode.INVALID_REQUEST


def test_get_public_missing_service_raises_unsupported_capability():
    dispatcher, _service = _dispatcher(with_service=False)
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(
            _envelope("keys.get_public", {"key_id": "key-1", "scope": "default"}),
            _state(),
        )
    assert excinfo.value.code is ErrorCode.UNSUPPORTED_CAPABILITY


def test_get_public_allowed_during_drain():
    dispatcher, _service = _dispatcher()
    dispatcher.begin_shutdown()
    result = dispatcher.dispatch(
        _envelope("keys.get_public", {"key_id": "key-1", "scope": "default"}),
        _state(),
    )
    assert isinstance(result, DeferredResult)


def test_get_public_command_key_matches_list_for_same_scope():
    dispatcher, _service = _dispatcher()
    list_result = dispatcher.dispatch(
        _envelope("keys.list", {"scope": "default"}),
        _state(),
    )
    read_result = dispatcher.dispatch(
        _envelope("keys.get_public", {"key_id": "key-1", "scope": "default"}),
        _state(),
    )
    assert list_result.command_key == read_result.command_key == ("keys", "default")


# ---------------------------------------------------------------------------
# keys.generate dispatch behaviour
# ---------------------------------------------------------------------------
def _generate_wire():
    return {
        "name": "id_ed25519",
        "key_type": "ed25519",
        "key_size": 0,
        "comment": "",
        "encrypted": False,
        "scope": "default",
    }


def test_generate_returns_deferred_execution_with_wire_result():
    dispatcher, service = _dispatcher()
    result = dispatcher.dispatch(
        _envelope("keys.generate", _generate_wire()),
        _state(),
    )
    assert isinstance(result, DeferredResult)
    assert service.generate_calls == []  # deferred: not executed yet
    wire = result.operation()
    assert service.generate_calls == [(
        GenerateKeyRequest(name="id_ed25519", scope=KeyStoreScope.DEFAULT),
        ClientId("client-1"),
    )]
    assert wire == {
        "key": {
            "key_id": "key-1",
            "name": "id_ed25519",
            "private_path": "/home/user/.ssh/id_ed25519",
            "public_path": "/home/user/.ssh/id_ed25519.pub",
            "public_key_available": True,
        }
    }
    assert result.command_key == ("keys", "default")


def test_generate_rejects_missing_fields():
    dispatcher, _service = _dispatcher()
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(
            _envelope("keys.generate", {"name": "id_ed25519"}),
            _state(),
        )
    assert excinfo.value.code is ErrorCode.INVALID_REQUEST


def test_generate_rejects_extra_fields():
    dispatcher, _service = _dispatcher()
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(
            _envelope("keys.generate", {**_generate_wire(), "path": "/x"}),
            _state(),
        )
    assert excinfo.value.code is ErrorCode.INVALID_REQUEST


def test_generate_missing_service_raises_unsupported_capability():
    dispatcher, _service = _dispatcher(with_service=False)
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(_envelope("keys.generate", _generate_wire()), _state())
    assert excinfo.value.code is ErrorCode.UNSUPPORTED_CAPABILITY


def test_generate_rejected_during_drain():
    dispatcher, _service = _dispatcher()
    dispatcher.begin_shutdown()
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(_envelope("keys.generate", _generate_wire()), _state())
    assert excinfo.value.code is ErrorCode.DAEMON_SHUTTING_DOWN


def test_list_and_read_remain_available_during_drain():
    dispatcher, _service = _dispatcher()
    dispatcher.begin_shutdown()
    assert isinstance(
        dispatcher.dispatch(_envelope("keys.list", {"scope": "default"}), _state()),
        DeferredResult,
    )


def test_verify_passphrase_dispatch_is_secret_free():
    dispatcher, service = _dispatcher()
    params = {
        "key_path": "/home/user/.ssh/id_ed25519",
        "interaction_scope_id": "key-operation-verify-1",
    }
    result = dispatcher.dispatch(
        _envelope("keys.verify_passphrase", params),
        _state(),
    )
    assert isinstance(result, DeferredResult)
    assert service.verify_calls == []
    assert result.operation() == {"valid": True}
    assert service.verify_calls == [(
        VerifyKeyPassphraseRequest(
            key_path="/home/user/.ssh/id_ed25519",
            interaction_scope_id="key-operation-verify-1",
        ),
        ClientId("client-1"),
    )]
    assert "passphrase" not in params
    assert isinstance(
        dispatcher.dispatch(
            _envelope("keys.get_public", {"key_id": "key-1", "scope": "default"}),
            _state(),
        ),
        DeferredResult,
    )

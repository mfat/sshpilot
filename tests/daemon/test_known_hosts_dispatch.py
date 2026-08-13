"""Known-hosts daemon dispatch RPC tests."""

from unittest import mock

import pytest

from sshpilot.api.capabilities import Capability
from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ClientId, RequestId
from sshpilot.api.transport.envelopes import HandshakeRequest, RequestEnvelope
from sshpilot.core.connection_application_service import ConnectionApplicationService
from sshpilot.core.known_hosts.document import parse_known_hosts_document
from sshpilot.daemon.dispatch import (
    ClientProtocolState,
    DeferredResult,
    RequestDispatcher,
    DAEMON_METHOD_CAPABILITIES,
    DEFERRED_DAEMON_METHODS,
    DRAIN_REJECTED_METHODS,
)
from sshpilot.daemon.known_hosts_service import KnownHostsService

CONTENT = b"one.test ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIone\n"

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


def _dispatcher(tmp_path, *, with_service=True):
    connections = ConnectionApplicationService(mock.Mock(), client_name="test")
    service = (
        KnownHostsService(lambda: tmp_path / "known_hosts")
        if with_service
        else None
    )
    return RequestDispatcher(connections, known_hosts_service=service), service


# ---------------------------------------------------------------------------
# Method mapping
# ---------------------------------------------------------------------------
def test_list_maps_to_read_capability():
    assert (
        DAEMON_METHOD_CAPABILITIES["known_hosts.list"]
        is Capability.KNOWN_HOSTS_READ
    )
    assert "known_hosts.list" in DEFERRED_DAEMON_METHODS
    assert "known_hosts.list" not in DRAIN_REJECTED_METHODS


def test_remove_maps_to_write_capability():
    assert (
        DAEMON_METHOD_CAPABILITIES["known_hosts.remove"]
        is Capability.KNOWN_HOSTS_WRITE
    )
    assert "known_hosts.remove" in DEFERRED_DAEMON_METHODS
    assert "known_hosts.remove" in DRAIN_REJECTED_METHODS


def test_list_handler_is_registered(tmp_path):
    dispatcher, _service = _dispatcher(tmp_path)
    assert "known_hosts.list" in dispatcher.HANDLERS
    assert "known_hosts.remove" in dispatcher.HANDLERS


# ---------------------------------------------------------------------------
# Capability advertisement
# ---------------------------------------------------------------------------
def test_installed_service_advertises_read(tmp_path):
    dispatcher, _service = _dispatcher(tmp_path)
    capabilities = dispatcher._capabilities_for(_state())
    assert Capability.KNOWN_HOSTS_READ in capabilities.supported
    assert Capability.KNOWN_HOSTS_WRITE in capabilities.supported


def test_missing_service_does_not_advertise_read(tmp_path):
    dispatcher, _service = _dispatcher(tmp_path, with_service=False)
    capabilities = dispatcher._capabilities_for(_state())
    assert Capability.KNOWN_HOSTS_READ not in capabilities.supported
    assert Capability.KNOWN_HOSTS_WRITE not in capabilities.supported


# ---------------------------------------------------------------------------
# Dispatch behaviour
# ---------------------------------------------------------------------------
def test_list_requires_empty_parameters(tmp_path):
    dispatcher, _service = _dispatcher(tmp_path)
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(_envelope("known_hosts.list", {"x": 1}), _state())
    assert excinfo.value.code is ErrorCode.INVALID_REQUEST


def test_list_returns_deferred_execution(tmp_path):
    dispatcher, service = _dispatcher(tmp_path)
    (tmp_path / "known_hosts").write_bytes(CONTENT)
    result = dispatcher.dispatch(_envelope("known_hosts.list", {}), _state())
    assert isinstance(result, DeferredResult)
    assert result.command_key == "known_hosts"
    wire = result.operation()
    assert set(wire) == {"revision", "entries"}
    assert wire["revision"] == parse_known_hosts_document(CONTENT).revision
    assert len(wire["entries"]) == 1
    assert wire["entries"][0]["hostname"] == "one.test"
    assert wire["entries"][0]["display_line"].startswith("one.test")


def test_list_missing_service_raises_unsupported_capability(tmp_path):
    dispatcher, _service = _dispatcher(tmp_path, with_service=False)
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(_envelope("known_hosts.list", {}), _state())
    assert excinfo.value.code is ErrorCode.UNSUPPORTED_CAPABILITY


def test_list_remains_available_while_draining(tmp_path):
    dispatcher, _service = _dispatcher(tmp_path)
    (tmp_path / "known_hosts").write_bytes(CONTENT)
    dispatcher.begin_shutdown()
    result = dispatcher.dispatch(_envelope("known_hosts.list", {}), _state())
    assert isinstance(result, DeferredResult)
    assert result.operation()["revision"] == parse_known_hosts_document(
        CONTENT
    ).revision
    # A genuine drain-rejected method is still refused.
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(
            _envelope("connections.create", {"nickname": "x"}), _state()
        )
    assert excinfo.value.code is ErrorCode.DAEMON_SHUTTING_DOWN


# ---------------------------------------------------------------------------
# Removal RPC
# ---------------------------------------------------------------------------
def test_remove_success_executes_to_wire_result(tmp_path):
    dispatcher, _service = _dispatcher(tmp_path)
    (tmp_path / "known_hosts").write_bytes(CONTENT)
    snapshot = _service.list_known_hosts()
    target = snapshot.entries[0].entry_id
    result = dispatcher.dispatch(
        _envelope(
            "known_hosts.remove",
            {"revision": snapshot.revision, "entry_ids": [target]},
        ),
        _state(),
    )
    assert isinstance(result, DeferredResult)
    assert result.command_key == "known_hosts"
    assert result.respond_on_accept is False
    wire = result.operation()
    assert set(wire) == {"revision", "removed_count", "entries"}
    assert wire["removed_count"] == 1
    assert wire["entries"] == []
    assert (tmp_path / "known_hosts").read_bytes() == b""


def test_remove_stale_error_propagates(tmp_path):
    dispatcher, service = _dispatcher(tmp_path)
    (tmp_path / "known_hosts").write_bytes(CONTENT)
    snapshot = service.list_known_hosts()
    result = dispatcher.dispatch(
        _envelope(
            "known_hosts.remove",
            {
                "revision": "stale-revision",
                "entry_ids": [snapshot.entries[0].entry_id],
            },
        ),
        _state(),
    )
    with pytest.raises(SshPilotError) as excinfo:
        result.operation()
    assert excinfo.value.code is ErrorCode.STALE_EDITOR
    assert excinfo.value.retryable is True


def test_remove_unknown_id_error_propagates(tmp_path):
    dispatcher, service = _dispatcher(tmp_path)
    (tmp_path / "known_hosts").write_bytes(CONTENT)
    snapshot = service.list_known_hosts()
    result = dispatcher.dispatch(
        _envelope(
            "known_hosts.remove",
            {"revision": snapshot.revision, "entry_ids": ["missing-id"]},
        ),
        _state(),
    )
    with pytest.raises(SshPilotError) as excinfo:
        result.operation()
    assert excinfo.value.code is ErrorCode.VALIDATION_FAILED
    assert (tmp_path / "known_hosts").read_bytes() == CONTENT


def test_remove_rejected_while_draining(tmp_path):
    dispatcher, _service = _dispatcher(tmp_path)
    (tmp_path / "known_hosts").write_bytes(CONTENT)
    dispatcher.begin_shutdown()
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(
            _envelope(
                "known_hosts.remove",
                {"revision": "r", "entry_ids": ["id"]},
            ),
            _state(),
        )
    assert excinfo.value.code is ErrorCode.DAEMON_SHUTTING_DOWN
    assert (tmp_path / "known_hosts").read_bytes() == CONTENT


def test_remove_malformed_request_is_rejected(tmp_path):
    dispatcher, _service = _dispatcher(tmp_path)
    (tmp_path / "known_hosts").write_bytes(CONTENT)
    with pytest.raises(SshPilotError) as excinfo:
        dispatcher.dispatch(
            _envelope("known_hosts.remove", {"revision": "r"}),
            _state(),
        )
    assert excinfo.value.code is ErrorCode.INVALID_REQUEST

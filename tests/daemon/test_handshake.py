import socket

import pytest

from sshpilot import __version__
from sshpilot.api import Capability, ErrorCode, SshPilotError
from sshpilot.api.transport import (
    ErrorResponseEnvelope,
    RequestEnvelope,
    SuccessResponseEnvelope,
    decode_envelope,
    encode_envelope,
    encode_frame,
    receive_frame,
)
from sshpilot.daemon.dispatch import ClientProtocolState, RequestDispatcher


def _send(peer, request):
    peer.sendall(encode_frame(encode_envelope(request)))
    return decode_envelope(receive_frame(peer))


_REQUEST_COUNTER = 0


def _next_request_id() -> str:
    global _REQUEST_COUNTER
    _REQUEST_COUNTER += 1
    return f"request-{_REQUEST_COUNTER}"


def _request(method, params=None, *, request_id=None, version="1.0", client_id="client-1"):
    return RequestEnvelope(
        protocol_version=version,
        request_id=request_id or _next_request_id(),
        method=method,
        params=params or {},
        client_id=client_id,
    )


def _handshake_params(versions=None):
    return {
        "client_name": "pytest",
        "client_version": "1.0",
        "supported_protocol_versions": versions or ["1.0"],
        "client_capabilities": [],
        "frontend_type": "test",
    }


@pytest.fixture
def raw_peer(daemon_factory):
    server, _manager = daemon_factory()
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    peer.settimeout(2)
    peer.connect(str(server.socket_path))
    yield peer, server
    peer.close()


def test_successful_protocol_v1_handshake(raw_peer):
    peer, _server = raw_peer

    response = _send(peer, _request("system.handshake", _handshake_params()))

    assert isinstance(response, SuccessResponseEnvelope)
    assert response.result["selected_protocol_version"] == "1.0"
    assert response.result["daemon_version"] == __version__
    assert response.result["core_version"] == __version__
    assert response.result["daemon_capabilities"] == sorted(
        value
        for value in (
            Capability.CONNECTIONS_EVENTS.value,
            Capability.CONNECTIONS_READ.value,
            Capability.CONNECTIONS_WRITE.value,
            Capability.CONNECTIONS_CONFIG_READ.value,
            Capability.CONNECTIONS_CONFIG_WRITE.value,
            Capability.CONNECTIONS_SECRETS_WRITE.value,
            Capability.CONNECTIONS_SECRETS_STATUS_READ.value,
            Capability.CONNECTIONS_METADATA_WRITE.value,
            Capability.CONNECTIONS_GROUPS.value,
            Capability.CONNECTIONS_SPLIT.value,
            Capability.SESSIONS_EVENTS.value,
            Capability.SESSIONS_READ.value,
            Capability.SESSIONS_WRITE.value,
            Capability.SFTP_READ.value,
            Capability.SFTP_WRITE.value,
            Capability.SFTP_EVENTS.value,
            Capability.SFTP_METADATA.value,
            Capability.SFTP_MUTATE.value,
            Capability.OPERATIONS_READ.value,
            Capability.OPERATIONS_CONTROL.value,
            Capability.TRANSFERS_READ.value,
            Capability.TRANSFERS_WRITE.value,
            Capability.TRANSFERS_EVENTS.value,
            Capability.TRANSFERS_UPLOAD.value,
            Capability.TRANSFERS_DOWNLOAD.value,
            Capability.FORWARDS_READ.value,
            Capability.FORWARDS_WRITE.value,
            Capability.FORWARDS_EVENTS.value,
            Capability.FORWARDS_LOCAL.value,
            Capability.FORWARDS_REMOTE.value,
            Capability.FORWARDS_DYNAMIC.value,
            Capability.DAEMON_STATUS.value,
            Capability.DAEMON_CONTROL.value,
            Capability.DAEMON_EVENTS.value,
        )
    )
    assert response.result["server_instance_id"]


def test_reveal_capability_requires_binary_secret_negotiation(raw_peer):
    peer, _server = raw_peer

    response = _send(peer, _request("system.handshake", _handshake_params()))

    assert isinstance(response, SuccessResponseEnvelope)
    capabilities = set(response.result["daemon_capabilities"])
    assert Capability.CONNECTIONS_SECRETS_STATUS_READ.value in capabilities
    assert Capability.CONNECTIONS_SECRETS_REVEAL.value not in capabilities


def test_ordinary_method_before_handshake_is_rejected(raw_peer):
    peer, _server = raw_peer

    response = _send(peer, _request("connections.list"))

    assert isinstance(response, ErrorResponseEnvelope)
    assert response.error.code is ErrorCode.HANDSHAKE_REQUIRED


def test_unknown_method_before_handshake_is_still_handshake_required(raw_peer):
    peer, _server = raw_peer

    response = _send(peer, _request("unknown.method"))

    assert isinstance(response, ErrorResponseEnvelope)
    assert response.error.code is ErrorCode.HANDSHAKE_REQUIRED


def test_duplicate_handshake_is_rejected(raw_peer):
    peer, _server = raw_peer
    _send(peer, _request("system.handshake", _handshake_params()))

    response = _send(peer, _request("system.handshake", _handshake_params()))

    assert isinstance(response, ErrorResponseEnvelope)
    assert response.error.code is ErrorCode.HANDSHAKE_ALREADY_COMPLETED


def test_unsupported_protocol_version_is_rejected(raw_peer):
    peer, _server = raw_peer

    response = _send(
        peer,
        _request(
            "system.handshake",
            _handshake_params(["2.0"]),
            version="2.0",
        ),
    )

    assert isinstance(response, ErrorResponseEnvelope)
    assert response.error.code is ErrorCode.PROTOCOL_VERSION_UNSUPPORTED
    assert "2.0" not in response.error.message


@pytest.mark.parametrize(
    "change",
    [
        {"client_name": ""},
        {"supported_protocol_versions": []},
        {"client_capabilities": {}},
        {"unexpected": True},
    ],
)
def test_malformed_client_metadata_is_safe(raw_peer, change):
    peer, _server = raw_peer
    params = _handshake_params()
    params.update(change)

    response = _send(peer, _request("system.handshake", params))

    assert isinstance(response, ErrorResponseEnvelope)
    assert response.error.code is ErrorCode.INVALID_REQUEST
    assert repr(change) not in response.error.message


def test_duplicate_request_id_is_rejected(raw_peer):
    peer, _server = raw_peer
    request_id = "same-request"
    _send(
        peer,
        _request(
            "system.handshake",
            _handshake_params(),
            request_id=request_id,
        ),
    )

    response = _send(
        peer,
        _request(
            "system.get_capabilities",
            request_id=request_id,
        ),
    )

    assert isinstance(response, ErrorResponseEnvelope)
    assert response.error.code is ErrorCode.PROTOCOL_ERROR


def test_unknown_method_uses_unsupported_method_error(raw_peer):
    peer, _server = raw_peer
    _send(peer, _request("system.handshake", _handshake_params()))

    response = _send(peer, _request("internal.invoke_anything"))

    assert isinstance(response, ErrorResponseEnvelope)
    assert response.error.code is ErrorCode.UNSUPPORTED_METHOD
    assert response.error.details == {}


def test_client_id_must_match_handshake(raw_peer):
    peer, _server = raw_peer
    _send(peer, _request("system.handshake", _handshake_params()))

    response = _send(
        peer,
        _request("system.get_capabilities", client_id="client-2"),
    )

    assert isinstance(response, ErrorResponseEnvelope)
    assert response.error.code is ErrorCode.PROTOCOL_ERROR


def test_dispatcher_rejects_requests_after_shutdown_begins():
    dispatcher = RequestDispatcher(object())
    dispatcher.begin_shutdown()

    with pytest.raises(SshPilotError) as caught:
        dispatcher.dispatch(
            _request("system.handshake", _handshake_params()),
            ClientProtocolState(),
        )

    assert caught.value.code is ErrorCode.DAEMON_SHUTTING_DOWN
    assert caught.value.retryable is True

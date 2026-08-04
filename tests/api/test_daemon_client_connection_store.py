"""Daemon client connection-store propagation tests.

Proves that a structured ``MUTATION_AMBIGUOUS`` error returned by the daemon
(after the repository rejects a write-to-verification race) survives a full
round trip through the API client — the raised ``SshPilotError`` still has
``ErrorCode.MUTATION_AMBIGUOUS`` and is not re-classified as a generic
persistence failure.
"""

import socket
import threading
import time

import pytest

from sshpilot.api import DaemonClient, ErrorCode, EventType, SshPilotError
from sshpilot.api.models import (
    ConnectionId,
    ConnectionSummary,
    CreateConnectionRequest,
)
from sshpilot.api.version import API_IMPLEMENTATION_VERSION
from sshpilot.api.transport import (
    EventEnvelope,
    ErrorResponseEnvelope,
    SuccessResponseEnvelope,
    decode_envelope,
    encode_envelope,
    encode_frame,
    receive_frame,
)
from sshpilot.api.transport.codec import connection_summary_to_wire
from sshpilot.api.transport.envelopes import ErrorData


def _connected_protocol_server(socket_path, action):
    ready = threading.Event()
    release = threading.Event()

    def _send(peer, response):
        peer.sendall(encode_frame(encode_envelope(response)))

    def _serve():
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(socket_path))
        listener.listen()
        ready.set()
        peer, _ = listener.accept()
        try:
            handshake = decode_envelope(receive_frame(peer))
            _send(
                peer,
                SuccessResponseEnvelope(
                    "1.0",
                    handshake.request_id,
                    {
                        "daemon_version": "test",
                        "core_version": "test",
                        "selected_protocol_version": "1.0",
                        "daemon_capabilities": [
                            "connections.events",
                            "connections.read",
                            "connections.write",
                        ],
                        "compatibility_status": "compatible",
                        "server_instance_id": "server-1",
                    },
                ),
            )
            capabilities = decode_envelope(receive_frame(peer))
            _send(
                peer,
                SuccessResponseEnvelope(
                    "1.0",
                    capabilities.request_id,
                    {
                        "protocol_version": "1.0",
                        "api_implementation_version": API_IMPLEMENTATION_VERSION,
                        "client": {
                            "name": "daemon-client",
                            "version": "test",
                            "client_id": capabilities.client_id,
                        },
                        "core": {
                            "name": "test-core",
                            "version": "test",
                            "implementation": "daemon",
                        },
                        "supported": [
                            "connections.events",
                            "connections.read",
                            "connections.write",
                        ],
                        "compatibility": {
                            "compatible": True,
                            "protocol_version": "1.0",
                            "message": "",
                        },
                    },
                ),
            )
            assert release.wait(2)
            action(peer)
        finally:
            peer.close()
            listener.close()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    assert ready.wait(2)
    return thread, release


def test_mutation_ambiguous_error_survives_client_round_trip(tmp_path):
    """An explicit ``MUTATION_AMBIGUOUS`` error response from the daemon is
    re-raised by the client with the original code, message, and retryable flag
    preserved (it must not collapse to a generic persistence failure).
    """
    socket_path = tmp_path / "ambiguous-store.sock"
    requests = []

    def _action(peer):
        request = decode_envelope(receive_frame(peer))
        requests.append(request)
        error = SshPilotError(
            ErrorCode.MUTATION_AMBIGUOUS,
            "The connection-store change may have completed",
            retryable=False,
            request_id=request.request_id,
        )
        peer.sendall(
            encode_frame(
                encode_envelope(
                    ErrorResponseEnvelope(
                        "1.0",
                        request.request_id,
                        ErrorData(
                            code=error.code,
                            message=error.message,
                            details=dict(error.details),
                            retryable=error.retryable,
                            request_id=request.request_id,
                        ),
                    )
                )
            )
        )

    thread, release = _connected_protocol_server(socket_path, _action)
    client = DaemonClient(socket_path=socket_path, client_version="test")
    release.set()

    with pytest.raises(SshPilotError) as caught:
        client.create_connection(
            CreateConnectionRequest(nickname="new", hostname="new.example")
        )
    assert caught.value.code is ErrorCode.MUTATION_AMBIGUOUS
    assert caught.value.retryable is False
    assert "connection-store change" in caught.value.message
    assert caught.value.request_id == requests[0].request_id
    assert [request.method for request in requests] == ["connections.create"]
    client.close()
    thread.join(2)


def test_persistence_failed_error_survives_client_round_trip(tmp_path):
    """Sanity check: a different persistence error is preserved distinctly, so
    the ambiguity test above is meaningful (not just "any error survives").
    """
    socket_path = tmp_path / "persist-store.sock"

    def _action(peer):
        request = decode_envelope(receive_frame(peer))
        error = SshPilotError(
            ErrorCode.PERSISTENCE_FAILED,
            "The connection state could not be stored",
            retryable=True,
            request_id=request.request_id,
        )
        peer.sendall(
            encode_frame(
                encode_envelope(
                    ErrorResponseEnvelope(
                        "1.0",
                        request.request_id,
                        ErrorData(
                            code=error.code,
                            message=error.message,
                            details=dict(error.details),
                            retryable=error.retryable,
                            request_id=request.request_id,
                        ),
                    )
                )
            )
        )

    thread, release = _connected_protocol_server(socket_path, _action)
    client = DaemonClient(socket_path=socket_path, client_version="test")
    release.set()

    with pytest.raises(SshPilotError) as caught:
        client.create_connection(
            CreateConnectionRequest(nickname="new", hostname="new.example")
        )
    assert caught.value.code is ErrorCode.PERSISTENCE_FAILED
    assert caught.value.code is not ErrorCode.MUTATION_AMBIGUOUS
    client.close()
    thread.join(2)
import socket
import threading

import pytest

from sshpilot.api import DaemonClient, ErrorCode, SshPilotError
from sshpilot.api.transport import (
    EventEnvelope,
    SuccessResponseEnvelope,
    decode_envelope,
    encode_envelope,
    encode_frame,
    receive_frame,
)


def _one_shot_server(socket_path, response_factory):
    ready = threading.Event()

    def _serve():
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(socket_path))
        listener.listen()
        ready.set()
        peer, _ = listener.accept()
        try:
            request = decode_envelope(receive_frame(peer))
            response = response_factory(request)
            peer.sendall(encode_frame(encode_envelope(response)))
        finally:
            peer.close()
            listener.close()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    assert ready.wait(2)
    return thread


def test_daemon_client_rejects_unknown_response_id(tmp_path):
    socket_path = tmp_path / "unknown-id.sock"

    def _response(_request):
        return SuccessResponseEnvelope("1.0", "not-the-request", {})

    thread = _one_shot_server(socket_path, _response)

    with pytest.raises(SshPilotError) as caught:
        DaemonClient(socket_path=socket_path)

    assert caught.value.code is ErrorCode.PROTOCOL_ERROR
    thread.join(2)


def test_daemon_client_accepts_event_envelopes_without_confusing_correlation(tmp_path):
    socket_path = tmp_path / "event-before-response.sock"
    ready = threading.Event()

    def _serve():
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(socket_path))
        listener.listen()
        ready.set()
        peer, _ = listener.accept()
        request = decode_envelope(receive_frame(peer))
        event = EventEnvelope("1.0", "connection.updated", 0, {"id": "connection-1"})
        response = SuccessResponseEnvelope(
            "1.0",
            request.request_id,
            {
                "daemon_version": "test",
                "core_version": "test",
                "selected_protocol_version": "1.0",
                "daemon_capabilities": ["connections.read"],
                "compatibility_status": "compatible",
                "server_instance_id": "server-1",
            },
        )
        peer.sendall(
            encode_frame(encode_envelope(event))
            + encode_frame(encode_envelope(response))
        )
        peer.close()
        listener.close()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    assert ready.wait(2)

    with pytest.raises(SshPilotError) as caught:
        # The handshake succeeds after parsing the event; closure during the
        # automatic capability request then becomes a structured transport error.
        DaemonClient(socket_path=socket_path)

    assert caught.value.code is ErrorCode.TRANSPORT_CLOSED
    thread.join(2)

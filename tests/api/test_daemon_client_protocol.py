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
    SuccessResponseEnvelope,
    decode_envelope,
    encode_envelope,
    encode_frame,
    receive_frame,
)
from sshpilot.api.transport.codec import connection_summary_to_wire


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


def _connected_protocol_server(
    socket_path,
    action,
    *,
    api_implementation_version=API_IMPLEMENTATION_VERSION,
    supported=None,
    daemon_capabilities=None,
):
    ready = threading.Event()
    release = threading.Event()

    supported = supported or [
        "connections.events",
        "connections.read",
        "connections.write",
    ]
    daemon_capabilities = daemon_capabilities or supported

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
                        "daemon_capabilities": list(daemon_capabilities),
                        "compatibility_status": "compatible",
                        "server_instance_id": "server-1",
                        "api_implementation_version": api_implementation_version,
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
                        "api_implementation_version": api_implementation_version,
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
                        "supported": list(supported),
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


def _wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        threading.Event().wait(0.005)
    return bool(predicate())


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
        summary = ConnectionSummary(
            id=ConnectionId("demo"),
            nickname="demo",
            host="demo",
            hostname="example.test",
            username="alice",
            port=22,
        )
        event = EventEnvelope(
            "1.0",
            "connection.updated",
            0,
            connection_summary_to_wire(summary),
        )
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


def test_daemon_client_rejects_non_monotonic_event_sequence(tmp_path):
    socket_path = tmp_path / "non-monotonic-event.sock"
    first_delivered = threading.Event()
    summary = ConnectionSummary(
        id=ConnectionId("demo"),
        nickname="demo",
        host="demo",
        hostname="example.test",
        username="alice",
        port=22,
    )

    def _action(peer):
        event = encode_frame(
            encode_envelope(
                EventEnvelope(
                    "1.0",
                    "connection.updated",
                    7,
                    connection_summary_to_wire(summary),
                )
            )
        )
        peer.sendall(event)
        assert first_delivered.wait(2)
        peer.sendall(event)

    thread, release = _connected_protocol_server(socket_path, _action)
    client = DaemonClient(socket_path=socket_path, client_version="test")
    received = []
    def receive(event):
        received.append(event)
        if event.type is EventType.CONNECTION_UPDATED:
            first_delivered.set()

    subscription = client.subscribe_events(receive)
    release.set()

    assert _wait_until(
        lambda: any(
            event.type is EventType.ERROR_OCCURRED for event in received
        )
    )
    assert received[0].sequence == 7
    assert received[-1].payload["code"] == ErrorCode.PROTOCOL_ERROR.value
    with pytest.raises(SshPilotError) as caught:
        client.list_connections()
    assert caught.value.code is ErrorCode.TRANSPORT_CLOSED
    subscription.close()
    client.close()
    thread.join(2)


def test_connection_write_is_not_retried_after_ambiguous_transport_close(
    tmp_path,
):
    socket_path = tmp_path / "ambiguous-write.sock"
    requests = []

    def _action(peer):
        requests.append(decode_envelope(receive_frame(peer)))
        # The server may have committed here, but closes before its response.

    thread, release = _connected_protocol_server(socket_path, _action)
    client = DaemonClient(socket_path=socket_path, client_version="test")
    release.set()

    with pytest.raises(SshPilotError) as caught:
        client.create_connection(
            CreateConnectionRequest(
                nickname="new",
                hostname="new.example",
            )
        )

    assert caught.value.code is ErrorCode.MUTATION_AMBIGUOUS
    assert caught.value.retryable is False
    assert caught.value.request_id == requests[0].request_id
    assert [request.method for request in requests] == ["connections.create"]
    client.close()
    thread.join(2)


def test_duplicate_response_closes_transport_after_first_correlation(tmp_path):
    socket_path = tmp_path / "duplicate-response.sock"

    def _action(peer):
        request = decode_envelope(receive_frame(peer))
        response = SuccessResponseEnvelope("1.0", request.request_id, [])
        encoded = encode_frame(encode_envelope(response))
        peer.sendall(encoded + encoded)
        threading.Event().wait(0.05)

    thread, release = _connected_protocol_server(socket_path, _action)
    client = DaemonClient(socket_path=socket_path, client_version="test")
    continuity = []
    client.subscribe_events(continuity.append)
    release.set()

    assert client.list_connections() == []
    assert _wait_until(lambda: bool(continuity))
    assert continuity[-1].payload["code"] == ErrorCode.PROTOCOL_ERROR.value
    with pytest.raises(SshPilotError) as caught:
        client.list_connections()
    assert caught.value.code is ErrorCode.TRANSPORT_CLOSED
    client.close()
    thread.join(2)


def test_group_place_rejected_when_daemon_api_implementation_is_older(tmp_path):
    """A client using the new group-placement contract must not treat an old
    API implementation as write-compatible.

    The updated client sends ``expected_generation`` under the existing
    ``groups.place`` RPC, which old strict daemons would not recognize, so the
    write is rejected client-side with the canonical restart error before any
    wire request is sent — no retry, no local fallback."""
    from sshpilot.api.models.connection_store import GroupId, PlaceGroupRequest

    old_api = "0.16"
    assert old_api != API_IMPLEMENTATION_VERSION

    socket_path = tmp_path / "old-api.sock"
    requests = []

    def _action(peer):
        peer.settimeout(0.5)
        try:
            requests.append(decode_envelope(receive_frame(peer)))
        except TimeoutError:
            pass

    thread, release = _connected_protocol_server(
        socket_path,
        _action,
        api_implementation_version=old_api,
        supported=[
            "connections.events",
            "connections.read",
            "connections.write",
            "connections.groups",
        ],
    )
    client = DaemonClient(socket_path=socket_path, client_version="test")
    release.set()

    with pytest.raises(SshPilotError) as caught:
        client.place_group(
            PlaceGroupRequest(
                group_id=GroupId("g"),
                parent_id=None,
                index=0,
                expected_generation=1,
            )
        )
    assert caught.value.code is ErrorCode.API_VERSION_MISMATCH
    assert caught.value.retryable is False

    thread.join(2)
    assert requests == []
    client.close()


def test_recursive_remove_rejected_when_daemon_api_implementation_is_older(tmp_path):
    """A client using the recursive ``sftp.remove`` contract must not treat an
    old API implementation as write-compatible.

    The updated client sends ``recursive`` under the existing ``sftp.remove``
    RPC, which old strict daemons would not recognize, so the recursive write
    is rejected client-side with the canonical restart error before any wire
    request is sent."""
    from sshpilot.api.models.operations import SftpPathRequest

    old_api = "0.18"
    assert old_api != API_IMPLEMENTATION_VERSION

    socket_path = tmp_path / "old-sftp-api.sock"
    requests = []

    def _action(peer):
        peer.settimeout(0.5)
        try:
            requests.append(decode_envelope(receive_frame(peer)))
        except TimeoutError:
            pass

    thread, release = _connected_protocol_server(
        socket_path,
        _action,
        api_implementation_version=old_api,
        supported=[
            "connections.events",
            "connections.read",
            "connections.write",
            "sftp.mutate",
        ],
    )
    client = DaemonClient(socket_path=socket_path, client_version="test")
    release.set()

    with pytest.raises(SshPilotError) as caught:
        client.sftp_remove(
            SftpPathRequest(service_id="sftp-1", path="/tree", recursive=True)
        )
    assert caught.value.code is ErrorCode.API_VERSION_MISMATCH
    assert caught.value.retryable is False

    thread.join(2)
    assert requests == []
    client.close()


def test_non_recursive_remove_allowed_when_daemon_api_implementation_is_older(tmp_path):
    """A plain ``sftp.remove`` (no ``recursive`` field) stays compatible with an
    old API implementation: only the recursive write introduces a new wire
    field, so only that write is gated."""
    from sshpilot.api.models.operations import SftpPathRequest
    from sshpilot.api.transport import decode_envelope

    old_api = "0.18"
    requests = []

    def _action(peer):
        peer.settimeout(0.5)
        try:
            requests.append(decode_envelope(receive_frame(peer)))
            peer.sendall(
                encode_frame(
                    encode_envelope(
                        SuccessResponseEnvelope("1.0", requests[-1].request_id, None)
                    )
                )
            )
        except TimeoutError:
            pass

    socket_path = tmp_path / "old-sftp-simple.sock"
    thread, release = _connected_protocol_server(
        socket_path,
        _action,
        api_implementation_version=old_api,
        supported=[
            "connections.events",
            "connections.read",
            "connections.write",
            "sftp.mutate",
        ],
    )
    client = DaemonClient(socket_path=socket_path, client_version="test")
    release.set()

    client.sftp_remove(SftpPathRequest(service_id="sftp-1", path="/file"))

    thread.join(2)
    assert len(requests) == 1
    assert requests[0].method == "sftp.remove"
    assert "recursive" not in requests[0].params
    client.close()

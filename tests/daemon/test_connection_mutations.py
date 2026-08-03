import socket
import threading
from dataclasses import replace

import pytest

from sshpilot.core.connection_application_service import ConnectionApplicationService
from sshpilot.api import (
    Capability,
    DaemonClient,
    ErrorCode,
    EventType,
    SshPilotError,
)
from sshpilot.api.models import CreateConnectionRequest
from sshpilot.api.models.common import ClientId, RequestId
from sshpilot.api.transport import (
    ErrorResponseEnvelope,
    HandshakeRequest,
    RequestEnvelope,
    decode_envelope,
    encode_envelope,
    encode_frame,
    receive_frame,
)
from sshpilot.api.transport.codec import handshake_request_to_wire
from sshpilot.api.version import PROTOCOL_VERSION


def _send(peer, request):
    peer.sendall(encode_frame(encode_envelope(request)))
    return decode_envelope(receive_frame(peer))


def _send_raw(peer, request):
    peer.sendall(encode_frame(request))
    return decode_envelope(receive_frame(peer))


def _handshaken_peer(socket_path):
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    peer.connect(str(socket_path))
    client_id = ClientId("client:mutation-wire-test")
    response = _send(
        peer,
        RequestEnvelope(
            protocol_version=PROTOCOL_VERSION,
            request_id=RequestId("handshake"),
            method="system.handshake",
            params=handshake_request_to_wire(
                HandshakeRequest(
                    client_name="test",
                    client_version="test",
                    supported_protocol_versions=(PROTOCOL_VERSION,),
                )
            ),
            client_id=client_id,
        ),
    )
    assert response.request_id == "handshake"
    return peer, client_id


def test_requesting_and_observing_clients_receive_one_created_event(
    daemon_factory,
):
    server, _manager = daemon_factory()
    requesting = DaemonClient(socket_path=server.socket_path)
    observing = DaemonClient(socket_path=server.socket_path)
    received = [[], []]
    ready = [threading.Event(), threading.Event()]
    subscriptions = [
        client.subscribe_events(
            lambda event, index=index: (
                received[index].append(event),
                ready[index].set(),
            )
        )
        for index, client in enumerate((requesting, observing))
    ]

    created = requesting.create_connection(
        CreateConnectionRequest(
            nickname="new",
            hostname="new.example",
        )
    )

    assert all(item.wait(2) for item in ready)
    assert [len(events) for events in received] == [1, 1]
    assert all(
        events[0].type is EventType.CONNECTION_CREATED
        for events in received
    )
    assert received[0][0].sequence == received[1][0].sequence
    assert received[0][0].payload.id == created.connection_id
    for subscription in subscriptions:
        subscription.close()
    requesting.close()
    observing.close()


def test_plugin_connection_payload_round_trips_through_daemon(daemon_factory):
    server, manager = daemon_factory()
    client = DaemonClient(socket_path=server.socket_path)

    created = client.create_connection(
        CreateConnectionRequest(
            nickname="serial-demo",
            hostname="/dev/ttyUSB0",
            protocol="serial",
            plugin_data={"baudrate": 115200},
        )
    )

    connection = manager.find_connection_by_nickname("serial-demo")
    assert created.connection_id == "serial-demo"
    assert connection.protocol == "serial"
    assert connection.data["baudrate"] == 115200
    client.close()


def test_duplicate_connection_routes_through_daemon_owner(daemon_factory):
    server, manager = daemon_factory()
    client = DaemonClient(socket_path=server.socket_path)

    duplicated = client.duplicate_connection("demo")

    assert duplicated.connection_id != "demo"
    assert manager.find_connection_by_nickname(duplicated.nickname) is not None
    client.close()


def test_saved_connection_password_lookup_routes_through_daemon_owner(
    daemon_factory,
):
    server, manager = daemon_factory(start=False)
    manager.get_connection_password = lambda connection: (
        "stored-password" if connection.nickname == "demo" else None
    )
    server.start_in_thread()
    client = DaemonClient(socket_path=server.socket_path)

    assert client.lookup_connection_password("demo") == "stored-password"
    client.close()


def test_malformed_secret_bearing_mutation_is_rejected_without_logging_payload(
    daemon_factory,
    caplog,
):
    server, manager = daemon_factory()
    peer, client_id = _handshaken_peer(server.socket_path)
    secret = "never-log-this-password"

    response = _send_raw(
        peer,
        {
            "type": "request",
            "protocol_version": PROTOCOL_VERSION,
            "request_id": "create-bad",
            "method": "connections.create",
            "params": {
                "nickname": "bad",
                "hostname": "bad.example",
                "username": "",
                "port": 22,
                "protocol": "ssh",
                "password": secret,
            },
            "client_id": client_id,
        },
    )

    assert isinstance(response, ErrorResponseEnvelope)
    assert response.error.code is ErrorCode.INVALID_REQUEST
    assert [connection.nickname for connection in manager.connections] == ["demo"]
    assert secret not in caplog.text
    assert secret not in repr(response)
    peer.close()


def test_older_daemon_without_write_capability_fails_locally(
    daemon_factory,
):
    server, manager = daemon_factory(start=False)

    class _ReadOnlyCore:
        def __init__(self):
            self._base = ConnectionApplicationService(manager, client_name="read-only")

        def get_capabilities(self):
            return replace(
                self._base.get_capabilities(),
                supported=frozenset(
                    {
                        Capability.CONNECTIONS_READ,
                        Capability.CONNECTIONS_EVENTS,
                    }
                ),
            )

        def list_connections(self):
            return self._base.list_connections()

        def get_connection(self, connection_id):
            return self._base.get_connection(connection_id)

        def subscribe_events(self, callback):
            return self._base.subscribe_events(callback)

        def close(self):
            self._base.close()

    server._core_factory = _ReadOnlyCore
    server.start_in_thread()
    client = DaemonClient(socket_path=server.socket_path)

    with pytest.raises(SshPilotError) as caught:
        client.create_connection(
            CreateConnectionRequest(
                nickname="new",
                hostname="new.example",
            )
        )

    assert caught.value.code is ErrorCode.UNSUPPORTED_CAPABILITY
    assert caught.value.details == {"capability": "connections.write"}
    client.close()


def test_daemon_update_command_fields_round_trip(daemon_factory):
    """A real daemon mutation carrying RemoteCommand/PreCommand/LocalCommand/
    ProxyJump must persist — a regression for empty command fields silently
    dropping out of the wire payload (see the ``update-command-fields``
    envelope test)."""
    from sshpilot.api.models import UpdateConnectionRequest
    from sshpilot.api.models.common import ConnectionId

    server, manager = daemon_factory()
    client = DaemonClient(socket_path=server.socket_path)

    client.update_connection(
        ConnectionId("demo"),
        UpdateConnectionRequest(
            config_patch={
                "proxy_jump": ["bastion"],
                "pre_command": "",
                "local_command": "",
                "remote_command": "echo hello",
            }
        ),
    )

    connection = manager.find_connection_by_nickname("demo")
    assert connection.data.get("remote_command") == "echo hello"
    assert connection.data.get("local_command") == ""
    assert connection.data.get("pre_command") == ""
    assert connection.data.get("proxy_jump") == ["bastion"]

    # A follow-up update touching only command fields must not drop the
    # previously persisted values.
    client.update_connection(
        ConnectionId("demo"),
        UpdateConnectionRequest(
            config_patch={"remote_command": "echo again"},
        ),
    )
    connection = manager.find_connection_by_nickname("demo")
    assert connection.data.get("remote_command") == "echo again"
    assert connection.data.get("proxy_jump") == ["bastion"]
    client.close()


def test_daemon_update_stale_editor_rejected_when_generation_mismatches(
    daemon_factory,
):
    from sshpilot.api.models import UpdateConnectionRequest
    from sshpilot.api.models.common import ConnectionId

    server, manager = daemon_factory()
    client = DaemonClient(socket_path=server.socket_path)

    connection = manager.find_connection_by_nickname("demo")
    connection.generation = 5

    client.update_connection(
        ConnectionId("demo"),
        UpdateConnectionRequest(
            config_patch={"remote_command": "echo hi"},
            expected_generation=5,
        ),
    )

    # A competing editor advanced the generation before our next save.
    manager.find_connection_by_nickname("demo").generation = 6

    with pytest.raises(SshPilotError) as caught:
        client.update_connection(
            ConnectionId("demo"),
            UpdateConnectionRequest(
                config_patch={"remote_command": "echo stale"},
                expected_generation=5,
            ),
        )
    assert caught.value.code is ErrorCode.STALE_EDITOR
    assert manager.find_connection_by_nickname("demo").data["remote_command"] == "echo hi"
    client.close()

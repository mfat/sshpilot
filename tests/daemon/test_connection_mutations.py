import socket
import threading
import time
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
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if all(
            any(event.type is EventType.CONNECTION_CREATED for event in events)
            and any(event.type is EventType.CONNECTION_STORE_CHANGED for event in events)
            for events in received
        ):
            break
        time.sleep(0.005)
    assert all(
        any(event.type is EventType.CONNECTION_CREATED for event in events)
        and any(event.type is EventType.CONNECTION_STORE_CHANGED for event in events)
        for events in received
    )
    for events in received:
        created_events = [
            event for event in events
            if event.type is EventType.CONNECTION_CREATED
        ]
        store_events = [
            event for event in events
            if event.type is EventType.CONNECTION_STORE_CHANGED
        ]
        assert len(created_events) == 1
        assert len(store_events) == 1
        assert created_events[0].payload.id == created.connection_id
    created_events = [
        event for event in received[0]
        if event.type is EventType.CONNECTION_CREATED
    ]
    observed_created_events = [
        event for event in received[1]
        if event.type is EventType.CONNECTION_CREATED
    ]
    assert created_events[0].sequence == observed_created_events[0].sequence
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


def test_saved_connection_password_status_and_reveal_route_through_daemon_owner(
    daemon_factory,
):
    server, manager = daemon_factory(start=False)
    manager.lookup_connection_password = lambda connection_id: (
        "stored-password" if str(connection_id) == "demo" else None
    )
    manager.has_connection_password = lambda connection_id: (
        str(connection_id) == "demo"
    )
    server.start_in_thread()
    client = DaemonClient(socket_path=server.socket_path)

    assert client.has_connection_password("demo") is True
    revealed = client.reveal_connection_password("demo")
    try:
        assert revealed == bytearray(b"stored-password")
    finally:
        revealed[:] = b"\\0" * len(revealed)
        revealed.clear()
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


def test_daemon_groups_place_stale_generation_rejected(daemon_factory, tmp_path):
    """A stale ``groups.place`` is rejected end-to-end as STALE_EDITOR and the
    persisted group layout is unchanged.

    Exercises client DTO -> codec -> dispatcher -> application service -> real
    repository, which compares the requested generation before mutating."""
    from sshpilot.api.models.connection_store import GroupId, PlaceGroupRequest
    from sshpilot.core.connections.repository import ConnectionRepository
    from sshpilot.core.connections.ssh_config_store import SshConfigStore

    repo = ConnectionRepository(
        ssh_store=SshConfigStore(tmp_path / "ssh_config"),
        state_path=tmp_path / "connections.json",
        legacy_config_path=tmp_path / "config.json",
        isolated=False,
    )
    server, _manager = daemon_factory(manager=repo)
    client = DaemonClient(socket_path=server.socket_path)

    parent_id = client.create_group("Parent")
    child_id = client.create_group("Child")
    assert parent_id and child_id

    # First placement succeeds with the current generation.
    snapshot = client.get_connection_store_snapshot()
    client.place_group(
        PlaceGroupRequest(
            group_id=GroupId(child_id),
            parent_id=GroupId(parent_id),
            index=0,
            expected_generation=snapshot.generation,
        )
    )
    placed = client.get_connection_store_snapshot()
    placed_child = next(g for g in placed.groups if g.id == child_id)
    assert placed_child.parent_id == parent_id

    # A stale generation is rejected before any mutation.
    with pytest.raises(SshPilotError) as caught:
        client.place_group(
            PlaceGroupRequest(
                group_id=GroupId(parent_id),
                parent_id=None,
                index=0,
                expected_generation=snapshot.generation,
            )
        )
    assert caught.value.code is ErrorCode.STALE_EDITOR

    # Persisted group state is unchanged by the rejected placement.
    current = client.get_connection_store_snapshot()
    assert current.generation == placed.generation
    current_child = next(g for g in current.groups if g.id == child_id)
    assert current_child.parent_id == parent_id
    client.close()


def test_delete_key_passphrase_roundtrips_over_daemon(daemon_factory, caplog):
    """Regression: ``connections.delete_passphrase`` must be accepted as deferred work.

    The handler returns a ``DeferredResult``; while the method was missing from
    ``DEFERRED_DAEMON_METHODS`` the dispatcher rejected it as "immediate daemon
    method returned deferred work", surfacing to clients as an opaque
    INTERNAL_ERROR ("The daemon could not complete the request").
    """
    from sshpilot.api.models.connections import (
        DeleteKeyPassphraseRequest,
    )
    from sshpilot.api.models.keys import KeyStoreScope
    from sshpilot.gtk.key_controller import KeyController

    server, _manager = daemon_factory()
    client = DaemonClient(socket_path=server.socket_path)

    key_path = "/tmp/sshpilot-mutation-test-id_ed25519"
    sentinel = "KEY_PASSPHRASE_SENTINEL_8F1C29"
    secret = bytearray(sentinel.encode("utf-8"))
    assert KeyController(client, KeyStoreScope.DEFAULT).store_key_passphrase(
        key_path,
        secret,
    ) is True
    assert secret == bytearray()
    assert client.has_key_passphrase(key_path) is True
    assert client.delete_key_passphrase(
        DeleteKeyPassphraseRequest(key_path=key_path)
    ) is True
    assert client.has_key_passphrase(key_path) is False
    assert sentinel not in caplog.text
    client.close()

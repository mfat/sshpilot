"""DaemonClient SSH-key method tests.

Covers capability gating, exact RPC payloads, strict result decoding,
mutation-ambiguity semantics for key generation, transport failure mapping,
and secret-free diagnostics.
"""
from __future__ import annotations

import logging
import socket
import threading
from pathlib import Path

import pytest

from sshpilot.api import DaemonClient, ErrorCode, SshPilotError
from sshpilot.api.models.keys import (
    GenerateKeyRequest,
    KeyId,
    KeyList,
    KeyStoreScope,
    KeySummary,
    ListKeysRequest,
    ReadPublicKeyRequest,
)
from sshpilot.api.version import API_IMPLEMENTATION_VERSION
from sshpilot.api.transport import (
    SuccessResponseEnvelope,
    decode_envelope,
    encode_envelope,
    encode_frame,
    receive_frame,
)
from sshpilot.daemon import DaemonServer
from sshpilot.daemon.key_service import DaemonKeyService
from sshpilot.daemon.server import CoreServices
from tests.helpers.fake_connection_repository import make_test_connection_service, make_test_repository

PRIVATE_HEADER = (
    b"-----BEGIN OPENSSH PRIVATE KEY-----\n"
    b"b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAFzAAAC\n"
)


def _write_key(root: Path, rel: str, pub_text: str) -> None:
    private = root / rel
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_bytes(PRIVATE_HEADER)
    private.with_suffix(private.suffix + ".pub").write_text(pub_text)


@pytest.fixture
def key_server(tmp_path):
    """Start an in-process daemon with a key service installed."""
    servers = []

    def _start(default_dir: Path):
        socket_path = tmp_path / f"key-sock-{len(servers)}" / "sshpilotd.sock"
        socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

        def _build():
            connections = make_test_connection_service(client_name="sshpilotd")
            service = DaemonKeyService(lambda scope: default_dir)
            return CoreServices(connections=connections, keys=service)

        server = DaemonServer(_build, socket_path=socket_path)
        server.start_in_thread()
        servers.append(server)
        return server

    yield _start
    for server in servers:
        server.shutdown()
        server.wait_stopped()


def _scripted_server(tmp_path, capabilities, action):
    """Run a scripted daemon advertising *capabilities*, then *action(peer)*."""
    socket_path = tmp_path / f"scripted-{threading.get_ident()}.sock"
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    ready = threading.Event()
    release = threading.Event()

    def _serve():
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(socket_path))
        listener.listen()
        ready.set()
        peer, _ = listener.accept()
        try:
            handshake = decode_envelope(receive_frame(peer))
            peer.sendall(
                encode_frame(
                    encode_envelope(
                        SuccessResponseEnvelope(
                            "1.0",
                            handshake.request_id,
                            {
                                "daemon_version": "test",
                                "core_version": "test",
                                "selected_protocol_version": "1.0",
                                "daemon_capabilities": list(capabilities),
                                "compatibility_status": "compatible",
                                "server_instance_id": "server-1",
                            },
                        )
                    )
                )
            )
            caps = decode_envelope(receive_frame(peer))
            peer.sendall(
                encode_frame(
                    encode_envelope(
                        SuccessResponseEnvelope(
                            "1.0",
                            caps.request_id,
                            {
                                "protocol_version": "1.0",
                                "api_implementation_version": API_IMPLEMENTATION_VERSION,
                                "client": {
                                    "name": "daemon-client",
                                    "version": "test",
                                    "client_id": caps.client_id,
                                },
                                "core": {
                                    "name": "test-core",
                                    "version": "test",
                                    "implementation": "daemon",
                                },
                                "supported": list(capabilities),
                                "compatibility": {
                                    "compatible": True,
                                    "protocol_version": "1.0",
                                    "message": "",
                                },
                            },
                        )
                    )
                )
            )
            assert release.wait(3)
            action(peer)
        finally:
            peer.close()
            listener.close()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    assert ready.wait(2)
    return thread, release, socket_path


def _drain_request(peer):
    return decode_envelope(receive_frame(peer))


def test_list_keys_round_trip_over_real_daemon(tmp_path, key_server):
    keys_dir = tmp_path / "keys"
    _write_key(keys_dir, "id_ed25519", "ssh-ed25519 AAAA-public\n")
    server = key_server(keys_dir)
    client = DaemonClient(socket_path=server.socket_path)
    try:
        result = client.list_keys(ListKeysRequest())
        assert type(result) is KeyList
        assert result.keys[0].name == "id_ed25519"
        assert "private_path" not in repr(result.keys[0])
    finally:
        client.close()


def test_read_public_key_round_trip_over_real_daemon(tmp_path, key_server):
    keys_dir = tmp_path / "keys"
    _write_key(keys_dir, "id_ed25519", "ssh-ed25519 AAAA-exact\n")
    server = key_server(keys_dir)
    client = DaemonClient(socket_path=server.socket_path)
    try:
        key_list = client.list_keys(ListKeysRequest())
        result = client.read_public_key(
            ReadPublicKeyRequest(key_id=key_list.keys[0].key_id)
        )
        assert result.text == "ssh-ed25519 AAAA-exact\n"
    finally:
        client.close()


def test_generate_key_round_trip_over_real_daemon(tmp_path, key_server):
    keys_dir = tmp_path / "keys"
    server = key_server(keys_dir)
    client = DaemonClient(socket_path=server.socket_path)
    try:
        result = client.generate_key(
            GenerateKeyRequest(name="generated", passphrase="a-secret")
        )
        assert result.key.name == "generated"
        assert "a-secret" not in repr(result)
        # The generated key now appears in a fresh listing.
        assert any(
            k.name == "generated"
            for k in client.list_keys(ListKeysRequest()).keys
        )
    finally:
        client.close()


def test_list_keys_capability_gated(tmp_path):
    thread, release, socket_path = _scripted_server(
        tmp_path,
        capabilities=("connections.read",),
        action=lambda peer: peer.close(),
    )
    release.set()
    client = DaemonClient(socket_path=socket_path)
    try:
        with pytest.raises(SshPilotError) as excinfo:
            client.list_keys(ListKeysRequest())
        assert excinfo.value.code is ErrorCode.UNSUPPORTED_CAPABILITY
    finally:
        client.close()
        thread.join(2)


def test_read_public_key_capability_gated(tmp_path):
    thread, release, socket_path = _scripted_server(
        tmp_path,
        capabilities=("keys.write",),
        action=lambda peer: peer.close(),
    )
    release.set()
    client = DaemonClient(socket_path=socket_path)
    try:
        with pytest.raises(SshPilotError) as excinfo:
            client.read_public_key(ReadPublicKeyRequest(key_id=KeyId("k")))
        assert excinfo.value.code is ErrorCode.UNSUPPORTED_CAPABILITY
    finally:
        client.close()
        thread.join(2)


def test_generate_key_capability_gated(tmp_path):
    thread, release, socket_path = _scripted_server(
        tmp_path,
        capabilities=("keys.read",),
        action=lambda peer: peer.close(),
    )
    release.set()
    client = DaemonClient(socket_path=socket_path)
    try:
        with pytest.raises(SshPilotError) as excinfo:
            client.generate_key(GenerateKeyRequest(name="x"))
        assert excinfo.value.code is ErrorCode.UNSUPPORTED_CAPABILITY
    finally:
        client.close()
        thread.join(2)


def test_generate_key_sends_exact_payload(tmp_path):
    seen = {}

    def action(peer):
        request = _drain_request(peer)
        seen["method"] = request.method
        seen["params"] = request.params
        peer.close()

    thread, release, socket_path = _scripted_server(
        tmp_path,
        capabilities=("keys.read", "keys.write"),
        action=action,
    )
    client = DaemonClient(socket_path=socket_path)
    release.set()
    try:
        request = GenerateKeyRequest(
            name="id_ed25519",
            key_type="ed25519",
            key_size=0,
            comment="",
            passphrase="secret",
            scope=KeyStoreScope.DEFAULT,
        )
        with pytest.raises(SshPilotError):
            client.generate_key(request)
    finally:
        client.close()
        thread.join(2)
    assert seen["method"] == "keys.generate"
    assert seen["params"]["name"] == "id_ed25519"
    assert seen["params"]["passphrase"] == "secret"
    assert seen["params"]["scope"] == "default"


def test_generate_transport_close_after_send_is_mutation_ambiguous(tmp_path):
    def action(peer):
        _drain_request(peer)  # the keys.generate request was sent
        peer.close()  # close without a response

    thread, release, socket_path = _scripted_server(
        tmp_path,
        capabilities=("keys.read", "keys.write"),
        action=action,
    )
    client = DaemonClient(socket_path=socket_path)
    release.set()
    try:
        with pytest.raises(SshPilotError) as excinfo:
            client.generate_key(GenerateKeyRequest(name="id_ed25519"))
        assert excinfo.value.code is ErrorCode.MUTATION_AMBIGUOUS
        assert "may have completed" in excinfo.value.message
        assert "id_ed25519" not in excinfo.value.message
    finally:
        client.close()
        thread.join(2)


def test_list_transport_close_is_not_mutation_ambiguous(tmp_path):
    def action(peer):
        _drain_request(peer)
        peer.close()

    thread, release, socket_path = _scripted_server(
        tmp_path,
        capabilities=("keys.read",),
        action=action,
    )
    client = DaemonClient(socket_path=socket_path)
    release.set()
    try:
        with pytest.raises(SshPilotError) as excinfo:
            client.list_keys(ListKeysRequest())
        assert excinfo.value.code is not ErrorCode.MUTATION_AMBIGUOUS
    finally:
        client.close()
        thread.join(2)


def test_generate_pre_send_encoding_failure_is_not_mutation_ambiguous(tmp_path):
    thread, release, socket_path = _scripted_server(
        tmp_path,
        capabilities=("keys.write",),
        action=lambda peer: peer.close(),
    )
    release.set()
    client = DaemonClient(socket_path=socket_path)
    try:
        with pytest.raises(TypeError):
            client.generate_key(object())  # type: ignore[arg-type]
    finally:
        client.close()
        thread.join(2)


def test_daemon_unavailable_has_no_local_fallback(tmp_path):
    socket_path = tmp_path / "missing" / "sshpilotd.sock"
    with pytest.raises(SshPilotError) as excinfo:
        DaemonClient(socket_path=socket_path)
    assert excinfo.value.code is ErrorCode.DAEMON_UNAVAILABLE


def test_generate_passphrase_absent_from_logs(tmp_path, key_server, caplog):
    keys_dir = tmp_path / "keys"
    server = key_server(keys_dir)
    client = DaemonClient(socket_path=server.socket_path)
    try:
        with caplog.at_level(logging.DEBUG):
            client.generate_key(
                GenerateKeyRequest(name="k", passphrase="top-secret-passphrase")
            )
        assert "top-secret-passphrase" not in caplog.text
    finally:
        client.close()

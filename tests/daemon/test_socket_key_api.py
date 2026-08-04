"""Socket-level DaemonClient SSH-key API tests.

Proves the keys.list and keys.get_public RPCs over a real in-process daemon:
exact public-key text, error mapping, no path accepted from the client, and no
private-key data returned.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from sshpilot.api import DaemonClient
from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.core.connection_application_service import ConnectionApplicationService
from sshpilot.daemon import DaemonServer
from sshpilot.daemon.key_service import DaemonKeyService
from sshpilot.daemon.server import CoreServices

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

    def _start(default_dir: Path, isolated_dir: Path | None = None):
        socket_path = tmp_path / f"key-sock-{len(servers)}" / "sshpilotd.sock"
        socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

        def _build():
            manager = ConnectionApplicationService(
                object(),
                client_name="sshpilotd",
            )
            resolver = (
                (lambda scope: default_dir)
                if isolated_dir is None
                else (
                    lambda scope: (
                        default_dir
                        if scope.value == "default"
                        else isolated_dir
                    )
                )
            )
            service = DaemonKeyService(resolver)
            return CoreServices(connections=manager, keys=service)

        server = DaemonServer(_build, socket_path=socket_path)
        server.start_in_thread()
        servers.append(server)
        return server

    yield _start
    for server in servers:
        server.shutdown()
        server.wait_stopped()


def _list_keys(client):
    return client._request("keys.list", {"scope": "default"})


def _get_public(client, key_id, scope="default"):
    return client._request("keys.get_public", {"key_id": key_id, "scope": scope})


def test_list_and_get_public_over_socket_return_exact_text(tmp_path, key_server):
    keys_dir = tmp_path / "keys"
    _write_key(
        keys_dir,
        "id_ed25519",
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI exact-comment\n",
    )
    server = key_server(keys_dir)
    client = DaemonClient(socket_path=server.socket_path)
    try:
        key_list = _list_keys(client)
        assert len(key_list["keys"]) == 1
        key_id = key_list["keys"][0]["key_id"]
        result = _get_public(client, key_id)
        assert result == {
            "key_id": key_id,
            "text": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI exact-comment\n",
        }
    finally:
        client.close()


def test_get_public_unknown_id(tmp_path, key_server):
    keys_dir = tmp_path / "keys"
    _write_key(keys_dir, "id_ed25519", "ssh-ed25519 AAAA\n")
    server = key_server(keys_dir)
    client = DaemonClient(socket_path=server.socket_path)
    try:
        with pytest.raises(SshPilotError) as excinfo:
            _get_public(client, "key-unknown")
        assert excinfo.value.code is ErrorCode.KEY_NOT_FOUND
    finally:
        client.close()


def test_get_public_missing_public_file(tmp_path, key_server):
    keys_dir = tmp_path / "keys"
    private = keys_dir / "id_ed25519"
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_bytes(PRIVATE_HEADER)  # no .pub file
    server = key_server(keys_dir)
    client = DaemonClient(socket_path=server.socket_path)
    try:
        key_id = _list_keys(client)["keys"][0]["key_id"]
        with pytest.raises(SshPilotError) as excinfo:
            _get_public(client, key_id)
        assert excinfo.value.code is ErrorCode.KEY_PUBLIC_UNAVAILABLE
    finally:
        client.close()


def test_get_public_rejects_path_parameter(tmp_path, key_server):
    keys_dir = tmp_path / "keys"
    _write_key(keys_dir, "id_ed25519", "ssh-ed25519 AAAA\n")
    server = key_server(keys_dir)
    client = DaemonClient(socket_path=server.socket_path)
    try:
        key_id = _list_keys(client)["keys"][0]["key_id"]
        with pytest.raises(SshPilotError) as excinfo:
            client._request(
                "keys.get_public",
                {"key_id": key_id, "scope": "default", "path": "/etc/passwd"},
            )
        assert excinfo.value.code is ErrorCode.INVALID_REQUEST
    finally:
        client.close()


def test_get_public_returns_no_private_key_data(tmp_path, key_server):
    keys_dir = tmp_path / "keys"
    _write_key(
        keys_dir,
        "id_ed25519",
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI only-public\n",
    )
    server = key_server(keys_dir)
    client = DaemonClient(socket_path=server.socket_path)
    try:
        key_id = _list_keys(client)["keys"][0]["key_id"]
        result = _get_public(client, key_id)
        assert "PRIVATE KEY" not in result["text"]
        assert "BEGIN" not in result["text"]
    finally:
        client.close()


def test_public_key_text_never_appears_in_logs(tmp_path, key_server, caplog):
    keys_dir = tmp_path / "keys"
    secret_marker = "ssh-ed25519 AAAASECRET-MARKER-12345\n"
    _write_key(keys_dir, "id_ed25519", secret_marker)
    server = key_server(keys_dir)
    client = DaemonClient(socket_path=server.socket_path)
    try:
        with caplog.at_level(logging.DEBUG):
            key_id = _list_keys(client)["keys"][0]["key_id"]
            result = _get_public(client, key_id)
            assert result["text"] == secret_marker
            # A failed read must also not leak text (unknown id path).
            with pytest.raises(SshPilotError):
                _get_public(client, "key-unknown")
        assert "AAAASECRET-MARKER-12345" not in caplog.text
    finally:
        client.close()


def test_isolated_scope_listing_over_socket(tmp_path, key_server):
    default_dir = tmp_path / "default"
    isolated_dir = tmp_path / "isolated"
    _write_key(default_dir, "default_key", "ssh-ed25519 AAAA-default\n")
    _write_key(isolated_dir, "isolated_key", "ssh-ed25519 AAAA-isolated\n")
    server = key_server(default_dir, isolated_dir)
    client = DaemonClient(socket_path=server.socket_path)
    try:
        default_list = client._request("keys.list", {"scope": "default"})
        isolated_list = client._request("keys.list", {"scope": "isolated"})
        assert [k["name"] for k in default_list["keys"]] == ["default_key"]
        assert [k["name"] for k in isolated_list["keys"]] == ["isolated_key"]
        # Same basename in different scopes yields distinct ids.
        _write_key(default_dir, "dup", "ssh-ed25519 AAAA-d1\n")
        _write_key(isolated_dir, "dup", "ssh-ed25519 AAAA-d2\n")
        default_ids = {
            k["key_id"]
            for k in client._request("keys.list", {"scope": "default"})["keys"]
        }
        isolated_ids = {
            k["key_id"]
            for k in client._request("keys.list", {"scope": "isolated"})["keys"]
        }
        assert default_ids != isolated_ids
    finally:
        client.close()

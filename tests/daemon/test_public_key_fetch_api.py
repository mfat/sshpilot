"""Socket-level ``authorized_keys.fetch``: typed round trip and typed errors.

The HTTPS fetch is replaced by an injected fetcher so no network is used; the
test proves the daemon owns the fetch and that structured error details (the
code GTK maps to a message) survive the transport.
"""
from __future__ import annotations

import pytest

from sshpilot.api import DaemonClient
from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.identity import (
    PUBLIC_KEY_SOURCE_NOT_FOUND,
    FetchPublicKeysRequest,
    ImportedPublicKey,
    ImportedPublicKeyList,
)
from sshpilot.core.identity_service import IdentityStateService
from sshpilot.core.keys.public_key_import import PublicKeyHttpError
from sshpilot.daemon import DaemonServer
from sshpilot.daemon.identity_service import DaemonIdentityService
from sshpilot.daemon.key_service import DaemonKeyService
from sshpilot.daemon.operation_runtime import OperationRuntime
from sshpilot.daemon.server import CoreServices
from tests.helpers.fake_connection_repository import make_test_connection_service

ED25519 = "AAAAC3NzaC1lZDI1NTE5AAAAIMkGoTfVoNpsJrNxzq9WpRhlCp0qsPwsOHopWxNbIM8Z"


@pytest.fixture
def fetch_server(tmp_path):
    requested = []

    def fetch(url, headers, timeout, *, follow_redirects):
        requested.append(url)
        if url.endswith("/ghost.keys"):
            raise PublicKeyHttpError(404)
        return f"ssh-ed25519 {ED25519} laptop\n".encode()

    socket_path = tmp_path / "fetch-sock" / "sshpilotd.sock"
    socket_path.parent.mkdir(mode=0o700, parents=True)

    def _build():
        identity = DaemonIdentityService(
            IdentityStateService(tmp_path / "identity.json", environ={"PATH": "/usr/bin"}),
            DaemonKeyService(lambda scope: tmp_path / "keys"),
            OperationRuntime(),
            environ={"PATH": "/usr/bin"},
            public_key_fetch=fetch,
        )
        return CoreServices(
            connections=make_test_connection_service(client_name="sshpilotd"),
            identity=identity,
        )

    server = DaemonServer(_build, socket_path=socket_path)
    server.start_in_thread()
    client = DaemonClient(socket_path=server.socket_path)
    try:
        yield client, requested
    finally:
        client.close()
        server.shutdown()
        server.wait_stopped()


def test_fetch_public_keys_round_trips_over_real_daemon(fetch_server):
    client, requested = fetch_server

    result = client.fetch_public_keys(FetchPublicKeysRequest(source="gl:dev"))

    assert requested == ["https://gitlab.com/dev.keys"]
    assert type(result) is ImportedPublicKeyList
    assert result.source == "gl:dev"
    assert result.keys == (
        ImportedPublicKey(
            key_type="ssh-ed25519",
            fingerprint="SHA256:XCEFfvF2V6u0ESVb/GLp/HAHVCZMoi36uskzHxTBUk0",
            comment="laptop dev@gitlab # ssh-import-id gl:dev",
            line=f"ssh-ed25519 {ED25519} laptop dev@gitlab # ssh-import-id gl:dev",
        ),
    )


def test_fetch_errors_keep_their_detail_code_over_the_socket(fetch_server):
    client, _requested = fetch_server

    with pytest.raises(SshPilotError) as exc_info:
        client.fetch_public_keys(FetchPublicKeysRequest(source="gl:ghost"))

    assert exc_info.value.code is ErrorCode.KEY_NOT_FOUND
    assert exc_info.value.details.get("code") == PUBLIC_KEY_SOURCE_NOT_FOUND

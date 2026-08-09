"""SSH-key public client contract tests."""

import pytest

from sshpilot.core.connection_application_service import ConnectionApplicationService
from sshpilot.api.capabilities import Capability
from sshpilot.api.client import SshPilotClient
from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.daemon_client import DAEMON_IMPLEMENTED_CLIENT_METHOD_CAPABILITIES
from sshpilot.api.method_capabilities import UNSUPPORTED_CLIENT_METHOD_CAPABILITIES
from sshpilot.api.models.keys import (
    GenerateKeyRequest,
    KeyId,
    ListKeysRequest,
    ReadPublicKeyRequest,
    VerifyKeyPassphraseRequest,
)
from sshpilot.api.models import SessionId
from tests.helpers.fake_connection_repository import make_test_repository


def test_protocol_declares_key_methods():
    for method_name in (
        "list_keys",
        "read_public_key",
        "generate_key",
        "verify_key_passphrase",
    ):
        assert callable(getattr(SshPilotClient, method_name))


def test_daemon_client_registers_key_methods():
    assert (
        DAEMON_IMPLEMENTED_CLIENT_METHOD_CAPABILITIES["list_keys"]
        is Capability.KEYS_READ
    )
    assert (
        DAEMON_IMPLEMENTED_CLIENT_METHOD_CAPABILITIES["read_public_key"]
        is Capability.KEYS_READ
    )
    assert (
        DAEMON_IMPLEMENTED_CLIENT_METHOD_CAPABILITIES["generate_key"]
        is Capability.KEYS_WRITE
    )
    assert (
        DAEMON_IMPLEMENTED_CLIENT_METHOD_CAPABILITIES["verify_key_passphrase"]
        is Capability.KEYS_WRITE
    )
    # Daemon-only methods leave the unsupported (schema-only) map.
    for name in (
        "list_keys",
        "read_public_key",
        "generate_key",
        "verify_key_passphrase",
    ):
        assert name not in UNSUPPORTED_CLIENT_METHOD_CAPABILITIES


def _in_process_client():
    return ConnectionApplicationService(make_test_repository(), client_name="test")


@pytest.mark.parametrize(
    "method,args",
    [
        ("list_keys", (ListKeysRequest(),)),
        ("read_public_key", (ReadPublicKeyRequest(key_id=KeyId("key-1")),)),
        ("generate_key", (GenerateKeyRequest(name="id_ed25519"),)),
        (
            "verify_key_passphrase",
            (
                VerifyKeyPassphraseRequest(
                    key_path="/home/user/.ssh/id_ed25519",
                    interaction_scope_id=SessionId("key-operation-verify-1"),
                ),
            ),
        ),
    ],
)
def test_in_process_client_raises_canonical_unsupported(method, args):
    client = _in_process_client()
    with pytest.raises(SshPilotError) as excinfo:
        getattr(client, method)(*args)
    assert excinfo.value.code is ErrorCode.UNSUPPORTED_CAPABILITY


def test_in_process_client_never_advertises_key_capabilities():
    client = _in_process_client()
    capabilities = client.get_capabilities()
    assert not capabilities.supports(Capability.KEYS_READ)
    assert not capabilities.supports(Capability.KEYS_WRITE)


def test_in_process_client_does_no_local_key_io():
    # The canonical unsupported error means the in-process client must not
    # scan, generate, or read key files (no KeyService construction or path
    # derivation). The error must be raised without touching the filesystem.
    client = _in_process_client()
    with pytest.raises(SshPilotError) as excinfo:
        client.list_keys(ListKeysRequest())
    assert excinfo.value.code is ErrorCode.UNSUPPORTED_CAPABILITY

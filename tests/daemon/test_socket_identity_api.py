"""Socket-level DaemonClient identity API tests.

Proves the daemon-owned identity provider state and the provider-scoped
native agent-key listing over a real in-process daemon: the system-agent
availability flag comes from daemon provider state (even when another
provider is selected), and ``identity.provider.keys.get`` runs the native
``ssh-add -l`` against the *named* provider's agent environment.
"""
from __future__ import annotations

import io

import pytest

from sshpilot.api import DaemonClient
from sshpilot.api.models.identity import (
    AgentKey,
    AgentKeyList,
    ListProviderAgentKeysRequest,
    UpdateIdentitySelectionRequest,
)
from sshpilot.core.identity_service import IdentityStateService
from sshpilot.daemon import DaemonServer
from sshpilot.daemon.identity_service import DaemonIdentityService
from sshpilot.daemon.key_service import DaemonKeyService
from sshpilot.daemon.operation_runtime import OperationRuntime
from sshpilot.daemon.server import CoreServices
from tests.helpers.fake_connection_repository import make_test_connection_service

SSH_ADD_OUTPUT = (
    "256 SHA256:systemKey ed25519 user@host (ED25519)\n"
    "3072 SHA256:another rsa user@host (RSA)\n"
)


class _Completed:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = io.StringIO(stdout)
        self.stderr = ""

    def communicate(self, *_args, **_kwargs):
        return self.stdout.getvalue(), self.stderr

    def wait(self, *_args, **_kwargs):
        return self.returncode

    def terminate(self):
        return None

    def kill(self):
        return None


@pytest.fixture
def identity_server(tmp_path):
    """Start an in-process daemon with a scripted native ssh-add runner."""
    servers = []
    recorded = []

    def _start(daemon_env=None):
        daemon_env = {"PATH": "/usr/bin", **(daemon_env or {})}
        socket_path = tmp_path / f"identity-sock-{len(servers)}" / "sshpilotd.sock"
        socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

        def popen(argv, **kwargs):
            recorded.append((list(argv), dict(kwargs)))
            return _Completed(returncode=0, stdout=SSH_ADD_OUTPUT)

        def _build():
            connections = make_test_connection_service(client_name="sshpilotd")
            state = IdentityStateService(
                tmp_path / f"identity-{len(servers)}.json", environ=daemon_env
            )
            identity = DaemonIdentityService(
                state,
                DaemonKeyService(lambda scope: tmp_path / "keys"),
                OperationRuntime(),
                popen=popen,
                environ=daemon_env,
            )
            return CoreServices(connections=connections, identity=identity)

        server = DaemonServer(_build, socket_path=socket_path)
        server.start_in_thread()
        servers.append(server)
        return server

    yield _start, recorded
    for server in servers:
        server.shutdown()
        server.wait_stopped()


def test_provider_agent_keys_round_trip_over_real_daemon(tmp_path, identity_server):
    _start, recorded = identity_server
    server = _start(daemon_env={"SSH_AUTH_SOCK": "/run/user/1000/agent.sock"})
    client = DaemonClient(socket_path=server.socket_path)
    try:
        result = client.list_provider_agent_keys(
            ListProviderAgentKeysRequest(provider_id="auto")
        )
        assert type(result) is AgentKeyList
        assert type(result.keys[0]) is AgentKey
        assert [key.fingerprint for key in result.keys] == [
            "SHA256:systemKey",
            "SHA256:another",
        ]
        argv, kwargs = recorded[0]
        assert argv == ["ssh-add", "-l"]
        assert kwargs["env"]["SSH_AUTH_SOCK"] == "/run/user/1000/agent.sock"
    finally:
        client.close()


def test_system_agent_listing_ignores_selected_provider(tmp_path, identity_server):
    """A provider-scoped listing observes the system agent even after another
    provider is selected, while the plain listing follows the selection."""
    _start, recorded = identity_server
    server = _start(daemon_env={"SSH_AUTH_SOCK": "/run/user/1000/agent.sock"})
    client = DaemonClient(socket_path=server.socket_path)
    try:
        client.update_identity_selection(
            UpdateIdentitySelectionRequest(provider="onepassword")
        )
        # The plain selected-agent listing now uses the fixed 1Password socket.
        client.list_agent_keys()
        assert recorded[-1][1]["env"]["SSH_AUTH_SOCK"].endswith(
            ".1password/agent.sock"
        )
        # The provider-scoped listing still targets the system agent.
        client.list_provider_agent_keys(
            ListProviderAgentKeysRequest(provider_id="auto")
        )
        assert recorded[-1][1]["env"]["SSH_AUTH_SOCK"] == "/run/user/1000/agent.sock"
    finally:
        client.close()


def test_system_agent_availability_comes_from_daemon_state(tmp_path, identity_server):
    """The ``'auto'`` descriptor's availability reflects the daemon's inherited
    agent socket regardless of the selected provider."""
    _start, _recorded = identity_server
    server = _start(daemon_env={"SSH_AUTH_SOCK": "/run/user/1000/agent.sock"})
    client = DaemonClient(socket_path=server.socket_path)
    try:
        registry = client.get_identity_providers()
        auto = next(p for p in registry.providers if p.provider_id == "auto")
        assert auto.available is True
        client.update_identity_selection(
            UpdateIdentitySelectionRequest(provider="onepassword")
        )
        registry = client.get_identity_providers()
        auto = next(p for p in registry.providers if p.provider_id == "auto")
        assert auto.available is True
        selected = next(p for p in registry.providers if p.selected)
        assert selected.provider_id == "onepassword"
    finally:
        client.close()


def test_provider_agent_keys_model_rejects_empty_provider():
    with pytest.raises(ValueError):
        ListProviderAgentKeysRequest(provider_id="")
    with pytest.raises(TypeError):
        ListProviderAgentKeysRequest(provider_id=None)


def test_list_provider_agent_keys_codec_round_trip():
    from sshpilot.api.transport.codec import (
        agent_key_list_from_wire,
        list_provider_agent_keys_request_to_wire,
    )

    request = ListProviderAgentKeysRequest(provider_id="auto")
    wire = list_provider_agent_keys_request_to_wire(request)
    assert wire == {"provider_id": "auto"}
    key_list = agent_key_list_from_wire(
        {
            "keys": [
                {
                    "fingerprint": "SHA256:x",
                    "comment": "user@host",
                    "key_type": "ed25519",
                }
            ]
        }
    )
    assert key_list.keys[0].fingerprint == "SHA256:x"

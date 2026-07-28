import pytest

from sshpilot.api import Capability, ErrorCode, SshPilotError
from sshpilot.api.models import CreateConnectionRequest


def test_capabilities_advertise_only_contract_tested_runtime(fake_manager, client_factory):
    client = client_factory(fake_manager)

    capabilities = client.get_capabilities()

    assert capabilities.supported == frozenset({Capability.CONNECTIONS_READ})
    assert capabilities.supports(Capability.CONNECTIONS_READ)
    assert not capabilities.supports(Capability.CONNECTIONS_WRITE)
    assert not capabilities.supports(Capability.TERMINAL)
    assert capabilities.compatibility.compatible is True


def test_capability_identifiers_are_stable_strings():
    assert Capability.CONNECTIONS_READ.value == "connections.read"
    assert Capability.TERMINAL_REPLAY.value == "terminal.replay"
    assert Capability.PORT_FORWARDING.value == "port_forwarding"


def test_schema_existence_does_not_enable_write_capability(fake_manager, client_factory):
    client = client_factory(fake_manager)
    request = CreateConnectionRequest(nickname="new", hostname="new.example")

    with pytest.raises(SshPilotError) as caught:
        client.create_connection(request)

    assert caught.value.code is ErrorCode.UNSUPPORTED_CAPABILITY
    assert caught.value.details == {"capability": "connections.write"}


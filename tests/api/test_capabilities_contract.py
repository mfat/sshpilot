import pytest

from sshpilot.api import Capability, ErrorCode, SshPilotError
from sshpilot.api.models import (
    CreateConnectionRequest,
    InteractionResponse,
    InteractionStatus,
    TerminalDimensions,
    TerminalInput,
    UpdateConnectionRequest,
)
from sshpilot.api.models.common import (
    AttachmentId,
    ClientId,
    ConnectionId,
    InteractionId,
    SessionId,
)
from sshpilot.api.models.sessions import (
    AttachSessionRequest,
    CloseSessionRequest,
    DetachSessionRequest,
    OpenSessionRequest,
)
from sshpilot.api.models.terminal import ResizeTerminalRequest


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


@pytest.mark.parametrize(
    "invoke,capability",
    [
        (
            lambda client: client.create_connection(
                CreateConnectionRequest(nickname="new", hostname="new.example")
            ),
            Capability.CONNECTIONS_WRITE,
        ),
        (
            lambda client: client.update_connection(
                ConnectionId("connection:v1:test"),
                UpdateConnectionRequest(username="user"),
            ),
            Capability.CONNECTIONS_WRITE,
        ),
        (
            lambda client: client.delete_connection(ConnectionId("connection:v1:test")),
            Capability.CONNECTIONS_WRITE,
        ),
        (
            lambda client: client.open_session(
                OpenSessionRequest(
                    connection_id=ConnectionId("connection:v1:test"),
                    client_id=ClientId("client:test"),
                )
            ),
            Capability.TERMINAL,
        ),
        (
            lambda client: client.attach_session(
                AttachSessionRequest(
                    session_id=SessionId("session:test"),
                    client_id=ClientId("client:test"),
                )
            ),
            Capability.TERMINAL_ATTACH,
        ),
        (
            lambda client: client.detach_session(
                DetachSessionRequest(
                    session_id=SessionId("session:test"),
                    attachment_id=AttachmentId("attachment:test"),
                )
            ),
            Capability.TERMINAL_ATTACH,
        ),
        (
            lambda client: client.close_session(
                CloseSessionRequest(session_id=SessionId("session:test"))
            ),
            Capability.TERMINAL,
        ),
        (
            lambda client: client.send_terminal_input(
                TerminalInput(
                    session_id=SessionId("session:test"),
                    attachment_id=AttachmentId("attachment:test"),
                    data=b"example",
                )
            ),
            Capability.TERMINAL,
        ),
        (
            lambda client: client.resize_terminal(
                ResizeTerminalRequest(
                    session_id=SessionId("session:test"),
                    attachment_id=AttachmentId("attachment:test"),
                    dimensions=TerminalDimensions(rows=24, columns=80),
                )
            ),
            Capability.TERMINAL,
        ),
        (
            lambda client: client.respond_to_interaction(
                InteractionResponse(
                    interaction_id=InteractionId("interaction:test"),
                    status=InteractionStatus.CANCELLED,
                )
            ),
            Capability.INTERACTIONS,
        ),
    ],
)
def test_unavailable_methods_fail_with_their_stable_capability(
    fake_manager,
    client_factory,
    invoke,
    capability,
):
    client = client_factory(fake_manager)

    with pytest.raises(SshPilotError) as caught:
        invoke(client)

    assert caught.value.code is ErrorCode.UNSUPPORTED_CAPABILITY
    assert caught.value.details == {"capability": capability.value}

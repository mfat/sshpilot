import inspect
from types import SimpleNamespace

import pytest

from sshpilot.api import Capability, ErrorCode, InProcessClient, SshPilotError
from sshpilot.api.client import SshPilotClient
from sshpilot.api.in_process_client import (
    IMPLEMENTED_CLIENT_METHOD_CAPABILITIES,
    UNSUPPORTED_CLIENT_METHOD_CAPABILITIES,
)
from sshpilot.api.models import (
    CreateConnectionRequest,
    InteractionResponse,
    InteractionStatus,
    TerminalDimensions,
    TerminalInput,
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
from sshpilot.api.models.terminal import ReplayRequest, ResizeTerminalRequest


UNSUPPORTED_OPERATION_CASES = [
    (
        "open_session",
        lambda client: client.open_session(
            OpenSessionRequest(
                connection_id=ConnectionId("connection:v1:test"),
                client_id=ClientId("client:test"),
            )
        ),
        Capability.TERMINAL,
    ),
    (
        "attach_session",
        lambda client: client.attach_session(
            AttachSessionRequest(
                session_id=SessionId("session:test"),
                client_id=ClientId("client:test"),
            )
        ),
        Capability.TERMINAL_ATTACH,
    ),
    (
        "detach_session",
        lambda client: client.detach_session(
            DetachSessionRequest(
                session_id=SessionId("session:test"),
                attachment_id=AttachmentId("attachment:test"),
            )
        ),
        Capability.TERMINAL_ATTACH,
    ),
    (
        "close_session",
        lambda client: client.close_session(
            CloseSessionRequest(session_id=SessionId("session:test"))
        ),
        Capability.TERMINAL,
    ),
    (
        "send_terminal_input",
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
        "resize_terminal",
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
        "replay_terminal",
        lambda client: client.replay_terminal(
            ReplayRequest(session_id=SessionId("session:test"))
        ),
        Capability.TERMINAL_REPLAY,
    ),
    (
        "respond_to_interaction",
        lambda client: client.respond_to_interaction(
            InteractionResponse(
                interaction_id=InteractionId("interaction:test"),
                status=InteractionStatus.CANCELLED,
            )
        ),
        Capability.INTERACTIONS,
    ),
]


def test_capabilities_advertise_only_contract_tested_runtime(fake_manager, client_factory):
    client = client_factory(fake_manager)

    capabilities = client.get_capabilities()

    assert capabilities.supported == frozenset(
        {
            Capability.CONNECTIONS_READ,
            Capability.CONNECTIONS_EVENTS,
            Capability.CONNECTIONS_WRITE,
        }
    )
    assert capabilities.supports(Capability.CONNECTIONS_READ)
    assert capabilities.supports(Capability.CONNECTIONS_EVENTS)
    assert capabilities.supports(Capability.CONNECTIONS_WRITE)
    assert not capabilities.supports(Capability.TERMINAL)
    assert capabilities.compatibility.compatible is True


def test_capability_identifiers_are_stable_strings():
    assert Capability.CONNECTIONS_READ.value == "connections.read"
    assert Capability.CONNECTIONS_EVENTS.value == "connections.events"
    assert Capability.TERMINAL_REPLAY.value == "terminal.replay"
    assert Capability.PORT_FORWARDING.value == "port_forwarding"


def test_method_status_metadata_covers_the_complete_client_contract():
    methods = {
        name
        for name, value in vars(SshPilotClient).items()
        if not name.startswith("_") and inspect.isfunction(value)
    }

    assert methods == (
        set(IMPLEMENTED_CLIENT_METHOD_CAPABILITIES)
        | set(UNSUPPORTED_CLIENT_METHOD_CAPABILITIES)
    )
    assert (
        set(IMPLEMENTED_CLIENT_METHOD_CAPABILITIES)
        & set(UNSUPPORTED_CLIENT_METHOD_CAPABILITIES)
    ) == set()
    assert {
        name for name, _invoke, _capability in UNSUPPORTED_OPERATION_CASES
    } == set(UNSUPPORTED_CLIENT_METHOD_CAPABILITIES)


def test_advertised_capabilities_have_implemented_operations(
    fake_manager,
    client_factory,
):
    client = client_factory(fake_manager)
    implemented_capabilities = {
        capability
        for capability in IMPLEMENTED_CLIENT_METHOD_CAPABILITIES.values()
        if capability is not None
    }
    assert client.get_capabilities().supported <= implemented_capabilities


def test_write_capability_is_backed_by_runtime_create(fake_manager, client_factory):
    client = client_factory(fake_manager)
    request = CreateConnectionRequest(nickname="new", hostname="new.example")

    created = client.create_connection(request)

    assert created.nickname == "new"


def test_in_process_write_capability_requires_manager_mutation_methods():
    client = InProcessClient(
        SimpleNamespace(get_connections=list),
    )

    assert not client.get_capabilities().supports(Capability.CONNECTIONS_WRITE)
    with pytest.raises(SshPilotError) as caught:
        client.create_connection(
            CreateConnectionRequest(
                nickname="new",
                hostname="new.example",
            )
        )
    assert caught.value.code is ErrorCode.UNSUPPORTED_CAPABILITY
    client.close()


@pytest.mark.parametrize(
    "method_name,invoke,capability",
    UNSUPPORTED_OPERATION_CASES,
)
def test_unavailable_methods_fail_with_their_stable_capability(
    fake_manager,
    client_factory,
    method_name,
    invoke,
    capability,
):
    assert method_name in UNSUPPORTED_CLIENT_METHOD_CAPABILITIES
    client = client_factory(fake_manager)

    with pytest.raises(SshPilotError) as caught:
        invoke(client)

    assert caught.value.code is ErrorCode.UNSUPPORTED_CAPABILITY
    assert caught.value.details == {"capability": capability.value}

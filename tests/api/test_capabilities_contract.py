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
    ClaimTerminalInputRequest,
    CreateConnectionRequest,
    InteractionDecisionRequest,
    ReleaseTerminalInputRequest,
    SecretDecision,
    TerminalDimensions,
    TerminalInput,
)
from sshpilot.api.models.common import (
    AttachmentId,
    ConnectionId,
    ForwardId,
    InteractionId,
    SessionId,
    SftpServiceId,
    TransferId,
)
from sshpilot.api.models.operations import (
    AttachSftpRequest,
    ClaimForwardRequest,
    CloseForwardRequest,
    CloseSftpRequest,
    ForwardType,
    ListDirectoryRequest,
    OpenForwardRequest,
    OpenSftpRequest,
    SftpChmodRequest,
    SftpPathRequest,
    SftpRenameRequest,
    SftpSymlinkRequest,
)
from sshpilot.api.models.sessions import (
    AttachSessionRequest,
    CloseSessionRequest,
    DetachSessionRequest,
    OpenSessionRequest,
)
from sshpilot.api.models.terminal import ReplayRequest, ResizeTerminalRequest
from sshpilot.api.models.transfers import (
    CancelTransferRequest,
    StartTransferRequest,
    TransferDirection,
)


DAEMON_RUNTIME_CAPABILITIES = frozenset(
    {
        Capability.SESSIONS_READ,
        Capability.SESSIONS_WRITE,
        Capability.SESSIONS_EVENTS,
        Capability.TERMINAL_OUTPUT,
        Capability.TERMINAL_INPUT,
        Capability.TERMINAL_RESIZE,
        Capability.TERMINAL_REPLAY,
        Capability.INTERACTIONS_READ,
        Capability.INTERACTIONS_RESPOND,
        Capability.INTERACTIONS_EVENTS,
        Capability.INTERACTIONS_HOST_KEY,
        Capability.INTERACTIONS_PASSWORD,
        Capability.INTERACTIONS_PASSPHRASE,
        Capability.SFTP_READ,
        Capability.SFTP_WRITE,
        Capability.SFTP_EVENTS,
        Capability.SFTP_METADATA,
        Capability.SFTP_MUTATE,
        Capability.TRANSFERS_READ,
        Capability.TRANSFERS_WRITE,
        Capability.TRANSFERS_EVENTS,
        Capability.TRANSFERS_UPLOAD,
        Capability.TRANSFERS_DOWNLOAD,
        Capability.FORWARDS_READ,
        Capability.FORWARDS_WRITE,
        Capability.FORWARDS_EVENTS,
        Capability.FORWARDS_LOCAL,
        Capability.FORWARDS_REMOTE,
        Capability.FORWARDS_DYNAMIC,
        Capability.DAEMON_STATUS,
        Capability.DAEMON_CONTROL,
        Capability.DAEMON_EVENTS,
    }
)


UNSUPPORTED_OPERATION_CASES = [
    (
        "open_session",
        lambda client: client.open_session(
            OpenSessionRequest(
                connection_id=ConnectionId("test"),
            )
        ),
        Capability.SESSIONS_WRITE,
    ),
    (
        "attach_session",
        lambda client: client.attach_session(
            AttachSessionRequest(
                session_id=SessionId("session:test"),
            )
        ),
        Capability.SESSIONS_WRITE,
    ),
    (
        "detach_session",
        lambda client: client.detach_session(
            DetachSessionRequest(
                session_id=SessionId("session:test"),
                attachment_id=AttachmentId("attachment:test"),
            )
        ),
        Capability.SESSIONS_WRITE,
    ),
    (
        "close_session",
        lambda client: client.close_session(
            CloseSessionRequest(session_id=SessionId("session:test"))
        ),
        Capability.SESSIONS_WRITE,
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
        Capability.TERMINAL_INPUT,
    ),
    (
        "claim_terminal_input",
        lambda client: client.claim_terminal_input(
            ClaimTerminalInputRequest(
                session_id=SessionId("session:test"),
                attachment_id=AttachmentId("attachment:test"),
            )
        ),
        Capability.TERMINAL_INPUT,
    ),
    (
        "release_terminal_input",
        lambda client: client.release_terminal_input(
            ReleaseTerminalInputRequest(
                session_id=SessionId("session:test"),
                attachment_id=AttachmentId("attachment:test"),
            )
        ),
        Capability.TERMINAL_INPUT,
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
        Capability.TERMINAL_RESIZE,
    ),
    (
        "replay_terminal",
        lambda client: client.replay_terminal(
            ReplayRequest(session_id=SessionId("session:test"))
        ),
        Capability.TERMINAL_REPLAY,
    ),
    (
        "subscribe_terminal",
        lambda client: client.subscribe_terminal(
            SessionId("session:test"),
            lambda _output: None,
        ),
        Capability.TERMINAL_OUTPUT,
    ),
    (
        "list_interactions",
        lambda client: client.list_interactions(),
        Capability.INTERACTIONS_READ,
    ),
    (
        "get_interaction",
        lambda client: client.get_interaction(
            InteractionId("interaction-1")
        ),
        Capability.INTERACTIONS_READ,
    ),
    (
        "claim_interaction",
        lambda client: client.claim_interaction(
            InteractionId("interaction-1")
        ),
        Capability.INTERACTIONS_RESPOND,
    ),
    (
        "release_interaction",
        lambda client: client.release_interaction(
            InteractionId("interaction-1")
        ),
        Capability.INTERACTIONS_RESPOND,
    ),
    (
        "respond_to_interaction",
        lambda client: client.respond_to_interaction(
            InteractionDecisionRequest(
                interaction_id=InteractionId(
                    "interaction-1"
                ),
                secret_decision=SecretDecision.CANCEL,
            )
        ),
        Capability.INTERACTIONS_RESPOND,
    ),
    (
        "cancel_interaction",
        lambda client: client.cancel_interaction(
            InteractionId("interaction-1")
        ),
        Capability.INTERACTIONS_RESPOND,
    ),
    (
        "send_interaction_secret",
        lambda client: client.send_interaction_secret(
            InteractionId("interaction-1"),
            "00" * 16,
            bytearray(b"value"),
        ),
        Capability.INTERACTIONS_RESPOND,
    ),
]
UNSUPPORTED_OPERATION_CASES.extend(
    [
        (
            "list_sessions",
            lambda client: client.list_sessions(),
            Capability.SESSIONS_READ,
        ),
        (
            "get_session",
            lambda client: client.get_session(SessionId("session:test")),
            Capability.SESSIONS_READ,
        ),
        (
            "list_sftp_services",
            lambda client: client.list_sftp_services(),
            Capability.SFTP_READ,
        ),
        (
            "get_sftp_service",
            lambda client: client.get_sftp_service(SftpServiceId("sftp:test")),
            Capability.SFTP_READ,
        ),
        (
            "open_sftp",
            lambda client: client.open_sftp(
                OpenSftpRequest(connection_id=ConnectionId("test"))
            ),
            Capability.SFTP_WRITE,
        ),
        (
            "attach_sftp",
            lambda client: client.attach_sftp(
                AttachSftpRequest(service_id=SftpServiceId("sftp:test"))
            ),
            Capability.SFTP_WRITE,
        ),
        (
            "detach_sftp",
            lambda client: client.detach_sftp(SftpServiceId("sftp:test")),
            Capability.SFTP_WRITE,
        ),
        (
            "close_sftp",
            lambda client: client.close_sftp(
                CloseSftpRequest(service_id=SftpServiceId("sftp:test"))
            ),
            Capability.SFTP_WRITE,
        ),
        (
            "sftp_list_directory",
            lambda client: client.sftp_list_directory(
                ListDirectoryRequest(
                    connection_id=ConnectionId("test"),
                    path="/",
                )
            ),
            Capability.SFTP_READ,
        ),
        (
            "sftp_stat",
            lambda client: client.sftp_stat(
                SftpPathRequest(
                    service_id=SftpServiceId("sftp:test"),
                    path="/",
                )
            ),
            Capability.SFTP_METADATA,
        ),
        (
            "sftp_lstat",
            lambda client: client.sftp_lstat(
                SftpPathRequest(
                    service_id=SftpServiceId("sftp:test"),
                    path="/",
                )
            ),
            Capability.SFTP_METADATA,
        ),
        (
            "sftp_realpath",
            lambda client: client.sftp_realpath(
                SftpPathRequest(
                    service_id=SftpServiceId("sftp:test"),
                    path="/",
                )
            ),
            Capability.SFTP_METADATA,
        ),
        (
            "sftp_readlink",
            lambda client: client.sftp_readlink(
                SftpPathRequest(
                    service_id=SftpServiceId("sftp:test"),
                    path="/link",
                )
            ),
            Capability.SFTP_METADATA,
        ),
        (
            "sftp_mkdir",
            lambda client: client.sftp_mkdir(
                SftpPathRequest(
                    service_id=SftpServiceId("sftp:test"),
                    path="/tmp",
                )
            ),
            Capability.SFTP_MUTATE,
        ),
        (
            "sftp_rmdir",
            lambda client: client.sftp_rmdir(
                SftpPathRequest(
                    service_id=SftpServiceId("sftp:test"),
                    path="/tmp",
                )
            ),
            Capability.SFTP_MUTATE,
        ),
        (
            "sftp_remove",
            lambda client: client.sftp_remove(
                SftpPathRequest(
                    service_id=SftpServiceId("sftp:test"),
                    path="/tmp/file",
                )
            ),
            Capability.SFTP_MUTATE,
        ),
        (
            "sftp_rename",
            lambda client: client.sftp_rename(
                SftpRenameRequest(
                    service_id=SftpServiceId("sftp:test"),
                    source_path="/a",
                    destination_path="/b",
                )
            ),
            Capability.SFTP_MUTATE,
        ),
        (
            "sftp_chmod",
            lambda client: client.sftp_chmod(
                SftpChmodRequest(
                    service_id=SftpServiceId("sftp:test"),
                    path="/tmp/file",
                    mode=0o644,
                )
            ),
            Capability.SFTP_MUTATE,
        ),
        (
            "sftp_symlink",
            lambda client: client.sftp_symlink(
                SftpSymlinkRequest(
                    service_id=SftpServiceId("sftp:test"),
                    link_path="/link",
                    target_path="/target",
                )
            ),
            Capability.SFTP_MUTATE,
        ),
        (
            "list_transfers",
            lambda client: client.list_transfers(),
            Capability.TRANSFERS_READ,
        ),
        (
            "get_transfer",
            lambda client: client.get_transfer(TransferId("transfer:test")),
            Capability.TRANSFERS_READ,
        ),
        (
            "start_transfer",
            lambda client: client.start_transfer(
                StartTransferRequest(
                    connection_id=ConnectionId("test"),
                    sftp_service_id=SftpServiceId("sftp:test"),
                    direction=TransferDirection.DOWNLOAD,
                    remote_path="/remote",
                    local_path="/local",
                )
            ),
            Capability.TRANSFERS_WRITE,
        ),
        (
            "cancel_transfer",
            lambda client: client.cancel_transfer(
                CancelTransferRequest(transfer_id=TransferId("transfer:test"))
            ),
            Capability.TRANSFERS_WRITE,
        ),
        (
            "list_forwards",
            lambda client: client.list_forwards(),
            Capability.FORWARDS_READ,
        ),
        (
            "get_forward",
            lambda client: client.get_forward(ForwardId("forward:test")),
            Capability.FORWARDS_READ,
        ),
        (
            "open_forward",
            lambda client: client.open_forward(
                OpenForwardRequest(
                    connection_id=ConnectionId("test"),
                    type=ForwardType.LOCAL,
                    bind_host="127.0.0.1",
                    bind_port=8080,
                    destination_host="127.0.0.1",
                    destination_port=80,
                )
            ),
            Capability.FORWARDS_WRITE,
        ),
        (
            "claim_forward",
            lambda client: client.claim_forward(
                ClaimForwardRequest(forward_id=ForwardId("forward:test"))
            ),
            Capability.FORWARDS_WRITE,
        ),
        (
            "close_forward",
            lambda client: client.close_forward(
                CloseForwardRequest(forward_id=ForwardId("forward:test"))
            ),
            Capability.FORWARDS_WRITE,
        ),
        (
            "get_daemon_status",
            lambda client: client.get_daemon_status(),
            Capability.DAEMON_STATUS,
        ),
        (
            "get_daemon_diagnostics",
            lambda client: client.get_daemon_diagnostics(),
            Capability.DAEMON_STATUS,
        ),
        (
            "stop_daemon",
            lambda client: client.stop_daemon(),
            Capability.DAEMON_CONTROL,
        ),
        (
            "restart_daemon",
            lambda client: client.restart_daemon(),
            Capability.DAEMON_CONTROL,
        ),
    ]
)


def test_capabilities_advertise_only_contract_tested_runtime(
    fake_manager,
    client_factory,
    client_backend,
):
    client = client_factory(fake_manager)

    capabilities = client.get_capabilities()

    expected = {
        Capability.CONNECTIONS_READ,
        Capability.CONNECTIONS_EVENTS,
        Capability.CONNECTIONS_WRITE,
        Capability.CONNECTIONS_CONFIG_READ,
    }
    if client_backend == "daemon":
        expected.update(DAEMON_RUNTIME_CAPABILITIES)
    assert capabilities.supported == frozenset(expected)
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
    client_backend,
):
    client = client_factory(fake_manager)
    implemented_capabilities = {
        capability
        for capability in IMPLEMENTED_CLIENT_METHOD_CAPABILITIES.values()
        if capability is not None
    }
    if client_backend == "daemon":
        implemented_capabilities.update(DAEMON_RUNTIME_CAPABILITIES)
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
    in_process_client_factory,
    method_name,
    invoke,
    capability,
):
    assert method_name in UNSUPPORTED_CLIENT_METHOD_CAPABILITIES
    client = in_process_client_factory(fake_manager)

    with pytest.raises(SshPilotError) as caught:
        invoke(client)

    assert caught.value.code is ErrorCode.UNSUPPORTED_CAPABILITY
    assert caught.value.details == {"capability": capability.value}

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import StringIO

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.broadcast import (
    BroadcastCommandSummary,
    HostCommandResult,
    HostCommandState,
)
from sshpilot.api.models.common import ConnectionId, utc_now
from sshpilot.api.models.connections import ConnectionHealth, ConnectionSummary
from sshpilot.api.models.interactions import (
    InteractionClaim,
    InteractionState,
    InteractionType,
    InteractionSummary,
    PassphrasePrompt,
)
from sshpilot.api.models.operations import (
    OperationId,
    OperationKind,
    OperationState,
    OperationSummary,
)

from sshpilot.cli.interactions import handle_interactions
from sshpilot.cli.main import EXIT_DAEMON_UNAVAILABLE, EXIT_OPERATION_FAILED, run
from sshpilot.cli.output import to_jsonable


@dataclass(frozen=True)
class Status:
    state: str
    api: str


@dataclass(frozen=True)
class Capabilities:
    supported: frozenset[str]


def _operation(state=OperationState.SUCCEEDED, *, result=None):
    return OperationSummary(
        OperationId("operation-1"),
        OperationKind.BROADCAST_COMMAND,
        state,
        "done",
        utc_now(),
        result=result,
    )


class FakeClient:
    def __init__(self):
        self.closed = False
        self.requests = []
        self.connections = [
            ConnectionSummary(
                ConnectionId("demo"),
                "demo",
                "demo",
                "demo.example",
                "alice",
                22,
                health=ConnectionHealth.UNKNOWN,
            )
        ]
        self.polls = 0

    def close(self):
        self.closed = True

    def get_daemon_status(self):
        return Status("ready", "0.25")

    def get_capabilities(self):
        return Capabilities(frozenset({"connections.read", "broadcast.write"}))

    def list_connections(self):
        return self.connections

    def get_connection(self, connection_id):
        assert connection_id == ConnectionId("demo")
        return self.connections[0]

    def list_sessions(self):
        return []

    def get_operation(self, operation_id):
        assert operation_id == "operation-1"
        return _operation()

    def cancel_operation(self, operation_id):
        assert operation_id == "operation-1"
        return _operation(OperationState.CANCELLED)

    def start_broadcast_command(self, request):
        self.requests.append(request)
        return BroadcastCommandSummary(_operation(OperationState.QUEUED), ())

    def get_broadcast_command(self, operation_id):
        self.polls += 1
        state = OperationState.RUNNING if self.polls == 1 else OperationState.SUCCEEDED
        target_state = HostCommandState.RUNNING if self.polls == 1 else HostCommandState.SUCCEEDED
        return BroadcastCommandSummary(
            _operation(state),
            (HostCommandResult(ConnectionId("demo"), target_state, 0, "hello\n"),),
        )

    def list_interactions(self):
        return []


def test_status_and_capabilities_json_use_stdout_only():
    client = FakeClient()
    out, err = StringIO(), StringIO()
    assert run(["status", "--json"], client_factory=lambda **_: client, stdout=out, stderr=err) == 0
    assert '"state": "ready"' in out.getvalue()
    assert not err.getvalue()

    out, err = StringIO(), StringIO()
    assert run(
        ["capabilities", "--json"],
        client_factory=lambda **_: client,
        stdout=out,
        stderr=err,
    ) == 0
    assert '"supported"' in out.getvalue()
    assert not err.getvalue()


def test_connection_list_and_alias_resolution():
    client = FakeClient()
    out, err = StringIO(), StringIO()
    assert run(
        ["connections", "list", "--json"],
        client_factory=lambda **_: client,
        stdout=out,
        stderr=err,
    ) == 0
    assert '"nickname": "demo"' in out.getvalue()

    out, err = StringIO(), StringIO()
    assert run(
        ["connections", "show", "demo.example", "--json"],
        client_factory=lambda **_: client,
        stdout=out,
        stderr=err,
    ) == 0
    assert '"hostname": "demo.example"' in out.getvalue()


def test_ambiguous_connection_is_usage_error():
    client = FakeClient()
    client.connections.append(
        ConnectionSummary(
            ConnectionId("other"),
            "other",
            "demo",
            "other.example",
            "bob",
            22,
        )
    )
    out, err = StringIO(), StringIO()
    assert run(
        ["exec", "demo", "--", "true"],
        client_factory=lambda **_: client,
        stdout=out,
        stderr=err,
    ) == 2
    assert "ambiguous" in err.getvalue()


def test_operations_get_and_cancel_use_typed_client_methods():
    client = FakeClient()
    for command, expected in (("get", "done"), ("cancel", "cancelled")):
        out, err = StringIO(), StringIO()
        assert run(
            ["operations", command, "operation-1", "--json"],
            client_factory=lambda **_: client,
            stdout=out,
            stderr=err,
        ) == 0
        assert expected in out.getvalue()


def test_exec_uses_broadcast_api_and_keeps_json_valid():
    client = FakeClient()
    out, err = StringIO(), StringIO()
    assert run(
        ["exec", "demo", "--json", "--", "uname", "-a"],
        client_factory=lambda **_: client,
        stdout=out,
        stderr=err,
    ) == 0
    assert len(client.requests) == 1
    assert client.requests[0].connection_ids == (ConnectionId("demo"),)
    assert client.requests[0].command == "uname -a"
    assert out.getvalue().lstrip().startswith("{")
    assert "hello" in out.getvalue()


def test_exec_failure_and_daemon_unavailable_exit_codes():
    client = FakeClient()
    client.get_broadcast_command = lambda _operation_id: BroadcastCommandSummary(
        _operation(OperationState.FAILED),
        (HostCommandResult(ConnectionId("demo"), HostCommandState.FAILED, 7, "", "bad\n"),),
    )
    out, err = StringIO(), StringIO()
    assert run(
        ["exec", "demo", "--", "false"],
        client_factory=lambda **_: client,
        stdout=out,
        stderr=err,
    ) == EXIT_OPERATION_FAILED
    assert "bad" in err.getvalue()

    out, err = StringIO(), StringIO()
    def unavailable(**_):
        raise SshPilotError(ErrorCode.DAEMON_UNAVAILABLE, "daemon unavailable")

    assert run(["status"], client_factory=unavailable, stdout=out, stderr=err) == EXIT_DAEMON_UNAVAILABLE
    assert not out.getvalue()
    assert "daemon_unavailable" in err.getvalue()


def test_protected_interaction_uses_bytearray_secret_frame_and_clears_it():
    now = datetime.now(timezone.utc)
    summary = InteractionSummary(
        "interaction-1",
        "operation-1",
        "demo",
        InteractionType.PRIVATE_KEY_PASSPHRASE,
        InteractionState.PENDING,
        now,
        now + timedelta(seconds=30),
        1,
        PassphrasePrompt("id_ed25519", None, 1, False, False),
    )

    class InteractionClient:
        def __init__(self):
            self.sent = None

        def list_interactions(self):
            return [summary]

        def claim_interaction(self, _interaction_id):
            return InteractionClaim("interaction-1", "cli", "00" * 16, summary.expires_at)

        def respond_to_interaction(self, _request):
            return None

        def send_interaction_secret(self, _interaction_id, _nonce, secret):
            self.sent = bytes(secret)

    client = InteractionClient()
    handle_interactions(
        client,
        "operation-1",
        write=lambda _text: None,
        getpass_fn=lambda _prompt: "CLI_SECRET_SENTINEL",
    )
    assert client.sent == b"CLI_SECRET_SENTINEL"


def test_public_dataclass_json_conversion_never_uses_repr():
    value = ConnectionSummary(
        ConnectionId("demo"), "demo", "demo", "demo.example", "alice", 22
    )
    converted = to_jsonable(value)
    assert converted["id"] == "demo"
    assert "ConnectionSummary" not in str(converted)

import threading

from sshpilot.api.daemon_client import DaemonClient
from sshpilot.api.events import EventType
from sshpilot.api.models import (
    HostKeyDecision,
    InteractionDecisionRequest,
    InteractionState,
    InteractionType,
    OpenSessionRequest,
    PasswordPrompt,
    RememberPolicy,
    SecretDecision,
    SessionState,
)


class _Handle:
    def terminate(self):
        return None

    def kill(self):
        return None

    def wait(self, _timeout):
        return None


class _Runner:
    terminal_capable = False

    def start(self, _spec, _on_exit, _on_output=None, _on_eof=None):
        return _Handle()

    def close(self):
        return None


class _BlockingRunner(_Runner):
    def __init__(self):
        self.block_next = False
        self.started = threading.Event()
        self.release = threading.Event()

    def start(self, spec, on_exit, on_output=None, on_eof=None):
        if self.block_next:
            self.started.set()
            assert self.release.wait(3)
        return super().start(spec, on_exit, on_output, on_eof)


def test_claim_response_and_secret_delivery_over_real_socket(
    daemon_factory,
) -> None:
    server, _manager = daemon_factory(session_runner=_Runner())
    client = DaemonClient(socket_path=server.socket_path, client_id="client:owner")
    opened = client.open_session(
        OpenSessionRequest(connection_id=client.list_connections()[0].id)
    )
    delivered = threading.Event()
    events = []
    subscription = client.subscribe_events(
        lambda event: (
            events.append(event),
            delivered.set()
            if event.type is EventType.INTERACTION_CREATED
            else None,
        )
    )
    summary = server._interaction_broker.create(
        session_id=opened.id,
        connection_id=opened.connection_id,
        interaction_type=InteractionType.PASSWORD,
        prompt=PasswordPrompt(
            username="alice",
            hostname="example.test",
            port=22,
            attempt=1,
            can_remember=True,
            stored_secret_available=False,
        ),
    )
    assert delivered.wait(2)
    assert client.list_interactions()[0] == summary
    assert client.get_interaction(summary.id) == summary
    claim = client.claim_interaction(summary.id)
    client.respond_to_interaction(
        InteractionDecisionRequest(
            interaction_id=summary.id,
            secret_decision=SecretDecision.SUBMIT,
            remember_policy=RememberPolicy.DO_NOT_STORE,
        )
    )
    secret = bytearray(b"one-use")
    client.send_interaction_secret(summary.id, claim.nonce, secret)
    assert secret == bytearray()
    result = server._interaction_broker.wait_for_result(summary.id)
    assert result is not None
    assert bytes(result.secret or b"") == b"one-use"
    result.clear()
    assert client.get_interaction(summary.id).state is InteractionState.ANSWERED
    subscription.close()
    client.close()


def test_host_key_decision_never_uses_secret_frame(daemon_factory) -> None:
    server, _manager = daemon_factory(session_runner=_Runner())
    client = DaemonClient(socket_path=server.socket_path, client_id="client:owner")
    opened = client.open_session(
        OpenSessionRequest(connection_id=client.list_connections()[0].id)
    )
    from sshpilot.api.models import HostKeyPrompt, HostKeyStatus

    summary = server._interaction_broker.create(
        session_id=opened.id,
        connection_id=opened.connection_id,
        interaction_type=InteractionType.HOST_KEY_CONFIRMATION,
        prompt=HostKeyPrompt(
            hostname="example.test",
            port=22,
            key_type="ssh-ed25519",
            fingerprint="SHA256:abc",
            status=HostKeyStatus.UNKNOWN,
        ),
    )
    client.claim_interaction(summary.id)
    client.respond_to_interaction(
        InteractionDecisionRequest(
            interaction_id=summary.id,
            host_key_decision=HostKeyDecision.ACCEPT,
        )
    )
    result = server._interaction_broker.wait_for_result(summary.id)
    assert result is not None
    assert result.decision is HostKeyDecision.ACCEPT
    client.close()


def test_interaction_response_is_not_blocked_by_pending_session_open(
    daemon_factory,
) -> None:
    runner = _BlockingRunner()
    server, _manager = daemon_factory(session_runner=runner)
    client = DaemonClient(
        socket_path=server.socket_path,
        client_id="client:owner",
        timeout=0.5,
    )
    connection_id = client.list_connections()[0].id
    first = client.open_session(OpenSessionRequest(connection_id=connection_id))
    runner.block_next = True
    second = client.open_session(OpenSessionRequest(connection_id=connection_id))
    assert second.state is SessionState.STARTING
    assert runner.started.wait(1)
    summary = server._interaction_broker.create(
        session_id=first.id,
        connection_id=connection_id,
        interaction_type=InteractionType.PASSWORD,
        prompt=PasswordPrompt(
            username="alice",
            hostname="example.test",
            port=22,
            attempt=1,
            can_remember=False,
            stored_secret_available=False,
        ),
    )
    claim = client.claim_interaction(summary.id)
    client.respond_to_interaction(
        InteractionDecisionRequest(
            interaction_id=summary.id,
            secret_decision=SecretDecision.SUBMIT,
        )
    )
    client.send_interaction_secret(
        summary.id,
        claim.nonce,
        bytearray(b"one-use"),
    )
    result = server._interaction_broker.wait_for_result(summary.id)
    assert result is not None
    result.clear()
    assert not runner.release.is_set()
    assert client.get_session(second.id).state is SessionState.STARTING
    runner.release.set()
    assert client.list_sessions()
    client.close()

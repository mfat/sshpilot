import threading
import time
from types import SimpleNamespace

import pytest

from sshpilot.api import ErrorCode
from sshpilot.api.models import ConnectionId, SessionId, StartScpTransferRequest, TransferDirection, TransferId
from sshpilot.daemon.native_scp_backend import NativeScpBackend


class _Broker:
    def __init__(self):
        self.prepared = []
        self.cancelled = []
        self.authenticated = []

    def prepare_operation_launch(self, argv, env, **kwargs):
        self.prepared.append((tuple(argv), dict(env), kwargs))
        result = dict(env)
        result["SSH_ASKPASS"] = "/private/helper"
        result["SSHPILOT_DAEMON_ASKPASS_TOKEN"] = "secret-token"
        return tuple(argv), result

    def mark_authenticated(self, session_id):
        self.authenticated.append(session_id)

    def cancel_session(self, scope_id):
        self.cancelled.append(scope_id)


class _Provider:
    def __init__(self):
        self.calls = []

    def prepare_daemon_scp_target(self, connection_id):
        return "alice@[2001:db8::1]"

    def prepare_daemon_scp_launch(self, connection_id, **kwargs):
        self.calls.append((connection_id, kwargs))
        return (
            (
                "/usr/bin/scp",
                "-F",
                "/tmp/ssh config",
                *kwargs.get("extra_args", ()),
                kwargs["target_override"],
            ),
            {"PATH": "/usr/bin", "SENTINEL": "not-secret"},
        )


class _Process:
    def __init__(self, returncode=0, stderr=b""):
        self.pid = 1234
        self.returncode = returncode
        # Yield the payload once, then EOF -- a real pipe returns b"" when
        # the child closes it. Returning it forever spun the drain thread.
        _pending = [stderr] if stderr else []
        self.stderr = SimpleNamespace(
            read=lambda _limit: _pending.pop(0) if _pending else b"",
        )
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode if not self.terminated else -15

    def wait(self, timeout=None):
        if self.returncode is None and not self.terminated:
            raise __import__("subprocess").TimeoutExpired("scp", timeout)
        return self.returncode if not self.terminated else -15

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        self.terminated = True


class _Popen:
    def __init__(self, processes):
        self.processes = list(processes)
        self.started = []
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((tuple(argv), kwargs))
        process = self.processes.pop(0)
        self.started.append(process)
        return process


def _request(**overrides):
    values = {
        "connection_id": ConnectionId("demo"),
        "direction": TransferDirection.UPLOAD,
        "sources": ("/tmp/a file", "/tmp/b;literal"),
        "destination": "/remote/drop",
    }
    values.update(overrides)
    return StartScpTransferRequest(**values)


def test_native_backend_builds_literal_multi_source_argv_without_shell():
    provider = _Provider()
    broker = _Broker()
    popen = _Popen([_Process()])
    backend = NativeScpBackend(provider, broker, popen=popen)

    backend.run(
        _request(),
        connection_target="alice@[2001:db8::1]",
        connection_id=ConnectionId("demo"),
        transfer_id=TransferId("transfer-1"),
        cancel_event=threading.Event(),
    )

    argv, kwargs = popen.calls[0]
    assert argv == (
        "/usr/bin/scp",
        "-F",
        "/tmp/ssh config",
        "/tmp/a file",
        "/tmp/b;literal",
        "alice@[2001:db8::1]:/remote/drop",
    )
    assert kwargs["shell"] is False
    assert kwargs["env"]["SENTINEL"] == "not-secret"
    assert "secret-token" in kwargs["env"]["SSHPILOT_DAEMON_ASKPASS_TOKEN"]
    assert broker.cancelled
    assert "secret-token" not in repr(_request())


def test_native_backend_drains_large_stderr_without_deadlock():
    provider = _Provider()
    broker = _Broker()
    large = b"x" * (128 * 1024)
    popen = _Popen([_Process(returncode=0, stderr=large)])
    backend = NativeScpBackend(provider, broker, popen=popen)

    result = backend.run(
        _request(sources=("/tmp/source",)),
        connection_target="alice@example.test",
        connection_id=ConnectionId("demo"),
        transfer_id=TransferId("transfer-1"),
        cancel_event=threading.Event(),
    )

    assert result.returncode == 0
    assert len(result.stderr) <= 64 * 1024
    import subprocess
    assert popen.calls[0][1]["stdout"] is subprocess.DEVNULL


def test_native_backend_marks_modern_and_legacy_process_groups():
    provider = _Provider()
    broker = _Broker()
    popen = _Popen([
        _Process(returncode=1, stderr=b"subsystem request failed"),
        _Process(returncode=0),
    ])
    backend = NativeScpBackend(provider, broker, popen=popen)

    backend.run(
        _request(sources=("/tmp/source",)),
        connection_target="alice@example.test",
        connection_id=ConnectionId("demo"),
        transfer_id=TransferId("transfer-1"),
        cancel_event=threading.Event(),
    )

    assert all(getattr(process, "_sshpilot_process_group", False)
               for process in popen.started)
    assert popen.calls[0][1]["start_new_session"] is True
    assert popen.calls[1][1]["start_new_session"] is True


def test_native_backend_retries_once_with_legacy_protocol_for_sftp_failure():
    provider = _Provider()
    broker = _Broker()
    popen = _Popen([
        _Process(returncode=1, stderr=b"subsystem request failed"),
        _Process(returncode=0),
    ])
    backend = NativeScpBackend(provider, broker, popen=popen)

    result = backend.run(
        _request(sources=("/tmp/source",)),
        connection_target="alice@example.test",
        connection_id=ConnectionId("demo"),
        transfer_id=TransferId("transfer-1"),
        cancel_event=threading.Event(),
    )

    assert result.returncode == 0
    assert popen.calls[1][0][1] == "-O"


def test_native_backend_does_not_retry_authentication_failure():
    provider = _Provider()
    broker = _Broker()
    popen = _Popen([_Process(returncode=1, stderr=b"Permission denied (publickey)")])
    backend = NativeScpBackend(provider, broker, popen=popen)

    with pytest.raises(Exception) as exc_info:
        backend.run(
            _request(sources=("/tmp/source",)),
            connection_target="alice@example.test",
            connection_id=ConnectionId("demo"),
            transfer_id=TransferId("transfer-1"),
            cancel_event=threading.Event(),
        )

    assert getattr(exc_info.value, "code", None) is ErrorCode.TRANSFER_IO_FAILED
    assert len(popen.calls) == 1


def test_typed_client_dispatches_to_native_scp_backend(tmp_path):
    from sshpilot.api import DaemonClient
    from sshpilot.core.connection_application_service import ConnectionApplicationService
    from sshpilot.daemon import DaemonServer
    from sshpilot.daemon.server import CoreServices
    from tests.helpers.fake_connection_repository import make_test_repository
    from tests.daemon.test_transfer_runtime import (
        _FakeScpBackend as RuntimeScpBackend,
        _scp_request,
    )
    base_service = ConnectionApplicationService(make_test_repository())
    backend = RuntimeScpBackend()
    from sshpilot.daemon.operation_runtime import OperationRuntime
    services = CoreServices(
        connections=base_service,
        scp_backend=backend,
        operations=OperationRuntime(),
    )
    socket_path = tmp_path / "daemon" / "sshpilotd.sock"
    socket_path.parent.mkdir(mode=0o700, parents=True)
    server = DaemonServer(
        lambda: services,
        socket_path=socket_path,
    )
    server.start_in_thread()
    client = DaemonClient(socket_path=server.socket_path)
    try:
        request = _scp_request()
        summary = client.start_scp_transfer(request)
        assert summary.backend.value == "native_scp"
        assert summary.sftp_service_id is None
    finally:
        client.close()
        server.shutdown()
        server.wait_stopped()


def test_native_backend_cancellation_terminates_process():
    provider = _Provider()
    broker = _Broker()
    process = _Process(returncode=None)
    popen = _Popen([process])
    backend = NativeScpBackend(provider, broker, popen=popen)
    cancelled = threading.Event()
    cancelled.set()

    with pytest.raises(Exception) as exc_info:
        backend.run(
            _request(sources=("/tmp/source",)),
            connection_target="alice@example.test",
            connection_id=ConnectionId("demo"),
            transfer_id=TransferId("transfer-1"),
            cancel_event=cancelled,
        )

    assert getattr(exc_info.value, "code", None) is ErrorCode.OPERATION_CANCELLED
    assert process.terminated or process.killed


def test_native_backend_scopes_interactions_to_public_transfer_id():
    """SCP interactions use the public TransferId, never a private scope."""
    provider = _Provider()
    broker = _Broker()
    popen = _Popen([_Process()])
    backend = NativeScpBackend(provider, broker, popen=popen)

    backend.run(
        _request(),
        connection_target="alice@example.test",
        connection_id=ConnectionId("demo"),
        transfer_id=TransferId("transfer-7"),
        cancel_event=threading.Event(),
    )

    assert broker.prepared, "no interaction launch prepared"
    scope_id = broker.prepared[0][2]["scope_id"]
    # The interaction scope IS the public transfer id — the one value the
    # frontend learns from TransferSummary. No independent random scope is
    # generated (previously ``scp-<connection>-<id(cancel_event)>``).
    assert str(scope_id) == "transfer-7"
    assert str(scope_id).startswith("scp-") is False
    assert broker.cancelled == [SessionId("transfer-7")]


def test_native_backend_commits_remembered_secret_before_cleanup():
    """mark_authenticated runs before cancel_session on success only."""
    provider = _Provider()
    broker = _Broker()
    popen = _Popen([_Process(returncode=0)])
    backend = NativeScpBackend(provider, broker, popen=popen)

    result = backend.run(
        _request(sources=("/tmp/source",)),
        connection_target="alice@example.test",
        connection_id=ConnectionId("demo"),
        transfer_id=TransferId("transfer-9"),
        cancel_event=threading.Event(),
    )

    assert result.returncode == 0
    assert broker.authenticated == [SessionId("transfer-9")]
    assert broker.cancelled == [SessionId("transfer-9")]


def test_native_backend_failure_does_not_commit_remembered_secret():
    provider = _Provider()
    broker = _Broker()
    popen = _Popen([_Process(returncode=1, stderr=b"Permission denied (publickey)")])
    backend = NativeScpBackend(provider, broker, popen=popen)

    with pytest.raises(Exception):
        backend.run(
            _request(sources=("/tmp/source",)),
            connection_target="alice@example.test",
            connection_id=ConnectionId("demo"),
            transfer_id=TransferId("transfer-9"),
            cancel_event=threading.Event(),
        )

    assert broker.authenticated == []
    assert broker.cancelled == [SessionId("transfer-9")]


def test_full_server_scp_interaction_visible_to_owner_client(tmp_path):
    """End-to-end: the owning client can see/answer an SCP transfer prompt.

    The SCP backend scopes its interactions to the public TransferId. Before
    the transfer-eligibility branch in ``_client_can_interact``, the daemon hid
    the prompt from every client and the transfer stalled.
    """
    from sshpilot.api import DaemonClient
    from sshpilot.api.models import (
        InteractionDecisionRequest,
        InteractionType,
        PasswordPrompt,
        RememberPolicy,
        SecretDecision,
    )
    from sshpilot.core.connection_application_service import ConnectionApplicationService
    from sshpilot.daemon import DaemonServer
    from sshpilot.daemon.interaction_broker import InteractionBroker
    from sshpilot.daemon.operation_runtime import OperationRuntime
    from sshpilot.daemon.server import CoreServices
    from tests.daemon.test_transfer_runtime import _scp_request
    from tests.helpers.fake_connection_repository import make_test_repository

    broker = InteractionBroker(
        password_lookup=lambda _connection_id: None,
        password_store=lambda _connection_id, _value: True,
    )
    release = threading.Event()
    received: list = []

    class _PromptingScpBackend:
        """Runs the real broker askpass prep, then creates a password
        interaction for the transfer scope and blocks until it is answered."""

        supported = True

        def target_for_connection(self, connection_id):
            return "alice@example.test"

        def run(self, request, *, connection_target, connection_id, transfer_id, cancel_event):
            from sshpilot.api.models.common import SessionId

            _argv, _env = broker.prepare_operation_launch(
                ("/usr/bin/scp", "-F", "/tmp/ssh config", "alice@example.test:/x"),
                {"PATH": "/usr/bin"},
                scope_id=SessionId(str(transfer_id)),
                connection_id=connection_id,
                hostname=connection_target,
            )
            interaction = broker.create(
                session_id=SessionId(str(transfer_id)),
                connection_id=connection_id,
                interaction_type=InteractionType.PASSWORD,
                prompt=PasswordPrompt(
                    username="alice",
                    hostname="example.test",
                    port=22,
                    attempt=1,
                    can_remember=True,
                    stored_secret_available=False,
                ),
                attempt=1,
            )
            result = broker.wait_for_result(interaction.id)
            if result is not None and result.secret is not None:
                received.append(bytes(result.secret))
                result.clear()
            release.set()
            return SimpleNamespace(returncode=0, stderr="")

    services = CoreServices(
        connections=ConnectionApplicationService(make_test_repository()),
        scp_backend=_PromptingScpBackend(),
        operations=OperationRuntime(),
    )
    socket_path = tmp_path / "daemon" / "sshpilotd.sock"
    socket_path.parent.mkdir(mode=0o700, parents=True)
    server = DaemonServer(
        lambda: services,
        socket_path=socket_path,
        interaction_broker_factory=lambda _session_runtime: broker,
    )
    server.start_in_thread()
    client = DaemonClient(socket_path=server.socket_path)
    try:
        summary = client.start_scp_transfer(_scp_request())

        interactions: list = []
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            interactions = client.list_interactions()
            if any(
                item.type is InteractionType.PASSWORD
                for item in interactions
            ):
                break
            time.sleep(0.05)
        else:
            pytest.fail(
                "no SCP password interaction visible to the owner client; "
                f"last list={[(str(i.type), str(i.session_id)) for i in interactions]}"
            )

        password = next(
            item
            for item in interactions
            if item.type is InteractionType.PASSWORD
        )
        # The core invariant: interaction.session_id == public transfer id.
        assert password.session_id == summary.id

        claim = client.claim_interaction(password.id)
        client.respond_to_interaction(
            InteractionDecisionRequest(
                interaction_id=password.id,
                secret_decision=SecretDecision.SUBMIT,
                remember_policy=RememberPolicy.DO_NOT_STORE,
            )
        )
        client.send_interaction_secret(
            password.id, claim.nonce, bytearray(b"hunter2")
        )
        assert release.wait(20)
        assert received == [b"hunter2"]
    finally:
        release.set()
        client.close()
        server.shutdown()
        server.wait_stopped()
        broker.close()

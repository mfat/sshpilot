"""Daemon-side regression tests for ssh-copy-id interaction scoping.

Covers the primary architectural invariant: every daemon-owned interactive
resource has ONE public interaction scope known by both daemon and frontend.
For key deployment (and authorized-key removal) that scope is the public
``OperationId``; interactions created by the operation must carry
``session_id == operation_id`` — never a private random scope.

Also covers the credential-remember lifecycle: ``mark_authenticated`` must run
before the interaction scope is cancelled on success, and must not run on
failure/cancellation/startup error.
"""

from __future__ import annotations

import io
import json
import socket
import struct
import threading
import time
from types import SimpleNamespace


from sshpilot.api.models import (
    ClientId,
    ConnectionId,
    InteractionDecisionRequest,
    InteractionType,
    RememberPolicy,
    SecretDecision,
    SessionId,
)
from sshpilot.api.models.identity import DeployKeyRequest
from sshpilot.api.models.keys import KeyStoreScope, KeyList, KeySummary
from sshpilot.api.models.operations import OperationId, OperationKind, OperationState
from sshpilot.api.transport.secret_frames import SecretFrame, SecretFrameKind
from sshpilot.core.identity_service import IdentityStateService
from sshpilot.daemon.identity_service import DaemonIdentityService
from sshpilot.daemon.interaction_broker import InteractionBroker
from sshpilot.daemon.operation_runtime import OperationHandle, OperationRuntime

CLIENT_ID = ClientId("client:scope-test")


class _Completed:
    """Fake Popen-compatible result used by the daemon identity service."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = io.StringIO(stdout)
        self.stderr = stderr

    def communicate(self, *_args, **_kwargs):
        if isinstance(self.stdout, io.StringIO):
            return self.stdout.read(), self.stderr
        return self.stdout, self.stderr

    def wait(self, *_args, **_kwargs):
        return self.returncode

    def terminate(self):
        return None

    def kill(self):
        return None


class _KeyService:
    def list_keys(self, _request):
        return KeyList(
            keys=(
                KeySummary(
                    key_id="key:one",
                    name="id_ed25519",
                    private_path="/home/test/.ssh/id_ed25519",
                    public_path="/home/test/.ssh/id_ed25519.pub",
                    public_key_available=True,
                ),
            )
        )

    def read_public_key(self, request):
        assert request.key_id == "key:one"
        return SimpleNamespace(text="ssh-ed25519 AAAA test\n")


class _Provider:
    """Target carries ``user@host`` so the broker derives display metadata."""

    def prepare_copy_id_launch(self, connection_id, public_path, *, force=False):
        return ["ssh-copy-id", "-i", public_path, "alice@example.test"], {"PATH": "/usr/bin"}

    def prepare_remote_command_launch(self, connection_id, command):
        return ["ssh", "alice@example.test", command], {"PATH": "/usr/bin"}


class _RecordingBroker:
    """Records the interaction-scope lifecycle a real broker would perform."""

    def __init__(self):
        self.calls = []

    def prepare_operation_launch(self, argv, environment, *, scope_id, **kwargs):
        self.calls.append(("prepare", scope_id))
        return tuple(argv), dict(environment)

    def mark_authenticated(self, session_id):
        self.calls.append(("mark_authenticated", session_id))

    def cancel_session(self, session_id):
        self.calls.append(("cancel_session", session_id))


def _service(tmp_path, popen, broker=None):
    state = IdentityStateService(
        tmp_path / "identity.json",
        environ={"PATH": "/usr/bin"},
        ssh_copy_id_probe=lambda: True,
    )
    return DaemonIdentityService(
        state,
        _KeyService(),
        OperationRuntime(),
        launch_provider=_Provider(),
        interaction_broker=broker,
        popen=popen,
        environ={"PATH": "/usr/bin"},
    )


def _deploy_request():
    return DeployKeyRequest("HostAlias", "key:one", KeyStoreScope.DEFAULT, force=True)


def _wait_until(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------------------
# A. Operation scope identity
# ---------------------------------------------------------------------------


def test_deploy_interaction_scope_is_public_operation_id(tmp_path):
    broker = _RecordingBroker()
    service = _service(
        tmp_path,
        lambda *_args, **_kwargs: _Completed(returncode=0, stdout="Number of key(s) added: 1\n"),
        broker=broker,
    )
    summary = service.deploy_key(_deploy_request())
    try:
        # No independent random interaction scope is generated: the only
        # prepare_operation_launch scope must equal the public operation id.
        assert _wait_until(lambda: broker.calls and broker.calls[0][0] == "prepare")
        prepare_scope = broker.calls[0][1]
        assert str(prepare_scope) == str(summary.operation_id)
        # The scope is the operation's own public identifier, never a second
        # freshly allocated operation-like id.
        assert prepare_scope == SessionId(str(summary.operation_id))
    finally:
        service._operations.shutdown()


def test_authorized_key_removal_scopes_prompts_to_operation_id(tmp_path):
    broker = _RecordingBroker()
    service = _service(
        tmp_path,
        lambda *_args, **_kwargs: _Completed(
            returncode=0, stdout="ssh-ed25519 AAAAC3 test@example\n"
        ),
        broker=broker,
    )
    # A real OperationHandle so ``_run_capture`` can set/clear the process.
    handle = OperationHandle(OperationRuntime(), OperationId("operation-42"))
    result = service._run_remote_text(
        ConnectionId("HostAlias"),
        "cat authorized_keys",
        None,
        handle=handle,
    )
    assert "ssh-ed25519" in result
    assert broker.calls[0][0] == "prepare"
    assert str(broker.calls[0][1]) == "operation-42"
    assert broker.calls[-1][0] == "cancel_session"


# ---------------------------------------------------------------------------
# H. Credential-remember lifecycle
# ---------------------------------------------------------------------------


def test_deploy_success_commits_remembered_secret_before_cleanup(tmp_path):
    broker = _RecordingBroker()
    service = _service(
        tmp_path,
        lambda *_args, **_kwargs: _Completed(returncode=0, stdout="Number of key(s) added: 1\n"),
        broker=broker,
    )
    summary = service.deploy_key(_deploy_request())
    try:
        assert _wait_until(lambda: any(call[0] == "mark_authenticated" for call in broker.calls))
        kinds = [call[0] for call in broker.calls]
        assert kinds.index("mark_authenticated") < kinds.index("cancel_session")
        assert broker.calls[-1][0] == "cancel_session"
        # The scope cleaned up is the same public operation scope.
        assert broker.calls[-1][1] == SessionId(str(summary.operation_id))
    finally:
        service._operations.shutdown()


def test_deploy_failure_does_not_commit_remembered_secret(tmp_path):
    broker = _RecordingBroker()
    service = _service(
        tmp_path,
        lambda *_args, **_kwargs: _Completed(
            returncode=1, stdout="Permission denied (publickey).\n"
        ),
        broker=broker,
    )
    summary = service.deploy_key(_deploy_request())
    try:
        assert _wait_until(
            lambda: (
                service._operations.get_operation(summary.operation_id).state
                is OperationState.FAILED
            )
        )
        kinds = [call[0] for call in broker.calls]
        assert "mark_authenticated" not in kinds
        assert kinds[-1] == "cancel_session"
    finally:
        service._operations.shutdown()


def test_deploy_startup_failure_cleans_up_without_commit(tmp_path):
    broker = _RecordingBroker()

    def _popen(*_args, **_kwargs):
        raise OSError("ssh-copy-id could not be started")

    service = _service(tmp_path, _popen, broker=broker)
    summary = service.deploy_key(_deploy_request())
    try:
        assert _wait_until(
            lambda: (
                service._operations.get_operation(summary.operation_id).state
                is OperationState.FAILED
            )
        )
        kinds = [call[0] for call in broker.calls]
        assert "mark_authenticated" not in kinds
        assert kinds[0] == "prepare"
        assert kinds[-1] == "cancel_session"
    finally:
        service._operations.shutdown()


# ---------------------------------------------------------------------------
# Eligibility: the operation's owner client can claim its interactions
# ---------------------------------------------------------------------------


def test_operation_runtime_client_can_interact_owner_only():
    runtime = OperationRuntime()
    owner = ClientId("client:owner")
    other = ClientId("client:other")
    summary = runtime.start_operation(
        OperationKind.KEY_DEPLOYMENT,
        lambda _handle: "done",
        owner_client_id=owner,
    )
    try:
        assert runtime.client_can_interact(summary.operation_id, owner)
        assert not runtime.client_can_interact(summary.operation_id, other)
        assert not runtime.client_can_interact(OperationId("operation-999999"), owner)
        # An operation-`` prefixed scope that is not a registered operation
        # (e.g. the private ssh-add scope) must stay denied.
        assert not runtime.client_can_interact(OperationId("operation-0"), owner)
    finally:
        runtime.shutdown()


# ---------------------------------------------------------------------------
# B. Real-broker password flow: interaction created for the operation scope,
#    answered, secret committed after success.
# ---------------------------------------------------------------------------


def test_real_broker_password_flow_reaches_deploy_operation(tmp_path):
    release = threading.Event()
    stored: list[str] = []
    calls: list[tuple[list, dict]] = []
    broker = InteractionBroker(
        password_lookup=lambda _connection_id: None,
        password_store=lambda _connection_id, value: stored.append(value) or True,
    )

    class _BlockingProcess:
        """ssh-copy-id child: emits a line, then blocks until the password
        interaction is answered and the test releases the run."""

        def __init__(self):
            self.returncode = None
            self.stdout = self._lines()

        def _lines(self):
            yield "Trying to install the key...\n"
            release.wait(20)
            yield "Number of key(s) added: 1\n"

        def wait(self):
            release.wait(20)
            self.returncode = 0
            return 0

        def terminate(self):
            release.set()

        def kill(self):
            release.set()

    def popen(argv, **kwargs):
        calls.append((list(argv), dict(kwargs)))
        return _BlockingProcess()

    service = _service(tmp_path, popen, broker=broker)
    summary = service.deploy_key(_deploy_request())
    try:
        assert _wait_until(lambda: bool(calls))
        token = calls[0][1]["env"]["SSHPILOT_DAEMON_ASKPASS_TOKEN"]
        assert token

        answered: list = []

        def _askpass():
            # Exactly what the daemon askpass worker runs when OpenSSH asks
            # for the password (REQUIRE=force, no TTY).
            secret = broker._resolve_askpass_secret(token, "alice@example.test's password: ")
            answered.append(bytes(secret) if secret is not None else None)

        thread = threading.Thread(target=_askpass, daemon=True)
        thread.start()
        assert _wait_until(
            lambda: any(item.type is InteractionType.PASSWORD for item in broker.list(CLIENT_ID))
        )
        interaction = next(
            item for item in broker.list(CLIENT_ID) if item.type is InteractionType.PASSWORD
        )
        # The core invariant: interaction.session_id == operation_id.
        assert interaction.session_id == SessionId(str(summary.operation_id))
        # Display metadata was derived from the launch target so the prompt
        # was constructible (an empty hostname would fail validation).
        assert interaction.prompt.hostname == "example.test"
        assert interaction.prompt.username == "alice"

        claim = broker.claim(interaction.id, CLIENT_ID)
        broker.respond(
            InteractionDecisionRequest(
                interaction_id=interaction.id,
                secret_decision=SecretDecision.SUBMIT,
                remember_policy=RememberPolicy.STORE_AFTER_SUCCESS,
            ),
            CLIENT_ID,
        )
        broker.submit_secret(
            SecretFrame(
                kind=SecretFrameKind.RESPONSE,
                interaction_id=interaction.id,
                nonce=bytes.fromhex(claim.nonce),
                secret=bytearray(b"hunter2"),
            ),
            CLIENT_ID,
        )
        thread.join(20)
        assert answered == [b"hunter2"]
        release.set()

        assert _wait_until(
            lambda: (
                service._operations.get_operation(summary.operation_id).state
                is OperationState.SUCCEEDED
            )
        )
        # mark_authenticated ran before the scope was cancelled, so the
        # remembered password was committed.
        assert stored == ["hunter2"]
    finally:
        release.set()
        broker.close()
        service._operations.shutdown()


def test_full_server_password_interaction_visible_to_owner_client(tmp_path):
    """End-to-end: the GTK client can see/answer the deploy password prompt.

    Drives the real askpass protocol over the daemon's helper socket. Before
    the operation-eligibility fix, ``list_interactions()`` returned nothing for
    the owner client and the prompt was invisible.
    """

    from sshpilot.api import DaemonClient
    from sshpilot.core.connection_application_service import (
        ConnectionApplicationService,
    )
    from sshpilot.daemon import DaemonServer
    from sshpilot.daemon.server import CoreServices
    from tests.helpers.fake_connection_repository import make_test_repository

    release = threading.Event()
    calls: list[tuple[list, dict]] = []

    class _BlockingProcess:
        def __init__(self):
            self.returncode = None
            self.stdout = self._lines()

        def _lines(self):
            yield "Trying to install the key...\n"
            release.wait(30)
            yield "Number of key(s) added: 1\n"

        def wait(self):
            release.wait(30)
            self.returncode = 0
            return 0

        def terminate(self):
            release.set()

        def kill(self):
            release.set()

    def popen(argv, **kwargs):
        calls.append((list(argv), dict(kwargs)))
        return _BlockingProcess()

    class _RootProvider:
        """Target matches the askpass prompt used below (root@192.168.8.1)."""

        def prepare_copy_id_launch(self, connection_id, public_path, *, force=False):
            return [
                "ssh-copy-id",
                "-i",
                public_path,
                "root@192.168.8.1",
            ], {"PATH": "/usr/bin"}

    state = IdentityStateService(
        tmp_path / "identity.json",
        environ={"PATH": "/usr/bin"},
        ssh_copy_id_probe=lambda: True,
    )
    operations = OperationRuntime()
    identity = DaemonIdentityService(
        state,
        _KeyService(),
        operations,
        launch_provider=_RootProvider(),
        popen=popen,
        environ={"PATH": "/usr/bin"},
    )
    core = CoreServices(
        connections=ConnectionApplicationService(make_test_repository()),
        identity=identity,
        operations=operations,
    )
    socket_dir = tmp_path / "daemon"
    socket_dir.mkdir(mode=0o700)
    server = DaemonServer(
        lambda: core,
        socket_path=socket_dir / "sshpilotd.sock",
    )
    server.start_in_thread()
    client = DaemonClient(socket_path=server.socket_path)
    received: list = []
    try:
        summary = client.deploy_key(_deploy_request())
        assert _wait_until(lambda: bool(calls))
        env = calls[0][1]["env"]
        token = env["SSHPILOT_DAEMON_ASKPASS_TOKEN"]
        askpass_socket = env["SSHPILOT_DAEMON_ASKPASS_SOCKET"]

        def _askpass():
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(askpass_socket)
                payload = json.dumps(
                    {
                        "token": token,
                        "prompt": "root@192.168.8.1's password: ",
                    }
                ).encode("utf-8")
                sock.sendall(struct.pack(">I", len(payload)) + payload)
                length = struct.unpack(">I", sock.recv(4))[0]
                data = b""
                while len(data) < length:
                    chunk = sock.recv(length - len(data))
                    if not chunk:
                        break
                    data += chunk
                return data
            finally:
                sock.close()

        thread = threading.Thread(target=lambda: received.append(_askpass()), daemon=True)
        thread.start()

        # The owner client sees the password interaction for the operation.
        box: dict = {}
        seen: list = []

        def _probe() -> bool:
            box["interactions"] = client.list_interactions()
            hit = any(item.type is InteractionType.PASSWORD for item in box["interactions"])
            seen.append((len(box["interactions"]), [str(i.type) for i in box["interactions"]], hit))
            return hit

        assert _wait_until(_probe, timeout=20.0), f"no password seen; polls={seen[:10]}"
        interactions = box["interactions"]
        password = next(item for item in interactions if item.type is InteractionType.PASSWORD)
        assert password.session_id == summary.operation_id
        assert password.prompt.hostname == "192.168.8.1"
        assert password.prompt.username == "root"

        claim = client.claim_interaction(password.id)
        client.respond_to_interaction(
            InteractionDecisionRequest(
                interaction_id=password.id,
                secret_decision=SecretDecision.SUBMIT,
                remember_policy=RememberPolicy.DO_NOT_STORE,
            )
        )
        client.send_interaction_secret(password.id, claim.nonce, bytearray(b"hunter2"))
        thread.join(30)
        assert received and received[0] == b"hunter2"
        release.set()
        assert _wait_until(
            lambda: client.get_operation(summary.operation_id).state.value == "succeeded"
        )
    finally:
        release.set()
        server.shutdown()
        assert server.wait_stopped()


def test_operation_launch_derives_display_identity_from_target():
    """prepare_operation_launch without hostname builds a constructible prompt."""
    broker = InteractionBroker(
        password_lookup=lambda _connection_id: None,
        password_store=lambda _connection_id, _value: True,
    )
    try:
        argv, env = broker.prepare_operation_launch(
            ["ssh-copy-id", "-i", "/tmp/key.pub", "alice@example.test"],
            {"PATH": "/usr/bin"},
            scope_id=SessionId("operation-1"),
            connection_id=ConnectionId("HostAlias"),
        )
        assert argv[0] == "ssh-copy-id"
        with broker._condition:
            context = broker._askpass_contexts[env["SSHPILOT_DAEMON_ASKPASS_TOKEN"]]
        assert context.hostname == "example.test"
        assert context.username == "alice"
        # An IPv6 bracket target stays safe and non-empty.
        argv, env = broker.prepare_operation_launch(
            ["scp", "alice@[2001:db8::1]"],
            {"PATH": "/usr/bin"},
            scope_id=SessionId("operation-2"),
            connection_id=ConnectionId("HostAlias"),
        )
        with broker._condition:
            context = broker._askpass_contexts[env["SSHPILOT_DAEMON_ASKPASS_TOKEN"]]
        assert context.hostname == "2001:db8::1"
    finally:
        broker.close()

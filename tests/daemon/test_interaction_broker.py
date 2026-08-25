import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from typing import Iterator

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models import (
    ClientId,
    ConnectionId,
    ExecutionInteractionMode,
    HostKeyDecision,
    HostKeyPrompt,
    HostKeyStatus,
    InteractionDecisionRequest,
    InteractionState,
    InteractionType,
    PasswordPrompt,
    PresencePrompt,
    RememberPolicy,
    SecretDecision,
    SessionId,
)
from sshpilot.api.transport.secret_frames import SecretFrame, SecretFrameKind
from sshpilot.daemon.interaction_broker import InteractionBroker, InteractionResult
from sshpilot.daemon.session_runtime import SessionLaunchSpec

SESSION_ID = SessionId("session-1")
CONNECTION_ID = ConnectionId(
    "conn-2"
)
CLIENT_A = ClientId("client-a")
CLIENT_B = ClientId("client-b")


@pytest.fixture
def broker() -> Iterator[InteractionBroker]:
    instance = InteractionBroker(secret_timeout=2, host_key_timeout=2)
    yield instance
    instance.close()


def _password(broker: InteractionBroker):
    return broker.create(
        session_id=SESSION_ID,
        connection_id=CONNECTION_ID,
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


def test_claim_release_and_disconnect_takeover(broker: InteractionBroker) -> None:
    summary = _password(broker)
    claim = broker.claim(summary.id, CLIENT_A)
    assert claim.responder_client_id == CLIENT_A
    with pytest.raises(SshPilotError) as conflict:
        broker.claim(summary.id, CLIENT_B)
    assert conflict.value.code is ErrorCode.INTERACTION_CLAIM_CONFLICT

    broker.disconnect_client(CLIENT_A)
    replacement = broker.claim(summary.id, CLIENT_B)
    assert replacement.responder_client_id == CLIENT_B


def test_secret_is_responder_bound_one_use_and_cleared(
    broker: InteractionBroker,
) -> None:
    summary = _password(broker)
    claim = broker.claim(summary.id, CLIENT_A)
    broker.respond(
        InteractionDecisionRequest(
            interaction_id=summary.id,
            secret_decision=SecretDecision.SUBMIT,
            remember_policy=RememberPolicy.STORE_AFTER_SUCCESS,
        ),
        CLIENT_A,
    )
    wrong = SecretFrame(
        kind=SecretFrameKind.RESPONSE,
        interaction_id=summary.id,
        nonce=bytes.fromhex(claim.nonce),
        secret=bytearray(b"not-authorised"),
    )
    with pytest.raises(SshPilotError) as denied:
        broker.submit_secret(wrong, CLIENT_B)
    assert denied.value.code is ErrorCode.INTERACTION_RESPONDER_UNAUTHORIZED
    assert wrong.secret == bytearray()

    frame = SecretFrame(
        kind=SecretFrameKind.RESPONSE,
        interaction_id=summary.id,
        nonce=bytes.fromhex(claim.nonce),
        secret=bytearray(b"correct"),
    )
    broker.submit_secret(frame, CLIENT_A)
    assert frame.secret == bytearray()
    result = broker.wait_for_result(summary.id)
    assert result is not None
    assert bytes(result.secret or b"") == b"correct"
    assert result.remember_policy is RememberPolicy.STORE_AFTER_SUCCESS
    assert broker.wait_for_result(summary.id) is None
    result.clear()


def test_request_client_secret_registers_the_owner_so_it_is_visible(
    broker: InteractionBroker,
) -> None:
    """Regression: SecretBackendService used to call broker.create() +
    wait_for_result() directly for master-password/Bitwarden/backup
    passphrase prompts, never registering a direct-scope owner. The server
    only forwards INTERACTION_CREATED/STATE_CHANGED events for a session id
    with no recognized prefix (secret-session-N matches none of sftp-/
    forward-/operation-/transfer-/key-operation-) to a client that
    broker.client_owns_direct_scope() confirms owns it — so those
    interactions were created but invisible to every client, and silently
    expired with no dialog ever shown. request_client_secret() is the
    broker API that actually registers the owner; this proves it does, and
    that ONLY the owning client can see the interaction while it's pending."""
    session_id = SessionId("secret-session-owner-test")
    connection_id = ConnectionId("secret-owner-test")
    started = threading.Event()
    result_box = []

    def _run():
        started.set()
        secret = broker.request_client_secret(
            owner_client_id=CLIENT_A,
            session_id=session_id,
            connection_id=connection_id,
            interaction_type=InteractionType.PASSWORD,
            prompt=PasswordPrompt(
                username="Secret backend",
                hostname="keepassxc",
                port=22,
                attempt=1,
                can_remember=True,
                stored_secret_available=False,
            ),
        )
        result_box.append(secret)

    thread = threading.Thread(target=_run)
    thread.start()
    started.wait(1)

    deadline = time.monotonic() + 2
    summary = None
    while time.monotonic() < deadline and summary is None:
        for candidate in broker.list(CLIENT_A):
            if candidate.session_id == session_id:
                summary = candidate
                break
    assert summary is not None, "interaction never became visible to its owner"

    # The core of the fix: the owning client sees it, an unrelated client does not.
    assert broker.client_owns_direct_scope(session_id, CLIENT_A) is True
    assert broker.client_owns_direct_scope(session_id, CLIENT_B) is False
    assert all(s.session_id != session_id for s in broker.list(CLIENT_B))

    claim = broker.claim(summary.id, CLIENT_A)
    broker.respond(
        InteractionDecisionRequest(
            interaction_id=summary.id,
            secret_decision=SecretDecision.SUBMIT,
            remember_policy=RememberPolicy.DO_NOT_STORE,
        ),
        CLIENT_A,
    )
    broker.submit_secret(
        SecretFrame(
            kind=SecretFrameKind.RESPONSE,
            interaction_id=summary.id,
            nonce=bytes.fromhex(claim.nonce),
            secret=bytearray(b"hunter2"),
        ),
        CLIENT_A,
    )
    thread.join(2)
    assert not thread.is_alive()
    assert bytes(result_box[0] or b"") == b"hunter2"
    # Scope ownership is released once the request completes.
    assert broker.client_owns_direct_scope(session_id, CLIENT_A) is False


def test_host_key_answer_is_typed_and_final(broker: InteractionBroker) -> None:
    summary = broker.create(
        session_id=SESSION_ID,
        connection_id=CONNECTION_ID,
        interaction_type=InteractionType.HOST_KEY_CONFIRMATION,
        prompt=HostKeyPrompt(
            hostname="example.test",
            port=22,
            key_type="ssh-ed25519",
            fingerprint="SHA256:abc",
            status=HostKeyStatus.UNKNOWN,
        ),
    )
    broker.claim(summary.id, CLIENT_A)
    broker.respond(
        InteractionDecisionRequest(
            interaction_id=summary.id,
            host_key_decision=HostKeyDecision.ACCEPT,
        ),
        CLIENT_A,
    )
    result = broker.wait_for_result(summary.id)
    assert result is not None
    assert result.decision is HostKeyDecision.ACCEPT
    assert broker.get(summary.id, CLIENT_A).state is InteractionState.ANSWERED


def test_expiry_wakes_waiter_and_rejects_late_response() -> None:
    broker = InteractionBroker(secret_timeout=0.03, host_key_timeout=0.03)
    try:
        summary = _password(broker)
        completed = threading.Event()

        def wait() -> None:
            assert broker.wait_for_result(summary.id) is None
            completed.set()

        thread = threading.Thread(target=wait)
        thread.start()
        assert completed.wait(1)
        thread.join(1)
        assert broker.get(summary.id, CLIENT_A).state is InteractionState.EXPIRED
        with pytest.raises(SshPilotError) as expired:
            broker.claim(summary.id, CLIENT_A)
        assert expired.value.code is ErrorCode.INTERACTION_EXPIRED
    finally:
        broker.close()


def test_request_client_secret_with_status_reports_expired_not_cancelled(
    broker: InteractionBroker,
) -> None:
    """Issue #1200: a timed-out backup-encryption prompt must be reported as
    EXPIRED, not collapsed into the same ``None`` a user cancellation returns —
    the ``timeout`` override (used for the shorter backup-encryption prompt) is
    exercised here, independent of the broker's configured default."""
    secret, state = broker.request_client_secret_with_status(
        owner_client_id=CLIENT_A,
        session_id=SessionId("secret-timeout-1"),
        connection_id=ConnectionId("secret-timeout-1"),
        interaction_type=InteractionType.PASSWORD,
        prompt=PasswordPrompt(
            username="Secret backend", hostname="secret backend", port=22,
            attempt=1, can_remember=False, stored_secret_available=False),
        timeout=0.03,
    )
    assert secret is None
    assert state is InteractionState.EXPIRED


def test_request_client_secret_with_status_reports_explicit_cancel(
    broker: InteractionBroker,
) -> None:
    """A real user cancellation must still report CANCELLED, distinct from
    a timeout — see the EXPIRED counterpart above."""
    session_id = SessionId("secret-cancel-1")
    connection_id = ConnectionId("secret-cancel-1")
    outcome: dict = {}

    def waiter() -> None:
        secret, state = broker.request_client_secret_with_status(
            owner_client_id=CLIENT_A,
            session_id=session_id,
            connection_id=connection_id,
            interaction_type=InteractionType.PASSWORD,
            prompt=PasswordPrompt(
                username="Secret backend", hostname="secret backend", port=22,
                attempt=1, can_remember=False, stored_secret_available=False),
        )
        outcome["secret"] = secret
        outcome["state"] = state

    thread = threading.Thread(target=waiter)
    thread.start()
    try:
        interaction = None
        deadline = time.monotonic() + 1.0
        while interaction is None and time.monotonic() < deadline:
            for candidate in broker.list(CLIENT_A):
                if candidate.session_id == session_id:
                    interaction = candidate
                    break
            if interaction is None:
                time.sleep(0.01)
        assert interaction is not None, "interaction was never created"
        broker.claim(interaction.id, CLIENT_A)
        broker.respond(
            InteractionDecisionRequest(
                interaction_id=interaction.id,
                secret_decision=SecretDecision.CANCEL,
                remember_policy=RememberPolicy.DO_NOT_STORE,
            ),
            CLIENT_A,
        )
    finally:
        thread.join(1)
    assert not thread.is_alive()
    assert outcome["secret"] is None
    assert outcome["state"] is InteractionState.CANCELLED


def test_backup_encryption_timeout_constant_is_shorter_than_default_secret_timeout() -> None:
    """Pins the ~30s design target for the backup-encryption prompt (issue
    #1200): far shorter than the general 120s secret-interaction timeout, so
    an unattended export fails fast instead of looking hung for two minutes."""
    from sshpilot.daemon.interaction_broker import (
        DEFAULT_BACKUP_ENCRYPTION_INTERACTION_TIMEOUT,
        DEFAULT_SECRET_INTERACTION_TIMEOUT,
    )

    assert DEFAULT_BACKUP_ENCRYPTION_INTERACTION_TIMEOUT == 30.0
    assert (
        DEFAULT_BACKUP_ENCRYPTION_INTERACTION_TIMEOUT
        < DEFAULT_SECRET_INTERACTION_TIMEOUT
    )


def test_session_cancel_and_close_leave_no_active_interactions(
    broker: InteractionBroker,
) -> None:
    summary = _password(broker)
    broker.cancel_session(SESSION_ID)
    assert broker.get(summary.id, CLIENT_A).state is InteractionState.CANCELLED
    assert tuple(broker.active_ids()) == ()


@pytest.mark.integration
def test_private_askpass_helper_delivers_only_one_brokered_secret(
    broker: InteractionBroker,
    monkeypatch,
) -> None:
    monkeypatch.setattr(broker, "_effective_ssh_config", lambda _argv, _environment=None: {})
    _argv, environment = broker.prepare_launch(
        SessionLaunchSpec(
            session_id=SESSION_ID,
            connection_id=CONNECTION_ID,
            protocol="ssh",
            hostname="example.test",
            username="alice",
            port=22,
        ),
        lambda _connection_id, **_kwargs: (
            ("/usr/bin/ssh", "example"),
            {
                "HOME": "/tmp",
                "PATH": os.environ.get("PATH", ""),
                "SSHPILOT_DAEMON_ASKPASS_ACTIVE": "1",
            },
        ),
    )
    # Helper must work without PYTHONPATH (OpenSSH may spawn with a reduced env).
    helper_env = {
        key: value
        for key, value in environment.items()
        if key != "PYTHONPATH"
    }
    helper = subprocess.Popen(
        (environment["SSH_ASKPASS"], "alice@example.test's password:"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=helper_env,
    )
    deadline = time.monotonic() + 2
    interactions = []
    while time.monotonic() < deadline:
        interactions = broker.list(CLIENT_A)
        if interactions:
            break
        time.sleep(0.01)
    assert len(interactions) == 1
    interaction = interactions[0]
    claim = broker.claim(interaction.id, CLIENT_A)
    broker.respond(
        InteractionDecisionRequest(
            interaction_id=interaction.id,
            secret_decision=SecretDecision.SUBMIT,
        ),
        CLIENT_A,
    )
    broker.submit_secret(
        SecretFrame(
            kind=SecretFrameKind.RESPONSE,
            interaction_id=interaction.id,
            nonce=bytes.fromhex(claim.nonce),
            secret=bytearray(b"brokered-value"),
        ),
        CLIENT_A,
    )
    stdout, stderr = helper.communicate(timeout=2)
    assert helper.returncode == 0
    assert stdout == b"brokered-value\n"
    assert stderr == b""


def test_frozen_build_askpass_helper_dispatches_via_internal_flag(monkeypatch) -> None:
    # sys.executable is this application's own binary in a frozen build, not
    # a Python interpreter: a "#!{sys.executable}" shebang over inlined
    # Python source (the non-frozen branch, exercised above) would make
    # OpenSSH's exec of SSH_ASKPASS just relaunch the GUI instead of running
    # the askpass helper — this is what actually caused GH #1166 for any
    # password/passphrase connection once the PTY spawn itself is fixed.
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/fake/SSHPilot", raising=False)

    instance = InteractionBroker(secret_timeout=2, host_key_timeout=2)
    try:
        content = instance._askpass_helper_path.read_text(encoding="utf-8")
    finally:
        instance.close()

    assert content == '#!/bin/sh\nexec "/fake/SSHPilot" --internal-askpass "$@"\n'


@pytest.mark.integration
def test_frozen_build_askpass_helper_round_trips_a_real_secret(tmp_path, monkeypatch) -> None:
    """Real end-to-end companion to the PTY spawn regression test: does the
    frozen-build askpass wrapper script, executed for real (not mocked),
    actually deliver a brokered secret? Uses the same self-dispatching fake
    frozen launcher as the PTY test — sys.executable here is that launcher,
    so the daemon's own askpass wrapper script really does
    "exec $LAUNCHER --internal-askpass", which must reach run.py's real
    --internal-askpass dispatch and the real daemon.askpass_helper.main().
    """
    from tests.helpers.fake_frozen_launcher import write_fake_frozen_launcher

    launcher = write_fake_frozen_launcher(tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(launcher), raising=False)

    instance = InteractionBroker(secret_timeout=2, host_key_timeout=2)
    try:
        monkeypatch.setattr(
            instance, "_effective_ssh_config", lambda _argv, _environment=None: {}
        )
        _argv, environment = instance.prepare_launch(
            SessionLaunchSpec(
                session_id=SESSION_ID,
                connection_id=CONNECTION_ID,
                protocol="ssh",
                hostname="example.test",
                username="alice",
                port=22,
            ),
            lambda _connection_id, **_kwargs: (
                ("/usr/bin/ssh", "example"),
                {
                    "HOME": "/tmp",
                    "PATH": os.environ.get("PATH", ""),
                    "SSHPILOT_DAEMON_ASKPASS_ACTIVE": "1",
                },
            ),
        )
        assert environment["SSH_ASKPASS"] == str(instance._askpass_helper_path)

        helper = subprocess.Popen(
            (environment["SSH_ASKPASS"], "alice@example.test's password:"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        deadline = time.monotonic() + 5
        interactions = []
        while time.monotonic() < deadline:
            interactions = instance.list(CLIENT_A)
            if interactions:
                break
            time.sleep(0.01)
        assert len(interactions) == 1
        interaction = interactions[0]
        claim = instance.claim(interaction.id, CLIENT_A)
        instance.respond(
            InteractionDecisionRequest(
                interaction_id=interaction.id,
                secret_decision=SecretDecision.SUBMIT,
            ),
            CLIENT_A,
        )
        instance.submit_secret(
            SecretFrame(
                kind=SecretFrameKind.RESPONSE,
                interaction_id=interaction.id,
                nonce=bytes.fromhex(claim.nonce),
                secret=bytearray(b"brokered-value"),
            ),
            CLIENT_A,
        )
        stdout, stderr = helper.communicate(timeout=5)
        assert helper.returncode == 0, stderr
        assert stdout == b"brokered-value\n"
    finally:
        instance.close()


def test_prepare_launch_brokers_interactions_without_saved_secret(
    broker: InteractionBroker,
    monkeypatch,
) -> None:
    monkeypatch.setattr(broker, "_effective_ssh_config", lambda _argv, _environment=None: {})
    _argv, environment = broker.prepare_launch(
        SessionLaunchSpec(
            session_id=SESSION_ID,
            connection_id=CONNECTION_ID,
            protocol="ssh",
            hostname="example.test",
            username="alice",
            port=22,
        ),
        lambda _connection_id, **_kwargs: (
            ("/usr/bin/ssh", "example"),
            {"PATH": os.environ.get("PATH", "")},
        ),
    )
    assert environment["SSH_ASKPASS"] == str(broker._askpass_helper_path)
    assert environment["SSH_ASKPASS_REQUIRE"] == "prefer"
    assert environment["SSHPILOT_DAEMON_ASKPASS_SOCKET"] == str(
        broker._askpass_socket_path
    )


def test_prepare_launch_forwards_remote_command_to_builder_when_present(
    broker: InteractionBroker,
    monkeypatch,
) -> None:
    monkeypatch.setattr(broker, "_effective_ssh_config", lambda _argv, _environment=None: {})
    seen = {}

    def _builder(connection_id, **kwargs):
        seen["connection_id"] = connection_id
        seen["kwargs"] = kwargs
        return (("/usr/bin/ssh", "example"), {"PATH": os.environ.get("PATH", "")})

    _argv, _environment = broker.prepare_launch(
        SessionLaunchSpec(
            session_id=SESSION_ID,
            connection_id=CONNECTION_ID,
            protocol="ssh",
            hostname="example.test",
            username="alice",
            port=22,
            remote_command="docker exec -it web sh",
            force_tty=True,
        ),
        _builder,
    )
    assert seen["connection_id"] == CONNECTION_ID
    assert seen["kwargs"]["interaction_policy"] == "normal"
    assert seen["kwargs"]["remote_command"] == "docker exec -it web sh"
    assert seen["kwargs"]["force_tty"] is True


def test_prepare_launch_omits_remote_command_when_absent(
    broker: InteractionBroker,
    monkeypatch,
) -> None:
    monkeypatch.setattr(broker, "_effective_ssh_config", lambda _argv, _environment=None: {})
    seen = {}

    def _builder(connection_id, **kwargs):
        seen["kwargs"] = kwargs
        return (("/usr/bin/ssh", "example"), {"PATH": os.environ.get("PATH", "")})

    broker.prepare_launch(
        SessionLaunchSpec(
            session_id=SESSION_ID,
            connection_id=CONNECTION_ID,
            protocol="ssh",
            hostname="example.test",
            username="alice",
            port=22,
        ),
        _builder,
    )
    assert "remote_command" not in seen["kwargs"]
    assert "force_tty" not in seen["kwargs"]


def test_headless_launch_preserves_normal_environment_and_replaces_askpass(
    broker: InteractionBroker,
    monkeypatch,
) -> None:
    monkeypatch.setattr(broker, "_effective_ssh_config", lambda _argv, _environment=None: {})
    policies = []

    def builder(_connection_id, *, interaction_policy):
        policies.append(interaction_policy)
        return ("/usr/bin/ssh", "example"), {
            "PATH": "/custom/bin",
            "USER_DEFINED_AUTH": "preserved",
            "SSH_ASKPASS": "/old/helper",
            "SSH_ASKPASS_REQUIRE": "prefer",
            "SSHPILOT_ASKPASS_SOCKET": "/old/socket",
            "SSHPILOT_SESSION_PASSWORD": "staged-secret",
            "SSHPILOT_DAEMON_ASKPASS_SOCKET": "/stale/socket",
            "SSHPILOT_DAEMON_ASKPASS_TOKEN": "stale-token",
        }

    _argv, environment = broker.prepare_launch(
        SessionLaunchSpec(
            session_id=SESSION_ID,
            connection_id=CONNECTION_ID,
            protocol="ssh",
            hostname="example.test",
            username="alice",
            port=22,
        ),
        builder,
        headless=True,
    )

    assert policies == ["normal"]
    assert environment["USER_DEFINED_AUTH"] == "preserved"
    assert environment["SSH_ASKPASS_REQUIRE"] == "force"
    assert environment["SSH_ASKPASS"] == str(broker._askpass_helper_path)
    assert environment["SSHPILOT_DAEMON_ASKPASS_SOCKET"] == str(
        broker._askpass_socket_path
    )
    assert "SSHPILOT_ASKPASS_SOCKET" not in environment
    assert "SSHPILOT_SESSION_PASSWORD" not in environment


@pytest.mark.parametrize("original", ["/user/custom/modules", None])
def test_prepare_launch_does_not_modify_pythonpath(
    broker: InteractionBroker,
    monkeypatch,
    original: str | None,
) -> None:
    monkeypatch.setattr(broker, "_effective_ssh_config", lambda _argv, _environment=None: {})
    prepared_environment = {
        "PATH": "/usr/bin",
        "SSHPILOT_DAEMON_ASKPASS_SOCKET": "/stale/socket",
        "SSHPILOT_DAEMON_ASKPASS_TOKEN": "stale-token",
    }
    if original is not None:
        prepared_environment["PYTHONPATH"] = original

    _argv, environment = broker.prepare_launch(
        SessionLaunchSpec(
            session_id=SESSION_ID,
            connection_id=CONNECTION_ID,
            protocol="ssh",
            hostname="example.test",
            username="alice",
            port=22,
        ),
        lambda *_args, **_kwargs: (
            ("/usr/bin/ssh", "example"),
            prepared_environment,
        ),
    )

    if original is None:
        assert "PYTHONPATH" not in environment
    else:
        assert environment["PYTHONPATH"] == original
    assert environment["SSHPILOT_DAEMON_ASKPASS_SOCKET"] == str(
        broker._askpass_socket_path
    )
    assert environment["SSHPILOT_DAEMON_ASKPASS_TOKEN"] != "stale-token"


def test_presence_rejects_submit_and_has_dedicated_lifetime() -> None:
    instance = InteractionBroker(
        secret_timeout=0.03,
        host_key_timeout=0.03,
        presence_timeout=10,
    )
    try:
        summary = instance.create(
            session_id=SESSION_ID,
            connection_id=CONNECTION_ID,
            interaction_type=InteractionType.SECURITY_KEY_PRESENCE,
            prompt=PresencePrompt(text="Touch your security key"),
        )
        instance.claim(summary.id, CLIENT_A)
        with pytest.raises(SshPilotError):
            instance.respond(
                InteractionDecisionRequest(
                    interaction_id=summary.id,
                    secret_decision=SecretDecision.SUBMIT,
                ),
                CLIENT_A,
            )
        assert (
            instance.get(summary.id, CLIENT_A).expires_at
            - instance.get(summary.id, CLIENT_A).created_at
        ).total_seconds() == 10
        assert instance.get(summary.id, CLIENT_A).state is InteractionState.CLAIMED
    finally:
        instance.close()


@pytest.mark.parametrize(
    ("prompt", "expected_type"),
    [
        ("Enter verification code:", InteractionType.KEYBOARD_INTERACTIVE),
        ("Custom PAM response:", InteractionType.KEYBOARD_INTERACTIVE),
        ("Touch your security key", InteractionType.SECURITY_KEY_PRESENCE),
        ("Allow signing?", InteractionType.CONFIRMATION),
    ],
)
def test_daemon_routes_interactive_and_presence_prompts(
    broker: InteractionBroker,
    monkeypatch,
    prompt: str,
    expected_type: InteractionType,
) -> None:
    monkeypatch.setattr(broker, "_effective_ssh_config", lambda _argv, _environment=None: {})
    _argv, environment = broker.prepare_launch(
        SessionLaunchSpec(
            session_id=SESSION_ID,
            connection_id=CONNECTION_ID,
            protocol="ssh",
            hostname="example.test",
            username="alice",
            port=22,
        ),
        lambda _connection_id, **_kwargs: (
            ("/usr/bin/ssh", "example"),
            {"SSHPILOT_DAEMON_ASKPASS_ACTIVE": "1"},
        ),
    )
    token = environment["SSHPILOT_DAEMON_ASKPASS_TOKEN"]
    monkeypatch.setattr(broker, "wait_for_result", lambda *_a, **_k: None)
    hint = "none" if expected_type is InteractionType.SECURITY_KEY_PRESENCE else (
        "confirm" if expected_type is InteractionType.CONFIRMATION else ""
    )
    assert broker._resolve_askpass_secret(token, prompt, hint=hint) is None
    assert broker.list(CLIENT_A)[-1].type is expected_type


@pytest.mark.integration
def test_askpass_helper_disconnect_cancels_pending_interaction(
    broker: InteractionBroker,
    monkeypatch,
) -> None:
    monkeypatch.setattr(broker, "_effective_ssh_config", lambda _argv, _environment=None: {})
    _argv, environment = broker.prepare_launch(
        SessionLaunchSpec(
            session_id=SESSION_ID,
            connection_id=CONNECTION_ID,
            protocol="ssh",
            hostname="example.test",
            username="alice",
            port=22,
        ),
        lambda _connection_id, **_kwargs: (
            ("/usr/bin/ssh", "example"),
            {"PATH": os.environ.get("PATH", ""), "SSHPILOT_DAEMON_ASKPASS_ACTIVE": "1"},
        ),
    )
    request = json.dumps(
        {
            "token": environment["SSHPILOT_DAEMON_ASKPASS_TOKEN"],
            "prompt": "alice@example.test's password:",
        },
        separators=(",", ":"),
    ).encode()
    transport = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    transport.connect(environment["SSHPILOT_DAEMON_ASKPASS_SOCKET"])
    transport.sendall(struct.pack(">I", len(request)) + request)
    deadline = time.monotonic() + 1
    interactions = []
    while time.monotonic() < deadline and not interactions:
        interactions = broker.list(CLIENT_A)
        time.sleep(0.005)
    assert len(interactions) == 1
    transport.close()

    deadline = time.monotonic() + 1
    while (
        time.monotonic() < deadline
        and broker.get(interactions[0].id, CLIENT_A).state
        is not InteractionState.CANCELLED
    ):
        time.sleep(0.005)
    assert (
        broker.get(interactions[0].id, CLIENT_A).state
        is InteractionState.CANCELLED
    )


def test_stored_password_is_used_once_without_public_secret_metadata(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("SSHPILOT_ASKPASS_LOG_DIR", str(tmp_path))
    from sshpilot import askpass_utils

    askpass_utils._ASKPASS_LOG_PATH = None

    lookups = []
    instance = InteractionBroker(
        secret_timeout=1,
        host_key_timeout=1,
        password_lookup=lambda connection_id: (
            lookups.append(connection_id) or "stored-value"
        ),
    )
    monkeypatch.setattr(instance, "_effective_ssh_config", lambda _argv, _environment=None: {})
    try:
        _argv, environment = instance.prepare_launch(
            SessionLaunchSpec(
                session_id=SESSION_ID,
                connection_id=CONNECTION_ID,
                protocol="ssh",
                hostname="example.test",
                username="alice",
                port=22,
            ),
            lambda _connection_id, **_kwargs: (
                ("/usr/bin/ssh", "example"),
                {"PATH": os.environ.get("PATH", ""), "SSHPILOT_DAEMON_ASKPASS_ACTIVE": "1"},
            ),
        )
        secret = instance._resolve_askpass_secret(
            environment["SSHPILOT_DAEMON_ASKPASS_TOKEN"],
            "alice@example.test's password:",
        )
        assert secret == bytearray(b"stored-value")
        assert lookups == [CONNECTION_ID]
        assert instance.list(CLIENT_A) == []
        secret[:] = b"\0" * len(secret)
        secret.clear()
        log_text = (tmp_path / "sshpilot-askpass.log").read_text(encoding="utf-8")
        assert "ASKPASS: daemon broker ready" in log_text
        assert "ASKPASS: password prompt for alice@example.test" in log_text
        assert "ASKPASS: Returning stored password" in log_text
        assert "stored-value" not in log_text
    finally:
        instance.close()
        askpass_utils._ASKPASS_LOG_PATH = None


def test_stored_passphrase_autofills_unquoted_openssh_prompt(monkeypatch) -> None:
    """OpenSSH often asks without quotes around the key path; still autofill."""
    lookups = []
    instance = InteractionBroker(
        secret_timeout=1,
        host_key_timeout=1,
        passphrase_lookup=lambda key_path: (
            lookups.append(key_path) or "stored-passphrase"
        ),
    )
    monkeypatch.setattr(instance, "_effective_ssh_config", lambda _argv, _environment=None: {})
    try:
        _argv, environment = instance.prepare_launch(
            SessionLaunchSpec(
                session_id=SESSION_ID,
                connection_id=CONNECTION_ID,
                protocol="ssh",
                hostname="example.test",
                username="alice",
                port=22,
            ),
            lambda _connection_id, **_kwargs: (
                ("/usr/bin/ssh", "example"),
                {"PATH": os.environ.get("PATH", ""), "SSHPILOT_DAEMON_ASKPASS_ACTIVE": "1"},
            ),
        )
        secret = instance._resolve_askpass_secret(
            environment["SSHPILOT_DAEMON_ASKPASS_TOKEN"],
            "Enter passphrase for /home/mahdi/.ssh/kwp4: ",
        )
        assert secret == bytearray(b"stored-passphrase")
        assert lookups == ["/home/mahdi/.ssh/kwp4"]
        assert instance.list(CLIENT_A) == []
        secret[:] = b"\0" * len(secret)
        secret.clear()
    finally:
        instance.close()


def test_stored_passphrase_retried_after_first_lookup_miss(monkeypatch) -> None:
    """A first miss must not burn autofill — secrets may not be ready yet.

    Reproduces: daemon marks stored_attempted before lookup succeeds; OpenSSH
    asks again and the user is prompted even though the passphrase is stored.
    """
    lookups = []

    def lookup(key_path: str):
        lookups.append(key_path)
        if len(lookups) == 1:
            return None
        return "stored-passphrase"

    instance = InteractionBroker(
        secret_timeout=1,
        host_key_timeout=1,
        passphrase_lookup=lookup,
    )
    monkeypatch.setattr(instance, "_effective_ssh_config", lambda _argv, _environment=None: {})
    prompt = "Enter passphrase for key '/home/u/.ssh/id_ed25519': "
    prompt_waits = []

    def wait_fail(*_args, **_kwargs):
        prompt_waits.append(True)
        return None

    try:
        _argv, environment = instance.prepare_launch(
            SessionLaunchSpec(
                session_id=SESSION_ID,
                connection_id=CONNECTION_ID,
                protocol="ssh",
                hostname="example.test",
                username="alice",
                port=22,
            ),
            lambda _connection_id, **_kwargs: (
                ("/usr/bin/ssh", "example"),
                {"PATH": os.environ.get("PATH", ""), "SSHPILOT_DAEMON_ASKPASS_ACTIVE": "1"},
            ),
        )
        token = environment["SSHPILOT_DAEMON_ASKPASS_TOKEN"]
        monkeypatch.setattr(instance, "wait_for_result", wait_fail)
        first = instance._resolve_askpass_secret(token, prompt)
        assert first is None
        assert lookups == ["/home/u/.ssh/id_ed25519"]
        assert prompt_waits == [True]
        context = instance._askpass_contexts[token]
        assert "passphrase:/home/u/.ssh/id_ed25519" not in context.stored_attempted
        # Second askpass: must retry stored and autofill (no second prompt).
        second = instance._resolve_askpass_secret(token, prompt)
        assert second == bytearray(b"stored-passphrase")
        assert lookups == [
            "/home/u/.ssh/id_ed25519",
            "/home/u/.ssh/id_ed25519",
        ]
        assert prompt_waits == [True]
        assert "passphrase:/home/u/.ssh/id_ed25519" in context.stored_attempted
        second[:] = b"\0" * len(second)
        second.clear()
    finally:
        instance.close()


def _prepare_autofill_operation(instance: InteractionBroker) -> str:
    _argv, environment = instance.prepare_operation_launch(
        ("ssh", "example.test", "cat .bash_history"),
        {},
        scope_id=SessionId("autocomplete-operation"),
        connection_id=CONNECTION_ID,
        hostname="example.test",
        username="alice",
        interaction_mode=ExecutionInteractionMode.AUTOFILL_ONLY,
    )
    return environment["SSHPILOT_DAEMON_ASKPASS_TOKEN"]


@pytest.mark.parametrize(
    ("prompt", "lookup_kwargs", "expected", "expected_lookup"),
    [
        (
            "alice@example.test's password:",
            {"password_lookup": lambda _connection_id: "stored-password"},
            b"stored-password",
            CONNECTION_ID,
        ),
        (
            "Enter passphrase for key '/home/u/.ssh/id_ed25519': ",
            {"passphrase_lookup": lambda _key_path: "stored-passphrase"},
            b"stored-passphrase",
            "/home/u/.ssh/id_ed25519",
        ),
    ],
)
def test_autofill_only_uses_stored_secret_without_publishing_interaction(
    prompt, lookup_kwargs, expected, expected_lookup
) -> None:
    lookups = []
    if "password_lookup" in lookup_kwargs:
        lookup_kwargs = {
            "password_lookup": lambda connection_id: (
                lookups.append(connection_id) or "stored-password"
            )
        }
    else:
        lookup_kwargs = {
            "passphrase_lookup": lambda key_path: (
                lookups.append(key_path) or "stored-passphrase"
            )
        }
    instance = InteractionBroker(
        secret_timeout=1,
        host_key_timeout=1,
        **lookup_kwargs,
    )
    try:
        secret = instance._resolve_askpass_secret(
            _prepare_autofill_operation(instance), prompt
        )
        assert secret == bytearray(expected)
        assert lookups == [expected_lookup]
        assert instance.list(CLIENT_A) == []
        secret[:] = b"\0" * len(secret)
        secret.clear()
    finally:
        instance.close()


@pytest.mark.parametrize(
    "prompt",
    [
        "alice@example.test's password:",
        "Enter passphrase for key '/home/u/.ssh/id_ed25519': ",
    ],
)
def test_autofill_only_missing_secret_returns_without_publishing(prompt) -> None:
    instance = InteractionBroker(secret_timeout=1, host_key_timeout=1)
    try:
        token = _prepare_autofill_operation(instance)
        instance.wait_for_result = lambda *_args, **_kwargs: pytest.fail(
            "autofill-only must not wait for an interaction"
        )
        assert instance._resolve_askpass_secret(token, prompt) is None
        assert instance.list(CLIENT_A) == []
    finally:
        instance.close()


@pytest.mark.parametrize(
    ("prompt", "hint"),
    [
        ("Enter verification code:", ""),
        ("Custom PAM response:", ""),
        ("Touch your security key", "none"),
        (
            "The authenticity of host 'example.test' can't be established.\n"
            "ED25519 key fingerprint is SHA256:abc\n"
            "Are you sure you want to continue connecting (yes/no)?",
            "",
        ),
    ],
)
def test_autofill_only_declines_all_nonstored_interactions(prompt, hint) -> None:
    instance = InteractionBroker(secret_timeout=1, host_key_timeout=1)
    try:
        token = _prepare_autofill_operation(instance)
        instance.wait_for_result = lambda *_args, **_kwargs: pytest.fail(
            "autofill-only must not wait for an interaction"
        )
        assert instance._resolve_askpass_secret(token, prompt, hint=hint) is None
        assert instance.list(CLIENT_A) == []
    finally:
        instance.close()


def test_interactive_operation_still_publishes_missing_secret_prompt() -> None:
    instance = InteractionBroker(secret_timeout=1, host_key_timeout=1)
    try:
        _argv, environment = instance.prepare_operation_launch(
            ("ssh", "example.test", "true"),
            {},
            scope_id=SessionId("interactive-operation"),
            connection_id=CONNECTION_ID,
            hostname="example.test",
            username="alice",
        )
        instance.wait_for_result = lambda *_args, **_kwargs: None
        assert (
            instance._resolve_askpass_secret(
                environment["SSHPILOT_DAEMON_ASKPASS_TOKEN"],
                "alice@example.test's password:",
            )
            is None
        )
        assert instance.list(CLIENT_A)[0].type is InteractionType.PASSWORD
    finally:
        instance.close()


def test_keygen_passphrase_confirmation_reuses_and_clears_protected_buffer(
    monkeypatch,
) -> None:
    sentinel = bytearray(b"KEY_PASSPHRASE_SENTINEL_8F1C29")
    instance = InteractionBroker(secret_timeout=1, host_key_timeout=1)
    try:
        _argv, environment = instance.prepare_operation_launch(
            ("ssh-keygen", "-t", "ed25519", "-f", "/tmp/key"),
            {},
            scope_id=SessionId("key-operation-generate-1"),
            connection_id=ConnectionId("key-operation-1"),
            hostname="SSH key generation",
            confirm_passphrase=True,
        )
        token = environment["SSHPILOT_DAEMON_ASKPASS_TOKEN"]
        waits = []
        prompts = []

        def wait_for_result(interaction_id, **_kwargs):
            waits.append(True)
            prompts.append(instance._records[interaction_id].summary.prompt)
            return InteractionResult(
                decision=SecretDecision.SUBMIT,
                remember_policy=RememberPolicy.DO_NOT_STORE,
                secret=bytearray(sentinel),
            )

        monkeypatch.setattr(instance, "wait_for_result", wait_for_result)
        first = instance._resolve_askpass_secret(
            token,
            "Enter passphrase (empty for no passphrase):",
        )
        second = instance._resolve_askpass_secret(
            token,
            "Enter same passphrase again:",
        )

        assert first == sentinel
        assert second == sentinel
        assert first is not second
        assert waits == [True]
        assert prompts[0].confirmation_required is True
        assert instance._askpass_contexts[token].confirmation_secret is None
        first[:] = b"\0" * len(first)
        first.clear()
        second[:] = b"\0" * len(second)
        second.clear()
    finally:
        sentinel[:] = b"\0" * len(sentinel)
        sentinel.clear()
        instance.close()


def test_keygen_confirmation_secret_is_wiped_on_cancellation(monkeypatch) -> None:
    instance = InteractionBroker(secret_timeout=1, host_key_timeout=1)
    scope_id = SessionId("key-operation-generate-cancel")
    try:
        _argv, environment = instance.prepare_operation_launch(
            ("ssh-keygen", "-t", "ed25519", "-f", "/tmp/key"),
            {},
            scope_id=scope_id,
            connection_id=ConnectionId("key-operation-cancel"),
            hostname="SSH key generation",
            confirm_passphrase=True,
        )
        token = environment["SSHPILOT_DAEMON_ASKPASS_TOKEN"]
        monkeypatch.setattr(
            instance,
            "wait_for_result",
            lambda *_args, **_kwargs: InteractionResult(
                decision=SecretDecision.SUBMIT,
                remember_policy=RememberPolicy.DO_NOT_STORE,
                secret=bytearray(b"KEY_PASSPHRASE_SENTINEL_8F1C29"),
            ),
        )
        first = instance._resolve_askpass_secret(
            token,
            "Enter passphrase (empty for no passphrase):",
        )
        cached = instance._askpass_contexts[token].confirmation_secret
        assert cached

        instance.cancel_session(scope_id)

        assert cached == bytearray()
        assert token not in instance._askpass_contexts
        first[:] = b"\0" * len(first)
        first.clear()
    finally:
        instance.close()


def test_concurrent_keygen_confirmation_secrets_do_not_cross_scopes(
    monkeypatch,
) -> None:
    instance = InteractionBroker(secret_timeout=1, host_key_timeout=1)
    try:
        tokens = {}
        for suffix in ("a", "b"):
            scope_id = SessionId(f"key-operation-generate-{suffix}")
            _argv, environment = instance.prepare_operation_launch(
                ("ssh-keygen", "-t", "ed25519", "-f", f"/tmp/key-{suffix}"),
                {},
                scope_id=scope_id,
                connection_id=ConnectionId(f"key-operation-{suffix}"),
                hostname="SSH key generation",
                confirm_passphrase=True,
            )
            tokens[suffix] = environment["SSHPILOT_DAEMON_ASKPASS_TOKEN"]

        def wait_for_result(interaction_id, **_kwargs):
            scope_id = instance._records[interaction_id].summary.session_id
            suffix = str(scope_id).rsplit("-", 1)[-1]
            return InteractionResult(
                decision=SecretDecision.SUBMIT,
                remember_policy=RememberPolicy.DO_NOT_STORE,
                secret=bytearray(f"secret-{suffix}".encode()),
            )

        monkeypatch.setattr(instance, "wait_for_result", wait_for_result)
        first = {
            suffix: instance._resolve_askpass_secret(
                token,
                "Enter passphrase (empty for no passphrase):",
            )
            for suffix, token in tokens.items()
        }
        confirmed = {
            suffix: instance._resolve_askpass_secret(
                token,
                "Enter same passphrase again:",
            )
            for suffix, token in tokens.items()
        }

        assert first["a"] == confirmed["a"] == bytearray(b"secret-a")
        assert first["b"] == confirmed["b"] == bytearray(b"secret-b")
        for secret in (*first.values(), *confirmed.values()):
            secret[:] = b"\0" * len(secret)
            secret.clear()
    finally:
        instance.close()


def test_prepare_daemon_terminal_launch_preloads_keys(monkeypatch) -> None:
    """Daemon launch must preload keys like the classic VTE path."""
    from sshpilot.core.connection_application_service import ConnectionApplicationService
    from tests.daemon.conftest import TestConnection, TestConnectionManager

    preloads = []

    class PreloadConnection(TestConnection):
        def __init__(self):
            super().__init__(nickname="preload", hostname="h", username="u")
            self.id = "preload"
            self.uuid = "preload"
            self.data["id"] = "preload"

        async def native_connect(self, **kwargs):
            from types import SimpleNamespace

            self.ssh_connection_cmd = SimpleNamespace(
                command=("ssh", "preload"),
                env={"PATH": "/usr/bin", "HOME": "/tmp", "TERM": "xterm"},
                use_askpass=False,
            )
            return True

        def _preload_keys_into_agent(self, app_config=None):
            preloads.append(True)

    manager = TestConnectionManager()
    connection = PreloadConnection()
    manager.connections = [connection]
    client = ConnectionApplicationService(manager, launch_provider=manager, allow_cross_thread_commands=True)
    cid = ConnectionId("preload")
    monkeypatch.setattr(
        "shutil.which",
        lambda name, path=None: "/usr/bin/ssh" if name == "ssh" else None,
    )
    argv, env = client.prepare_daemon_terminal_launch(
        cid, interaction_policy="broker"
    )
    assert argv[0] == "/usr/bin/ssh"
    assert preloads == [True]


def test_prepare_daemon_terminal_launch_carries_local_command(monkeypatch) -> None:
    """Daemon SSH argv must preserve the host's parsed LocalCommand."""
    from sshpilot.core.connection_application_service import ConnectionApplicationService
    from tests.daemon.conftest import TestConnection, TestConnectionManager

    class LocalCommandConnection(TestConnection):
        def __init__(self):
            super().__init__(nickname="local-command", hostname="h", username="u")
            self.id = "local-command"
            self.uuid = "local-command"
            self.local_command = 'notify-send "connected"'
            self.data.update({
                "id": "local-command",
                "local_command": self.local_command,
            })

        async def native_connect(self, **kwargs):
            from types import SimpleNamespace

            self.ssh_connection_cmd = SimpleNamespace(
                command=("ssh", "local-command"),
                env={"PATH": "/usr/bin", "HOME": "/tmp", "TERM": "xterm"},
                use_askpass=False,
            )
            return True

    manager = TestConnectionManager()
    manager.connections = [LocalCommandConnection()]
    client = ConnectionApplicationService(manager, launch_provider=manager, allow_cross_thread_commands=True)
    monkeypatch.setattr("shutil.which", lambda name, path=None: "/usr/bin/ssh")

    argv, _env = client.prepare_daemon_terminal_launch(
        ConnectionId("local-command"), interaction_policy="broker"
    )

    assert argv == (
        "/usr/bin/ssh",
        "-o",
        "PermitLocalCommand=yes",
        "-o",
        'LocalCommand=notify-send "connected"',
        "local-command",
    )


def test_prepare_daemon_terminal_launch_dispatches_non_ssh_provider(monkeypatch) -> None:
    """Telnet/Mosh/custom protocols use their registered daemon provider."""
    from sshpilot.core.connection_application_service import ConnectionApplicationService
    from sshpilot.core.plugins import SpawnSpec
    from sshpilot.plugins.registry import protocol_registry
    from tests.daemon.conftest import TestConnection, TestConnectionManager

    class Provider:
        protocol_id = "test-wire"
        display_name = "Test wire"

        def build_spawn(self, connection, ctx):
            assert connection.protocol == "test-wire"
            assert ctx.plugin_id == "test-plugin"
            return SpawnSpec(argv=["wire-client", connection.hostname], env={"PATH": "/bin"})

    manager = TestConnectionManager()
    manager.config = None
    connection = TestConnection(nickname="wire", hostname="wire.test", username="u")
    connection.id = connection.uuid = "wire"
    connection.protocol = "test-wire"
    connection.data.update({"id": "wire", "protocol": "test-wire"})
    manager.connections = [connection]
    registry = protocol_registry()
    registry.register(Provider(), plugin_id="test-plugin")
    monkeypatch.setattr("shutil.which", lambda name, path=None: "/bin/wire-client")
    try:
        service = ConnectionApplicationService(manager, launch_provider=manager, allow_cross_thread_commands=True)
        argv, env = service.prepare_daemon_terminal_launch(ConnectionId("wire"))
    finally:
        registry.unregister_plugin("test-plugin")

    assert argv == ("/bin/wire-client", "wire.test")
    assert env == {"PATH": "/bin"}


def test_daemon_entered_password_is_never_implicitly_stored(
    monkeypatch,
) -> None:
    stored = []
    instance = InteractionBroker(
        secret_timeout=1,
        host_key_timeout=1,
        password_store=lambda connection_id, value: (
            stored.append((connection_id, value)) or True
        ),
    )
    monkeypatch.setattr(instance, "_effective_ssh_config", lambda _argv, _environment=None: {})
    try:
        _argv, environment = instance.prepare_launch(
            SessionLaunchSpec(
                session_id=SESSION_ID,
                connection_id=CONNECTION_ID,
                protocol="ssh",
                hostname="example.test",
                username="alice",
                port=22,
            ),
            lambda _connection_id, **_kwargs: (
                ("/usr/bin/ssh", "example"),
                {"PATH": os.environ.get("PATH", ""), "SSHPILOT_DAEMON_ASKPASS_ACTIVE": "1"},
            ),
        )
        token = environment["SSHPILOT_DAEMON_ASKPASS_TOKEN"]
        resolved = []

        def resolve() -> None:
            resolved.append(
                instance._resolve_askpass_secret(
                    token,
                    "alice@example.test's password:",
                )
            )

        waiter = threading.Thread(target=resolve)
        waiter.start()
        deadline = time.monotonic() + 1
        interactions = []
        while time.monotonic() < deadline and not interactions:
            interactions = instance.list(CLIENT_A)
            time.sleep(0.005)
        interaction = interactions[0]
        claim = instance.claim(interaction.id, CLIENT_A)
        instance.respond(
            InteractionDecisionRequest(
                interaction_id=interaction.id,
                secret_decision=SecretDecision.SUBMIT,
                remember_policy=RememberPolicy.STORE_AFTER_SUCCESS,
            ),
            CLIENT_A,
        )
        instance.submit_secret(
            SecretFrame(
                kind=SecretFrameKind.RESPONSE,
                interaction_id=interaction.id,
                nonce=bytes.fromhex(claim.nonce),
                secret=bytearray(b"new-value"),
            ),
            CLIENT_A,
        )
        waiter.join(1)
        assert not waiter.is_alive()
        assert stored == []
        assert stored == []
        assert resolved == [bytearray(b"new-value")]
        resolved[0][:] = b"\0" * len(resolved[0])
        resolved[0].clear()
    finally:
        instance.close()


def test_prepare_launch_preserves_canonical_ssh_options(
    broker: InteractionBroker,
    monkeypatch,
) -> None:
    monkeypatch.setattr(broker, "_effective_ssh_config", lambda _argv, _environment=None: {})
    argv, _environment = broker.prepare_launch(
        SessionLaunchSpec(
            session_id=SESSION_ID,
            connection_id=CONNECTION_ID,
            protocol="ssh",
            hostname="example.test",
            username="alice",
            port=22,
        ),
        lambda _connection_id, **_kwargs: (
            (
                "/usr/bin/ssh",
                "-F",
                "/tmp/config",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                "UserKnownHostsFile=/tmp/user-known-hosts",
                "-o",
                "ConnectTimeout=5",
                "example",
            ),
            {"PATH": os.environ.get("PATH", ""), "SSHPILOT_DAEMON_ASKPASS_ACTIVE": "1"},
        ),
    )
    assert argv[:3] == (
        "/usr/bin/ssh",
        "-F",
        "/tmp/config",
    )
    assert argv == (
        "/usr/bin/ssh", "-F", "/tmp/config",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "UserKnownHostsFile=/tmp/user-known-hosts",
        "-o", "ConnectTimeout=5",
        "example",
    )


def test_prepare_launch_does_not_add_ssh_option_defaults(
    broker: InteractionBroker,
    monkeypatch,
) -> None:
    monkeypatch.setattr(broker, "_effective_ssh_config", lambda _argv, _environment=None: {})

    argv, _environment = broker.prepare_launch(
        SessionLaunchSpec(
            session_id=SESSION_ID,
            connection_id=CONNECTION_ID,
            protocol="ssh",
            hostname="example.test",
            username="alice",
            port=22,
        ),
        lambda _connection_id, **_kwargs: (
            ("/usr/bin/ssh", "example"),
            {"PATH": os.environ.get("PATH", ""), "SSHPILOT_DAEMON_ASKPASS_ACTIVE": "1"},
        ),
    )

    assert argv == ("/usr/bin/ssh", "example")
    assert not any("UserKnownHostsFile" in value for value in argv)


def test_strict_host_key_mode_selects_first_occurrence() -> None:
    """Match OpenSSH: first obtained StrictHostKeyChecking wins."""
    argv = (
        "/usr/bin/ssh",
        "-o",
        "StrictHostKeyChecking=ask",
        "-o",
        "StrictHostKeyChecking=yes",
        "-oStrictHostKeyChecking=no",
        "example",
    )
    assert InteractionBroker._strict_host_key_mode(argv, {}) == "ask"
    assert InteractionBroker._strict_host_key_mode(
        (
            "/usr/bin/ssh",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "StrictHostKeyChecking=ask",
            "example",
        ),
        {},
    ) == "accept-new"
    # Glued -o form as first hit.
    assert InteractionBroker._strict_host_key_mode(
        (
            "/usr/bin/ssh",
            "-oStrictHostKeyChecking=yes",
            "-o",
            "StrictHostKeyChecking=ask",
            "example",
        ),
        {},
    ) == "yes"


def test_prepare_launch_preserves_authored_user_known_hosts_file(
    broker: InteractionBroker,
    monkeypatch,
) -> None:
    monkeypatch.setattr(broker, "_effective_ssh_config", lambda _argv, _environment=None: {})
    argv, _environment = broker.prepare_launch(
        SessionLaunchSpec(
            session_id=SESSION_ID,
            connection_id=CONNECTION_ID,
            protocol="ssh",
            hostname="example.test",
            username="alice",
            port=22,
        ),
        lambda _connection_id, **_kwargs: (
            (
                "/usr/bin/ssh",
                "-o",
                "StrictHostKeyChecking=ask",
                "-o",
                "UserKnownHostsFile=/tmp/user-known-hosts",
                "example",
            ),
            {"PATH": os.environ.get("PATH", ""), "SSHPILOT_DAEMON_ASKPASS_ACTIVE": "1"},
        ),
    )
    assert "StrictHostKeyChecking=ask" in argv
    known_hosts_options = [
        value for value in argv if value.startswith("UserKnownHostsFile=")
    ]
    assert known_hosts_options == ["UserKnownHostsFile=/tmp/user-known-hosts"]


@pytest.mark.parametrize(
    ("known_hosts", "trailing_args"),
    (
        ("none", ()),
        ("/tmp/first /tmp/second", ("sftp",)),
    ),
)
def test_prepare_launch_leaves_known_hosts_policy_to_openssh(
    broker: InteractionBroker,
    monkeypatch,
    known_hosts: str,
    trailing_args: tuple[str, ...],
) -> None:
    monkeypatch.setattr(broker, "_effective_ssh_config", lambda *_args: {})
    authored = f"UserKnownHostsFile={known_hosts}"
    argv, _environment = broker.prepare_launch(
        SessionLaunchSpec(
            session_id=SESSION_ID,
            connection_id=CONNECTION_ID,
            protocol="ssh",
            hostname="example.test",
            username="alice",
            port=22,
        ),
        lambda _connection_id, **_kwargs: (
            ("/usr/bin/ssh", "-o", authored, "example"),
            {"PATH": os.environ.get("PATH", "")},
        ),
        trailing_args=trailing_args,
    )
    assert argv == ("/usr/bin/ssh", "-o", authored, "example", *trailing_args)
    assert tuple(value for value in argv if "UserKnownHostsFile" in value) == (
        authored,
    )


def test_parse_openssh_host_key_askpass_prompt(broker: InteractionBroker) -> None:
    prompt = broker._parse_host_key_askpass_prompt(
        (
            "The authenticity of host '127.0.0.1 (127.0.0.1)' can't be established.\n"
            "ED25519 key fingerprint is: "
            "SHA256:57moBNKAME3b6kuvL2DktyyfYIkZDCDA3nhyrdfTD9w\n"
            "This key is not known by any other names.\n"
            "Are you sure you want to continue connecting (yes/no/[fingerprint])? "
        ),
        hostname="fallback.host",
        port=22,
    )
    assert prompt is not None
    assert prompt.hostname == "127.0.0.1"
    assert prompt.key_type == "ssh-ed25519"
    assert prompt.fingerprint.startswith("SHA256:")
    assert prompt.status is HostKeyStatus.UNKNOWN


def test_prepare_launch_does_not_invoke_keyscan(
    broker: InteractionBroker,
    monkeypatch,
) -> None:
    monkeypatch.setattr(broker, "_effective_ssh_config", lambda _argv, _environment=None: {})

    def _forbid_remote(*_args, **_kwargs):
        cmd = _args[0] if _args else ()
        if cmd and "ssh-keyscan" in str(cmd[0]):
            raise AssertionError("ssh-keyscan must not run during prepare_launch")
        # Allow ssh -G effective-config probes if any remain.
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr(
        "sshpilot.daemon.interaction_broker.subprocess.run",
        _forbid_remote,
    )
    monkeypatch.setattr(
        "sshpilot.daemon.interaction_broker.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    argv, environment = broker.prepare_launch(
        SessionLaunchSpec(
            session_id=SESSION_ID,
            connection_id=CONNECTION_ID,
            protocol="ssh",
            hostname="example.test",
            username="alice",
            port=22,
        ),
        lambda _connection_id, **_kwargs: (
            ("/usr/bin/ssh", "-F", "/tmp/config", "example"),
            {"PATH": os.environ.get("PATH", ""), "SSHPILOT_DAEMON_ASKPASS_ACTIVE": "1"},
        ),
    )
    assert environment["SSH_ASKPASS_REQUIRE"] == "prefer"
    assert "StrictHostKeyChecking=ask" not in argv
    assert not any(
        item.startswith("HostKeyAlgorithms=")
        or item.startswith("GlobalKnownHostsFile=")
        for item in argv
    )


def test_two_broadcast_targets_receive_independent_operation_askpass_contexts(broker):
    scope = SessionId("operation-broadcast")
    argv_a, env_a = broker.prepare_operation_launch(
        ("ssh", "host-a", "sensitive command"),
        {},
        scope_id=scope,
        connection_id=ConnectionId("host-a"),
        hostname="host-a",
    )
    argv_b, env_b = broker.prepare_operation_launch(
        ("ssh", "host-b", "sensitive command"),
        {},
        scope_id=scope,
        connection_id=ConnectionId("host-b"),
        hostname="host-b",
    )
    assert argv_a[-1] == argv_b[-1] == "sensitive command"
    assert env_a["SSHPILOT_DAEMON_ASKPASS_TOKEN"] != env_b[
        "SSHPILOT_DAEMON_ASKPASS_TOKEN"
    ]
    contexts = tuple(broker._askpass_contexts.values())
    assert {context.connection_id for context in contexts} == {
        ConnectionId("host-a"),
        ConnectionId("host-b"),
    }
    assert all(context.session_id == scope for context in contexts)
    assert all(context.hostname != "sensitive command" for context in contexts)
    broker.cancel_session(scope)
    assert not broker._askpass_contexts

import json
import os
import socket
import struct
import subprocess
import threading
import time
from types import SimpleNamespace
from typing import Iterator

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models import (
    ClientId,
    ConnectionId,
    HostKeyDecision,
    HostKeyPrompt,
    HostKeyStatus,
    InteractionDecisionRequest,
    InteractionState,
    InteractionType,
    PasswordPrompt,
    RememberPolicy,
    SecretDecision,
    SessionId,
)
from sshpilot.api.transport.secret_frames import SecretFrame, SecretFrameKind
from sshpilot.daemon.interaction_broker import InteractionBroker
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
            host_key_decision=HostKeyDecision.ACCEPT_ONCE,
        ),
        CLIENT_A,
    )
    result = broker.wait_for_result(summary.id)
    assert result is not None
    assert result.decision is HostKeyDecision.ACCEPT_ONCE
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


def test_session_cancel_and_close_leave_no_active_interactions(
    broker: InteractionBroker,
) -> None:
    summary = _password(broker)
    broker.cancel_session(SESSION_ID)
    assert broker.get(summary.id, CLIENT_A).state is InteractionState.CANCELLED
    assert tuple(broker.active_ids()) == ()


def test_private_askpass_helper_delivers_only_one_brokered_secret(
    broker: InteractionBroker,
    monkeypatch,
) -> None:
    monkeypatch.setattr(broker, "_effective_ssh_config", lambda _argv: {})
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


def test_askpass_helper_disconnect_cancels_pending_interaction(
    broker: InteractionBroker,
    monkeypatch,
) -> None:
    monkeypatch.setattr(broker, "_effective_ssh_config", lambda _argv: {})
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
    monkeypatch.setattr(instance, "_effective_ssh_config", lambda _argv: {})
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
                {"PATH": os.environ.get("PATH", "")},
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
    monkeypatch.setattr(instance, "_effective_ssh_config", lambda _argv: {})
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
                {"PATH": os.environ.get("PATH", "")},
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
    monkeypatch.setattr(instance, "_effective_ssh_config", lambda _argv: {})
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
                {"PATH": os.environ.get("PATH", "")},
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


def test_prepare_daemon_terminal_launch_preloads_keys(monkeypatch) -> None:
    """Daemon launch must preload keys like the classic VTE path."""
    from sshpilot.api.in_process_client import InProcessClient
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
    client = InProcessClient(manager, allow_cross_thread_commands=True)
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
    from sshpilot.api.in_process_client import InProcessClient
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
    client = InProcessClient(manager, allow_cross_thread_commands=True)
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


def test_remembered_password_is_stored_only_after_authenticated_status(
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
    monkeypatch.setattr(instance, "_effective_ssh_config", lambda _argv: {})
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
                {"PATH": os.environ.get("PATH", "")},
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
        instance._store_authenticated_secrets(token)
        assert stored == [(CONNECTION_ID, "new-value")]
        assert resolved == [bytearray(b"new-value")]
        resolved[0][:] = b"\0" * len(resolved[0])
        resolved[0].clear()
    finally:
        instance.close()


def test_persistent_host_key_write_is_atomic_private_and_hashable(
    broker: InteractionBroker,
    tmp_path,
) -> None:
    path = tmp_path / "known_hosts"
    broker._persist_host_key(
        path,
        "[example.test]:2222",
        ("ssh-ed25519", "QUJDRA=="),
        hash_host=True,
    )

    content = path.read_text()
    assert content.startswith("|1|")
    assert "example.test" not in content
    assert content.endswith(" ssh-ed25519 QUJDRA==\n")
    assert path.stat().st_mode & 0o777 == 0o600


def test_persistent_host_key_rejects_symlink(
    broker: InteractionBroker,
    tmp_path,
) -> None:
    target = tmp_path / "target"
    target.write_text("")
    path = tmp_path / "known_hosts"
    path.symlink_to(target)

    with pytest.raises(SshPilotError) as caught:
        broker._persist_host_key(
            path,
            "example.test",
            ("ssh-ed25519", "QUJDRA=="),
            hash_host=False,
        )

    assert caught.value.code is ErrorCode.HOST_KEY_PERSISTENCE_FAILED
    assert target.read_text() == ""


def test_broker_options_win_over_conflicting_preference_overrides(
    broker: InteractionBroker,
    monkeypatch,
) -> None:
    monkeypatch.setattr(broker, "_effective_ssh_config", lambda _argv: {})
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
            {"PATH": os.environ.get("PATH", "")},
        ),
    )
    assert argv[:5] == (
        "/usr/bin/ssh",
        "-F",
        "/tmp/config",
        "-o",
        "BatchMode=no",
    )
    text = " ".join(argv)
    assert text.index("BatchMode=no") < text.index("ConnectTimeout=5")
    assert "BatchMode=yes" not in argv
    # Preferences / argv StrictHostKeyChecking must survive (default accept-new).
    assert "StrictHostKeyChecking=accept-new" in argv
    assert "StrictHostKeyChecking=ask" not in argv
    # UserKnownHostsFile from argv is left alone unless mode is ask.
    assert "UserKnownHostsFile=/tmp/user-known-hosts" in argv
    assert argv[-1] == "example"


def test_prepare_launch_does_not_force_connection_multiplexing(
    broker: InteractionBroker,
    monkeypatch,
) -> None:
    monkeypatch.setattr(broker, "_effective_ssh_config", lambda _argv: {})

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
            {"PATH": os.environ.get("PATH", "")},
        ),
    )

    assert not any("ControlMaster=" in item for item in argv)
    assert not any("ControlPersist=" in item for item in argv)
    assert not any("ControlPath=" in item for item in argv)


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


def test_prepare_launch_ask_uses_session_known_hosts(
    broker: InteractionBroker,
    monkeypatch,
) -> None:
    monkeypatch.setattr(broker, "_effective_ssh_config", lambda _argv: {})
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
            {"PATH": os.environ.get("PATH", "")},
        ),
    )
    assert "StrictHostKeyChecking=ask" in argv
    known_opt = next(
        item for item in argv if item.startswith("UserKnownHostsFile=")
    )
    assert known_opt.startswith("UserKnownHostsFile=")
    assert "/tmp/user-known-hosts" not in known_opt
    assert "known_hosts" in known_opt or "/h-" in known_opt or "h-" in known_opt


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
    monkeypatch.setattr(broker, "_effective_ssh_config", lambda _argv: {})

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
            {"PATH": os.environ.get("PATH", "")},
        ),
    )
    assert environment["SSH_ASKPASS_REQUIRE"] == "force"
    assert "StrictHostKeyChecking=ask" not in argv
    assert not any(
        item.startswith("HostKeyAlgorithms=")
        or item.startswith("GlobalKnownHostsFile=")
        for item in argv
    )

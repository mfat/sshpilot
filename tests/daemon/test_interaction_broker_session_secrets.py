"""RememberPolicy.REMEMBER_FOR_SESSION: daemon-memory credentials.

Where connection sharing is unavailable (OpenSSH for Windows has no
ControlMaster) every tab, SFTP channel and Host Info sample logs in again;
a password the user chose to remember for the session answers those
prompts without a backend ever seeing it.
"""

from __future__ import annotations

import os
import threading
import time

from sshpilot.api.models import (
    ClientId,
    ConnectionId,
    InteractionDecisionRequest,
    RememberPolicy,
    SecretDecision,
    SessionId,
)
from sshpilot.api.transport.secret_frames import SecretFrame, SecretFrameKind
from sshpilot.daemon.interaction_broker import InteractionBroker
from sshpilot.daemon.session_runtime import SessionLaunchSpec

CONNECTION_ID = ConnectionId("conn-session-secret")
CLIENT = ClientId("client-a")
PROMPT = "alice@example.test's password:"


def _broker(monkeypatch, stored):
    broker = InteractionBroker(
        secret_timeout=2,
        host_key_timeout=2,
        password_store=lambda connection_id, value: stored.append((connection_id, value)) or True,
        password_lookup=lambda _connection_id: None,
    )
    monkeypatch.setattr(broker, "_effective_ssh_config", lambda _argv, _environment=None: {})
    return broker


def _launch(broker, session: str) -> str:
    _argv, environment = broker.prepare_launch(
        SessionLaunchSpec(
            session_id=SessionId(session),
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
    return environment["SSHPILOT_DAEMON_ASKPASS_TOKEN"]


def _resolve_in_thread(broker, token):
    result = []
    thread = threading.Thread(target=lambda: result.append(broker._resolve_askpass_secret(token, PROMPT)))
    thread.start()
    return thread, result


def _answer(broker, secret: bytes, policy: RememberPolicy) -> None:
    deadline = time.monotonic() + 2
    pending = []
    while time.monotonic() < deadline and not pending:
        pending = [item for item in broker.list(CLIENT) if item.state.value == "pending"]
        time.sleep(0.005)
    assert pending, "no prompt was published"
    interaction = pending[0]
    claim = broker.claim(interaction.id, CLIENT)
    broker.respond(
        InteractionDecisionRequest(
            interaction_id=interaction.id,
            secret_decision=SecretDecision.SUBMIT,
            remember_policy=policy,
        ),
        CLIENT,
    )
    broker.submit_secret(
        SecretFrame(
            kind=SecretFrameKind.RESPONSE,
            interaction_id=interaction.id,
            nonce=bytes.fromhex(claim.nonce),
            secret=bytearray(secret),
        ),
        CLIENT,
    )


def _pending_count(broker) -> int:
    return sum(1 for item in broker.list(CLIENT) if item.state.value == "pending")


def test_remembered_for_session_autofills_the_next_login(monkeypatch):
    stored = []
    broker = _broker(monkeypatch, stored)
    try:
        token = _launch(broker, "s1")
        thread, result = _resolve_in_thread(broker, token)
        _answer(broker, b"hunter2", RememberPolicy.REMEMBER_FOR_SESSION)
        thread.join(2)
        assert bytes(result[0]) == b"hunter2"
        broker.mark_authenticated(SessionId("s1"))

        # A second channel to the same connection is answered silently.
        second = _launch(broker, "s2")
        answer = broker._resolve_askpass_secret(second, PROMPT)
        assert answer is not None and bytes(answer) == b"hunter2"
        assert _pending_count(broker) == 0
        # Never written to a secret backend.
        assert stored == []
    finally:
        broker.close()


def test_not_kept_before_the_login_succeeds(monkeypatch):
    broker = _broker(monkeypatch, [])
    try:
        token = _launch(broker, "s1")
        thread, _result = _resolve_in_thread(broker, token)
        _answer(broker, b"wrong", RememberPolicy.REMEMBER_FOR_SESSION)
        thread.join(2)
        broker.cancel_session(SessionId("s1"))  # never authenticated

        second = _launch(broker, "s2")
        thread, _result = _resolve_in_thread(broker, second)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not _pending_count(broker):
            time.sleep(0.005)
        assert _pending_count(broker) == 1  # asked again
        broker.cancel_session(SessionId("s2"))
        thread.join(2)
    finally:
        broker.close()


def test_a_repeated_prompt_drops_the_remembered_password(monkeypatch):
    broker = _broker(monkeypatch, [])
    try:
        token = _launch(broker, "s1")
        thread, _result = _resolve_in_thread(broker, token)
        _answer(broker, b"old-password", RememberPolicy.REMEMBER_FOR_SESSION)
        thread.join(2)
        broker.mark_authenticated(SessionId("s1"))

        second = _launch(broker, "s2")
        assert bytes(broker._resolve_askpass_secret(second, PROMPT)) == b"old-password"
        # OpenSSH asks again on the same channel: the remembered one failed.
        thread, result = _resolve_in_thread(broker, second)
        _answer(broker, b"new-password", RememberPolicy.DO_NOT_STORE)
        thread.join(2)
        assert bytes(result[0]) == b"new-password"
        assert broker._session_secrets == {}
    finally:
        broker.close()


def test_close_wipes_session_secrets(monkeypatch):
    broker = _broker(monkeypatch, [])
    token = _launch(broker, "s1")
    thread, _result = _resolve_in_thread(broker, token)
    _answer(broker, b"hunter2", RememberPolicy.REMEMBER_FOR_SESSION)
    thread.join(2)
    broker.mark_authenticated(SessionId("s1"))
    (secret,) = broker._session_secrets.values()
    broker.close()
    assert broker._session_secrets == {}
    assert secret == bytearray()  or set(secret) == {0}

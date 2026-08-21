"""Containerized end-to-end OpenSSH password authentication for Phase 9."""

from __future__ import annotations

import logging
import os
import threading
import time

import pytest

from sshpilot.core.connection_evidence import classify_connection_evidence

from sshpilot.api.events import EventType
from sshpilot.api.models import (
    ClientId,
    ConnectionId,
    InteractionDecisionRequest,
    InteractionType,
    RememberPolicy,
    SecretDecision,
    SessionId,
)
from sshpilot.api.transport.secret_frames import SecretFrame, SecretFrameKind
from sshpilot.daemon.interaction_broker import InteractionBroker
from sshpilot.daemon.pty_runner import PtySessionProcessRunner
from sshpilot.daemon.session_runtime import SessionLaunchSpec

from tests.daemon.password_sshd import container_runtime, start_password_sshd
from tests.daemon.integration_environment import skip_or_fail


pytestmark = pytest.mark.integration


def _require_container_password_env(tmp_path):
    if container_runtime() is None:
        skip_or_fail("Containerized password authentication requires podman or docker")
    try:
        return start_password_sshd(tmp_path)
    except RuntimeError as error:
        skip_or_fail(f"Isolated password sshd unavailable: {error}")


def _wait_until(predicate, timeout: float = 12.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


def test_password_auth_wrong_then_correct_with_isolated_sshd(
    tmp_path,
    caplog,
) -> None:
    """Exercise real password auth against a disposable container sshd."""

    env = _require_container_password_env(tmp_path)
    stored_passwords = []
    looked_up = []
    broker = InteractionBroker(
        password_lookup=lambda connection_id: (
            looked_up.append(connection_id) or None
        ),
        password_store=lambda connection_id, value: (
            stored_passwords.append((connection_id, value)) or True
        ),
    )
    runner = None
    session_id = SessionId("session-90")
    connection_id = ConnectionId(
        "conn-91"
    )
    client_id = ClientId("client:phase9-password")
    password_prompts = []
    attempts = {"count": 0}
    output = bytearray()
    exited = threading.Event()

    def answer(event) -> None:
        if event.type is not EventType.INTERACTION_CREATED:
            return
        summary = event.payload
        if summary.type is not InteractionType.PASSWORD:
            return
        password_prompts.append(summary)
        claim = broker.claim(summary.id, client_id)
        attempts["count"] += 1
        secret = (
            env.password
            if attempts["count"] > 1
            else f"wrong-{env.password}"
        )
        broker.respond(
            InteractionDecisionRequest(
                interaction_id=summary.id,
                secret_decision=SecretDecision.SUBMIT,
                remember_policy=RememberPolicy.STORE_AFTER_SUCCESS,
            ),
            client_id,
        )
        broker.submit_secret(
            SecretFrame(
                kind=SecretFrameKind.RESPONSE,
                interaction_id=summary.id,
                nonce=bytes.fromhex(claim.nonce),
                secret=bytearray(secret.encode()),
            ),
            client_id,
        )

    broker.subscribe_events(answer)
    try:
        with caplog.at_level(logging.DEBUG):
            def collect_output(data):
                output.extend(data)
                if classify_connection_evidence(
                    bytes(output).decode("utf-8", errors="replace")
                ).verdict == "connected":
                    broker.mark_authenticated(session_id)

            runner = PtySessionProcessRunner(
                lambda spec: broker.prepare_launch(
                    spec,
                    lambda _connection_id, **_kwargs: (
                        (
                            "ssh",
                            "-F",
                            str(env.ssh_config),
                            env.host_alias,
                        ),
                        {
                            "HOME": str(env.client_home),
                            "LANG": "C.UTF-8",
                            "LOGNAME": env.username,
                            "PATH": os.environ.get("PATH", ""),
                            "TERM": "xterm-256color",
                            "USER": env.username,
                        },
                    ),
                )
            )
            handle = runner.start(
                SessionLaunchSpec(
                    session_id=session_id,
                    connection_id=connection_id,
                    protocol="ssh",
                    hostname="127.0.0.1",
                    username=env.username,
                    port=env.port,
                ),
                lambda _info: exited.set(),
                collect_output,
                lambda: None,
            )

            assert _wait_until(lambda: len(password_prompts) >= 1, timeout=15)
            # Wrong password creates a bounded retry prompt before success.
            assert _wait_until(lambda: len(password_prompts) >= 2, timeout=15)
            assert _wait_until(
                lambda: b"PHASE9_PASSWORD_SHELL" in output
                or b"$" in output
                or b"#" in output,
                timeout=20,
            )
            # Keep the ControlMaster alive until remember-after-success commits.
            handle.write(b"printf 'PHASE9_PASSWORD_OK\\n'\n")
            assert _wait_until(
                lambda: b"PHASE9_PASSWORD_OK" in bytes(output),
                timeout=20,
            )
            assert env.password.encode() not in bytes(output)
            assert f"wrong-{env.password}".encode() not in bytes(output)
            assert all(
                env.password not in record.message
                and f"wrong-{env.password}" not in record.message
                for record in caplog.records
            )
            assert _wait_until(lambda: len(stored_passwords) == 1, timeout=15)
            assert stored_passwords == [(connection_id, env.password)]
            handle.write(b"exit\n")
            assert exited.wait(20)
            assert attempts["count"] >= 2
            assert all(
                prompt.type is InteractionType.PASSWORD
                for prompt in password_prompts
            )
    finally:
        if runner is not None:
            runner.close()
        broker.close()
        env.destroy()


def test_password_auth_reconnect_uses_stored_secret_only_after_success(
    tmp_path,
) -> None:
    env = _require_container_password_env(tmp_path)
    vault = {}
    broker = InteractionBroker(
        password_lookup=lambda connection_id: vault.get(connection_id),
        password_store=lambda connection_id, value: (
            vault.__setitem__(connection_id, value) or True
        ),
    )
    runner = None
    connection_id = ConnectionId(
        "conn-92"
    )
    client_id = ClientId("client:phase9-password-store")

    def _run_once(session_suffix: str, expect_prompt: bool) -> bytes:
        nonlocal runner
        session_id = SessionId(
            f"session-{session_suffix}"
        )
        output = bytearray()
        exited = threading.Event()
        prompted = []

        def answer(event) -> None:
            if event.type is not EventType.INTERACTION_CREATED:
                return
            summary = event.payload
            if summary.type is not InteractionType.PASSWORD:
                return
            prompted.append(summary)
            claim = broker.claim(summary.id, client_id)
            broker.respond(
                InteractionDecisionRequest(
                    interaction_id=summary.id,
                    secret_decision=SecretDecision.SUBMIT,
                    remember_policy=RememberPolicy.STORE_AFTER_SUCCESS,
                ),
                client_id,
            )
            broker.submit_secret(
                SecretFrame(
                    kind=SecretFrameKind.RESPONSE,
                    interaction_id=summary.id,
                    nonce=bytes.fromhex(claim.nonce),
                    secret=bytearray(env.password.encode()),
                ),
                client_id,
            )

        subscription = broker.subscribe_events(answer)
        try:
            runner = PtySessionProcessRunner(
                lambda spec: broker.prepare_launch(
                    spec,
                    lambda _connection_id, **_kwargs: (
                        (
                            "ssh",
                            "-F",
                            str(env.ssh_config),
                            env.host_alias,
                        ),
                        {
                            "HOME": str(env.client_home),
                            "LANG": "C.UTF-8",
                            "LOGNAME": env.username,
                            "PATH": os.environ.get("PATH", ""),
                            "TERM": "xterm-256color",
                            "USER": env.username,
                        },
                    ),
                )
            )
            def collect_output(data):
                output.extend(data)
                if classify_connection_evidence(
                    bytes(output).decode("utf-8", errors="replace")
                ).verdict == "connected":
                    broker.mark_authenticated(session_id)

            handle = runner.start(
                SessionLaunchSpec(
                    session_id=session_id,
                    connection_id=connection_id,
                    protocol="ssh",
                    hostname="127.0.0.1",
                    username=env.username,
                    port=env.port,
                ),
                lambda _info: exited.set(),
                collect_output,
                lambda: None,
            )
            if expect_prompt:
                assert _wait_until(lambda: prompted, timeout=15)
            else:
                # Stored password should satisfy askpass without a new dialog.
                time.sleep(1.0)
            handle.write(b"printf 'PHASE9_RECONNECT_OK\\n'\n")
            assert _wait_until(
                lambda: b"PHASE9_RECONNECT_OK" in bytes(output),
                timeout=20,
            )
            assert env.password.encode() not in bytes(output)
            if expect_prompt:
                assert prompted
                assert _wait_until(
                    lambda: connection_id in vault,
                    timeout=15,
                )
            else:
                assert not prompted
            handle.write(b"exit\n")
            assert exited.wait(20)
            return bytes(output)
        finally:
            subscription.close()
            if runner is not None:
                runner.close()
                runner = None

    try:
        assert connection_id not in vault
        _run_once("93", expect_prompt=True)
        assert _wait_until(lambda: connection_id in vault, timeout=10)
        assert vault[connection_id] == env.password
        _run_once("94", expect_prompt=False)
    finally:
        if runner is not None:
            runner.close()
        broker.close()
        env.destroy()

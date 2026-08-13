"""Terminal handling for daemon-owned protected interactions."""

from __future__ import annotations

import getpass
from typing import Callable, Set

from sshpilot.api.models.interactions import (
    ChallengePrompt,
    ConfirmationPrompt,
    HostKeyDecision,
    HostKeyPrompt,
    InteractionDecisionRequest,
    InteractionState,
    InteractionSummary,
    PassphrasePrompt,
    PasswordPrompt,
    PresencePrompt,
    RememberPolicy,
    SecretDecision,
)


def handle_interactions(
    client,
    operation_id,
    *,
    write,
    input_fn: Callable[[str], str] = input,
    getpass_fn: Callable[[str], str] = getpass.getpass,
    handled: Set[str] | None = None,
) -> Set[str]:
    """Claim and answer pending interactions belonging to one operation.

    Secret values are converted directly into a bytearray and sent through the
    authenticated secret-frame method. They are never included in a normal DTO.
    """

    handled = handled if handled is not None else set()
    for summary in client.list_interactions():
        if str(summary.session_id) != str(operation_id):
            continue
        if summary.id in handled or summary.state is not InteractionState.PENDING:
            continue
        _handle_one(
            client,
            summary,
            write=write,
            input_fn=input_fn,
            getpass_fn=getpass_fn,
        )
        handled.add(summary.id)
    return handled


def _handle_one(client, summary: InteractionSummary, *, write, input_fn, getpass_fn) -> None:
    prompt = summary.prompt
    if isinstance(prompt, PresencePrompt):
        # Security-key presence is passive: ssh/ssh-keygen waits for the user
        # to touch the key.  The broker deliberately rejects submit frames for
        # this interaction type, so leave it pending and only display it once.
        write(f"{prompt.text}\n")
        return

    claim = client.claim_interaction(summary.id)
    if isinstance(prompt, HostKeyPrompt):
        write(
            f"Accept host key {prompt.hostname}:{prompt.port} "
            f"({prompt.fingerprint})? [y/N] "
        )
        answer = input_fn("")
        decision = (
            HostKeyDecision.ACCEPT if answer.strip().lower() in {"y", "yes"}
            else HostKeyDecision.REJECT
        )
        client.respond_to_interaction(
            InteractionDecisionRequest(summary.id, host_key_decision=decision)
        )
        return

    if isinstance(prompt, ConfirmationPrompt):
        write(f"{prompt.text} [y/N] ")
        answer = input_fn("")
        decision = (
            SecretDecision.SUBMIT
            if answer.strip().lower() in {"y", "yes"}
            else SecretDecision.CANCEL
        )
        client.respond_to_interaction(
            InteractionDecisionRequest(summary.id, secret_decision=decision)
        )
        if decision is SecretDecision.SUBMIT:
            secret = bytearray(b"yes")
            try:
                client.send_interaction_secret(summary.id, claim.nonce, secret)
            finally:
                secret[:] = b"\0" * len(secret)
                secret.clear()
        return

    if isinstance(prompt, PassphrasePrompt):
        first = getpass_fn(f"Passphrase for {prompt.key_display_name}: ")
        if prompt.confirmation_required:
            second = getpass_fn("Confirm passphrase: ")
            if first != second:
                client.respond_to_interaction(
                    InteractionDecisionRequest(
                        summary.id, secret_decision=SecretDecision.CANCEL
                    )
                )
                raise ValueError("passphrases did not match")
        _send_secret(client, summary, claim.nonce, first)
        return

    if isinstance(prompt, (PasswordPrompt, ChallengePrompt)):
        label = (
            f"Password for {prompt.username}@{prompt.hostname}: "
            if isinstance(prompt, PasswordPrompt)
            else f"{prompt.text}: "
        )
        _send_secret(client, summary, claim.nonce, getpass_fn(label))
        return

    raise ValueError("unsupported daemon interaction")


def _send_secret(client, summary, nonce: str, value: str) -> None:
    secret = bytearray(value.encode("utf-8"))
    try:
        client.respond_to_interaction(
            InteractionDecisionRequest(
                summary.id,
                secret_decision=SecretDecision.SUBMIT,
                remember_policy=RememberPolicy.DO_NOT_STORE,
            )
        )
        client.send_interaction_secret(summary.id, nonce, secret)
    finally:
        secret[:] = b"\0" * len(secret)
        secret.clear()

# Typed interaction broker

Phase 8 makes authentication and trust interaction state daemon-owned. Phase 9
integrates this with production GTK SSH terminals. It does not parse terminal
output. OpenSSH invokes a private askpass helper, the helper contacts
`InteractionBroker`, and the broker publishes only typed safe metadata:

```text
OpenSSH -> private askpass helper -> InteractionBroker
                                      |
                                      +-> interaction event -> eligible client
                                      +<- typed decision + one-use secret frame
```

## Identity and lifecycle

Every interaction has an immutable daemon-lifetime
`interaction-<n>` ID and links to one session and stable
connection ID. Supported types are:

- `host_key_confirmation`;
- `password`;
- `private_key_passphrase`.

States are `pending`, `claimed`, `answered`, `cancelled`, `expired`, and
`failed`. Answered/cancelled/expired/failed are final. Exactly one result wins
under response, timeout, disconnect, session-close, process-exit, and shutdown
races.

One shared monotonic scheduler owns deadlines: password/passphrase interactions
default to 120 seconds and host-key confirmation to 180 seconds. Completed
metadata is capped at 100 records; secret values and responder nonces are never
retained in public summaries.

## Responder ownership

The client that originated the session or an attached session client may
observe it. A client explicitly claims the interaction; the claim contains a
fresh private 16-byte nonce. Only that handshaken client may reserve a decision
and send the matching secret. A claim conflict is deterministic. Disconnect
before answer releases the claim so another eligible client can take over.

The selected secret backend may act as a daemon responder. It never grants a
frontend access to backend objects or lookup keys.

## Typed prompts

Password metadata contains only host, port, username, attempt count, remember
availability, and whether an automatic saved value may exist. Passphrase
metadata contains a bounded safe key display name, optional public
fingerprint, attempt, and remember availability. Askpass text is untrusted,
control-character stripped, length-bounded, and used only for conservative
classification; it is not copied into events or logs.

Host-key metadata contains the host, port, key type, SHA256 fingerprint, and
`unknown`/`changed`/`revoked` status. Unknown keys allow reject, accept once,
or accept and atomically store. Changed/revoked keys remain reject-only in
Phase 8.

## Concurrency and shutdown

Private askpass connections use a fixed four-worker pool and a bounded queue of
32. Backend access, key scanning, known-hosts writes, and user waits never run
on the daemon selector. Broker locks are released before callbacks or backend
operations. Session close/process exit cancels linked pending interactions.
Daemon shutdown closes the listener and all active helper transports, wakes
waiters, bounds worker joins, clears pending secret buffers, and removes its
private runtime directory.

## Keyboard-interactive, confirmation and presence prompts

Prompts that are neither a password nor a passphrase nor a host key are
brokered too. A keyboard-interactive challenge (an OTP or verification code,
for example) becomes a `keyboard_interactive` interaction with a
`ChallengePrompt`. An OpenSSH confirmation becomes `confirmation`, and a
security-key touch reminder becomes `security_key_presence`, which is passive
and needs no answer. Headless channels (SFTP, forwards, one-shot commands)
run with `SSH_ASKPASS_REQUIRE=force`, so these prompts reach the user there
as well. Passive autofill-only operations (live Host Info samples, history
reads) never raise a prompt.

## Session-only secrets

When the daemon advertises `interactions.session_secrets`, a password or
passphrase answer may carry `RememberPolicy.remember_for_session`. It is
queued with the askpass context, like a stored answer, and kept only after
the session authenticates. It lives in daemon memory, keyed by connection id
(password) or key path (passphrase), and is never handed to a secret
backend. Later prompts for the same connection or key are answered from it,
after any stored secret, including from autofill-only operations. A repeated
prompt after it was offered proves it wrong, and it is wiped. Broker close
wipes them all. This matters most where OpenSSH connection sharing is
unavailable (OpenSSH for Windows), because every channel logs in on its own
there.

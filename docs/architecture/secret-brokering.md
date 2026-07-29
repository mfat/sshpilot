# Secret brokering

Phase 8 keeps SSH authentication values outside ordinary Protocol v1 JSON,
events, terminal frames, replay, logs, argv, and environment values.

## One-use transport

After an eligible client claims a password/passphrase interaction it sends
typed decision metadata over JSON. A submit decision reserves one exact
interaction/client/nonce slot. The value then uses negotiated
`binary-secret-v1`:

```text
magic "SPSB"
version 1
flags 0
canonical interaction UUID
16-byte claim nonce
1..16384 raw secret bytes
```

The daemon validates frame kind, version, flags, canonical ID, responder,
nonce, interaction state, size, and NUL exclusion. The slot is consumed once.
A Phase 8 password/passphrase response must contain 1–16,384 bytes; empty
values and embedded NULs are rejected explicitly.
A duplicate, wrong-peer, wrong-nonce, late, or malformed frame is rejected and
never retried automatically. Python cannot guarantee physical memory
zeroisation, but mutable caller/broker buffers are overwritten and cleared at
the earliest ownership boundary.

## Private askpass channel

The daemon creates a mode-0700 private runtime directory containing a mode-0600
Unix socket and a minimal non-GTK helper launcher. The OpenSSH environment
contains only the helper path, private socket path, and a short-lived opaque
session token; it contains no password, passphrase, backend master password, or
host-key decision. On platforms with peer credentials, the broker verifies the
helper is the same user.

The helper sends the opaque token and bounded prompt metadata, waits until the
interaction deadline, writes only the resulting secret plus newline to stdout,
clears its mutable buffer, and exits nonzero on reject, expiry, cancellation,
daemon loss, or unsupported prompt.

## Existing backends and remembering

Daemon sessions reuse `ConnectionManager`/`SecretManager` canonical password
and passphrase APIs. No parallel credential schema is introduced. Stored
values may satisfy one bounded authentication attempt automatically. A known
failed value is not submitted repeatedly.

`store_after_success` and `replace_stored_after_success` stage a mutable value
inside the private session context. Consumption by askpass is not success.
The broker confirms authentication through OpenSSH's owned ControlMaster
status channel, then calls the selected backend and clears the staged value.
Failure, cancellation, exit, or shutdown clears it without storage.

Locked KDBX/Bitwarden master-password prompts remain a distinct local backend
unlock concern. Phase 8 never sends a backend master password as an SSH
password/passphrase response; failed automatic lookup falls back to direct
typed SSH credential entry.

## Host keys

The broker retrieves the presented public key, compares it with configured
known-hosts entries, and displays a SHA256 fingerprint as untrusted evidence,
not proof of identity. An accepted key is written to a session-private
known-hosts file and OpenSSH is launched with strict checking, the exact
accepted key algorithm, and no global known-hosts fallback. A key change
between scan and connection therefore fails.

Broker-owned OpenSSH options (`BatchMode`, `StrictHostKeyChecking`, known-hosts
pins, ControlMaster path, and related auth controls) are inserted immediately
after `ssh`/`-F` and conflicting earlier copies are stripped. OpenSSH keeps the
first obtained value for each option, so preference overrides cannot weaken the
session pin.

Persistent unknown-key acceptance uses an atomic mode-0600 known-hosts update,
respects the effective `HashKnownHosts` policy, and rejects unsafe primary-file
symlinks. There is no `StrictHostKeyChecking=no` fallback and changed/revoked
keys are not replaced in this phase.

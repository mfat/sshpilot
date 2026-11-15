# Decision: no SSH backend abstraction layer yet

**Status:** accepted, 2026-11-15
**Scope:** daemon SSH execution (terminal, SFTP, forwards, remote commands, SCP)

## Decision

OpenSSH stays the only SSH backend. We do not add a backend facade, a backend
registry or a filesystem interface ahead of a second backend. Native SSH
library work (libssh, a Rust implementation, a Python library) starts only
when a concrete target needs it: an Android client, or a Windows build that
must run without OpenSSH installed.

The one change made now is to write down the terminal runner contract the code
already follows (see [session runtime](session-runtime.md#process-runner-boundary)).

## Why

The seams a native backend would plug into already exist and already hide the
OpenSSH process from their callers:

| Seam | Where | What a native backend would supply |
| --- | --- | --- |
| Terminal runner | `daemon/session_runtime.py`: `TerminalSessionProcessRunner`, `SessionLaunchSpec` | A runner whose handle drives a channel with a remote PTY. `SessionLaunchSpec` holds no argv or PID. |
| SFTP runner | `daemon/sftp_runtime.py`: `SftpProcessRunner`, `SftpProcessHandle` | A handle carrying the existing `OpenSSHSFTPClient`, built over a channel to the `sftp` subsystem. The client is pure-Python SFTP v3 over any stream pair. |
| Forward runner | `daemon/forward_runtime.py`: `ForwardProcessRunner` | A handle that owns local listeners and forwarding channels. |
| Secret prompts | `daemon/interaction_broker.py`: `request_client_secret*` | The protected prompt transport for passwords, passphrases and one-time codes. |

Building a facade over these before a second backend exists would mean
guessing its shape.

`SshLauncher` (`daemon/ssh_launch.py`) is the **OpenSSH adapter**, not a
backend-neutral executor: it builds argv and environments and its scopes own
subprocesses. What a native backend would share with it is the set of launch
intents (terminal, SFTP, forward, SCP, remote command, copy-id), served by a
separate executor. `SshLauncher` itself stays as it is.

## Work a native backend would need

These are real work items, not plumbing, and any future plan must budget them:

- **One backend per connection.** A native terminal alone would leave SFTP,
  forwards, remote commands, privileged file access and broadcast on OpenSSH
  for the same connection. The daemon needs one backend choice per connection
  that every operation honours, with a typed refusal for unsupported intents.
- **Authentication versus readiness.** Today's verdict comes from OpenSSH
  debug output (`daemon/ssh_readiness.py`) or the PTY evidence gate, through
  an OpenSSH-shaped private callback. A native backend must report
  authentication, channel readiness and failures between the two separately.
  The evidence gate must be set per session, because the same runner also
  serves plugin protocols.
- **Credential lifecycle.** Keyring lookup, retry tracking and
  commit-after-authentication (`InteractionBroker.mark_authenticated`) are
  tied to askpass contexts. `request_client_secret` provides only the prompt.
- **SFTP stream adapter.** The client needs blocking streams that accept whole
  writes; async libraries need a bounded adapter with cancellable reads and
  clean handling of a closed channel.
- **Host-key verification.** On the login path OpenSSH verifies and records
  host keys. `core/known_hosts/` can parse and atomically write the files, but
  nothing verifies a server key during a connection.
- **Config parity.** Connections must resolve like `ssh <alias>`. `ssh -G`
  gives values, not the behaviour of `ProxyCommand`, `Match exec`,
  `LocalCommand` or `ControlMaster`, and does not exist on Android.
- **Identities and proxies.** Agent, certificates, FIDO and PKCS#11 keys,
  `ProxyJump` and `ProxyCommand`.

## Rules for any native backend, including a spike

- The host key is verified before any credential is sent. Changed and revoked
  keys are hard failures. Never use a library default that accepts unknown
  keys.
- Config keywords outside the backend's declared subset are refused before
  connecting, never ignored.
- No fallback: a native failure is never retried through OpenSSH, and an
  unsupported operation is refused rather than run through OpenSSH.
- Secrets travel only through the interaction broker.
- Refusals cross the API as codes plus parameters and are worded by a GTK
  presenter (see the localization rules in `CLAUDE.md`).
- No user-visible backend setting until a native backend reaches parity for
  the connections it accepts.

## When this is revisited

Pick the library by target: if Android is the goal, evaluate Rust-compatible
options first, so the spike is not thrown away. Run a feasibility spike (shell
and SFTP, with backend choice, credential lifecycle and the rules above) and a
separate parity study (config keywords, identities, proxies, the auth matrix
against both backends) before deciding to build it.

# SSH Pilot API Changelog

All public frontend-neutral API changes are recorded here. Application release
notes remain separate.

## Unreleased

### Phase 10: SFTP, Transfers, and Forwards (Added)

- Added daemon-owned SFTP service lifecycle with narrow capabilities
  (`sftp.read`, `sftp.write`, `sftp.events`, `sftp.metadata`, `sftp.mutate`)
  and client methods for open/attach/list/metadata/mutate operations.
- Added daemon-owned transfers with narrow capabilities
  (`transfers.read`, `transfers.write`, `transfers.events`, `transfers.upload`,
  `transfers.download`) for daemon-path upload/download and cancel.
- Added daemon-owned port forwards with narrow capabilities
  (`forwards.read`, `forwards.write`, `forwards.events`, `forwards.local`,
  `forwards.remote`, `forwards.dynamic`) for local/remote/dynamic forwards.
- Added matching CoreEvent types, ErrorCode values, and live DTOs; legacy coarse
  `sftp` and `port_forwarding` capabilities remain schema-only and are never
  advertised.
- `API_IMPLEMENTATION_VERSION` is `0.10`. Protocol remains additive `1.0`.

### Phase 9.3: GUI Transport Stability (Changed)

- PTY autofill uses the canonical `feed_child_data` widget/backend input API.
  Daemon-backed SSH terminals disable PTY autofill; authentication stays on
  interaction dialogs. Local/legacy GTK-owned children keep one-shot sudo /
  residual password fills without logging secrets.
- `DaemonClient` logs structured, payload-free transport timeout diagnostics
  (request id, method, elapsed time, instance id, queue depths, thread
  liveness) and exposes `threads_alive()` / `build_mismatch()` for tests.
- Handshake may include optional `daemon_started_at`, `development_revision`
  (`SSHPILOT_DEV_REVISION`), and `api_implementation_version`. Mismatch is
  surfaced as a safe warning; active sessions are never killed automatically.
- GUI tests isolate `XDG_RUNTIME_DIR` and force
  `SSHPILOT_CLIENT_MODE=in_process` so the suite cannot attach to a developer
  user daemon. Explicit env `in_process` wins over Stage C
  `terminal.daemon_backed_ssh` auto-promotion.
- Daemon session restore lists sessions through `GtkClientBridge` so a blocked
  control RPC cannot stall the GTK main loop behind welcome
  `connections.list`.

### Phase 9.2: Non-Blocking Session Open Acknowledgement (Changed)

- `sessions.open` now returns the accepted `starting` `SessionSummary` as soon
  as the bounded executor admits startup work. It no longer waits for PTY
  allocation, OpenSSH launch, host-key/password/passphrase interaction, or
  `running`.
- Startup failures after acknowledgement are reported only through session
  lifecycle events and `sessions.get`/`sessions.list`, never as a second RPC
  response for the same open.
- Executor admission rejection still returns retryable `server_busy` and marks
  the prepared record `failed` without a misleading `starting` summary.
- GTK `DaemonTerminalSessionController` treats `STARTING` as a successful open,
  attaches immediately, and updates the existing tab from asynchronous
  `failed`/`exited`/`closed` events.
- The normal five-second `DEFAULT_REQUEST_TIMEOUT` is unchanged.
- Follow-up: optional client-generated `client_open_token` for idempotent open
  reconciliation after genuine transport loss.

### Phase 9.1: Strict Terminal Routing (Added)

- Separated SSH terminal route selection (`SshTerminalRoute`) from daemon
  readiness (`DaemonTerminalReadiness`)
- Daemon route failures show clear readiness errors and never silently launch
  local internal SSH
- Secret vault unlock runs only after route resolution (and after daemon
  readiness for the daemon route)
- Preferences wording: “Use legacy local SSH terminals” (explicit mode, not
  automatic failure fallback)

### Phase 9: GTK Terminal Migration (Added)

- Added production GTK daemon SSH terminal path with VTE emulation as default
- Added multi-attachment support allowing multiple GTK tabs per daemon session
- Added exclusive input ownership with `claim_terminal_input` and `release_terminal_input` APIs
- Added session persistence across GTK restarts through detach/reattach mechanism
- Added `DaemonTerminalTabState` for per-tab session state tracking
- Added `TerminalSessionController` interface with `DaemonTerminalSessionController` implementation
- Added session restoration manager with safe metadata persistence (no secrets/output)
- Added live daemon sessions dialog for developer session discovery and reattachment
- Added continuity loss detection with local GTK markers (never sent to daemon)
- Added Stage C rollout: `terminal.daemon_backed_ssh` defaults to `True`
- Added explicit legacy fallback via `terminal.legacy_local_ssh_fallback` setting
- Added daemon terminal close policies: detach (default), terminate, or ask
- Added broadcast command integration limited to input-owning terminals
- Added VTE as unified daemon SSH emulator (PyXtermJS remains for local terminals)

### Phase 9: Behavior Changes

- **Breaking**: `terminal.daemon_backed_ssh` now defaults to `True` (was `False`)
- Changed daemon SSH from experimental to production for SSH terminal sessions
- Changed terminal activation to prefer daemon when capabilities available
- Changed close behavior to detach by default (preserves running sessions)

### Added

- Added daemon-owned typed host-key, password, and private-key-passphrase
  interactions with strict daemon-lifetime IDs, claim ownership, deadlines,
  cancellation, bounded retention, and safe lifecycle events.
- Added capability-gated `binary-secret-v1` one-use responder-bound frames;
  secret values never enter JSON, events, terminal replay, logs, argv, or
  process environment.
- Added a private same-user daemon askpass helper channel, conservative prompt
  classification, bounded attempts, existing selected-backend lookup, and
  remember-after-authentication-success storage.
- Added strict unknown-host accept-once/accept-and-store handling through an
  exact session key pin. Changed/revoked keys remain blocking failures.
- Added experimental daemon-mode GTK interaction dialogs on an independent
  bridge lane so authentication UI does not block terminal streaming.
- Added daemon-owned Unix PTYs with exact child/process-group ownership, one
  shared non-blocking PTY I/O owner, bounded input, and final-output draining.
- Added negotiated `binary-terminal-v1` frames, per-session absolute byte
  sequences, bounded 2 MiB replay rings, attach-time replay, and explicit
  slow-peer continuity loss.
- Added truthful `terminal.output`, `terminal.input`, `terminal.resize`, and
  `terminal.replay` capabilities plus daemon terminal subscriptions, input,
  resize, and replay operations.
- Added a development-only VTE feed integration through the bounded GTK bridge;
  the normal terminal launch path remains unchanged.

- Added daemon-owned monitoring for the SSH root, resolved includes, wildcard
  include directories, and JSON-backed connection metadata.
- Added debounced authoritative reload with last-known-good rollback and
  stable-ID semantic diff publication through existing connection events.
- Added single-token stale UUID-marker recognition for rename-safe external
  edits.
- Added `DaemonClient`, the `python -m sshpilot.daemon` development entry point,
  secure per-user Unix-socket lifecycle, strict length-prefixed JSON envelopes,
  Protocol v1 handshake, request correlation, and structured transport errors.

- Added explicit daemon methods `system.handshake`,
  `system.get_capabilities`, `connections.list`, and `connections.get`.
- Added shared connection contracts across `InProcessClient` and
  `DaemonClient`, plus framing, handshake, socket-security, and lifecycle tests.
- Added the experimental `SSHPILOT_CLIENT_MODE=daemon` GTK composition path,
  bounded on-demand daemon launcher, application-scoped GTK worker bridge, and
  safe compatibility-mode fallback.
- Added typed daemon forwarding for `connection.created`,
  `connection.updated`, and `connection.deleted`, with daemon-global sequences,
  bounded per-peer queues, selector-driven partial writes, and explicit
  overflow disconnection.
- Added one persistent `DaemonClient` reader, pending-response correlation,
  bounded event dispatch isolated from socket reads, sequence validation, and
  application-scoped coalesced GTK refreshes.
- Added the truthful `connections.events` capability; experimental GTK daemon
  selection now requires both snapshot reads and live connection events.
- Added Protocol v1 `connections.create`, `connections.update`, and
  `connections.delete`, the truthful `connections.write` capability, strict
  secret-free mutation codecs, and shared write contracts across both clients.
- Added non-retryable `mutation_ambiguous`, `connection_already_exists`, and
  `persistence_failed` errors for deliberate mutation failure handling.
- Added a 4 MiB total per-peer outbound bound covering responses and events.
- Added immutable UUIDv4 identity to every persisted connection, secure
  idempotent upgrade migration, duplicate/malformed identity repair, and
  UUID-based group, metadata, and saved-layout references.
- Added stable `connection:<uuid>` public IDs plus deprecated Protocol v1
  lookup compatibility for the former nickname-derived ID form.
- Added daemon-owned `session:<uuid>` lifecycle records, an explicit
  `created`/`starting`/`running`/`closing`/`exited`/`failed`/`closed` state
  machine, bounded closed-record retention, and logical multi-client
  attachment bookkeeping.
- Added Protocol v1 `sessions.list`, `sessions.get`, `sessions.open`,
  `sessions.attach`, `sessions.detach`, and `sessions.close`, plus truthful
  `sessions.read`, `sessions.write`, and `sessions.events` capabilities.
- Added typed `session.created`, `session.state_changed`, `session.exited`, and
  `session.closed` forwarding on the existing daemon-global event sequence.
- Added a daemon-internal process-runner boundary with exact process ownership,
  one shared reaper, and bounded terminate/kill shutdown; Phase 7 supplies the
  non-interactive PTY runner.
- Added a daemon-scoped four-worker session command executor with a hard
  64-command bound, per-session serialization, stable internal peer tokens,
  selector-owned deferred response completion, and bounded shutdown draining.
- Added retryable `server_busy` for non-blocking session-command admission
  failure.
- Added the schema-only `replay_terminal` client operation and complete
  package-level convenience exports for all documented model types.
- Aligned schema-only `delete_connection` with `DeleteConnectionRequest`.

### Changed

- Daemon connection mutations now share a bounded configuration command lane
  with external reload. Self-write notifications reconcile as semantic no-ops,
  so Protocol v1 methods, capabilities, DTOs, and event names are unchanged.
- Increased `API_IMPLEMENTATION_VERSION` to `0.9`; `PROTOCOL_VERSION` remains
  compatible `1.0`.
- The broker-enabled native SSH launch keeps the canonical builder/auth
  resolver, disables `BatchMode` only when a capable typed responder path
  exists, and keeps strict exact-key verification. Unrestricted
  keyboard-interactive prompts remain unsupported.
- Capability discovery over `DaemonClient` now comes from the negotiated daemon
  response and advertises only contract-tested runtime capabilities.
- Defined publisher-global serial FIFO event delivery, including concurrent,
  re-entrant, unsubscription, and shutdown behaviour.
- Connection DTOs, mutation results, and events now always emit stable
  UUID-backed IDs. Rename and host metadata changes retain identity across
  reload and daemon restart.
- The GTK welcome page now keeps a non-blocking safe fallback visible when a
  structured connection-read error occurs.
- Daemon-backed GTK connection reads now run off the GTK main thread and use
  GLib delivery with refresh/destruction stale-result suppression. In-process
  mode remains the default.
- Daemon event continuity is process-lifetime only. Queue overflow, malformed
  events, sequence gaps, or transport loss close the affected client; no replay
  or automatic reconnect is implied.
- Experimental GTK daemon mode now requires read, event, and write
  capabilities. Basic CRUD runs on the GTK client worker without optimistic
  row changes; unsupported advanced, metadata, and secret edits are rejected
  rather than discarded.
- Renaming through `update_connection` returns and emits the same stable ID.
  Mutation requests are never automatically retried after ambiguous transport
  failure.
- `sessions.open` and `sessions.close` process-runner work no longer executes
  on the daemon selector. Open returns the captured `starting` acceptance
  snapshot as soon as the executor admits startup; later state changes arrive
  as events and never as a second open response. Close responds after bounded
  worker termination. Neither mutation is automatically retried after ambiguous
  transport loss, while logical attach/detach remain idempotent set operations
  on one connection.
- Replaced the pre-runtime schema-only session states with the seven-state
  daemon lifecycle and removed caller-supplied client IDs from open/attach
  requests. This is an API 0.6 Python source change but not a Protocol v1 wire
  break because the former models had no implemented session wire methods.

### Deprecated

- Nickname-derived `connection:v1:<hash>` values are accepted only as current
  lookup aliases during the remaining Protocol v1 compatibility window. They
  are never emitted and are scheduled for removal in Protocol v2.

### Removed

### Fixed

### Security

- UUID migration uses mode-0600 same-directory temporary files, atomic replace,
  one-shot backups, symlink refusal for JSON state, and safe rollback without
  logging raw connection records.
- Restricted daemon endpoints to owned mode-0700 directories and mode-0600
  sockets; stale cleanup verifies type and inode and refuses symlinks or
  non-socket paths.
- The GTK launcher validates endpoint ownership/type/permissions before
  connecting, uses an argv launch with `shell=False`, detaches child output,
  and strips known session-secret environment variables.
- Wire serialization accepts only strict JSON envelopes and explicit public DTO
  codecs with a 1 MiB frame limit; pickle, marshal, arbitrary objects, raw
  exceptions, persistence records, and secret values cannot cross the boundary.
- Excluded terminal output bytes, replay bytes, and plugin operation result
  values from dataclass `repr`; drift tests now enforce this for every field
  classified sensitive.
- Event payloads are now bound to approved public payload types and excluded
  from event `repr`; structured error details accept only validated safe values
  and exclude details from error `repr`.
- Session wire payloads expose only stable IDs, typed state, timestamps, safe
  exit information, sanitised failures, capabilities, and attachment counts;
  command lines, environments, process handles, PTY paths, prompts, and secret
  material remain private or absent.

## Protocol v1 — Initial documented baseline

### Added

- Protocol version `1.0` and API implementation version `0.1`.
- Synchronous `SshPilotClient` protocol.
- `InProcessClient` adapter.
- Capability discovery with stable capability identifiers.
- Implemented `connections.read` operations: connection list and retrieval.
- Secret-free `ConnectionSummary` and `ConnectionDetails` projections.
- Transitional opaque connection IDs derived from protocol and nickname.
- Structured `SshPilotError` envelopes and stable error codes.
- Frontend-neutral `CoreEvent`, subscription, and publisher infrastructure.
- Runtime `connection.created`, `connection.updated`, and
  `connection.deleted` event adaptation from manager signals.
- Schema-only connection-write, session, terminal-byte, replay, interaction,
  transfer, SFTP, port-forward, and plugin models.
- Schema-only session and asynchronous error event identifiers.
- Contract-test foundations and one migrated GTK connection-read path.
- Maintained API reference, compatibility policy, structural catalog,
  documentation drift checks, and public-surface snapshot.

### Security

- Ordinary connection DTOs exclude passwords, passphrases, key/certificate
  paths, provider objects, environments, and internal records.
- Terminal and interaction secret-bearing fields are classified sensitive;
  secret input models suppress values from `repr` where implemented.
- Structured errors exclude raw exceptions and stack traces.

### Not implemented

- Connection writes
- Core-owned runtime sessions, PTYs, terminal input/output, attach, or replay
- Interaction broker
- SFTP, forwarding, plugin, or secret client operations
- Remote access, TCP/WebSocket transport, named pipes, and terminal/session
  event transport

# SSH Pilot API Changelog

All public frontend-neutral API changes are recorded here. Application release
notes remain separate.

## Unreleased

### Added

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
  one shared reaper, bounded terminate/kill shutdown, and a production-safe
  unsupported runner until prompt-safe PTY startup exists.
- Added the schema-only `replay_terminal` client operation and complete
  package-level convenience exports for all documented model types.
- Aligned schema-only `delete_connection` with `DeleteConnectionRequest`.

### Changed

- Increased `API_IMPLEMENTATION_VERSION` to `0.6`; `PROTOCOL_VERSION` remains
  compatible `1.0`.
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
- `sessions.open` returns the current record after bounded startup initiation;
  later state changes arrive as events. Open/close are not automatically
  retried after ambiguous transport loss, while logical attach/detach are
  idempotent set operations on one connection.
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

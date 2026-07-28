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
- Added the schema-only `replay_terminal` client operation and complete
  package-level convenience exports for all documented model types.
- Aligned schema-only `delete_connection` with `DeleteConnectionRequest`.

### Changed

- Increased `API_IMPLEMENTATION_VERSION` to `0.2`; `PROTOCOL_VERSION` remains
  compatible `1.0`.
- Capability discovery over `DaemonClient` now comes from the negotiated daemon
  response and remains limited to `connections.read`.
- Defined publisher-global serial FIFO event delivery, including concurrent,
  re-entrant, unsubscription, and shutdown behaviour.
- Marked nickname-derived connection IDs as explicitly transitional with a
  UUID-and-alias migration plan required before wire-protocol freeze.
- The GTK welcome page now keeps a non-blocking safe fallback visible when a
  structured connection-read error occurs.

### Deprecated

### Removed

### Fixed

### Security

- Restricted daemon endpoints to owned mode-0700 directories and mode-0600
  sockets; stale cleanup verifies type and inode and refuses symlinks or
  non-socket paths.
- Wire serialization accepts only strict JSON envelopes and explicit public DTO
  codecs with a 1 MiB frame limit; pickle, marshal, arbitrary objects, raw
  exceptions, persistence records, and secret values cannot cross the boundary.
- Excluded terminal output bytes, replay bytes, and plugin operation result
  values from dataclass `repr`; drift tests now enforce this for every field
  classified sensitive.
- Event payloads are now bound to approved public payload types and excluded
  from event `repr`; structured error details accept only validated safe values
  and exclude details from error `repr`.

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
- Remote access, TCP/WebSocket transport, named pipes, and daemon event or
  terminal/session transport

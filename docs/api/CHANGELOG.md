# SSH Pilot API Changelog

All public frontend-neutral API changes are recorded here. Application release
notes remain separate.

## Unreleased

### Added

### Changed

### Deprecated

### Removed

### Fixed

### Security

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
- `DaemonClient`, IPC transport, wire serialization, or remote access

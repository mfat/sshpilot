# SSH Pilot frontend-neutral API

This directory is the maintained reference for the transport-neutral contract
implemented by `sshpilot.api`. The Python models and tested client behaviour are
the source of truth. Architecture documents explain why the boundary exists;
these documents describe the concrete contract.

## Reference

- [Protocol v1](protocol-v1.md) — scope, identity, conventions, ordering, and
  wire framing/security rules
- [Methods](methods.md) — every `SshPilotClient` method and its runtime status
- [Models](models.md) — DTO and enum semantics
- [Generated model index](generated/model-index.md) — field-by-field structural
  reference and synthetic examples
- [Events](events.md) — event payload, ordering, and delivery behaviour
- [Errors](errors.md) — stable machine-readable failures
- [Capabilities](capabilities.md) — feature discovery and current providers
- [State machines](state-machines.md) — health, session, interaction, transfer,
  and forwarding states

## Governance

- [Compatibility](compatibility.md) — Protocol v1 compatibility and versioning
- [Maintenance](maintenance.md) — required workflow for public API changes
- [API changelog](CHANGELOG.md) — public contract history
- [Implementation audit](implementation-audit.md) — code/documentation/test
  inventory and known gaps
- [Generated structural schema](generated/schema.json) — deterministic,
  machine-readable catalog; it is not OpenAPI or an HTTP contract

## Current runtime baseline

`InProcessClient` currently advertises only `connections.read`. Capability
discovery, connection listing/retrieval, connection-created/updated/deleted
events, subscription cleanup, and client shutdown are implemented. Connection
writes and all session, terminal, interaction, SFTP, forwarding, plugin, and
secret operations are unsupported or schema-only.

`DaemonClient` negotiates the same `connections.read` capability over a secure
per-user Unix socket and passes the shared connection contract. The daemon
supports handshake, capability discovery, connection list/get, structured
errors, and clean lifecycle only. It does not forward events or own terminal,
session, secret, prompt, SFTP, forwarding, or plugin runtime.

The API package is GTK-free. The existing managers wrapped by
`InProcessClient` are not yet GTK/GObject-free, so this is a frontend-neutral
boundary rather than a completed headless core.

See [the daemon transport architecture](../architecture/daemon-transport.md)
for concurrency, socket security, lifecycle, and deferred platform work.

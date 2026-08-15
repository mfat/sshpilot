# SSH Pilot frontend-neutral API

This directory is the maintained reference for the transport-neutral contract
implemented by `sshpilot.api`. The Python models and tested client behaviour are
the source of truth. Architecture documents explain why the boundary exists;
these documents describe the concrete contract.

## Reference

These topic guides describe the current Protocol 1.0/API 0.40 contract:

- [Daemon lifecycle](daemon-lifecycle.md)
- [Sessions](sessions.md)
- [Interactions](interactions.md)
- [SFTP](sftp.md)
- [Transfers](transfers.md)
- [Forwards](forwards.md)
- [Errors and states](errors-and-states.md)
- [Versioning and capabilities](versioning-and-capabilities.md)


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
- [Generated structural schema](generated/schema.json) — deterministic,
  machine-readable catalog; it is not OpenAPI or an HTTP contract

## Current runtime baseline

`DaemonClient` negotiates connection read/event/write, daemon-lifetime session lifecycle,
binary PTY terminal streaming, typed authentication/trust interactions, SFTP,
transfers, and port forwarding over a secure per-user Unix socket.

See the topic guides above and [capabilities.md](capabilities.md) for the
current advertised surface. Plugin settings, command, session-facing, and
secret APIs are included where listed by the current capabilities and methods
references; unsupported capabilities remain explicit and never trigger a
frontend fallback.

The public API implementation version is `0.40`; the wire protocol remains
`1.0`.

The API package is GTK-free. Compatibility shims over existing managers are
documented in [core-compatibility-shims.md](../architecture/core-compatibility-shims.md).

See [the daemon transport architecture](../architecture/daemon-transport.md)
for concurrency, socket security, and lifecycle details.

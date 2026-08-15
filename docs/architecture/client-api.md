# Current client/API architecture

`SshPilotClient` is the stable frontend-neutral seam. Production frontends use
typed requests, results, events, capabilities, and structured errors; they do
not own daemon state, SSH processes, PTYs, SFTP services, transfers, forwards,
secrets, or persistent configuration.

```text
GTK / CLI / future frontends
        ↓
SshPilotClient
        ↓
DaemonClient / daemon transport
        ↓
daemon services/runtime
```

## Client roles

`DaemonClient` is the production transport for daemon-owned operations. It
uses the local authenticated daemon transport, negotiates Protocol v1 and
capabilities, sends typed requests, receives typed responses/events, and
streams terminal and protected-secret data through their dedicated binary
frames.

Direct core service composition is retained for daemon unit tests. It is not a
client implementation and is never selected by a production frontend.

The client implementations share the same frontend-neutral models and error
semantics. Unsupported capabilities produce `unsupported_capability`; clients
never silently select a frontend-owned backend.

## Ownership boundary

The daemon owns authoritative persistence, connection and group state, native
OpenSSH process launch, PTYs and sessions, terminal streams, interactions,
SFTP, transfers, forwarding, secret brokering, identity/key operations, and
plugin operational state. Frontends own presentation, navigation, selection,
dialogs, rendering, local layout, file/portal selection, browser launch, and
other explicitly frontend-local/platform operations.

DTOs are deliberate public values, not persistence objects, manager instances,
GObjects, subprocesses, PTYs, provider handles, or callbacks. Ordinary API
payloads contain no passwords, passphrases, private keys, provider tokens,
secret-bearing environments, or raw terminal handles. Protected interaction
responses carry secret input only through the one-use secret path.

## Runtime behavior

`SshPilotClient` commands are synchronous at the Python API boundary. The GTK
bridge runs blocking daemon calls away from the UI thread and marshals typed
results/events back to the main context. `DaemonClient` owns one persistent
socket reader, correlates responses by request ID, enforces bounded timeouts,
and delivers events through a bounded serial handoff. Daemon services own
long-running operation lifecycle, cancellation, resource attachment, replay,
and shutdown semantics.

Remote terminal activation follows the daemon route:

```text
frontend → DaemonClient.sessions.open
         → daemon session/PTY/OpenSSH runtime
         → typed session events and binary terminal stream
         → frontend renderer
```

Session records and terminal resources may outlive a frontend attachment.
Attach/detach, input ownership, replay, resize, interaction claims, and
resource closure are daemon operations. A daemon failure is surfaced as a
structured unavailable/recovery state; it does not start an internal GTK SSH,
SFTP, or other backend fallback.

## Versioning and extension rules

Current versions are:

```text
API implementation: 0.29
Protocol:           1.0
```

Protocol v1 compatibility is additive unless a deliberate contract decision
requires a version change. New public operations must define typed DTOs,
capabilities, errors, events, threading/cancellation behavior, and contract
tests before being advertised. Internal objects are never serialized by
inspection.

For concrete current API details, see:

- [API overview](../api/README.md)
- [Methods](../api/methods.md)
- [Capabilities](../api/capabilities.md)
- [Compatibility and versioning](../api/compatibility.md)

For transport and ownership enforcement, see:

- [Daemon transport](daemon-transport.md)
- [Core boundary](core-boundary.md)
- [Frontend closure audit](frontend-closure-audit.md)
- [Session runtime](session-runtime.md)

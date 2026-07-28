# SSH Pilot Protocol v1

<!-- api-version: 1.0 -->

## Purpose

Protocol v1 defines a common frontend-neutral contract through which GTK and
future CLI, Tauri, daemon, or other clients can interact with SSH Pilot core
services. It defines Python calling semantics, DTOs, capabilities, events, and
structured errors independently of a wire transport.

## Scope

The current runtime is `InProcessClient`. It implements capability discovery,
connection reads, connection events, subscriptions, and shutdown. Other
methods and models establish vocabulary but are explicitly unsupported or
schema-only. No daemon, IPC handshake, Unix socket, named pipe, WebSocket,
HTTP endpoint, remote access, or `DaemonClient` exists.

See [methods](methods.md) and [capabilities](capabilities.md) for the precise
runtime matrix.

## Ownership boundary

- **Core-owned:** connection persistence and validation, native SSH/auth policy,
  runtime session/process/PTY lifecycle, SFTP operations, secret-provider
  access, and frontend-neutral events.
- **Frontend-owned:** GTK/Tauri/CLI rendering, tabs, dialogs, toasts, terminal
  display, focus, keybindings, and user interaction presentation.
- **Transport-owned:** framing, serialization, local endpoint security,
  request/response correlation, backpressure, reconnect, and handshake.

The current code has not completed that ownership split. Read
[the boundary audit](../architecture/core-boundary-audit.md) and
[daemon ownership](../architecture/daemon-ownership.md) for migration details.

## Protocol identity

| Identifier | Current value | Meaning |
| --- | --- | --- |
| `PROTOCOL_VERSION` | `1.0` | Public contract family and compatibility semantics |
| `API_IMPLEMENTATION_VERSION` | `0.1` | Version of the Python API implementation |

`get_capabilities()` returns both values plus `ClientInfo`, `CoreInfo`, and a
`CompatibilityResult`. In-process compatibility is currently constructed as
compatible with Protocol `1.0`; it is not negotiated.

A future daemon handshake must exchange supported protocol versions before any
stateful command, reject incompatible major versions with a structured error,
and then return capabilities. Those semantics are future work, not present
runtime behaviour. Protocol identity is therefore internal to the current
process today.

## Identifiers

All public IDs are opaque, non-empty strings. Clients may compare them within a
current snapshot but must not parse them.

| Type | Intended identity | Current stability |
| --- | --- | --- |
| `ConnectionId` | Saved connection | Current adapter hashes `protocol + NUL + nickname`; stable across reload while both stay unchanged, changes on rename |
| `SessionId` | Runtime terminal session | Schema only; no allocation or persistence guarantee |
| `RequestId` | Operation/request correlation | Schema only; no allocator |
| `InteractionId` | One frontend interaction | Schema only; no allocator |
| `TransferId` | One transfer | Schema only; no allocator |
| `ClientId` | One frontend client | Schema only; optional in `ClientInfo` |
| `AttachmentId` | One client/session attachment | Schema only; no allocator |

Current connection IDs are transitional and are not persistence UUIDs. A future
immutable-ID migration is potentially breaking unless aliases preserve lookup.

<!-- api-connection-id: transitional-nickname-hash -->

Clients must not persist current `ConnectionId` values as long-lived external
references. Before freezing a daemon wire protocol, persistence must assign an
immutable UUID to every connection. Migration will retain the current
`protocol + NUL + nickname` hash as a temporary lookup alias, accept both forms
for a documented compatibility window, and emit UUID-backed IDs after clients
refresh their connection list.

## Data conventions

- Python dataclass fields without defaults are required; fields with defaults
  are optional at construction.
- `Optional[T]` accepts `None`. Absence versus explicit null has no wire
  semantics because no serializer or transport is implemented.
- Public enums are lowercase string enums. Exact values are listed in
  [models](models.md).
- Timestamps are timezone-aware `datetime` values. When serialized by a future
  transport they must use RFC 3339/ISO 8601 UTC form, for example
  `2030-01-01T00:00:00Z`.
- Tuple and frozen-set fields are immutable Python collections in-process. A
  future text encoding may represent them as arrays without implying mutable
  core state.
- Sequences are non-negative integers. `CoreEvent.sequence` is global only to
  one `EventPublisher`; terminal sequences are schema-only and intended to be
  per session.
- Unknown fields and unknown enum values have no defined deserialization
  behaviour yet because Protocol v1 has no wire codec. Future codecs must
  define forward-compatible unknown-field handling before deployment.

The [generated structural catalog](generated/schema.json) records actual field
types, defaults, required flags, and sensitive classifications.

## Terminal bytes

Terminal input, output, and replay payloads are `bytes`. PTY output must never
be assumed to be UTF-8. In-process APIs preserve bytes exactly. A future
transport should prefer binary frames; base64 is acceptable only inside a text
envelope. Frontends own decoding strategy, terminal emulation, and rendering.

No runtime terminal streaming, batching, replay, or slow-consumer policy is
implemented.

## Ordering

- `list_connections()` preserves the order returned by the current
  `ConnectionManager`.
- `get_connection()` returns one current snapshot.
- In-process events use one monotonically increasing sequence per client,
  starting at zero.
- Events are accepted into one serial FIFO. The first active publisher drains
  it; concurrent publishers wait, and all subscribers observe sequence order.
- Re-entrant publication queues behind the current subscriber snapshot without
  recursively growing the callback stack.
- The three connection events follow the order in which manager signals reach
  the adapter. They are not persisted or replayed.
- Session events, terminal output ordering, per-session replay, and cross-client
  global ordering are schema-only and have no runtime guarantee.

## Cancellation and timeouts

Current client calls are synchronous and expose no cancellation token or
timeout parameter. Unsupported methods fail immediately. `close()` tears down
subscriptions but does not cancel an already executing manager call.

The `operation_cancelled` and `operation_timed_out` error codes are reserved
schema vocabulary. Future cancellation must define request identity, race
semantics, cleanup, and whether completion can win over cancellation.

## Security rules

- Ordinary DTOs contain no passwords, passphrases, private-key contents,
  backend tokens, authentication environments, or provider objects.
- Secret input and terminal bytes must not be logged.
- `SshPilotError` exposes safe metadata, never raw internal exceptions or stack
  traces.
- Frontends must use core operations and interaction requests; they must not
  access secret providers directly.
- Internal persistence records, GTK/GObject objects, PTYs, subprocesses, and
  file descriptors are never public DTOs.
- Protocol v1 currently supports only same-process access. Remote access is out
  of scope.

## Transport independence

The contract is independent of in-process calls, Unix-domain sockets, Windows
named pipes, WebSocket, Tauri, GTK, and HTTP. Mentioning a possible transport
does not make it implemented or preferred.

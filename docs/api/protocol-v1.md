# SSH Pilot Protocol v1

<!-- api-version: 1.0 -->

## Purpose

Protocol v1 defines the frontend-neutral contract used both in-process and over
the local daemon transport. It covers Python calling semantics, deliberate
DTOs, capabilities, events, structured errors, and the local wire envelope.

## Scope

`InProcessClient` implements connection reads and in-process connection events.
`DaemonClient` implements equivalent connection reads plus live connection
events over a secure per-user Unix-domain socket. Other methods and models
establish vocabulary but are explicitly unsupported or schema-only. Named
pipes, TCP, WebSocket, HTTP, remote access, and terminal/session transport do
not exist.

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
| `API_IMPLEMENTATION_VERSION` | `0.4` | Version of the Python API implementation |

`get_capabilities()` returns both values plus `ClientInfo`, `CoreInfo`, and a
`CompatibilityResult`. `DaemonClient` first sends `system.handshake`, selects
Protocol `1.0` from the client's supported list, and then fetches negotiated
capabilities. Application versions are diagnostic identity only and never
substitute for protocol negotiation.

<!-- api-wire-framing: length-prefixed-json-v1 -->
<!-- api-handshake: required-once-before-ordinary-methods -->

## Wire framing and envelopes

Each local IPC message is four unsigned big-endian bytes followed by that many
UTF-8 JSON bytes. Payload length must be between 1 and 1,048,576 bytes. Frames
may be fragmented or coalesced by the socket. Empty, oversized, incomplete,
non-UTF-8, invalid-JSON, and non-object frames are rejected. Pickle, marshal,
arbitrary class serialization, and object `repr` are never used.

Every envelope has a strict `type` and rejects missing or extra fields:

- request: `protocol_version`, `request_id`, `method`, `params`, `client_id`;
- success: `protocol_version`, `request_id`, `result`;
- error: `protocol_version`, `request_id`, structured `error`;
- event: `protocol_version`, `event`, `sequence`, `payload`.

JSON is the control-plane encoding. Future terminal bytes require a separate
binary frame type or channel and are not base64-encoded by this implementation.

## Handshake and correlation

The first request supplies client name/version, supported protocol versions,
claimed client capabilities, and optional frontend type. The daemon returns its
application/core versions, selected protocol, implemented daemon capabilities,
compatibility status, and a random per-process server instance ID.

Ordinary requests before handshake and a second handshake are errors. Client
capability claims are diagnostic and are not authorization. Request IDs are
random UUID hex strings, unique for the connection and never derived from
request data. Duplicate requests, unknown response IDs, and response protocol
mismatches are protocol errors. A timed-out `DaemonClient` closes its socket so
a late response cannot be correlated with later work.

## Identifiers

All public IDs are opaque, non-empty strings. Clients may compare them within a
current snapshot but must not parse them.

| Type | Intended identity | Current stability |
| --- | --- | --- |
| `ConnectionId` | Saved connection | Current adapter hashes `protocol + NUL + nickname`; stable across reload while both stay unchanged, changes on rename |
| `SessionId` | Runtime terminal session | Schema only; no allocation or persistence guarantee |
| `RequestId` | Operation/request correlation | Random UUID hex per daemon request; never reused on a connection |
| `InteractionId` | One frontend interaction | Schema only; no allocator |
| `TransferId` | One transfer | Schema only; no allocator |
| `ClientId` | One frontend client | Random per `DaemonClient`; enforced after handshake |
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
- `Optional[T]` accepts `None`. Wire envelopes use explicit fields and nulls;
  unknown or omitted required fields are rejected in v1.
- Public enums are lowercase string enums. Exact values are listed in
  [models](models.md).
- Timestamps are timezone-aware `datetime` values. When serialized by a future
  transport they must use RFC 3339/ISO 8601 UTC form, for example
  `2030-01-01T00:00:00Z`.
- Tuple and frozen-set fields are immutable Python collections in-process and
  encoded as JSON arrays without implying mutable core state.
- Sequences are non-negative integers. `CoreEvent.sequence` is global only to
  one `EventPublisher`; terminal sequences are schema-only and intended to be
  per session.
- Protocol v1 envelope and connection/capability DTO codecs reject unknown fields
  and enum values. Additive wire evolution therefore requires an explicit
  compatibility change or a new tolerant envelope/version policy.

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
  the adapter.
- The daemon assigns one sequence across all accepted connection events,
  starting at zero per daemon instance. All handshaken clients receive the same
  sequence for the same event. New clients receive no history, and daemon
  restart may reset the sequence.
- Per-peer event queues are bounded to 256. Overflow disconnects only the slow
  peer so continuity cannot be silently lost. A fresh connection and
  `connections.list` snapshot are required after a future explicit reconnect.
- The complete per-peer output deque, including responses and events, is also
  bounded to 4 MiB of remaining frame bytes. Partial writes decrement that
  accounting; exceeding the bound disconnects only the affected peer.
- A successful create/update/delete emits exactly one matching connection
  event. Response and event frames share one output stream, so frontends must
  remain correct whether the response or event is delivered first.
- Session events, terminal output ordering, and per-session replay remain
  schema-only and have no runtime guarantee.

## Event/response multiplexing

The server places complete framed responses and events onto the same ordered
per-peer output deque and uses selector write readiness for partial writes.
The core event callback performs no socket I/O.

Each `DaemonClient` socket has one persistent reader thread. Request callers
register an opaque ID and serialize writes; only the reader decodes frames.
Responses complete the matching pending request, while connection events enter
a separate bounded serial dispatch queue. Thus a slow subscriber does not block
response correlation. Unknown or duplicate response IDs, malformed event
payloads, and duplicate/regressing/gapped event sequences are protocol errors
that close the transport and wake every pending caller.

## Cancellation and timeouts

Client calls are synchronous and expose no cancellation token. `DaemonClient`
uses a finite constructor-configurable transport timeout; timeout closes the
socket and returns `transport_timeout` for reads. After a write frame may have
been sent, timeout or closure returns non-retryable `mutation_ambiguous`;
clients must refresh before an explicit retry. `close()` tears down resources and can
interrupt a blocked socket call through socket shutdown.

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
- Protocol v1 supports same-process access and same-user local Unix sockets.
  Remote access is out of scope.

## Transport independence

Public DTOs remain independent of in-process calls, Unix-domain sockets,
Windows named pipes, WebSocket, Tauri, GTK, and HTTP. The current transport
implementation is Linux/Unix `AF_UNIX`; other transports remain future work.

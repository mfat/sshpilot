# SSH Pilot Protocol v1

<!-- api-version: 1.0 -->

## Purpose

Protocol v1 defines the frontend-neutral contract used by frontends over the
local daemon transport. It covers Python calling semantics, deliberate
DTOs, capabilities, events, structured errors, and the local wire envelope.

## Scope

`DaemonClient` implements connection reads/events and daemon-lifetime session control,
lifecycle events, SFTP services, transfers, and port forwards over the same
secure per-user Unix-domain socket. Capability-gated clients may also use
daemon-owned Unix PTYs, the binary terminal stream, and typed
authentication/trust interactions over a separate one-use secret frame.
Unrestricted keyboard-interactive prompts, reconnect replay, and session
persistence remain unsupported. Named pipes, TCP, WebSocket, HTTP, and remote
access do not exist.

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

The ownership split is complete. See the
[current architecture](../architecture.md), [core boundary](../architecture/core-boundary.md),
and [frontend closure audit](../architecture/frontend-closure-audit.md) for
current ownership and final evidence.

## Protocol identity

| Identifier | Current value | Meaning |
| --- | --- | --- |
| `PROTOCOL_VERSION` | `1.0` | Public contract family and compatibility semantics |
| `API_IMPLEMENTATION_VERSION` | `0.47` | Version of the Python API implementation |

`get_capabilities()` returns both values plus `ClientInfo`, `CoreInfo`, and a
`CompatibilityResult`. `DaemonClient` first sends `system.handshake`, selects
Protocol `1.0` from the client's supported list, and then fetches negotiated
capabilities. Application versions are diagnostic identity only and never
substitute for protocol negotiation.

<!-- api-wire-framing: length-prefixed-json-v1 -->
<!-- api-terminal-framing: binary-terminal-v2 -->
<!-- api-secret-framing: binary-secret-v2 -->
<!-- api-handshake: required-once-before-ordinary-methods -->

## Wire framing and envelopes

Each local IPC message is four unsigned big-endian bytes followed by that many
payload bytes. Control payloads are UTF-8 JSON and remain limited to 1,048,576
bytes. Negotiated terminal payloads begin with binary magic `SPTB`, use stream
version 2, and are limited to a 68-byte header plus 65,536 raw bytes.
Negotiated one-use secret responses begin with `SPSB`, use version 2, and carry
a 32-byte correlation ID, a 16-byte nonce, and at most 16,384 raw secret bytes.
Interaction responses use the interaction ID and responder nonce; explicit
connection-editor reveals use the request ID and `REVEAL_RESPONSE` frame kind.
Frames may be fragmented or coalesced by the socket. Empty,
oversized, incomplete, invalid JSON, malformed binary frames, unsupported flags,
and non-canonical identifiers are rejected. Pickle, marshal, arbitrary class
serialization, and object `repr` are never used.

Every envelope has a strict `type` and rejects missing or extra fields:

- request: `protocol_version`, `request_id`, `method`, `params`, `client_id`;
- success: `protocol_version`, `request_id`, `result`;
- error: `protocol_version`, `request_id`, structured `error`;
- event: `protocol_version`, `event`, `sequence`, `payload`.

JSON remains the control-plane encoding. Terminal output/input use the
capability-gated `binary-terminal-v2` format documented in
[terminal streaming](../architecture/terminal-streaming.md); raw bytes are
never base64-encoded.

Secret bytes use only capability-gated `binary-secret-v2`. A frontend first
claims an interaction and sends typed JSON decision metadata. The daemon then
reserves one exact interaction/client/nonce slot. Only that peer may send one
secret frame; it is delivered directly to the waiting private askpass channel,
never enqueued as an event, response, terminal frame, or replay item.

## Handshake and correlation

The first request supplies client name/version, supported protocol versions,
claimed client capabilities, optional frontend type, and supported binary frame
types. The daemon returns its
application/core versions, selected protocol, implemented daemon capabilities,
compatibility status, and a random per-process server instance ID.

Ordinary requests before handshake and a second handshake are errors. Client
capability claims are diagnostic and are not authorization. Request IDs are
opaque counter strings such as `request-1`, unique for the connection and never derived from
request data. Duplicate requests, unknown response IDs, and response protocol
mismatches are protocol errors. A timed-out `DaemonClient` closes its socket so
a late response cannot be correlated with later work.

The daemon reserves request IDs for deferred `sessions.close` until
selector-owned completion or peer closure. `sessions.open` acknowledges on
executor admission and therefore does not reserve the request ID through
process startup. Each accepted peer also receives an internal monotonically
allocated token that is never sent over the wire. Deferred completions match
this token as well as the request ID, preventing an old completion from
reaching a new socket after file descriptor reuse.

## Identifiers

All public IDs are opaque, non-empty strings. Clients may compare them within a
current snapshot but must not parse them.

| Type | Intended identity | Current stability |
| --- | --- | --- |
| `ConnectionId` | Saved connection | SSH Host alias (opaque string) |
| `SessionId` | Daemon-lifetime runtime session | `session-<n>` for one daemon process; not persisted across restart |
| `RequestId` | Operation/request correlation | Client-scoped `request-<n>`; never reused among outstanding requests |
| `InteractionId` | One daemon interaction | `interaction-<n>` for one daemon process; not persisted across restart |
| `TransferId` | One transfer | `transfer-<n>` for one daemon process |
| `ClientId` | One frontend client | `client-<n>` per process / handshake |
| `AttachmentId` | One logical client/session attachment | Daemon-scoped `attachment-<n>` counter |

Saved connection IDs are SSH Host aliases. Runtime IDs are daemon-scoped
counters (for example `session-12`). Request IDs are client-scoped counters.
Consumers must treat all identifiers as opaque strings.

<!-- api-connection-id: ssh-host-alias -->
<!-- api-session-id: daemon-counter-v1 -->
<!-- api-interaction-id: daemon-counter-v1 -->

See [stable connection identity](../architecture/connection-identity.md).

## Data conventions

- Python dataclass fields without defaults are required; fields with defaults
  are optional at construction.
- `Optional[T]` accepts `None`. Wire envelopes use explicit fields and nulls;
  unknown or omitted required fields are rejected in v1.
- Public enums are lowercase string enums. Exact values are listed in
  [models](models.md).
- Timestamps are timezone-aware `datetime` values. Session summaries serialize
  them using RFC 3339/ISO 8601 UTC form, for example
  `2030-01-01T00:00:00Z`.
- Tuple and frozen-set fields are immutable Python collections at the Python
  boundary and encoded as JSON arrays without implying mutable core state.
- Sequences are non-negative integers. `CoreEvent.sequence` is daemon-global;
  terminal sequence is a per-session absolute byte offset beginning at zero.
- Protocol v1 envelope and connection/capability DTO codecs reject unknown fields
  and enum values. Additive wire evolution therefore requires an explicit
  compatibility change or a new tolerant envelope/version policy.

The [generated structural catalog](generated/schema.json) records actual field
types, defaults, required flags, and sensitive classifications.

## Terminal bytes

Terminal input, output, and replay payloads are raw `bytes`. PTY output is not
assumed to be UTF-8 and the transport never decodes or normalizes it. Output
uses absolute byte offsets, a bounded 2 MiB per-session replay ring, and a
separate bounded peer queue. Frontends own decoding state, emulation, and
rendering. See [terminal streaming](../architecture/terminal-streaming.md).

## Ordering

- `list_connections()` preserves the order returned by the daemon's
  `ConnectionRepository` / `ConnectionApplicationService` snapshot.
- `get_connection()` returns one current snapshot.
- Events are accepted into one serial FIFO. The daemon assigns the sequence
  and the first active publisher drains
  it; concurrent publishers wait, and all subscribers observe sequence order.
- Re-entrant publication queues behind the current subscriber snapshot without
  recursively growing the callback stack.
- The three connection events follow the order in which manager signals reach
  the adapter.
- The daemon assigns one sequence across all accepted connection, session, and
  interaction lifecycle events,
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
- Session lifecycle events share that order. `session.created` precedes state
  transitions. Final PTY output and EOF are accepted before `session.exited`,
  which precedes `session.closed`.
- Open preparation accepts `session.created` and `starting`, then acknowledges
  the open RPC as soon as the executor admits startup. Later `running`/`failed`
  events arrive asynchronously and are never a second response for the same
  open. Close accepts `closing` before its deferred response and emits
  exit/closed events during worker completion. Frontends reconcile all of these
  by session ID rather than byte arrival.

## Event/response multiplexing

The server queues complete response, event, and terminal frames and uses
selector write readiness for partial writes. Control and lifecycle traffic has
priority over separately accounted terminal traffic. The core and PTY
callbacks perform no socket I/O.

Each `DaemonClient` socket has one persistent reader thread. Request callers
register an opaque ID and serialize writes; only the reader decodes frames.
Responses complete the matching pending request, lifecycle events enter one
bounded serial queue, and terminal frames enter a separate bounded terminal
dispatch queue. Secret frames are write-only from capable clients and never
reach callback dispatch. Thus a slow event or terminal subscriber does not
block response correlation. Unknown or duplicate response IDs, malformed event
payloads, invalid secret ownership/nonces, and duplicate/regressing/gapped
event sequences are protocol errors that close the offending transport and
wake its pending callers.

The server has a separate bounded command plane for blocking session work:
four daemon-owned workers, at most 64 outstanding commands, keyed FIFO
serialization per session, and a bounded completion queue. Workers call no
socket or selector API. They enqueue immutable success/error completions and
wake the selector; the selector validates peer token and request reservation,
then queues the response. A full command plane returns retryable
`server_busy` immediately. Connection mutations remain synchronous and may
still perform bounded persistence work on the selector.

## Cancellation and timeouts

Client calls are synchronous and expose no cancellation token. `DaemonClient`
uses a finite constructor-configurable transport timeout; timeout closes the
socket and returns `transport_timeout` for reads. After a connection mutation
or session open/close frame may have been sent, timeout or closure returns
non-retryable `mutation_ambiguous`; clients must refresh the relevant snapshot
before an explicit retry. Attach/detach are idempotent membership operations
but are not automatically reconnected or retried. `close()` tears down
resources and can interrupt a blocked socket call through socket shutdown.

Peer closure discards a deferred response but does not cancel an accepted open
or close: runtime ownership and cleanup are independent of response interest.
Daemon shutdown rejects new submissions, cancels commands that have not
started where safe, runs required exact-resource cleanup, drains workers under
one finite deadline, and discards late response completions.

Typed interactions own monotonic deadlines (120 seconds for password/
passphrase and 180 seconds for host-key decisions by default). Session close,
process exit, helper loss, responder cancellation, and daemon shutdown wake
pending waits. Exactly one response, cancellation, or expiry wins.

## Security rules

- Ordinary DTOs contain no passwords, passphrases, private-key contents,
  backend tokens, authentication environments, or provider objects.
- Secret input and terminal bytes must not be logged. Secret responses never
  use JSON, event history, terminal replay, argv, or environment values.
- `SshPilotError` exposes safe metadata, never raw internal exceptions or stack
  traces.
- Frontends must use core operations and interaction requests; they must not
  access secret providers directly.
- Internal persistence records, GTK/GObject objects, PTYs, subprocesses, and
  file descriptors are never public DTOs.
- Protocol v1 supports same-user local Unix sockets. Remote access is out of
  scope.

## Transport independence

Public DTOs remain independent of transport calls, Unix-domain sockets,
Windows named pipes, WebSocket, Tauri, GTK, and HTTP. The current transport
implementation is Linux/Unix `AF_UNIX`; other transports remain future work.

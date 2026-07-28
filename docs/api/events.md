# Events

Events are immutable `CoreEvent` records containing `type`, `payload`,
non-negative `sequence`, UTC `timestamp`, and optional request/connection/session
IDs.

<!-- api-event-semantics: serial-fifo-v1 -->

`InProcessClient` uses one publisher-global FIFO ordering point. Sequence
allocation and queue insertion are atomic, and exactly one publishing thread
drains the queue. Therefore every subscriber observes accepted events in
strictly increasing sequence order, including when publishers run concurrently.
The first active publisher is the dispatcher; a concurrent publisher waits
until its event has been delivered and its callbacks can run on that active
dispatcher thread rather than the concurrent caller's thread.

Callbacks run in subscriber-registration order. One slow subscriber delays the
remaining subscribers and later events but cannot reorder them. One
subscriber's exception is logged and does not prevent later subscribers.

Re-entrant publication appends to the same FIFO and does not recurse: event
`N` reaches its complete subscriber snapshot before re-entrantly published
event `N+1` begins. The re-entrant `publish()` call returns after enqueueing;
the outer dispatcher delivers it after the current callback stack unwinds.

Events are not durable, acknowledged, replayed, batched, or coalesced. A
subscriber that is absent or already closed does not receive prior events.

## Daemon delivery

<!-- api-daemon-event-semantics: global-sequence-bounded-v1 -->

The daemon subscribes once to its `InProcessClient` publisher and accepts only
the three typed connection events. It replaces the source publisher's
process-local sequence with one daemon-global sequence that begins at `0`.
Every healthy, handshaken peer receives the same sequence for the same accepted
event, in daemon acceptance order. Sequence assignment and peer enqueueing are
atomic. A daemon restart may reset the counter; Protocol v1 has no cross-restart
continuity, resume token, acknowledgement, or replay.

Each peer has a bounded queue of 256 event frames. The core callback encodes
once, enqueues immutable frame references, wakes the selector, and never writes
to a socket. Responses and events share the peer's ordered output stream;
partial writes resume without byte interleaving. A slow peer cannot delay the
core or another peer. If its queue is full, continuity is explicitly lost and
only that peer is disconnected; events are never silently dropped while the
connection remains usable.

`DaemonClient` has exactly one persistent socket reader. It correlates responses
through a pending-request table and sends events to a separate bounded serial
event-dispatch thread, so slow subscribers cannot stop response reading. The
first event after connection may have any daemon-global sequence because older
events are not replayed; every later event must be exactly the previous
sequence plus one. A duplicate, regression, gap, malformed payload, local event
queue overflow, or transport closure fails the transport and emits one safe
local `error.occurred` continuity notification where delivery remains possible.

## Inventory

| Event | Runtime status | Capability | Trigger | Payload |
| --- | --- | --- | --- | --- |
| `connection.created` | Implemented in-process and daemon | `connections.events` | Manager `connection-added` signal | `ConnectionSummary` |
| `connection.updated` | Implemented in-process and daemon | `connections.events` | Manager `connection-updated` signal | `ConnectionSummary` |
| `connection.deleted` | Implemented in-process and daemon | `connections.events` | Manager `connection-removed` signal | `ConnectionSummary` |
| `session.created` | Schema only | `terminal` | Future session creation | Intended `SessionSummary` |
| `session.state_changed` | Schema only | `terminal` | Future state transition | `SessionSummary` |
| `session.output` | Schema only | `terminal` | Future PTY output | Intended `TerminalOutput` |
| `session.interaction_requested` | Schema only | `interactions` | Future core prompt | Intended `InteractionRequest` |
| `session.exited` | Schema only | `terminal` | Future child exit | Intended `SessionExitInfo` |
| `session.closed` | Schema only | `terminal` | Future session cleanup | `SessionSummary` |
| `error.occurred` | Local runtime transport-continuity signal in `DaemonClient` | None fixed | Daemon transport/protocol continuity failure | Safe structured error envelope dictionary |

<!-- api-event: connection.created -->
## `connection.created`

- **Status / introduced:** Implemented in-process and over daemon transport / v1
- **Trigger / payload:** Existing `connection-added` GObject signal;
  `ConnectionSummary`.
- **Related IDs:** `connection_id` is populated and equals payload `id`.
- **Ordering / delivery:** In-process publisher-global serial FIFO; over IPC,
  daemon-global sequence and bounded at-most-once live delivery.
- **Coalescing / dropping:** Not coalesced. Absent/new subscribers miss prior
  events. Queue overflow disconnects the affected daemon peer.
- **Loop behaviour:** The adapter can be tested headlessly with a fake manager.
  Production signal timing may depend on GLib scheduling in
  `ConnectionManager`.

<!-- api-event: connection.updated -->
## `connection.updated`

- **Status / introduced:** Implemented / v1
- **Trigger / payload:** Existing `connection-updated` signal;
  `ConnectionSummary`.
- **Related IDs/order/delivery:** Same guarantees as `connection.created`.
- **Coalescing / dropping:** Every signal reaching the adapter is published;
  there is no coalescing or replay.

<!-- api-event: connection.deleted -->
## `connection.deleted`

- **Status / introduced:** Implemented / v1
- **Trigger / payload:** Existing `connection-removed` signal;
  `ConnectionSummary` adapted from the removed object.
- **Related IDs/order/delivery:** Same guarantees as `connection.created`.
- **Coalescing / dropping:** Not coalesced or buffered.

<!-- api-event: session.created -->
## `session.created`

- **Status / introduced:** Schema only / v1
- **Capability / intended trigger:** `terminal`; successful future session
  creation.
- **Payload / IDs:** Intended `SessionSummary` with session and connection IDs.
- **Guarantees:** None until runtime implementation and contract tests exist.

<!-- api-event: session.state_changed -->
## `session.state_changed`

- **Status / introduced:** Schema only / v1
- **Capability / intended trigger:** `terminal`; a runtime session transition.
- **Payload / IDs:** `SessionSummary`; session ID is intended.
- **Guarantees:** No transition, ordering, coalescing, or delivery semantics are
  implemented.

<!-- api-event: session.output -->
## `session.output`

- **Status / introduced:** Schema only / v1
- **Capability / intended trigger:** `terminal`; PTY output bytes.
- **Payload / IDs:** Intended `TerminalOutput` and session ID.
- **Guarantees:** No batching, replay, backpressure, retention, or slow-client
  policy exists. Do not infer these from the schema.

<!-- api-event: session.interaction_requested -->
## `session.interaction_requested`

- **Status / introduced:** Schema only / v1
- **Capability / intended trigger:** `interactions`; core requires user input.
- **Payload / IDs:** Intended `InteractionRequest`, request ID, and optional
  session ID.
- **Guarantees / security:** No delivery guarantee. Secret answers must never
  appear in the request event or event history.

<!-- api-event: session.exited -->
## `session.exited`

- **Status / introduced:** Schema only / v1
- **Capability / intended trigger:** `terminal`; child process exit.
- **Payload / IDs:** Intended `SessionExitInfo` and session ID.
- **Guarantees:** None; exit-versus-close ordering is not defined.

<!-- api-event: session.closed -->
## `session.closed`

- **Status / introduced:** Schema only / v1
- **Capability / intended trigger:** `terminal`; final session cleanup.
- **Payload / IDs:** `SessionSummary`; session ID is intended.
- **Guarantees:** None; no terminal-state delivery or replay promise.

<!-- api-event: error.occurred -->
## `error.occurred`

- **Status / introduced:** Safe schema plus local `DaemonClient` continuity
  emission / v1
- **Capability / trigger:** No fixed capability; transport closure, protocol
  violation, sequence discontinuity, or client event-queue overflow.
- **Payload / IDs:** A dictionary matching the `SshPilotError.to_dict()`
  envelope: required non-empty string `code` and `message`, plus optional
  `details`, `retryable`, and correlation IDs. Unknown envelope fields are
  rejected. Details use the same constrained policy as `SshPilotError.details`.
  The accepted mapping and nested lists/dictionaries are exposed read-only so
  one subscriber cannot mutate what later subscribers observe.
- **Guarantees / security:** This local event is not sent by the daemon and
  does not claim a daemon-global sequence. Raw exceptions, secret
  key names, arbitrary objects, environments, commands, and stack traces are
  rejected by payload validation.

## Subscriber lifecycle

Keep the returned `Subscription` and call `unsubscribe()` or `close()`.
Context-manager use is supported and cleanup is idempotent. Unsubscription
during an event affects later events; the current event still reaches the
subscriber snapshot captured when it was accepted.

`InProcessClient.close()` disconnects its manager signal handlers, rejects new
publication/subscription, and deactivates subscription handles. An event
already being delivered, plus events accepted into its FIFO, finish in order;
callbacks are then released. Closing from inside a callback does not deadlock.

`DaemonClient.close()` first rejects requests and event acceptance, closes the
socket to wake the sole reader, wakes pending requests, stops the event handoff,
and closes subscriptions. Close from a subscriber callback skips self-join and
does not deadlock. The daemon stops core acceptance and unsubscribes before it
closes peers and the core client.

Callbacks must marshal to their frontend thread when the dispatcher thread is
unsuitable. The API does not import GTK or implicitly call `GLib.idle_add`.

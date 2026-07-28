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

## Inventory

| Event | Runtime status | Capability | Trigger | Payload |
| --- | --- | --- | --- | --- |
| `connection.created` | Implemented | `connections.read` | Manager `connection-added` signal | `ConnectionSummary` |
| `connection.updated` | Implemented | `connections.read` | Manager `connection-updated` signal | `ConnectionSummary` |
| `connection.deleted` | Implemented | `connections.read` | Manager `connection-removed` signal | `ConnectionSummary` |
| `session.created` | Schema only | `terminal` | Future session creation | Intended `SessionSummary` |
| `session.state_changed` | Schema only | `terminal` | Future state transition | `SessionSummary` |
| `session.output` | Schema only | `terminal` | Future PTY output | Intended `TerminalOutput` |
| `session.interaction_requested` | Schema only | `interactions` | Future core prompt | Intended `InteractionRequest` |
| `session.exited` | Schema only | `terminal` | Future child exit | Intended `SessionExitInfo` |
| `session.closed` | Schema only | `terminal` | Future session cleanup | `SessionSummary` |
| `error.occurred` | Schema only | None fixed | Future asynchronous core failure | Safe structured error envelope dictionary |

<!-- api-event: connection.created -->
## `connection.created`

- **Status / introduced:** Implemented / v1
- **Trigger / payload:** Existing `connection-added` GObject signal;
  `ConnectionSummary`.
- **Related IDs:** `connection_id` is populated and equals payload `id`.
- **Ordering / delivery:** Publisher-global serial FIFO sequence; synchronous
  at-most-once delivery to the subscriber snapshot captured at publication.
- **Coalescing / dropping:** Not coalesced. No buffering; absent subscribers
  miss it.
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

- **Status / introduced:** Schema only / v1
- **Capability / intended trigger:** No fixed capability; future asynchronous
  safe failure.
- **Payload / IDs:** A dictionary matching the `SshPilotError.to_dict()`
  envelope: required non-empty string `code` and `message`, plus optional
  `details`, `retryable`, and correlation IDs. Unknown envelope fields are
  rejected. Details use the same constrained policy as `SshPilotError.details`.
  The accepted mapping and nested lists/dictionaries are exposed read-only so
  one subscriber cannot mutate what later subscribers observe.
- **Guarantees / security:** Runtime emission is absent. Raw exceptions, secret
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

Callbacks must marshal to their frontend thread when the dispatcher thread is
unsuitable. The API does not import GTK or implicitly call `GLib.idle_add`.

# Events

Events are immutable `CoreEvent` records containing `type`, `payload`,
non-negative `sequence`, UTC `timestamp`, and optional request/connection/session
IDs.

Current `InProcessClient` delivery is synchronous, in registration order, on
the thread that emitted the wrapped manager signal. One subscriber's exception
is logged and does not prevent later subscribers. Subscriptions are thread-safe
to create/remove and cleanup is idempotent.

Events are not durable, acknowledged, replayed, batched, or coalesced. A
subscriber that is absent or already closed does not receive prior events.

## Inventory

| Event | Runtime status | Capability | Trigger | Payload |
| --- | --- | --- | --- | --- |
| `connection.created` | Implemented | `connections.read` | Manager `connection-added` signal | `ConnectionSummary` |
| `connection.updated` | Implemented | `connections.read` | Manager `connection-updated` signal | `ConnectionSummary` |
| `connection.deleted` | Implemented | `connections.read` | Manager `connection-removed` signal | `ConnectionSummary` |
| `session.created` | Schema only | `terminal` | Future session creation | Intended `SessionSummary` |
| `session.state_changed` | Schema only | `terminal` | Future state transition | Payload not fixed beyond `CoreEvent[Any]` |
| `session.output` | Schema only | `terminal` | Future PTY output | Intended `TerminalOutput` |
| `session.interaction_requested` | Schema only | `interactions` | Future core prompt | Intended `InteractionRequest` |
| `session.exited` | Schema only | `terminal` | Future child exit | Intended `SessionExitInfo` |
| `session.closed` | Schema only | `terminal` | Future session cleanup | Intended `SessionSummary` or ID; not fixed |
| `error.occurred` | Schema only | None fixed | Future asynchronous core failure | Payload not fixed |

<!-- api-event: connection.created -->
## `connection.created`

- **Status / introduced:** Implemented / v1
- **Trigger / payload:** Existing `connection-added` GObject signal;
  `ConnectionSummary`.
- **Related IDs:** `connection_id` is populated and equals payload `id`.
- **Ordering / delivery:** Publisher-global sequence; synchronous at-most-once
  delivery to each currently registered callback.
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
- **Payload / IDs:** Payload shape is not yet fixed; session ID is intended.
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
- **Payload / IDs:** Session ID intended; payload type remains unfixed.
- **Guarantees:** None; no terminal-state delivery or replay promise.

<!-- api-event: error.occurred -->
## `error.occurred`

- **Status / introduced:** Schema only / v1
- **Capability / intended trigger:** No fixed capability; future asynchronous
  safe failure.
- **Payload / IDs:** Payload type is not fixed. It must use a safe structured
  envelope and may use correlation IDs.
- **Guarantees / security:** None. Raw exceptions and stack traces must not be
  published.

## Subscriber lifecycle

Keep the returned `Subscription` and call `unsubscribe()` or `close()`.
Context-manager use is supported. `InProcessClient.close()` disconnects its
manager signal handlers and clears subscribers. A callback must marshal to its
frontend thread when the source thread is unsuitable; the API does not
implicitly call `GLib.idle_add`.

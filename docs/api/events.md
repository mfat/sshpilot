# Events

Events are immutable `CoreEvent` records containing `type`, `payload`,
non-negative `sequence`, UTC `timestamp`, and optional request/connection/session
IDs.

<!-- api-event-semantics: serial-fifo-v1 -->
<!-- api-event: broadcast.output -->

The daemon uses one publisher-global FIFO ordering point. Sequence allocation
and queue insertion are atomic, and exactly one publishing thread drains the
queue. Therefore every subscriber observes accepted events in strictly
increasing sequence order, including when publishers run concurrently.
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

The daemon subscribes to its connection repository, its owned `SessionRuntime`
publisher, and its owned `InteractionBroker` publisher. It
accepts three connection events, four session lifecycle events, and two safe
interaction lifecycle events. It replaces each source publisher's process-local
sequence with one daemon-global sequence that begins at `0`.
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

Responses and events also share a 4 MiB total remaining-byte bound per peer.
Successful create/update/delete calls produce exactly one corresponding event
from the manager signal path; handlers never publish a duplicate. Because the
response and event use the same framed output deque, clients accept either
valid response/event ordering and refresh from the resulting snapshot.
Connection event payload IDs are SSH Host aliases. Alias rename is delete + create.
Startup identity migration is schema maintenance and emits no lifecycle event.

Session start and close execute on a bounded keyed worker pool, but workers do
not write transport frames. Runtime events enter the existing encoded event
path; deferred results enter a separate bounded completion queue. The selector
alone queues response frames. Created/starting and closing events are accepted
before their corresponding deferred responses, while later running/failed/
exited/closed events can interleave with response completion. Consumers use the
session ID and state machine, never arrival order alone.

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
| `connection.created` | Daemon implemented | `connections.events` | Repository commit | `ConnectionSummary` |
| `connection.updated` | Daemon implemented | `connections.events` | Repository commit | `ConnectionSummary` |
| `connection.deleted` | Daemon implemented | `connections.events` | Repository commit | `ConnectionSummary` |
| `session.created` | Daemon implemented | `sessions.events` | Session record allocation | `SessionSummary` |
| `session.state_changed` | Daemon implemented | `sessions.events` | Accepted lifecycle transition other than exit/close | `SessionSummary` |
| `session.output` | Legacy schema only; not emitted | `terminal.output` | None | Terminal bytes use dedicated binary frames |
| `session.interaction_requested` | Legacy schema only; not emitted | Legacy `interactions` | None | Replaced by typed interaction events |
| `session.exited` | Daemon implemented | `sessions.events` | Owned runtime resource exit | `SessionExitInfo` plus envelope session ID |
| `session.closed` | Daemon implemented | `sessions.events` | Final in-memory lifecycle transition | `SessionSummary` |
| `interaction.created` | Daemon implemented | `interactions.events` | Broker accepts a typed authentication/trust interaction | `InteractionSummary` |
| `interaction.state_changed` | Daemon implemented | `interactions.events` | Claim, release, answer, cancel, expiry, or failure | `InteractionSummary` |
| `sftp.created` | Daemon implemented | `sftp.events` | SFTP service record allocation | `SftpServiceSummary` |
| `sftp.state_changed` | Daemon implemented | `sftp.events` | Accepted SFTP lifecycle transition other than close/fail | `SftpServiceSummary` |
| `sftp.closed` | Daemon implemented | `sftp.events` | Final SFTP service closure | `SftpServiceSummary` |
| `sftp.failed` | Daemon implemented | `sftp.events` | SFTP service failure | `SftpServiceSummary` |
| `transfer.created` | Daemon implemented | `transfers.events` | Transfer record allocation | `TransferSummary` |
| `transfer.started` | Daemon implemented | `transfers.events` | Transfer begins moving bytes | `TransferSummary` |
| `transfer.progress` | Daemon implemented | `transfers.events` | Bounded progress update | `TransferSummary` |
| `transfer.item_completed` | Daemon implemented | `transfers.events` | One recursive item finished | `TransferSummary` |
| `transfer.completed` | Daemon implemented | `transfers.events` | Transfer finished successfully | `TransferSummary` |
| `transfer.cancelled` | Daemon implemented | `transfers.events` | Transfer cancelled | `TransferSummary` |
| `transfer.failed` | Daemon implemented | `transfers.events` | Transfer failed | `TransferSummary` |
| `forward.created` | Daemon implemented | `forwards.events` | Forward record allocation | `ForwardSummary` |
| `forward.starting` | Daemon implemented | `forwards.events` | Forward bind/startup begins | `ForwardSummary` |
| `forward.active` | Daemon implemented | `forwards.events` | Forward is listening/active | `ForwardSummary` |
| `forward.closed` | Daemon implemented | `forwards.events` | Forward closed | `ForwardSummary` |
| `forward.failed` | Daemon implemented | `forwards.events` | Forward failed | `ForwardSummary` |
| `daemon.state_changed` | Daemon implemented | `daemon.events` | Lifecycle state transition | `DaemonStatus` |
| `error.occurred` | Local runtime transport-continuity signal in `DaemonClient` | None fixed | Daemon transport/protocol continuity failure | Safe structured error envelope dictionary |

<!-- api-event: connection.created -->
## `connection.created`

- **Status / introduced:** Implemented over daemon transport / v1
- **Trigger / payload:** A successful repository/application-service
  persistence change; `ConnectionSummary`.
- **Related IDs:** `connection_id` is populated and equals payload `id`.
- **Ordering / delivery:** Daemon-global sequence and bounded at-most-once live
  delivery.
- **Coalescing / dropping:** Not coalesced. Absent/new subscribers miss prior
  events. Queue overflow disconnects the affected daemon peer.
- **Compatibility:** Direct core tests may derive the event from service
  notifications, but the daemon repository is authoritative in production.

<!-- api-event: connection.updated -->
## `connection.updated`

- **Status / introduced:** Implemented / v1
- **Trigger / payload:** A successful repository/application-service
  persistence change; `ConnectionSummary`.
- **Related IDs/order/delivery:** Same guarantees as `connection.created`.
- **Coalescing / dropping:** Every successful store change is published; there
  is no coalescing or replay.

<!-- api-event: operation.created -->
## `operation.created`

- **Status / review:** Shared daemon operation lifecycle completed and reviewed;
  identity operation producers remain pending their separate phase review.
- **Trigger / payload:** A daemon-owned operation is accepted;
  `OperationSummary` contains safe state and identifiers only.
- **Security:** No private keys, credentials, or secret values are included.

<!-- api-event: operation.state_changed -->
## `operation.state_changed`

- **Status / review:** Shared daemon operation lifecycle completed and reviewed;
  identity operation producers remain pending their separate phase review.
- **Trigger / payload:** An operation changes lifecycle state;
  `OperationSummary` contains safe state and identifiers only.
- **Ordering / delivery:** Delivered through the daemon event stream according
  to the negotiated event and capability contract.
- **Security:** No private keys, credentials, or secret values are included.

<!-- api-event: connection.deleted -->
## `connection.deleted`

- **Status / introduced:** Implemented / v1
- **Trigger / payload:** Existing `connection-removed` signal;
  `ConnectionSummary` adapted from the removed object.
- **Related IDs/order/delivery:** Same guarantees as `connection.created`.
- **Coalescing / dropping:** Not coalesced or buffered.

<!-- api-event: connection_store.changed -->
## `connection_store.changed`

- **Status / introduced:** Schema-only / API 0.16. Published by the daemon
  after each committed connection-store mutation in a later task.
- **Trigger / payload:** One committed daemon connection-store mutation;
  exact `ConnectionStoreSnapshot` payload. No envelope fields.
- **Related IDs/order/delivery:** Emitted after the compatibility
  `connection.created` / `connection.updated` / `connection.deleted` events
  for the same mutation, in the shared daemon sequence.
- **Coalescing / dropping:** One snapshot per committed mutation; a pure
  group or metadata change publishes only this event.

<!-- api-event: session.created -->
## `session.created`

- **Status / introduced:** Daemon implemented / v1, API 0.6.
- **Capability / trigger:** `sessions.events`; allocation of one durable
  daemon-lifetime session record.
- **Payload / IDs:** `SessionSummary`; session and stable connection IDs are
  populated.
- **Guarantees:** Exactly once per accepted open, before later state events.

<!-- api-event: session.state_changed -->
## `session.state_changed`

- **Status / introduced:** Daemon implemented / v1, API 0.6.
- **Capability / trigger:** `sessions.events`; entry into `starting`,
  `running`, `closing`, or `failed`.
- **Payload / IDs:** Immutable `SessionSummary` and session/connection IDs.
- **Guarantees:** One event per accepted transition, in the shared daemon
  sequence. Attachment count changes do not emit lifecycle events.

<!-- api-event: session.output -->
## `session.output`

- **Status / introduced:** Legacy schema only / v1.
- **Capability / intended trigger:** None. Runtime PTY output deliberately does
  not use `CoreEvent`.
- **Payload / IDs:** Terminal subscriptions receive `TerminalOutput` DTOs from
  `binary-terminal-v2` frames.
- **Guarantees:** See
  [terminal streaming](../architecture/terminal-streaming.md) for sequencing,
  replay, backpressure, and continuity rules.

<!-- api-event: interaction.created -->
## `interaction.created`

- **Status / introduced:** Daemon implemented / v1, API 0.9.
- **Capability / trigger:** `interactions.events`; the broker accepts a typed
  host-key, password, or private-key-passphrase interaction.
- **Payload / IDs:** `InteractionSummary` with safe typed prompt metadata,
  session ID, stable connection ID, state, attempt, and deadline.
- **Guarantees / security:** Only eligible session clients receive the event.
  Raw askpass text, responder nonce, backend keys, and secrets are absent.

<!-- api-event: interaction.state_changed -->
## `interaction.state_changed`

- **Status / introduced:** Daemon implemented / v1, API 0.9.
- **Capability / trigger:** `interactions.events`; a claim, release, accepted
  answer, cancellation, expiry, or failure.
- **Payload / IDs:** A fresh immutable `InteractionSummary`.
- **Guarantees / security:** Exactly one final state wins. Interaction events
  share the daemon-global sequence and bounded event queue; secret bytes use
  neither events nor replay.

<!-- api-event: sftp.created -->
## `sftp.created`

- **Status / introduced:** Daemon implemented / v1, API 0.10.
- **Capability / trigger:** `sftp.events`; allocation of one SFTP service.
- **Payload / IDs:** `SftpServiceSummary` with service and connection IDs.

<!-- api-event: sftp.state_changed -->
## `sftp.state_changed`

- **Status / introduced:** Daemon implemented / v1, API 0.10.
- **Capability / trigger:** `sftp.events`; entry into `starting`, `ready`, or
  `closing`.
- **Payload / IDs:** Immutable `SftpServiceSummary`.

<!-- api-event: sftp.closed -->
## `sftp.closed`

- **Status / introduced:** Daemon implemented / v1, API 0.10.
- **Capability / trigger:** `sftp.events`; final `closed` transition.
- **Payload / IDs:** Final `SftpServiceSummary`.

<!-- api-event: sftp.failed -->
## `sftp.failed`

- **Status / introduced:** Daemon implemented / v1, API 0.10.
- **Capability / trigger:** `sftp.events`; service failure with safe failure
  metadata.
- **Payload / IDs:** `SftpServiceSummary` including optional `SftpFailure`.

<!-- api-event: transfer.created -->
## `transfer.created`

- **Status / introduced:** Daemon implemented / v1, API 0.10.
- **Capability / trigger:** `transfers.events`; transfer record allocation.
- **Payload / IDs:** `TransferSummary`.

<!-- api-event: transfer.started -->
## `transfer.started`

- **Status / introduced:** Daemon implemented / v1, API 0.10.
- **Capability / trigger:** `transfers.events`; bytes begin moving.
- **Payload / IDs:** `TransferSummary`.

<!-- api-event: transfer.progress -->
## `transfer.progress`

- **Status / introduced:** Daemon implemented / v1, API 0.10.
- **Capability / trigger:** `transfers.events`; bounded progress snapshot.
- **Payload / IDs:** `TransferSummary` with completed/total byte counts.

<!-- api-event: transfer.item_completed -->
## `transfer.item_completed`

- **Status / introduced:** Daemon implemented / v1, API 0.10.
- **Capability / trigger:** `transfers.events`; one recursive item finished.
- **Payload / IDs:** `TransferSummary`.

<!-- api-event: transfer.completed -->
## `transfer.completed`

- **Status / introduced:** Daemon implemented / v1, API 0.10.
- **Capability / trigger:** `transfers.events`; successful terminal state.
- **Payload / IDs:** Final successful `TransferSummary`.

<!-- api-event: transfer.cancelled -->
## `transfer.cancelled`

- **Status / introduced:** Daemon implemented / v1, API 0.10.
- **Capability / trigger:** `transfers.events`; cancelled terminal state.
- **Payload / IDs:** Final cancelled `TransferSummary`.

<!-- api-event: transfer.failed -->
## `transfer.failed`

- **Status / introduced:** Daemon implemented / v1, API 0.10.
- **Capability / trigger:** `transfers.events`; failed terminal state.
- **Payload / IDs:** `TransferSummary` including optional `SftpFailure` for the
  SFTP backend or the unchanged `ServiceFailure` for native SCP.

<!-- api-event: forward.created -->
## `forward.created`

- **Status / introduced:** Daemon implemented / v1, API 0.10.
- **Capability / trigger:** `forwards.events`; forward record allocation.
- **Payload / IDs:** `ForwardSummary`.

<!-- api-event: forward.starting -->
## `forward.starting`

- **Status / introduced:** Daemon implemented / v1, API 0.10.
- **Capability / trigger:** `forwards.events`; bind/startup begins.
- **Payload / IDs:** `ForwardSummary`.

<!-- api-event: forward.active -->
## `forward.active`

- **Status / introduced:** Daemon implemented / v1, API 0.10.
- **Capability / trigger:** `forwards.events`; forward is listening/active.
- **Payload / IDs:** `ForwardSummary`.

<!-- api-event: forward.closed -->
## `forward.closed`

- **Status / introduced:** Daemon implemented / v1, API 0.10.
- **Capability / trigger:** `forwards.events`; forward closed.
- **Payload / IDs:** Final `ForwardSummary`.

<!-- api-event: forward.failed -->
## `forward.failed`

- **Status / introduced:** Daemon implemented / v1, API 0.10.
- **Capability / trigger:** `forwards.events`; forward failed.
- **Payload / IDs:** `ForwardSummary` including optional `ServiceFailure`.

<!-- api-event: daemon.state_changed -->
## `daemon.state_changed`

- **Status / introduced:** Daemon implemented / v1, API 0.11.
- **Capability / trigger:** `daemon.events`; lifecycle state transition.
- **Payload / IDs:** `DaemonStatus` (no secrets, paths, or terminal data).

<!-- api-event: session.interaction_requested -->
## `session.interaction_requested`

- **Status / introduced:** Legacy schema only / v1.
- **Capability / trigger:** None. The runtime never emits this broad event.
- **Payload / IDs:** Historical `InteractionRequest` schema only.
- **Guarantees / security:** Typed interaction events replace this vocabulary;
  clients must not infer runtime support from the model.

<!-- api-event: session.exited -->
## `session.exited`

- **Status / introduced:** Daemon implemented / v1, API 0.6.
- **Capability / trigger:** `sessions.events`; an exact owned resource exits.
- **Payload / IDs:** Typed `SessionExitInfo`; the envelope reconstructs the
  public `CoreEvent.session_id`.
- **Guarantees:** Emitted once before `session.closed`. Exit code and signal are
  deliberate fields; no command, environment, process path, or exception is
  exposed.

<!-- api-event: session.closed -->
## `session.closed`

- **Status / introduced:** Daemon implemented / v1, API 0.6.
- **Capability / trigger:** `sessions.events`; entry into final `closed`.
- **Payload / IDs:** Final `SessionSummary` and IDs.
- **Guarantees:** Emitted once after exit when a process existed, or directly
  after close for a failed/resource-free record. Closed records are retained
  in memory up to the documented count; events are never replayed.

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

`DaemonClient.close()` first rejects requests and event acceptance, closes the
socket to wake the sole reader, wakes pending requests, stops the event handoff,
and closes subscriptions. Close from a subscriber callback skips self-join and
does not deadlock. The daemon stops core acceptance and unsubscribes before it
closes peers and the core client.

Callbacks must marshal to their frontend thread when the dispatcher thread is
unsuitable. The API does not import GTK or implicitly call `GLib.idle_add`.

Daemon-authoritative external reload uses the existing connection events.
Diffs are keyed by stable connection ID: rename is `connection.updated`, while
no-op/self-write reloads emit nothing. A committed batch is accepted in
deterministic deleted, created, updated order and then joins the daemon-global
event sequence.

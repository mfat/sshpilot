# State models

The public state domains are deliberately separate. None of the schema-only
diagrams below asserts that current GTK terminal state has moved into the API.

<!-- api-state: ConnectionHealth -->
## Connection health: `ConnectionHealth`

| State | Meaning | Runtime status |
| --- | --- | --- |
| `unknown` | No frontend-neutral health result exists | Implemented output; all current DTOs use it |
| `checking` | A future health check is in progress | Schema only |
| `reachable` | A future check found the host reachable | Schema only |
| `unreachable` | A future check could not reach the host | Schema only |

There is no runtime health monitor, transition engine, persistence, event, or
reconnection behaviour. `InProcessClient` intentionally does not map legacy
terminal-derived `ConnectionState` into health. A future monitor may define
`connection.health_changed`; that event does not exist today.

<!-- api-state: SessionState -->
## Runtime session lifecycle: `SessionState`

The daemon `SessionRuntime` enforces this lifecycle. Records are in-memory and
survive frontend disconnect, but are not persisted across daemon restart.

```mermaid
stateDiagram-v2
    [*] --> created
    created --> starting
    created --> failed
    created --> closed
    starting --> running
    starting --> closing
    starting --> exited
    starting --> failed
    running --> closing
    running --> exited
    running --> failed
    closing --> exited
    closing --> failed
    closing --> closed
    exited --> closed
    failed --> closed
```

The table is normative and contract-tested. `closed` is final; no transition
returns to `running`. Invalid internal transitions are programming errors.
Frontend requests against absent/final records use structured public errors.
Repeated close is idempotent. `session.created`, `session.state_changed`,
`session.exited`, and `session.closed` each represent at most one accepted
transition. Process exit information is separate from safe startup failure
metadata. There is no reconnect, replay, prompt, PTY, or terminal-byte state.

<!-- api-state: InteractionStatus -->
## Legacy interaction schema: `InteractionStatus`

The legacy broad request/response models remain schema-only. Runtime Phase 8
uses the narrower `InteractionState` machine below.

```mermaid
stateDiagram-v2
    [*] --> pending
    pending --> answered
    pending --> cancelled
    pending --> timed_out
    pending --> rejected
```

`answered`, `cancelled`, `timed_out`, and `rejected` are intended terminal.

<!-- api-state: InteractionState -->
## Runtime typed interaction lifecycle: `InteractionState`

```mermaid
stateDiagram-v2
    [*] --> pending
    pending --> claimed
    pending --> answered
    pending --> cancelled
    pending --> expired
    pending --> failed
    claimed --> pending: release/disconnect
    claimed --> answered
    claimed --> cancelled
    claimed --> expired
    claimed --> failed
```

`answered`, `cancelled`, `expired`, and `failed` are final. The daemon broker
serializes claim/response/timeout/cancel races; exactly one result wins. A
responder disconnect before answer releases the claim. Session close, process
exit, and daemon shutdown cancel linked active interactions. Completed safe
metadata is retained under a bounded count; secrets are never retained in the
public state.

<!-- api-state: TransferState -->
## Transfer lifecycle: `TransferState`

The daemon `TransferRuntime` uses `queued`, `starting`, `running`, `paused`,
`cancelling`, `cancelled`, `completed`, and `failed`. `completed`, `failed`,
and `cancelled` are terminal. Binary streaming mode remains unimplemented;
Phase 10 transfers use daemon-path local mode only.

```mermaid
stateDiagram-v2
    [*] --> queued
    queued --> starting
    starting --> running
    starting --> failed
    running --> paused
    paused --> running
    running --> cancelling
    paused --> cancelling
    queued --> cancelled
    cancelling --> cancelled
    running --> completed
    running --> failed
    cancelling --> failed
```

<!-- api-state: ForwardState -->
## Port-forward lifecycle: `ForwardState`

The daemon `ForwardRuntime` uses `created`, `starting`, `active`, `closing`,
`closed`, and `failed`. Legacy schema values `stopping`/`stopped` remain in the
enum for older readers but are not emitted by the Phase 10 runtime.
`closed` and `failed` are terminal for practical clients; failed records may
still be closed for cleanup.

```mermaid
stateDiagram-v2
    [*] --> created
    created --> starting
    created --> closed
    starting --> active
    starting --> failed
    active --> closing
    closing --> closed
    failed --> closed
```

## Duplicate state concepts

The legacy `connection_manager.ConnectionState` values
`unknown/connecting/connected/disconnected/failed` describe aggregate terminal
lifecycle in current GTK code. They are not `ConnectionHealth` and are not the
Protocol v1 `SessionState`. Likewise, the current `SessionManager` persists tab
layouts, not runtime sessions. These naming overlaps are migration debt and
must not be collapsed by adapters.

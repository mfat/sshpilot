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

All states and transitions are schema design only. No API runtime session
exists, no transitions are enforced, and no state is persisted.

```mermaid
stateDiagram-v2
    [*] --> creating
    creating --> connecting
    connecting --> waiting_for_interaction
    waiting_for_interaction --> connecting
    connecting --> connected
    connecting --> failed
    connected --> reconnecting
    reconnecting --> connected
    reconnecting --> disconnected
    connected --> disconnected
    creating --> closing
    connecting --> closing
    connected --> closing
    disconnected --> closing
    failed --> closing
    closing --> closed
```

The diagram records the intended vocabulary only; it is not yet a normative
transition table. Before runtime support, valid transitions, reconnect policy,
exit/close ordering, attachment ownership, persistence, and invalid-transition
errors must be made contract-tested. `closed` is intended terminal.

Related schema events are `session.created`, `session.state_changed`,
`session.exited`, and `session.closed`; none is emitted.

<!-- api-state: InteractionStatus -->
## Interaction lifecycle: `InteractionStatus`

Construction validation currently enforces only:

- `InteractionRequest` starts as `pending`.
- `InteractionResponse` cannot be `pending`.
- A response that is not `answered` cannot include answer data.

```mermaid
stateDiagram-v2
    [*] --> pending
    pending --> answered
    pending --> cancelled
    pending --> timed_out
    pending --> rejected
```

`answered`, `cancelled`, `timed_out`, and `rejected` are intended terminal.
There is no runtime store, ownership, timeout scheduler, persistence, duplicate
answer detection, or event delivery. The
`interaction_already_answered`/`interaction_not_found` codes are schema only.

<!-- api-state: TransferState -->
## Transfer lifecycle: `TransferState`

The enum contains `queued`, `running`, `paused`, `completed`, `failed`, and
`cancelled`. All are schema only. The model does not enforce transitions.
`completed`, `failed`, and `cancelled` are intended terminal; pause/resume,
retry, persistence, and progress-event behaviour are undefined.

```mermaid
stateDiagram-v2
    [*] --> queued
    queued --> running
    running --> paused
    paused --> running
    running --> completed
    running --> failed
    queued --> cancelled
    running --> cancelled
    paused --> cancelled
```

This is future design guidance, not current runtime behaviour.

<!-- api-state: ForwardState -->
## Port-forward lifecycle: `ForwardState`

The enum contains `starting`, `active`, `failed`, `stopping`, and `stopped`.
All are schema only and no method or event exposes them.

```mermaid
stateDiagram-v2
    [*] --> starting
    starting --> active
    starting --> failed
    active --> stopping
    stopping --> stopped
    failed --> stopped
```

This intended shape is non-normative until a core owner and contract tests
exist. `stopped` is intended terminal.

## Duplicate state concepts

The legacy `connection_manager.ConnectionState` values
`unknown/connecting/connected/disconnected/failed` describe aggregate terminal
lifecycle in current GTK code. They are not `ConnectionHealth` and are not the
Protocol v1 `SessionState`. Likewise, the current `SessionManager` persists tab
layouts, not runtime sessions. These naming overlaps are migration debt and
must not be collapsed by adapters.

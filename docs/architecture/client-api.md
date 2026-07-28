# SshPilotClient API

`SshPilotClient` is the stable frontend/core seam for sshPilot. GTK currently
uses `InProcessClient`; a future `DaemonClient` will implement the same
connection contract over local IPC.

## Package layout

```text
sshpilot/api/
  version.py
  capabilities.py
  errors.py
  events.py
  client.py
  in_process_client.py
  models/
    common.py
    connections.py
    sessions.py
    terminal.py
    interactions.py
    transfers.py
    operations.py
```

All files except `in_process_client.py` are implementation-neutral.
`in_process_client.py` accepts existing managers by dependency injection and
does not import GTK, GObject, VTE or a transport.

## Why the boundary exists

Frontend code should not need to know how connections are stored, which secret
backend is selected, how native SSH arguments are built, which process owns a
PTY, or how a future daemon is reached. Core code should not know how a dialog,
tab, toast or terminal renderer is presented.

This phase proves the boundary with one narrow path:

```text
WelcomePage._populate_recent_box
    -> client.list_connections()
    -> secret-free ConnectionSummary DTOs
```

When a Recent row is clicked, GTK temporarily resolves the nickname to the
legacy `Connection` object and enters the unchanged terminal path. That direct
manager action is explicit migration debt; terminal execution was intentionally
not moved.

## Calling convention

The initial contract is synchronous commands plus event subscription. This
matches the current GTK/GLib runtime, where blocking work already uses worker
threads and UI results return through GLib.

`InProcessClient` command methods must run on the thread that constructed the
client. In GTK that is the main thread. Cross-thread command attempts return a
structured `invalid_request`. Event registration is thread-safe, while event
delivery happens on the underlying signal's source thread.

Do not:

- create an asyncio loop per method;
- call `asyncio.run()` from GTK callbacks;
- block GTK waiting for futures;
- make the Python calling convention mimic a not-yet-designed wire transport.

A future `DaemonClient` can have an async internal implementation while still
offering a GTK-safe façade.

## Versioning and compatibility

- `PROTOCOL_VERSION` versions public semantics and DTO compatibility.
- `API_IMPLEMENTATION_VERSION` versions this Python implementation.
- `ClientInfo`, `CoreInfo`, and `CompatibilityResult` describe both endpoints.
- Additive optional fields can remain within a compatible protocol version.
- Removing fields, changing meanings, or changing byte/order/error semantics
  requires an explicit compatibility decision and normally a protocol version
  change.

## Capabilities

Capability identifiers are stable strings. A capability is advertised only
when its operations are reachable through the client and pass reusable contract
tests.

The current `InProcessClient` advertises only:

```text
connections.read
```

It does not advertise connection writes, terminal, attach, replay,
interactions, SFTP, forwarding, plugins or secrets. Those schemas exist to
stabilize vocabulary, not to claim a runtime.

Unsupported methods raise:

```text
code: unsupported_capability
details.capability: <stable capability string>
```

## Connection DTOs and IDs

`ConnectionSummary` is the list shape. `ConnectionDetails` adds deliberate safe
configuration metadata. Neither is an internal `Connection`, a persistence
dictionary, a manager, or a GObject.

Ordinary responses never contain:

- passwords or key passphrases;
- private key contents;
- secret backend tokens;
- source config paths;
- environment variables;
- subprocess, PTY, provider, GTK or VTE objects.

sshPilot persistence currently has no immutable connection UUID. Protocol v1
therefore computes an opaque ID from protocol plus nickname. It is stable across
reloads while those values are unchanged, but changes on rename. A later
persistence migration should add immutable UUIDs and document alias resolution
during transition.

`ConnectionHealth` is separate from `SessionState`. The current
terminal-derived `ConnectionState` is not converted into reachability;
`InProcessClient` reports connection health as `unknown`.

## Errors

`SshPilotError` carries:

- stable `ErrorCode`;
- human-readable message;
- safe details;
- retryable flag;
- optional request, connection and session IDs.

Frontends must switch on error codes, not parse exception strings. Raw
tracebacks and internal exceptions stay in developer logs. Error details must
not include passwords, tokens, key material, full environments, sensitive
arguments or secret paths.

When adding an error code:

1. add a stable lowercase identifier;
2. document its retry and correlation semantics;
3. translate known manager failures at the adapter;
4. add contract tests for its public envelope and redaction.

## Events and subscriptions

`CoreEvent` includes type, UTC timestamp, process-local sequence, typed payload,
and optional request/connection/session correlation.

Current runtime events:

- `connection.created`
- `connection.updated`
- `connection.deleted`

Schema-only event types include session creation/state/output/interaction/exit/
close and core errors.

Subscribers receive current in-process events in registration order. Subscriber
failure is isolated. `Subscription.unsubscribe()` and `close()` are idempotent.
The client disconnects manager signal handlers during shutdown.

Terminal output must not use this simple synchronous publisher. Before terminal
runtime support, add bounded per-session queues, batching, per-session sequence
ordering, replay bounds, truncation, and slow-client policy.

## Terminal and interaction rules

- Terminal input, output and replay data are `bytes`.
- Do not decode arbitrary PTY output as UTF-8.
- Rows/columns must be positive and bounded.
- Public models never expose PTY descriptors or subprocess objects.
- Interaction responses hide secret values from `repr`.
- Secret answers must never enter event histories, error details or logs.
- Completed/cancelled/timed-out interactions cannot be represented as pending.

## Adding a client method

1. Decide whether the operation is core-owned.
2. Add a deliberate typed request and response; do not expose an internal
   record or generic dictionary.
3. Define capability and structured-error behaviour.
4. Specify thread ownership, cancellation and shutdown.
5. Implement the smallest adapter over existing business logic.
6. Add reusable contract tests.
7. Advertise the capability only after the implementation passes those tests.
8. Migrate one frontend caller without rewriting adjacent subsystems.

## Adding an event

1. Add a stable event identifier and typed payload.
2. Define ordering scope and correlation IDs.
3. Define emitting thread and GTK bridge.
4. Define subscriber cleanup, failure isolation and shutdown.
5. Define durability/loss/slow-consumer behaviour.
6. Test ordering, cleanup, payload type and failure isolation.

GObject signals may feed an adapter, but they are not the cross-frontend
contract.

## Frontend and core access rules

Frontend code must not directly access persistence or secret backends. During
incremental migration, every remaining direct call should be treated as
explicit debt and moved one vertical slice at a time.

Core/API modules must not import or return GTK, GObject, Adwaita, VTE, WebKit,
frontend controllers, frontend callbacks, raw PTY descriptors or subprocess
objects.

## Future transports

The same models and contract are intended for:

- the current in-process adapter;
- Unix-domain sockets;
- Windows named pipes;
- a local WebSocket only if a later frontend requires it.

No transport is implemented here. A future transport needs a versioned message
envelope, protocol negotiation, binary terminal frames, request cancellation,
bounded event queues, authentication/permissions for the per-user endpoint,
stale-daemon recovery, and reusable contract tests against `DaemonClient`.

See `core-boundary-audit.md` for the current concurrency/state evidence and
`daemon-ownership.md` for ownership decisions and the next-phase backlog.

The concrete, maintained contract is indexed in
[`docs/api/README.md`](../api/README.md). Use the architecture documents for
rationale and the API reference for exact methods, models, capabilities,
events, errors, state semantics, and compatibility rules.

# SshPilotClient API

> **Historical migration record.** This document describes an earlier phase and
> names components/settings as they existed then. It is not the current runtime
> contract; production GTK now requires the daemon and has no local SSH fallback.


`SshPilotClient` is the stable frontend/core seam for sshPilot. GTK uses
`InProcessClient` by default and can explicitly opt into `DaemonClient` for
connection-read development through `SSHPILOT_CLIENT_MODE=daemon`.
The experimental daemon slice covers connection CRUD/events, session control,
PTY terminal streaming, and typed authentication/trust interactions.

## Package layout

```text
sshpilot/api/
  version.py
  capabilities.py
  errors.py
  events.py
  client.py
  client_factory.py
  in_process_client.py
  daemon_client.py
  transport/
  models/
    common.py
    connections.py
    sessions.py
    terminal.py
    interactions.py
    transfers.py
    operations.py
```

All model, contract, and envelope files are implementation-neutral.
`in_process_client.py` accepts existing managers by dependency injection and
does not import GTK, GObject, VTE or a transport. `daemon_client.py` depends on
Unix sockets but not frontend or manager types.

## Why the boundary exists

Frontend code should not need to know how connections are stored, which secret
backend is selected, how native SSH arguments are built, which process owns a
PTY, or how a future daemon is reached. Core code should not know how a dialog,
tab, toast or terminal renderer is presented.

The boundary is exercised by connection reads and basic mutations:

```text
WelcomePage._populate_recent_box
    -> client.list_connections()
    -> secret-free ConnectionSummary DTOs

Connection editor/delete actions in daemon mode
    -> GtkClientBridge
    -> client.create/update/delete_connection()
    -> existing ConnectionManager through the daemon
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
delivery runs through the first active publisher thread's serial FIFO
dispatcher. Concurrent publisher threads wait for their accepted event; GTK
subscribers must still marshal when the dispatcher is not the main thread.

Do not:

- create an asyncio loop per method;
- call `asyncio.run()` from GTK callbacks;
- block GTK waiting for futures;
- make the Python calling convention mimic a not-yet-designed wire transport.

`DaemonClient` uses one persistent blocking socket, one concurrency gate, one
send lock, one persistent reader thread, and a finite timeout. Callers register
pending request IDs and never read the socket. A separate bounded serial event
handoff prevents slow subscribers from blocking response correlation. On
timeout the client logs payload-free diagnostics (ids, method, elapsed time,
queue depths, thread liveness) and closes the transport. It creates no event
loop or thread per call. In daemon mode, the application-scoped GTK bridge uses
one bounded worker and returns snapshots and mutation results through
`GLib.idle_add`. Request tokens suppress stale results after refresh/window
destruction. GLib remains outside `DaemonClient`, and `InProcessClient` stays
on its construction/main thread. Session restore listing also runs on the
bridge worker so it cannot stall GTK behind welcome connection reads.

Inside the daemon, `sessions.open` and `sessions.close` are deferred through a
four-worker, 64-command keyed executor. `sessions.open` acknowledges the
accepted `starting` session immediately when the executor admits the startup
command; PTY/OpenSSH work and authentication interactions proceed in the
background and never hold the five-second control RPC deadline. The selector
reserves deferred close responses and drains a bounded completion queue;
workers never write sockets. Commands for one session serialize, while slow
work for one session does not delay handshake, reads, attachments, or commands
for other sessions. Connection mutations remain synchronous persistence
operations in API 0.9.

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

Both current client implementations advertise:

```text
connections.read
connections.events
connections.write
```

The daemon additionally advertises:

```text
sessions.read
sessions.write
sessions.events
terminal.output
terminal.input
terminal.resize
terminal.replay
interactions.read
interactions.respond
interactions.events
interactions.host_key
interactions.password
interactions.passphrase
```

`InProcessClient` deliberately returns `unsupported_capability` for session,
terminal, and interaction control because current production GTK terminal
ownership has not moved. Daemon terminal capabilities require
`binary-terminal-v1`; responder/secret capabilities require
`binary-secret-v2`. Neither provider advertises SFTP, forwarding, plugins, or
broad secret access.

The write DTO intentionally contains only basic connection metadata. Daemon
GTK mode rejects secret, key/certificate, advanced SSH, group/tag, and
Wake-on-LAN edits rather than silently losing them. It waits for success before
refreshing and never retries an ambiguous mutation automatically.

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

Every persisted connection has an identifier equal to its SSH Host alias.
Protocol v1 emits this alias in all connection DTOs, mutation results, and
events. Rename is deletion of the old alias plus creation of a new alias;
host/user/port/protocol updates preserve the alias.

`ConnectionHealth` is separate from `SessionState`. The current
terminal-derived `ConnectionState` is not converted into reachability;
`InProcessClient` reports connection health as `unknown`.

## Session DTOs and IDs

Daemon sessions use strict opaque `session-<n>` IDs unique for one daemon
process. `SessionSummary` exposes lifecycle state, creation time, safe
exit/failure metadata, empty terminal capabilities, and logical attachment
count. It never exposes a process/PID, command, environment, PTY, secret, or
internal record. Client identity for attach/detach comes from the completed
handshake, not request-controlled metadata. See
[daemon session runtime](session-runtime.md).

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
arguments or secret paths. Runtime validation permits only finite
JSON-compatible scalar/list/dictionary values with string keys and rejects
secret-bearing key names, exceptions, arbitrary objects, environments and
process command lines. Error `repr` excludes details and correlation IDs.

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
- `session.created` (daemon)
- `session.state_changed` (daemon)
- `session.exited` (daemon)
- `session.closed` (daemon)
- `interaction.created` (daemon)
- `interaction.state_changed` (daemon)

Schema-only event types still include session output and the legacy broad
interaction request.
`error.occurred` is a local safe `DaemonClient` continuity notification.

Subscribers receive accepted in-process events in publisher-global sequence
order and subscriber-registration order. The first active publisher drains the
FIFO; concurrent publishers wait and re-entrant events queue without recursive
delivery. Subscriber failure is isolated. `Subscription.unsubscribe()` and
`close()` are idempotent. The client disconnects manager signal handlers during
shutdown.

The daemon forwards the three connection, four session, and two interaction
lifecycle events. It
assigns one daemon-global sequence starting at zero, uses explicit typed
codecs, and keeps bounded per-peer queues. An overflowed peer is disconnected
instead of continuing with a silent gap. `DaemonClient` checks sequence
continuity and publishes decoded events through the same subscription API on
its event dispatcher thread.

GTK owns one application-scoped daemon subscription. It marshals event types to
the main context and coalesces bursts into one asynchronous
`list_connections()` refresh, with at most one follow-up if events arrive while
a refresh is active. Transport/continuity failure replaces live cached state
with the existing safe unavailable view; no automatic reconnect loop runs.

Terminal output and secret responses never use this control-plane publisher.
They use separately negotiated binary frame types and bounded ownership paths.

## Terminal and interaction rules

- Terminal input, output and replay data are `bytes`.
- Do not decode arbitrary PTY output as UTF-8.
- Rows/columns must be positive and bounded.
- Public models never expose PTY descriptors or subprocess objects.
- Interaction decisions contain no secret values. A claimed responder sends a
  secret only through one-use `binary-secret-v2`.
- Secret answers must never enter event histories, error details or logs.
- Answered/cancelled/expired/failed interactions are final.

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

## Phase 9: GTK Terminal Migration

Phase 9 introduces production daemon-backed SSH terminals with VTE emulation:

- **Production path**: GTK → DaemonClient.sessions.open → PTY → attach → VTE feed
- **Multi-attachment**: Multiple GTK tabs can attach to same daemon session
- **Input ownership**: Exclusive input ownership with claim/release API
- **Session persistence**: Sessions survive GTK restart with detach/reattach
- **VTE emulation**: Unified VTE-based production emulator for daemon SSH
- **Internal SSH**: Daemon-only; readiness failure never starts a frontend SSH
  process
- **Route model**: `SshTerminalRoute` (`daemon` / `external`)
  resolved before readiness or secret unlock

See [GTK Terminal Migration](gtk-terminal-migration.md) and [Session Reattachment](session-reattachment.md).

## Adding an event

1. Add a stable event identifier and typed payload.
2. Define ordering scope and correlation IDs.
3. Define emitting thread and GTK bridge.
4. Define subscriber cleanup, failure isolation and shutdown.
5. Define durability/loss/slow-consumer behaviour.
6. Test ordering, cleanup, payload type and failure isolation.

GObject signals may feed an adapter, but they are not the cross-frontend
contract.

The in-process publisher provides one global serial FIFO. The first active
publisher drains accepted events; concurrent publishers wait for delivery.
Re-entrant publication queues behind the current event, so callbacks do not
recursively grow the stack. Subscriber snapshots make unsubscription during
delivery deterministic, and close rejects new events while accepted events
finish.

## Frontend and core access rules

Frontend code must not directly access persistence or secret backends. During
incremental migration, every remaining direct call should be treated as
explicit debt and moved one vertical slice at a time.

Core/API modules must not import or return GTK, GObject, Adwaita, VTE, WebKit,
frontend controllers, frontend callbacks, raw PTY descriptors or subprocess
objects.

## Transports

The same models and contract are intended for:

- the current in-process adapter;
- the implemented local Unix-domain socket;
- Windows named pipes;
- a local WebSocket only if a later frontend requires it.

The local daemon implements versioned length-prefixed JSON envelopes, protocol
negotiation, strict local-user socket permissions, stale-socket recovery, and
reusable connection contracts. Future phases still need binary terminal frames,
request cancellation, bounded event queues, reconnect/resume semantics, prompt
routing, and non-Linux transports.

See `core-boundary-audit.md` for the current concurrency/state evidence and
`daemon-ownership.md` for ownership decisions. The implemented transport is
described in [daemon-transport.md](daemon-transport.md).

The concrete, maintained contract is indexed in
[`docs/api/README.md`](../api/README.md). Use the architecture documents for
rationale and the API reference for exact methods, models, capabilities,
events, errors, state semantics, and compatibility rules.

In experimental daemon mode, connection events also cover committed external
configuration reloads. GTK treats them like CRUD events: it coalesces an
asynchronous `connections.list` refresh, preserves selection by stable ID, and
never parses or migrates the local SSH configuration itself.

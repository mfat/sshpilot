# Local daemon transport

The local daemon transport is the production frontend-neutral route for
daemon-owned backend state and remote operations. It provides typed connection,
session, terminal, interaction, SFTP, transfer, forwarding, secret, identity,
and plugin-facing APIs according to negotiated capabilities. Frontends do not
silently fall back to local SSH/SFTP or other backend implementations.

Linux is the supported platform for this phase. Unix-socket primitives may be
present elsewhere, but macOS lifecycle integration and Windows named pipes are
explicitly deferred.

```text
DaemonClient
    -> one persistent AF_UNIX socket
    -> sshpilot.daemon selector loop
    -> explicit RequestDispatcher
       -> daemon services/runtime
       -> owned session, process, transfer, and interaction resources
```

## Ownership and threading

The daemon composition root constructs its services and runtime owners before
serving requests. One selector loop handles multiple client sockets without a
thread per client, a thread per request, `asyncio.run()`, or a per-call event
loop.

The selector thread owns envelope validation, immediate bounded dispatch,
deferred-request reservation, completion draining, and all socket/selector
operations. A daemon-scoped executor has four workers and a hard limit of 64
outstanding session commands. It runs session start and close operations;
equal session IDs serialize while unrelated sessions may progress in parallel.
Workers submit immutable completions to a bounded queue and wake the selector.
Only the selector queues the correlated response. Stable daemon-local peer
tokens prevent late completions from reaching a reused file descriptor.

One runtime lock serializes state/attachment mutations; it is released before
runner start, terminate, kill, wait, event publication, or thread joins. One
shared process reaper reports exits without a thread per event or byte. Event
callbacks are never invoked while runtime or transport locks are held.

`DaemonClient` presents synchronous methods, but exactly one persistent reader
thread owns socket receive. Callers register pending request IDs and serialize
writes; responses and unsolicited events may arrive in either order. Events
move through a separate bounded serial dispatcher, so a slow callback cannot
stop response correlation. Every call has a finite timeout (five seconds by
default); timeout closes the transport, making late responses unambiguous.
Timeout diagnostics log only safe fields (request id, method, elapsed time,
client/server instance ids, socket state, pending/event/terminal queue depths,
reader/event/terminal thread liveness, and whether the frame was sent). Request
payloads, secrets, terminal bytes, and environment values are never logged.

GTK never invokes that blocking API on its main thread. One application-scoped
`GtkClientBridge` owns a single worker. It serializes selection/startup and
connection reads/writes, then posts results through `GLib.idle_add`. The
application owns one event subscription and also marshals it through GLib.
Welcome-page refreshes are coalesced: one pending/active snapshot plus at most
one follow-up. Per-widget request tokens suppress stale delivery after refresh
or destruction. Closing the application unsubscribes before it closes the
selected client and bridge; closing one window only invalidates callbacks.
Daemon session restore also lists sessions through the bridge so a blocked
control RPC cannot stall GTK behind welcome `connections.list`.

## Client selection and startup

The production application uses the daemon client for backend operations.
`SSHPILOT_CLIENT_MODE` is a process-local compatibility/diagnostic override;
an explicit in-process client is limited to its advertised compatibility/test
surface and does not provide a frontend remote-operation backend.

```bash
SSHPILOT_CLIENT_MODE=daemon python3 run.py
SSHPILOT_CLIENT_MODE=in_process python3 run.py   # explicit; wins over Stage C
```

The environment value is not persisted as an application preference. A daemon
selection or handshake failure is reported as a structured recovery state; it
does not silently select a frontend-owned SSH/SFTP backend.

### Stale daemon diagnosis

After updating daemon code, **restart sshpilotd before testing**. GTK may remain
connected to an older process that still holds the runtime socket.

Verify the active daemon safely:

1. Note handshake identity in logs: `version`, `api`, `instance`, `started_at`,
   optional opaque `SSHPILOT_DEV_REVISION`.
2. Compare application `__version__` / `API_IMPLEMENTATION_VERSION` with the
   daemon values (`DaemonClient.build_mismatch()`).
3. Confirm the process command line and `XDG_RUNTIME_DIR/.../sshpilotd.sock`.
4. Stop the old daemon (no active sessions, or after warning), then relaunch
   the app so `DaemonLauncher` starts a fresh process from current sources.

Never auto-kill a daemon that still owns live sessions.

`api/client_factory.py` is the single selection policy. Widgets neither read
the environment nor construct `DaemonClient`. The current application has one
main window and one application-scoped client; future multi-window composition
must continue sharing that client rather than opening one socket per widget.

## On-demand startup and recovery

Daemon selection runs off the GTK thread:

1. validate the owned mode-0700 parent and any existing mode-0600 socket;
2. attempt a real Protocol v1 handshake with a short bounded probe;
3. require negotiated `connections.read`, `connections.events`, and
   `connections.write`
   capabilities;
4. if and only if the endpoint is unavailable, launch
   `sys.executable -m sshpilot.daemon --socket <resolved endpoint>`;
5. poll with a monotonic three-second deadline and repeated real handshakes;
6. use the negotiated daemon client or surface a structured recovery error.

There is no restart loop or backend fallback. Protocol incompatibility,
malformed framing, transport closure during handshake, missing capability, and
unsafe filesystem state produce a stable local failure category. The UI shows:

```text
The local SSH Pilot service could not be used. SSH Pilot is running in
daemon recovery state.
```

Raw exceptions, socket paths, commands, environment values, and protocol data
are never placed in that notification.

The child is launched with an argument list and `shell=False`; standard streams
are detached to `/dev/null`. Known session-secret environment variables are
removed. A source-tree `src` import path is added so the documented module
command works before installation.

## Core construction

`python -m sshpilot.daemon` constructs the daemon application services and
runtime owners. Transport and envelope modules do not import GTK or PyGObject.
Headless tests may inject test services through the same server factory and use
a real Unix socket.

Daemon handlers delegate persistence, DTO mapping, stable identifiers,
mutations, safe errors, and secret exclusion to daemon-owned application
services. No frontend client or compatibility manager is the authoritative
production route.

The server binds and secures its socket before constructing the authoritative
core manager. Core construction migrates persisted connection identities while
holding the per-config migration lock, and readiness is not reported until that
migration succeeds. Only the daemon-owned process performs this authoritative
migration. See
[stable connection identity](connection-identity.md).

## Socket security

The default endpoint is:

1. `$XDG_RUNTIME_DIR/sshpilot/sshpilotd.sock`; or
2. a development fallback under the platform temporary directory at
   `sshpilot-<uid>/sshpilotd.sock`.

The immediate socket directory must be a real directory, owned by the current
UID, with mode `0700`. The socket is created with mode `0600`; ownership, file
type, and permissions are verified after bind. Symlinks and non-socket targets
are refused.

An existing socket is probed. A successful connection means another daemon is
active and startup fails. Only a same-inode socket whose connection is refused
is removed as stale. Shutdown unlinks only the socket inode created by that
server instance. No TCP listener, remote authentication, TLS, or cross-user
access exists.

## Lifecycle

The server owns its listener, wakeup pair, active client sockets, dispatcher,
session command executor/completion queue, session runtime, and core client.
`shutdown()` wakes the selector, rejects new deferred submissions, cancels
queued non-cleanup commands, closes exact owned session resources under a
global bounded deadline, drains worker threads, discards late responses,
closes peers and the core client, and removes its socket.
The module entry point maps SIGINT and SIGTERM to this path. It does not install
or supervise a service.

An already-running daemon is unowned by GTK. An on-demand child is represented
by the exact `Popen` object that created it; no PID lookup or process-name
matching exists. Failed startup terminates only that exact child. A successfully
started daemon is deliberately left running when GTK exits because Protocol v1
cannot prove that no other clients need it. Tests terminate their own exact
child and verify socket cleanup.

The installed `sshpilot-daemon` launcher and optional systemd user unit are
documented in [packaging-daemon.md](../operations/packaging-daemon.md). Socket
activation is intentionally not used; the app launcher remains the primary,
race-hardened activation path.

## Connection event forwarding

The daemon subscribes once to the existing `InProcessClient` event publisher,
which already maps manager signals into typed, secret-free
`ConnectionSummary` payloads. The transport does not subscribe to persistence
or duplicate DTO mapping.

One daemon-global event sequence starts at zero. Assignment, encoding, and
enqueue acceptance are serialized; every eligible peer gets the same immutable
encoded frame and sequence. Restart resets the sequence because no replay or
resume protocol exists. A newly handshaken client must read
`connections.list` for its current snapshot.

Each handshaken peer has a queue bounded to 256 event frames. Core callbacks
never perform socket I/O: they enqueue and wake the selector. The selector adds
write interest only while output exists, handles partial writes, and sends
responses and events as complete non-interleaved frames in deque order.
Handshake-incomplete peers receive no runtime events.

The whole outbound deque is separately bounded to 4 MiB per peer. Responses
and events both count, and accounting tracks remaining bytes after partial
writes. Exceeding either bound disconnects only that peer without logging
payload contents.

If a queue fills, that peer's continuity is marked lost, its queue is cleared,
and the selector disconnects it. No event is silently dropped while the peer
continues. Other peers and the core remain unaffected. `DaemonClient` applies
the same bounded-queue principle between its reader and callback dispatcher.
Malformed payloads, gaps, duplicates, regressions, and local overflow close the
transport and surface a safe local continuity error.

There is no automatic reconnect in Protocol v1. GTK stops trusting cached live
state and shows the safe unavailable state. Client code may use
`DaemonReconnectPolicy` / `DaemonReconnectHelper` for bounded backoff; after
success it must take fresh list snapshots. Resource restoration across restart
is out of scope.

## Session lifecycle foundation

The explicit session methods are `sessions.list`, `sessions.get`,
`sessions.open`, `sessions.attach`, `sessions.detach`, and `sessions.close`.
The daemon advertises `sessions.read`, `sessions.write`, and `sessions.events`.
One daemon-scoped `session-<n>` identifier identifies a record for the daemon lifetime.

Logical attachment uses the handshaken peer ID. Peer disconnect detaches it
from every session without terminating the resource. Open/close transport
ambiguity is never retried automatically. Four typed events share the existing
daemon-global sequence and bounded peer queues with connection events.

`sessions.open` and `sessions.close` are the explicit deferred method class.
`sessions.open` prepares a `starting` record, submits startup on the bounded
keyed executor, and returns that captured `starting` snapshot as soon as
admission succeeds. PTY allocation, OpenSSH launch, host-key/password/
passphrase interaction, and the transition to `running` or `failed` continue
asynchronously and are reported through session lifecycle events and
`sessions.get`/`sessions.list`. A later startup failure is never a second RPC
response for the same open. `sessions.close` enters `closing` before submitting
bounded terminate/wait/kill and responds when that worker step finishes. A full
64-command executor returns retryable `server_busy` without blocking the
selector and without leaving a misleading `starting` summary. Immediate reads,
handshake, attachment bookkeeping, and capability discovery remain on the
selector because they are bounded. Connection persistence mutations remain
synchronous; the selector is therefore hardened against session runner
blocking, not claimed to be free of every possible filesystem delay.

The production runner owns a real Unix PTY and canonical OpenSSH child.
Control-only clients retain safe non-interactive failure. A
`binary-secret-v2` client may use typed host-key/password/passphrase
interactions through the daemon broker. See
[session runtime](session-runtime.md), [terminal streaming](terminal-streaming.md),
and [interaction broker](interaction-broker.md).

## Current boundary

Handshake, capability discovery, connection list/get/create/update/delete, and
`connection.created`/`connection.updated`/`connection.deleted` cross the
daemon. Session control, lifecycle events, negotiated terminal data, and typed
interaction metadata also cross it. The daemon advertises the current
connection, session, terminal, interaction, SFTP, transfer, forwarding,
secret, identity, and plugin capabilities negotiated by the client. The exact
public surface is maintained in the API references, not duplicated here.

Typed write contracts preserve fields they do not own and reject unsupported
requests explicitly; they never silently discard advanced, group, tag, or
secret-bearing data. GTK waits for mutation responses and subsequent coalesced
snapshot refreshes; it never removes or changes rows optimistically.

Write requests are not automatically retried. If the transport closes after a
request may have reached the daemon, `mutation_ambiguous` requires a fresh
snapshot before explicit user action. There is no exactly-once/idempotency-key
contract. SFTP, forwarding, plugin, secret, and interaction operations use
their negotiated daemon capabilities; remote multi-user transport remains out
of scope.

## Remaining transport and lifecycle scope

- define launchd lifecycle integration for macOS;
- add Windows named-pipe transport and ownership checks;
- remove deprecated transitional-ID lookup in Protocol v2;
- wire GTK preferences for daemon status/restart controls;
- extend platform-specific lifecycle integration where supported;
- preserve the single daemon ownership route as new transports are added.

## External configuration synchronization

The selector does not parse or persist configuration. A headless watcher marks
the configuration dirty, a 200 ms coordinator debounce coalesces bursts, and
the bounded daemon executor runs reload under the same keyed lane used by
connection mutations. Handshakes, socket I/O, events, and unrelated session
command lanes remain responsive. See
[configuration reload](configuration-reload.md) for watch-set, rollback,
self-write, and shutdown rules.

# Local daemon transport

The current experimental path provides Linux per-user connection CRUD/events
plus daemon-lifetime session lifecycle control/events. Production composition
remains in-process by default, and normal GTK terminals remain on their legacy
in-process path.

Linux is the supported platform for this phase. Unix-socket primitives may be
present elsewhere, but macOS lifecycle integration and Windows named pipes are
explicitly deferred.

```text
DaemonClient
    -> one persistent AF_UNIX socket
    -> sshpilot.daemon selector loop
    -> explicit RequestDispatcher
       -> InProcessClient -> existing ConnectionManager
       -> SessionRuntime -> owned session records/process-runner boundary
```

## Ownership and threading

`DaemonServer` constructs its injected core client on the same thread that runs
the selector and dispatches requests. This preserves `InProcessClient` and
GObject manager owner-thread rules. One selector loop handles multiple client
sockets without a thread per client, a thread per request, `asyncio.run()`, or a
per-call event loop.

The selector thread also owns `SessionRuntime` commands. One runtime lock
serializes state/attachment mutations; one shared process reaper reports exits
without a thread per event or byte. Event callbacks are never invoked while
runtime or transport locks are held.

`DaemonClient` presents synchronous methods, but exactly one persistent reader
thread owns socket receive. Callers register pending request IDs and serialize
writes; responses and unsolicited events may arrive in either order. Events
move through a separate bounded serial dispatcher, so a slow callback cannot
stop response correlation. Every call has a finite timeout (five seconds by
default); timeout closes the transport, making late responses unambiguous.

GTK never invokes that blocking API on its main thread. One application-scoped
`GtkClientBridge` owns a single worker. It serializes selection/startup and
connection reads/writes, then posts results through `GLib.idle_add`. The
application owns one event subscription and also marshals it through GLib.
Welcome-page refreshes are coalesced: one pending/active snapshot plus at most
one follow-up. Per-widget request tokens suppress stale delivery after refresh
or destruction. Closing the application unsubscribes before it closes the
selected client and bridge; closing one window only invalidates callbacks.

## Experimental GTK selection

Normal startup still selects `InProcessClient`. Development daemon mode is
enabled only for the current process:

```bash
SSHPILOT_CLIENT_MODE=daemon python3 run.py
```

The parser ignores surrounding whitespace and case. Missing or blank values
mean `in_process`; invalid values retain in-process mode and produce the same
safe compatibility warning as a failed daemon selection. The environment value
is not persisted as an application preference.

`api/client_factory.py` is the single selection policy. Widgets neither read
the environment nor construct `DaemonClient`. The current application has one
main window and one application-scoped client; future multi-window composition
must continue sharing that client rather than opening one socket per widget.

## On-demand startup and fallback

When daemon mode is requested, selection runs off the GTK thread:

1. validate the owned mode-0700 parent and any existing mode-0600 socket;
2. attempt a real Protocol v1 handshake with a short bounded probe;
3. require negotiated `connections.read`, `connections.events`, and
   `connections.write`
   capabilities;
4. if and only if the endpoint is unavailable, launch
   `sys.executable -m sshpilot.daemon --socket <resolved endpoint>`;
5. poll with a monotonic three-second deadline and repeated real handshakes;
6. use the daemon client, or fall back once to the existing in-process client.

There is no restart loop. Protocol incompatibility, malformed framing,
transport closure during handshake, missing capability, and unsafe filesystem
state do not trigger a daemon launch or retry. They still fall back for this
experimental application run so the established local core remains usable.
Logs record only a stable local failure category. The UI shows:

```text
The local SSH Pilot service could not be used. SSH Pilot is running in
compatibility mode.
```

Raw exceptions, socket paths, commands, environment values, and protocol data
are never placed in that notification.

The child is launched with an argument list and `shell=False`; standard streams
are detached to `/dev/null`. Known session-secret environment variables are
removed. A source-tree `src` import path is added so the documented module
command works before installation.

## Core construction

`python -m sshpilot.daemon` lazily constructs `Config`, `ConnectionManager`,
`GroupManager`, and one `InProcessClient`. Transport and envelope modules do not
import those managers or PyGObject. Tests inject a headless manager through the
same server factory and use a real Unix socket.

Daemon handlers never read persistence directly. Connection ordering, DTO
mapping, stable identifiers, mutations, safe errors, and secret exclusion
continue to come from `InProcessClient`, which delegates writes to the existing
`ConnectionManager`.

The server binds and secures its socket before constructing the authoritative
core manager. Core construction migrates persisted connection identities while
holding the per-config migration lock, and readiness is not reported until that
migration succeeds. Experimental daemon-mode GTK disables migration in its
local compatibility manager until selection completes, so only the selected
authoritative process migrates. See
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
session runtime, and core client. `shutdown()` wakes the selector, stops new
work/events, closes exact owned session resources under a global bounded
deadline, closes peers and the core client on its owner thread, and removes its
socket.
The module entry point maps SIGINT and SIGTERM to this path. It does not install
or supervise a service.

An already-running daemon is unowned by GTK. An on-demand child is represented
by the exact `Popen` object that created it; no PID lookup or process-name
matching exists. Failed startup terminates only that exact child. A successfully
started daemon is deliberately left running when GTK exits because Protocol v1
cannot prove that no other clients need it. Tests terminate their own exact
child and verify socket cleanup. An installed `sshpilotd` launcher and bounded
idle exit remain packaging/lifecycle work.

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

There is no automatic reconnect. GTK stops trusting cached live state and shows
the safe unavailable state. Restarting the application re-runs daemon
selection; a future explicit reconnect must take a fresh list snapshot.

## Session lifecycle foundation

The explicit session methods are `sessions.list`, `sessions.get`,
`sessions.open`, `sessions.attach`, `sessions.detach`, and `sessions.close`.
The daemon advertises `sessions.read`, `sessions.write`, and `sessions.events`.
One random `session:<uuid>` identifies a record for the daemon lifetime.

Logical attachment uses the handshaken peer ID. Peer disconnect detaches it
from every session without terminating the resource. Open/close transport
ambiguity is never retried automatically. Four typed events share the existing
daemon-global sequence and bounded peer queues with connection events.

The production Phase 6 process runner deliberately produces a safe failed
session because prompt/secret/PTY startup is not yet supported. Tests inject
the concrete owned-subprocess runner or deterministic handles to prove
running/exit/terminate/kill/reaping behaviour. See
[session runtime](session-runtime.md).

## Current boundary

Handshake, capability discovery, connection list/get/create/update/delete, and
`connection.created`/`connection.updated`/`connection.deleted` cross the
daemon. Session control and lifecycle events also cross it. The daemon
advertises the three connection capabilities plus `sessions.read`,
`sessions.write`, and `sessions.events`.

The write contract intentionally contains only nickname, hostname, username,
port, and SSH protocol creation. Existing advanced SSH settings are preserved
internally during basic updates, but advanced/group/tag/Wake-on-LAN edits and
secret changes are rejected by experimental GTK daemon mode rather than
discarded. GTK waits for the mutation response and subsequent coalesced
snapshot refresh; it never removes or changes rows optimistically.

Write requests are not automatically retried. If the transport closes after a
request may have reached the daemon, `mutation_ambiguous` requires a fresh
snapshot before explicit user action. There is no exactly-once/idempotency-key
contract yet. PTYs, terminal bytes/input/resize/replay, secrets, prompts, SFTP,
forwarding, plugins, and binary channels remain out of scope.

## Packaging and lifecycle backlog

- add an explicit `sshpilotd` installed launcher after lifecycle policy settles;
- define systemd user activation without making it a runtime dependency;
- add launchd lifecycle integration for macOS;
- add Windows named-pipe transport and ownership checks;
- remove deprecated transitional-ID lookup in Protocol v2;
- define explicit reconnect/resume/replay semantics;
- define a separate binary terminal channel, PTY ownership, and prompt routing;
- keep daemon mode experimental until extended GTK lifecycle testing is
  complete; production-default selection is a separate decision.

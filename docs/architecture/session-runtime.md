# Daemon session runtime

Phase 6 introduced daemon-owned session control, and API 0.7 moved blocking
runner work off the selector. Phase 7/API 0.8 adds the concrete PTY runner and
binary terminal data plane described in
[terminal streaming](terminal-streaming.md).
`SessionManager` remains the GTK saved-layout store and is unrelated to live
runtime records.

## Ownership

```text
DaemonServer
    owns SessionRuntime
        owns SessionRecord
            may own one PTY-backed SessionProcessHandle
```

`RequestDispatcher` delegates the six explicit `sessions.*` methods to this
runtime. It never stores session state itself. Public snapshots contain no
process handle, PID, command, environment, PTY path, persistence object,
secret, or GTK/GObject value.

## Identity and lifetime

Each open allocates a random UUIDv4 and exposes it as
`session:<canonical-lowercase-uuid>`. Parsing is strict, bounded, rejects wrong
prefixes and nil UUIDs, and never interprets PIDs, timestamps, connection
names, or other input as identity. IDs are stable only for one daemon process;
sessions are intentionally not persisted or restored after restart.

Records remain visible in creation order after closure. Retention is bounded
to the newest 100 closed records. Active records are never evicted.

## State machine

The enforced states are `created`, `starting`, `running`, `closing`, `exited`,
`failed`, and `closed`. The normative transition table is in
[the API state reference](../api/state-machines.md). `closed` is final.
Repeated close is idempotent, and no state can return to `running` after exit,
failure, or closure.

Every accepted transition records an internal UTC update timestamp. Creation,
start, exit, and close timestamps are stored separately. Process exit data and
safe startup/termination failure data are distinct.

## Process runner boundary

`SessionProcessRunner.start(SessionLaunchSpec, on_exit)` is the only launch
boundary. `SessionLaunchSpec` is built from authoritative secret-free
`ConnectionDetails`; frontends cannot provide argv, executable paths, shell
fragments, or environment values.

The production Phase 6 runner deliberately returns a safe failed startup.
Starting real SSH without the later prompt/secret/PTY contract would be
misleading and could block indefinitely. The concrete injectable subprocess
runner exists for ownership/lifecycle testing and future extraction:

- `shell=False` and an argument list are mandatory;
- stdin/stdout/stderr are detached;
- the default child environment is empty rather than inherited;
- one shared reaper thread observes every owned child;
- terminate/kill target only the exact stored `Popen` handle;
- runner shutdown kills and reaps only handles it created.

No command or environment is logged or exposed.

## Attachments

Attachments are logical set membership, not byte streams. The authoritative
client ID comes from the completed handshake. Repeated attach returns the same
attachment, repeated detach when absent is safe, and one client cannot detach
another client's attachment. Socket closure removes that peer from every
session. Detach never terminates a session, and attachment count does not
control process lifetime.

`input_owner` is false and session terminal capabilities are empty because
Phase 6 has no input/output channel.

## Concurrency

The daemon selector thread owns socket reads/writes, envelope validation,
immediate bounded handlers, deferred-request reservation, and final response
queueing. It never calls `SessionProcessRunner.start()`, `terminate()`, `kill()`,
or `wait()`.

One daemon-scoped keyed executor owns four daemon worker threads and accepts at
most 64 outstanding session commands, including running commands. Submission
never waits for capacity. Equal session IDs form one serial lane; commands for
different sessions may use different workers. A close accepted while startup
is blocked therefore runs only after that startup step reconciles its handle.
Open commands use their newly allocated session ID as the lane key.

Worker completions enter a bounded thread-safe completion queue and wake the
selector. Only the selector validates the immutable peer token and pending
request ID, constructs a response envelope, updates selector interest, and
writes the frame. A worker never touches a socket or selector registration.
The monotonically allocated peer token is independent of the file descriptor,
so a late completion cannot reach a new peer after descriptor reuse.

`SessionRuntime` uses one re-entrant lock to serialize record transitions and
attachment membership. It does not hold that lock while starting, terminating,
killing, waiting, joining, or publishing callbacks. A runner reaper callback
re-enters the same transition path; duplicate exit/close races are ignored
after the first final transition.

Session events use the existing `EventPublisher`, daemon-global sequence,
encoded-once frame, bounded peer queues, and `DaemonClient` event handoff. A
slow subscriber cannot block socket response correlation.

## Open and close

Open is split into bounded record preparation and worker-owned startup. The
selector creates a `starting` record and accepts its created/starting events,
then submits exactly one start command. The response is completed only when
that worker step returns, but it deliberately contains the immutable
`starting` snapshot captured at acceptance. Later `running` or `failed` state
is learned through events or `sessions.get/list`; it may be observed before
the response because events and deferred responses share the peer output
stream. The production runner therefore yields a real failed record rather
than a fake running SSH session.

Close first enters `closing` on the selector and then runs terminate/wait/kill
on the same session's worker lane. Its response means bounded termination
finished, not merely that it was accepted. Exit emits `session.exited` followed
by final `session.closed`. A lost open/close response is
`mutation_ambiguous`; clients refresh `sessions.list` and do not automatically
retry. If termination cannot be confirmed, the record remains `failed` with
the exact handle retained; a later explicit close retries it.

If the 64-command bound is full, the request receives retryable `server_busy`.
An already-created open record is transitioned to visible `failed` state so it
cannot become an unreachable orphan. A rejected close becomes a safe failed
record and can be retried explicitly after a fresh snapshot.

## Shutdown

Daemon shutdown stops new protocol requests and executor submissions, cancels
commands that have not started, and runs required runtime cleanup on a separate
shutdown thread. Running commands and cleanup share one global monotonic
deadline; the runner's `close()` releases or kills exact owned resources.
Workers are then drained within the remaining bound, late response completions
are discarded, attachments and subscriptions close, peers close, the
connection core closes, and the owned socket is unlinked. Shutdown is
idempotent and bounded. Disconnecting a requesting peer discards its response
but does not cancel an already accepted open or close operation.

## GTK scope

Normal VTE/PyXtermJS launch remains unchanged and in-process. Experimental
daemon composition has a development/test diagnostic hook that submits
`open_session` through the application-scoped `GtkClientBridge`, records
session events after `GLib.idle_add`, and suppresses callbacks after window
shutdown. It does not create or attach a terminal widget.

## Explicitly deferred

- PTY allocation, resize, terminal input/output and binary framing;
- replay buffers, reconnect and cross-restart session restoration;
- askpass, prompts, secret lookup, authentication interaction;
- SFTP, forwarding, plugins and remote access;
- migration of normal GTK terminal launch to daemon sessions.

## Connection reload interaction

Session records keep their stable launch-time connection ID. An external edit
does not mutate an owned process, and deleting the saved connection does not
close an active session. A later `sessions.open` resolves the latest committed
daemon snapshot. Configuration reload uses a distinct executor key and never
runs on the selector thread.

# Daemon session runtime

Phase 6 introduces daemon-owned session control without terminal transport.
`SessionManager` remains the GTK saved-layout store and is unrelated to live
runtime records.

## Ownership

```text
DaemonServer
    owns SessionRuntime
        owns SessionRecord
            may own one SessionProcessHandle
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

The daemon selector thread handles requests and socket I/O. `SessionRuntime`
uses one re-entrant lock to serialize record transitions and attachment
membership. It never publishes callbacks while holding that lock. A runner
reaper callback re-enters the runtime through the same serialized transition
path; duplicate exit/close races are ignored after the first final transition.

Session events then use the existing `EventPublisher`, daemon-global sequence,
encoded-once frame, bounded peer queues, and `DaemonClient` event handoff. A
slow subscriber cannot block socket response correlation.

## Open and close

`sessions.open` returns after startup initiation completes for the configured
runner. Its snapshot may be `running` or `failed`. The record and
`session.created` event exist before startup. The production runner therefore
returns a real failed record rather than a fake running SSH session.

Close first enters `closing`, sends terminate to the exact handle, waits for a
bounded grace interval, and escalates to kill only for that handle. Exit emits
`session.exited` followed by final `session.closed`. A lost open/close response
is `mutation_ambiguous`; clients refresh `sessions.list` and do not
automatically retry. If termination cannot be confirmed, the record remains
`failed` with the exact handle retained; a later explicit close retries it.

## Shutdown

Daemon shutdown stops new session commands and unsubscribes event forwarding,
then closes active sessions under one global monotonic deadline. Remaining
exact owned handles are killed/reaped, attachments are cleared, the shared
runner/reaper stops, subscriptions close, peers close, the connection core
closes, and the owned socket is unlinked. Shutdown is idempotent and bounded.

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

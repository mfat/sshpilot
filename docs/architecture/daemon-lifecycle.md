# Daemon lifecycle architecture

Phase 11 adds explicit production lifecycle for the local per-user daemon:
states, idle shutdown, management RPCs, and client reconnect policy.

## States

```text
STARTING -> READY <-> IDLE -> DRAINING -> STOPPING -> STOPPED
                \                              /
                 ----------------> FAILED <----
```

| State | Meaning |
| --- | --- |
| `starting` | Socket bound; core migration and runtime wiring in progress |
| `ready` | Accepting work; idle countdown not yet active |
| `idle` | No live blockers; idle shutdown timer running |
| `draining` | Stop/restart/idle shutdown accepted; new opens rejected |
| `stopping` | Owned resources closing under a bounded deadline |
| `stopped` | Clean exit; socket removed when this instance created it |
| `failed` | Unrecoverable startup or crash path |

Blockers include connected clients, active sessions, SFTP services, running
transfers, active forwards, pending interactions, config reload, and an
optional keep-alive lease.

## Idle shutdown

When enabled, the daemon enters `idle` after all blockers clear and exits after
`idle_shutdown_seconds` unless a new client connects. Development trees default
to a short timeout; packaged desktop builds use a longer default. A systemd user
service may disable idle shutdown later via `service_mode` environment wiring.

Idle shutdown is cooperative: the selector observes deadlines and transitions to
`draining` before tearing down clients.

## Management RPCs

Wire methods (daemon-only):

- `daemon.status` — lifecycle snapshot and resource counts
- `daemon.diagnostics` — status plus process/thread/queue metrics
- `daemon.stop` — graceful shutdown with optional confirmation for live resources
- `daemon.restart` — same as stop with `restart_requested` set; exit code 75 for supervisors

Events: `daemon.state_changed` when lifecycle state changes.

## Client reconnect

GTK and other frontends must not block the UI thread on daemon I/O. When transport
continuity is lost:

1. stop trusting cached live session/transfer state;
2. use `DaemonReconnectPolicy` / `DaemonReconnectHelper` for bounded backoff;
3. on success, take a fresh `connections.list` / `sessions.list` snapshot;
4. never auto-restore terminals, transfers, or forwards.

Crash-loop protection stops reconnect after repeated failures within a window or
after a monotonic deadline.

## Activation models

**Primary: on-demand app launcher.** `DaemonLauncher.connect_or_start()` is
race-hardened: probe an existing compatible daemon, otherwise spawn
`sshpilot-daemon` once per attempt.

It is also self-healing, because a background service must never be able to
make the application unusable. A resident daemon that answers but cannot serve
this build — different API implementation (the in-place app upgrade case),
unsupported protocol, missing capabilities, or wedged before the handshake —
is *replaced*, not reported: `daemon.stop(force)` first so the outgoing daemon
tears down its own sessions and ControlMasters, then SIGTERM, then SIGKILL,
then removal of the orphaned socket. Locally repairable states (a runtime
directory left at the wrong mode, a stray non-socket file at the endpoint) are
repaired in place. What still fails closed is a location that is not really
ours: a symlinked runtime directory, or one owned by another user.

**Optional: systemd user service.** Packaged unit at
`share/sshpilot/systemd/sshpilot-daemon.service` for users who want a persistent
daemon. See [packaging-daemon.md](../operations/packaging-daemon.md).

**Not used: socket activation.** systemd socket units would introduce dual
activation paths alongside the app launcher without simplifying ownership or
security checks. The launcher already handles stale sockets and racing starts.

## Boundaries

- Same-user Unix socket only; no remote transport
- No process persistence across restart (new instance id, empty session table)
- No Windows daemon in this phase
- macOS launchd integration deferred

See also [daemon-transport.md](daemon-transport.md) and
[session-runtime.md](session-runtime.md).

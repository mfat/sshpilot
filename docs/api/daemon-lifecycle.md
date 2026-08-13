# Daemon lifecycle API

Stability: **stable** (Protocol v1 daemon management surface).

Canonical markers: see [methods.md](methods.md), [state-machines.md](state-machines.md),
[errors.md](errors.md), [events.md](events.md).

## Methods

| Client method | Wire method | Role |
| --- | --- | --- |
| `get_daemon_status` | `daemon.status` | Lifecycle state, idle info, resource counts |
| `stop_daemon` | `daemon.stop` | Request graceful stop / drain |
| `restart_daemon` | `daemon.restart` | Request restart (exit code 75 for supervisors) |

## Request / response fields

* `StopDaemonRequest` — `force: bool`, `confirmation: Optional[str]`.
  * `force=False` (default): requires confirmation token when live resources exist.
  * `force=True`: bypasses confirmation, immediately accepts stop.
  * `confirmation`: token from a previous refused stop request.
* `RestartDaemonRequest` — same `force` / `confirmation` semantics.
* `DaemonStatus` — `lifecycle_state`, `instance_id`, `started_at`, `idle`, `resources: DaemonResourceCounts`.
* `DaemonStopResult` — `accepted`, `state`, `resources`, `will_lose`, `confirmation`, `message`, `restart_requested`.

## Events

* `DAEMON_STATE_CHANGED` — payload is lifecycle status.

## State machine

```
starting → ready ↔ idle → draining → stopping → stopped
                                  ↗
                         failed ←
```

| State | Meaning |
| --- | --- |
| `starting` | Server initializing, not yet accepting clients |
| `ready` | Active — clients connected, resources may exist |
| `idle` | No live resources, idle countdown running |
| `draining` | Stop accepted, waiting for drain timeout or resource cleanup |
| `stopping` | Drain complete, tearing down transport and children |
| `stopped` | Terminal — process will exit |
| `failed` | Startup or runtime failure |

## Idle shutdown

When all of the following are true, the daemon enters `IDLE` and starts a countdown timer:
* `idle_shutdown_seconds > 0` (configurable; default 300s in dev, 120s packaged, disabled in service mode)
* No connected clients
* No active sessions, SFTP services, transfers, forwards
* No pending interactions
* No keep-alive lease
* No config-reload activity

When the timer expires, the daemon transitions `IDLE → DRAINING → STOPPING → STOPPED`.

**Suppression:** Any live resource (session, forward, transfer, SFTP service, interaction) or new client connection cancels the idle countdown and transitions back to `READY`.

**Reconnect:** A new client connecting during the idle window resets the timer (enters `READY`, then re-enters `IDLE` when the client disconnects if still idle).

## Graceful stop

```python
status = client.get_daemon_status()
assert status.lifecycle_state.value in {"ready", "idle"}
result = client.stop_daemon(StopDaemonRequest())
assert result.accepted is True
```

### Confirmation protocol

When live resources exist and `force=False`:

1. First call returns `accepted=False` with a `confirmation` token and `will_lose` listing the resource types.
2. Client inspects `will_lose` and retries with the matching token:
   ```python
   result = client.stop_daemon(StopDaemonRequest(confirmation=token))
   ```
3. If `force=True`, confirmation is bypassed and stop is immediately accepted.

### Drain semantics

After acceptance:
* `DRAINING`: no new work accepted (sessions, SFTP, transfers, forwards are rejected with `DAEMON_SHUTTING_DOWN`). Status/list/close/cancel remain available.
* After drain timeout (`drain_timeout_seconds`, default 5s): transitions to `STOPPING`.
* `STOPPING`: transport flushed, session commands shut down, runtimes torn down, socket removed.

## Force / terminate-all

```python
result = client.stop_daemon(StopDaemonRequest(force=True))
```

Immediately accepts stop regardless of active resources. The daemon enters `DRAINING`, then `STOPPING` after the drain timeout. Active sessions are terminated, transfers cancelled, forwards closed, SFTP services disposed.

## Ownership / retention / cleanup

### App-launched daemon

* The GTK frontend launches the daemon via `DaemonLauncher.connect_or_start()`.
* On application quit, the frontend calls `client.stop_daemon(StopDaemonRequest())` before closing the transport.
* If stop fails (e.g. daemon already exited), the error is logged and the daemon will idle-out on its own.
* `DaemonProcessHandle.started_by_frontend` marks the child as app-launched.

### Externally-managed daemon

* The frontend connects to a pre-existing daemon (`DaemonLaunchResult.process is None`).
* Normal UI exit does **not** call `stop_daemon()` — the daemon continues running.
* The daemon's own idle shutdown handles eventual exit when no clients remain.
* Explicit user-requested daemon stop (via Preferences) may stop it if policy permits.

### Socket + metadata cleanup

The daemon uses a Unix socket identity (device + inode pair) rather than PID files.
On clean stop, `unlink_owned_socket()` removes only the exact socket created by this
daemon instance (matching `st_dev + st_ino`). Stale askpass sockets are swept at
startup and shutdown.

## Error and timeout behavior

| Scenario | Behavior |
| --- | --- |
| Stop with active resources, no confirmation | Refused with `confirmation` token |
| Stop during draining | Accepted (idempotent) |
| Stop during stopping | Accepted (idempotent) |
| Client disconnect during drain | Daemon continues draining; drain timeout fires |
| Drain timeout expires with active resources | Forced transition to STOPPING |
| Transport error during stop request | Client receives `TRANSPORT_CLOSED` |

## Public method examples

```python
# Status check
status = client.get_daemon_status()
print(f"state={status.lifecycle_state.value} idle={status.idle}")
print(f"resources={status.resources.live_blockers}")

# Graceful stop (no work)
result = client.stop_daemon(StopDaemonRequest())
assert result.accepted

# Graceful stop (with work — requires confirmation)
result = client.stop_daemon(StopDaemonRequest())
if not result.accepted:
    result = client.stop_daemon(
        StopDaemonRequest(confirmation=result.confirmation)
    )
assert result.accepted

# Force stop (immediate)
result = client.stop_daemon(StopDaemonRequest(force=True))
assert result.accepted

# Resource drain before stop
for s in client.list_sessions():
    client.close_session(CloseSessionRequest(session_id=s.id))
for f in client.list_forwards():
    client.close_forward(CloseForwardRequest(forward_id=f.id))
# ... then stop
```

## Stability

Stable for Protocol v1. Idle defaults may differ between packaged/service/dev modes.

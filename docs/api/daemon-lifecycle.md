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

* `StopDaemonRequest` / `RestartDaemonRequest` — optional force and drain timeout.
* `DaemonStatus` — `lifecycle_state`, `instance_id`, `started_at`, `idle`, resource counts.
* `DaemonStopResult` — accepted / already stopping.

## Events

* `DAEMON_STATE_CHANGED` — payload is lifecycle status.

## State machine

`starting` → `ready` ↔ `idle` → `draining` → `stopping` → `stopped`.

## Timeouts / cancellation

* Idle shutdown uses configured `idle_shutdown_seconds` (environment default when unset).
* Drain is bounded (`drain_timeout_seconds`).
* Stop during active work: daemon drains owned sessions/forwards/transfers then exits.

## Ownership / retention / cleanup

* App-launched daemons are managed by the GTK client lifecycle policy.
* Externally launched daemons are not killed merely because one UI exits if policy says so;
  smoke injects an ephemeral daemon and proves detach + final stop.
* Socket + PID metadata removed on clean stop; startup sweeps stale askpass sockets.

## Cancellation / active work

If sessions, forwards, or transfers remain active, the daemon stays `ready` (not idle).
When final owned work ends, idle timer starts; then bounded exit.

## Examples

```python
status = client.get_daemon_status()
assert status.lifecycle_state.value in {"ready", "idle"}
client.stop_daemon(StopDaemonRequest())
```

## Stability

Stable for Protocol v1. Idle defaults may differ between packaged/service/dev modes.

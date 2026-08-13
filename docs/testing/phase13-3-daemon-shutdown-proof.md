# Phase 13.3 — Daemon shutdown proof

Phase 13.3 closes the remaining acceptance gap by proving natural daemon shutdown
through public APIs and separating daemon/API readiness from GTK/widget readiness.

## Previous lifecycle gap

Phase 13.2 proved daemon-backed sessions, SFTP, transfers, forwards, and
rediscovery (steps 1–40). It did **not** prove:

* Natural daemon shutdown via public `stop_daemon()` API
* Resource drain through public close/cancel APIs before shutdown
* Lifecycle state transitions (`ready → draining → stopping → stopped`)
* Socket and metadata removal after clean exit
* Child process reaping
* Idle shutdown with deterministic short timeouts
* Active-work suppression of idle exit
* App-launched vs externally-managed daemon distinction

The old smoke left the daemon alive (step 38 asserted `sock_exists=True`) and
`_finalize()` called `daemon_server.shutdown()` directly — violating the rule
that the success path must use only public client APIs.

## What Phase 13.3 proves

### Resource drain (steps 41–46)

Before requesting daemon shutdown, every daemon-owned resource is enumerated and
closed through public APIs:

| Resource | Close method | Terminal state |
| --- | --- | --- |
| Sessions | `client.close_session()` | `CLOSED` or `EXITED` |
| SFTP services | `client.close_sftp()` | `CLOSED` |
| Transfers | `client.cancel_transfer()` | `CANCELLED` |
| Forwards | `client.close_forward()` | `CLOSED` |

Step 46 verifies `live_blockers == ()` — no active work remains.

### Graceful stop (steps 47–48)

`client.stop_daemon(StopDaemonRequest())` is the **only** method used to stop
the daemon. The acceptance path never calls `DaemonServer.shutdown()`.

Step 48 polls `get_daemon_status()` to observe the lifecycle transition from
`READY` to `DRRAINING`/`STOPPING`/`STOPPED` (or client disconnect).

### Natural exit (steps 49–50)

Step 49 waits for the daemon server thread to exit (`wait_stopped(timeout=15)`).
Step 50 verifies the socket file no longer exists.

### Child reaping (step 51)

Verifies no orphaned child processes remain under the daemon PID.

### Interaction cleanup (step 52)

Verifies no stale askpass sockets remain in the daemon socket directory.

## Emergency cleanup separation

`_finalize()` now distinguishes:

1. **Acceptance path** (steps 41–52 pass): daemon already exited naturally,
   no emergency cleanup needed.
2. **Emergency cleanup** (acceptance path failed): calls `daemon_server.shutdown()`
   as a fallback, clearly logged as `[cleanup] Emergency:` — never counted as
   lifecycle proof.

## Automated test coverage

New tests in `tests/daemon/test_lifecycle_phase13_3.py` (19 tests):

| Test | What it proves |
| --- | --- |
| `test_idle_shutdown_fires_with_short_timeout` | Idle timer fires and daemon exits |
| `test_active_session_suppresses_idle_exit` | Session prevents idle exit |
| `test_active_forward_suppresses_idle_exit` | Forward prevents idle exit |
| `test_final_work_ending_starts_idle_timer` | Last resource ending starts idle |
| `test_reconnect_resets_idle_timer` | New client resets idle countdown |
| `test_force_stop_terminates_all_resources` | `force=True` with active sessions |
| `test_repeated_stop_request_is_idempotent` | Double stop does not crash |
| `test_stop_while_already_stopping` | Stop during drain is accepted |
| `test_socket_removed_on_clean_exit` | Socket file disappears |
| `test_socket_removed_on_idle_exit` | Socket file disappears after idle |
| `test_no_pid_or_metadata_files_after_exit` | No leftover files in socket directory |
| `test_externally_managed_daemon_not_killed_by_client_disconnect` | External daemon stays alive |
| `test_app_launched_daemon_stop_on_quit` | App-launched path calls stop_daemon |
| `test_app_launched_daemon_force_when_refused` | Force stop when first attempt refused |
| `test_no_zombie_children_after_force_stop` | No child processes remain |
| `test_client_disconnect_during_shutdown` | Disconnect during drain is safe |
| `test_sftp_resource_tracking_blocks_idle` | SFTP resource counters are present and zero |
| `test_daemon_resource_counts_reflect_session_close` | Session close drops resource count to zero |
| `test_claim_orphaned_forward` | New client claims and closes orphaned forward |

## Smoke harness changes

The production smoke harness now has 52 steps (was 40):

* Steps 1–40: unchanged (CRUD, auth, SFTP, transfers, forwards, import/export, restart)
* Steps 41–52: new lifecycle shutdown proof

The report format adds a `Lifecycle shutdown: X/Y` layer (Layer D).

* 52/52 steps pass.
* Overall gate: PASS.
* Emergency cleanup is **not** invoked.
* 19 lifecycle unit tests all pass.

### Resource ownership after client restart

When a client disconnects, orphaned resources (forwards, SFTP services,
transfers) have their `owner_client_id` cleared via `detach_client`. A
reconnecting client can then claim ownership via `forward.claim` (or close
directly, since the ownership check passes when `owner_client_id` is None).
This enables the full public-API shutdown sequence after a GTK restart.

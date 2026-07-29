# Daemon diagnostics

Safe observability for the local daemon without exposing secrets, paths, or
terminal data.

## CLI

```bash
sshpilot-daemon status
sshpilot-daemon diagnostics
```

Both commands print JSON to stdout. Errors use exit code `2` when the daemon is
unavailable and `1` for other protocol failures.

## Management API

Daemon clients with `daemon.status` / `daemon.control` capabilities:

- `get_daemon_status()` — lifecycle state, resource counts, idle policy
- `get_daemon_diagnostics()` — status plus uptime, executor queue depth, thread
  counts by role, optional RSS / open descriptor counts, socket bound flag

Wire payloads never include environment values, filesystem paths, connection
secrets, terminal bytes, or interaction secrets.

## Support bundle

**Help → Export Diagnostics…** adds `daemon-diagnostics.json` to the ZIP:

- `available: true` plus sanitized wire diagnostics when reachable
- `available: false` with a stable reason when not

The bundle still excludes connection lists and `~/.ssh/config` for privacy.
See [support-bundle.md](support-bundle.md).

## Log-safe fields

When logging daemon health locally, prefer:

- lifecycle `state`
- `server_instance_id`
- protocol / API implementation versions
- resource counts and idle blockers
- disconnect reason enums

Never log socket paths, handshake payloads, terminal output, or askpass data.

## Stale daemon detection

Compare application `__version__` / `API_IMPLEMENTATION_VERSION` with daemon
handshake metadata. `DaemonClient.build_mismatch()` summarizes differences.
After updating daemon code, restart `sshpilot-daemon` before testing new
behavior.

See [daemon-management.md](daemon-management.md).

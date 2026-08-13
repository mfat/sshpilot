# Daemon management

Day-to-day operations for the local sshPilot daemon on Linux.

## Starting the daemon

**Default (recommended):** launch sshPilot normally. When daemon-backed mode is
active, the application calls `DaemonLauncher.connect_or_start()` and starts the
daemon only if no compatible instance is already listening.

After an explicit **Restart daemon** (Preferences) or unexpected transport loss,
the GTK app schedules `DaemonReconnectHelper` with bounded backoff and starts a
new compatible instance. Live sessions/transfers are not restored.

**Manual / persistent:**

```bash
sshpilot-daemon              # foreground server
sshpilot-daemon status       # JSON lifecycle snapshot
sshpilot-daemon diagnostics  # JSON process metrics
sshpilot-daemon stop         # graceful shutdown
sshpilot-daemon restart      # graceful restart request
sshpilot-daemon --socket /path/to.sock status
```

Development equivalent:

```bash
python3 -m sshpilot.daemon
python3 -m sshpilot.daemon --verbose status
```

## systemd user service (optional)

Install the reference unit from `share/sshpilot/systemd/sshpilot-daemon.service`
or copy it to `~/.config/systemd/user/`. Then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now sshpilot-daemon.service
systemctl --user status sshpilot-daemon.service
```

The unit uses `RuntimeDirectory=sshpilot` under `$XDG_RUNTIME_DIR`. Idle
shutdown remains enabled unless you later set a service-mode environment
override.

**Why not socket activation?** The app launcher already validates socket
ownership, probes handshakes, and handles racing starts. A systemd socket unit
would add a second activation path without removing launcher logic.

## Stopping safely

1. Prefer `sshpilot-daemon stop` or in-app shutdown when available.
2. If live sessions/transfers exist, the daemon returns a confirmation token;
   retry with that token or use `--force` from the CLI.
3. Do not `kill -9` while sessions are active unless you accept data loss.
4. After package upgrades, restart the daemon so handshake metadata matches the
   installed application build.

## After transport loss (clients)

Protocol v1 does not restore live resources after daemon loss. The GTK app does
perform **bounded availability reconnect**:

- show a safe unavailable state for old session/SFTP/transfer IDs;
- use `DaemonReconnectHelper` / `request_daemon_reconnect()` with backoff;
- refresh snapshots after a successful handshake;
- never assume sessions survived restart.

Supervisors may also respawn on exit status `75` (`RESTART_EXIT_CODE`) when a
restart was requested.
## Environment overrides

| Variable | Effect |
| --- | --- |
| `SSHPILOT_CLIENT_MODE=daemon` | Force daemon client selection for diagnostics/tests |
| `SSHPILOT_CLIENT_MODE=in_process` | Force the compatibility in-process client for tests |
| `XDG_RUNTIME_DIR` | Base directory for `sshpilot/sshpilotd.sock` |

See [daemon-diagnostics.md](daemon-diagnostics.md) and
[upgrade-and-recovery.md](upgrade-and-recovery.md).

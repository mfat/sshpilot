# Upgrade and recovery

Recovering from daemon upgrades, crashes, and stuck state.

## After upgrading sshPilot

1. Quit running sshPilot windows if possible.
2. Stop the old daemon: `sshpilot-daemon stop` or
   `systemctl --user stop sshpilot-daemon.service`.
3. Confirm the socket is gone under `$XDG_RUNTIME_DIR/sshpilot/sshpilotd.sock`.
4. Launch sshPilot or start the service again.

Mismatch between application and daemon build metadata produces a warning, not
silent fallback for daemon-backed terminals. Restart the daemon after installing
a build that changes daemon code.

## Crash or kill

- Daemon restart assigns a **new** `server_instance_id`.
- Session, SFTP, transfer, and forward records do **not** survive restart.
- Clients must discard cached live state and list fresh snapshots.

Exit code `75` indicates an intentional restart request for supervisors
(`Restart=on-failure` user units).

## Crash-loop protection (clients)

`DaemonReconnectPolicy` tracks recent startup failures within a sliding window.
After the configured threshold or deadline, reconnect stops with `GIVE_UP` so
GTK does not hammer the system. Reset occurs only after a successful handshake.

## Stuck socket

If startup reports the socket is busy but no daemon responds:

1. Ensure no sshPilot process holds the socket (`sshpilot-daemon status`).
2. If status fails with unavailable, remove **only** a stale same-inode socket
   after verifying nothing is listening (the server refuses symlinks and wrong
   ownership).
3. Prefer letting the launcher or systemd unit start a fresh process.

Never delete sockets owned by another user's runtime directory.

## Forced stop

When graceful stop is blocked by live resources:

```bash
sshpilot-daemon stop --force
sshpilot-daemon restart --force
```

Forced stop still drains briefly so connected clients can receive disconnect
reasons where possible.

## Data preserved across restart

- Saved connections in daemon-owned `~/.ssh/config` / app configuration
- The model-only `connection_manager` import shim does not own or recover data
- OS keyring / secret backend entries
- Application preferences and logs under the state directory

Not preserved: open terminals, partial transfers, runtime forwards, in-flight
interactions.

See [daemon-management.md](daemon-management.md) and
[daemon-diagnostics.md](daemon-diagnostics.md).

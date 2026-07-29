# Packaging the daemon

How distro packages ship the local daemon launcher and optional systemd unit.

## Installed artifacts (Meson)

| Path | Purpose |
| --- | --- |
| `bin/sshpilot-daemon` | Console launcher (`python -m sshpilot.daemon` equivalent) |
| `share/sshpilot/systemd/sshpilot-daemon.service` | Reference systemd user unit |
| Python package `sshpilot.daemon` | Server, launcher, lifecycle policy |

Flatpak: Meson installs the Python package and `sshpilot-daemon` into `/app/bin`.
No separate module is required.

## systemd user unit

Upstream ships the unit under `share/sshpilot/systemd/` rather than forcing
`systemctl --user` enablement. Packagers may:

1. **Document only** — point advanced users at the share path (default upstream).
2. **Install to systemd user dir** — copy or symlink into
   `%{_unitdir_user}` / `/usr/lib/systemd/user/` in RPM/deb postinst.

Unit highlights:

- `ExecStart=sshpilot-daemon`
- `RuntimeDirectory=sshpilot` (mode 0700)
- `Restart=on-failure`
- Idle shutdown enabled for desktop sessions

## Why not socket activation?

Socket activation would require:

- a `.socket` unit plus `.service` unit;
- agreement with the app launcher on stale-socket removal and handshake races;
- extra failure modes when both app spawn and systemd start concurrently.

The on-demand `DaemonLauncher` already implements bounded, race-safe startup.
Keeping a single primary activation path avoids dual ownership bugs.

## Packaging checklist

- [ ] Ship `sshpilot-daemon` in `%files` / `.deb` contents (via Meson)
- [ ] Verify `python3 -m sshpilot.daemon --help` works after install
- [ ] Mention optional user service in distro README or postinst notes
- [ ] Do not ship a separate TCP listener or setuid helper

## References

- [daemon-management.md](daemon-management.md)
- [daemon-lifecycle.md](../architecture/daemon-lifecycle.md)
- `CONTRIBUTING.md` → Packaging

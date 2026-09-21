# Diagnostics & logging

## Where the logs are

sshPilot writes rotating logs under the state directory
(`~/.local/state/sshpilot/`, or the Flatpak equivalent under
`~/.var/app/io.github.mfat.sshpilot/.local/state/sshpilot/`):

| File | Contents |
| --- | --- |
| `sshpilot.log` | All messages (rotating, 10 MB × 5) |
| `app.log` | Frontend application subset |
| `ssh.log` | Frontend SSH / connection / terminal subset |
| `daemon.log` | Daemon process messages; never shared for rotation with frontend files |
| `sshpilot-askpass.log` | GTK-free helper/broker trace, sanitized before append |
| `crash.log` | Fatal-signal tracebacks, captured automatically. The previous run's crash is kept as `crash.log.previous` and offered on next launch and via **Help ▸ Report a Problem**. |

The askpass trace uses synchronized append semantics because the helper and
daemon broker can write it concurrently. It does not use Python's ordinary
rotating handler; safe bounded rotation for this special multi-process file is
remaining retention debt. Redaction is applied before every append and before
any forwarding or export.

## From the app

GTK warnings and uncaught exceptions are routed through the logging system, so they show
up in **Help ▸ View Logs** (filter by *Warning*/*Error*, or pick the **Crash** category to
read the last crash report). From there you can **Copy** a bug-report bundle (logs + crash
report) or **Export Diagnostics…**.

**Help ▸ Export Diagnostics…** saves a single ZIP (sanitized log text + system
info + a *redacted* `config.json`) that you can attach to a bug report. The
bundle includes frontend, daemon, askpass, and crash logs when present, while
saved connections, SSH config, known_hosts, terminal buffers, and key material
are excluded. Local raw logs may still contain hostnames, usernames, and paths;
review them before sharing.

The frontend master log is `sshpilot.log`; it is not a chronological merge of
the independent frontend and daemon processes. In explicit `--verbose` mode,
new daemon records are also forwarded into the frontend console/master stream
using a `daemon.forwarded.*` logger namespace, while the Log Viewer continues
to read `daemon.log` directly.

## Tracing a pre-connection command

A connection's **port knock sequence** and **Pre-Connection Command** (a VPN
dial-up, an `fwknop` SPA) are run by the daemon before every launch, so their
trace is in `daemon.log` — and, under `--verbose`/`--diagnostics`, inline in
the frontend master stream as
`daemon.forwarded.*`. Every line carries `[connection=… session=…]`, so one
connect reads as a sequence:

```
pre-connection command starting kind=terminal timeout_s=30 coalesce_s=5 [connection=… session=…]
pre-connection command finished exit=0 duration_ms=412 stdout_bytes=0 stderr_bytes=0 kind=terminal
```

A knock is sent first and traces separately, with no exit status of its own
because it runs no process:

```
port knock starting kind=terminal ports=3 [connection=… session=…]
port knock finished ports=3 duration_ms=412 kind=terminal
```

- `kind=` is the launch it belongs to: `terminal`, `sftp`, `forward`, `scp`,
  `remote_command` or `copy_id`.
- `reused age_ms=… window_s=…` means a recent run was coalesced rather than
  repeated — opening three tabs at once knocks once.
- `lock wait ms=…` means another launch to the same host was running it.
- A non-zero exit, a timeout or a shell that could not start logs a WARNING
  and raises a toast; the connection continues either way, so OpenSSH's own
  error is still the primary signal.
- `port knock incomplete sent=… failed=…` means packets did not reach the
  network at all — a down VPN or no route, not a closed port. A *refused* or
  *dropped* knock is the normal case and is reported as success: a knocked
  port is meant to be silent, so there is nothing to acknowledge it.
- **The knock ports, the command text and its output are DEBUG-only** (a knock
  sequence is a door code, and the command line may carry a token or a
  password), so reproduce with `--diagnostics` when the failure is in the
  command itself. A timeout kills the shell but not any
  descendants it left running; the WARNING says so.

## Command-line flags

By default the logs already capture **GTK/GLib warnings & criticals** (the
`Gtk-CRITICAL` / `Gtk-WARNING` lines that name the exact bad widget/render operation) and
**uncaught Python exceptions** (main thread, worker threads, and GLib callbacks) — no flag
needed. Extra flags for deeper diagnostics:

```bash
sshpilot --diagnostics        # shorthand for --verbose --log-gtk-warnings
sshpilot --log-gtk-warnings   # also capture lower-severity GTK/GLib info & debug
sshpilot --fatal-warnings     # abort at the first GTK/GLib warning with a backtrace
```

Running from a source checkout, use `python3 run.py` in place of `sshpilot`. Under Flatpak,
prefix with `flatpak run io.github.mfat.sshpilot`.

- `--log-gtk-warnings` additionally records lower-severity GTK/GLib info & debug messages
  (deep GTK/widget tracing). Warnings & criticals are captured without it.
- `--fatal-warnings` turns the first GTK/GLib warning/critical into a fatal abort with a
  full backtrace (written to `crash.log` and the terminal), pinpointing the offending
  operation. It is aggressive and will also abort on benign warnings, so use it in a
  focused repro session.
- `--diagnostics` is the recommended one-stop flag when reporting a bug.

`--verbose` and `--quiet` override the configured log level. Run
`sshpilot --help` for the full list.

# Diagnostics & logging

## Where the logs are

sshPilot writes rotating logs under the state directory
(`~/.local/state/sshpilot/`, or the Flatpak equivalent under
`~/.var/app/io.github.mfat.sshpilot/.local/state/sshpilot/`):

| File | Contents |
| --- | --- |
| `sshpilot.log` | All messages (rotating, 10 MB × 5) |
| `app.log` | Application messages |
| `ssh.log` | SSH / connection / terminal messages |
| `crash.log` | Fatal-signal tracebacks, captured automatically. The previous run's crash is kept as `crash.log.previous` and offered on next launch and via **Help ▸ Report a Problem**. |

## From the app

GTK warnings and uncaught exceptions are routed through the logging system, so they show
up in **Help ▸ View Logs** (filter by *Warning*/*Error*, or pick the **Crash** category to
read the last crash report). From there you can **Copy** a bug-report bundle (logs + crash
report) or **Export Diagnostics…**.

**Help ▸ Export Diagnostics…** saves a single ZIP (logs + system info + a *redacted*
`config.json`) that you can attach to a bug report — secrets are stripped and your saved
connections / SSH config are not included.

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

`--verbose` and `--quiet` / `-q` override the configured log level. Run
`sshpilot --help` for the full list.

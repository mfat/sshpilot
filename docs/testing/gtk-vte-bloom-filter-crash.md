# GTK / VTE bloom-filter crash

## Summary

On this development host, opening a real VTE terminal tab via
`terminal_manager.connect_to_host` under daemon-backed SSH can abort the
process with a GTK/GLib bloom-filter assertion (or related Adwaita
`dialog_closing_cb` assert when dialogs race during teardown).

Phase 13.2 keeps **daemon/API acceptance independent** of the VTE widget path.
The production smoke defaults to proving `DaemonClient.open_session` →
`SessionState.RUNNING` (the same API the GTK terminal uses). Opt into the
widget path with `SSHPILOT_SMOKE_GTK_TERMINAL=1`.

## Captured environment

| Component | Version (this host) |
| --- | --- |
| OS | Ubuntu 26.04 LTS x86_64 |
| Python | 3.14.4 |
| GTK | 4.22.4 |
| libadwaita | 1.9.1 |
| VTE | 0.84.0 |
| Renderer | `GSK_RENDERER=cairo`, `GDK_BACKEND=x11`, `LIBGL_ALWAYS_SOFTWARE=1` |

## Observed aborts

1. **VTE / bloom-filter** (historical, opt-in GTK terminal):
   Process aborts when churning VTE tabs under the smoke harness.
2. **Adwaita dialog host** (`adw-dialog-host.c:221` `dialog_closing_cb`):
   Triggered when the harness poked `.response()` on toplevels, or when builtin
   file-manager windows were opened/closed around import/export. Mitigated by
   removing dialog poking and not requiring FM window open for Layer A SFTP.

## Root-cause determination

| Hypothesis | Status |
| --- | --- |
| Smoke harness lifecycle (dialog poking) | Confirmed contributor for Adwaita abort |
| Multiple GTK application instances | Mitigated with unique `application_id` + `NON_UNIQUE` + quit |
| VTE misuse in production path | Not proven; needs minimal reproducer |
| External GTK/VTE/Adwaita bug | Possible on GTK 4.22.4 / Adw 1.9.1 / VTE 0.84.0 |

## Isolation policy

* Layer A (daemon/API) must pass without VTE tabs.
* Layer B (GTK controllers) may use daemon client adapters without embedding VTE.
* Layer C (widget) VTE terminal remains opt-in until a minimal reproducer either
  proves app misuse or pins an upstream bug.

## Minimal reproducer (opt-in)

```bash
SSHPILOT_SMOKE_GTK_TERMINAL=1 GSK_RENDERER=cairo GDK_BACKEND=x11 \
  LIBGL_ALWAYS_SOFTWARE=1 SSHPILOT_GUI_TESTS=1 PYTHONPATH=src:. \
  xvfb-run -a python3 -u tests/manual/phase13_production_smoke.py
```

Also try Wayland (if available) and `GSK_RENDERER=cairo` vs default.

## Remaining limitation

Do **not** mark the real GTK VTE terminal path green on this environment until
the bloom-filter abort is fixed or conclusively attributed to an upstream
dependency with a tracked minimal reproducer.

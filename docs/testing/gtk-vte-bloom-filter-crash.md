# GTK / VTE bloom-filter crash

## Update (Phase 14.1)

On this host (GTK 4.22.4 / libadwaita 1.9.1 / VTE 0.84.0), production
`TerminalManager.connect_to_host` → daemon `TerminalWidget` → `vte.feed` now
passes repeatedly under:

```bash
SSHPILOT_GUI_TESTS=1 GSK_RENDERER=cairo GDK_BACKEND=x11 LIBGL_ALWAYS_SOFTWARE=1 \
  xvfb-run -a pytest -m gui tests/gui/test_phase14_terminal_integration.py
```

Observed: 4/4 terminal integration tests PASS (connect, visible output, input,
resize) without bloom-filter abort. Restore/FM/quit GTK tests also PASS in
isolation.

Remaining instability after Phase 14.1 was an application-level
`terminal input was rejected` race during STARTING→RUNNING. That is fixed by:

* silently dropping terminal input while the session is still `STARTING`
* deferring live output delivery until `RUNNING`
* treating transient input/resize errors as non-fatal in `TerminalWidget`

Phase 14 GUI stress (3× `tests/gui/`) and production smoke (2×) then stayed green.

## Historical summary

Earlier Phase 13.2/13.3 smoke treated VTE as opt-in (`SSHPILOT_SMOKE_GTK_TERMINAL=1`)
after bloom-filter / Adwaita dialog-host aborts under harness churn.

## Captured environment

| Component | Version (this host) |
| --- | --- |
| OS | Ubuntu 26.04 LTS x86_64 |
| Python | 3.14.4 |
| GTK | 4.22.4 |
| libadwaita | 1.9.1 |
| VTE | 0.84.0 |
| Renderer | `GSK_RENDERER=cairo`, `GDK_BACKEND=x11`, `LIBGL_ALWAYS_SOFTWARE=1` |

## Policy

* Phase 14 production VTE gate is mandatory (not opt-in).
* Prefer `xvfb-run` + cairo for automated proof on this VM.
* Do not mark packaged readiness from source-tree VTE success alone.

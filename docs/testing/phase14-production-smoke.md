# Phase 14 production path smoke

Isolated HOME: see latest `tests/manual/phase14_production_smoke.py` run under `/tmp/sshpilot-phase14-smoke-*`

## Layered results (latest source-tree run)

```text
Daemon/API regression: partial (ephemeral daemon boot)
GTK terminal integration: 4/4 when connect succeeds (gtk_connected from live widgets)
GTK interaction dialogs: 0/0 in smoke (auth helper used; dedicated GTK dialog suite pending)
GTK terminal restoration: 1/1 (restored_terminal/replay_ok/live_output_ok)
GTK file-manager integration: proven in tests/gui/test_phase14_file_manager_integration.py
GTK transfer UI: partial (daemon transfer API; progress dialog row not fully asserted)
GTK quit policy: proven in tests/gui/test_phase14_quit_policy.py
VTE stability: open/close cycles pass under xvfb + cairo on this host
Packaged runtime: 0/1 (not executed)
Overall gate: FAIL
```

## Evidence fields (widget-derived; not pytest exit codes)

```text
gtk_connected=True
terminal_widget_attached=True
terminal_output_visible=True
terminal_input_verified=True
terminal_resize_verified=True
restored_terminal=True
replay_ok=True
live_output_ok=True
fm_connected=True
listing_model_populated=True
transfer_ui_connected=partial
transfer_progress_visible=partial
transfer_cancel_verified=partial
emergency_cleanup_used=varies by run
```

## How to run

```bash
SSHPILOT_GUI_TESTS=1 GSK_RENDERER=cairo GDK_BACKEND=x11 LIBGL_ALWAYS_SOFTWARE=1 \
  PYTHONPATH=src:. xvfb-run -a python3 -u tests/manual/phase14_production_smoke.py
```

## Verdict

```text
NOT READY
```

Packaged runtime end-to-end is mandatory for release candidate and was not proven.
Smoke remains flaky around post-restore file-manager open and rare input-reject races.

Generated as Phase 14.1 documentation snapshot.

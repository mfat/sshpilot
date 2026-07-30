# Phase 13.1 production GUI smoke

Isolated HOME: (see latest harness run under `/tmp/sshpilot-phase13-smoke-*`)

## Honesty note (acceptance status)

The previous smoke table overstated GUI coverage. The harness has been rewritten to:

* inject an **ephemeral `DaemonServer`** (never the user production socket)
* set **`terminal.daemon_backed_ssh=True`**
* open SSH via **`DaemonClient.open_session`** (production `SessionRuntime`)
* drive SFTP / transfers / forwards via **`DaemonClient`** APIs the GUI controllers use
* label ConnectionManager / BackupManager steps as **API-level**, not dialog clicks

### Still not acceptance-complete

| Gap | Status |
| --- | --- |
| GTK VTE tab via `terminal_manager.connect_to_host` | Opt-in (`SSHPILOT_SMOKE_GTK_TERMINAL=1`); aborts on this host (GTK bloom-filter assert) |
| Host-key ask path (step 12) | Fails: session stays `STARTING` |
| Prompt cancel / rejected auth (13–14) | Fails: sessions still reach `RUNNING` in some cases |
| Builtin FM connected + SFTP READY (15–22) | Fails: SFTP service never becomes READY in harness |
| Local/dynamic forwards ACTIVE (23–25) | Partially failing |
| GTK close/restart rediscovery (33–38) | D-Bus app-id collision partially mitigated; needs green rerun |

Latest automated harness result: **20/40 passed** (see `/tmp/phase13-smoke-daemon5.log`).

## Verdict for Phase 13.1 gate

```text
NOT READY
```

## Latest step table

Regenerate with:

```bash
GSK_RENDERER=cairo GDK_BACKEND=x11 LIBGL_ALWAYS_SOFTWARE=1 \
SSHPILOT_GUI_TESTS=1 PYTHONPATH=src:. \
xvfb-run -a python3 -u tests/manual/phase13_production_smoke.py
```

The harness overwrites this file on completion. Until a full green run exists, treat the
gap table above as authoritative over any stale PASS rows.

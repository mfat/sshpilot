# Phase 13.1 full-suite validation

Recorded against acceptance work on `dev` after baseline `b9eca377`.

## Acceptance gate status

```text
NOT READY
```

Reason: production GUI smoke was rewritten for daemon-owned paths, but the latest
harness run is **20/40 passed**. Remaining blockers are host-key/cancel/reject
auth cases, daemon SFTP READY, some forwards, and GTK restart rediscovery.
See `docs/testing/phase13-production-smoke.md`.

## Automated suite (still valuable; does not replace smoke)

### Combined authentication

| Run | Result |
| --- | --- |
| 1–3 | `20 passed, 14 skipped` each (pre-existing in-module skips) |

### Unfiltered pytest

```text
2785 passed, 45 skipped (0 CLI deselected, 0 XPASS)
```

### Temporary OpenSSH fixture

`tests/integration/test_temporary_openssh_fixture.py` — keep; valuable.

### Race ×5 / GTK ×5

Previously green after automated acceptance; re-run after any further
production-code changes.

## What changed after the smoke rejection

* Smoke no longer disables `terminal.daemon_backed_ssh`
* External `/usr/bin/sftp` / `scp` / `ssh -L` paths removed from intended
  production steps
* Steps are labeled as CM/GM/BackupManager/DaemonClient vs widget clicks
* Ephemeral daemon injection avoids touching the user `sshpilotd.sock`
* Latest harness: password/pubkey/encrypted-key `open_session` → `RUNNING` works;
  remaining interactive/SFTP/forward/restart gaps keep the gate **NOT READY**

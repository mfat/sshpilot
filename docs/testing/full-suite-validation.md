# Phase 13.2 full-suite validation

Recorded against Phase 13.2 runtime repair on `feature/phase13-2-runtime-integration-repair`
(baseline start `f621d7c6`).

## Acceptance gate status

```text
READY FOR FINAL RELEASE HARDENING
```

Production smoke (layered):

```text
Daemon/API: 21/21
GTK controller: 19/19
Widget interaction: 0/0
Overall gate: PASS
```

See `docs/testing/phase13-production-smoke.md` and
`docs/testing/phase13-2-runtime-failure-analysis.md`.

## What Phase 13.2 repaired

* Host-key / cancel / reject auth via owner-eligible interaction answering
* `RUNNING` gated on ControlMaster authentication proof
* SFTP READY + daemon transfers + cancel/atomic cleanup
* Local / remote / dynamic forward ACTIVE with traffic proof
* GTK restart rediscovery without D-Bus app-id collision
* VTE/Adwaita crash isolated; Layer A independent of VTE tabs

## Automated suite notes

Re-run after the final code change:

```bash
pytest tests/core tests/api tests/daemon tests/integration
pytest tests -k combined_auth -vv
pytest
meson test
```

Race-sensitive repaired paths should be repeated ×10; GTK controller tests ×5
where the `gui` marker is available.

# Phase 13 completion matrix

Maps each Phase 13 ownership claim to implementation, production callers,
automated evidence, and manual evidence. Baseline start: `b9eca377`.

| Claim | Implementation | Production caller | Automated evidence | Manual evidence | Status |
| --- | --- | --- | --- | --- | --- |
| Connection domain is GTK-free | `core/connections/service.py` | `ConnectionManager.add_connection_from_data` / update / remove | `tests/core/test_connections.py`, `test_group_domain_sync.py` | Smoke steps 3–8 | Complete |
| Groups sync into domain | `groups.py` + `ensure_group` / `assign_group` | Sidebar DnD / group actions | `tests/core/test_group_domain_sync.py` | Smoke steps 5–6 | Complete |
| SSH ProcessSpec is canonical | `core/ssh` + `ssh_connection_builder` native path | Terminal / SCP / native connect | `tests/core/test_ssh_*.py`, CLI `build-ssh-command` | Smoke steps 9–11 | Complete |
| Askpass policy separated | `core/interaction` + `askpass_utils` + `gtk/interaction` | Askpass helper / MainWindow provider | `tests/core/test_races.py`, askpass unit tests | Smoke steps 11–13 | Complete |
| Secret unlock policy | `core/secrets.decide_unlock` | `SecretManager.needs_unlock` | `tests/core/test_secrets_plugins.py` | Unlock path during smoke auth | Complete |
| Import/export orchestration | `core/import_export` + `backup_manager` | Export/import dialogs | `tests/core` import/export tests | Smoke steps 27–32 | Complete |
| Transfer conflict policy shared | `core/transfers.decide_conflict` | Daemon `transfer_runtime`, FM dialog | `tests/core/test_transfers.py` | Smoke steps 17–22 | Complete |
| Forwarding validation shared | `core/forwards.validate_forwarding_rule` | Connection dialog port-forward editor | `tests/core/test_validation.py` | Smoke steps 23–26 | Complete |
| Daemon tests isolated | `tests/daemon/conftest.py` XDG fixtures | N/A (tests only) | `tests/daemon/test_daemon_isolation.py` | Smoke step 40 | Complete |
| Headless core CLI | `sshpilot-core` / `core/cli.py` | N/A | `tests/core/test_cli.py`, headless imports | Smoke step 39 | Complete |
| Temporary OpenSSH fixture | `tests/fixtures/temporary_openssh.py` | Smoke / integration | `tests/integration/test_temporary_openssh_fixture.py` | Interactive host ops | Complete |
| Phase 13.1 production GUI smoke | `tests/manual/phase13_production_smoke.py` | Real GTK + ephemeral daemon | partial harness runs | See smoke doc gap table | **NOT READY** |
| Phase 13.2 runtime repair + layered smoke | `tests/manual/phase13_production_smoke.py` + daemon auth gate | Real GTK + ephemeral daemon + DaemonClient | daemon/integration + 52-step smoke (52/52, gate PASS) | `phase13-production-smoke.md` layered PASS | **Complete** |
| Phase 13.3 daemon lifecycle proof + shutdown | `tests/manual/phase13_production_smoke.py` (steps 41-52) + `tests/daemon/test_lifecycle_phase13_3.py` | Ephemeral daemon + public API shutdown + idle/force tests | 52-step smoke (52/52, gate PASS) + 19 lifecycle tests | `phase13-3-daemon-shutdown-proof.md` | **Complete** |

## Stale claims corrected

* ConnectionManager is **not** the canonical domain owner; it adapts to `ConnectionService`.
* Transfer overwrite/conflict decisions are **not** GTK-only; daemon uses `decide_conflict`.
* Production native SSH construction goes through `build_ssh_process_spec`.
* Combined-auth tests are part of the unfiltered suite (not deselected for green).
* Readiness uses only `READY FOR FINAL RELEASE HARDENING` or `NOT READY`.
* A filtered pytest run or CLI/policy-only smoke is **not** Phase 13.1 acceptance.
* The earlier “40/40 PASS” smoke table was **overstated** (external sftp/scp, daemon SSH off,
  no real GTK restart). Phase 13.2 repaired the daemon production paths; the layered smoke
  is documented under `docs/testing/phase13-production-smoke.md` with gate
  `READY FOR FINAL RELEASE HARDENING` when Daemon/API + GTK controller layers pass.

"""Dependency audit map for Phase 13 (UI-agnostic core completion).

Baseline: ``801eef44a8cc0418d94e3bd46f29c7b03fdc3e73`` on ``dev``.

## Classification legend

| Tag | Meaning |
| --- | --- |
| core-domain | Reusable GTK-free policy / state under ``sshpilot.core`` |
| daemon-runtime | Process ownership, sockets, session/transfer execution |
| platform-adapter | OS backends (may isolate ``gi`` for libsecret) |
| GTK-controller | Collects input, calls core/API, maps errors to UI |
| GTK-view | Widgets / dialogs only |
| compatibility-shim | Thin re-export or adapter over core |

## Responsibility map

| Area | Current module(s) | Responsibility | Classification | Target | Status |
| --- | --- | --- | --- | --- | --- |
| Connection records / CRUD | `core/connections` + `connection_manager` | Domain state, groups, events | core-domain + GTK-controller | `core.connections` | **Complete** — CM creates/upserts/deletes via domain; SSH-config I/O stays in CM |
| Groups | `groups.GroupManager` + domain | Membership / hierarchy | GTK-controller + core-domain | synced | **Complete** — create/move/remove/delete sync domain; load rebuilds groups |
| SSH argv construction | `core/ssh` + `ssh_connection_builder` | ProcessSpec / launch policy | core-domain + compatibility-shim | `core.ssh` | **Complete** — native path uses `build_ssh_process_spec` |
| Askpass prompt policy | `core/interaction` + `askpass_utils` + `gtk/interaction` | Classify / request / response | core-domain + GTK-controller | `core.interaction` | **Complete** — askpass uses `build_request_from_prompt`; GTK provider wired from MainWindow |
| Secret backend policy | `core/secrets` + `secret_storage` + `secret_unlock_dialog` | Selection / unlock / refs | core-domain + platform + GTK | `core.secrets` | **Complete** — `decide_unlock` gates unlock; GTK dialogs stay separate |
| Import/export | `core/import_export` + `backup_manager` | Schema / plan / merge / atomic write | core-domain + GTK-controller | `core.import_export` | **Complete** — plan + atomic export; spbk crypto remains in backup layer |
| Transfers | `core/transfers` + daemon runtime + GTK controller | Request/policy/conflict | core-domain + daemon-runtime + GTK | `core.transfers` | **Complete** — validate + `decide_conflict` + UI response mapping |
| Forwarding editor | `core/forwards` + `connection_dialog_port_forwarding` | Rule validation / defaults | core-domain + GTK-controller | `core.forwards` | **Complete** — dialog validates via core (incl. remote SOCKS / field aliases) |
| SFTP runtime | `daemon/*` + file manager | Active sessions | daemon-runtime + GTK-controller | unchanged | Runtime stays daemon (by design) |
| Keys / known_hosts / plugins / settings | Phase 12 core packages | Validation / discovery | core-domain | same | Phase 12 |

## Intentionally remaining (non-blockers)

* Rewriting SSH-config file I/O itself into core (persistence adapter stays in CM).
* `.spbk` crypto/archive format remains in `backup_archive` (not domain policy).
* Recursive transfers are now daemon-owned in `TransferRuntime` (tested).
* Full Preferences/sidebar presentation chrome stays GTK (already calls core validators/services).

Phase 13.1 acceptance artifacts:

* `docs/architecture/phase13-completion-matrix.md`
* `docs/testing/phase13-production-smoke.md` (40/40)
* `docs/testing/temporary-openssh-fixture.md`
* `docs/testing/full-suite-validation.md`

See also `core-boundary.md`, `dependency-direction.md`, `core-compatibility-shims.md`, `daemon-test-isolation.md`.

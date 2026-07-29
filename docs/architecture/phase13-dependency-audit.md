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
| Connection records / CRUD | `core/connections` + `connection_manager` | Domain state, groups, events | core-domain + GTK-controller | `core.connections` | **Moved** — CM is adapter |
| SSH argv construction | `core/ssh` + `ssh_connection_builder` | ProcessSpec / launch policy | core-domain + compatibility-shim | `core.ssh` | **Moved** — builder resolves askpass env |
| Askpass prompt policy | `core/interaction` + `askpass_utils` | Classify / request / response | core-domain + platform-adapter | `core.interaction` | **Moved** — GTK provider in `gtk/interaction` |
| Secret backend policy | `core/secrets` + `secret_storage` | Selection / unlock / refs | core-domain + platform-adapter | `core.secrets` | **Extended** — protocols added |
| Import/export | `core/import_export` + `backup_manager` | Schema / plan / merge | core-domain + GTK-controller | `core.import_export` | **Moved** orchestration |
| Transfers | `core/transfers` + daemon runtime + GTK controller | Request/policy models | core-domain + daemon-runtime | `core.transfers` | **Moved** policy models |
| SFTP runtime | `daemon/*` + file manager | Active sessions | daemon-runtime + GTK-controller | unchanged | Runtime stays daemon |
| Forwarding validation | `core/forwards` | Rule validation | core-domain | `core.forwards` | Phase 12 |
| Keys / known_hosts | `core/keys`, `core/known_hosts` | Discovery / parse / save | core-domain | same | Phase 12 |
| Plugin contracts | `core/plugins` + `gtk/plugins` | Headless API + GTK contrib | core-domain + GTK-view | same | Phase 12 |
| Settings | `core/settings` + `config.Config` | Defaults / migration | core-domain + compatibility-shim | same | Phase 12 |

## Intentionally deferred

* Full rewrite of `ConnectionManager` SSH-config I/O into core (adapter keeps persistence).
* Moving `ssh_connection_builder.resolve_native_auth` secret lookups entirely out of the shim (needs secret backend handles without pulling GTK).
* Recursive transfers (explicitly unsupported in core policy).
* Replacing every Preferences dialog path with core-only calls (already delegates overrides/validation where extracted).

See also `core-boundary.md`, `dependency-direction.md`, `core-compatibility-shims.md`, `daemon-test-isolation.md`.

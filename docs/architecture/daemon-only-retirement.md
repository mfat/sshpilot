# SSH Pilot daemon-only retirement ledger

Status: active migration ledger for the `dev` branch. This is the authoritative
cross-session plan, decision record, evidence log, and handoff. Read it before
working on this migration and update it before stopping.

## 1. Purpose and final architecture

Production has exactly one authoritative backend path:

```text
GTK or another frontend -> typed SshPilotClient API -> daemon transport
    -> daemon-owned core services/repositories -> OpenSSH/platform adapters
```

The migration retires frontend ownership of saved connections, SSH config and
known-hosts mutation, secret lookup/storage, effective-config decisions,
operation-mode transitions, remote SSH/SFTP/SCP/transfer/forward processes,
and compatibility-client selection. Daemon loss is an explicit unavailable or
recovery state. It never selects a local backend.

The ledger was created from the current `dev` checkout at
`a2c8e68d11de71ee2672927a27a00877be488714` after a fresh code-graph index and
required repository searches. The worktree remains intentionally uncommitted;
the commit value below is the branch HEAD, not a claim that the changes are in
that commit.

## 2. Non-negotiable ownership invariants

1. `ConnectionPresentationStore` is read-only DTO/event projection state.
2. GTK does not construct repositories, config stores, secret managers, or
   other authoritative core persistence services.
3. GTK does not read/write authoritative SSH config or known-hosts files or
   choose their active paths.
4. GTK does not obtain passwords/passphrases through a local manager.
5. Internal SSH, SFTP/SCP, transfers, forwarding, keys, secrets, known-hosts,
   effective config, and operation mode are daemon-owned.
6. Daemon failure never opens a GTK-owned SSH process or routes a mutation to a
   local store.
7. Existing sessions retain their launch state; new work uses the daemon's
   current snapshot and generation.
8. External terminal processes may remain OS-owned, but GTK receives a
   daemon-prepared non-secret launch specification and does not rebuild SSH
   semantics.
9. Semantic modes/scopes cross the API; frontend-derived filesystem paths do
   not.
10. Availability is based on confirmed daemon state and capabilities, never on
    `hasattr()` fallback guesses.

## 3. Definitions

* **Obsolete in-process backend:** a production frontend path that owns or
  reconstructs authoritative SSH Pilot state/I/O without daemon transport,
  including local persistence, config/known-hosts mutation, local secret
  access, or internal remote SSH processes.
* **Daemon-owned operation:** a typed API request whose authoritative state,
  persistence, interaction, process, or repository work is performed by the
  daemon/core composition.
* **Legitimate local/frontend operation:** non-authoritative local behavior,
  including local shell/PTy tabs, VTE/PyXtermJS rendering, external terminal
  selection/launch, local filesystem panes, UI preferences, GTK dialogs, and
  explicitly local plugin commands.
* **Compatibility shim:** a documented, thin import/facade retained for a
  bounded compatibility window; it has no manager, persistence, I/O, or
  competing authority.
* **Test-only direct core invocation:** direct construction of a core service
  in a headless unit test to test daemon-owned business behavior without GTK or
  transport; it is not a production client/backend path.

## 4. Complete inventory of discovered legacy paths

Discovery used the `sshpilot` code graph (`index_repository`,
`search_graph`, `trace_path`, `get_code_snippet`, and `search_code`) plus literal
searches on the requested names. The following is the classified inventory;
remaining matches are covered by the explicit classifications, not ignored.

| Classification | Current path/evidence | Disposition |
|---|---|---|
| production obsolete path | Old GTK create/update/delete branches in `MainWindow`, including local `connection_manager` persistence and post-disconnect removal | Removed; GTK now submits typed daemon mutations and preserves input on unavailable errors |
| production obsolete path | Connection-dialog password/passphrase lookup through old manager methods | Removed; protected daemon capability/API is required |
| production obsolete path | GTK effective-config `ssh -G`, host-block collection, config-root selection, and projection formatting | Removed; GTK requests generation-tagged daemon comparison DTOs |
| production obsolete path | Frontend unsaved-host destination comparison | Removed; daemon checks saved identity/normalized alias-host tokens and username |
| production obsolete path | Frontend operation-mode chooser, path/seeding logic, restart split-brain, and restore mode inference | Removed; one serialized daemon transition workflow and semantic restore facts are used |
| production obsolete path | GTK-built external SSH command | Removed; daemon returns `ExternalTerminalLaunchSpec`; GTK only chooses/launches the terminal emulator |
| production obsolete path | GVFS/system remote SFTP route, first-run remote file-manager chooser, and `force_internal` setting | Removed; remote file manager is daemon SFTP only; local filesystem panes remain |
| production obsolete path | Docker plugin copying/looking up SSH passwords through projection/manager state | Removed; plugin prompts without storage and stores only through typed daemon secret API |
| production obsolete/dead path | `LegacyInProcessSshController`, old extended-service local flags, dead client-mode selection | Removed; readiness/capability failure is unavailable/recovery |
| production obsolete/dead path | GTK `askpass_server`, `--askpass` frontend entry point, and GTK startup wiring for the old main-app askpass socket | Removed; daemon `InteractionBroker` owns SSH askpass sockets and typed prompt routing |
| compatibility shim | `src/sshpilot/connection_manager.py` and model imports in tests/manual tools | Retained as model-only `Connection`/`ConnectionState` import shim for one documented v1 window; no `ConnectionManager` class or I/O |
| daemon implementation with compatibility names | `ssh_connection_builder`, `HeadlessConnectionView`, `DaemonConnectionLaunchProvider`, and `secret_storage` contain builder/secret terminology and in-process buffers | Retained only in daemon composition or direct-core tests; production daemon now supplies an authoritative credential seam, and no GTK caller reaches it |
| daemon implementation | `ssh_config_utils`, formatter, backup engine, core repository, and OpenSSH `ssh -G` calls | Retained in daemon/core; GTK config editor delegates validation/write/reload to daemon |
| daemon implementation | `core.ssh_config_effective` and loader watch paths | Retained in GTK-free core; effective-config comparison and Include-directory invalidation stay behind the daemon service |
| daemon compatibility facade | `DaemonConnectionServices` and plugin `connection_manager` attribute | Retained only as a synchronous facade whose mutations/secrets call the typed daemon client; projection reads remain DTO-only |
| legitimate local/frontend feature | Local terminal PTYs, VTE/PyXtermJS, external terminal process ownership, local panes, dialogs, UI settings, local plugin commands | Retained; none is authoritative remote SSH behavior |
| test-only core usage | `tests/core`, daemon fixtures, direct builder/formatter/parser/secret tests | Retained; documented as direct core/service tests, not client modes |
| historical documentation/test evidence | `docs/history/**`, versioned API changelog, phase smoke records, test-only in-process protocol language | Retained only as historical evidence and labeled where needed; never current instructions |

Final source audit residuals are intentional: `askpass_utils` retains a
GTK-free standalone/direct-core helper and daemon sudo credential helpers;
`ssh_connection_builder` retains manager-shaped compatibility seams used by
daemon launch composition and direct core tests; `build_native_command` and
`spawn_async` are respectively core argv construction and legitimate local
terminal rendering/PTY APIs. The only frontend `connection_manager` matches
are projection/facade reads or test doubles; no frontend production caller
constructs a repository, config store, secret manager, known-hosts store, or
remote SSH process. `--askpass` no longer has a `run.py`/GTK entry point; the
remaining core CLI option and daemon `SSH_ASKPASS` terminology refer to typed
core process specs and the daemon InteractionBroker.

## 5. Migration phases and dependency ordering

1. Audit and ledger.
2. Define typed daemon contracts and capabilities.
3. Implement headless daemon/core operations and generation invalidation.
4. Connect GTK/plugin consumers and explicit unavailable/recovery handling.
5. Remove obsolete branches, settings, controllers, and compatibility routing.
6. Decide/document compatibility imports and update current documentation and
   generated artifacts.
7. Run focused, architecture, API/generated, integration, lint, and practical
   full verification; repeat the audit and record the handoff.

## 6. Status table

| ID | Domain | Existing path | Target owner/API | Status | Tests | Last verified commit | Notes |
|---|---|---|---|---|---|---|---|
| M00 | Audit/ledger | No current cross-session ledger | This ledger linked from current docs | VERIFIED | graph index; required searches; worktree audit | a2c8e68d | Ledger created before production edits and refreshed here |
| M01 | Connection mutation | GTK local create/update branches | `SshPilotClient.create/update/split_connection` | VERIFIED | GTK mutation/bulk/window composition: 71 passed | a2c8e68d + worktree | Unavailable save preserves input and reports recovery |
| M02 | Connection deletion | GTK local remove/save/reload branches | typed daemon delete/events | VERIFIED | included in 71 GTK mutation tests | a2c8e68d + worktree | Post-disconnect local deletion removed |
| M03 | Unsaved host | GTK reran local destination comparison | daemon unsaved-host operation | VERIFIED | unsaved-host/core/repository suite: 118 passed; explicit alias/username rule | a2c8e68d + worktree | Same ID, or normalized host/alias token plus username, is saved |
| M04 | Secrets | dialog/Docker manager secret fallbacks | protected daemon secret status/write APIs | VERIFIED | passphrase 14; Docker/plugin GUI slice 88 passed, 26 skipped | a2c8e68d + worktree | No password/passphrase in projection or external launch DTO |
| M05 | Effective config | GTK `ssh -G`, host blocks, paths | daemon effective-config comparison DTO | VERIFIED | core/effective/API suite: 118; API artifacts current | a2c8e68d + worktree | Daemon owns root/includes/Match/OpenSSH resolution; generation invalidates GTK cache |
| M06 | Operation mode | frontend flags, paths, seeding, restart flow | serialized `set/get_operation_mode` daemon workflow | VERIFIED | operation-mode/backup/edge slice: 66; preferences slice passed | a2c8e68d + worktree | Prepare/lock/atomic publish/rollback/conflict and confirmed result implemented |
| M07 | Restore safety | GTK inferred mode/path and constructed backup authority | daemon-owned backup/semantic mode facts | VERIFIED | backup/mode slice: 66; architecture/core tests | a2c8e68d + worktree | Production daemon uses headless backup config; GTK only selects destination/presents facts |
| M08 | External terminal | GTK rebuilt SSH argv | `ExternalTerminalLaunchSpec` daemon operation | VERIFIED | external-terminal core/API tests included in 56/118 suites | a2c8e68d + worktree | Secret autofill intentionally unsupported; GTK uses safe `shlex.join` only for terminal wrapper |
| M09 | In-process controller | `LegacyInProcessSshController` | no production remote controller | REMOVED | architecture/source audit; local-terminal tests retained | a2c8e68d + worktree | Local PTY/VTE was not removed |
| M10 | Compatibility | old stateful `connection_manager` module | model-only deprecated import shim | VERIFIED | compatibility shim and architecture suite: 104 focused total; no bundled plugin imports | a2c8e68d + worktree | One v1 window; documented removal at next incompatible plugin/API window |
| M11 | Routing/readiness | mode flags, local policies, `hasattr` guesses | mandatory daemon + ready/capability state | VERIFIED | architecture/routing/extended-service tests; 80 architecture/API tests | a2c8e68d + worktree | No local authority on daemon failure; precise `_daemon_ready` used |
| M12 | Docs/generated | stale current docs, API snapshots, migration references | current daemon-only documentation/artifacts | VERIFIED | `generate_api_artifacts.py --check`; API docs/schema tests: 23 passed; current-doc audit; no unclassified current in-process/backend references | a2c8e68d + worktree | Historical phase records are explicitly labeled; plugin in-process wording is plugin-process isolation, not SSH backend authority |
| M13 | Final verification | broad audit not yet complete | full practical evidence and final handoff | VERIFIED | architecture/core/API: 1445 passed, 1 skipped; migration-focused: 228 passed, 24 skipped; operation/API: 67 passed; askpass/reload/PTy: 104 passed; practical configured run: 4857 passed, 29 skipped; lint/artifacts/compile/diff clean | a2c8e68d + worktree | Optional MCP smoke collection is unavailable; parallel practical run retains three environment-sensitive failures (system `ssh -G` timing, libsecret binding, inherited Xvfb child) and excludes EasyEnv fixture setup |

## 7. Decision log

| Date | Decision | Alternatives considered | Reason | Affected paths/APIs |
|---|---|---|---|---|
| 2026-08-15 | Daemon is the only production backend authority | Preserve local fallback or infer backend from projection availability | Prevent split-brain persistence, config, secrets, and process ownership | client factory, GTK mutations/routing |
| 2026-08-15 | Keep local PTY/VTE/PyXtermJS and explicit local plugin operations | Delete every `spawn_async`/in-process match | They are explicitly local and non-authoritative | terminal/UI/plugin paths |
| 2026-08-15 | Keep core services and direct core tests | Delete code because it is called “in-process” | Daemon composition and headless tests are valid | core/daemon tests |
| 2026-08-15 | `ConnectionPresentationStore` remains read-only | Add persistence/config/secret methods | Projection authority would recreate the retired backend | GTK store |
| 2026-08-15 | Unsaved-host rule is same saved ID, or normalized host/alias token plus username | Compare guessed resolved destination/path locally | Stable identity is daemon-owned and works in default/isolated modes | `unsaved_host`, typed operation |
| 2026-08-15 | Operation-mode transition is lock-serialized, prepares target, atomically publishes config/repository state, and rolls back on failure | Persist a frontend flag and restart; mutate presentation store | Avoid split brain, data loss, and unsafe live transitions | operation-mode service/API |
| 2026-08-15 | External terminal has no secret autofill in its general launch DTO | Return password/passphrase or let GTK resolve it | Prevent secret leakage into argv/env/general API results | `ExternalTerminalLaunchSpec` |
| 2026-08-15 | Remote file management is daemon SFTP only | Preserve GVFS/system SSH URI fallback | GVFS rebuilt SSH/auth semantics outside the daemon | file manager integration/preferences |
| 2026-08-15 | `connection_manager` remains a model-only shim for one v1 compatibility window | Delete module or retain a stateful adapter | Tests/third-party model imports may exist, but manager authority is unsafe | shim/docs/changelog |
| 2026-08-15 | GTK config-editor validation/write is daemon-owned | Run local `ssh -G` validation before RPC | The daemon must validate/reload the active root and own rollback | config editor/service |
| 2026-08-15 | Remove the GTK main-app askpass socket and CLI helper | Keep a frontend socket alongside the daemon broker; let SSH fall back to a GTK child | The daemon broker already owns every production SSH child; the duplicate frontend route enabled local secret lookup/prompt authority | `main.py`, `run.py`, `askpass_server.py`, GTK askpass methods |
| 2026-08-15 | Track Include parents/directories separately from parsed source files | Watch only currently existing included files | A new file matching `Include conf.d/*` must invalidate daemon state and effective-config generations | `ssh_config_loader`, `ConnectionRepository.discover_paths`, configuration watcher |

## 8. Compatibility/deprecation decisions

`sshpilot.connection_manager` exports only ephemeral `Connection` and
`ConnectionState`. It contains no manager, repository, persistence, config,
secret, known-hosts, or process behavior. Bundled/documented plugin imports
were checked; plugin code uses the documented `PluginContext` facade/API, not
this module. The shim is retained for one v1 window and is scheduled for
removal at the next incompatible application/plugin API window, preceded by a
deprecation release note. Direct core/service tests remain supported.

`DaemonConnectionServices` is not a compatibility backend: in production it
requires a live typed daemon client for every mutation or secret operation.
The daemon launch builder's manager-shaped credential seam is likewise created
inside the daemon and is not a frontend manager.

## 9. Test and verification matrix

| Area | Evidence recorded or pending |
|---|---|
| Ownership/boundaries | Architecture and API boundary suite: 80 passed in the latest combined run; focused file-manager/preferences/core suite: 104 passed |
| Mutations/unavailable | GTK mutation/bulk/window suite: 71 passed; connection-dialog passphrase: 14 passed |
| Effective config/unsaved host | Core/effective/repository/unsaved/API suite: 118 passed; Include-watch reload slice: 54 passed |
| Operation mode/restore | Operation-mode/backup/edge slice: 66 passed; operation preferences focused suite passed |
| External terminal | Core/API external launch tests passed in the recorded 56/118 suites; no-secret DTO assertions present |
| SFTP/file manager/plugins | file-manager/preferences focused suite: 27 passed; Docker/plugin GUI-stub: 88 passed, 26 skipped |
| API/schema/generated | `python3 scripts/generate_api_artifacts.py --check`; API docs/snapshot tests: 23 passed |
| Syntax | `python3 -m compileall -q src/sshpilot` passed |
| Final checks | architecture/core/API: 1445 passed, 1 skipped; migration-focused: 228 passed, 24 skipped; operation/API: 67 passed; askpass/reload/PTy: 104 passed; practical configured run: 4857 passed, 29 skipped; lint, artifacts, compile, diff, and final audit clean |

## 10. Known failures and unresolved questions

The final audit found no current production path constructing a local
authority. Remaining verification limitations are environmental: the optional
MCP smoke module cannot import `mcp.ClientSession`; the parallel practical
suite has a flaky `ssh -G` comparison under concurrent workers, a libsecret
binding mismatch, and an inherited Xvfb child assertion. Product limitations
are: external terminal secret autofill is unsupported, and the
`connection_manager` model shim has one documented compatibility window.

## 11. Session handoff

* **Last completed item:** M13 final source/docs audit and daemon Include-watch
  invalidation; all migration-focused suites are passing.
* **Current in-progress item:** None in the migration implementation. The
  ledger remains available for environmental test follow-up or compatibility
  window work.
* **Exact next action:** A future agent should first read this ledger, verify
  HEAD/worktree, then either reproduce the three environmental practical-suite
  failures with the project's supported runtime or record their resolution;
  no production fallback code should be reintroduced.
* **Commands already run:** fresh `sshpilot` graph indexes; graph search,
  call tracing, and snippets; required source/docs audits; architecture/core/API
  suite; migration-focused GTK/daemon/API suites; operation/API suite;
  askpass/reload/PTy suite; practical configured suite; API artifact check;
  ruff; compileall; and `git diff --check`.
* **Failing tests:** default pytest collection cannot import optional
  `mcp.ClientSession`; the practical parallel run retains
  `test_default_config_path_excludes_system_defaults`,
  `test_key_passphrase_roundtrip` under the optional libsecret binding, and
  inherited-Xvfb `test_no_zombie_children_after_force_stop`. The functional
  effective-config and secret-provider tests pass in valid serial environments.
* **Files currently modified:** the worktree contains the API models/client/
  codec/capability changes; daemon/core services and loader/watcher changes;
  GTK/window/preferences/file-manager/plugin routing changes; askpass/run.py
  cleanup; current and generated docs; updated architecture/API/daemon/GTK/
  plugin tests; deleted `askpass_server.py`, `sftp_utils.py`, and stale tests;
  new operation-mode/effective-config services and version snapshots; this
  ledger; and the untracked `.codebase-memory/` artifact. Exact paths remain in
  `git status --short`.
* **Branch HEAD:** `a2c8e68d11de71ee2672927a27a00877be488714`; no migration commit
  has been created. The worktree also contains the fresh untracked graph
  artifact under `.codebase-memory/`.

## 12. Completion checklist

- [x] production client selection is daemon-only and precise readiness is used
- [x] all saved connection reads/writes are daemon-owned
- [x] effective config and unsaved-host decisions are daemon-owned
- [x] secrets, keys, known-hosts, transfers, forwards and internal SSH have no frontend fallback
- [x] operation mode and restore safety are coherent daemon workflows
- [x] external terminal receives daemon-prepared non-secret SSH semantics
- [x] obsolete production code/settings/controller/flags are removed
- [x] compatibility shim is intentional and documented
- [x] current documentation and generated artifacts describe reality
- [x] focused, architecture, generated, lint/type and practical migration tests pass
- [x] final audit has no unexplained legacy/in-process production matches
- [x] final evidence and handoff are recorded here

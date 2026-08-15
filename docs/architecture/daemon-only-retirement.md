# SSH Pilot daemon-only retirement ledger

This is the live cross-session ledger for the repair of the daemon-only
retirement introduced by `170de28f3ad174ee80829ae6282d961d12d84bc0`. Read it
before editing and update it before stopping. It is the current plan and
handoff; historical phase documents are not current instructions.

## 1. Purpose and final architecture

Production remote authority is:

```text
GTK or another frontend
    -> typed SshPilotClient API
    -> daemon transport/dispatch
    -> daemon-owned core services and repositories
    -> OpenSSH/platform adapters
```

There is no production in-process remote backend and no fallback from daemon
failure to frontend persistence, SSH config/known-hosts files, secrets,
effective-config resolution, remote SSH/SFTP/SCP/forward processes, or local
connection stores. Local PTY/VTE/PyXtermJS tabs, local filesystem panes,
UI-only preferences, dialogs, external-terminal process selection, and
explicitly local plugin commands remain frontend-owned.

Current checkpoint (2026-08-15): branch `dev`; reviewed HEAD is
`1173de32f8cd9164ab25a293e15348118cc88ab7` (`Fixed the Preferences “Done”
transport error`). The original cleanup is `170de28f3ad174ee80829ae6282d961d12d84bc0`,
parent `a2c8e68d11de71ee2672927a27a00877be488714`; the reviewed repair commits
include `57de9e18` and `fae52a61`. The worktree is intentionally dirty with
the uncommitted repair below; no unrelated changes were discarded. The active
supported environment is `.venv` on Python 3.14.4. The current retirement
decision is **NOT YET SAFE TO RETIRE** because broad verification and several
end-to-end lifecycle matrices remain incomplete.

## 2. Non-negotiable ownership invariants

1. `ConnectionPresentationStore` is read-only projection/event state.
2. GTK never constructs authoritative repositories, config stores, secret
   backends, known-host stores, or core persistence services.
3. GTK never reads/writes authoritative SSH config or known-host files and
   never selects the active SSH root.
4. GTK never obtains connection passwords/passphrases through a local manager.
5. Internal SSH, SFTP, SCP, transfers, forwarding, keys, secrets, known hosts,
   effective config, unsaved-host identity, and operation mode are daemon-owned.
6. Daemon failure produces unavailable/recovery state; it never selects a local
   remote backend or spawns a GTK-owned remote SSH child.
7. Existing sessions retain their launch snapshot; new operations use the
   daemon's confirmed current generation.
8. External terminals remain OS-owned, but receive a daemon-prepared,
   non-secret launch specification.
9. Only semantic modes/scopes cross the API; frontend filesystem paths do not.
10. Availability uses confirmed daemon capabilities/state, never `hasattr()`
    guesses or presentation-store feature detection.

## 3. Definitions

* **Obsolete in-process backend:** a production frontend path that owns or
  reconstructs authoritative SSH state, persistence, secrets, config, or
  remote processes without daemon transport.
* **Daemon-owned operation:** a typed API operation whose repository, state,
  interaction, persistence, or remote process side effect is performed by the
  daemon/core service.
* **Legitimate local/frontend operation:** a local shell/PTY, VTE renderer,
  local pane, UI preference/dialog, external emulator selection, or explicitly
  local plugin command; it has no remote authority.
* **Compatibility shim:** a documented thin import/facade with no manager,
  persistence, secret, process, or I/O behavior.
* **Test-only direct core invocation:** direct headless core-service use in a
  unit test or daemon composition; it is not a production client alternative.

## 4. Complete classified legacy-path inventory

| Classification | Path/evidence | Current disposition |
|---|---|---|
| production obsolete | GTK saved create/update/delete and post-disconnect local persistence | Removed in `170de28f`; mutation/unavailable tests cover daemon-only calls |
| production obsolete | local secret lookup/storage fallbacks and Docker unconditional persistence | Removed; Docker uses protected session input and explicit persistent consent |
| production obsolete | GTK `ssh -G`, host-block collection, config-root selection, local effective comparison | Callers use daemon DTOs; core resolver is GTK-free |
| production obsolete | raw frontend unsaved-host comparison and per-repository subprocess fanout | Daemon resolves semantic identity with omitted-port provenance and bounded work |
| production obsolete | frontend operation-mode path/seeding/restart authority | `OperationModeService` owns transitions and recovery results |
| production obsolete | frontend-built remote SSH/SFTP/SCP/forward/transfer commands and fallback routing | Daemon launch/operation services own remote side effects |
| production obsolete | `LegacyInProcessSshController`, `ClientMode.IN_PROCESS`, dead local SSH settings | No production selection remains; historical matches are documented only |
| daemon implementation | application services, repositories, config/reload, known-host, secret, transfer, key and forward services | Retained in daemon composition and direct core tests |
| compatibility shim | `connection_manager.py` model-only `Connection`/`ConnectionState` import | Retained for one documented Protocol v1/plugin compatibility window |
| compatibility facade | `ssh_config_utils.py` effective/Include functions | Thin forwarding facade; specialized editor/write helpers remain distinct |
| legitimate local | `spawn_async`, PTY/VTE/PyXtermJS, local panes, external emulator launch, local plugin commands | Retained only where side effect is explicitly local |
| test-only | daemon-in-thread fixtures and direct core service tests | Retained; not production backend selection |
| stale documentation/history | old migration/history/API snapshots mentioning in-process behavior | Historical only; current docs must not use them as instructions |
| ambiguous | plugin process isolation, local PTY bridge, daemon-in-test terminology | Requires ownership review; no blanket deletion |

Required search terms were audited with the code graph and literal searches:
`ConnectionManager`, `connection_manager`, `ConnectionPresentationStore`,
`DaemonConnectionServices`, `InProcessClient`, `ClientMode`, `IN_PROCESS`,
`in_process`, `legacy_local`, `fallback`, `ssh_config_path`,
`known_hosts_path`, `isolated_mode`, `load_ssh_config`, `save_connection`,
`create_connection`, `update_connection`, `delete_connection`, secret methods,
`ssh -G`, `spawn_async`, config utilities, and backend capability flags.

## 5. Migration phases and dependency ordering

1. Audit and persistent ledger baseline.
2. Typed API models, codec, capability handshake, compatibility boundary and
   generated artifacts.
3. Headless daemon/core ownership, generation invalidation and operation mode.
4. GTK/plugin callers, unavailable/recovery handling and reconnect lifecycle.
5. Remove obsolete branches/controllers/settings/fallbacks.
6. Compatibility/documentation cleanup.
7. Focused, architecture, serial concurrency, practical and supported GUI
   verification; final source-to-side-effect audit.

## 6. Status table

Statuses in this table are `PENDING`, `IN PROGRESS`, `BLOCKED`, `REMOVED`, or
`VERIFIED`. A test-only or narrow mocked result is not sufficient for
`VERIFIED`.

| ID | Domain | Existing path | Target owner/API | Status | Tests | Last verified commit | Notes |
|---|---|---|---|---|---|---|---|
| R00 | Ledger/audit | Stale HEAD, dirty-tree and green-suite claims | This ledger and linked architecture docs | IN PROGRESS | orientation and current audit; final audit pending | 1173de32 | Update before every stop |
| R01 | Startup info | `print_info()` indexed removed `config_dir`/`ssh_dir` | semantic mode + daemon authority | VERIFIED | startup suite: 10 passed | 170de28f | No frontend SSH path output |
| R02 | Dialog errors | unavailable callback lacked detail | separate unavailable/rejection/recovery handlers | VERIFIED | window and Preferences suites passed | 170de28f | Conflict explanations are retained |
| R03 | Watcher reload | `start()` lost initial debounced reload | watcher registration + semantic reload | VERIFIED | config reload/coordinator tests passed | 170de28f | `refresh_paths()` remains mode-transition path |
| R04 | Docker secrets | use-once unconditionally persisted password | protected session credential + explicit store API | IN PROGRESS | provider/dispatch tests pass; GUI/plugin end-to-end pending | 1173de32 | Need supported GUI evidence and full retry matrix |
| R05 | Error routing | effective-config used external-terminal error handler | operation-specific error classification | VERIFIED | daemon-error/effective-config tests pass | 170de28f | Optional post-connect check skips prompt on failure |
| R06 | Operation mode | rollback failure could split runtime/config | transactional result with truthful recovery state | IN PROGRESS | 12 service tests pass; full daemon restart/fault matrix pending | 1173de32 | Must prove all transition steps and restart behavior |
| R07 | Mode RPCs | programmatic radio updates re-entered toggle | scoped suppression + in-flight guard | VERIFIED | Preferences operation-mode tests pass | 170de28f | Initial controls stay disabled until confirmed |
| R08 | Effective config | duplicate top-level/core resolver | canonical `core.ssh_config_effective` | IN PROGRESS | core/effective/generation tests pass | 1173de32 | Supported OpenSSH parity still pending |
| R09 | Host alias safety | leading-dash alias could reach OpenSSH as option | API, repository and resolver validation | VERIFIED | model/core/effective tests pass | 1173de32 | Imported invalid aliases fail closed |
| R10 | Unsaved identity | raw-token/exact-user regression and CLI port 22 default | daemon-resolved host/user/port/ProxyJump identity | IN PROGRESS | core/CLI tests pass; full Include/Match matrix pending | 1173de32 | Need bounded large-repository evidence |
| R11 | Store boundary | repository reached private `_isolated` | public read-only `SshConfigStore.isolated` | VERIFIED | core/architecture suites pass | 170de28f | No frontend authority added |
| R12 | API compatibility | changed wire behavior remained at 0.39 | API 0.40 boundary, historical 0.39 snapshot | VERIFIED | API/docs/generator checks pass | 1173de32 | Stale daemon must return restart-required; no plaintext fallback |
| R13 | Shared settings | stale GTK tree could overwrite daemon keys | cross-process lock plus baseline-aware merge; daemon semantic writers lock full transactions | IN PROGRESS | cross-process and 4-writer tests pass | 1173de32 | Full caller ownership migration remains a risk; see decisions |
| R14 | Protected input | global request-id dictionary lacked owner/TTL/limits | registered client-owned protected-input lifecycle | IN PROGRESS | dispatch tests pass; real transport suite pending final pass | 1173de32 | Need disconnect/late-frame matrix in supported transport |
| R15 | Reconnect | long-lived controllers held closed client | canonical replacement lifecycle | IN PROGRESS | lightweight reconnect tests pass; real open-Preferences/in-flight matrix pending | 1173de32 | Must verify every client-backed service and stale completion |
| R16 | Credentials | stale session password shadowed corrections/deletes | replace/clear/wipe session credential API | IN PROGRESS | provider tests pass; Docker auth retry and wipe matrix pending | 1173de32 | Memory and transport no-leak proof incomplete |
| R17 | External terminal | capability could be omitted despite provider | conditional handshake + typed preparation handler | VERIFIED | capability/dispatcher/client integration tests pass | 1173de32 | GTK chooses emulator only |
| R18 | Key scope | key manager could exist before mode confirmation | no DEFAULT key manager until confirmed mode | VERIFIED | delayed-mode/key-dialog tests pass | 1173de32 | Stale client reconnect refresh remains R15 |
| R19 | File manager | external preference risked GVFS/frontend SSH path | standalone daemon-backed file-manager window | VERIFIED | Manage Files preference tests pass | 170de28f | Both embedded and standalone use daemon SFTP |
| R20 | Docs/artifacts | stale changelog/version/snapshots and graph artifacts | 0.40 docs, restored 0.39 snapshot, ignored graph cache | VERIFIED | generator, ruff, compileall, diff check pass | 1173de32 | Final source/documentation audit still pending |
| R21 | Final retirement | prior ledger overstated evidence | source-to-side-effect trace and honest verdict | PENDING | broad practical and GUI evidence incomplete | 1173de32 | Cannot mark safe yet |

## 7. End-to-end contract audit

| Domain | Frontend entry/GUI state | Client guard/dispatch | Daemon owner and side effect | Config/cache/restart contract | Evidence/status |
|---|---|---|---|---|---|
| Client/readiness | startup/reconnect state | handshake, API version, capabilities | launcher/transport | mismatch is restart-required; no local client | focused tests; R15/R21 pending |
| Saved connections | dialogs and projections | connection read/write capabilities | application service/repository/config | generation/events refresh projection; restart reloads repository | core/API suites; verified boundary |
| Metadata/groups | sidebar/group controllers | metadata/group capability methods | repository metadata sidecar | atomic daemon write and event refresh | core/API suites; R15 reconnect pending |
| Secrets/passphrases | dialogs, Docker, askpass presenter | secret capability and protected input | secret provider/broker/backend | persistent writes locked; session values memory-only and restart-cleared | provider/dispatch pass; R04/R16 pending |
| SSH config/editor | GTK renders daemon DTO/text | config capability and dispatch | `SshConfigStore`/reload coordinator | daemon owns root/includes/watch paths and generation | reload/effective slices pass |
| Known hosts | viewer/editor | known-host read/write capability | known-host service | atomic daemon mutation/reload | existing service tests; final aggregate pending |
| Effective config | checker/dialog stores DTO/generation | effective-config capability | canonical resolver/OpenSSH | daemon generation invalidates stale results | generation/core pass; R08 pending parity |
| Unsaved host | post-connect optional prompt | `check_unsaved_host` capability | application service + resolver | semantic identity, bounded work, generation | core/CLI pass; R10 pending matrix |
| Operation mode | Preferences radios, confirmed mode, key scope | mode get/set guard and deferred handler | mode service/repository/watchers | lock covers read/modify/write; runtime/config/UI/restart agree or recovery result | 12 service + 9 UI pass; R06 pending |
| Backup/restore | GTK selects semantic options | backup/restore capability | daemon transfer/backup services | mode facts come from daemon; restart reads persisted mode | targeted coverage; final aggregate pending |
| Internal SSH | terminal tabs/readiness | sessions capability | daemon session/launch provider | existing sessions retain launch snapshot; no local child | terminal ownership tests |
| External terminal | GTK selects emulator | `terminal.external_launch` only if provider callable | launch provider prepares non-secret argv/env | active config semantics prepared daemon-side | handshake/client tests pass |
| SFTP/file manager | embedded/standalone window | SFTP capability | daemon SFTP runtime | remote process daemon-owned; local panes remain local | Manage Files/SFTP tests |
| SCP/transfers | file manager/plugin operation UI | transfer/SCP capability | daemon transfer runtime | operation lifecycle daemon-owned | service tests; aggregate pending |
| Forwarding | forward controls | forwarding capability | daemon forward runtime | blockers prevent unsafe mode transition | service tests; aggregate pending |
| Keys/agent | key actions gated by confirmed mode | keys/identity capabilities | daemon key/identity services | semantic scope only after confirmation; restart derives persisted scope | key tests; R18 pass |
| Plugins | Docker/mosh/SDK UI | plugin/session/settings capabilities | daemon remote plugin operations; explicit local commands stay local | plugin settings transaction cannot clobber unrelated keys | EasyEnv fixture repaired; GUI evidence pending |
| Reconnect/shutdown | app lifecycle and Preferences | lifecycle/status/reconnect | daemon owns remote cleanup | one replacement lifecycle rebinds all services and invalidates caches | R15 pending full matrix |
| Compatibility shim | plugin imports | no backend capability | model-only `connection_manager` shim | removal after one compatibility window | documented; pending final source audit |
| Local features | local terminal/panes/UI prefs | no remote capability | frontend-only | no authoritative SSH/config/secret side effect | practical local tests; aggregate pending |

## 8. Decisions and rejected alternatives

| Date | Decision | Alternatives rejected | Reason and affected paths |
|---|---|---|---|
| 2026-08-15 | API implementation version is 0.40; 0.39 snapshot is historical and restored | Rewrite 0.39 or silently interoperate | Password transport, session input, mode and unsaved-host wire changes require a boundary; stale daemon gets restart-required |
| 2026-08-15 | Shared settings use a GTK-free cross-process lock; `Config` merges only its baseline-owned changes under that lock | RLock-only, reload-before-write, or stale whole-tree replacement | Process-local locks cannot protect GTK/daemon; full caller ownership migration remains tracked as R13 risk |
| 2026-08-15 | Docker use-once is a daemon session credential; persistence requires explicit consent | Persistent store on every dialog completion or local keyring | Secret authority and user consent remain daemon-owned |
| 2026-08-15 | Session credentials can be replaced and explicitly cleared; persistent store/delete clears the session override | Let a one-hour stale session value shadow correction/deletion | Authentication retry and deletion must be truthful |
| 2026-08-15 | Protected command input is client-owned, registered, bounded, TTL-limited and wiped | Global request-id dictionary or plaintext JSON | Prevent unsolicited/wrong-client/late secret frames and memory leaks |
| 2026-08-15 | Operation-mode failure after publication is recovery-required with runtime/persisted modes exposed | Ordinary rejection or hiding split brain | UI and restart must not lie about active mode |
| 2026-08-15 | Leading-dash Host aliases are rejected as OpenSSH option ambiguity | Assume shell injection or rely on `--` support | argv has no shell, but OpenSSH option confusion must be prevented at API/repository/resolver boundaries |
| 2026-08-15 | Omitted CLI port remains `None`; explicit `-p 22` remains explicit | Treat parser default 22 as user input | Preserve alias `Port`/Include/Match semantics |
| 2026-08-15 | No key manager is constructed while daemon mode is unconfirmed | Construct DEFAULT scope and hide actions later | Avoid wrong-scope key operations during delayed startup |
| 2026-08-15 | Standalone “external” file manager remains daemon-backed, not GVFS/frontend SSH | Restore GVFS or local remote backend | Preserve feature semantics without reviving remote authority |
| 2026-08-15 | Reconnect uses one client-replacement lifecycle | Rebind only window/client fields | Controllers, Preferences, key scope and generation caches otherwise retain closed clients |

### Unsaved-host identity rule

For SSH, an internal saved connection ID is authoritative when present. An
ephemeral CLI connection never receives a durable ID merely because nickname
equals hostname. Without a matching ID, the daemon compares canonical semantic
identity: hostname case-insensitively, username after empty input is replaced
by daemon local login, explicit/default port, effective ProxyJump, and protocol.
Aliases/direct hosts match only when canonical OpenSSH resolution agrees;
`User` from Host/Match/Include participates. Display-name or rename changes do
not change an internal-ID match. Non-SSH destinations do not match by SSH host
fields. The rule is tested for alice/root, empty users, aliases, Include/Match,
IPv4/IPv6, ports, ProxyJump, deletion and rename; the larger real-daemon matrix
remains R10.

## 9. Compatibility and deprecation

`connection_manager.py` is a model-only import shim for `Connection` and
`ConnectionState`; it contains no manager, persistence, secret, config,
known-host, process, or I/O behavior. Owner: API/plugin maintainers. Remove
after one documented Protocol v1/plugin compatibility window and a deprecation
release note.

`ssh_config_utils.py` forwards effective/Include behavior to canonical core;
specialized editor/write helpers are retained. Remove the facade after all
production editor/backup callers use the canonical or dedicated editor module.

API 0.39 is not a supported mixed-version peer for the changed 0.40 wire
surface. The launcher/client detects mismatch before normal calls, reports a
restart/recovery outcome, and does not kill a daemon with live resources or
fall back to plaintext secrets.

## 10. Test and verification matrix

| Area | Command/result in current worktree |
|---|---|
| Architecture/core/API | `.venv/bin/pytest -q -n0 tests/architecture tests/core tests/api` → `1453 passed, 1 skipped` |
| Cross-process/shared settings | `.venv/bin/pytest -q -n0 tests/core/test_settings_transaction_lock.py tests/test_config_cross_process.py` (included above) → passed; multiprocessing uses `fcntl` lock, not only `RLock` |
| Focused startup/preferences/reconnect/passphrase/effective/unsaved/daemon | Named daemon/startup/preferences/reconnect/passphrase/effective/CLI/unsaved set → `180 passed, 1 failed`; event callback synchronization fixed; final event modules → `22 passed`; final startup/preferences/reconnect/passphrase/event subset → `61 passed` |
| Generated API artifacts | `.venv/bin/python scripts/generate_api_artifacts.py --check` → `API artifacts are current.` |
| Lint | `.venv/bin/ruff check src/ tests/ scripts/generate_api_artifacts.py` → `All checks passed!` |
| Compile | `.venv/bin/python -m compileall -q src/sshpilot` → passed |
| Diff hygiene | `git diff --check` → passed |
| Practical non-GUI broad run | `find tests -type f -name 'test_*.py' ! -path 'tests/mcp/*' -print0 \| xargs -0 .venv/bin/pytest -q -n auto -m 'not integration and not gui' --maxfail=20` → `4924 passed, 31 skipped, 11 failed`; failures are recorded below and are not green |

## 11. Known failures and unresolved risks

* The broad practical run above had 11 failures: read-only daemon log path in
  `test_run_server_returns_restart_exit_code`; xdist/environment-sensitive
  askpass and effective-config tests; stale key-discovery expectation now
  corrected to the confirmed-mode invariant; file-manager/terminal teardown
  tests affected by GTK stubs; and the architecture debt ratchet, which was
  corrected by moving the lock to `platform.locking`. The corrected serial
  architecture/core/API run is green. These failures must be rerun in the
  supported environment before a final verdict.
* The focused daemon run initially failed
  `test_connection_store_changed_event_reaches_idle_client` because its event
  callback released the test after the preceding `connection.updated` event.
  The daemon emitted both events; the test now waits specifically for
  `connection_store.changed` and the event/backpressure modules pass `22
  passed`. Classification: test synchronization defect, corrected.
* The restart-exit-policy test initially attempted to create a daemon log in
  the sandbox's read-only `/home/mahdi/.local/state` and failed with
  `OSError: [Errno 30]`. It now isolates logging in the exit-policy unit test;
  the final subset passes 61 tests. Classification: test environment
  isolation, corrected; production logging behavior remains covered by its
  own logging tests.
* The optional MCP suite is not a supported green signal until the installed
  SDK provides `mcp.ClientSession`; any collection failure must retain its
  exact dependency classification.
* `test_default_config_path_excludes_system_defaults` was previously sensitive
  to an unreadable system OpenSSH include symlink; it passes directly in the
  current environment but needs supported-environment confirmation.
* Operation-mode full fault injection/restart proof, reconnect with an open
  Preferences window and in-flight operation, Docker retry/no-leak GUI proof,
  and the large-repository unsaved-host bound remain incomplete.
* R13 is not a claim that every `Config.set_setting()` caller is now daemon
  owned. The cross-process merge prevents demonstrated lost updates, but the
  remaining frontend JSON-backed settings must be classified and migrated or
  explicitly separated before the final safe-retirement verdict.

## 12. Session handoff

* Last completed action: moved the shared settings transaction primitive to
  GTK-free `src/sshpilot/platform/locking.py`; removed its new unapproved
  frontend core-debt entry; corrected the stale key-discovery test; reran
  architecture/core/API and hygiene checks.
* Current action: complete the final source audit and record the dirty-tree
  evidence; the broad practical run remains incomplete and must not be called
  green.
* Exact next command:

  ```bash
  .venv/bin/pytest -q -n0 tests/daemon tests/test_startup_behavior.py \
    tests/test_window_daemon_errors.py tests/test_preferences_operation_mode.py \
    tests/test_daemon_reconnect_gtk.py tests/test_connection_dialog_passphrase.py \
    tests/test_manage_files_ui.py tests/test_effective_config_checker_generation.py \
    tests/test_cli_connect.py tests/test_unsaved_host.py
  ```

* Commands already run: baseline/status/rev-parse; graph index and source
  traces; focused regression batches; broad non-GUI xdist run; corrected
  architecture/core/API serial run; generator `--check`; Ruff; compileall;
  `git diff --check`.
* Current modified files: API 0.40 models/client/dispatch/docs/generated
  artifacts; settings/config/operation-mode/secret/protected-input/effective
  config/reconnect/preferences/Docker/terminal/core validation sources; tests
  for all of those areas; new `src/sshpilot/platform/locking.py`,
  `tests/api/snapshots/versions/0.40.json`, and
  `tests/test_config_cross_process.py`; this ledger.
  Exact `git status --short` paths at this checkpoint are:

  ```text
  docs/api/CHANGELOG.md docs/api/README.md docs/api/compatibility.md
  docs/api/generated/model-index.md docs/api/generated/schema.json
  docs/api/methods.md docs/api/protocol-v1.md
  docs/architecture/daemon-only-retirement.md
  src/sshpilot/api/client.py src/sshpilot/api/daemon_client.py
  src/sshpilot/api/models/connections.py src/sshpilot/api/transport/codec.py
  src/sshpilot/api/version.py src/sshpilot/command_converter.py
  src/sshpilot/config.py src/sshpilot/core/connection_application_service.py
  src/sshpilot/core/connections/service.py src/sshpilot/core/package_graph.py
  src/sshpilot/core/settings/store.py src/sshpilot/daemon/connection_secret_provider.py
  src/sshpilot/daemon/dispatch.py src/sshpilot/daemon/operation_mode_service.py
  src/sshpilot/daemon/server.py src/sshpilot/effective_config_check.py
  src/sshpilot/main.py src/sshpilot/plugins/builtin/docker_manager/page.py
  src/sshpilot/preferences.py src/sshpilot/terminal_manager.py src/sshpilot/window.py
  src/sshpilot/platform/locking.py
  tests/api/snapshots/public_api.json tests/api/snapshots/versions/0.39.json
  tests/api/snapshots/versions/0.40.json tests/api/test_capabilities_contract.py
  tests/api/test_daemon_client_keys.py tests/api/test_daemon_client_protocol.py
  tests/core/test_connection_application_service.py tests/daemon/test_connection_secret_provider.py
  tests/daemon/test_event_forwarding.py tests/daemon/test_operation_mode_service.py
  tests/daemon/test_secret_dispatch.py tests/test_cli_connect.py
  tests/test_connection_dialog_fixes.py tests/test_config_cross_process.py
  tests/test_daemon_reconnect_gtk.py tests/test_easyenv_plugin_e2e.py
  tests/test_effective_config_checker_generation.py tests/test_preferences_operation_mode.py
  ```
* No commit was created in this session. Current HEAD remains
  `1173de32f8cd9164ab25a293e15348118cc88ab7`; the worktree status is recorded
  by the final command below. The next agent must rerun the broad practical
  command and then audit every remaining `Config.set_setting()` caller.

## 13. Completion checklist

- [x] Production selection is daemon-only; no silent local remote fallback.
- [x] Startup/dialog/reload/external-terminal/key-scope repairs have focused coverage.
- [x] API 0.40 boundary and historical 0.39 snapshot are documented/generated.
- [x] Cross-process settings lock and deterministic lost-update regression exist.
- [ ] All shared JSON callers are classified and ownership split is complete.
- [ ] Reconnect replaces every client-backed object and updates open Preferences.
- [ ] Operation-mode fault/restart/concurrency matrix is complete and truthful.
- [ ] Credential replacement/clear/wipe and real protected-input transport matrix is complete.
- [ ] Unsaved-host bounded large-repository and full OpenSSH semantics matrix is complete.
- [ ] Practical, serial, GUI-supported and full verification are reproducibly green.
- [ ] Final audit has no unexplained legacy/in-process production matches.
- [ ] Final ledger evidence supports `SAFE TO RETIRE` or documented-shim verdict.

**Current final decision: NOT YET SAFE TO RETIRE.**

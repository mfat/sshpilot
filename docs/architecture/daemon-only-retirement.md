# SSH Pilot daemon-only retirement ledger

This is the authoritative cross-session plan, status, decision, evidence, and
handoff document for the retirement repair on `dev`. Every agent must read it
before editing and update it before stopping.

## 1. Purpose and final architecture

The production path is exactly:

```text
GTK or another frontend
    -> typed SshPilotClient API
    -> daemon transport/dispatch
    -> daemon-owned core services and repositories
    -> OpenSSH/platform adapters
```

Daemon failure is an explicit unavailable, rejected, or recovery-required
state. It never selects a frontend persistence/config/secret/process backend.
Local PTY/VTE/PyXtermJS tabs, local filesystem panes, UI state, dialogs,
external-terminal process selection, and explicitly local plugin commands remain
legitimate frontend features.

Current repair-pass baseline: 2026-08-15, branch `dev`, HEAD
`170de28f3ad174ee80829ae6282d961d12d84bc0` (`daemon-only retirement cleanup`),
parent `a2c8e68d11de71ee2672927a27a00877be488714`. The worktree was already
dirty at session start; unrelated changes were preserved. This session also
removed the committed `.codebase-memory` binaries and added the directory to
`.gitignore` because repository policy does not require generated graph
artifacts. No final commit has been created; HEAD and the parent are unchanged.
The supported environment is the active project `.venv`; optional MCP,
libsecret, Xvfb, and integration dependencies require explicit evidence.
Orientation read before production edits: `AGENTS.md`, this ledger, the
frontend-closure/core-boundary/daemon-transport documents, API compatibility
and changelog, issue #1159 (the public issue fetch was unavailable in the web
cache), and commits `170de28f` and `a2c8e68d`.

## 2. Non-negotiable ownership invariants

1. `ConnectionPresentationStore` is read-only projection/event state.
2. GTK never constructs repositories, `SshConfigStore`, secret backends,
   known-host stores, or other authoritative persistence services.
3. GTK never reads/writes authoritative SSH config or known-host files and
   never chooses the active SSH root.
4. GTK never obtains a connection password/passphrase through a local manager.
5. Internal SSH, SFTP, SCP, transfers, forwarding, keys, secrets, known hosts,
   effective config, unsaved-host identity, and operation mode are daemon-owned.
6. Daemon failure never opens a GTK-owned remote SSH child or routes mutation
   into a local store.
7. Existing sessions retain their launch state; new operations use the
   daemon's confirmed current snapshot/generation.
8. External terminals remain OS-owned, but receive a daemon-prepared,
   non-secret SSH launch specification.
9. Only semantic modes/scopes cross the API; frontend filesystem paths do not.
10. Availability is based on confirmed daemon state/capabilities, never on
    `hasattr()` backend selection.

## 3. Definitions

* **Obsolete in-process backend:** a production frontend path that owns or
  reconstructs authoritative SSH state/I/O without daemon transport.
* **Daemon-owned operation:** a typed API request whose state, repository,
  interaction, persistence, or process side effect is performed by daemon/core.
* **Legitimate local/frontend operation:** local shell/PTy, VTE/PyXtermJS,
  local files, UI preferences/dialogs, external-terminal selection, or an
  explicitly local plugin command; it is not remote SSH authority.
* **Compatibility shim:** a documented thin import/facade with no manager,
  persistence, secret, process, or I/O authority.
* **Test-only direct core invocation:** direct headless construction of a core
  service in unit tests or daemon composition; it is not a production client.

## 4. Complete classified legacy-path inventory

The audit used the persisted code graph (`index_repository`, `search_graph`,
`trace_path`, `get_code_snippet`, and `search_code`) plus literal `rg` searches
for every term required by the migration brief. Classification is by import,
call, and side effect, not filename or comments.

| Classification | Discovered path and evidence | Current disposition |
|---|---|---|
| production obsolete path | Old GTK create/update/delete and post-disconnect local persistence branches | Removed by `170de28f`; focused mutation/unavailable tests cover daemon-only calls |
| production obsolete path | Dialog/Docker local secret lookup/storage fallbacks | Docker use-once now uses protected `set_session_connection_password`; persistent storage remains explicit daemon API |
| production obsolete path | GTK `ssh -G`, host-block collection, config-root/path selection, and local effective comparison | Removed from callers; canonical resolver is GTK-free core and UI receives DTOs |
| production obsolete path | Raw frontend unsaved-host comparison | Daemon resolves semantic host/user/port/ProxyJump identity |
| production obsolete path | Frontend operation-mode path/seeding/restart authority and restore inference | Daemon service owns transition; rollback result now distinguishes recovery |
| production obsolete path | GTK-built remote SSH/SFTP/SCP/forward/transfer commands and local fallback routing | Daemon APIs/launch providers remain authoritative; local PTY is retained |
| production obsolete path | `LegacyInProcessSshController`, `ClientMode.IN_PROCESS`, dead local SSH flags/policies | No production selection remains; historical docs/tests are classified below |
| daemon implementation | `ConnectionApplicationService`, `ConnectionRepository`, `SshConfigStore`, `KnownHostsService`, interaction/secret/transfer/forward/key services | Retained in daemon composition and direct core tests |
| daemon implementation | `ConnectionPresentationStore` | Retained as read-only GTK projection only |
| compatibility shim | `connection_manager.py` exports ephemeral `Connection`/`ConnectionState` | Retained for one documented compatibility window; no manager/I/O |
| compatibility facade | `ssh_config_utils.py` | Now forwards effective/Include helpers to `core.ssh_config_effective`; write/validation helpers remain specialized |
| compatibility facade | `DaemonConnectionServices` and plugin `connection_manager` attribute | Retained as daemon-client facade, not a local backend |
| legitimate local/frontend feature | `spawn_async`, PTY/VTE/PyXtermJS, local terminal fallback wording, external emulator launch, local panes | Retained only where the side effect is explicitly local |
| test-only core usage | `tests/core`, daemon fixtures, direct parser/builder/secret/service tests | Retained and must not be mistaken for production client alternatives |
| stale documentation/history | `docs/history/**`, old API changelog entries, phase records mentioning `InProcessClient` or old fallback | Historical only; current docs/ledger must not present them as instructions |
| ambiguous/decision required | Remaining `in-process` in plugin process isolation, local PTY bridge, daemon-in-thread tests, or secret internals | Each requires side-effect review; no blanket deletion |

## 5. Migration phases and dependency ordering

1. Audit/ledger and baseline evidence.
2. Typed API models/codecs/capabilities and generated artifacts.
3. Headless daemon/core ownership and generation invalidation.
4. GTK/plugin callers plus explicit unavailable/recovery handling.
5. Remove obsolete branches/controllers/settings/fallbacks.
6. Consolidate compatibility facades and current documentation.
7. Focused, architecture, API, integration, lint, practical-suite, and final
   source-to-side-effect verification.

## 6. Status table

Status values in this table are limited to `PENDING`, `IN PROGRESS`,
`BLOCKED`, `REMOVED`, and `VERIFIED`. “Verified” requires reproducible test
evidence; the migration is not complete while any item is pending or blocked.
The verification column records the base HEAD because this repair pass is
intentionally uncommitted; every result below applies to the dirty worktree
described in the handoff, not to `170de28f` alone.

| ID | Domain | Existing path | Target owner/API | Status | Tests | Last verified commit | Notes |
|---|---|---|---|---|---|---|---|
| R00 | Audit/ledger | Stale ledger claimed old HEAD and green verification | This ledger plus linked architecture docs | IN PROGRESS | orientation, baseline, graph traces, focused regressions, and current audit run; final practical audit pending | 170de28f | Must be updated at every stop |
| R01 | Startup diagnostics | `StartupInfo.print_info` indexed removed path keys | semantic operation mode + daemon authority | VERIFIED | `pytest -q -n0 tests/test_startup_behavior.py` — 10 passed | 170de28f | No frontend SSH paths returned or printed |
| R02 | Dialog callbacks | unavailable callback accepted no detail | explicit detail/recovery dialog methods | VERIFIED | `tests/test_window_daemon_errors.py` — 4 passed; Preferences — 9 passed | 170de28f | Operation rejection is not labeled transport unavailability |
| R03 | Config reload | `start()` did not schedule initial semantic reload | watcher registration + debounced initial reload | VERIFIED | `tests/daemon/test_config_reload.py` plus `tests/daemon/test_config_reload_coordinator.py` — 18 passed | 170de28f | `refresh_paths()` remains mode-transition path |
| R04 | Docker credentials | use-once dialog unconditionally called persistent store | protected daemon session credential API and explicit persistent callback | IN PROGRESS | provider/dispatch focused tests pass; GUI test collection skipped without GUI | 170de28f | Persistent request now sends password in a protected command-input frame; full GUI/plugin verification pending |
| R05 | Error routing | effective-config errors used external-terminal handler | operation-specific GTK handlers | VERIFIED | `tests/test_window_daemon_errors.py` — 4 passed | 170de28f | Optional unsaved-host failure logs/skips prompt |
| R06 | Operation mode transaction | rollback failure returned ordinary rejection and could split runtime/config | explicit persisted/runtime/recovery result plus shared settings transaction lock | IN PROGRESS | `tests/daemon/test_operation_mode_service.py` — 12 passed, including four deterministic concurrent writers | 170de28f | Server watcher hook and GTK cache/scope sync covered; full daemon restart/fault matrix pending |
| R07 | Mode RPC suppression | programmatic radio state could re-enter toggle | scoped suppression + in-flight guard | VERIFIED | `tests/test_preferences_operation_mode.py` — 9 passed | 170de28f | Callbacking-radio regression coverage added |
| R08 | Effective config | duplicate core/top-level Include/ssh-G/parser logic | canonical `core.ssh_config_effective`; top-level forwarding facade | IN PROGRESS | effective/include/core slice and generation tests pass; `test_default_config_path_excludes_system_defaults` fails even alone in this container because plain `ssh -G` rejects the unreadable `/etc/ssh/ssh_config.d/20-systemd-ssh-proxy.conf` symlink, so its expected display side is empty | 170de28f | Production launch provider now imports canonical core module; supported-environment parity/audit pending |
| R09 | Host aliases | leading-dash alias could reach `ssh -G` as option | API/repository validation and safe resolver rejection | VERIFIED | `tests/api/test_connection_models.py`, loader suite, repository suite, effective resolver tests — passed | 170de28f | Existing invalid imports fail closed and are never executed |
| R10 | Unsaved-host identity | raw token/exact-user comparison changed old behavior | daemon-resolved host/user/port/ProxyJump semantics | VERIFIED | `tests/core/test_connection_application_service.py` resolved-identity matrix and CLI ephemeral tests pass | 170de28f | Host casefold, user exact, local-login inference, port/ProxyJump participate; semantics recorded below |
| R11 | Store boundary | repository reached private `_isolated` | read-only `SshConfigStore.isolated` property | VERIFIED | repository/core suite in effective slice — 134 passed | 170de28f | No new frontend authority |
| R12 | API/generated docs | new typed operations/models not fully documented; changelog malformed | generated schema/snapshots and current docs | VERIFIED | API documentation — 18 passed; generator check — `API artifacts are current`; changelog separates current 0.39 from historical 0.38/0.37/0.36 | 170de28f | `API_IMPLEMENTATION_VERSION` and current docs are 0.39; 0.39 snapshot is the reviewed current baseline |
| R13 | Graph artifacts | committed `.codebase-memory` binaries caused dirty baseline | ignored local graph cache | REMOVED | deletion and `.gitignore` change applied; final status pending | 170de28f | Deletion is requested cleanup; graph can be regenerated locally |
| R14 | Compatibility policy | model import shim and historical in-process references | documented bounded shim/history | IN PROGRESS | architecture/core/API run passed; final current-doc/source audit pending | 170de28f | Removal at next incompatible plugin/API window |
| R15 | Full retirement audit | prior ledger asserted verification despite failures | 20-domain source-to-side-effect trace and final search | PENDING | final practical and serial runs pending | 170de28f | Cannot mark migration complete yet |

## 6a. Source-to-side-effect trace

| Domain | Entry point | Frontend caller | Typed API/capability | Wire dispatch | Daemon/core owner | Side effect | Unavailable behavior | Evidence |
|---|---|---|---|---|---|---|---|---|
| 1 Client/readiness | `api.client_factory` | GTK startup | `SshPilotClient`, daemon status/capabilities | handshake/status | `DaemonLauncher`/transport | daemon connection | unavailable/recovery; no local client | client-factory and routing tests pending final run |
| 2 Saved list/get | connection projection refresh | GTK stores | `list_connections`, `get_connection` / `connections.read` | `connections.list/get` | `ConnectionApplicationService` + repository | snapshot read | projection remains stale/marked unavailable | core/API suites |
| 3 Create/update/duplicate/split/delete | GTK dialogs/actions | window/plugin facade | typed mutation methods / `connections.write` | `connections.create/update/duplicate/delete/split` | repository/config store | atomic SSH/state persistence | dialog retains input; no local mutation | mutation tests |
| 4 Metadata/groups | GTK sidebar/group controllers | typed metadata/group APIs | `connections.metadata/groups` | `connections.*` | repository sidecar | metadata/group persistence | reject/unavailable; no projection write | API/core tests |
| 5 Secrets/passphrases | Docker/dialog/askpass | protected client interactions | secret status/store/reveal/session APIs | secret JSON ack + protected frames | daemon secret provider/broker | protected credential lookup/storage | unavailable; no blank/local fallback | provider and Docker tests |
| 6 SSH config read/edit | editor actions | GTK editor | get/save config DTOs / config capabilities | `connections.get/save_ssh_config_text` | `SshConfigStore`/reload coordinator | authoritative config read/write | reject and preserve editor text | config/editor tests |
| 7 Include/reload | watcher and mode transition | none (daemon) | generation/events | internal daemon callback | canonical loader/coordinator | watch paths and repository reload | bounded debounce/retry | reload tests |
| 8 Known hosts | host-key/editor UI | typed known-host client | known-host APIs | `known_hosts.*` | `KnownHostsService` | parse/revision/atomic mutation | capability/error state | known-host tests |
| 9 Effective config | warning/viewer | `EffectiveConfigChecker`/window | `get_effective_config` / config read | `connections.get_effective_config` | `core.ssh_config_effective` | OpenSSH resolution/comparison | effective-config-specific message | 134 effective/core tests |
| 10 Unsaved host | post-connect hook | `TerminalManager` | `check_unsaved_host` / connections.read | `connections.check_unsaved_host` | application service + canonical resolver | identity comparison only | skip optional prompt and log | core tests; matrix pending |
| 11 Operation mode | Preferences/startup | radio/status projection | set/get operation mode / operation.mode | `daemon.set/get_operation_mode` | `OperationModeService` | target/config/repository/service transition | conflict or recovery result | 7 service + 9 UI tests |
| 12 Backup/restore | backup controller/API | GTK selects options only | daemon backup/secret-transfer APIs | backup operation dispatch | daemon secret transfer + headless backup engine | archive/config/known-host/secret transfer | semantic mode facts and rejection | backup/restore suite pending |
| 13 Internal SSH | terminal activation | `TerminalManager` | session APIs / sessions capability | `sessions.*` | daemon session runtime/launch provider | daemon-owned SSH child/PTY | readiness failure; never local SSH | terminal routing tests |
| 14 External terminal | external action | GTK chooses emulator | `prepare_external_terminal_launch` | connections launch dispatch | daemon launch provider | daemon-prepared argv/env; GTK process launch | unavailable/capability error | external-terminal tests |
| 15 SFTP/file manager | file pane | GTK view/controller | SFTP APIs / sftp capabilities | `sftp.*` | daemon SFTP runtime | remote SFTP process/operations | unavailable; local panes remain local | SFTP/file-manager tests |
| 16 SCP/transfers | copy/upload/download | file manager/plugin | transfer/SCP APIs | `transfers.*` | daemon transfer runtime | remote transfer process/files | operation failure; no local remote fallback | transfer tests |
| 17 Forwarding | forward actions | GTK controller/plugin | forward APIs | `forwards.*` | daemon forward runtime | forwarding SSH child/socket | reject/unavailable; no `ssh -N` frontend | forward tests |
| 18 Keys/agent | key UI/plugin | GTK key controllers | key/identity APIs | `keys.*`/`identity.*` | daemon key/identity services | key generation/agent/deployment | capability/error state | key/agent tests |
| 19 Plugins | Docker/mosh/SDK | plugin context | typed plugin/connection/session APIs | plugin dispatch | daemon plugin operation services | remote commands daemon; explicit local commands local | no local remote fallback | plugin contract tests |
| 20 Shutdown/reconnect/compat/local | lifecycle and shim | GTK lifecycle + local tabs | daemon status/reconnect; model shim | lifecycle methods/events | daemon lifecycle; `connection_manager` model shim | daemon cleanup; local PTY cleanup | recovery/reconnect; shim has no authority | lifecycle/local-terminal/compat tests |

## 6b. End-to-end contract audit checkpoint

The following audit records the complete contract shape, including the GUI
state boundary and restart/cache behavior. “Advertised” means the daemon
handshake capability set; a dispatcher method is not considered available just
because a handler exists. The audit was revalidated against the current graph
and source after the repairs in this session; focused evidence is listed in
the status table and matrix below.

| Domain | Frontend entry and GUI state | Typed guard / dispatcher advertisement / handler | Daemon owner and side effect | Config/cache/restart contract |
|---|---|---|---|---|
| Client/readiness | startup factory owns a pending/ready/unavailable selection; GTK never creates a local client | `DaemonLauncher.connect_or_start()` plus daemon status/capabilities; no `IN_PROCESS` branch | daemon transport and handshake | reconnect replaces client-backed projections; unavailable disables/recovery state |
| Saved connections | dialogs/sidebar retain input and project immutable summaries | `connections.*` capability checks; dispatch maps each mutation to a deferred handler | `ConnectionApplicationService` → `ConnectionRepository`/`SshConfigStore` | atomic repository/config writes; daemon reloads same root after restart |
| Metadata/groups | GTK group/sidebar stores are projections/controllers only | metadata/group capabilities and typed mutation handlers | repository metadata sidecar | generation/events refresh projections; restart reads daemon state |
| Secrets/passphrases | dialogs receive protected interaction results; no password in projection DTO | secrets capabilities; store/session handlers require protected command-input frames | daemon secret provider/broker/key-passphrase service | session secrets are memory-only; persistent consent is explicit; restart drops session secret |
| SSH config/editor | editor renders daemon-returned text/DTO and preserves unsaved text on failure | config read/write capability; `connections.get/save_ssh_config_text` handlers | canonical config store and reload coordinator | generation/watch graph updates; daemon restart rediscovers active root |
| Include/reload | no GTK authority; events update projections | internal coordinator path, not a frontend-selected path | canonical loader + watcher | `start()` schedules initial debounced reload; `refresh_paths()` replaces Include watch paths and reloads |
| Known hosts | GTK renders snapshots and asks typed remove operation | known-host read/write capabilities and handlers | daemon `KnownHostsService` | atomic mutation/revision; restart reopens daemon-owned store |
| Effective config | checker/dialog holds generation-tagged DTO, never a path or `ssh -G` result | config-read capability; deferred effective-config handler | canonical GTK-free resolver and app service | result rejected if repository generation races; checker rejects stale daemon generations |
| Unsaved host | post-connect prompt is optional UI state; failed check skips prompt | connections-read capability; deferred check handler | app service resolves host/user/port/ProxyJump through canonical resolver | snapshot generation is returned; restart uses same semantic rule |
| Operation mode | radios disabled/confirmed; `_confirmed_operation_mode`, `_key_scope`, `KeyManager`, and `Config.config_data` update only after daemon result | operation-mode capability; deferred get/set handlers; no radio callback re-entry | `OperationModeService` → repository transition + watcher hook | shared settings lock covers read/modify/write; canonical config tree, target, runtime, watcher, UI cache, and restart mode remain coherent or return recovery-required |
| Backup/restore | GTK chooses semantic restore options and displays daemon facts | backup/secret-transfer capability handlers | daemon transfer/backup services | mode facts are confirmed by daemon; restart reloads persisted canonical settings |
| Internal SSH | terminal tabs track daemon session readiness and launch state | sessions/terminal capabilities; session dispatch | daemon session runtime and launch provider | existing sessions retain launch state; new sessions use current snapshot; no local child on failure |
| External terminal | GTK chooses emulator and launches returned argv; no shell interpolation | `terminal.external_launch` is advertised only when launch provider has callable preparation; typed handler reaches `prepare_external_terminal_launch` | daemon launch provider/OpenSSH adapter | non-secret argv/env spec reflects active root; daemon/capability failure is explicit |
| SFTP/file manager | Manage Files chooses embedded or standalone daemon-backed window from preference | file-transfer/SFTP capability guard; SFTP handlers | daemon SFTP runtime | remote authority stays daemon-owned; local filesystem panes remain local; restart creates fresh daemon SFTP |
| SCP/transfers | GTK renders operation state and progress | transfer capabilities and deferred handlers | daemon transfer/SCP runtime | operation lifecycle survives UI projection loss; no frontend remote subprocess |
| Forwarding | GTK controller requests typed forward and displays status | forward capabilities and handlers | daemon forward runtime | active forwards block unsafe mode transition; restart does not silently recreate them |
| Keys/agent | key UI/actions stay unavailable while operation mode is unconfirmed | keys/identity capabilities; typed key handlers | daemon key/identity service and native agent | semantic `DEFAULT`/`ISOLATED` scope only after confirmation; restart derives scope from persisted mode |
| Plugins/Docker | plugin UI uses daemon client/context; explicit local plugin commands remain local | plugin settings/command/session capabilities and dispatch | daemon plugin operation services; Docker password callback uses protected API | plugin settings transaction cannot clobber mode; no password in ordinary responses/logs |
| Shutdown/reconnect | GTK closes projections/local PTYs and shows recovery state | daemon lifecycle/status capability | daemon lifecycle owns remote cleanup | reconnect creates new client/projections; compatibility imports remain inert |
| Local/frontend features | local terminal PTY/VTE, local panes, UI preferences, dialogs, external emulator process | no remote capability is implied | frontend-only by explicit scope | no authoritative SSH/config/secret side effect; retained intentionally |

## 7. Decision log

| Date | Decision | Alternatives considered | Reason | Affected paths/APIs |
|---|---|---|---|---|
| 2026-08-15 | Daemon remains the only production backend authority | Restore local fallback or infer backend from projection features | Prevent split-brain state and secret/config/process ownership | client factory, GTK routing/mutations |
| 2026-08-15 | Keep local PTY/VTE/PyXtermJS and explicit local plugin commands | Delete every `spawn_async` or “in-process” match | Those side effects are explicitly local | terminal/UI/plugin paths |
| 2026-08-15 | Docker “use once” uses daemon-memory session credentials | Call persistent `store_connection_password`; revive local keyring | Consent and secret ownership remain explicit | Docker plugin, protected command-input frame |
| 2026-08-15 | Operation-mode rollback failure is a recovery result, not a conflict | Return ordinary rejection or hide split brain | Runtime/persisted modes must be truthful | `OperationModeResult`, mode service/UI |
| 2026-08-15 | Canonical effective-config implementation is GTK-free core | Keep duplicated top-level resolver | Launch and UI semantics must not diverge | `core.ssh_config_effective`, facade |
| 2026-08-15 | Hostnames casefold and normalize IPs; usernames remain exact; port and ProxyJump participate | Raw token equality; ignore port; casefold usernames | Preserve OpenSSH destination semantics without conflating accounts | `UnsavedHostCheckRequest`, daemon core |
| 2026-08-15 | Reject new/renamed Host aliases beginning with `-`; safely return unavailable for imported invalid aliases | Pass `--` without platform proof; trust GTK-only validation | Avoid OpenSSH option confusion at every authoritative boundary | API models, repository, resolver |
| 2026-08-15 | Remove committed graph artifacts and ignore `.codebase-memory/` | Keep generated local binary in source control | No repository policy requires versioned graph binaries and the artifact causes false dirty state | `.gitignore`, `.codebase-memory/` |
| 2026-08-15 | Advertise external-terminal capability only when the daemon provider implements the typed preparation method | Advertise based only on a handler or let GTK rebuild SSH argv | Handshake availability must describe the actual daemon provider | dispatch handshake, `prepare_external_terminal_launch` |
| 2026-08-15 | Keep API implementation version at 0.39 and repair the 0.39 documentation/snapshot rather than inventing 0.42 | Treat dirty generated additions as a version bump | Source authority and protocol compatibility remain 0.39/1.0; current additive surface is captured by the reviewed baseline | `api/version.py`, docs, generated artifacts |
| 2026-08-15 | Persistent Docker password storage uses the same protected command-input frame as session credentials; the dialog callback is the only consent signal | Send password in ordinary RPC JSON or persist every “use once” entry | Secrets must not cross ordinary wire/log/DTO surfaces and storage consent must be explicit | daemon client/dispatch, Docker page |

### Unsaved-host identity decision table

The daemon compares a saved session by internal ID first. For SSH
destinations without a matching ID, it resolves both the ad-hoc destination and
each saved alias through the daemon's canonical OpenSSH resolver. Hostname is
case-insensitive; username is case-sensitive after empty input is replaced by
the daemon's local login; port and effective ProxyJump participate; protocol
must be SSH. IPv4/IPv6 literals are normalized as host identities. A direct
hostname and an alias match only when their resolved host/user/port/ProxyJump
tuple matches. Rename/display-name changes do not alter an internal-ID match;
deleted IDs fall back to semantic comparison. Non-SSH destinations never match
by host fields.

| Case | Result |
|---|---|
| Same saved connection ID | saved, regardless of display name or current text |
| Saved `alice@host`, ad-hoc `root@host` | unsaved |
| Saved `alice@host`, ad-hoc empty user and daemon login `alice` | saved |
| Alias versus direct hostname | saved only after canonical effective resolution agrees |
| `User` from `Host`, `Match`, or `Include` | included by daemon effective resolution |
| Host case differs | equal after casefold |
| IPv4/IPv6 spelling differs | equal only under canonical literal normalization |
| Explicit/default port differs | unsaved when port participates and differs |
| ProxyJump differs | unsaved |
| Non-SSH protocol, deleted ID | no host-field match; deleted SSH ID may use semantic fallback |

## 8. Compatibility/deprecation decisions

`src/sshpilot/connection_manager.py` is retained only as a model-only import
shim for `Connection` and `ConnectionState`. It has no manager, repository,
config, secret, known-host, process, or I/O behavior. Bundled/documented plugin
imports were audited; the supported plugin route is `PluginContext` and typed
daemon services. Owner: API/plugin maintainers. Removal condition: one
documented Protocol v1 compatibility window, followed by a deprecation release
note and an incompatible plugin/API window.

`ssh_config_utils.py` is a thin forwarding facade for effective/Include
resolution. Its atomic editor and validation helpers are specialized write
helpers, not a second effective resolver. Owner: core/API maintainers. Removal
condition: all backup/editor imports use `core.ssh_config_effective` or a
dedicated editor module.

`DaemonConnectionServices` is not a fallback backend: production mutations and
secret operations require its attached typed daemon client. Direct core
service tests and daemon-in-thread fixtures are test/composition usage only.

## 9. Test and verification matrix

| Area | Required evidence | Current evidence/result |
|---|---|---|
| Ownership/boundaries | architecture/frontend-closure, AST/import guards | Pending final run; current graph/literal audit recorded above |
| Startup/dialog/reload | focused startup, GTK callback, watcher/debounce tests | Startup 10 passed; window 4 passed; Preferences 9 passed; reload included in focused pass; lifecycle 20 passed |
| Docker/secrets | provider, protected frame, persistence consent, no-leak tests | `tests/daemon/test_secret_dispatch.py` — 24 passed; Docker GUI module skipped because GUI environment is unavailable; full GUI/plugin evidence pending |
| Operation mode | transition, blockers, target/config/repository/hook/rollback/cleanup/restart faults | `tests/daemon/test_operation_mode_service.py` — 12 passed, including canonical missing-config and four-writer concurrency; complete live daemon fault/restart matrix pending |
| Effective config | Include/glob/cycle/tokens/repeated/Match/paths/roots and canonical facade guard | 134 effective/core tests pass; default-path regression is blocked by malformed/unreadable container system SSH config; parity/guard/full run pending |
| Unsaved host | ID, alias/direct, user inference, Include/Match, case/IP/port/ProxyJump/non-SSH/rename | Existing core tests pass; required matrix pending |
| Alias safety | GUI/API/repository/import/effective lookup | model/repository/effective regressions added; full matrix pending |
| API/contracts | codecs, dispatch/client, capabilities, docs, schema, version snapshots | focused API documentation — 18 passed; architecture/API/core prior run — 1449 passed, 1 skipped; generator check pending final run; source/docs/snapshot version is 0.39 |
| Remote services | SSH sessions, SFTP, SCP, transfers, forwarding, known hosts, keys, askpass | Prior evidence exists but must be reproduced in this pass |
| Local features | PTY/VTE/PyXtermJS/local panes/external emulator/local plugins | Must remain covered by local-terminal/plugin suites |
| Broad verification | supported full suite, serial concurrency-sensitive runs | Parallel explicit non-MCP run: 4875 passed, 29 skipped, 8 failed, 21 errors; eight failures pass isolated; serial full run was interrupted; see section 10 |
| Hygiene | `ruff`, compileall, `git diff --check`, generated check | `.venv/bin/python scripts/generate_api_artifacts.py --check`, `.venv/bin/ruff check src tests scripts/generate_api_artifacts.py`, `.venv/bin/python -m compileall -q src/sshpilot`, and `git diff --check` all passed after the final source edits |

## 10. Known failures and unresolved questions

Previously recorded failures must not be called green without reproduction:

* Default environment: `pytest -q -n0` fails collection because the optional
  MCP dependency does not provide `mcp.ClientSession` (`113 deselected, 21
  skipped, 1 collection error`). Classification: dependency/environment.
* Explicit parallel non-MCP headless command:
  `find tests -type f -name 'test_*.py' ! -path 'tests/mcp/*' -print0 | xargs
  -0 pytest -q -n auto -m 'not integration and not gui'` produced `4875
  passed, 29 skipped, 8 failed, 21 errors` in 103.30s. The eight failures
  pass as `8 passed` under the isolated serial command. Classification:
  concurrency, environment, or test-fixture; not a product-green result.
* `test_default_config_path_excludes_system_defaults` was reproduced both in
  the larger serial selection and alone in this container:
  `.venv/bin/pytest -q -n0 tests/test_effective_config_diff.py::test_default_config_path_excludes_system_defaults`
  -> `1 failed`. The scratch HOME config resolves correctly with `-F`, but the
  displayed plain `ssh -G foo` side returns no output because OpenSSH rejects
  the container's unreadable `/etc/ssh/ssh_config.d/20-systemd-ssh-proxy.conf`
  symlink (`Bad owner or permissions ...`). Classification: environment/test
  contract, not silently product-green; reproduce in the supported environment
  after fixing or explicitly isolating that system configuration. This blocks
  the effective-config verification gate.
* `test_key_passphrase_roundtrip` passes alone (`1 passed`); the earlier
  libsecret failure is not reproduced in this environment. Classification:
  optional dependency/environment.
* `test_no_zombie_children_after_force_stop` initially detected the inherited
  pytest Xvfb child and logged an injected runner `AttributeError`; adding the
  fixture's required `close()` contract and filtering the assertion to SSH
  executables makes the lifecycle module pass (`20 passed`). Classification:
  test fixture/harness, corrected in this pass.

Each final failure must include the exact command/environment, output,
classification (product/test/dependency/packaging/concurrency/environment),
supported-environment reproduction, and whether it blocks retirement. Open
design verification still required: complete operation-mode fault injection,
unsaved-host behavior through a real daemon config with Include/Match, and
transport-level session credential consent/no-leak coverage.

### 10a. Final source-to-side-effect audit register

The final literal audit was run on 2026-08-15 with the required search terms,
then checked against the persisted graph call traces. Remaining matches are
classified here so terminology is not mistaken for authority:

| Match family | Remaining production matches | Classification and proof |
|---|---|---|
| `ConnectionManager` / `connection_manager` | GTK parameters and properties, `DaemonConnectionServices`, CLI/model compatibility names, daemon launch/backup shims | GTK instances are ephemeral projection/facade state; graph traces show repository/config/secret side effects enter through daemon composition. `connection_manager.py` is model-only compatibility. CLI and historical/test names are not GTK backend selection. |
| `credential_manager` / `secret_storage` | `CredentialManager` is reached by daemon backup export; secret backends are reached by daemon secret/interaction services and daemon-owned askpass | daemon-only persistence/backup composition; no frontend production constructor/caller was found. Direct secret backend tests remain test/core coverage. |
| `ssh_connection_builder`, `build_ssh_connection`, `ssh -G` | daemon launch provider, daemon secret-transfer runner, daemon SSH readiness/interactions, canonical core resolver, specialized config-text validation | remote process argv and secret lookup are daemon-owned. `core.ssh_config_effective` is the only production effective resolver; `ssh_config_utils` forwards to it. No GTK effective-config subprocess path remains. |
| `ssh_config_path`, `known_hosts_path` | daemon launch/interaction/backup services and daemon-owned specialized adapters; tests | filesystem authority remains daemon-owned. GTK receives semantic DTOs and does not choose or mutate these paths. |
| `get_connection_password`, `get_key_passphrase`, `store_connection_password` | protected GTK facade calls, daemon credential provider/launch shims, daemon dispatch, historical docs/tests | GTK only submits protected typed operations; local manager fallback is absent from production GTK routing. Session passwords are daemon-memory-only; persistent storage requires explicit consent. |
| `spawn_async`, PTY, VTE, PyXtermJS, `subprocess` | local terminal tabs, VTE rendering/input, external terminal launch, daemon-owned remote services, test doubles | retained legitimate local/frontend operations are not remote authority; remote process construction is reached through daemon services. |
| `fallback`, `legacy`, `in-process` | recovery/error wording, local-terminal/plugin terminology, historical documents and this ledger | no production backend selection or remote fallback. Historical documents are explicitly historical; current docs link this ledger. |
| `hasattr` | GTK widget/API compatibility probes, daemon optional-service lifecycle probes, platform feature probes | no remaining `hasattr` branch selects a local SSH/backend after daemon failure. Readiness uses confirmed daemon state and capabilities for route availability. |
| `LegacyInProcessSshController`, `InProcessClient`, `ClientMode.IN_PROCESS`, `_daemon_mode_active` | only this ledger/history references where required for audit | no source implementation or production setting remains. |

The audit intentionally does not delete local PTY/VTE code, external-terminal
selection, local filesystem panes, or explicit local plugin commands merely
because they spawn a process. Those side effects are outside the obsolete
in-process remote backend definition.

## 11. Session handoff

* **Last completed item:** repaired the confirmed capability-enum bug in
  `MainWindow`, added the deterministic settings-writer lock test, restored
  Manage Files preference semantics, added protected Docker password consent,
  added startup watcher-race coverage, and re-audited the external-launch
  handshake through handler/service ownership using the code graph.
* **Current in-progress item:** final hygiene and verification after the
  effective-config environment failure was reproduced precisely.
* **Exact next action:** run the following command in the supported project
  environment after correcting the system OpenSSH configuration or recording
  an accepted test isolation strategy:
  `.venv/bin/pytest -q -n0 tests/test_effective_config_diff.py tests/core tests/architecture tests/api`
  Then run the final artifact/lint/compile/diff checks. Do not make production
  authority depend on the container's broken `/etc/ssh` config.
* **Commands already run:** orientation reads and commit/parent inspection;
  graph index/search/trace/snippets; focused startup/reload/preferences/Docker,
  operation-mode, effective-config/include/core/repository, API documentation,
  window-error, Manage Files, and lifecycle suites; `.venv/bin/pytest -q -n0
  tests/architecture tests/api tests/core` (1449 passed, 1 skipped, prior
  baseline); migration-focused selection (216 passed, 1 skipped, prior
  baseline); newest focused batch (62 passed), operation-mode/Manage Files
  (20 passed), loader/coordinator/API model slice (45 passed), window slice
  (26 passed), secret/Docker slice (24 passed, 1 skipped); contract batch
  (1573 passed, 1 skipped, 1 failed); the final focused contract command
  (291 passed); explicit parallel non-MCP headless
  suite (4875 passed, 29 skipped, 8 failed, 21 errors); isolated reproduction
  of those eight failures (8 passed); API documentation (18 passed). The
  final checks passed after the source edits: `.venv/bin/python
  scripts/generate_api_artifacts.py --check` (`API artifacts are current`),
  `.venv/bin/ruff check src tests scripts/generate_api_artifacts.py` (`All
  checks passed`), `.venv/bin/python -m compileall -q src/sshpilot`, and
  `git diff --check`.
* **Failing tests:** the latest `.venv/bin/pytest -q -n0` run was manually
  interrupted after reaching a known effective-config failure, so it has no
  aggregate result and must not be called green. The focused contract batch
  before the final clean run was `1573 passed, 1 skipped, 1 failed`; the
  failure is
  `tests/test_effective_config_diff.py::test_default_config_path_excludes_system_defaults`.
  Direct reproduction shows `ssh -G foo` exits 255 with `Bad owner or
  permissions on /etc/ssh/ssh_config.d/20-systemd-ssh-proxy.conf`, so the
  test's comparison display is empty. This is an environment/test-contract
  blocker, not an excuse to claim verification. The explicit parallel non-MCP
  run completed with `4875 passed, 29 skipped, 8 failed, 21 errors` in 103.30
  seconds; those failures isolated to `8 passed` and are
  concurrency/environment/fixture-sensitive. The 21 EasyEnv errors are the
  daemon-settings fake lacking `set_plugin_setting`. GUI Docker coverage is
  explicitly skipped because the GUI environment is unavailable. These facts
  block a full practical-suite completion claim. A prior default collection
  attempt also reported missing `mcp.ClientSession` (`113 deselected, 21
  skipped, 1 collection error`); the newest default run progressed further,
  so this dependency result needs a fresh supported-environment reproduction.
* **Files currently modified:** `.codebase-memory/.gitattributes`,
  `.codebase-memory/artifact.json`, `.codebase-memory/graph.db.zst` (deleted);
  `.gitignore`; `docs/api/CHANGELOG.md`,
  `docs/api/generated/model-index.md`, `docs/api/generated/schema.json`,
  `docs/api/methods.md`; `docs/architecture.md`,
  `docs/architecture/daemon-only-retirement.md`; `scripts/generate_api_artifacts.py`;
  `src/sshpilot/api/{client.py,daemon_client.py,models/__init__.py,models/common.py,models/connections.py,models/daemon.py,transport/codec.py}`;
  `src/sshpilot/core/{connection_application_service.py,ssh_config_effective.py}`;
  `src/sshpilot/core/connections/{repository.py,ssh_config_loader.py,ssh_config_store.py}`;
  `src/sshpilot/daemon/{config_reload.py,connection_launch_provider.py,connection_secret_provider.py,dispatch.py,operation_mode_service.py,server.py}`;
  `src/sshpilot/{effective_config_check.py,preferences.py,ssh_config_utils.py,startup_info.py,terminal_manager.py,window.py,window_file_manager.py}`;
  `src/sshpilot/plugins/builtin/docker_manager/page.py`;
  `tests/api/snapshots/public_api.json`, `tests/api/snapshots/versions/0.39.json`;
  `tests/api/{test_capabilities_contract.py,test_connection_models.py}`;
  `tests/architecture/test_core_boundary.py`;
  `tests/core/{test_connection_application_service.py,test_connection_repository.py,test_ssh_config_loader.py}`;
  `tests/daemon/{test_config_reload.py,test_connection_mutations.py,test_connection_secret_provider.py,test_lifecycle_phase13_3.py,test_operation_mode_service.py,test_secret_dispatch.py}`;
  `tests/daemon/test_config_reload_coordinator.py`,
  `tests/test_effective_config_checker_generation.py`,
  `tests/{test_effective_config_diff.py,test_gui_docker_password.py,test_manage_files_ui.py,test_preferences_operation_mode.py,test_startup_behavior.py,test_window_client_composition.py,test_window_daemon_errors.py}`.
* **Current commit:** `170de28f3ad174ee80829ae6282d961d12d84bc0`; no commit has
  been created for this repair pass. Current modified/deleted files are the
  paths reported by `git status --short`; no unrelated changes were discarded.

## 12. Completion checklist

- [ ] client selection is daemon-only and readiness/capability precise
- [ ] all saved connection reads/writes and metadata/groups are daemon-owned
- [ ] effective config, unsaved-host identity, operation mode, and restore facts are daemon-owned
- [ ] secrets/keys/known hosts/transfers/forwards/internal SSH have no frontend fallback
- [ ] external terminal uses daemon-prepared non-secret SSH semantics
- [ ] obsolete production branches/settings/controllers are removed
- [ ] compatibility shims are intentional, documented, and bounded
- [ ] current docs and generated artifacts describe reality
- [ ] focused, architecture, API, integration, lint, compile, and practical suites pass
- [ ] final audit has no unexplained legacy/in-process production matches
- [ ] final evidence, exact commands, failures, and handoff are recorded here

## 13. Current decision

`NOT YET SAFE TO RETIRE`

The production authority repairs and focused regression coverage are present,
and the architecture/API/core and migration-focused suites pass in `.venv`.
The decision remains conservative because the broad practical command still
has 8 isolated-pass concurrency/environment failures and 21 EasyEnv fixture
collection errors, the optional MCP dependency prevents default collection,
and the operation-mode/effective-config/unsaved-host integration matrices are
not yet complete. No production fallback is being restored while these gates
remain open.

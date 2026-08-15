# SSH Pilot daemon-only retirement ledger

This is the live cross-session ledger for repairing and verifying the
daemon-only retirement introduced by `170de28f3ad174ee80829ae6282d961d12d84bc0`.
Read it before editing and update it before stopping. It is the current
continuation document; older migration notes are historical only.

## 1. Purpose and final architecture

The required production path is:

```text
GTK or another frontend
    -> typed SshPilotClient API
    -> daemon transport and dispatcher
    -> daemon-owned core services and repositories
    -> OpenSSH/platform adapters
```

There is no production frontend authority or fallback for saved connections,
SSH config/known-hosts, secrets, effective configuration, remote SSH/SFTP/SCP,
transfers, forwarding, operation mode, or internal remote process spawning.
Local PTY/VTE/PyXtermJS tabs, local filesystem panes, UI preferences and
dialogs, external-terminal process selection, and explicitly local plugin
commands remain legitimate frontend operations.

Starting implementation checkpoint: branch `dev`,
`abc9468bd7c59ad34ce9c561b17dbf97926e581e` (the supplied reviewed head).
The worktree was clean before this session. The live checkpoint at handoff
must always be obtained with `git rev-parse HEAD`; this document must not claim
the SHA of a future commit. Current decision: **NOT YET SAFE TO RETIRE**.

Current post-fix checkpoint: HEAD is still
`abc9468bd7c59ad34ce9c561b17dbf97926e581e` with an intentionally dirty
worktree containing only the files listed in Section 12. No commit or push was
made. The `.venv` environment is Python 3.14.4; serial GUI-capable tests are
available, but no supported display/manual GUI matrix was run.

## 2. Non-negotiable ownership invariants

1. `ConnectionPresentationStore` is read-only presentation state.
2. GTK never constructs authoritative repositories, config stores, secret
   backends, known-host stores, or core persistence services.
3. GTK never reads/writes authoritative SSH config or known-host files and
   never selects the active SSH root.
4. GTK never obtains connection passwords/passphrases through a local manager.
5. Internal SSH, SFTP, SCP, transfers, forwarding, keys, secrets, known hosts,
   effective config, unsaved-host identity, and operation mode are daemon-owned.
6. Daemon failure yields unavailable/recovery state and never selects a local
   remote backend or spawns a GTK-owned remote SSH child.
7. Existing sessions retain their launch snapshot; new work uses the daemon's
   confirmed current generation.
8. External terminals are OS-owned but receive daemon-prepared, non-secret
   launch specifications.
9. Only semantic scopes/modes cross the API; frontend filesystem paths do not.
10. Availability is based on confirmed capabilities/state, never `hasattr()`
    feature inference.

## 3. Definitions

* **Obsolete in-process backend:** production frontend code that owns or
  reconstructs authoritative SSH state, persistence, secrets, config, or
  remote processes without daemon transport.
* **Daemon-owned operation:** a typed API operation whose repository, state,
  interaction, persistence, or remote process side effect belongs to daemon
  core services.
* **Legitimate local/frontend operation:** local shell/PTY, VTE rendering,
  local panes, UI preferences/dialogs, external emulator selection, or an
  explicitly local plugin command with no remote authority.
* **Compatibility shim:** a documented thin import/facade with no manager,
  persistence, secret, process, or I/O behavior.
* **Test-only direct core invocation:** direct headless service use in unit
  tests or daemon composition; it is not a production client alternative.

## 4. Complete classified legacy-path inventory

| Classification | Discovered path | Evidence/disposition |
|---|---|---|
| production obsolete | GTK local connection CRUD/delete/post-disconnect persistence | Removed in `170de28f`; current mutation boundary tests remain required |
| production obsolete | local secret/config/known-host authority and fallback routing | Daemon API/controllers only; source audit and architecture tests required at final gate |
| production obsolete | frontend `ssh -G`, host-block collection, config-root selection | Effective-config and launch semantics are daemon/core-owned |
| production obsolete | frontend raw unsaved-host probing and unbounded local fanout | Daemon semantic check; bounded large-repository proof remains pending |
| production obsolete | frontend operation-mode path/seeding/restart authority | `OperationModeService` owns transaction and recovery result |
| production obsolete | `LegacyInProcessSshController`, `ClientMode.IN_PROCESS`, old local remote routing | No production selection remains; historical/test matches must be classified |
| daemon implementation | core application services, repositories, config reload, key/secret/known-host, transfer, SFTP and forward runtimes | Retained in daemon composition and direct core tests |
| compatibility shim/facade | `connection_manager.py`; `ssh_config_utils.py` | Model-only import shim and forwarding effective-config facade; removal windows documented |
| legitimate local | `spawn_async`, PTY/VTE/PyXtermJS, local panes, external emulator launch, local plugin commands | Retained only where side effect is explicitly local |
| test-only core usage | daemon-in-thread fixtures and direct service tests | Not a production fallback |
| stale documentation/history | old in-process references in historical docs/API snapshots | Must remain clearly historical and absent from current instructions |
| ambiguous | plugin process isolation and local terminal terminology | Requires ownership trace, not blanket deletion |

Audited search concepts include `ConnectionManager`, `connection_manager`,
`ConnectionPresentationStore`, `DaemonConnectionServices`, in-process/client
mode flags, fallback/local routing, SSH config and known-host paths, secret
methods, `ssh -G`, subprocess/PTY/spawn, effective config, operation mode,
external launch, and compatibility facades. The final audit must list every
remaining production match with one of these classifications.

## 5. Migration phases and dependency ordering

1. Baseline and source-to-side-effect audit.
2. Typed API/codec/capability and explicit API 0.40 compatibility boundary.
3. Daemon/core ownership, protected input, shared settings transactions and
   operation-mode transaction/recovery.
4. GTK/plugin state, unavailable handling, mode/key scope and reconnect.
5. Effective-config/unsaved-host/terminal launch semantic parity and bounds.
6. Remove dead obsolete branches and update compatibility/docs.
7. Focused, serial concurrency, practical non-GUI, supported GUI and final
   source audit. Retirement is not safe before every gate is evidenced.

## 6. Status table

Allowed statuses are `DISCOVERED`, `DECISION_REQUIRED`, `BLOCKED`, `READY`,
`IN_PROGRESS`, `IMPLEMENTED`, `VERIFIED`, and `REMOVED`.

| ID | Domain | Existing path | Target owner/API | Status | Tests | Last verified commit | Notes |
|---|---|---|---|---|---|---|---|
| R00 | ledger/audit | stale historical checkpoint and evidence | this ledger plus architecture docs | IN_PROGRESS | current orientation and gate results | `abc9468` | update from Git before every stop |
| R01 | startup diagnostics | removed path keys still indexed by verbose output | semantic mode/authority DTO | VERIFIED | startup tests in prior baseline; current broad rerun pending | `abc9468` | no frontend SSH root output |
| R02 | dialogs | unavailable callback/detail mismatch | typed unavailable/rejection/recovery handlers | VERIFIED | current focused UI tests | `abc9468` | conflict details retained |
| R03 | watcher | startup did not schedule initial semantic reload | watcher registration plus debounced reload | VERIFIED | `tests/daemon/test_config_reload_coordinator.py` | `abc9468` | mode refresh still rediscover paths |
| R04 | Docker credentials | use-once could persist or stale session value hide retry | daemon session credential plus explicit store | IN_PROGRESS | Docker focused tests pass; supported GUI retry pending | `abc9468` | user-consent and auth-failure matrix remains |
| R05 | effective-config errors | external-terminal handler used for comparison failures | operation-specific GTK error routing | VERIFIED | effective-config focused tests | `abc9468` | unavailable result is not daemon-unavailable |
| R06 | operation mode | rollback/missing persistence could claim healthy | transactional runtime/persisted/recovery result | IN_PROGRESS | operation-mode suite plus new missing-persisted regression pass | `abc9468` | full fault/restart matrix pending |
| R07 | mode RPCs | radio projection re-entered toggle | suppression and in-flight guard | VERIFIED | `tests/test_preferences_operation_mode.py` | `abc9468` | recovery keeps controls disabled |
| R08 | effective resolver | overlapping effective-config implementations | canonical GTK-free core resolver | IMPLEMENTED | architecture/core/effective generation tests | `abc9468` | parity audit still pending |
| R09 | alias safety | leading dash could be interpreted by OpenSSH | API/repository/resolver validation | VERIFIED | API/core alias tests | `abc9468` | imported invalid data fails closed |
| R10 | unsaved identity/effective config | raw token/exact-user/implicit port semantics and stale closed-client checker RPC | daemon-resolved semantic identity plus generation-scoped effective-config cache | IN_PROGRESS | effective-config generation/reconnect subset passes; closed-client regression added | `abc9468` + worktree | large repository and full semantic matrix pending |
| R11 | settings/cache | stale frontend cache and direct reload assignment | canonical reload plus cross-process transaction | IN_PROGRESS | architecture/core/API pass; backup caller repaired | `abc9468` | every shared writer still needs final inventory |
| R12 | API compatibility | changed wire behavior under 0.39 | API 0.40 and immutable 0.39 history | VERIFIED | API/generator checks pass | `abc9468` | bidirectional stale-daemon matrix pending |
| R13 | protected input | broadcast not registered; weak lifecycle | peer-owned bounded protected input | VERIFIED | real broadcast transport plus owner/duplicate/size/TTL/disconnect tests pass | `abc9468` + worktree | full independent-client transport fuzz matrix remains a risk |
| R14 | reconnect | partial client rebinding/publication, stale Preferences SSH-overrides controller, lazy mode-page reset crash, and delayed old-client transport callback | prepare/commit replacement, generation guards, explicit unavailable detachment, and source-client transport filtering | IN_PROGRESS | reconnect + Preferences/mode regression: `42 passed` | `abc9468` + worktree | stale post-reconnect transport loss is ignored; open Preferences/in-flight service matrix remains pending |
| R15 | temporary credentials | stale session shadowed correction/deletion | replace/clear/wipe daemon credential lifecycle | IMPLEMENTED | provider/dispatch tests pass | `abc9468` | Docker auth retry and memory proof pending |
| R16 | external terminal | capability/handler provider contract | conditional dispatcher capability and typed launch spec | VERIFIED | dispatcher/handshake/client integration tests | `abc9468` | GTK only chooses emulator |
| R17 | key scope | default key manager before mode confirmation | no key manager until confirmed semantic mode | VERIFIED | delayed startup/key tests | `abc9468` | reconnect confirmation tied to R14 |
| R18 | file manager | external setting risked frontend remote path | daemon-backed standalone/embedded SFTP window | VERIFIED | Manage Files/SFTP tests | `abc9468` | local panes remain local |
| R19 | docs/artifacts | stale changelog/version/ledger claims | current API/docs and truthful ledger | VERIFIED | changelog, generator, Ruff, compile, diff checks pass | `abc9468` + worktree | ledger updated with stale SSH-overrides controller repair |
| R21 | event transport | one daemon-wide event sequence advanced for peer-filtered interaction events | peer-scoped visible-event sequence with handshake baseline; retain global diagnostic counter | IMPLEMENTED | event backpressure/forwarding/API protocol suite: `31 passed` | `abc9468` + worktree | prevents false `protocol_error` and reconnect after an interaction event hidden from a peer |
| R22 | protocol diagnostics | unsolicited daemon protocol rejection was reported as an unknown response and reconnect logs dropped the reason | preserve sanitized reserved-error explanation through `DaemonClient` and include it in transport-loss diagnostics | IMPLEMENTED | transport/event/reconnect suite: `47 passed` | `abc9468` + worktree | required to distinguish a stale daemon or binary-frame violation from an event continuity defect |
| R23 | operation-mode status wire compatibility | a recovery result without the additive rollback flag could fail client model validation and be mislabeled as transport protocol failure | infer `rollback_completed=False` when `recovery_required=True` and the flag is absent; preserve decode detail | IMPLEMENTED | operation contract/client/event/reconnect subset: `15 passed`; production-core smoke returned truthful recovery status | `abc9468` + worktree | prevents a secondary validation error during startup recovery |
| R24 | operation-mode/reconnect UI | healthy status used an empty success message that strict decoding rejected; reconnect subscription failures hid the candidate error detail | allow empty success message; log subscription error code/detail while retaining prepare/commit rejection | IMPLEMENTED | operation-mode/reconnect/UI subset: `31 passed`; production-core healthy status smoke passed | `abc9468` + worktree | candidate is still never published unless event subscription succeeds |
| R20 | final retirement | prior verdict overstated evidence | executable end-to-end trace and honest verdict | READY | broad and supported GUI gates pending | `abc9468` | cannot mark safe |

## 7. End-to-end contract audit

| Domain | Frontend entry/GUI state | Typed guard/dispatcher | Handler/service side effect | config/cache/restart contract | Evidence/status |
|---|---|---|---|---|---|
| client/readiness | startup/reconnect availability | handshake/API/capability checks | launcher and daemon transport | mismatch is restart-required; no local client | focused pass; broad pending |
| saved connections/groups | dialogs/sidebar projections | CRUD/metadata/group capabilities | application service/repository | generation/events refresh projection; restart reloads repository | core/API pass |
| secrets/keys | dialogs, Docker, interaction presenter | secret/key capabilities and protected frames | daemon provider/broker/key service | persistent writes locked; session memory clears on restart | focused pass; GUI pending |
| SSH config/known hosts | render daemon DTO/text | config/known-host capability | stores and reload coordinator | daemon owns root/includes/watch generation | focused pass; final trace pending |
| effective config | checker/dialog generation cache | effective-config capability | canonical resolver/OpenSSH | daemon generation invalidates stale result | generation tests pass |
| unsaved host | optional post-connect prompt | semantic check capability | daemon identity resolver | omitted port/user provenance preserved; bounded work pending | focused pass; large repo pending |
| operation mode | Preferences confirmed radios/key scope | mode get/set capability | mode service, repository, watchers | lock covers read/modify/write; runtime/disk/UI/restart agree or recovery | focused pass; fault matrix pending |
| backup/restore | semantic options and warning | backup/restore API | daemon transfer/backup service | daemon reports mode facts; restore transaction lock | targeted pass; final aggregate pending |
| internal SSH/session | terminal tabs/readiness | session/terminal capabilities | daemon session runtime | existing launch snapshot retained; no local remote child | ownership tests |
| external terminal | emulator selection only | `terminal.external_launch` if provider exists | daemon launch provider prepares argv/env | alias/isolated semantics daemon-owned | integration pass |
| SFTP/SCP/transfers/forwards | file manager and operation UI | typed capabilities | daemon runtimes and operation lifecycle | remote process/operation state daemon-owned | focused pass |
| plugins | Docker and SDK UI | plugin/settings/session APIs | daemon remote operations; local commands explicit | shared settings transactions preserve unrelated values | focused pass; GUI pending |
| reconnect/shutdown | app lifecycle and Preferences | lifecycle/status/reconnect | daemon cleanup and replacement lifecycle | stale callbacks rejected by client generation | focused pass; full matrix pending |
| local features | local terminal/pane/UI preferences | no remote capability | frontend-only | no authoritative SSH/config/secret side effect | local tests; GUI pending |

## 8. Decision log

| Date | Decision | Alternatives considered | Reason/affected paths |
|---|---|---|---|
| 2026-08-16 | Keep API implementation version 0.40 and historical 0.39 immutable | rewrite 0.39 or downgrade secrets | protected input/session credentials/mode/unsaved-host changes are incompatible; stale peers need restart |
| 2026-08-16 | Shared settings use a GTK-free cross-process transaction lock and baseline merge | RLock only, reload immediately before write, stale whole-tree replacement | GTK and daemon are separate processes; lock must cover complete read/modify/write |
| 2026-08-16 | Broadcast protected input is registered explicitly and peer-owned | global request-id dictionary/plaintext JSON | dispatcher must be able to await protected input without weakening ownership |
| 2026-08-16 | Missing or inconsistent persisted mode is recovery-required, including failed transitions before publication | ordinary rejection or silently defaulting | runtime/disk truth must agree and restart must not surprise the user |
| 2026-08-16 | Reconnect publishes only after dependent rebinding succeeds | assign selection first and repair controllers later | prevents mixed old/new client graph; failed candidate is closed |
| 2026-08-16 | Rejected mode results do not confirm or apply a new mode | apply daemon active mode on every response | only accepted, consistent results may change key scope/UI confirmation |
| 2026-08-16 | Leading-dash aliases are OpenSSH option ambiguity, rejected at typed/repository boundaries | treat as shell injection or rely on `--` | argv has no shell, but OpenSSH option parsing remains unsafe |
| 2026-08-16 | Omitted CLI port remains absent; explicit 22 remains explicit | parser default 22 as user input | preserve OpenSSH alias Include/Match semantics |
| 2026-08-16 | External file manager remains daemon-backed SFTP | restore GVFS/frontend SSH | preserve feature without reviving remote authority |

Unsaved-host identity rule: a saved internal connection ID is authoritative
when present. Ephemeral CLI connections do not gain a durable ID merely because
nickname equals hostname. Otherwise the daemon compares canonical SSH semantic
identity: case-normalized host/destination, empty username resolved as the
daemon's local login, explicit/default port provenance, effective User and
ProxyJump from Host/Match/Include, and protocol. Display-name/rename does not
change an internal-ID match. Exact decision-table coverage remains R10.

## 9. Compatibility and deprecation decisions

`connection_manager.py` remains a model-only `Connection`/`ConnectionState`
import shim for one documented plugin/API compatibility window. Owner: API and
plugin maintainers. Remove after the next incompatible plugin/API window and a
deprecation release note; it must never gain persistence or I/O.

`ssh_config_utils.py` is a forwarding facade for effective/Include resolution;
specialized editor/write helpers are retained. Remove the facade when all
production callers use the canonical core resolver or dedicated editor module.

API 0.39 is not a supported mixed-version peer for changed 0.40 operations.
Handshake/launcher/client paths must preserve typed `API_VERSION_MISMATCH` as a
restart-required outcome, must not send plaintext secrets, and must not kill a
daemon with live resources automatically. Only current 0.40 artifacts may be
regenerated; `tests/api/snapshots/versions/0.39.json` is immutable.

## 10. Test and verification matrix

| Area | Exact command/result |
|---|---|
| Permanent checkpoint reproductions | `.venv/bin/pytest -q -n0 tests/test_daemon_retirement_current_checkpoint.py tests/daemon/test_daemon_retirement_contract.py` → `8 passed` after fixes; baseline on `abc9468` was `1 passed, 7 failed` |
| Event continuity after filtered interaction events | `.venv/bin/pytest -q -n0 tests/daemon/test_event_backpressure.py tests/daemon/test_event_forwarding.py tests/api/test_daemon_client_protocol.py` → `31 passed` |
| Protocol rejection diagnostics/startup burst/reconnect | `.venv/bin/pytest -q -n0 tests/api/test_daemon_client_protocol.py tests/daemon/test_event_backpressure.py tests/daemon/test_event_forwarding.py tests/test_daemon_reconnect_gtk.py` → `47 passed` |
| Operation-mode recovery wire compatibility | `.venv/bin/pytest -q -n0 tests/api/test_operation_contract.py tests/api/test_daemon_client_protocol.py tests/daemon/test_event_forwarding.py::test_startup_request_burst_survives_idle_lifecycle_event` → `15 passed`; production-core smoke exercised `DaemonClient.get_operation_mode()` and returned a typed recovery result |
| Healthy operation-mode/reconnect regression | `.venv/bin/pytest -q -n0 tests/api/test_operation_contract.py tests/test_daemon_reconnect_gtk.py tests/test_preferences_operation_mode.py tests/daemon/test_event_forwarding.py::test_startup_request_burst_survives_idle_lifecycle_event` → `31 passed`; production-core smoke returned `True default ''` |
| Focused daemon/UI/API regressions | `.venv/bin/pytest -q -n0 tests/daemon/test_secret_dispatch.py tests/daemon/test_broadcast_service.py tests/daemon/test_operation_mode_service.py tests/test_daemon_reconnect_gtk.py tests/api/test_daemon_client_protocol.py tests/daemon/test_config_reload_coordinator.py tests/test_preferences_operation_mode.py tests/test_effective_config_checker_generation.py tests/test_gui_docker_password.py tests/test_docker_manager_plugin.py tests/test_docker_plugin.py` → `204 passed, 25 skipped` (split runs: `69 passed`, `135 passed, 25 skipped`) |
| Architecture/core/API | `.venv/bin/pytest -q -n0 tests/architecture tests/core tests/api` → `1453 passed, 1 skipped` |
| Hygiene | `git diff --check` passed; `.venv/bin/ruff check .` passed; `.venv/bin/python -m compileall -q src tests` passed; generator `--check` reported `API artifacts are current.` |
| Full non-GUI suite | `find tests -type f -name 'test_*.py' ! -path 'tests/mcp/*' -print0 | xargs -0 .venv/bin/pytest -q -n auto -m 'not integration and not gui'` → `4937 passed, 31 skipped, 8 failed`; all 8 rerun serially in relevant subsets → pass; parallel failures are GTK/askpass/system-config isolation artifacts and remain non-green parallel evidence |
| Serial focused replacements | `.venv/bin/pytest -q -n0` focused aggregate → `276 passed, 25 skipped`; file-manager/terminal subset → `26 passed`; askpass/effective/terminal subset → `31 passed` |
| Supported GUI/manual matrix | Not run in this session; supported display, Unix socket, secret backend and system binaries must be documented before any safe verdict |

## 11. Known failures, blockers and unresolved questions

* The complete parallel non-GUI suite is not green: it recorded 8 failures.
  Each passes in serial. The failures are reproducible under parallel workers
  in tests that share process-global GTK stubs, askpass staging/socket state,
  or system OpenSSH config fixtures. Classification: test/environment
  isolation and concurrency, not product behavior in the serial supported
  environment; nevertheless this blocks claiming a broadly green suite until
  the parallel contract is accepted or those tests are isolated.
* Supported-environment GUI verification is outstanding and blocks retirement.
* R11 still needs a final inventory of every production `Config.set_setting`,
  `save_json_config`, backup/restore/import/reset and plugin helper writer;
  the canonical reload and backup merge fixes do not by themselves prove all
  ownership is daemon-exclusive.
* R06 needs the complete failure matrix, immediate `status()` agreement and
  restart proof for malformed/missing settings, publication/rollback failures,
  cleanup failure and concurrent external modification.
* R14/R15 need real open-Preferences/in-flight reconnect and Docker wrong-
  session-password retry evidence, including no secret in logs/DTOs/errors.
* A closed daemon client could remain in the open Preferences SSH-overrides
  controller. The page previously logged a warning and fell back to local
  Config values. This is repaired in the worktree: closed reads now return
  disabled presentation placeholders only, transport loss detaches the
  controller, and saves are rejected while unavailable. The focused proof is
  `tests/test_preferences_ssh_overrides.py` plus the transport-loss assertion
  in `tests/test_daemon_reconnect_gtk.py`.
* Reconnect could call the Preferences mode reset before its lazy
  operation-mode radios existed, aborting the replacement. Reset, projection,
  unavailable, and radio helpers now tolerate an unbuilt page and defer the
  status RPC until the page is built. The regression is
  `test_reconnect_reset_is_safe_before_operation_mode_page_is_built`.
* A delayed transport-loss callback from the replaced client could arrive after
  a successful reconnect and trigger a second reconnect. Transport callbacks
  now carry their source client and are ignored unless that client is current.
  Regression: `test_stale_client_transport_loss_after_reconnect_is_ignored`.
* The effective-config worker could already have selected the old client when
  reconnect closed it, producing a traceback-level debug log. It now skips
  closed/transport-failed clients and treats expected transport errors as
  normal stale cancellation. Regression:
  `test_checker_does_not_query_an_already_closed_daemon_client`.
* A daemon-wide event sequence was advanced for interaction events that were
  correctly filtered from peers without interaction ownership. Those peers
  then reported a false sequence gap as `protocol_error`, closed their
  transport, and triggered reconnect/listing warnings. Event delivery now
  assigns sequence numbers per peer over visible events only, initializes a
  newly handshaken peer at the current stream position, and retains the global
  counter only for server diagnostics. Regression coverage is the `31 passed`
  event backpressure/forwarding/API protocol command in Section 10.
* A reserved daemon protocol-error response used request id `protocol`, but
  the client treated it as an unknown response and the application reduced it
  to a code-only reconnect warning. The client now preserves the daemon's
  sanitized explanation and the application logs it. The startup burst/lifecycle
  regression and transport diagnostic are covered by the `47 passed` command
  in Section 10. A resident daemon must still be restarted after source changes
  so the running daemon actually contains the repaired event dispatcher.
* The daemon returned an operation-mode recovery payload that the client could
  reject while constructing `OperationModeResult` if the older additive
  `rollback_completed` field was absent. The codec now derives the only
  truthful value (`False`) when `recovery_required` is true, and client decode
  failures retain the underlying safe field/type explanation. This keeps
  startup recovery explicit instead of converting it into a reconnect loop.
* A healthy status result has an intentionally empty `message`; strict generic
  text decoding rejected it and caused the same reconnect loop. Operation-mode
  messages now allow empty success text while still requiring non-empty
  rejection/recovery explanations at the service boundary. Reconnect
  subscription failures now include the typed error code/detail in logs; the
  candidate remains unpublished and is closed on failure.
* R10 needs bounded large-repository unsaved-host and effective-config work
  evidence plus full Include/Match/ProxyJump parity.
* Optional MCP tests may be unavailable if the environment lacks
  `mcp.ClientSession`; this is an environmental dependency blocker, not a
  reason to weaken tests.

## 12. Session handoff

* Last completed action: reproduced and repaired the closed-client
  SSH-overrides warning and the preloaded-Preferences reconnect crash,
  removed the forbidden local Config fallback, added unavailable-state,
  transport-loss, and lazy-mode-page regression coverage, and reran the
  focused reconnect/mode/Preferences tests (`53 passed`), then reran the
  checkpoint, real broadcast transport, mode, Preferences, and reconnect
  gate (`49 passed`), then repaired the stale post-reconnect transport-loss
  callback and stale effective-config worker logging, and reran effective-config,
  reconnect, and mode tests (`28 passed`), then repaired the peer-filtered
  daemon event sequence continuity defect behind the reported `protocol_error`
  reconnect loop and reran event backpressure, event forwarding, and
  daemon-client protocol tests (`31 passed`), then preserved unsolicited
  daemon protocol rejection details, added a startup-RPC/lifecycle-event
  regression, fixed recovery-mode decoding when the additive rollback flag is
  absent, and reran the combined transport/reconnect suite (`47 passed`) plus
  the operation-mode contract subset (`15 passed`). A production-core smoke
  then exercised `DaemonClient.get_operation_mode()` and returned a typed
  recovery result instead of closing the transport.
  Earlier completed
  work reproduced seven
  required checkpoint failures,
  repaired them, added real broadcast protected-input transport coverage,
  added bounded/expiry/disconnect protected-input tests, fixed reconnect
  publication and generation guards, corrected Preferences recovery state,
  repaired direct shared-config reload/backup merge paths, and passed the
  serial architecture/core/API gate.
* Current action: preserve the truthful NOT YET SAFE verdict while reviewing
  the eight parallel-only failures and completing the final source-to-side-
  effect audit. The reported reconnect loop now has focused regression
  evidence: ownership-filtered events no longer create client sequence gaps.
  Restart the resident daemon and reproduce once; use the new `detail=` field
  in the reconnect warning to classify any remaining protocol violation.
  The current source-level operation-mode recovery path is verified against a
  production-core smoke daemon.
* Exact next command:

  ```bash
  git status --short --branch && git rev-parse HEAD && git diff --check
  ```

* Commands already run this session: `git status --short`, `git rev-parse
  HEAD`; `.venv/bin/pytest -q -n0 tests/test_preferences_ssh_overrides.py
  tests/test_preferences_operation_mode.py tests/test_daemon_reconnect_gtk.py
  tests/test_window_client_composition.py` → `53 passed`; serial
  `.venv/bin/pytest -q -n0 tests/architecture tests/core tests/api` →
  `1453 passed, 1 skipped`; full Ruff/compile/generator/diff checks; baseline
  and post-fix permanent tests; final gate `.venv/bin/pytest -q -n0
  tests/test_daemon_retirement_current_checkpoint.py
  tests/daemon/test_daemon_retirement_contract.py
  tests/test_preferences_operation_mode.py tests/test_preferences_ssh_overrides.py
  tests/test_daemon_reconnect_gtk.py` → `49 passed`; reconnect/mode/Preferences
  rerun → `42 passed`; effective-config/reconnect/mode rerun → `28 passed`; final Ruff,
  compileall, API artifact check, and diff check all passed; event
  backpressure/forwarding/API protocol tests (`31 passed`), combined
  transport/reconnect suite (`47 passed`); broad parallel
  non-GUI and supported GUI results remain as recorded above.
  tests; focused secret/broadcast,
  operation-mode, reconnect, API, reload, Preferences, effective-config and
  Docker tests; `git diff --check`; Ruff; compileall; API artifact check; and
  serial `tests/architecture tests/core tests/api`.
* Current modified files, obtained from Git at this handoff, are:

  ```bash
  git status --short
  git diff --name-only
  ```

  `docs/api/CHANGELOG.md`, `docs/architecture/daemon-only-retirement.md`,
  `src/sshpilot/backup_manager.py`, `src/sshpilot/daemon/dispatch.py`,
  `src/sshpilot/daemon/launcher.py`,
  `src/sshpilot/daemon/operation_mode_service.py`,
  `src/sshpilot/daemon/secret_transfer.py`, `src/sshpilot/daemon/server.py`,
  `src/sshpilot/api/daemon_client.py`, `src/sshpilot/api/transport/codec.py`,
  `src/sshpilot/effective_config_check.py`,
  `src/sshpilot/main.py`,
  `src/sshpilot/preferences.py`,
  `src/sshpilot/window.py`, `src/sshpilot/window_dialogs.py`,
  `tests/api/test_daemon_client_protocol.py`,
  `tests/api/test_operation_contract.py`,
  `tests/daemon/test_event_backpressure.py`,
  `tests/daemon/test_event_forwarding.py`,
  `tests/daemon/test_secret_dispatch.py`,
  `tests/test_daemon_reconnect_gtk.py`,
  `tests/test_effective_config_checker_generation.py`,
  `tests/test_preferences_operation_mode.py`,
  `tests/test_preferences_ssh_overrides.py`, plus new untracked
  `tests/daemon/test_daemon_retirement_contract.py` and
  `tests/test_daemon_retirement_current_checkpoint.py`.
* No commit or push was created. HEAD remains the supplied checkpoint until a
  user explicitly requests a commit. Do not put a future commit SHA into this
  ledger; record it only after the commit exists.

## 13. Completion checklist

- [x] Production client selection remains daemon-only.
- [x] Required checkpoint reproductions are permanent and pass after fixes.
- [x] Protected broadcast input reaches the daemon handler over real transport.
- [x] Startup reload, dialog details, API 0.40 docs/artifact checks and
      operation-mode radio suppression remain covered.
- [ ] All shared JSON readers/writers have one authoritative ownership model.
- [ ] Reconnect replacement covers every client-backed object and open
      Preferences/in-flight callbacks.
- [ ] Operation-mode failure/restart/concurrency matrix is complete.
- [ ] Temporary credential retry/clear/wipe and Docker GUI matrix is complete.
- [ ] Unsaved-host/effective-config semantic and bounded-work matrix is complete.
- [ ] Full non-GUI suite and supported GUI/manual matrix are green and recorded.
- [ ] Final audit has no unexplained prohibited production in-process matches.
- [ ] Retirement verdict is supported by reproducible evidence.

**Current final decision: NOT YET SAFE TO RETIRE.**

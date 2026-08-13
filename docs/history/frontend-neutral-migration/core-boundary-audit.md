# Core boundary audit

This audit records the pre-daemon architecture as observed on 2026-07-28. It is
descriptive: production SSH, PTY, SFTP, secret, plugin, and terminal behaviour
was not moved while producing it.

For the exact current contract, see the maintained
[API reference](../../api/README.md). The superseded implementation inventory
is preserved as [API implementation audit](api-implementation-audit.md).

## Implementation map

The first migration slice is:

```text
WelcomePage Recent list
    -> SshPilotClient.list_connections()
    -> InProcessClient
    -> ConnectionManager.get_connections()
```

Terminal activation still resolves the selected DTO back to the existing
`Connection` object and enters `TerminalManager.connect_to_host()`. This keeps
terminal execution out of the milestone.

New implementation files live under `src/sshpilot/api/`. The GTK composition
root creates one `InProcessClient` beside its existing managers. Contract tests
live under `tests/api/`.

## Responsibility inventory

Classification uses `CORE`, `FRONTEND`, `TRANSPORT`, and
`MIXED_NEEDS_SPLIT`.

| Current component | Current responsibilities | GTK/VTE coupling | Concurrency model | Classification / target owner | Proposed service/API | Migration risk |
| --- | --- | --- | --- | --- | --- | --- |
| `connection_manager.Connection` | Parsed connection fields, native SSH preparation, compatibility connection state, old async disconnect data | Imports no widgets, but its state is driven by terminal widgets; keeps asyncio loop/tasks/listeners | Three coroutines; loop obtained during construction | `MIXED_NEEDS_SPLIT`: connection data and command preparation to core; runtime session state to a session service | Connection records plus session service | High: object identity keys active tabs and rows |
| `connection_manager.ConnectionManager` | SSH-config parsing/writing, non-SSH persistence, credential façade, connection lifecycle signals | Subclasses `GObject.Object`; emits UI-consumed signals and schedules with `GLib.idle_add` | Synchronous file I/O plus GLib-delivered signals | `MIXED_NEEDS_SPLIT`: persistence/validation/core events to core; GObject adapter to frontend | Connection service and a GTK event adapter | High: `~/.ssh/config` is authoritative and writes are transactional |
| `ssh_connection_builder` | Single SSH/auth command path and askpass environment | Frontend-neutral | Synchronous subprocess/config helpers | `CORE` | SSH command/auth service | Critical security path; reuse unchanged |
| `session_manager.SessionManager` | Saves/restores named tab-layout snapshots and “previous session” | Data is frontend layout despite the generic name | Synchronous JSON/config reads and writes | `FRONTEND` for current class; do not treat as runtime session service | Rename only in a later compatibility phase; create a separate core session service | Medium: terminology currently overlaps future daemon sessions |
| `ssh_process_manager.SSHProcessManager` | Tracks terminal widgets, polls OS processes, cleans orphaned SSH children | Stores terminal widget references and invokes widget `disconnect()` | Singleton, cleanup thread, lock, polling | `MIXED_NEEDS_SPLIT`: PID/process ownership to core; widget registry removed | Process/session lifecycle service | High: shutdown and orphan detection |
| `identity.IdentityManager` and providers | Select identity provider and inject agent environment/config directives | No GTK widgets | Synchronous registry and provider calls | `CORE` | Identity service | Medium: environment mutation must remain centralized |
| `terminal_manager.TerminalManager` | Creates tabs/widgets, unlock presentation, connection orchestration, colors, terminal lookup, reconnect dialogs/state | Direct window, GTK, Adwaita, VTE/PyXtermJS, dialogs, tabs, groups, plugin host | GTK callbacks, threads, `GLib.idle_add`, GLib timers, and `run_until_complete` | `MIXED_NEEDS_SPLIT`: presentation stays frontend; session/reconnect/process policy moves core | GTK terminal controller over session API | Very high: largest cross-domain coordinator |
| `terminal.TerminalWidget` | VTE/PyXterm rendering, PTY spawn, SSH evidence detection, process exit classification, reconnect, askpass log wiring, session flags | Is a GTK widget and owns VTE/backend objects | GLib sources, worker threads, child callbacks, polling, limited asyncio bridging | `MIXED_NEEDS_SPLIT`: renderer/frontend versus PTY/process/session/core | Terminal renderer adapter plus session API | Very high: current PTY and state source of truth |
| `terminal_backends` / `xterm_pty_bridge` | VTE/PyXterm adapters, PTY bridge, batching and WebKit integration | Explicit VTE/WebKit/GTK coupling | GLib fd/source watches, child watches, timers | `MIXED_NEEDS_SPLIT`: renderer frontend; portable byte framing concepts core/transport | Terminal stream adapter | High |
| `file_manager.openssh_backend.OpenSSHSFTPManager` | Builds headless native SSH/SFTP path, authentication, reconnect/retry, manager façade | Core backend has little widget coupling, but some callback surfaces serve GTK | Worker threads, `ThreadPoolExecutor`, locks, request queues | `CORE` after callbacks become core events | SFTP service | High: authentication and multiplexing must keep using the shared SSH path |
| `file_manager.openssh_backend.OpenSSHSFTPClient` | SFTP v3 packets over subprocess streams | Frontend-neutral | Dedicated reader thread, request IDs, locks and pending requests | `CORE` | SFTP protocol implementation | Medium |
| `file_manager.pane`, progress/properties dialogs, `file_manager_window` | Navigation, selection, transfer UI, dialogs and rendering | GTK/Adwaita | Worker callbacks bridged via GLib; render polling | `FRONTEND`, with operation execution later behind SFTP API | GTK SFTP controller | Medium |
| `secret_storage.SecretManager` and backends | Backend selection, lookup/store/delete, vault session lock/unlock | Core module is GTK-free; unlock presentation is separate | Locks and subprocess calls; session timeout state | `CORE` | Secret service | Critical security path; never expose provider objects or values |
| `secret_unlock_dialog`, shared password dialogs | Presents credential/unlock interactions | GTK/Adwaita by design | Worker completion returns through GLib | `FRONTEND` | Interaction presenter | High: modal parenting and secret redaction |
| `config.Config` | Persistent core settings and frontend preferences in one JSON tree; GObject change signal | Subclasses GObject and is used directly by many widgets | Synchronous I/O and GObject signals | `MIXED_NEEDS_SPLIT`: namespace settings by core/frontend ownership | Settings service plus frontend preference store | High: broad fan-in and compatibility keys |
| `groups.GroupManager` | Persistent groups and connection membership/order | No widget dependency, but stores presentation ordering/color/expanded state together | Synchronous config writes | `MIXED_NEEDS_SPLIT`: membership/name core; expanded/color/order ownership must be explicitly versioned | Group service plus frontend presentation metadata | Medium |
| `plugins.api.PluginContext` | Scoped connection, secret, settings, UI, command, and spawn capabilities | Intentionally avoids exposing raw windows, but includes registered UI abilities and frontend callbacks | Threads/locks for streams and commands | `MIXED_NEEDS_SPLIT`: core plugin execution versus frontend contribution API | Core plugin service and separate frontend extension host | High: existing plugin compatibility |
| `plugins.host.PluginHost` / `UiHost` | Event bus, UI contribution host, opens tabs/terminals, dispatches session events with terminal objects | Bound to the first live window; exposes frontend objects internally | GObject/GTK callbacks and GLib dispatch | `MIXED_NEEDS_SPLIT` | Core plugin event service plus GTK UI host | High |
| `main.SshPilotApplication` | Process composition, resources, logging, actions, shutdown | GTK/Adwaita application root | GTK main loop and GLib timers | `FRONTEND` composition root today | Compose `InProcessClient`; later choose `DaemonClient` | Medium |
| `window.MainWindow` and window mixins | Own managers, tabs, sidebar, dialogs, session-layout capture, terminal maps, config monitors | Entirely GTK/Adwaita/VTE-facing | GTK main loop, worker callbacks via GLib, file monitors | `FRONTEND`, while remaining direct manager access is migration debt | GTK controllers consuming `SshPilotClient` | High but incremental |
| `sidebar.ConnectionRow` / `GroupRow` | Connection/group presentation, status, DnD, filters, context menus | GTK/GObject | GTK signals and timers | `FRONTEND` | DTO-based sidebar controller | Medium |
| `welcome_page.WelcomePage` | Start-page presentation, Recent/Pinned shortcuts | GTK/Adwaita | GTK signal callbacks | `FRONTEND` | First DTO consumer | Low; Recent read slice migrated |

## Concurrency inventory

Counts below are repository-wide lexical occurrence counts in production Python
under `src/sshpilot/`, captured before the new `src/sshpilot/api/` package was
added. They describe the migration baseline, not the small locks used by the
new event publisher. A count means the named construct matched; it does not
imply that many distinct threads or live tasks. Locations list every file
containing a match.

| Mechanism | Matches | Locations |
| --- | ---: | --- |
| `threading` | 110 | `ssh_process_manager.py`, Docker manager page, OpenSSH SFTP backend, secret/setup dialogs, connection dialog, `ssh_multiplex.py`, plugin host/API, preferences, terminal/manager, askpass, update checker, autocomplete, effective config checker, file-manager properties, main, SCP/SFTP utilities, window, dialog helpers |
| `Thread(...)` constructors | 50 | 23 production files, dominated by terminal, SFTP, secret/setup, preferences, plugin, dialog, and effective-config paths |
| Executors | 2 | `file_manager/openssh_backend.py` |
| `subprocess.*` | 153 | secret storage, plugin API, platform/askpass helpers, SCP, multiplexing, terminal, agent client, OpenSSH SFTP backend, identity provider, config utilities, validators, startup/WoL/fingerprint helpers |
| `GLib.idle_add` | 231 | 43 files; concentrated in terminal manager/widget, window, connection manager/dialogs, file manager, plugins, preferences and setup flows |
| `GLib.timeout_add*` | 65 | 23 files; terminal/window/sidebar, transfers, shutdown, xterm bridge/prewarm, Docker manager and log/file UI |
| `GLib.io_add_watch` | 0 | None by exact spelling; xterm uses other GLib source/watch helpers |
| GObject `__gsignals__` declarations | 14 | config, OpenSSH SFTP backend, command blocks, terminal, connection dialog/manager, key manager, sidebar, file-manager pane/controllers |
| `.emit(...)` calls | 88 | config, connection manager, OpenSSH SFTP, terminal, command blocks, dialogs, panes, plugin host, editor, key manager, sidebar |
| `asyncio` occurrences | 26 | `connection_manager.py`, `terminal_manager.py`, `terminal.py`, `main.py` |
| `async def` | 3 | all in `connection_manager.Connection` |
| `await` | 3 | all in `connection_manager.Connection` |
| Event-loop creation/access | 9 | connection manager, terminal manager and terminal widget |
| `asyncio.run(...)` | 0 | None |
| `run_until_complete(...)` | 4 | terminal manager and terminal widget |
| Queues/deques | 9 | Docker logs/page, log viewer, autocomplete, effective-config checker, OpenSSH SFTP, file-manager progress |
| Locks/RLocks | 18 | SSH process manager, effective-config checker, OpenSSH SFTP, plugin API, secret storage, multiplexing, autocomplete, askpass |
| Conditions | 0 | None by exact `threading.Condition`/`asyncio.Condition` spelling |
| Cancellation primitives/calls | 15 | connection manager, file manager/window/progress, log viewer, OpenSSH SFTP, editor, window, askpass |

### Dominant and incidental mechanisms

The dominant model is the GTK/GLib main loop with blocking work delegated to
plain worker threads and results marshalled back with `GLib.idle_add`.
`GLib.timeout_add*` supplies UI refresh, connection evidence, reconnect delay,
shutdown, and polling.

Asyncio is incidental rather than application-wide. Its only coroutines are on
`Connection`; terminal code sometimes obtains an event loop and blocks with
`run_until_complete`. There is no `asyncio.run()` call and no long-lived,
explicitly owned daemon event-loop thread. Therefore protocol v1 uses
synchronous commands plus event subscriptions. Adding an async client contract
now would not match the runtime.

### Concrete cross-boundary call chains

Current mixed chains include:

```text
GTK activation
  -> TerminalManager.connect_to_host
  -> construct TerminalWidget
  -> GLib.idle_add(_set_terminal_colors)
  -> Connection.native_connect coroutine
  -> loop.run_until_complete(...)
  -> TerminalWidget._connect_ssh
  -> worker/child callbacks
  -> GLib callbacks and GObject signals
```

```text
TerminalWidget spawn/evidence callback
  -> ConnectionManager.update_connection_state
  -> mutate ConnectionState immediately
  -> GLib.idle_add(manager.emit, ...)
  -> sidebar/window/plugin listeners on GTK thread
```

```text
GTK SFTP action
  -> file-manager worker
  -> OpenSSHSFTPManager/OpenSSHSFTPClient
  -> subprocess streams + dedicated reader thread
  -> request-id wakeup/queue
  -> GLib.idle_add or fixed-cadence GTK progress timer
```

```text
GTK vault action
  -> worker thread / backend subprocess
  -> SecretManager session state
  -> GLib.idle_add
  -> shared unlock or error presentation
```

### Main-thread blocking and loop dependencies

- `TerminalManager.connect_to_host()` can call `run_until_complete()` while
  preparing a native SSH command. Comments in `Connection.native_connect()`
  already acknowledge that the GLib loop is blocked and defer key preload to a
  worker thread.
- Connection config loading and many `Config` reads are synchronous. Initial
  construction calls `ConnectionManager.set_isolated_mode()` and
  `load_ssh_config()` before its slower secret/identity setup is deferred.
- Dialog construction, tab/widget mutation, GObject signal handling, VTE
  spawn/evidence, and most presentation callbacks require the GTK main loop.
- OpenSSH command construction, config parsing utilities, secret backends,
  identity providers, SFTP packet handling, credential adapters, and much of
  plugin command execution are already usable headlessly.
- Polling is used for process cleanup, connection evidence/reconnect delays,
  UI progress rendering, live logs, Docker status, and selected file-monitor
  fallbacks.

## Concurrency bridge decision

`InProcessClient` has synchronous command methods and a synchronous,
frontend-neutral publisher.

- Commands must run on the thread that constructed the client. This reflects
  current GObject manager ownership and is enforced with a structured
  `invalid_request` error.
- Connection-manager GObject signals are adapted into `CoreEvent` records.
  Delivery uses a publisher-global serial FIFO. The first active publisher
  becomes its dispatcher; concurrent publisher calls wait and may receive
  callbacks on that dispatcher thread. Today, relevant manager signals normally
  originate on or are marshalled to GTK's main thread.
- Subscribers are invoked in registration order. One failing subscriber is
  logged and does not block later subscribers.
- Re-entrant publication is queued behind the current subscriber snapshot and
  does not recurse.
- `Subscription.unsubscribe()` is idempotent and thread-safe.
- `InProcessClient.close()` disconnects manager signal handlers and closes all
  subscriptions. The window calls it only when close is actually accepted.
- There is no per-call event loop, `asyncio.run()`, or GTK wait on a future.
- Slow subscribers currently delay subsequent subscribers and events without
  changing order. This is acceptable for the small connection event foundation;
  subscribers must return quickly.
  Daemon event forwarding will need bounded outbound queues and explicit
  slow-client policy.

Phase 1 `DaemonClient` uses one persistent blocking socket, one request lock,
and finite timeouts. GTK has not switched to it. A later GTK integration may
add a GLib-facing non-blocking adapter without changing the synchronous public
contract.

## Existing state models

| State model | Location and values | Writers | Readers | Meaning today | Decision |
| --- | --- | --- | --- | --- | --- |
| `ConnectionState` | `connection_manager.py`: `unknown`, `connecting`, `connected`, `disconnected`, `failed` | `Connection`, `TerminalWidget`, `MainWindow._recompute_connection_state`, `ConnectionManager.update_connection_state` | Sidebar, window/terminal managers, tests | Aggregate runtime terminal lifecycle for a saved connection, despite its “connection health” wording | Do not expose as host health. Split future `ConnectionHealth` from `SessionState`; retain as compatibility state during migration |
| `Connection.is_connected` | Compatibility property on `Connection` | Many legacy terminal paths | Many UI/action paths | Boolean view of `ConnectionState.CONNECTED` | Deprecate only after all session readers use session records |
| `TerminalWidget.connection_state` and `connection_state_reason` | `terminal.py` | Spawn, evidence, exit, reconnect and failure paths | Window aggregation/sidebar/update handlers | Per-widget runtime SSH session state using the same enum | Migrate to core `SessionState`; renderer keeps only presentation state |
| `TerminalWidget.is_connected` | `terminal.py` and backends | Spawn/evidence/disconnect paths | Terminal manager, shutdown, tab logic | Per-widget live flag | Replace with session state snapshots/events |
| Process state | `SSHProcessManager`, terminal backends, subprocess `returncode`/child-exited callbacks | Process and child watchers | Cleanup, exit classification, UI | Implicit PID/return-code lifecycle | Introduce explicit core process/session exit information |
| SFTP request/transfer state | OpenSSH SFTP pending request IDs, file-manager progress fields and cancellation flags | Reader/worker threads | Progress dialogs and callers | Operation-local state, not a single enum | Normalize later as `TransferState`; do not change runtime now |
| Plugin stream state | `StreamHandle.running()` and subprocess status | Plugin command workers | Plugins/UI | Implicit running/stopped state | Map to plugin operation events later |
| `SessionManager` records | `session_manager.py` named/current/previous layout data | Window actions and close capture | Window restore/actions | Saved frontend tab layout, not live SSH sessions | Keep frontend-owned and distinguish from new runtime session IDs |

The protocol schemas define `ConnectionHealth` as
`unknown/checking/reachable/unreachable` and runtime `SessionState` as
`creating/connecting/waiting_for_interaction/connected/reconnecting/disconnected/failed/closing/closed`.
The in-process connection DTO reports health as `unknown`; it does not convert
terminal-derived `ConnectionState` into a false reachability claim.

## Key conflicts and blockers

- Connection identity is the SSH Host alias. Protocol v1 emits the alias
  in connection DTOs. Reload reuses objects by alias, preserving those
  maps without prematurely moving terminal ownership.
- `TerminalManager` combines session policy with tabs, colors, dialogs, vault
  prompts, plugin events, and reconnect presentation.
- `TerminalWidget` owns renderer, PTY/process mechanics, connection evidence,
  state classification, and reconnect mechanics.
- The current “session manager” name describes saved layouts, not runtime
  sessions.
- `Config` and group persistence mix domain data with presentation state.
- `PluginHost` is process-wide but bound to the first window and dispatches
  terminal objects, so it is not yet a frontend-neutral core event bus.
- Shutdown is distributed across window close handling,
  `SSHProcessManager.cleanup_all()`, terminal widgets, background workers,
  askpass, vault sessions, and GLib sources.

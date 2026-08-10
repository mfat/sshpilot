# Phase 14 — GTK Runtime Audit

> **Historical migration record.** This document describes an earlier phase and
> names components/settings as they existed then. It is not the current runtime
> contract; production GTK now requires the daemon and has no local SSH fallback.


Audit of all GTK application execution paths connecting to the daemon runtime.
Covers terminal, file manager, port forwarding, session restore, quit policy,
crash recovery, and protocol compatibility.

## 1. Terminal Connection Path

### Traced Flow (Production)

```
User double-clicks connection / quick-connect / CLI
→ TerminalManager.connect_to_host()                    [terminal_manager.py:136]
  → resolve_ssh_terminal_route(config, connection)     [daemon_terminal_policy.py:125]
    Returns DAEMON when terminal.daemon_backed_ssh=True (default)
  → _open_daemon_ssh(connection)                        [terminal_manager.py:314]
    → _ensure_daemon_terminal_ready()                  [terminal_manager.py:430]
      → resolve_daemon_terminal_readiness(window)      [daemon_terminal_policy.py:237]
        Checks: client, bridge, handshake, capabilities
      → _try_start_daemon_client() if not ready        [terminal_manager.py:466]
    → _create_internal_terminal_tab(connection)        [terminal_manager.py:535]
    → terminal.start_daemon_session(client, bridge, …) [terminal.py:1081]
      → DaemonTerminalSessionController(client, bridge, connection_id, …)
                                                       [terminal_session_controller.py:137]
      → controller.open(connection_id, dimensions)     [terminal_session_controller.py:193]
        → bridge.submit(client.open_session)           [gtk_client_bridge.py:94]
        → _on_session_opened → controller.attach()     [terminal_session_controller.py:362]
          → bridge.bind_terminal(output_callback)      [gtk_client_bridge.py:59]
          → bridge.submit(client.attach_session)
```

### Data Flow

**Output (daemon → GTK):**
```
Daemon SSH process stdout
→ SessionRuntime output callback
→ GtkTerminalBinding._receive_output()                 [gtk_client_bridge.py:305]
→ GtkTerminalBinding._drain() on GTK thread            [gtk_client_bridge.py:358]
→ TerminalWidget._on_daemon_output(data)               [terminal.py:1211]
→ vte.feed(data)                                       [VTE widget]
```

**Input (GTK → daemon):**
```
VTE commit signal
→ TerminalWidget._on_daemon_commit(text)               [terminal.py:1237]
→ controller.send_input(encoded_bytes)                 [terminal_session_controller.py:315]
→ bridge.submit(client.send_terminal_input)
```

**Resize:**
```
VTE char-size-changed signal
→ TerminalWidget._on_daemon_size_changed()             [terminal.py:1248]
→ controller.resize(TerminalDimensions)                [terminal_session_controller.py:335]
→ bridge.submit(client.resize_terminal)
```

### Key Files

| File | Key Class/Function | Lines |
| --- | --- | --- |
| `terminal_manager.py` | `TerminalManager.connect_to_host()` | 136-213 |
| `terminal_manager.py` | `_open_daemon_ssh()` | 314-428 |
| `terminal_manager.py` | `_ensure_daemon_terminal_ready()` | 430-464 |
| `terminal_manager.py` | `_try_start_daemon_client()` | 466-533 |
| `terminal.py` | `TerminalWidget.start_daemon_session()` | 1081-1141 |
| `terminal.py` | `TerminalWidget.attach_daemon_session()` | 1143-1209 |
| `terminal.py` | `TerminalWidget._on_daemon_output()` | 1211-1221 |
| `terminal.py` | `TerminalWidget._on_daemon_commit()` | 1237-1246 |
| `terminal.py` | `TerminalWidget._on_daemon_size_changed()` | 1248-1260 |
| `terminal.py` | `TerminalWidget._update_daemon_connection_state()` | 1442-1497 |
| `terminal_session_controller.py` | `DaemonTerminalSessionController` | 137-615 |
| `terminal_session_controller.py` | `controller.open()` | 193-218 |
| `terminal_session_controller.py` | `controller.attach()` | 220-260 |
| `terminal_session_controller.py` | `controller.detach()` | 262-283 |
| `terminal_session_controller.py` | `controller.close()` | 285-313 |
| `gtk_client_bridge.py` | `GtkClientBridge` | 36-260 |
| `gtk_client_bridge.py` | `bridge.bind_terminal()` | 59-88 |
| `gtk_client_bridge.py` | `bridge.submit()` | 94-108 |
| `gtk_client_bridge.py` | `GtkTerminalBinding._drain()` | 358 |
| `daemon_terminal_policy.py` | `resolve_ssh_terminal_route()` | 125-161 |
| `daemon_terminal_policy.py` | `resolve_daemon_terminal_readiness()` | 237-293 |
| `daemon_interaction_dialogs.py` | `DaemonInteractionDialogs` | 29-317 |

### Legacy Fallback Path

The former `TerminalManager._open_removed_local_ssh()` path has been removed.
Internal SSH activation and reconnect require the daemon owner; daemon failure
does not launch a frontend SSH process.

### External Terminals

Explicitly selected external terminals launch via `build_native_command()` and
operate out of process. No daemon involvement.

---

## 2. File Manager Path

### Traced Flow (Production)

```
User clicks "Manage Files" in sidebar/context/tab menu
→ Window dispatches to create_file_manager_backend()    [file_manager/__init__.py:54]
  → resolve_file_manager_route(config)                  [extended_service_policy.py:184]
    Returns DAEMON when daemon client + capabilities present
  → DaemonSftpManager(client, bridge, connection_id)    [daemon_sftp_backend.py]
    → DaemonSftpServiceController.open()                [daemon_sftp_backend.py]
      → bridge.submit(client.open_sftp)
    → sftp_list_directory()                             [daemon_sftp_backend.py]
      → bridge.submit(client.sftp_list_directory)
    → start_transfer() (upload/download)
      → bridge.submit(client.start_transfer)
```

### Key Files

| File | Key Class/Function | Lines |
| --- | --- | --- |
| `file_manager/__init__.py` | `create_file_manager_backend()` | 54 |
| `daemon_sftp_backend.py` | `DaemonSftpManager` | — |
| `daemon_sftp_backend.py` | `DaemonSftpServiceController` | — |
| `sftp_service_controller.py` | `DaemonSftpServiceController` (core) | — |
| `transfer_service_controller.py` | `TransferServiceController` | — |
| `extended_service_policy.py` | `prefer_daemon_extended_services()` | 69-117 |
| `extended_service_policy.py` | `resolve_file_manager_route()` | 184-200 |
| `window_file_manager.py` | `_open_manage_files_now_for_connection()` | 625 |

### Legacy Path

Legacy `OpenSSHSFTPManager` runs `ssh -s sftp` subprocess. Retained only when
`file_manager.removed_local_sftp=True` or when client mode is strictly in-process.

---

## 3. Port Forwarding Path

### Traced Flow (Production)

```
Connection Dialog (Forwarding tab) / Sidebar context actions
→ ForwardServiceController                              [forward_service_controller.py]
  → client.open_forward(OpenForwardRequest)
  → Status updates via client.subscribe_events()
  → client.close_forward(CloseForwardRequest)
  → client.claim_forward(ClaimForwardRequest)           [orphan claim, Phase 13.3]
```

### Key Files

| File | Key Class/Function | Lines |
| --- | --- | --- |
| `forward_service_controller.py` | `ForwardServiceController` | — |
| `api/daemon_client.py` | `DaemonClient.open_forward()` | — |
| `api/daemon_client.py` | `DaemonClient.claim_forward()` | 810 |
| `daemon/forward_runtime.py` | `ForwardRuntime.detach_client()` | 692 |
| `daemon/forward_runtime.py` | `ForwardRuntime.claim_forward()` | — |

All port forwarding in daemon mode delegates to the daemon. No legacy
standalone forwarding is used for daemon connections.

---

## 4. Application Lifecycle & Quit Policy

### Tab Close Policy

```
User closes a terminal tab
→ TerminalWidget._on_close_requested()                  [terminal.py:1333]
  → resolve_tab_close_policy(config)                    [daemon_terminal_policy.py:365]
    DETACH: controller.detach() → session stays alive in daemon
    TERMINATE: controller.close() → daemon terminates session
    ASK: prompt user, then detach or terminate
```

Policies: `TerminalClosePolicy.DETACH` (default), `TERMINATE`, `ASK`
Setting: `terminal.daemon_tab_close_policy`

### App Close Policy

```
User closes main window / quits app
→ cleanup_and_quit(window)                              [shutdown.py:18]
  → _perform_cleanup_and_quit(window, connections)      [shutdown.py:60]
    → For each terminal: _disconnect_terminal_safely()  [shutdown.py:251]
    → SSHProcessManager cleanup
    → SecretManager.lock_all()
    → window._do_quit()

App quit policy setting: terminal.daemon_app_close_policy
  DETACH: Detach GTK views, daemon stays running
  TERMINATE: stop_daemon(force=True), all sessions terminated
  ASK: prompt user
```

### Key Files

| File | Key Class/Function | Lines |
| --- | --- | --- |
| `shutdown.py` | `cleanup_and_quit()` | 18-57 |
| `shutdown.py` | `_perform_cleanup_and_quit()` | 60-139 |
| `shutdown.py` | `_disconnect_terminal_safely()` | 251-266 |
| `daemon_terminal_policy.py` | `resolve_tab_close_policy()` | 365-366 |
| `daemon_terminal_policy.py` | `resolve_app_close_policy()` | 369-370 |
| `daemon_terminal_policy.py` | `TerminalClosePolicy` enum | 33-38 |

---

## 5. Session Restore / Reattach

### Traced Flow

```
GTK app launches / reconnects after restart
→ DaemonSessionRestoreManager.get_restorable_sessions(client)
                                                       [daemon_session_restore.py:66]
  → Checks terminal.daemon_restore_sessions setting
  → Filters stored metadata against current daemon instance + active sessions
→ For each restorable session:
  → restore_session(window, metadata, client, bridge)   [daemon_session_restore.py:126]
    → Finds connection by connection_id
    → Creates TerminalWidget tab
    → terminal.attach_daemon_session(client, bridge, session_id, from_sequence)
                                                       [terminal.py:1143]
      → DaemonTerminalSessionController.attach()
        → bridge.bind_terminal(output_callback)
        → client.attach_session(AttachSessionRequest(from_sequence=N))
        → Replay output from sequence N, then stream live
```

### Metadata Persistence

- Stored in `terminal.daemon_session_restore_state` config setting
- Contains: session_id, daemon_instance_id, connection_id, last_sequence, tab_title, view_id, timestamp
- Capped at 50 entries; deduplicated by session_id
- **Never stores output data or secrets**

### Key Files

| File | Key Class/Function | Lines |
| --- | --- | --- |
| `daemon_session_restore.py` | `DaemonSessionRestoreManager` | 31-183 |
| `daemon_session_restore.py` | `save_session_metadata()` | 37-64 |
| `daemon_session_restore.py` | `get_restorable_sessions()` | 66-108 |
| `daemon_session_restore.py` | `restore_session()` | 126-183 |
| `terminal.py` | `TerminalWidget.attach_daemon_session()` | 1143-1209 |

---

## 6. Crash Recovery / Daemon Resurrection

### Recovery Path

```
Daemon dies unexpectedly (process crash, OOM kill, etc.)
→ Client transport event: TRANSPORT_CLOSED
→ TerminalWidget._on_daemon_output receives EOF or error
→ TerminalWidget._update_daemon_connection_state(FAILED/CLOSED)
  → Shows disconnected overlay on tab
  → User can reconnect: TerminalManager.connect_to_host(force_new=True)
    → Opens new session via daemon (if daemon is still running)
    → Or starts new daemon + session (via _try_start_daemon_client)
```

### Daemon Start on Demand

`TerminalManager._try_start_daemon_client()` (line 466):
- Creates `GtkClientBridge` if needed
- Creates `DaemonLauncher`, calls `launcher.connect_or_start()`
- On success: stores client, installs API event subscription
- On failure: returns `DaemonTerminalReadinessReason` for error display
- Bounded: respects timeout, shows error dialog on failure

### Key Files

| File | Key Class/Function | Lines |
| --- | --- | --- |
| `terminal_manager.py` | `_try_start_daemon_client()` | 466-533 |
| `terminal.py` | `TerminalWidget._update_daemon_connection_state()` | 1442-1497 |

---

## 7. Protocol Compatibility

### Version Checks

`resolve_daemon_terminal_readiness()` (daemon_terminal_policy.py:237) checks:
1. Client availability (server instance ID)
2. Protocol version compatibility via `daemon.versions_compatible()`
3. Required terminal capabilities: `TERMINAL_OUTPUT`, `TERMINAL_INPUT`, `TERMINAL_RESIZE`, `TERMINAL_REPLAY`
4. Required interaction capabilities: `INTERACTIONS_READ`, `INTERACTIONS_RESPOND`, `INTERACTIONS_EVENTS`, `INTERACTIONS_HOST_KEY`, `INTERACTIONS_PASSWORD`, `INTERACTIONS_PASSPHRASE`

Missing capabilities return `DaemonTerminalReadinessReason.MISSING_CAPABILITIES` with the specific list.

### Key Files

| File | Key Class/Function | Lines |
| --- | --- | --- |
| `daemon_terminal_policy.py` | `resolve_daemon_terminal_readiness()` | 237-293 |
| `daemon_terminal_policy.py` | `daemon_terminal_capabilities_missing()` | 611-615 |
| `terminal_session_controller.py` | `required_daemon_terminal_capabilities()` | 592-608 |

---

## 8. Architecture Boundary

### Strict Rules

1. **No OpenSSH command building in GTK**: When connected to daemon mode, GTK views never assemble `ssh`, `scp`, or `sftp` command lines directly.
2. **No subprocess ownership in GTK**: GTK widgets do not spawn or monitor local child PTY processes when connected to a daemon session.
3. **No direct thread mutations**: All background thread → GTK delivery passes through `GtkClientBridge` / `GLib.idle_add`.
4. **No stale fallbacks**: Daemon connection failures present clear errors; no silent fallback to unmanaged local SSH sessions unless explicitly configured.
5. **No per-host SSH settings on command line**: Persisted to `~/.ssh/config` (see `connection_manager.py` config writer).

### Boundary Map

```
┌─────────────────────────────────────────────────┐
│ GTK Layer (window.py, terminal.py, etc.)        │
│  - UI widgets, user interaction                 │
│  - State display (connected/disconnected)       │
│  - User prompts (host key, password)            │
└───────────┬─────────────────────────┬───────────┘
            │ GLib.idle_add           │ bridge.submit()
            ▼                         ▼
┌───────────────────────┐  ┌──────────────────────┐
│ GtkClientBridge       │  │ DaemonInteraction    │
│  - Thread-safe handoff│  │  Dialogs             │
│  - Terminal binding   │  │  - Host key dialog   │
│  - Request tracking   │  │  - Password dialog   │
└───────────┬───────────┘  └──────────┬───────────┘
            │ bridge.submit()         │ bridge.submit_interaction()
            ▼                         ▼
┌─────────────────────────────────────────────────┐
│ DaemonClient (api/daemon_client.py)             │
│  - Wire protocol (JSON over Unix socket)        │
│  - All daemon API calls                         │
└─────────────────────────────────────────────────┘
            │ Unix socket IPC
            ▼
┌─────────────────────────────────────────────────┐
│ Daemon Process (sshpilotd)                      │
│  - SSH process management                       │
│  - SFTP / Transfer / Forward runtimes           │
│  - Resource lifecycle                           │
└─────────────────────────────────────────────────┘
```

---

## 9. Summary of Affected Code Modules

| Module | Role |
| --- | --- |
| `terminal_manager.py` | Host connection routing, daemon readiness, tab creation |
| `terminal.py` | VTE event handling, daemon attachment, input/output/resize |
| `terminal_session_controller.py` | Session state machine (open/attach/detach/close) |
| `gtk_client_bridge.py` | Thread-safe GTK↔daemon handoff |
| `daemon_terminal_policy.py` | Routing policy, readiness checks, close policies |
| `daemon_interaction_dialogs.py` | Host key and password GTK dialogs |
| `daemon_session_restore.py` | Session metadata persistence and restore |
| `file_manager/__init__.py` | Backend factory routing to daemon/legacy |
| `daemon_sftp_backend.py` | Daemon SFTP backend |
| `sftp_service_controller.py` | SFTP service lifecycle |
| `transfer_service_controller.py` | Transfer lifecycle |
| `forward_service_controller.py` | Port forwarding lifecycle |
| `extended_service_policy.py` | Daemon vs legacy routing for extended services |
| `window.py` | Client mode determination, quit handlers |
| `window_file_manager.py` | FM dispatch, open/manage files |
| `shutdown.py` | Clean shutdown, progress, reconnection |
| `api/daemon_client.py` | Wire protocol client |
| `api/models/` | Request/response models |

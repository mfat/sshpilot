# Phase 14 — GTK Runtime Audit

This document details the audit of current GTK application execution paths before completing final daemon integration and release hardening.

## 1. Terminal Connection Path

### Traced Flow
`connection row` / `quick connect` / `CLI connect`
→ `MainWindow` or `TerminalManager.connect_to_host()`
→ `resolve_ssh_terminal_route()` (reads `terminal.daemon_backed_ssh`, default True)
→ `_open_daemon_ssh()` vs `_open_legacy_local_ssh()`
→ `TerminalWidget` creation (VTE-based)
→ `start_daemon_session()` / `DaemonTerminalSessionController` vs `Vte.Terminal.spawn_async()`
→ `DaemonInteractionDialogs` / `InteractionBroker` vs local GTK askpass dialogs
→ `_update_daemon_connection_state()` (`TerminalSessionState.ACTIVE` → `is_connected=True`)
→ Input via `send_input()`, Output via `bind_terminal()` feed, Resize via `resize()`
→ Tab close via `resolve_tab_close_policy()` (`detach` vs `close`).

### Audit Findings & Legacy Paths
- **Daemon Route**: When `terminal.daemon_backed_ssh` is `True` (default) and daemon capabilities are available, `TerminalManager._open_daemon_ssh()` invokes `TerminalWidget.start_daemon_session()`. This uses `DaemonTerminalSessionController` to send `OpenSessionRequest` via `GtkClientBridge` and streams terminal data directly into `Vte.Terminal.feed()`.
- **Legacy Fallback Path**: `TerminalManager._open_legacy_local_ssh()` launches OpenSSH directly using `Vte.Terminal.spawn_async()` with askpass environment. This path is retained ONLY behind the explicit configuration setting `terminal.legacy_local_ssh_fallback=True`.
- **External Terminals**: Explicitly selected external terminals launch external terminal emulators via `build_native_command()` and operate out of process.
- **Session State Alignment**: `TerminalWidget._update_daemon_connection_state()` maps `TerminalSessionState.ACTIVE` to `is_connected=True` and updates GTK UI status bars, tab icons, and header actions.

---

## 2. File Manager Path

### Traced Flow
`open file manager` (Sidebar, context menu, or tab menu)
→ `open_file_manager_for_connection()` or `FileManagerWindow` instantiation
→ `create_file_manager_backend()` in `file_manager/__init__.py`
→ Route resolution via `resolve_file_manager_route()`
→ `DaemonSftpManager` (using `DaemonSftpServiceController` / `DaemonClient`) vs legacy `OpenSSHSFTPManager` (`ssh -s sftp` child PTY)
→ `sftp_list_directory()` / `RemoteFileEntry` model binding in `FilePane`
→ Directory navigation & cached listing update
→ Remote mutations: `sftp_mkdir()`, `sftp_rename()`, `sftp_remove()`, `sftp_rmdir()`
→ Transfers: `start_transfer()` (UPLOAD / DOWNLOAD), progress callback via bridge
→ Conflict handling dialog / transfer cancellation via `cancel_transfer()`
→ Teardown: `close()` / service detach vs termination.

### Audit Findings & Legacy Paths
- **Daemon Route**: `create_file_manager_backend()` checks for `DaemonClient` + `GtkClientBridge` + `ConnectionId` and required SFTP/transfer capabilities to instantiate `DaemonSftpManager`. This talks asynchronously to daemon `SftpService` and `TransferService`.
- **Legacy Path**: Legacy `OpenSSHSFTPManager` runs an independent `ssh -s sftp` subprocess. Retained only when `file_manager.legacy_local_sftp=True` or when client mode is strictly in-process.

---

## 3. Port Forwarding Path

### Traced Flow
`Connection Dialog (Forwarding tab)` / `Sidebar context actions` / `Forwarding Controller`
→ `ForwardServiceController` / `DaemonClient.open_forward()`
→ Forward types: `LOCAL`, `REMOTE`, `DYNAMIC` (SOCKS5)
→ Status updates & lifecycle tracking in `ForwardServiceController`
→ Rediscovery via `client.list_forwards()` on GTK startup/reconnect
→ Claiming orphaned forwards via `client.claim_forward()` (API added in Phase 13.3/14).

### Audit Findings & Legacy Paths
- All port forwarding controls in daemon mode delegate to `DaemonClient.open_forward()`, `close_forward()`, and `claim_forward()`.
- Legacy standalone forwarding via SSH command line overrides is bypassable for daemon connections.

---

## 4. Application Lifecycle & Quit Policy

### Traced Flow
`App launch` → Daemon discovery (`ClientFactory.get_client()`) or automatic background daemon spawn → `GtkClientBridge` initialization → Connection manager load.
`Window close` / `App Quit` → Check active sessions, SFTP services, and forwards via `DaemonClient.get_daemon_status()` or `TerminalSessionController` state.
Prompt user according to `terminal.daemon_app_close_policy`:
- **Keep Running**: Detach GTK views, close client transport, daemon remains running for active sessions/forwards.
- **Terminate All**: Call `client.stop_daemon()` or close all active sessions/forwards then exit.
- **Cancel**: Abort quit.

---

## 5. Summary of Affected Code Modules
- `src/sshpilot/terminal_manager.py`: Host connection routing and backend initialization.
- `src/sshpilot/terminal.py`: `TerminalWidget` VTE event handling, daemon attachment, input commit, resize, and state updates.
- `src/sshpilot/file_manager/__init__.py`: Backend factory `create_file_manager_backend`.
- `src/sshpilot/file_manager_window.py`: File manager window controller and SFTP backend signal wiring.
- `src/sshpilot/daemon_sftp_backend.py`: Daemon SFTP backend implementation.
- `src/sshpilot/sftp_service_controller.py` & `forward_service_controller.py`: Daemon service controllers.
- `src/sshpilot/window.py` & `src/sshpilot/shutdown.py`: Application quit and session detachment/termination policies.

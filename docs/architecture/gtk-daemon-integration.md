# GTK Daemon Integration Architecture

This document defines the integration boundary between the SSH Pilot GTK desktop user interface and the backend daemon runtime (`DaemonClient`).

## 1. Integration Boundary Overview

To ensure clear separation of concerns and prevent direct `DaemonClient` calls from leaking into individual GTK widgets:

- **`GtkRuntimeCoordinator` / `DaemonRuntimeController`**: The unified GTK application-level coordinator managing client connections, background event subscriptions, and service mappings.
- **`GtkClientBridge`**: The thread-safe handoff mechanism executing synchronous `DaemonClient` API requests on a background worker thread and dispatching results onto the GTK GLib main loop.
- **Service Controllers**:
  - `DaemonTerminalSessionController`: Drives individual VTE terminal tab sessions.
  - `DaemonSftpServiceController` / `DaemonSftpManager`: Drives built-in SFTP file manager views.
  - `TransferServiceController`: Drives background file uploads and downloads with GTK progress callbacks.
  - `ForwardServiceController`: Drives local, remote, and dynamic SOCKS port forwarding.

---

## 2. Responsibilities of `GtkRuntimeCoordinator`

1. **Client & Bridge Lifecycle**:
   - Holds references to the active frontend-neutral client (`DaemonClient` or in-process fallback) and `GtkClientBridge`.
   - Subscribes to daemon events (`sessions.*`, `sftp.*`, `transfers.*`, `forwards.*`, `interactions.*`).
2. **Resource Mappings**:
   - Maintains explicit mappings:
     - GTK Terminal Tab widget ↔ Daemon `SessionId` & `AttachmentId`
     - SFTP File Manager view ↔ Daemon `SftpServiceId`
     - Transfer progress row ↔ Daemon `TransferId`
     - Port Forward UI entry ↔ Daemon `ForwardId`
3. **Reattachment & Rediscovery**:
   - Enumerates existing active daemon sessions/forwards upon GTK application launch or reconnect.
   - Restores corresponding GTK tabs and view states asynchronously.
4. **Error Translation & Dispatches**:
   - Translates `SshPilotError` and wire error codes into GTK user-facing dialog messages.
   - Ensures all GTK UI updates execute strictly on the GLib main thread (`GLib.idle_add`).
5. **Clean Teardown**:
   - Cancels pending bridge requests and unsubscribes from daemon events on GTK window destroy / app exit.

---

## 3. Strict Boundary Rules

- **No OpenSSH Command Building in GTK**: GTK views/controllers must never assemble `ssh`, `scp`, or `sftp` command lines directly when running in daemon mode.
- **No Subprocess Ownership**: GTK widgets do not spawn or monitor local child PTY processes when connected to a daemon session.
- **No Direct Thread Mutations**: No background thread may mutate GTK widget properties directly. All deliveries must pass through `GtkClientBridge` / `GLib.idle_add`.
- **No Stale Fallbacks**: Daemon connection failures must present clear errors rather than silently falling back to unmanaged local SSH sessions unless explicitly requested by legacy user settings.

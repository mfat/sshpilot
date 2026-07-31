# Resource Ownership and Reattachment Architecture

This document defines the ownership model, orphan rules, claim semantics, and reattachment lifecycles for all daemon-managed resource types in SSH Pilot.

---

## Resource Types Matrix

| Resource Type | Owner Identity | Attachment Identity | Disconnect Behavior | Orphaned? | Who May List | Who May Attach / Reconnect | Who May Mutate / Control | Who May Close | Explicit Claim Required? | Concurrent Claim Behavior | App Restart Behavior | Daemon Shutdown Behavior |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **Session** | `ClientId` of creator | `AttachmentId` per client view | Client transport closes; session remains running | Yes (if no active attachments remain) | Any authenticated client | Any authenticated client | Active attachment with `input_owner=True` | Owner or client with session access | Input claim via `claim_terminal_input()` | First claim wins; secondary clients gain view-only | Session listed in daemon; GTK restores tab & replays output | Session process sent SIGTERM then SIGKILL |
| **Terminal Input** | `ClientId` holding input ownership | `AttachmentId` holding input flag | Ownership released / available to other clients | N/A | Any client listing session details | Client requesting input via `attach_session(request_input=True)` | Input owner only | Input owner releases input or closes session | Yes (`claim_terminal_input`) | Explicit claim preempts current input owner | GTK claims input on restored active tab | Session closes; input state destroyed |
| **SFTP Service** | `ClientId` of opener | None (shared backend service) | Client transport closes; SFTP service remains alive | Yes (when client count reaches 0) | Any authenticated client | Any client via `open_sftp` / `attach_sftp` | Attached clients | Owner or last attached client closing | No | Shared read/write access across views | Listed in daemon; GTK reattaches FM view | SFTP channel closed; remote connection terminated |
| **Transfer** | `ClientId` initiating transfer | `TransferId` | Background transfer worker continues | Yes (if client disconnects) | Any authenticated client | Any client monitoring progress | Transfer owner (can cancel) | Transfer owner or client calling `cancel_transfer()` | No | Progress events broadcast to all subscribers | Status query returns state; finished transfers reported | Worker thread cancelled; temporary `.sshpilot-tmp-*` cleaned |
| **Port Forward** | `ClientId` creating forward | `ForwardId` | Client disconnects → forward becomes ORPHANED | Yes | Any authenticated client | Any client via `claim_forward()` | Forward owner / claimed owner | Current owner or claimed owner via `close_forward()` | Yes (`claim_forward()`) | Claiming an active forward owned by another client returns error (`FORWARD_OWNED_BY_ANOTHER`) | Daemon keeps forward ACTIVE; GTK rediscovers & claims | Listening socket closed; remote tunnel terminated |

---

## Key Ownership Rules

### 1. Terminal Input Ownership
- A daemon session can have multiple attached viewers (read-only output streaming).
- Only ONE client attachment holds `input_owner=True` at any given time.
- If client A disconnects, input ownership becomes available to client B via `claim_terminal_input()`.

### 2. Forward Claim Semantics (`claim_forward`)
- When client A creates a port forward and then disconnects without closing it, the forward state changes from `ACTIVE` to `ORPHANED`.
- Client B discovers the orphaned forward via `client.list_forwards()` and calls `client.claim_forward(forward_id)`.
- Upon successful claim, client B becomes the new owner and can manage or close the forward.
- Attempting to claim a forward currently owned by a live client returns `SshPilotError(ErrorCode.FORWARD_OWNED_BY_ANOTHER)`.

### 3. Application Quit & Reattachment
- **Keep Running Policy**: GTK presents Keep connections running / Terminate everything / Cancel when `terminal.daemon_app_close_policy=ask` (default). Keep-running detaches all views; the daemon keeps active sessions, SFTP services, and forwards running. App-launched daemons are **not** stopped on keep-running.
- **Terminate everything**: drains sessions/SFTP/transfers/forwards/interactions via public APIs, then `stop_daemon(force=True)` for app-launched daemons.
- Upon GTK restart after keep-running, `daemon_session_restore.py` and `forward_service_controller.py` query the daemon and restore GTK tabs and forward UI entries with full output replay.

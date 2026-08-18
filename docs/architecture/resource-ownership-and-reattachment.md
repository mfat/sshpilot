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
- **Quit ends everything.** There is no keep-running outcome. `terminal.daemon_app_close_policy` chooses only whether quit confirms first (`ask`, the default, when live work exists) or proceeds immediately (`terminate`); a configuration still holding the retired `detach` value resolves to `ask`.
- **Teardown order**: drain sessions/SFTP/transfers/forwards/interactions via public APIs, then `stop_daemon(force=True)` — for *any* daemon this app is connected to, not only one it launched itself. A daemon that will not stop is escalated to SIGTERM then SIGKILL; the children it could not reap are collected from its durable process registry, and the ControlMasters it could not retire are swept, under the shared `owns_default_control_master_namespace` rule.
- **Exit is gated on verification, not on teardown having run.** `verify_sshpilot_runtime_terminated()` re-checks the socket's peer, the daemon PID, every registered child (PID *and* creation time, so a recycled PID is never counted as ours), and sshPilot's own ControlMasters. If anything owned survives, the application stays open and names it. Quit is never cancelled merely because the daemon *refused* — only because something is still running.
- **Ownership is explicit, never inferred from process names.** Sources: `subprocess.Popen` handles while the daemon lives, the durable registry beside the socket after it dies, Unix-socket peer credentials for the daemon itself, and `ssh -O check` on sshPilot's private ControlPath for masters. Nothing scans the process table, so an unrelated `ssh` is untouchable by construction.
- **Restore** (`daemon_session_restore.py`, `forward_service_controller.py`) is therefore crash recovery: it repopulates tabs and forward entries with full output replay when the UI went away *without* a clean quit (crash, SIGKILL, logout), and the daemon's sessions are still live on relaunch.

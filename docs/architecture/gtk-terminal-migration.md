# GTK Terminal Migration to Daemon Sessions (Phase 9)

> **Historical migration record.** This document describes an earlier phase and
> names components/settings as they existed then. It is not the current runtime
> contract; production GTK now requires the daemon and has no local SSH fallback.


Phase 9 implements the production daemon-backed SSH terminal path with multi-attachment support, input ownership management, and session persistence across GTK restarts. This document covers the complete activation flow, state management, and architectural decisions.

## Production Activation Flow

The daemon SSH terminal path follows this activation sequence:

```
GTK Terminal Request
    ↓
DaemonClient.sessions.open(connection_id, dimensions)
    ↓
Daemon acknowledges STARTING session (before PTY/OpenSSH/auth)
    ↓
GTK attaches immediately while state may still be STARTING
    ↓
Daemon worker allocates PTY / launches OpenSSH / brokers interactions
    ↓
Lifecycle events report RUNNING or FAILED asynchronously
    ↓
DaemonTerminalSessionController delivers output to TerminalWidget
    ↓
BaseTerminalBackend.feed() displays it in the active emulator
```

### Key Components

- **TerminalSessionController**: Frontend-neutral session controller interface
- **DaemonTerminalSessionController**: Production implementation using DaemonClient + GtkClientBridge
- **DaemonTerminalTabState**: Per-tab state tracking for sessions, attachments, and input ownership
- **GtkClientBridge**: Async bridge connecting daemon operations to GTK main thread

## Tab/Session/Attachment Mapping

Each daemon SSH terminal tab maintains state through `DaemonTerminalTabState`:

```python
@dataclass
class DaemonTerminalTabState:
    view_id: str                    # GTK tab identifier
    session_id: SessionId | None    # Daemon session ID
    attachment_id: AttachmentId | None  # This tab's attachment ID
    connection_id: ConnectionId     # SSH connection identifier
    daemon_instance_id: str         # Daemon instance for restart detection
    expected_sequence: int = 0      # Next expected terminal output sequence
    input_owner: bool = False       # Whether this tab owns input
    state: TerminalSessionState     # Current session state
```

### State Machine

The daemon terminal state progresses through these stages:

- **IDLE**: Initial state, no session
- **OPENING**: Session creation in progress
- **ATTACHING**: Attachment creation in progress
- **REPLAYING**: Receiving replay data from daemon buffer
- **ACTIVE**: Live terminal session with real-time I/O
- **DETACHED**: Session exists but tab is detached
- **CLOSING**: Session termination in progress
- **FAILED**: Operation failed
- **CLOSED**: Session terminated

## Process, Emulator, and Container Responsibilities

These are independent layers:

- **Daemon session/process ownership:** the daemon owns the SSH process and PTY,
  replay buffer, attachment state, and input ownership. GTK never creates a
  second PTY for a daemon session.
- **Terminal emulator backend:** `BaseTerminalBackend` is the display/input/size
  contract. `VTETerminalBackend` implements it with VTE and
  `PyXtermBridgeBackend` implements it with xterm.js. Either implementation can
  render and control a daemon-backed session.
- **GTK terminal container:** `TerminalWidget` owns layout, overlays, menus,
  session-controller wiring, split-pane integration, and drag-and-drop. It uses
  `backend.widget` but does not access emulator implementation objects.

Local and legacy terminals still have GTK-side process ownership, but their PTY
creation and spawning live behind `BaseTerminalBackend.spawn_async()`. External
terminals remain process-owned outside sshPilot.

The daemon data flow is:

```
Daemon output
    → DaemonTerminalSessionController
    → TerminalWidget
    → BaseTerminalBackend.feed()
    → active emulator

User input
    → active emulator
    → BaseTerminalBackend callback
    → TerminalWidget
    → DaemonTerminalSessionController.send_input()
```

## Close Policies

Daemon SSH terminals support three close policies configurable via settings:

### Tab Close Policy (`terminal.daemon_tab_close_policy`)

- **DETACH** (default): Tab close detaches from session without terminating
- **TERMINATE**: Tab close terminates the SSH session
- **ASK**: Prompt user for detach vs terminate choice

### App Close Policy (`terminal.daemon_app_close_policy`)

- **DETACH** (default): App exit detaches all sessions, leaves them running
- **TERMINATE**: App exit terminates all SSH sessions
- **ASK**: Prompt user for global detach vs terminate choice

### Policy Resolution

```python
def resolve_tab_close_policy(config) -> TerminalClosePolicy:
    return resolve_close_policy(config, "terminal.daemon_tab_close_policy")

def resolve_app_close_policy(config) -> TerminalClosePolicy:
    return resolve_close_policy(config, "terminal.daemon_app_close_policy")
```

## Input Ownership and Claim/Release

Daemon terminals implement exclusive input ownership with explicit claim/release:

### Input Ownership Rules

- **One input owner per session**: Only one attachment can own input at a time
- **Resize authority**: Input owner controls terminal resize operations
- **Ownership transfer**: Input can be claimed by other attachments
- **Broadcast limitation**: Only input-owning terminals receive broadcast commands

### API Methods

```python
# Claim input ownership for this attachment
def claim_terminal_input(
    session_id: SessionId,
    attachment_id: AttachmentId
) -> None

# Release input ownership
def release_terminal_input(
    session_id: SessionId,
    attachment_id: AttachmentId
) -> None
```

### Controller Interface

```python
@property
def input_owner(self) -> bool:
    """Whether this controller owns input."""
    return self._tab_state.input_owner

def send_input(self, data: bytes) -> None:
    """Send input data. Requires input ownership."""
    if not self._tab_state.input_owner:
        return
    # Send input...

def resize(self, dimensions: TerminalDimensions) -> None:
    """Resize terminal. Requires input ownership."""
    if not self._tab_state.input_owner:
        return
    # Resize terminal...
```

## Split Panes: Multi-Attachment Support

Split panes create multiple attachments to the same daemon session:

### Architecture

- **One attachment per pane**: Each split pane gets its own attachment ID
- **Shared session**: Multiple panes can attach to the same SSH session
- **Independent input ownership**: Each pane can claim/release input independently
- **Synchronized output**: All attachments receive the same terminal output

### Creation Flow

```python
def create_terminal_for_pane(self, connection, on_connected=None):
    """Create terminal for split pane - no tab_view integration."""
    terminal = TerminalWidget(connection, config, connection_manager)

    # Start daemon session if enabled
    if use_daemon:
        terminal.start_daemon_session(client, bridge, connection_id)

    return terminal
```

## Continuity Loss and Local Markers

### Continuity Loss Detection

When daemon buffer truncation causes output gaps, the system handles this gracefully:

- **Daemon detection**: Daemon detects when expected sequence doesn't match available replay
- **GTK notification**: `on_continuity_lost` callback notifies GTK layer
- **Local marker**: GTK shows local "output truncated" marker in terminal
- **Never propagated**: Continuity markers never enter daemon replay buffer

### Implementation

```python
def _handle_continuity_lost(self, session_id, expected, available) -> None:
    """Handle terminal continuity loss."""
    if self._on_continuity_lost:
        self._on_continuity_lost()  # GTK shows local marker

# In TerminalWidget
def _on_continuity_lost(self):
    """Show local continuity loss marker."""
    marker = "\r\n[sshPilot: Terminal output was truncated]\r\n"
    if self.backend:
        self.backend.feed(marker.encode())
        self.backend.commit()
```

## Phase 9.1 — Strict Terminal Routing

Phase 9.1 separates **route selection** from **daemon readiness**. Readiness
failures never change the selected route and never launch local internal SSH.

### Route model

```python
class SshTerminalRoute(str, Enum):
    DAEMON = "daemon"
    EXTERNAL = "external"
```

`resolve_ssh_terminal_route(config, connection)` decides only user/product
policy. It does **not** inspect client existence, capabilities, bridge state,
handshake status, or daemon startup progress.

### Precedence (first match wins)

1. Local shell tab / non-SSH protocol / missing connection → not an SSH route
2. External-terminal preference active and not policy-hidden → `EXTERNAL`
3. Internal SSH → `DAEMON`

### Readiness (separate)

```python
@dataclass(frozen=True)
class DaemonTerminalReadiness:
    ready: bool
    reason: DaemonTerminalReadinessReason | None
    missing_capabilities: tuple[Capability, ...] = ()
```

Reasons include `client_unavailable`, `bridge_unavailable`,
`handshake_incomplete`, `protocol_incompatible`,
`terminal_transport_unavailable`, `secret_transport_unavailable`,
`missing_capabilities`, `daemon_start_failed`, and `daemon_start_timeout`.

### No silent fallback guarantee

| Selected route | Service state | Outcome |
| --- | --- | --- |
| `DAEMON` | ready | daemon-backed SSH |
| `DAEMON` | unavailable / incompatible | clear error; **no** local SSH |
| `EXTERNAL` | (irrelevant) | external process |

`should_use_daemon_ssh_terminal()` now means “route is `DAEMON`” only. It no
longer returns false when the daemon is merely unavailable.

### Activation phases

```text
1. Resolve route
2. EXTERNAL → optional vault unlock → external launch
3. DAEMON → readiness (+ bounded on-demand start) → optional vault unlock
   → daemon session open (never native_connect / _connect_ssh)
```

### Secret-preflight ownership

- **Daemon route**: resolve route and readiness first. GTK may unlock a
  session-backed vault afterward so the daemon broker can read stored
  credentials; SSH passwords/passphrases are not retrieved into GTK.
- **External**: existing external askpass/secret policy unchanged.

Locked Bitwarden/KDBX backends remain a distinct unlock concern. Unlock is
backend unlock, not SSH authentication. Unsupported autofill falls through to
typed daemon interactions — never to silent local SSH.

### Decision Logic

```python
def resolve_ssh_terminal_route(config, connection, *, is_local=False):
    if is_local or connection is None or connection.protocol != "ssh":
        return None
    if use_external and not should_hide_external_terminal_options():
        return SshTerminalRoute.EXTERNAL
    return SshTerminalRoute.DAEMON
```

## Terminal Type Ownership

### Local Terminals: GTK-Owned

- **Process ownership**: GTK spawns and owns local shell processes
- **Backend choice**: User can select VTE or PyXtermJS via `terminal.backend`
- **No daemon involvement**: Local terminals bypass daemon entirely
- **Daemon settings do not apply**: local tabs open even when daemon is absent

### External Terminals: External Process

- **System ownership**: External terminal application owns process
- **No sshPilot control**: No terminal emulator, replay, or session management
- **SSH command only**: sshPilot builds SSH command, external terminal executes
- **Precedence**: external preference wins over daemon and legacy settings

### Daemon SSH Terminals: Daemon-Owned

- **Daemon ownership**: Daemon spawns and owns SSH processes
- **VTE emulation**: GTK receives output via VTE feed API
- **Session persistence**: Sessions survive GTK restart
- **Multi-attachment**: Multiple GTK tabs can attach to same session
- **No GTK SSH spawn**: `native_connect` / `_connect_ssh` / local askpass are
  not used for this route

## Rollout Stage C: Default On

Phase 9 / 9.1 implement Stage C rollout policy:

- **Default enabled**: `terminal.daemon_backed_ssh = True` by default
- **Readiness gated**: Launch blocked with a clear error when capabilities or
  transport are missing — never auto-switched to local SSH
- **Smooth upgrade**: Existing users get daemon SSH automatically

## Daemon Lifecycle

### Startup Behavior

- **On-demand start**: Bounded `DaemonLauncher.connect_or_start()` may run when
  the daemon route is selected and the client is unavailable
- **Capability check**: GTK verifies required capabilities before activation
- **Bridge initialization**: GtkClientBridge connects daemon to GTK main thread
- **Startup failure**: shows error; does not launch local SSH

### Persistence Behavior

- **Survive GTK exit**: Daemon remains alive after GTK closes if sessions exist
- **Session retention**: Live SSH sessions continue running in daemon
- **Idle exit**: Daemon shuts down when no sessions remain (deferred to Phase 10)
- **Restart recovery**: GTK startup can reattach to surviving daemon sessions
- **Restored sessions**: attach by session ID — they do not re-resolve a new
  terminal route as though opening a fresh connection

### Error Handling

```python
def _show_daemon_error_dialog(self, window, message):
    dialog = Adw.AlertDialog.new(_("Daemon Terminal Unavailable"), message)
    dialog.add_response("ok", _("OK"))
    dialog.add_response("prefs", _("Open Preferences"))
    dialog.present(window)
```

## Required Capabilities

Daemon SSH terminals require these capabilities:

```python
REQUIRED_DAEMON_TERMINAL_CAPABILITIES = frozenset({
    Capability.SESSIONS_READ,
    Capability.SESSIONS_WRITE,
    Capability.SESSIONS_EVENTS,
    Capability.TERMINAL_OUTPUT,
    Capability.TERMINAL_INPUT,
    Capability.TERMINAL_RESIZE,
    Capability.TERMINAL_REPLAY,
    Capability.INTERACTIONS_READ,
    Capability.INTERACTIONS_RESPOND,
    Capability.INTERACTIONS_EVENTS,
    Capability.INTERACTIONS_HOST_KEY,
    Capability.INTERACTIONS_PASSWORD,
    Capability.INTERACTIONS_PASSPHRASE,
})
```

Absence of terminal capabilities maps to `terminal_transport_unavailable`
(binary-terminal-v2). Absence of interaction capabilities maps to
`secret_transport_unavailable` (binary-secret-v2).

### Capability Gating

```python
def daemon_terminal_capabilities_missing(client) -> frozenset[Capability]:
    """Return missing required capabilities."""
    required = required_daemon_terminal_capabilities()
    supported = client.get_capabilities().supported
    return required - supported
```
## SSH Options Compatibility Matrix

| SSH Option Category | Support Status | Notes |
|-------------------|----------------|-------|
| **Core Connection** | ✅ Supported | Host, Port, User, IdentityFile |
| **Authentication** | ✅ Supported | Passwords, keys, certificates, agent |
| **Host Key Verification** | ✅ Supported | StrictHostKeyChecking, UserKnownHostsFile |
| **Connection Multiplexing** | ✅ Supported | ControlMaster, ControlPath, ControlPersist |
| **Forwarding** | ⏸️ Phase 10 | LocalForward, RemoteForward, DynamicForward |
| **X11 Forwarding** | ⏸️ Phase 10 | ForwardX11, ForwardX11Trusted |
| **Agent Forwarding** | ⏸️ Phase 10 | ForwardAgent |
| **Proxy/Jump** | ✅ Supported | ProxyJump, ProxyCommand |
| **Advanced Options** | ✅ Supported | ConnectTimeout, ServerAliveInterval, etc. |
| **Custom Commands** | ❌ Deferred | RemoteCommand handled by daemon |

### SFTP/Forwarding Boundary

SFTP file manager and port forwarding are implemented in Phase 10 (see
`sftp-services.md`, `file-transfers.md`, `port-forwarding.md`). Historical note
from Phase 9 planning:

- **Current scope**: Interactive SSH terminal sessions only
- **SFTP status**: Remains on local SSH path for Phase 9
- **Forwarding status**: Local/remote/dynamic forwarding deferred
- **Future integration**: Phase 10 will integrate SFTP and forwarding with daemon

## Implementation Files

### Core Controller
- `src/sshpilot/terminal_session_controller.py` - Controller interface and daemon implementation
- `src/sshpilot/daemon_terminal_policy.py` - Policy resolution and settings
- `src/sshpilot/daemon_session_restore.py` - Session persistence across restarts
- `src/sshpilot/daemon_sessions_dialog.py` - Live session management dialog

### Terminal Integration
- `src/sshpilot/terminal.py` - TerminalWidget daemon session integration
- `src/sshpilot/terminal_manager.py` - Daemon path activation logic

### API Models
- `src/sshpilot/api/models/terminal.py` - Terminal I/O and claim/release requests
- `src/sshpilot/api/models/sessions.py` - Session lifecycle models

### Daemon Runtime
- `src/sshpilot/daemon/session_runtime.py` - Daemon session lifecycle
- `src/sshpilot/daemon/interaction_broker.py` - Authentication interaction handling

## PTY autofill ownership (Phase 9.3)

| Runtime | Autofill | Input path |
| --- | --- | --- |
| Local / legacy GTK-owned SSH or shell | Allowed (sudo / residual password) | `TerminalWidget.feed_child_data` → backend `feed_child_data` / VTE child |
| Daemon-backed SSH | Disabled | Interaction dialogs; `send_input` only with attachment ownership |

Daemon tabs must never call VTE `feed_child` for SSH secrets, must not send
before attachment is active, and must not replay autofill after reattach or GTK
restart. One-shot values are cleared after delivery and never logged.

## Production soak reminder

After pulling daemon changes, restart `sshpilotd` (or quit sessions and relaunch
the app so the launcher starts a fresh process). Confirm handshake identity in
logs before trusting interactive SSH behavior.

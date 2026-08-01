# Session Reattachment and Persistence (Phase 9)

This document covers daemon SSH session persistence across GTK restarts, including detachment policies, restoration metadata, and reattachment mechanisms.

## Routing boundary

Restored sessions attach **directly by session ID**. They do not call
`resolve_ssh_terminal_route` / `connect_to_host` as though opening a new SSH
connection. Phase 9.1 routing changes therefore must not bypass restored-session
attach paths (`DaemonSessionRestoreManager.restore_session`).

## Detach vs Terminate

Daemon SSH sessions support two distinct close operations:

### Detach Operation

**Detach preserves the running SSH session while disconnecting the GTK tab:**

- **Session remains alive**: SSH process continues running in daemon
- **Output buffered**: Terminal output continues to be captured in daemon replay buffer
- **Reattachable**: GTK can later reattach to the live session
- **No data loss**: Full session history available on reattach

```python
def detach(self) -> None:
    """Detach from session without terminating it."""
    if self._closed or not self._tab_state.attachment_id:
        return

    self._tab_state.state = TerminalSessionState.DETACHED
    self._tab_state.input_owner = False

    # Close output stream
    if self._stream:
        self._stream.close()
        self._stream = None

    # Send detach request to daemon
    self._bridge.submit(
        lambda: self._client.detach_session(
            DetachSessionRequest(
                session_id=self._tab_state.session_id,
                attachment_id=self._tab_state.attachment_id,
            )
        )
    )
```

### Terminate Operation

**Terminate kills the SSH session and cleans up all resources:**

- **Process killed**: SSH process terminated immediately
- **Session destroyed**: All session data and replay buffer cleared
- **Not reattachable**: Session cannot be restored
- **Resource cleanup**: All daemon resources freed

```python
def close(self) -> None:
    """Terminate the session."""
    self._closed = True
    self._tab_state.state = TerminalSessionState.CLOSING

    # Close output stream first
    if self._stream:
        self._stream.close()
        self._stream = None

    # Terminate session if we have one
    if self._tab_state.session_id:
        self._bridge.submit(
            lambda: self._client.close_session(
                CloseSessionRequest(session_id=self._tab_state.session_id)
            )
        )
```

## GTK Restart: Detach and Persist Safe Metadata

When GTK exits, daemon SSH sessions are detached and safe restoration metadata is persisted:

### Safe Metadata Only

**Only connection identifiers and sequence numbers are persisted - never output content or secrets:**

```python
@dataclass
class SessionRestoreMetadata:
    session_id: str              # Daemon session identifier
    daemon_instance_id: str      # Daemon instance for stale detection
    connection_id: str           # Connection identifier for lookup
    last_sequence: int           # Last received output sequence
    tab_title: str              # Display title for restored tab
    view_id: str                # GTK view identifier
    timestamp: float             # Persistence timestamp
```

### Metadata Persistence

```python
def save_session_metadata(self, tab_state, tab_title: str = "Terminal") -> None:
    """Save safe restoration metadata to config."""
    metadata = SessionRestoreMetadata(
        session_id=str(tab_state.session_id),
        daemon_instance_id=str(tab_state.daemon_instance_id or ""),
        connection_id=str(tab_state.connection_id),
        last_sequence=int(tab_state.expected_sequence or 0),
        tab_title=tab_title or "Terminal",
        view_id=str(tab_state.view_id),
        timestamp=time.time(),
    )

    # Store in config with deduplication
    restore_state = self.config.get_setting(SESSION_RESTORE_STATE_SETTING, [])
    restore_state = [
        entry for entry in restore_state
        if entry.get("session_id") != metadata.session_id
    ]
    restore_state.append(asdict(metadata))

    # Keep only recent 50 entries
    self.config.set_setting(SESSION_RESTORE_STATE_SETTING, restore_state[-50:])
```

## Daemon Instance ID Verification

To prevent attaching to sessions from previous daemon instances, the system verifies daemon instance IDs:

### Instance ID Tracking

- **Daemon startup**: Each daemon instance generates a unique instance ID
- **Client awareness**: GTK tracks the daemon's current instance ID
- **Metadata comparison**: Restore only matches current daemon instance

### Stale Session Detection

```python
def get_restorable_sessions(self, client, sessions=None) -> List[SessionRestoreMetadata]:
    """Get sessions that can be restored to current daemon instance."""
    current_instance_id = getattr(client, "server_instance_id", "") or ""
    if not current_instance_id:
        return []

    # Prefer a pre-fetched list from GtkClientBridge so the GTK main loop
    # never blocks on client.list_sessions().
    active_sessions = {
        str(session.id)
        for session in (sessions if sessions is not None else client.list_sessions() or [])
    }
    restore_state = self.config.get_setting(SESSION_RESTORE_STATE_SETTING, [])
    restorable = []

    for entry in restore_state:
        metadata = SessionRestoreMetadata(**entry)

        # Skip if wrong daemon instance
        if metadata.daemon_instance_id != current_instance_id:
            continue

        # Verify session still exists in daemon
        if metadata.session_id not in active_sessions:
            continue

        restorable.append(metadata)

    return restorable
```

## Replay from Last Sequence

When reattaching to a persistent session, GTK requests replay from its last known sequence:

### Sequence Tracking

- **Output sequences**: Each terminal output byte has an absolute sequence number
- **Last known**: GTK tracks the last sequence it received before detach
- **Replay request**: Reattach specifies `from_sequence` for gap filling

### Reattachment Flow

```python
def attach_daemon_session(self, client, bridge, session_id, *,
                         connection_id=None, from_sequence=0):
    """Attach to existing daemon session with replay."""
    controller = DaemonTerminalSessionController(
        client=client,
        bridge=bridge,
        connection_id=connection_id or self.connection.id,
        view_id=str(uuid.uuid4()),  # GTK-local view identifier
        on_output=self._on_daemon_output,
        on_continuity_lost=self._on_continuity_lost,
        on_error=self._on_daemon_error,
    )

    # Attach with replay from last known sequence
    controller.attach(
        want_output=True,
        request_input=True,
        from_sequence=from_sequence,
    )

    self._daemon_controller = controller
    return True
```

### Replay Processing

```python
def _on_session_attached(self, result) -> None:
    """Handle session attach completion."""
    self._tab_state.attachment_id = result.attachment.id
    self._tab_state.input_owner = result.attachment.input_owner
    self._tab_state.expected_sequence = result.live_sequence

    # Check if we have replay data to process
    if result.available_start < result.live_sequence:
        self._tab_state.state = TerminalSessionState.REPLAYING
    else:
        self._tab_state.state = TerminalSessionState.ACTIVE
```

## Truncation Handling with Local Markers

When daemon replay buffer cannot provide complete history, GTK shows local truncation markers:

### Continuity Loss Detection

The daemon detects when requested replay sequence is not available:

- **Buffer limits**: Daemon maintains bounded 2MB replay buffers per session
- **Sequence gaps**: When `from_sequence` < `available_start`, data was truncated
- **Loss notification**: Daemon notifies GTK of continuity loss via callback

### Local Marker Display

GTK shows a local marker without sending it to the daemon:

```python
def _on_continuity_lost(self):
    """Show local continuity loss marker - never sent to daemon."""
    marker = "\r\n[sshPilot: Terminal output was truncated]\r\n"

    # Feed marker locally only - never to daemon replay
    if self.backend:
        self.backend.feed(marker.encode())
        self.backend.commit()
    elif hasattr(self, 'vte') and self.vte:
        self.vte.feed(marker.encode())
```

### Marker Properties

- **Local only**: Continuity markers exist only in GTK display
- **Never replayed**: Markers never enter daemon replay buffer
- **User visible**: Clear indication of missing output
- **Non-persistent**: Markers don't survive further detach/reattach cycles

## Live Sessions Dialog

The developer-facing live sessions dialog enables discovery and reattachment to daemon sessions:

### Dialog Features

- **Session listing**: Shows all live daemon sessions with state and attachment info
- **Connection mapping**: Maps session IDs back to saved connection names
- **Reattachment**: Create new GTK tab attached to existing session
- **Termination**: Force terminate sessions from dialog

### Session Discovery

```python
def _refresh_sessions(self, *_args):
    """Load and display live daemon sessions."""
    self.bridge.submit(
        lambda: self.client.list_sessions(),
        on_success=self._on_sessions_loaded,
        on_error=self._on_sessions_error,
    )

def _on_sessions_loaded(self, sessions):
    """Display session list with attachment and ownership info."""
    for session in sessions:
        row = self._create_session_row(session)
        row.set_subtitle(
            f"state={session.state} attachments={session.attachment_count} "
            f"input_owner={session.input_owner.attachment_id if session.input_owner else 'none'}"
        )
        self._sessions_list.append(row)
```

### Reattachment Action

```python
def _attach_to_session(self, session):
    """Create new GTK tab attached to existing session."""
    # Find connection for session
    connection = self._find_connection_for_session(session.connection_id)
    if not connection:
        return

    # Create new terminal widget
    terminal = TerminalWidget(connection, self.window.config, connection_manager)

    # Add to tab view
    page = self.window.tab_view.append(terminal)
    page.set_title(connection.nickname)

    # Attach to existing daemon session
    terminal.attach_daemon_session(
        self.client,
        self.bridge,
        session.id,
        connection_id=session.connection_id,
    )
```

## Stale Session Handling After Daemon Restart

When the daemon restarts, all previous sessions become stale and unrecoverable:

### Automatic Cleanup

- **Instance mismatch**: Restore only matches current daemon instance ID
- **Stale detection**: Sessions from previous daemon instances are ignored
- **Metadata cleanup**: Stale entries can be cleaned from restore metadata

### User Experience

- **Graceful degradation**: Stale sessions simply don't appear in restore list
- **No errors**: No confusing errors about missing sessions
- **Fresh start**: New GTK session starts clean after daemon restart

## Restoration Metadata Fields

The complete restoration metadata schema:

```python
@dataclass
class SessionRestoreMetadata:
    # Required identifiers
    session_id: str              # Daemon session identifier (e.g. session-12)
    daemon_instance_id: str      # Daemon instance for stale detection
    connection_id: str           # SSH connection identifier

    # Replay positioning
    last_sequence: int           # Last received output sequence number

    # Display information
    tab_title: str              # Tab display title
    view_id: str                # GTK view identifier

    # Metadata
    timestamp: float             # Persistence timestamp
```

### Security Properties

- **No secrets**: Never contains passwords, keys, or sensitive data
- **No output**: Never contains terminal output or command history
- **Identifiers only**: Contains only connection and sequence identifiers
- **Config storage**: Persisted in user config, not shared storage

### Privacy Properties

- **Connection references**: Contains connection IDs but not connection details
- **Sequence numbers**: Contains replay positions but not output content
- **Local only**: Restoration data never leaves user's machine
- **User controlled**: Can be disabled via `terminal.daemon_restore_sessions = false`

## Configuration Settings

Session reattachment behavior is controlled by these settings:

```python
# Enable/disable session restoration across GTK restarts
RESTORE_SESSIONS_SETTING = "terminal.daemon_restore_sessions"  # default: True

# Auto-attach to restorable sessions on GTK startup
AUTO_ATTACH_SETTING = "terminal.daemon_auto_attach"  # default: False

# Internal: restoration metadata storage
SESSION_RESTORE_STATE_SETTING = "terminal.daemon_session_restore_state"
```

### Setting Effects

- **Restore disabled**: No session persistence, normal termination on GTK exit
- **Auto-attach disabled**: Restorable sessions shown in dialog only, manual reattach
- **Auto-attach enabled**: GTK startup automatically creates tabs for restorable sessions

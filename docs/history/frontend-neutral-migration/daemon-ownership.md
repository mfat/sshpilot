# Daemon and frontend ownership

This document defines the intended ownership boundary. The local daemon
currently owns connection CRUD/events, daemon-lifetime sessions, PTYs,
terminal streams, and typed authentication/trust brokering.

## Current ownership and known exceptions

Phase 9 completes the GTK terminal migration to daemon ownership:

- **Production SSH terminals**: Daemon now owns SSH processes and PTY lifecycle for production GTK terminals
- **Multi-attachment**: Multiple GTK tabs can attach to the same daemon session
- **Session persistence**: SSH sessions survive GTK restarts through detach/reattach
- **Input ownership**: Exclusive input control with claim/release API
- **VTE emulation**: Unified VTE-based terminal emulation for all daemon SSH sessions

Phase 9.1 hardens activation ownership: route selection
(`SshTerminalRoute`) is independent of daemon readiness. A selected daemon
route that is not ready shows a clear error and never silently launches
GTK-owned local SSH.

Local shell tabs and user-selected external terminals are explicit exceptions:
their processes are owned by GTK/the external emulator, not by `sshpilotd`.
They must not be described as daemon sessions. A GObject `ConnectionManager`
may remain as an explicitly selected compatibility adapter, but it is not the
saved-connection authority for the daemon route; daemon-fed DTO stores are the
production frontend boundary.

The concrete current client contract is maintained in the
[API reference](../../api/README.md). This document describes intended ownership;
it does not advertise runtime capabilities.

## Architectural direction

```text
GTK / Tauri / CLI
       -> SshPilotClient
       -> DaemonClient
       -> sshpilotd for connection CRUD/events and remote session lifecycle
```

Protocol models are independent from the in-process adapter and any future Unix
socket, named pipe, or WebSocket framing.

## Core ownership

The core owns:

- connection and group persistence and validation;
- persistent application settings with domain semantics;
- SSH configuration parsing and native SSH command construction;
- the single native authentication resolution path;
- SSH process, PTY, terminal-session and reconnect lifecycle;
- terminal input, output bytes, dimensions, attachments and replay bounds;
- authentication, passphrase, host-key and keyboard-interactive coordination;
- secret-backend access and credential metadata;
- SFTP operations and file-transfer state;
- port-forwarding processes and state;
- plugin execution and core-domain events.

The core API never exposes GTK, GObject, Adwaita, VTE, WebKit widgets,
frontend controllers, dialogs, callbacks used as domain values, subprocess
objects, secret-provider objects, or raw PTY file descriptors.

In daemon mode, `sshpilotd` is also the single writer and reload owner for SSH
configuration, included fragments, JSON-backed connections, and connection
metadata. External edits are
detected and transactionally reloaded by the daemon as described in
[configuration reload](../../architecture/configuration-reload.md).

## Frontend ownership

Frontends own:

- widgets, terminal rendering, windows, dialogs and prompt presentation;
- tabs, pane layouts, navigation, sidebar presentation and selection;
- geometry, theme, fonts, CSS, animation, focus and shortcuts;
- toasts and user-facing notifications;
- frontend-specific transient state and local selection state.

GTK-specific GObject/GLib adapters may exist at the frontend boundary. They are
not the canonical domain contract.

## Protocol v1 session decisions

- The daemon owns session records, exact runner handles, SSH processes, PTYs,
  replay/input queues, and typed interaction records.
- Process start, terminate, kill, and wait execute only on the daemon's bounded
  keyed session executor. The selector owns request validation and response
  framing, never those runner calls.
- A session may live without a frontend.
- Attachment and session lifetime are separate. Detaching does not close a
  session; closing is explicit.
- The first eligible terminal attachment is the daemon-authoritative input
  owner; later attachments are view-only until ownership is released.
- Terminal output is bytes and is ordered per session.
- A bounded per-session replay buffer is required in the daemon phase.
- Typed prompts are visible only to the originating or attached eligible
  clients; one explicit responder claim wins.
- Secret values cross only through a nonce-bound one-use binary response from
  the claimed frontend, or are retrieved directly by the daemon through the
  existing selected backend. They are never ordinary connection fields.
- If no prompt-capable frontend is attached, the operation fails or times out
  with a structured interaction error.
- Frontend disconnect does not by itself terminate the session.
- The future daemon may outlive all frontend windows.
- Remote access, idle daemon expiry, and session-expiry policy are out of scope
  until the daemon lifecycle phase.

### Terminal stream rules

Terminal input and output remain `bytes` in Python. PTY/process-group ownership,
binary framing, replay, and slow-peer isolation are defined in
[terminal streaming](../../architecture/terminal-streaming.md).
- Invalid UTF-8 is preserved.
- Public models carry sequence metadata, never PTY descriptors.
- Future transports may use binary frames; Base64 JSON is not required.
- Output should be batched rather than emitting one protocol message per tiny
  PTY read.
- Each session needs a bounded output queue, earliest/latest replay bounds,
  truncation indication, and an explicit slow-client policy.
- Detached clients consume no live delivery capacity; the bounded replay buffer
  is their catch-up mechanism.

## State ownership

### Core-owned state

- connections, groups and core settings;
- runtime session records, SSH processes, PTYs and lifecycle;
- reconnect policy/state;
- output sequence and replay bounds;
- transfer and forwarding state;
- credential metadata and vault session state;
- plugin runtime state and core operation status.

### Frontend-owned state

- window geometry and sidebar width;
- selected tab, navigation location, pane arrangement and keyboard focus;
- terminal font, theme, renderer preferences and animation;
- dialog/toast visibility;
- local list selection and transient transfer presentation.

### Ambiguous state decisions

| State | Decision |
| --- | --- |
| Open tabs versus active sessions | Tabs are frontend attachments. Core sessions may exist with zero tabs. Closing a tab detaches unless the user explicitly closes the session. |
| Saved terminal layouts | Frontend-owned composition referencing opaque connection/session intents. Current `SessionManager` remains a layout store. |
| Recent connections | Last-used timestamp is core metadata; how many and how they render is frontend-owned. |
| Terminal titles | Core may expose remote/session title metadata; each frontend chooses presentation and user overrides. |
| Per-session display preferences | Renderer/font/theme stay frontend-owned. Session terminal dimensions are core-owned runtime state. |
| Connection selection | Frontend-owned. |
| Transfer presentation | Progress bytes/state are core-owned; selected rows, expanded details and dialogs are frontend-owned. |
| Group color/expanded state | Group identity and membership are core-owned. Color, expansion and sidebar order need a later compatibility split because current persistence stores them together. |

## Protocol v1 contract decisions

- Commands are synchronous for `InProcessClient`, with a frontend-neutral event
  subscription. Calling convention and wire protocol are separate decisions.
- In-process advertises only the three connection capabilities. The daemon
  additionally advertises `sessions.read`, `sessions.write`, and
  `sessions.events`.
- Terminal, interaction, SFTP, forwarding, plugin, and secret models do not
  imply runtime support.
- Unsupported methods raise `unsupported_capability`, never
  `NotImplementedError` or a missing method.
- Public DTO fields are mapped explicitly. `__dict__`, GObject properties,
  internal data dictionaries and manager instances are never serialized.
- Connection DTOs omit passwords, passphrases, private keys, provider tokens,
  environment variables and internal source paths.
- Connection IDs are the SSH Host alias (`connection.id == connection.nickname`).
  Rename is deletion of the old alias plus creation of the new alias.
- Expected failures use stable `ErrorCode` values. Unexpected exceptions are
  logged internally and become safe `internal_error` responses.

## Event guarantees in this phase

- `InProcessClient` adapts existing `connection-added`,
  `connection-updated`, and `connection-removed` GObject signals.
- Events have a process-local monotonically increasing sequence.
- Delivery uses a publisher-global serial FIFO in sequence order. The first
  active publisher drains the queue; concurrent publishers wait and their
  callbacks may run on that dispatcher thread.
- Re-entrant publication queues behind the current subscriber snapshot and
  does not recurse.
- Subscriber exceptions are isolated and logged.
- Unsubscribe and close are idempotent; client close removes manager handlers,
  rejects new events, and lets already accepted events finish.
- Daemon session and connection lifecycle events share one global sequence and
  bounded per-peer queues. Client callback dispatch is separate from socket
  reading, so slow subscribers cannot block responses.
- Delivery is not durable and has no reconnect replay.

## Current behaviour conflicts

- Existing `ConnectionState` is an aggregate of terminal widget states, not
  persistent reachability health.
- Existing `SessionManager` stores frontend tab layouts.
- `TerminalWidget` and `TerminalManager` own both core mechanics and UI.
- Active terminal maps depend on live `Connection` and widget identity.
- Plugin session events currently carry terminal objects.
- Shutdown and prompt routing are distributed across GTK, workers, askpass and
  secret backend code.

These conflicts are documented migration inputs, not reasons to silently alter
current behaviour.

## Next-phase backlog

1. Extend the process-runner boundary to the existing native SSH builder
   without changing authentication.
2. Add PTY ownership and binary terminal input/output/resize framing.
3. Define one-input-owner arbitration and replay bounds.
4. Add byte output batching, bounded queues, replay bounds and truncation.
5. Route prompts through interaction IDs with timeout/cancellation.
6. Add Windows named-pipe transport requirements.
7. Migrate GTK terminal rendering to the byte-stream contract, initially with
   the existing renderers; phase VTE out only after parity.
8. Add `sshpilotctl` and interactive CLI attach/detach/resize.
9. Split core plugin execution from frontend UI contributions.
10. Define daemon lifecycle, idle shutdown and session expiry.
11. Update Flatpak, DEB/RPM, Homebrew, macOS and Windows packaging only after
    lifecycle and transport are stable.
12. Build the Svelte/Tauri frontend after protocol negotiation and terminal
    streaming are contract-tested.
13. Design separately secured remote/mobile access only as a later, explicit
    project.


## Phase 10 extended services

Daemon ownership now also covers:

- SFTP services (`docs/architecture/sftp-services.md`)
- File transfers (`docs/architecture/file-transfers.md`)
- Port forwards (`docs/architecture/port-forwarding.md`)
- Cross-cutting lifecycle (`docs/architecture/extended-service-lifecycle.md`)

Production GTK routes (file manager, uploads/downloads, plugin
`ensure_local_forward`) use the daemon when capabilities are present. Silent
local process fallback is forbidden; see `sshpilot.extended_service_policy`
and the legacy settings documented in the service architecture notes.

Terminal PTY transport is unchanged. Config-static Host forwards remain
bound to the interactive terminal process. Ordinary SCP UI is gated behind
`file_manager.legacy_scp` until migrated.

## Logging ownership

Frontend and daemon processes use the same GTK-free logging policy for level
normalization, format, redaction, managed-handler lifecycle, and correlation
context. Their rotating files remain process-specific: `sshpilot.log`,
`app.log`, and `ssh.log` belong to the frontend; `daemon.log` belongs to the
daemon. The frontend may tail and forward new daemon records in explicit
verbose mode, but it never opens `daemon.log` for writing. The local Log Viewer
reads each source directly and does not create a merged RPC log stream.

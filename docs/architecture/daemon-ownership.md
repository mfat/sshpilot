# Daemon and frontend ownership

This document defines the intended ownership boundary. It does not claim that a
daemon or daemon transport exists.

## Architectural direction

```text
GTK / Tauri / CLI
       -> SshPilotClient
       -> InProcessClient today
       -> DaemonClient later
       -> sshpilotd later
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

- The core owns the SSH process and PTY.
- A session may live without a frontend.
- Attachment and session lifetime are separate. Detaching does not close a
  session; closing is explicit.
- Initially one attachment owns terminal input. Observer support can be added
  later without weakening that rule.
- Terminal output is bytes and is ordered per session.
- A bounded per-session replay buffer is required in the daemon phase.
- Prompts are routed to the client that initiated the relevant operation.
- Secret values cross the boundary only when a prompt-capable frontend is
  actively collecting an answer. They are never ordinary connection fields.
- If no prompt-capable frontend is attached, the operation fails or times out
  with a structured interaction error.
- Frontend disconnect does not by itself terminate the session.
- The future daemon may outlive all frontend windows.
- Remote access, idle daemon expiry, and session-expiry policy are out of scope
  until the daemon lifecycle phase.

### Terminal stream rules

- Terminal input and output remain `bytes` in Python.
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
- `connections.read` is the only advertised runtime capability in this phase.
- Connection write, terminal, interaction, SFTP, forwarding, plugin, and secret
  models do not imply runtime support.
- Unsupported methods raise `unsupported_capability`, never
  `NotImplementedError` or a missing method.
- Public DTO fields are mapped explicitly. `__dict__`, GObject properties,
  internal data dictionaries and manager instances are never serialized.
- Connection DTOs omit passwords, passphrases, private keys, provider tokens,
  environment variables and internal source paths.
- Current connection IDs are transitional opaque hashes of protocol and
  nickname. They change on rename because persistence has no immutable UUID.
- Expected failures use stable `ErrorCode` values. Unexpected exceptions are
  logged internally and become safe `internal_error` responses.

## Event guarantees in this phase

- `InProcessClient` adapts existing `connection-added`,
  `connection-updated`, and `connection-removed` GObject signals.
- Events have a process-local monotonically increasing sequence.
- Delivery is synchronous, in subscriber-registration order, on the source
  signal thread.
- Subscriber exceptions are isolated and logged.
- Unsubscribe and close are idempotent; client close removes manager handlers.
- Delivery is not durable and slow subscribers block later subscribers. This
  limitation must change before terminal streams or daemon delivery.

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

1. Extract a headless terminal-session service around the existing native SSH
   builder without changing authentication.
2. Move SSH process and PTY ownership behind that service.
3. Add session open/attach/detach/close and one-input-owner enforcement.
4. Add byte output batching, bounded queues, replay bounds and truncation.
5. Route prompts through interaction IDs with timeout/cancellation.
6. Specify a versioned message envelope and Unix-domain socket framing.
7. Add Windows named-pipe transport requirements.
8. Implement `DaemonClient`, then `sshpilotd`, and reuse the connection contract
   tests against it.
9. Add per-user socket security, version negotiation, stale-daemon and
   single-instance handling.
10. Migrate GTK terminal rendering to the byte-stream contract, initially with
    the existing renderers; phase VTE out only after parity.
11. Add `sshpilotctl` and interactive CLI attach/detach/resize.
12. Split core plugin execution from frontend UI contributions.
13. Define daemon lifecycle, idle shutdown and session expiry.
14. Update Flatpak, DEB/RPM, Homebrew, macOS and Windows packaging only after
    lifecycle and transport are stable.
15. Build the Svelte/Tauri frontend after protocol negotiation and terminal
    streaming are contract-tested.
16. Design separately secured remote/mobile access only as a later, explicit
    project.


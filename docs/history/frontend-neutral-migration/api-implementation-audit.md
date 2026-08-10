# API implementation audit

> **Superseded for Phase 10–13.2 runtime status.** Prefer
> [capabilities.md](../../api/capabilities.md), [methods.md](../../api/methods.md), and the
> [current API topic guides](../../api/README.md). This inventory
> still documents early Protocol v1 scaffolding and should not be read as the
> current SFTP/forward/interaction/terminal capability matrix.

Audit date: 2026-07-29 (historical). This inventory describes the repository after the
daemon-owned session lifecycle foundation. “Snapshot” means the
name/field/value surface is protected by
`tests/api/snapshots/public_api.json`; it does not prove runtime semantics.

## Client methods

| Public element | Location | Implemented at runtime | Advertised capability | Contract-tested | Documented | Notes |
| --- | --- | ---: | --- | ---: | ---: | --- |
| `get_capabilities` | `api/client.py`, both client adapters | Yes | Bootstrap | Both clients | Yes | Daemon result is negotiated; cached after close |
| `list_connections` | same | Yes | `connections.read` | Both clients | Yes | Preserves manager order |
| `get_connection` | same | Yes | `connections.read` | Both clients | Yes | Safe DTO or `connection_not_found` |
| `create_connection` | same | Yes | `connections.write` | Shared mutation contract | Yes | Basic secret-free SSH metadata |
| `update_connection` | same | Yes | `connections.write` | Shared mutation contract | Yes | Partial basic update; preserves advanced data internally |
| `delete_connection` | same | Yes | `connections.write` | Shared mutation contract | Yes | Takes `DeleteConnectionRequest` |
| `list_sessions` | same | Daemon only | `sessions.read` | Daemon integration | Yes | Creation-ordered daemon-lifetime snapshot |
| `get_session` | same | Daemon only | `sessions.read` | Daemon integration | Yes | Strict `session-<n>` lookup |
| `open_session` | same | Daemon only | `sessions.write` | Lifecycle + IPC | Yes | Creates a real record; production runner fails safely until PTY phase |
| `attach_session` | same | Daemon only | `sessions.write` | Multi-client + IPC | Yes | Logical attachment; no stream |
| `detach_session` | same | Daemon only | `sessions.write` | Multi-client + IPC | Yes | Idempotent caller-owned detach |
| `close_session` | same | Daemon only | `sessions.write` | Lifecycle + IPC | Yes | Bounded exact-owned-process termination |
| `send_terminal_input` | same | No | Not advertised: `terminal` | Unsupported contract | Yes | Bytes schema only |
| `resize_terminal` | same | No | Not advertised: `terminal` | Unsupported contract | Yes | Dimensions schema only |
| `replay_terminal` | same | No | Not advertised: `terminal.replay` | Both clients unsupported | Yes | Coherent schema-only method |
| `respond_to_interaction` | same | No | Not advertised: `interactions` | Unsupported contract | Yes | Current dialogs are outside API |
| `subscribe_events` | same | Yes | `connections.events`, `sessions.events` | Both clients / daemon lifecycle | Yes | Typed connection and session events; local continuity errors |
| `close` | same | Yes | None | Yes | Yes | Idempotent; releases adapter subscriptions |

## Capabilities

| Public element | Location | Implemented at runtime | Advertised capability | Contract-tested | Documented | Notes |
| --- | --- | ---: | --- | ---: | ---: | --- |
| `connections.read` | `api/capabilities.py` | Yes | Yes | Behaviour + snapshot | Yes | Snapshot list/get |
| `connections.events` | same | Yes | Yes | Behaviour + transport integration | Yes | Bounded live delivery; no replay |
| `connections.write` | same | Yes | Yes | Shared API/daemon mutation contracts | Yes | All three methods implemented |
| `sessions.read` | same | Daemon only | Daemon only | Snapshot and IPC lifecycle | Yes | List/get daemon-lifetime records |
| `sessions.write` | same | Daemon only | Daemon only | Open/attach/detach/close lifecycle | Yes | No terminal byte ownership implied |
| `sessions.events` | same | Daemon only | Daemon only | Codec, ordering, multi-client | Yes | Four typed lifecycle events |
| `terminal` | same | No | No | Unsupported + snapshot | Yes | Existing GTK terminal path bypasses API |
| `terminal.attach` | same | No | No | Unsupported + snapshot | Yes | Schema vocabulary only |
| `terminal.replay` | same | No | No | Unsupported + snapshot | Yes | Schema-only client method exists |
| `interactions` | same | No | No | Unsupported + snapshot | Yes | Response method is a stub |
| `sftp` | same | No | No | Snapshot only | Yes | No client methods/events |
| `port_forwarding` | same | No | No | Snapshot only | Yes | No client methods/events |
| `plugins` | same | No | No | Snapshot only | Yes | Separate legacy plugin API exists |
| `secrets` | same | No | No | Snapshot only | Yes | No direct frontend secret API |

No advertised capability lacks runtime operations or contract tests. The
daemon filters its negotiated set to the three connection capabilities and
adds the three daemon-session lifecycle capabilities. `InProcessClient`
truthfully leaves all session lifecycle operations unsupported.

## Daemon transport

| Public element | Runtime | Contract-tested | Notes |
| --- | ---: | ---: | --- |
| Request/success/error/event envelopes | Yes | Yes | Strict fields and JSON-safe values |
| Length-prefixed JSON framing | Yes | Yes | 4-byte big-endian length; 1 MiB maximum |
| `system.handshake` | Yes | Yes | Required once; exact Protocol `1.0` selection |
| `system.get_capabilities` | Yes | Yes | Negotiated daemon result |
| Connection read/write methods | Yes | Shared parity | Delegates to `InProcessClient` |
| Six `sessions.*` methods | Daemon only | Codec, lifecycle, multi-client, ambiguity | Delegates to daemon-owned `SessionRuntime` |
| Unix socket lifecycle/security | Yes | Yes | Owned 0700 directory, 0600 socket, safe stale cleanup |
| Daemon runtime event forwarding | Yes | Codec, multi-client, ordering, interleaving, backpressure, shutdown | Connection and session lifecycle events; bounded queue disconnects on overflow |

## Events

| Public element | Location | Implemented at runtime | Advertised capability | Contract-tested | Documented | Notes |
| --- | --- | ---: | --- | ---: | ---: | --- |
| `connection.created` | `api/events.py`, `api/in_process_client.py`, daemon transport | Yes | `connections.events` | Both clients + codec | Yes | Translated from `connection-added` |
| `connection.updated` | same | Yes | `connections.events` | Both clients + codec | Yes | Translated from `connection-updated` |
| `connection.deleted` | same | Yes | `connections.events` | Both clients + codec | Yes | Translated from `connection-removed` |
| `session.created` | `api/events.py`, `daemon/session_runtime.py` | Daemon only | `sessions.events` | Runtime, codec, multi-client | Yes | `SessionSummary` at allocation |
| `session.state_changed` | same | Daemon only | `sessions.events` | Transition matrix, codec, IPC | Yes | Typed `SessionSummary` |
| `session.output` | same | No | Not advertised: `terminal` | Snapshot only | Yes | No queue/batching/replay |
| `session.interaction_requested` | same | No | Not advertised: `interactions` | Snapshot only | Yes | No interaction broker |
| `session.exited` | same | Daemon only | `sessions.events` | Exit/close race + codec | Yes | Typed `SessionExitInfo` and session ID |
| `session.closed` | same | Daemon only | `sessions.events` | Lifecycle + multi-client | Yes | Final typed `SessionSummary` |
| `error.occurred` | same | Local daemon-client continuity only | None fixed | Payload safety + transport tests | Yes | Safe structured error mapping |

`EventPublisher` ordering, cleanup, idempotent subscription closure, and
subscriber exception isolation are behaviour-tested. Connection and session
lifecycle events share the daemon-global sequence and bounded peer queues.

## Errors

| Public element | Location | Implemented at runtime | Advertised capability | Contract-tested | Documented | Notes |
| --- | --- | ---: | --- | ---: | ---: | --- |
| `unsupported_capability` | `api/errors.py` | Yes | All unavailable groups | Behaviour + snapshot | Yes | Safe capability detail |
| `invalid_request` | same | Yes | Lifecycle/threading | Behaviour + snapshot | Yes | Closed/wrong-thread cases |
| `validation_failed` | same | Defined only | Future operations | Envelope + snapshot | Yes | Not emitted by runtime client |
| `connection_not_found` | same | Yes | `connections.read` | Behaviour + snapshot | Yes | Includes safe opaque ID |
| `session_not_found` | same | Daemon only | `sessions.read`/`write` | Runtime + IPC | Yes | Strict opaque session lookup |
| `session_already_closed` | same | Daemon only | `sessions.write` | Attachment lifecycle | Yes | Rejects new attachment to a final record |
| `session_invalid_state` | same | Reserved | `sessions.write` | Snapshot + transition tests | Yes | Public code for future runtime conflicts |
| `session_startup_failed` | same | Daemon only | `sessions.write` | Sanitisation + IPC | Yes | No raw runner exception |
| `session_termination_failed` | same | Daemon only | `sessions.write` | Bounded close tests | Yes | Exact-owned-resource failure |
| `unsupported_session_protocol` | same | Daemon only | `sessions.write` | Runtime tests | Yes | Non-SSH connection rejected |
| `interaction_not_found` | same | No | Future interactions | Snapshot only | Yes | Schema code |
| `interaction_already_answered` | same | No | Future interactions | Snapshot only | Yes | Schema code |
| `permission_denied` | same | Daemon only | `sessions.write` | Attachment ownership | Yes | Caller cannot detach another client |
| `operation_cancelled` | same | No | Future cancellable operations | Snapshot only | Yes | No cancellation API |
| `operation_timed_out` | same | No | Future bounded operations | Snapshot only | Yes | No timeout API |
| `internal_error` | same | Yes | `connections.read` | Behaviour + snapshot | Yes | Adapter logs original exception |

## Models

All field lists, defaults, types, and synthetic examples are documented in the
[generated model index](../../api/generated/model-index.md).

| Public element | Location | Runtime | Capability/domain | Contract-tested | Documented | Notes |
| --- | --- | ---: | --- | ---: | ---: | --- |
| `ClientInfo` | `models/common.py` | Yes | Discovery | Validation + snapshot | Yes | Optional client ID |
| `CoreInfo` | same | Yes | Discovery | Validation + snapshot | Yes | In-process implementation identity |
| `CompatibilityResult` | same | Yes | Discovery | Snapshot | Yes | No negotiation runtime |
| `Capabilities` | `api/capabilities.py` | Yes | Discovery | Behaviour + snapshot | Yes | Immutable set |
| `CoreEvent` | `api/events.py` | Partial | Events | Behaviour + snapshot | Yes | Generic payload not tied to event type |
| `GroupReference` | `models/connections.py` | Yes | `connections.read` | Behaviour + snapshot | Yes | Safe group projection |
| `ConnectionSummary` | same | Yes | `connections.read` | Behaviour + snapshot | Yes | Secret-free |
| `ConnectionDetails` | same | Yes | `connections.read` | Behaviour + snapshot | Yes | Safe booleans instead of paths |
| `CreateConnectionRequest` | same | Yes | `connections.write` | Validation, codec, shared contract | Yes | Minimal fields only; no secrets |
| `UpdateConnectionRequest` | same | Yes | `connections.write` | Validation, codec, shared contract | Yes | Null means unchanged |
| `DeleteConnectionRequest` | same | Yes | `connections.write` | Validation, codec, shared contract | Yes | Deliberate request wrapper |
| `DeleteConnectionResult` | same | Yes | `connections.write` | Validation, codec, shared contract | Yes | Validates ID and boolean |
| `ConnectionValidationError` | same | No | `connections.write` | Snapshot only | Yes | Safe field/code/message intended |
| `ConnectionValidationResult` | same | No | `connections.write` | Validation + snapshot | Yes | Valid result cannot contain errors |
| `OpenSessionRequest` | `models/sessions.py` | Daemon | `sessions.write` | Validation, codec, daemon integration | Yes | Stable connection ID only |
| `InputOwner` | same | No | `terminal.attach` | Snapshot only | Yes | Reserved until terminal input |
| `SessionCapabilities` | same | Daemon | `sessions.read` | Codec + integration | Yes | Empty in Phase 6 |
| `SessionExitInfo` | same | Daemon | `sessions.events` | Validation, codec, lifecycle | Yes | Safe process-exit projection |
| `SessionFailure` | same | Daemon | `sessions.events` | Validation, codec, sanitisation | Yes | Stable code and safe message |
| `SessionSummary` | same | Daemon | `sessions.read` | Validation, codec, lifecycle | Yes | Immutable runtime snapshot |
| `AttachSessionRequest` | same | Daemon | `sessions.write` | Validation, codec, multi-client | Yes | Caller identity is server-derived |
| `AttachmentInfo` | same | Daemon | `sessions.write` | Codec + multi-client | Yes | Logical attachment only |
| `AttachSessionResult` | same | Daemon | `sessions.write` | Codec + integration | Yes | No PTY or replay data |
| `DetachSessionRequest` | same | Daemon | `sessions.write` | Codec + idempotency | Yes | Caller-owned attachment |
| `CloseSessionRequest` | same | Daemon | `sessions.write` | Codec + lifecycle | Yes | Bounded exact-resource close |
| `TerminalDimensions` | `models/terminal.py` | No | `terminal` | Validation + snapshot | Yes | 1–10,000 |
| `TerminalInput` | same | No | `terminal` | Bytes/repr + snapshot | Yes | `data` excluded from repr |
| `TerminalOutput` | same | No | `terminal` | Bytes/repr + snapshot | Yes | `data` excluded from repr |
| `ResizeTerminalRequest` | same | No | `terminal` | Snapshot only | Yes | Schema only |
| `ReplayRequest` | same | No | `terminal.replay` | Validation + snapshot | Yes | Unsupported `replay_terminal` |
| `ReplayBounds` | same | No | `terminal.replay` | Validation + snapshot | Yes | Bounds validation |
| `ReplayResult` | same | No | `terminal.replay` | Repr + snapshot | Yes | Bytes excluded from repr |
| `InteractionRequest` | `models/interactions.py` | No | `interactions` | Validation + snapshot | Yes | Requests must start pending |
| `InteractionResponse` | same | No | `interactions` | Validation/repr + snapshot | Yes | Secret value excluded from repr |
| `InteractionCancellation` | same | No | `interactions` | Snapshot only | Yes | ID not validated |
| `InteractionTimeout` | same | No | `interactions` | Snapshot only | Yes | ID not validated |
| `InteractionRejection` | same | No | `interactions` | Snapshot only | Yes | ID not validated |
| `TransferSummary` | `models/transfers.py` | No | Future transfer/SFTP | Validation + snapshot | Yes | Transition semantics absent |
| `SftpEntry` | `models/operations.py` | No | `sftp` | Validation + snapshot | Yes | Remote path is public user data |
| `ListDirectoryRequest` | same | No | `sftp` | Validation + snapshot | Yes | No client method |
| `PortForwardSummary` | same | No | `port_forwarding` | Validation + snapshot | Yes | ID is plain `str`, unlike other ID aliases |
| `PluginArgument` | same | No | `plugins` | Repr + snapshot | Yes | Value excluded from repr |
| `PluginOperationRequest` | same | No | `plugins` | Validation + snapshot | Yes | No client method |
| `PluginOperationResult` | same | No | `plugins` | Repr + snapshot | Yes | Potentially sensitive values excluded from repr |

## Public enums and state models

| Public element | Location | Runtime | Capability/domain | Contract-tested | Documented | Notes |
| --- | --- | ---: | --- | ---: | ---: | --- |
| `Capability` | `api/capabilities.py` | Yes | Discovery | Snapshot + capability tests | Yes | Connection and daemon session capabilities advertised truthfully |
| `EventType` | `api/events.py` | Partial | Events | Snapshot + event tests | Yes | Connection and four session lifecycle events emitted |
| `ErrorCode` | `api/errors.py` | Partial | Errors | Snapshot + envelope/transport tests | Yes | Client, domain, handshake, framing, and lifecycle codes |
| `ConnectionHealth` | `models/connections.py` | Partial | `connections.read` | Snapshot | Yes | Only `unknown` produced |
| `AuthenticationMethod` | same | Yes | `connections.read` | Snapshot | Yes | Safe key/password projection |
| `SessionState` | `models/sessions.py` | Daemon | `sessions.read` | Transition table + lifecycle tests | Yes | Separate from legacy GTK terminal state |
| `InteractionKind` | `models/interactions.py` | No | `interactions` | Snapshot | Yes | Six schema kinds |
| `InteractionStatus` | same | No | `interactions` | Validation + snapshot | Yes | Five schema states |
| `TransferDirection` | `models/transfers.py` | No | Transfer/SFTP | Snapshot | Yes | Schema only |
| `TransferState` | same | No | Transfer/SFTP | Snapshot | Yes | Schema only |
| `FileEntryKind` | `models/operations.py` | No | `sftp` | Snapshot | Yes | Schema only |
| `ForwardKind` | same | No | `port_forwarding` | Snapshot | Yes | Schema only |
| `ForwardState` | same | No | `port_forwarding` | Snapshot | Yes | Schema only |

## Version constants

| Public element | Location | Runtime | Advertised capability | Contract-tested | Documented | Notes |
| --- | --- | ---: | --- | ---: | ---: | --- |
| `PROTOCOL_VERSION = "1.0"` | `api/version.py` | Yes | Discovery result | Snapshot/docs drift | Yes | Current contract family |
| `API_IMPLEMENTATION_VERSION = "0.6"` | same | Yes | Discovery result | Snapshot | Yes | Daemon-owned session lifecycle |

## Migrated GTK path

```text
WelcomePage._populate_recent_box
    -> MainWindow.client
    -> InProcessClient or GtkClientBridge + DaemonClient.list_connections()
    -> ConnectionSummary

application-scoped client subscription
    -> GLib main-context handoff
    -> coalesced WelcomePage snapshot refresh
```

Activation still resolves the nickname through `ConnectionManager` before
entering `TerminalManager.connect_to_host`. Therefore the read slice uses the
API, but terminal execution and most of the core remain GTK/GObject-coupled.

## Objective inconsistencies and gaps

1. `sftp`, `port_forwarding`, `plugins`, and `secrets` have capability names
   but no client methods; some have models and none has API events.
2. Daemon connection events are live but reconnect/resume semantics remain
   undefined.
3. Connection IDs are SSH Host aliases. Deprecated
   nickname-hash lookup aliases remain for the bounded Protocol v1 window.
4. Some schema records do not validate their opaque IDs consistently.
5. Behavioural contract coverage is intentionally thin for schema-only models,
    proposed errors, and state transitions. The snapshot protects shape, not
    semantics.
6. The API contract is GTK-free, but `InProcessClient` wraps
    `ConnectionManager`, which is a GObject/GLib component. The core is not yet
    GTK-free.

These issues were recorded rather than silently changing public names or
architecture during the documentation task.

# Models and enums

The public contract uses frozen dataclasses, string enums, and opaque string ID
aliases. These are DTOs, not persistence records. The
[generated model index](generated/model-index.md) is the canonical field-by-field
reference: every model has its types, constructor-required fields, defaults,
sensitive classification, related methods/events, and a synthetic safe
representation. The [machine-readable catalog](generated/schema.json) contains
the same structural data.

“Schema only” means the model can be constructed and validated but no current
`SshPilotClient` runtime operation produces or consumes it.

All documented DTOs, enums, and opaque ID aliases from the model submodules are
convenience exports from `sshpilot.api.models`. Consumers may import specialised
types from their defining module, but the package-level export is the complete
documented model surface.

## Identifier types

| Type | Purpose | Required form | Runtime status |
| --- | --- | --- | --- |
| `ConnectionId` | Saved connection identity | Non-empty opaque string | Implemented; stable UUID-backed ID |
| `SessionId` | Runtime session identity | Non-empty opaque string | Schema only |
| `RequestId` | Request correlation | Non-empty opaque string | Schema only |
| `InteractionId` | Interaction identity | Non-empty opaque string | Schema only |
| `TransferId` | Transfer identity | Non-empty opaque string | Schema only |
| `ClientId` | Frontend identity | Non-empty opaque string | Schema only |
| `AttachmentId` | Session attachment identity | Non-empty opaque string | Schema only |

The aliases are `typing.NewType` wrappers over `str`; they add static intent,
not runtime serialization. Consumers must not parse their contents.
`ConnectionId` is currently rendered as `connection:<canonical UUID>`, remains
stable across rename and reload, and is validated by centralized internal
helpers.

## Discovery and event envelopes

| Model | Purpose | Runtime support |
| --- | --- | --- |
| `ClientInfo` | Frontend name/version and optional ID | Implemented through `get_capabilities` |
| `CoreInfo` | Core name/version/implementation | Implemented through `get_capabilities` |
| `CompatibilityResult` | Compatibility decision and safe message | Implemented; currently always compatible v1 in-process |
| `Capabilities` | Version, endpoint, compatibility, and supported-set result | Implemented |
| `CoreEvent` | Typed payload plus sequence/timestamp/correlation | Partially implemented; connection events only |

`ClientInfo` and `CoreInfo` reject empty required strings. `CoreEvent.sequence`
must be non-negative. Timestamps default to aware UTC values.

## Connection models

| Model | Purpose | Runtime support | Related methods/events |
| --- | --- | --- | --- |
| `GroupReference` | Safe group ID/name projection | Implemented | Embedded in connection DTOs |
| `ConnectionSummary` | Secret-free list/event view | Implemented | `list_connections`, `connection.*` |
| `ConnectionDetails` | Expanded safe connection metadata | Implemented | `get_connection`; future writes |
| `CreateConnectionRequest` | Minimal secret-free create input | Implemented | `create_connection` |
| `UpdateConnectionRequest` | Optional basic-metadata update fields | Implemented | `update_connection` |
| `DeleteConnectionRequest` | Request-form deletion ID | Implemented | `delete_connection` |
| `DeleteConnectionResult` | Deletion acknowledgement | Implemented | `delete_connection` |
| `ConnectionValidationError` | Field/code/message validation item | Schema only | Future writes |
| `ConnectionValidationResult` | Valid flag plus validation items | Schema only | Future validation |

Validation rules:

- IDs and nicknames are non-empty.
- Ports are between 1 and 65535.
- Protocol strings are non-empty.
- `ConnectionDetails.forwarding_rule_count` is non-negative.
- A valid `ConnectionValidationResult` cannot contain errors.
- `ConnectionSummary.display_target` chooses `hostname`, then `host`, then
  nickname and prepends `username@` when present.

Connection DTOs deliberately omit passwords, passphrases, private paths,
provider objects, SSH environments, raw config records, GTK/GObject instances,
and terminal state. `identity_configured` and `certificate_configured` are
booleans, not paths.

## Session and attachment models

| Model | Purpose | Runtime support |
| --- | --- | --- |
| `OpenSessionRequest` | Connection/client pair for a future session | Schema only |
| `InputOwner` | Client and attachment allowed to send input | Schema only |
| `SessionCapabilities` | Per-session feature strings | Schema only |
| `SessionExitInfo` | Exit code, signal, and safe reason | Schema only |
| `SessionSummary` | Runtime session snapshot | Schema only |
| `AttachSessionRequest` | Attach a client and optionally request input | Schema only |
| `AttachmentInfo` | Attachment identity and ownership result | Schema only |
| `AttachSessionResult` | Session plus attachment | Schema only |
| `DetachSessionRequest` | Remove one attachment | Schema only |
| `CloseSessionRequest` | Close one runtime session | Schema only |

All present IDs must be non-empty. `created_at` defaults to aware UTC.
`request_input=True` requests ownership but does not define arbitration; no
runtime currently implements it.

## Terminal and replay models

| Model | Purpose | Runtime support |
| --- | --- | --- |
| `TerminalDimensions` | PTY rows and columns | Schema only |
| `TerminalInput` | Attachment-scoped input bytes | Schema only |
| `TerminalOutput` | Sequenced session output bytes | Schema only |
| `ResizeTerminalRequest` | Attachment-scoped dimension update | Schema only |
| `ReplayRequest` | Retained-byte query after a sequence | Schema-only `replay_terminal` request |
| `ReplayBounds` | Retained sequence/byte range | Schema only |
| `ReplayResult` | Replay bytes and continuation metadata | Schema-only `replay_terminal` result |

Rows and columns must be 1–10,000. Input/output/replay data must be `bytes`.
Sequences are non-negative; replay bounds are ordered; retained bytes are
non-negative. `ReplayRequest.max_bytes` defaults to 1 MiB and is limited to
16 MiB. Byte payloads are sensitive transient content and must not be logged.

## Interaction models

| Model | Purpose | Runtime support |
| --- | --- | --- |
| `InteractionRequest` | Pending prompt from core to a frontend | Schema only |
| `InteractionResponse` | Answer or terminal disposition | Schema only |
| `InteractionCancellation` | Cancellation record | Schema only |
| `InteractionTimeout` | Timeout record | Schema only |
| `InteractionRejection` | Rejection record | Schema only |

New requests must be `pending`; expiry must follow creation. Responses cannot
remain pending, and non-answered responses cannot carry `value` or `choice`.
`InteractionResponse.value` is excluded from `repr` and classified sensitive.
This protects accidental representation only; callers must still prevent
logging, persistence, and event-history exposure.

## Transfer, SFTP, forwarding, and plugin models

| Model | Purpose | Runtime support |
| --- | --- | --- |
| `TransferSummary` | Progress and state for one upload/download | Schema only |
| `SftpEntry` | Remote directory entry | Schema only |
| `ListDirectoryRequest` | Connection/path directory query | Schema only |
| `PortForwardSummary` | Forward endpoint and lifecycle snapshot | Schema only |
| `PluginArgument` | Named plugin argument with sensitivity flag | Schema only |
| `PluginOperationRequest` | Explicit plugin operation input | Schema only |
| `PluginOperationResult` | Explicit plugin operation values | Schema only |

Transfer byte counts and optional totals are non-negative. SFTP names/paths are
non-empty and sizes non-negative. Forward ports are 1–65535. Plugin IDs,
operation names, request IDs, and argument names are non-empty.
`PluginArgument.value` and `PluginOperationResult.values` are excluded from
`repr`; plugin result values are classified potentially sensitive because
result semantics are plugin-defined.

## Public enums

Enum values are serialized as the exact lowercase strings below. Unknown-enum
handling is not defined until a transport codec exists.

| Enum | Values | Runtime support |
| --- | --- | --- |
| `Capability` | Connection, terminal, interaction, transfer, plugin, and secret feature groups | `connections.read`, `connections.events`, and `connections.write` advertised |
| `EventType` | `connection.created`, `connection.updated`, `connection.deleted`, `session.created`, `session.state_changed`, `session.output`, `session.interaction_requested`, `session.exited`, `session.closed`, `error.occurred` | First three emitted |
| `ErrorCode` | `unsupported_capability`, `invalid_request`, `validation_failed`, `connection_not_found`, `session_not_found`, `interaction_not_found`, `interaction_already_answered`, `permission_denied`, `operation_cancelled`, `operation_timed_out`, `internal_error` | See [errors](errors.md) |
| `ConnectionHealth` | `unknown`, `checking`, `reachable`, `unreachable` | DTO runtime always reports `unknown` |
| `AuthenticationMethod` | `key`, `password` | Implemented safe projection |
| `SessionState` | `creating`, `connecting`, `waiting_for_interaction`, `connected`, `reconnecting`, `disconnected`, `failed`, `closing`, `closed` | Schema only |
| `InteractionKind` | `password`, `key_passphrase`, `host_key_confirmation`, `keyboard_interactive`, `overwrite_confirmation`, `plugin_question` | Schema only |
| `InteractionStatus` | `pending`, `answered`, `cancelled`, `timed_out`, `rejected` | Schema only |
| `TransferDirection` | `upload`, `download` | Schema only |
| `TransferState` | `queued`, `running`, `paused`, `completed`, `failed`, `cancelled` | Schema only |
| `FileEntryKind` | `file`, `directory`, `symlink`, `other` | Schema only |
| `ForwardKind` | `local`, `remote`, `dynamic` | Schema only |
| `ForwardState` | `starting`, `active`, `failed`, `stopping`, `stopped` | Schema only |

## Representation examples

Every model has a synthetic, deterministic example in the
[generated model index](generated/model-index.md). Those examples are assembled
from type metadata and safe placeholders; they do not instantiate live
connections, consult persistence, or read secret providers. Sensitive values
are shown only as `<sensitive value omitted>`.

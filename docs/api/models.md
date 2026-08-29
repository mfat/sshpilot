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
| `ConnectionId` | Saved connection identity | SSH Host alias (opaque string) | Implemented; alias-backed ID |
| `SessionId` | Daemon-lifetime runtime session identity | `session-<n>` | Daemon implemented |
| `RequestId` | Request correlation | `request-<n>` or opaque string | Implemented |
| `InteractionId` | Daemon-lifetime interaction identity | `interaction-<n>` | Daemon implemented |
| `TransferId` | Transfer identity | `transfer-<n>` | Daemon implemented |
| `SftpServiceId` | SFTP service identity | `sftp-<n>` | Daemon implemented |
| `ForwardId` | Forward identity | `forward-<n>` | Daemon implemented |
| `ClientId` | Handshaken frontend identity | `client-<n>` or opaque string | Daemon implemented |
| `AttachmentId` | Logical session attachment identity | `attachment-<n>` | Daemon implemented |
| `DaemonLogLevel` | Strict daemon logging level: `warning`, `info`, or `debug` | Daemon implemented |
| `SetDaemonLogLevelRequest` | Typed daemon logging-level control request | Daemon implemented |

The aliases are `typing.NewType` wrappers over `str`; they add static intent,
not runtime serialization. Consumers must not parse their contents.
`ConnectionId` equals the concrete SSH Host alias and changes only when that
alias is renamed (delete + create semantics). Runtime IDs are unique for one
daemon lifetime; consumers must not infer cross-restart persistence.
`InteractionId` follows the same daemon-lifetime rule and is never derived from
prompt text, session IDs, connection IDs, or timestamps.

## Discovery and event envelopes

| Model | Purpose | Runtime support |
| --- | --- | --- |
| `ClientInfo` | Frontend name/version and optional ID | Implemented through `get_capabilities` |
| `CoreInfo` | Core name/version/implementation | Implemented through `get_capabilities` |
| `CompatibilityResult` | Compatibility decision and safe message | Implemented for daemon Protocol v1 handshake |
| `Capabilities` | Version, endpoint, compatibility, and supported-set result | Implemented |
| `CoreEvent` | Typed payload plus sequence/timestamp/correlation | Implemented for connection and session lifecycle events |

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
| `OpenSessionRequest` | Stable connection reference for daemon session creation | Daemon implemented |
| `InputOwner` | Reserved input-ownership projection | Schema only |
| `SessionCapabilities` | Per-session feature strings | Daemon implemented; PTY sessions report narrow terminal features |
| `SessionExitInfo` | Safe exit code, signal, and reason | Daemon implemented |
| `SessionFailure` | Sanitised stable failure code and message | Daemon implemented |
| `SessionSummary` | Immutable public lifecycle snapshot | Daemon implemented |
| `AttachSessionRequest` | Logical caller attachment request | Daemon implemented |
| `AttachmentInfo` | Server-derived caller attachment | Daemon implemented |
| `AttachSessionResult` | Session plus logical attachment | Daemon implemented |
| `DetachSessionRequest` | Remove the caller's logical attachment | Daemon implemented |
| `CloseSessionRequest` | Request bounded session closure | Daemon implemented |

All present IDs must be non-empty. `created_at` defaults to aware UTC.
`request_input=True` remains a forward-compatible request, but Phase 6 always
returns `input_owner=False`: logical attachment does not imply terminal input.
`attachment_count` is bookkeeping independent of process state.

## Terminal and replay models

| Model | Purpose | Runtime support |
| --- | --- | --- |
| `TerminalDimensions` | PTY rows and columns | Daemon implemented |
| `TerminalInput` | Attachment-scoped input bytes | Daemon implemented over binary frames |
| `BroadcastTerminalInputRequest` | Command for existing interactive sessions | Daemon implemented over `terminal.broadcast_input` |
| `TerminalOutput` | Sequenced session output bytes | Daemon implemented over binary frames |
| `ResizeTerminalRequest` | Attachment-scoped dimension update | Daemon implemented |
| `ReplayRequest` | Retained-byte query after a sequence | Daemon implemented |
| `ReplayBounds` | Retained sequence/byte range | Daemon implemented |
| `ReplayResult` | Replay continuation metadata | Daemon implemented; bytes use binary frames |

Rows and columns must be 1–10,000. Input/output/replay data must be `bytes`.
Sequences are non-negative; replay bounds are ordered; retained bytes are
non-negative. `ReplayRequest.max_bytes` defaults to 1 MiB and is limited to
16 MiB. Byte payloads are sensitive transient content and must not be logged.

## Interaction models

| Model | Purpose | Runtime support |
| --- | --- | --- |
| `HostKeyPrompt` | Safe host/port/key-type/SHA256 fingerprint and trust status | Daemon implemented |
| `PasswordPrompt` | Safe SSH login identity or structured secret-prompt kind/parameters, plus attempt and remember availability | Daemon implemented |
| `PassphrasePrompt` | Safe key display identity, attempt, and remember availability | Daemon implemented |
| `InteractionSummary` | Immutable typed lifecycle snapshot | Daemon implemented |
| `InteractionClaim` | Responder ownership plus one-use nonce | Daemon implemented; nonce excluded from `repr` and events |
| `InteractionDecisionRequest` | Typed host-key or secret-response metadata | Daemon implemented |
| `InteractionRequest` | Pending prompt from core to a frontend | Schema only |
| `InteractionResponse` | Answer or terminal disposition | Schema only |
| `InteractionCancellation` | Cancellation record | Schema only |
| `InteractionTimeout` | Timeout record | Schema only |
| `InteractionRejection` | Rejection record | Schema only |

Runtime interactions use strict `InteractionType`, `InteractionState`,
`HostKeyDecision`, `SecretDecision`, `RememberPolicy`, and `SecretPromptKind`
enums. A structured secret prompt has empty `username`/`hostname`, a known
`SecretPromptKind`, and the exact validated string parameters for that kind;
an ordinary SSH password prompt has no kind or parameters. The decision
DTO contains no secret value. Password/passphrase bytes use the separately
negotiated one-use secret frame after a claim and metadata response reserve an
exact nonce. The retained legacy `InteractionResponse.value` is excluded from
`repr` and remains schema-only.

## Transfer, SFTP, forwarding, and plugin models

| Model | Purpose | Runtime support |
| --- | --- | --- |
| `ServiceFailure` | Sanitised stable failure code and message | Daemon implemented |
| `SftpServiceSummary` | Immutable SFTP service lifecycle snapshot | Daemon implemented |
| `OpenSftpRequest` | Open an SFTP service for a connection | Daemon implemented |
| `AttachSftpRequest` | Attach to an existing SFTP service | Daemon implemented |
| `CloseSftpRequest` | Close an SFTP service | Daemon implemented |
| `ListDirectoryRequest` | Directory listing query | Daemon implemented |
| `ListDirectoryResult` | Typed directory listing page | Daemon implemented |
| `RemoteFileEntry` | Typed remote filesystem entry | Daemon implemented |
| `SftpPathRequest` | Path-scoped SFTP metadata/mutate request | Daemon implemented |
| `SftpRenameRequest` | Rename/move request | Daemon implemented |
| `SftpChmodRequest` | Mode-change request | Daemon implemented |
| `SftpSymlinkRequest` | Symlink creation request | Daemon implemented |
| `TransferSummary` | Progress and state for one upload/download | Daemon implemented |
| `StartTransferRequest` | Start a daemon-path transfer | Daemon implemented |
| `CancelTransferRequest` | Cancel one transfer | Daemon implemented |
| `ForwardSummary` | Runtime forward endpoint and lifecycle snapshot | Daemon implemented |
| `OpenForwardRequest` | Open a local/remote/dynamic forward | Daemon implemented |
| `CloseForwardRequest` | Close one forward | Daemon implemented |
| `SftpEntry` | Legacy remote directory entry | Schema only; prefer `RemoteFileEntry` |
| `PortForwardSummary` | Legacy forward snapshot | Schema only; prefer `ForwardSummary` |
| `PluginArgument` | Named plugin argument with sensitivity flag | Schema only |
| `PluginOperationRequest` | Explicit plugin operation input | Schema only |
| `PluginOperationResult` | Explicit plugin operation values | Schema only |

Transfer byte counts and optional totals are non-negative. SFTP names/paths are
non-empty and sizes non-negative. Forward ports are 0–65535 for bind and
1–65535 for destinations. Plugin IDs, operation names, request IDs, and
argument names are non-empty. `PluginArgument.value` and
`PluginOperationResult.values` are excluded from `repr`; plugin result values
are classified potentially sensitive because result semantics are plugin-defined.

## Public enums

Enum values are serialized as the exact lowercase strings below. Unknown-enum
handling is not defined until a transport codec exists.

| Enum | Values | Runtime support |
| --- | --- | --- |
| `Capability` | Connection, session, terminal, interaction, SFTP, transfer, forward, plugin, and secret feature groups | Daemon advertises narrow implemented connection/session/terminal/interaction/SFTP/transfer/forward capabilities |
| `EventType` | Connection, session, interaction, SFTP, transfer, forward, and local error identifiers | Connection, session, interaction, SFTP, transfer, and forward lifecycle events emitted |
| `ErrorCode` | `unsupported_capability`, `invalid_request`, `validation_failed`, `connection_not_found`, `session_not_found`, `interaction_not_found`, `sftp_service_not_found`, `transfer_not_found`, `forward_not_found`, and related codes | See [errors](errors.md) |
| `ConnectionHealth` | `unknown`, `checking`, `reachable`, `unreachable` | DTO runtime always reports `unknown` |
| `AuthenticationMethod` | `key`, `password` | Implemented safe projection |
| `SessionState` | `created`, `starting`, `running`, `closing`, `exited`, `failed`, `closed` | Daemon implemented |
| `InteractionKind` | `password`, `key_passphrase`, `host_key_confirmation`, `keyboard_interactive`, `overwrite_confirmation`, `plugin_question` | Schema only |
| `InteractionStatus` | `pending`, `answered`, `cancelled`, `timed_out`, `rejected` | Schema only |
| `InteractionType` | `host_key_confirmation`, `password`, `private_key_passphrase` | Daemon implemented |
| `InteractionState` | `pending`, `claimed`, `answered`, `cancelled`, `expired`, `failed` | Daemon implemented |
| `HostKeyStatus` | `unknown`, `changed`, `revoked` | Daemon implemented |
| `HostKeyDecision` | `accept`, `reject` | Daemon implemented |
| `SecretDecision` | `submit`, `cancel` | Daemon implemented |
| `RememberPolicy` | `do_not_store`, `store_after_success`, `replace_stored_after_success`, `delete_stored_secret` | Daemon implemented |
| `SecretMessageCode` | Stable secret lifecycle/status presentation reasons | Daemon implemented; strict codec |
| `SecretTransferMessageCode` | Stable backup/import presentation reasons | Daemon implemented; strict codec |
| `SftpServiceState` | `created`, `starting`, `ready`, `closing`, `closed`, `failed` | Daemon implemented |
| `RemoteFileType` | `regular`, `directory`, `symlink`, `socket`, `fifo`, `block`, `character`, `unknown` | Daemon implemented |
| `TransferDirection` | `upload`, `download` | Daemon implemented |
| `TransferState` | `queued`, `starting`, `running`, `paused`, `cancelling`, `cancelled`, `completed`, `failed` | Daemon implemented |
| `TransferConflictPolicy` | `fail`, `overwrite`, `skip`, `rename` | Daemon implemented |
| `TransferLocalMode` | `daemon_path`, `binary_stream` | Daemon implemented for `daemon_path` only |
| `FileEntryKind` | `file`, `directory`, `symlink`, `other` | Schema only; prefer `RemoteFileType` |
| `ForwardType` / `ForwardKind` | `local`, `remote`, `dynamic` | Daemon implemented |
| `ForwardState` | `created`, `starting`, `active`, `closing`, `closed`, `failed` (legacy `stopping`/`stopped` retained) | Daemon implemented |

## Representation examples

Every model has a synthetic, deterministic example in the
[generated model index](generated/model-index.md). Those examples are assembled
from type metadata and safe placeholders; they do not instantiate live
connections, consult persistence, or read secret providers. Sensitive values
are shown only as `<sensitive value omitted>`.

`SecretUnlockResult`, `SecretOperationResult`, `BitwardenStatus`, and
`RbwStatus` carry a nullable `SecretMessageCode`, the exact validated string
parameters required by that code, and a separate diagnostic. Message codes are
machine contracts; diagnostics from `bw` or another backend are opaque and are
never translated. The frontend selects and translates a local template before
formatting the parameters.

`SecretTransferResult` carries an optional `SecretTransferMessage` and an
ordered tuple of structured warnings. Each message contains a stable
`SecretTransferMessageCode`, the exact validated JSON-safe parameters for that
code, and a separate opaque diagnostic. Backup preview methods return
`SecretTransferPreview`, which applies the same message contract to preview
errors instead of exposing a free-form `error` string. GTK owns translation,
plural selection, parameter formatting, and backup-section display labels.

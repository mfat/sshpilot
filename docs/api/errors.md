# Structured errors

`SshPilotError` is the public failure envelope. Frontends switch on
`error.code`, not human-readable `message`. `to_dict()` returns:

| Field | Meaning |
| --- | --- |
| `code` | Stable string from `ErrorCode` |
| `message` | Safe user/developer-facing explanation; wording is not stable |
| `details` | Safe structured context; never secrets or raw exceptions |
| `retryable` | Operation-specific retry hint |
| `request_id` | Optional request correlation |
| `connection_id` | Optional connection correlation |
| `session_id` | Optional session correlation |

`details` accepts only `None`, strings, booleans, integers, finite floats,
lists, and dictionaries with non-empty string keys. Values are deep-copied.
Exceptions, bytes, callables, arbitrary/custom objects, non-finite numbers,
raw environments, command arguments, and keys conventionally associated with
passwords, passphrases, tokens, credentials, cookies, or private keys are
rejected. `repr(error)` never includes details or correlation identifiers.
Reading `error.details` returns another detached copy, so callers cannot mutate
the validated internal envelope.

## Error inventory

| Code | Runtime status | Retryable | Related operations | Frontend guidance |
| --- | --- | --- | --- | --- |
| `api_version_mismatch` | Implemented | No | Client initialization | Abort initialization and show a compatibility error |
<!-- api-error: api_version_mismatch -->
| `unsupported_capability` | Implemented | No | All unsupported client methods | Re-check capabilities; hide or disable the feature |
| `invalid_request` | Implemented | Usually no | Closed client, wrong owner thread, invalid subscription state | Fix caller lifecycle/threading; do not blindly retry |
| `validation_failed` | Implemented | No until input changes | Connection writes and future operations | Show field-safe validation feedback |
| `connection_already_exists` | Implemented | No until nickname changes | `create_connection`, `update_connection` | Keep the editor open and request another nickname |
| `connection_not_found` | Implemented | No without refreshed ID | Connection get/update/delete | Refresh the snapshot; transitional aliases may expire after rename |
| `persistence_failed` | Implemented | No automatic retry | Connection writes | Keep current UI state and let the user retry explicitly |
| `mutation_ambiguous` | Implemented locally | No automatic retry | Daemon connection writes and session open/close | Refresh the corresponding snapshot before explicit retry |
| `session_not_found` | Implemented | No without refreshed state | Session get/attach/detach/close | Refresh `sessions.list` |
| `session_already_closed` | Implemented | No | Attach to exited/failed/closed session | Refresh session state; do not attach |
| `session_invalid_state` | Reserved public code | No | Session lifecycle | Refresh state; report an implementation defect if repeated |
| `session_startup_failed` | Implemented | No automatic retry | Session process-runner startup | Show safe failure and leave record inspectable |
| `session_termination_failed` | Implemented | Explicit retry only | Bounded session close | Warn safely; daemon retains the exact handle for close/shutdown retry |
| `unsupported_session_protocol` | Implemented | No | Session open | Disable session runtime for that connection protocol |
| `terminal_attachment_required` | Implemented | No until attach | Terminal input, resize, replay | Attach to the session and retry explicitly |
| `terminal_input_owner_required` | Implemented | No until ownership changes | Terminal input and resize | Keep the attachment view-only or reattach after owner release |
| `terminal_input_backpressure` | Implemented | Yes after drain | Terminal input | Pause input and retry without duplicating accepted bytes |
| `terminal_invalid_dimensions` | Implemented | No until corrected | Terminal resize | Clamp rows and columns to the documented range |
| `terminal_unavailable` | Implemented | No for current state | Terminal operations | Show lifecycle state; do not pretend a PTY exists |
| `terminal_replay_unavailable` | Implemented | No for current attachment/session | Terminal replay | Reattach or show retained history is unavailable |
| `terminal_sequence_out_of_range` | Implemented | No until offset changes | Terminal replay | Use the returned replay bounds |
| `terminal_continuity_lost` | Implemented status | Recover through replay | Slow-client overflow | Request retained replay or show truncation |
| `pty_allocation_failed` | Implemented | Explicit retry only | Session startup | Show safe startup failure without OS details |
| `interaction_not_found` | Implemented | No | Interaction list/get/claim/respond/cancel and secret frame | Dismiss stale prompt |
| `interaction_expired` | Implemented | No | Interaction claim/response after deadline | Dismiss the expired dialog |
| `interaction_already_answered` | Implemented | No | Duplicate or late interaction response | Treat the prompt as complete |
| `interaction_claim_conflict` | Implemented | Yes after release/disconnect | Claim by a second eligible client | Keep observing or retry after ownership changes |
| `interaction_responder_unauthorized` | Implemented | No until eligibility/claim changes | Claim, decision, or secret frame from wrong peer | Do not expose the response control |
| `interaction_secret_expected` | Implemented | No until metadata response accepted | Secret frame without a reserved secret slot | Restart the typed response flow |
| `interaction_secret_duplicate` | Implemented | No | Reused nonce or duplicate secret frame | Treat the interaction as already completed |
| `interaction_type_unsupported` | Implemented | No | Unknown or deferred prompt type | Explain that the authentication mechanism is unsupported |
| `prompt_classification_failed` | Implemented | No | Unknown askpass prompt | Fail authentication safely |
| `askpass_helper_unavailable` | Implemented | Explicit retry only | Helper or host-key verification tooling unavailable | Repair the installation before retry |
| `secret_backend_unavailable` | Implemented | Explicit retry or direct entry | Stored-secret lookup | Enter once or unlock/configure the selected backend |
| `secret_storage_failed` | Implemented | Explicit retry only | Remember-after-success commit | Authentication may succeed; report that saving failed |
| `host_key_persistence_failed` | Reserved | No automatic retry | Legacy clients only | No daemon host-key persistence operation emits this code |
| `authentication_attempts_exhausted` | Implemented | No automatic retry | Repeated password/passphrase failure | Start a fresh explicit session attempt |
| `permission_denied` | Implemented | Depends on policy change | Attachment ownership and future protected operations | Explain denied action without exposing policy internals |
| `operation_cancelled` | Schema only | Caller-dependent | Future cancellable operations | Return UI to idle; retry only on explicit user action |
| `operation_timed_out` | Schema only | Operation-dependent | Future bounded operations | Use `retryable`; preserve safe context |
| `internal_error` | Implemented | Read `retryable` | Manager read translation and future adapters | Show generic failure and keep detailed diagnostics in logs |
| `daemon_unavailable` | Implemented locally | Yes | Client connect | Offer daemon startup/retry without displaying socket paths |
| `transport_closed` | Implemented locally | Yes | Any daemon call | Mark endpoint disconnected; reconnect with a new client |
| `transport_timeout` | Implemented locally | Yes | Any daemon call | Offer retry through a new client; the timed-out socket is closed |
| `frame_too_large` | Implemented | No for same payload | Frame decode/encode | Treat peer or payload as incompatible |
| `invalid_frame` | Implemented | No | Frame decode | Disconnect; do not display raw bytes |
| `handshake_required` | Implemented remotely | No | Pre-handshake ordinary method | Fix client protocol order |
| `handshake_already_completed` | Implemented remotely | No | Second handshake | Reuse negotiated state or reconnect |
| `protocol_version_unsupported` | Implemented remotely | No until upgrade | Handshake/version check | Report incompatibility and stop |
| `protocol_error` | Implemented locally/remotely | No | Correlation/envelope/state violation | Disconnect and retain safe diagnostics |
| `unsupported_method` | Implemented remotely | No | Unknown wire method | Update caller; do not infer a capability |
| `daemon_shutting_down` | Implemented remotely | Yes | Request after shutdown begins | Reconnect after daemon restart |
| `server_busy` | Implemented remotely | Yes after snapshot | Deferred session-command admission | Refresh session state, then retry only on explicit user action |

<!-- api-error: unsupported_capability -->
## `unsupported_capability`

The requested feature group is unavailable. Safe details contain only the
stable `capability` string. The daemon client uses this for all declared but
unsupported commands. Secret workflows use `protected_secret_interactions`
when the daemon cannot provide their protected interaction channel.

<!-- api-error: invalid_request -->
## `invalid_request`

The call cannot be accepted in the current client lifecycle or calling context.
Current examples are commands made after close and subscribing after close.

<!-- api-error: validation_failed -->
## `validation_failed`

The submitted public mutation failed model/domain validation. Safe details may
identify public field names and validation codes, never submitted values.

<!-- api-error: connection_already_exists -->
## `connection_already_exists`

A create or rename would collide with an existing saved nickname. Alias
identity does not remove the persistence rule that nicknames are unique. The
error never includes the submitted nickname.

<!-- api-error: connection_not_found -->
## `connection_not_found`

The opaque connection ID does not match a current connection. The error may
include `connection_id`; its message deliberately does not interpolate internal
record data.

<!-- api-error: persistence_failed -->
## `persistence_failed`

The existing connection manager could not commit a create, update, or delete.
It is not automatically retryable. Internal logs may record only the safe
exception type; the public error contains no path, parser output, or payload.

<!-- api-error: mutation_ambiguous -->
## `mutation_ambiguous`

The daemon transport timed out or closed after a mutation frame may have been
sent. The server may already have committed the change. `retryable` is false:
clients must obtain a fresh `connections.list` or `sessions.list` snapshot
before offering an explicit retry. The error carries safe correlation IDs but
never the request payload.

<!-- api-error: session_not_found -->
## `session_not_found`

The strict session ID does not identify a retained daemon session.

<!-- api-error: session_already_closed -->
## `session_already_closed`

A logical attachment was requested after the session stopped accepting
attachments. Refresh state rather than retrying automatically.

<!-- api-error: session_invalid_state -->
## `session_invalid_state`

Reserved stable code for a public lifecycle conflict. Internal invalid
transition attempts are programming errors and are not exposed with raw state.

<!-- api-error: session_startup_failed -->
## `session_startup_failed`

The daemon could not establish an owned runtime resource. The safe failure may
be represented in `SessionSummary.failure`; raw command, environment, process
path, secret backend, and exception text are excluded.

<!-- api-error: session_termination_failed -->
## `session_termination_failed`

The exact owned resource did not terminate within the bounded graceful/kill
policy. It is not safe to retry blindly; inspect a fresh session snapshot. A
later explicit user-directed close retries the same handle, and daemon shutdown
also performs bounded cleanup before closing the process runner.

<!-- api-error: unsupported_session_protocol -->
## `unsupported_session_protocol`

The referenced connection protocol has no daemon session runner. The error may
carry the safe connection ID and never reflects arbitrary executable input.

<!-- api-error: terminal_attachment_required -->
## `terminal_attachment_required`

Terminal output, input, resize, and replay are scoped to an active logical
attachment. The daemon derives client identity from the handshaken peer.

<!-- api-error: terminal_input_owner_required -->
## `terminal_input_owner_required`

The attachment exists but is view-only. The first eligible attachment owns
input until detach, release, or disconnect.

<!-- api-error: terminal_input_owner_exists -->
## `terminal_input_owner_exists`

Another attachment already owns terminal input. Forced takeover is not allowed;
the current owner must release or detach first.

<!-- api-error: terminal_input_backpressure -->
## `terminal_input_backpressure`

The bounded per-session PTY input queue is full. No input contents are included
in errors or logs.

<!-- api-error: terminal_invalid_dimensions -->
## `terminal_invalid_dimensions`

Rows or columns are outside 1–1000. Only safe numeric bounds may be exposed.

<!-- api-error: terminal_unavailable -->
## `terminal_unavailable`

The session has no live PTY or is no longer in a terminal-writable state.

<!-- api-error: terminal_replay_unavailable -->
## `terminal_replay_unavailable`

The session or attachment cannot provide retained output. Replay is never
persistent across daemon restart.

<!-- api-error: terminal_sequence_out_of_range -->
## `terminal_sequence_out_of_range`

The requested absolute byte offset is beyond the current output end. Valid
retained bounds may be returned as safe integer details.

<!-- api-error: terminal_continuity_lost -->
## `terminal_continuity_lost`

A peer-specific terminal queue overflow created an output gap. The control
channel remains usable; recovery uses replay if retained bytes cover the gap.

<!-- api-error: pty_allocation_failed -->
## `pty_allocation_failed`

The daemon could not establish the owned PTY. No device path, descriptor,
command, environment, or raw OS exception crosses the API boundary.

<!-- api-error: interaction_not_found -->
## `interaction_not_found`

The strict interaction ID does not identify visible retained broker state.

<!-- api-error: interaction_expired -->
## `interaction_expired`

The monotonic interaction deadline won before a response. Expiry is final,
wakes the waiting helper, and rejects all later claims, decisions, or secrets.

<!-- api-error: interaction_already_answered -->
## `interaction_already_answered`

The interaction is already answered, cancelled, expired, or failed. Exactly one
terminal outcome is accepted.

<!-- api-error: interaction_claim_conflict -->
## `interaction_claim_conflict`

Another eligible client owns the responder claim. No responder nonce or peer
metadata is disclosed.

<!-- api-error: interaction_responder_unauthorized -->
## `interaction_responder_unauthorized`

The handshaken peer is not eligible for the linked session or does not own the
current claim. A client cannot answer for another peer or session.

<!-- api-error: interaction_secret_expected -->
## `interaction_secret_expected`

The broker has not accepted matching typed response metadata and therefore has
no one-use secret slot. Secret bytes are discarded and never reflected in the
error.

<!-- api-error: interaction_secret_duplicate -->
## `interaction_secret_duplicate`

The one-use response nonce has already been consumed or does not match the
reserved slot. Automatic resend is forbidden because delivery is ambiguous.

<!-- api-error: interaction_type_unsupported -->
## `interaction_type_unsupported`

The prompt type is outside the Phase 8 host-key/password/passphrase vocabulary.
Unrestricted keyboard-interactive and security-key PIN/touch prompts fail
safely.

<!-- api-error: prompt_classification_failed -->
## `prompt_classification_failed`

The bounded, sanitized askpass prompt could not be conservatively classified.
It is never treated as a password by default and raw prompt text is not logged.

<!-- api-error: askpass_helper_unavailable -->
## `askpass_helper_unavailable`

The private helper, required OpenSSH verification tool, or private broker
channel could not be established. No command path or OS exception is exposed.

<!-- api-error: secret_backend_unavailable -->
## `secret_backend_unavailable`

The selected existing backend could not satisfy an automatic lookup or a
secret lifecycle operation. The safe `backend` detail is a stable presentation
parameter; frontends map this error code to local text instead of displaying or
parsing the error message. Locked KDBX/Bitwarden master-password handling
remains separate from SSH interactions.

<!-- api-error: secret_storage_failed -->
## `secret_storage_failed`

Authentication completed but a requested remember-after-success operation did
not commit through the selected backend. The secret and backend exception are
not included in the envelope.

<!-- api-error: host_key_persistence_failed -->
## `host_key_persistence_failed`

Reserved for compatibility with older clients. The daemon does not write
known-hosts files and does not emit this error for host-key prompts; OpenSSH
reports persistence failures through its normal process output and exit status.

<!-- api-error: authentication_attempts_exhausted -->
## `authentication_attempts_exhausted`

The bounded password/passphrase attempt limit was reached. The process is
allowed to fail and a fresh session requires explicit user action.

<!-- api-error: permission_denied -->
## `permission_denied`

An authenticated local peer attempted to detach an attachment owned by another
handshaken client. Protocol v1 still has no remote access.

<!-- api-error: operation_cancelled -->
## `operation_cancelled`

The requested operation was cancelled before completion.

<!-- api-error: operation_not_found -->
## `operation_not_found`

The requested daemon operation ID is unknown or no longer retained.

<!-- api-error: operation_timed_out -->
## `operation_timed_out`

Schema-only code. No current client method defines an API timeout.

Direct SFTP RPC responses use the stable SFTP/remote error codes below for
frontend message selection; their `message` field is not a presentation
contract. `details.server_message`, when present, is an opaque server
diagnostic. The optional boolean `details.server_message_is_specific` marks a
diagnostic that the frontend may append unchanged after its localized generic
message. Neither diagnostic field is a gettext message identifier.

<!-- api-error: sftp_service_not_found -->
## `sftp_service_not_found`

No SFTP service exists for the requested opaque service ID.

<!-- api-error: sftp_service_not_ready -->
## `sftp_service_not_ready`

The SFTP service exists but is not in the `ready` state for the requested
operation.

<!-- api-error: sftp_command_failed -->
## `sftp_command_failed`

A remote SFTP command failed.

<!-- api-error: sftp_protocol_lost -->
## `sftp_protocol_lost`

The SFTP subprocess or channel lost continuity. The service transitions to a
terminal failed/closed state.

<!-- api-error: sftp_protocol_error -->
## `sftp_protocol_error`

The SFTP framing or response violated protocol expectations.

<!-- api-error: remote_path_not_found -->
## `remote_path_not_found`

The requested remote path does not exist.

<!-- api-error: remote_path_exists -->
## `remote_path_exists`

The remote path already exists and the operation refused to overwrite it.

<!-- api-error: remote_permission_denied -->
## `remote_permission_denied`

The remote host denied the filesystem operation.

<!-- api-error: remote_not_directory -->
## `remote_not_directory`

A directory operation was attempted against a non-directory path.

<!-- api-error: remote_is_directory -->
## `remote_is_directory`

A file operation was attempted against a directory path.

<!-- api-error: remote_directory_not_empty -->
## `remote_directory_not_empty`

A directory removal failed because the directory still contains entries.

<!-- api-error: remote_unsupported_operation -->
## `remote_unsupported_operation`

The remote SFTP server rejected the operation as unsupported.

<!-- api-error: file_content_too_large -->
## `file_content_too_large`

A file read or replacement was rejected because the content exceeds the
daemon's bounded size limit.

<!-- api-error: file_revision_conflict -->
## `file_revision_conflict`

A file replacement was rejected because the file changed since it was read;
the optimistic revision check prevents stale-snapshot overwrites.

<!-- api-error: file_replacement_failed -->
## `file_replacement_failed`

An atomic file replacement could not be completed on the remote host or the
daemon-local target.

<!-- api-error: file_backup_failed -->
## `file_backup_failed`

The pre-replacement backup copy could not be created; the replacement was not
applied.

<!-- api-error: remote_command_failed -->
## `remote_command_failed`

A native remote command operation completed unsuccessfully; safe command
status metadata may be returned without exposing secrets.

<!-- api-error: transfer_not_found -->
## `transfer_not_found`

No transfer exists for the requested opaque transfer ID.

<!-- api-error: transfer_conflict -->
## `transfer_conflict`

A transfer conflict policy blocked the start or continuation.

<!-- api-error: transfer_cancelled -->
## `transfer_cancelled`

The transfer ended because cancellation was accepted.

<!-- api-error: transfer_io_failed -->
## `transfer_io_failed`

Local or remote I/O failed during a transfer.

<!-- api-error: transfer_disk_full -->
## `transfer_disk_full`

A transfer failed because local or remote storage was exhausted.

<!-- api-error: forward_not_found -->
## `forward_not_found`

No forward exists for the requested opaque forward ID.

<!-- api-error: forward_bind_failed -->
## `forward_bind_failed`

The forward could not bind its local or remote listen address/port.

<!-- api-error: forward_destination_invalid -->
## `forward_destination_invalid`

The forward destination host/port was missing or invalid for its type.

<!-- api-error: forward_startup_failed -->
## `forward_startup_failed`

Forward startup failed before the forward became active.

<!-- api-error: forward_not_active -->
## `forward_not_active`

The forward exists but is not active for the requested operation.

<!-- api-error: service_owner_required -->
## `service_owner_required`

The handshaken client is not the owner of the SFTP, transfer, or forward
resource for a mutating operation.

<!-- api-error: internal_error -->
## `internal_error`

An implementation failure was translated into a safe frontend envelope. Raw
exceptions and stack traces remain in developer logs. The current connection
list adapter sets `retryable=True` when its manager fails to load connections;
clients must use the instance flag rather than assuming all internal errors are
retryable.

<!-- api-error: daemon_unavailable -->
## `daemon_unavailable`

Local connection setup could not reach the Unix daemon endpoint. It is
retryable, has no request ID, and never exposes the socket path or raw OS error
to the frontend. Experimental GTK composition treats this as authority for one
bounded on-demand launch attempt. Protocol incompatibility, malformed
handshake, unsafe socket state, or a missing expected capability are distinct
local launcher categories and do not trigger a restart. Any daemon-selection
failure remains an unavailable/recovery state; it never selects a local
backend. These composition categories do not change the Protocol v1 error-code
surface.

<!-- api-error: stale_editor -->
## `stale_editor`

The connection was modified since the editor last read it. The caller should
re-read the connection and retry the operation. This is raised when the
`expected_generation` on an update or split request does not match the
connection's current generation counter.

<!-- api-error: transport_closed -->
## `transport_closed`

The persistent socket closed or failed. It is retryable and carries the current
request ID when failure occurred during a request. `DaemonClient` rejects later
calls with the same stable code until replaced. When a live event subscription
exists, the client also attempts one safe local `error.occurred` notification
before closing subscriptions; GTK treats its cached connection view as
unavailable.

<!-- api-error: transport_timeout -->
## `transport_timeout`

A finite daemon request deadline expired. It is retryable and correlated to the
request. The client closes the socket so a late response cannot be reused.

<!-- api-error: frame_too_large -->
## `frame_too_large`

A payload exceeded 1,048,576 bytes. It may originate from local encoding or
remote frame parsing. No payload bytes or size-derived content appear in the
message.

<!-- api-error: invalid_frame -->
## `invalid_frame`

The frame was empty, incomplete, non-UTF-8, invalid JSON, or not a JSON object.
The server sends a safe error when possible and then closes that peer.

<!-- api-error: handshake_required -->
## `handshake_required`

An ordinary wire method arrived before `system.handshake`. It is remote,
non-retryable on the same protocol sequence, and correlated to the request.

<!-- api-error: handshake_already_completed -->
## `handshake_already_completed`

One socket attempted a second handshake. Reconnection creates a new handshake
scope.

<!-- api-error: protocol_version_unsupported -->
## `protocol_version_unsupported`

No offered version matched Protocol `1.0`, or a later request used a version
other than the selected version. Safe details may list daemon-supported
versions, never application environment or client payloads.

<!-- api-error: protocol_error -->
## `protocol_error`

The peer violated envelope type, request/client correlation, request-ID
uniqueness, typed event payload, exact event-sequence continuity, or
negotiated-version rules. A bounded client event queue overflow has the same
continuity consequence. It can originate locally or remotely and is not safe to
retry on the same connection.

<!-- api-error: unsupported_method -->
## `unsupported_method`

The wire method is not in the explicit dispatcher. No client-supplied method
text is reflected in details. This is distinct from `unsupported_capability`,
which describes a known public client operation whose feature is unavailable.

<!-- api-error: daemon_shutting_down -->
## `daemon_shutting_down`

The server had begun shutdown and rejected new work. It is remotely originated,
retryable after restart, and correlated to the rejected request.

<!-- api-error: daemon_active_resources -->
## `daemon_active_resources`

A stop or restart was refused because live resources still require confirmation
or an explicit force. No resource contents are exposed.

<!-- api-error: daemon_confirmation_required -->
## `daemon_confirmation_required`

The caller must pass the returned confirmation value (or force) before a
destructive daemon stop/restart proceeds.

<!-- api-error: daemon_incompatible -->
## `daemon_incompatible`

The connected daemon is incompatible with this client (protocol, API
implementation, or required capability). Restart or upgrade is required.

<!-- api-error: daemon_restart_required -->
## `daemon_restart_required`

The client detected a stale or mismatched daemon and requires an explicit
restart before continuing.

<!-- api-error: key_not_found -->
## `key_not_found`

The requested opaque key ID does not exist in the selected key store scope.
The message never contains filesystem paths, private-key contents, or
passphrases.

<!-- api-error: key_already_exists -->
## `key_already_exists`

A key with the requested name already exists in the daemon-owned key store.
Structured details may carry a safe suggested filename. The message never
contains filesystem paths, private-key contents, or passphrases.

<!-- api-error: key_public_unavailable -->
## `key_public_unavailable`

The requested key's public file is missing, unsafe, unreadable, oversized, or
invalid. The message never contains filesystem paths, private-key contents, or
passphrases.

<!-- api-error: key_generation_failed -->
## `key_generation_failed`

The daemon-owned `ssh-keygen` invocation failed. The message never contains the
full command line, private-key contents, or passphrases.

<!-- api-error: key_verification_failed -->
## `key_verification_failed`

The daemon could not complete protected private-key passphrase verification.
The message never contains the native command, key contents, prompt response,
or passphrase.

<!-- api-error: key_deletion_failed -->
## `key_deletion_failed`

The daemon could not safely delete the requested managed key pair. The
message never contains filesystem paths, private-key contents, or passphrases.

<!-- api-error: server_busy -->
## `server_busy`

The bounded daemon session-command executor already contains its maximum 64
running or queued commands. Submission fails immediately; the selector never
waits for capacity. The error is remotely originated, request-correlated, and
marked retryable, but accepted session mutations are never retried
automatically. If open preparation already created a session record, that
record is visible as `failed` with the same safe code; clients refresh
`sessions.list` before offering an explicit retry. No operation payload,
command, environment, queue contents, or peer filesystem data is exposed.

## Handling example

```python
try:
    details = client.get_connection(connection_id)
except SshPilotError as error:
    if error.code is ErrorCode.CONNECTION_NOT_FOUND:
        refresh_connection_list()
    elif error.retryable:
        offer_retry()
    else:
        show_safe_message(error.message)
```

## Adding error codes

- Never require parsing `message`.
- Never change an existing code's semantic meaning.
- Add a new code for a new semantic condition.
- Keep details limited to frontend-safe identifiers and fields.
- Never include credentials, terminal input, tokens, environments, raw
  commands, stack traces, or internal exception representations.
- Log safe developer diagnostics separately.

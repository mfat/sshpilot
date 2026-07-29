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
| `interaction_not_found` | Schema only | No | Future interaction response | Dismiss stale prompt |
| `interaction_already_answered` | Schema only | No | Future interaction response | Treat the prompt as complete |
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

<!-- api-error: unsupported_capability -->
## `unsupported_capability`

The requested feature group is unavailable. Safe details contain only the
stable `capability` string. `InProcessClient` uses this for all declared but
unsupported commands.

<!-- api-error: invalid_request -->
## `invalid_request`

The call cannot be accepted in the current client lifecycle or calling context.
Current examples are commands made after close, commands made off the
in-process owner thread, and subscribing after close.

<!-- api-error: validation_failed -->
## `validation_failed`

The submitted public mutation failed model/domain validation. Safe details may
identify public field names and validation codes, never submitted values.

<!-- api-error: connection_already_exists -->
## `connection_already_exists`

A create or rename would collide with an existing saved nickname. Stable UUID
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

<!-- api-error: interaction_not_found -->
## `interaction_not_found`

Schema-only code for an unknown or expired interaction identifier.

<!-- api-error: interaction_already_answered -->
## `interaction_already_answered`

Schema-only code for a response race after an interaction became terminal.

<!-- api-error: permission_denied -->
## `permission_denied`

An authenticated local peer attempted to detach an attachment owned by another
handshaken client. Protocol v1 still has no remote access.

<!-- api-error: operation_cancelled -->
## `operation_cancelled`

Schema-only code. No current method accepts a cancellation token.

<!-- api-error: operation_timed_out -->
## `operation_timed_out`

Schema-only code. No current client method defines an API timeout.

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
failure falls back once to in-process mode with a fixed safe notification;
these composition categories do not change the Protocol v1 error-code surface.

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

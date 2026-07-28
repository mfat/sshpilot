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

## Error inventory

| Code | Runtime status | Retryable | Related operations | Frontend guidance |
| --- | --- | --- | --- | --- |
| `unsupported_capability` | Implemented | No | All unsupported client methods | Re-check capabilities; hide or disable the feature |
| `invalid_request` | Implemented | Usually no | Closed client, wrong owner thread, invalid subscription state | Fix caller lifecycle/threading; do not blindly retry |
| `validation_failed` | Schema code; envelope tested | No until input changes | Future writes and operations | Show field-safe validation feedback |
| `connection_not_found` | Implemented | No without refreshed ID | `get_connection`; future connection/session calls | Refresh connection list; handle transitional rename IDs |
| `session_not_found` | Schema only | No without refreshed state | Future session/terminal operations | Remove stale session UI |
| `interaction_not_found` | Schema only | No | Future interaction response | Dismiss stale prompt |
| `interaction_already_answered` | Schema only | No | Future interaction response | Treat the prompt as complete |
| `permission_denied` | Schema only | Depends on policy change | Future transport/plugin/secret operations | Explain denied action without exposing policy internals |
| `operation_cancelled` | Schema only | Caller-dependent | Future cancellable operations | Return UI to idle; retry only on explicit user action |
| `operation_timed_out` | Schema only | Operation-dependent | Future bounded operations | Use `retryable`; preserve safe context |
| `internal_error` | Implemented | Read `retryable` | Manager read translation and future adapters | Show generic failure and keep detailed diagnostics in logs |

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

Defined for structured model/domain validation but not currently emitted by an
implemented client operation. Safe details may identify public field names and
validation codes, never submitted secret values.

<!-- api-error: connection_not_found -->
## `connection_not_found`

The opaque connection ID does not match a current connection. The error may
include `connection_id`; its message deliberately does not interpolate internal
record data.

<!-- api-error: session_not_found -->
## `session_not_found`

Schema-only code for a missing runtime session.

<!-- api-error: interaction_not_found -->
## `interaction_not_found`

Schema-only code for an unknown or expired interaction identifier.

<!-- api-error: interaction_already_answered -->
## `interaction_already_answered`

Schema-only code for a response race after an interaction became terminal.

<!-- api-error: permission_denied -->
## `permission_denied`

Schema-only code for an authenticated caller that lacks permission. Protocol v1
currently has no transport authentication or remote access.

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

# Client methods

`SshPilotClient` is synchronous. Examples below use the repository's actual
calling convention. “Unsupported” means `InProcessClient` implements the method
only to raise the documented `unsupported_capability` error.

## Runtime summary

| Method | Status | Capability |
| --- | --- | --- |
| `get_capabilities` | Implemented | Bootstrap; none |
| `list_connections` | Implemented | `connections.read` |
| `get_connection` | Implemented | `connections.read` |
| `create_connection` | Unsupported | `connections.write` |
| `update_connection` | Unsupported | `connections.write` |
| `delete_connection` | Unsupported | `connections.write` |
| `open_session` | Unsupported | `terminal` |
| `attach_session` | Unsupported | `terminal.attach` |
| `detach_session` | Unsupported | `terminal.attach` |
| `close_session` | Unsupported | `terminal` |
| `send_terminal_input` | Unsupported | `terminal` |
| `resize_terminal` | Unsupported | `terminal` |
| `respond_to_interaction` | Unsupported | `interactions` |
| `subscribe_events` | Implemented | Bootstrap; event availability follows capabilities |
| `close` | Implemented | None |

<!-- api-method: get_capabilities -->
## `get_capabilities`

- **Status / introduced:** Implemented / Protocol v1
- **Purpose:** Discover versions, endpoint identity, compatibility, and
  supported feature groups.
- **Parameters / return:** No parameters; returns `Capabilities`.
- **Errors:** None from the current implementation.
- **Events:** None.
- **Cancellation / ordering:** Immediate, not cancellable; returns the same
  immutable value object for the client's lifetime.
- **Threading:** Synchronous and currently callable from any thread.
- **Side effects / security:** None; contains no secrets. It continues to
  return metadata after `close()`.

```python
capabilities = client.get_capabilities()
if capabilities.supports(Capability.CONNECTIONS_READ):
    connections = client.list_connections()
```

<!-- api-method: list_connections -->
## `list_connections`

- **Status / introduced:** Implemented / Protocol v1
- **Capability / purpose:** `connections.read`; return secret-free summaries.
- **Parameters / return:** No parameters; returns `list[ConnectionSummary]`.
- **Errors:** `invalid_request` if closed or called off the owner thread;
  `internal_error` when the wrapped manager cannot load records.
- **Events:** None directly.
- **Cancellation / ordering:** Not cancellable; preserves manager order.
- **Threading:** Synchronous; must run on the thread that constructed
  `InProcessClient`.
- **Side effects / security:** Reads current manager state. It returns DTOs, not
  persistence or GObject instances, and omits secrets and sensitive paths.

```python
for connection in client.list_connections():
    print(connection.nickname, connection.display_target)
```

<!-- api-method: get_connection -->
## `get_connection`

- **Status / introduced:** Implemented / Protocol v1
- **Capability / purpose:** `connections.read`; retrieve one secret-free detail
  snapshot.
- **Parameters / return:** `connection_id: ConnectionId`; returns
  `ConnectionDetails`.
- **Errors:** `connection_not_found`, `invalid_request`, or `internal_error`.
- **Events:** None directly.
- **Cancellation / ordering:** Not cancellable; one point-in-time result.
- **Threading:** Synchronous on the client's owner thread.
- **Side effects / security:** Reads manager state. The identifier is opaque;
  returned authentication fields are booleans/enums and never secret values.

```python
summary = client.list_connections()[0]
details = client.get_connection(summary.id)
```

<!-- api-method: create_connection -->
## `create_connection`

- **Status / introduced:** Unsupported / Protocol v1 schema
- **Capability / purpose:** `connections.write`; intended to create a saved
  connection from `CreateConnectionRequest`.
- **Parameters / return:** Request model; intended return is
  `ConnectionDetails`.
- **Errors:** Always `unsupported_capability` with
  `details.capability == "connections.write"`.
- **Events:** Intended `connection.created`; none emitted now.
- **Cancellation / ordering / threading:** Immediate unsupported failure; no
  cancellation or owner-thread requirement is currently reached.
- **Side effects / security:** None now. Future implementation must use the
  existing persistence and single SSH/auth path and must not accept secrets in
  this request.

```python
try:
    client.create_connection(
        CreateConnectionRequest(nickname="example", hostname="example.invalid")
    )
except SshPilotError as error:
    assert error.code is ErrorCode.UNSUPPORTED_CAPABILITY
```

<!-- api-method: update_connection -->
## `update_connection`

- **Status / introduced:** Unsupported / Protocol v1 schema
- **Capability / purpose:** `connections.write`; intended partial update.
- **Parameters / return:** `connection_id` and `UpdateConnectionRequest`;
  intended return is `ConnectionDetails`.
- **Errors:** Always `unsupported_capability` for `connections.write`.
- **Events:** Intended `connection.updated`; none emitted by this call now.
- **Cancellation / ordering / threading:** Immediate unsupported failure.
- **Side effects / security:** None. Optional fields do not currently imply
  patch/clear wire semantics because there is no transport.

```python
client.update_connection(
    connection_id,
    UpdateConnectionRequest(username="user"),
)
```

<!-- api-method: delete_connection -->
## `delete_connection`

- **Status / introduced:** Unsupported / Protocol v1 schema
- **Capability / purpose:** `connections.write`; intended saved-connection
  deletion.
- **Parameters / return:** `connection_id`; intended return is
  `DeleteConnectionResult`.
- **Errors:** Always `unsupported_capability` for `connections.write`.
- **Events:** Intended `connection.deleted`; none emitted by this call now.
- **Cancellation / ordering / threading:** Immediate unsupported failure.
- **Side effects / security:** None now. Future deletion must define credential
  cleanup separately and must not silently delete secrets in unrelated stores.

```python
client.delete_connection(connection_id)
```

<!-- api-method: open_session -->
## `open_session`

- **Status / introduced:** Unsupported / Protocol v1 schema
- **Capability / purpose:** `terminal`; intended creation of a core-owned
  terminal session.
- **Parameters / return:** `OpenSessionRequest`; intended return is
  `SessionSummary`.
- **Errors:** Always `unsupported_capability` for `terminal`.
- **Events:** Intended `session.created` and later state events; none now.
- **Cancellation / ordering / threading:** Immediate unsupported failure.
- **Side effects / security:** No SSH process or PTY is created.

```python
client.open_session(OpenSessionRequest(connection_id, client_id))
```

<!-- api-method: attach_session -->
## `attach_session`

- **Status / introduced:** Unsupported / Protocol v1 schema
- **Capability / purpose:** `terminal.attach`; intended attachment to an
  existing runtime session.
- **Parameters / return:** `AttachSessionRequest`; intended return is
  `AttachSessionResult`.
- **Errors:** Always `unsupported_capability` for `terminal.attach`.
- **Events:** None now.
- **Cancellation / ordering / threading:** Immediate unsupported failure.
- **Side effects / security:** No attachment or input ownership is created.

```python
client.attach_session(AttachSessionRequest(session_id, client_id))
```

<!-- api-method: detach_session -->
## `detach_session`

- **Status / introduced:** Unsupported / Protocol v1 schema
- **Capability / purpose:** `terminal.attach`; intended attachment cleanup.
- **Parameters / return:** `DetachSessionRequest`; intended return is `None`.
- **Errors:** Always `unsupported_capability` for `terminal.attach`.
- **Events:** None now.
- **Cancellation / ordering / threading:** Immediate unsupported failure.
- **Side effects / security:** None.

```python
client.detach_session(DetachSessionRequest(session_id, attachment_id))
```

<!-- api-method: close_session -->
## `close_session`

- **Status / introduced:** Unsupported / Protocol v1 schema
- **Capability / purpose:** `terminal`; intended explicit session shutdown.
- **Parameters / return:** `CloseSessionRequest`; intended return is `None`.
- **Errors:** Always `unsupported_capability` for `terminal`.
- **Events:** Intended `session.closed`; none now.
- **Cancellation / ordering / threading:** Immediate unsupported failure.
- **Side effects / security:** No process is signalled.

```python
client.close_session(CloseSessionRequest(session_id))
```

<!-- api-method: send_terminal_input -->
## `send_terminal_input`

- **Status / introduced:** Unsupported / Protocol v1 schema
- **Capability / purpose:** `terminal`; intended byte input from the owning
  attachment.
- **Parameters / return:** `TerminalInput`; intended return is `None`.
- **Errors:** Always `unsupported_capability` for `terminal`.
- **Events:** None.
- **Cancellation / ordering / threading:** Immediate unsupported failure; no
  byte ordering is established.
- **Side effects / security:** No bytes are written. Future implementations
  must never log input or decode it as text.

```python
client.send_terminal_input(TerminalInput(session_id, attachment_id, b"ls\r"))
```

<!-- api-method: resize_terminal -->
## `resize_terminal`

- **Status / introduced:** Unsupported / Protocol v1 schema
- **Capability / purpose:** `terminal`; intended PTY dimension update.
- **Parameters / return:** `ResizeTerminalRequest`; intended return is `None`.
- **Errors:** Always `unsupported_capability` for `terminal`.
- **Events:** None.
- **Cancellation / ordering / threading:** Immediate unsupported failure.
- **Side effects / security:** No PTY is changed.

```python
client.resize_terminal(
    ResizeTerminalRequest(session_id, attachment_id, TerminalDimensions(24, 80))
)
```

<!-- api-method: respond_to_interaction -->
## `respond_to_interaction`

- **Status / introduced:** Unsupported / Protocol v1 schema
- **Capability / purpose:** `interactions`; intended answer/cancel/reject of a
  core-requested interaction.
- **Parameters / return:** `InteractionResponse`; intended return is `None`.
- **Errors:** Always `unsupported_capability` for `interactions`.
- **Events:** None now.
- **Cancellation / ordering / threading:** Immediate unsupported failure.
- **Side effects / security:** No secret is consumed. `value` is excluded from
  `repr`; callers must also avoid logs and event histories.

```python
response = InteractionResponse(
    interaction_id=interaction_id,
    status=InteractionStatus.CANCELLED,
)
client.respond_to_interaction(response)
```

<!-- api-method: subscribe_events -->
## `subscribe_events`

- **Status / introduced:** Implemented / Protocol v1
- **Capability / purpose:** No bootstrap capability; subscribe to events that
  the provider can emit.
- **Parameters / return:** Callable accepting one `CoreEvent`; returns
  `Subscription`.
- **Errors:** `invalid_request` after client close; a non-callable callback
  raises Python `TypeError`.
- **Events:** Current provider emits only the three connection events.
- **Cancellation / ordering:** `unsubscribe()`/`close()` is the cancellation
  mechanism and is idempotent. Callbacks run in registration order.
- **Threading:** Registration is thread-safe. Delivery is synchronous on the
  underlying signal's source thread.
- **Side effects / security:** Retains the callback until unsubscribe or client
  close. Subscriber exceptions are logged and isolated from other subscribers.

```python
subscription = client.subscribe_events(handle_event)
try:
    run_frontend()
finally:
    subscription.close()
```

<!-- api-method: close -->
## `close`

- **Status / introduced:** Implemented / Protocol v1
- **Capability / purpose:** None; release manager signal handlers and event
  subscribers.
- **Parameters / return:** No parameters; returns `None`.
- **Errors / events:** No documented error; emits no event.
- **Cancellation / ordering:** Idempotent. Existing callbacks are removed.
- **Threading:** No owner-thread assertion exists, although production callers
  should close from their composition/GTK owner thread.
- **Side effects / security:** Disconnects registered manager signals. It does
  not close the wrapped manager, saved connections, SSH processes, or secrets.

```python
client.close()
client.close()  # idempotent
```

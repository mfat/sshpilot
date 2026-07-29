# Client methods

`SshPilotClient` is synchronous. Both clients implement connection CRUD.
Phase 6 session lifecycle methods are daemon-only; `InProcessClient` returns
`unsupported_capability` for the corresponding `sessions.*` capability.

## Runtime summary

| Method | Status | Capability |
| --- | --- | --- |
| `get_capabilities` | Implemented | Bootstrap; none |
| `list_connections` | Implemented | `connections.read` |
| `get_connection` | Implemented | `connections.read` |
| `create_connection` | Implemented | `connections.write` |
| `update_connection` | Implemented | `connections.write` |
| `delete_connection` | Implemented | `connections.write` |
| `list_sessions` | Daemon only | `sessions.read` |
| `get_session` | Daemon only | `sessions.read` |
| `open_session` | Daemon only | `sessions.write` |
| `attach_session` | Daemon only | `sessions.write` |
| `detach_session` | Daemon only | `sessions.write` |
| `close_session` | Daemon only | `sessions.write` |
| `send_terminal_input` | Daemon only | `terminal.input` |
| `resize_terminal` | Daemon only | `terminal.resize` |
| `replay_terminal` | Daemon only | `terminal.replay` |
| `claim_terminal_input` | Daemon only | `terminal.input` |
| `release_terminal_input` | Daemon only | `terminal.input` |
| `subscribe_terminal` | Daemon only | `terminal.output` |
| `list_interactions` | Daemon only | `interactions.read` |
| `get_interaction` | Daemon only | `interactions.read` |
| `claim_interaction` | Daemon only | `interactions.respond` |
| `release_interaction` | Daemon only | `interactions.respond` |
| `respond_to_interaction` | Daemon only | `interactions.respond` |
| `cancel_interaction` | Daemon only | `interactions.respond` |
| `send_interaction_secret` | Daemon only | `interactions.respond` |
| `list_sftp_services` | Daemon only | `sftp.read` |
| `get_sftp_service` | Daemon only | `sftp.read` |
| `open_sftp` | Daemon only | `sftp.write` |
| `attach_sftp` | Daemon only | `sftp.write` |
| `detach_sftp` | Daemon only | `sftp.write` |
| `close_sftp` | Daemon only | `sftp.write` |
| `sftp_list_directory` | Daemon only | `sftp.read` |
| `sftp_stat` | Daemon only | `sftp.metadata` |
| `sftp_lstat` | Daemon only | `sftp.metadata` |
| `sftp_realpath` | Daemon only | `sftp.metadata` |
| `sftp_readlink` | Daemon only | `sftp.metadata` |
| `sftp_mkdir` | Daemon only | `sftp.mutate` |
| `sftp_rmdir` | Daemon only | `sftp.mutate` |
| `sftp_remove` | Daemon only | `sftp.mutate` |
| `sftp_rename` | Daemon only | `sftp.mutate` |
| `sftp_chmod` | Daemon only | `sftp.mutate` |
| `sftp_symlink` | Daemon only | `sftp.mutate` |
| `list_transfers` | Daemon only | `transfers.read` |
| `get_transfer` | Daemon only | `transfers.read` |
| `start_transfer` | Daemon only | `transfers.write` |
| `cancel_transfer` | Daemon only | `transfers.write` |
| `list_forwards` | Daemon only | `forwards.read` |
| `get_forward` | Daemon only | `forwards.read` |
| `open_forward` | Daemon only | `forwards.write` |
| `close_forward` | Daemon only | `forwards.write` |
| `subscribe_events` | Implemented | Bootstrap; event availability follows capabilities |
| `close` | Implemented | None |

<!-- api-method-contract: attach_session status=daemon-only capability=sessions.write -->
<!-- api-method-contract: attach_sftp status=daemon-only capability=sftp.write -->
<!-- api-method-contract: cancel_interaction status=daemon-only capability=interactions.respond -->
<!-- api-method-contract: cancel_transfer status=daemon-only capability=transfers.write -->
<!-- api-method-contract: claim_interaction status=daemon-only capability=interactions.respond -->
<!-- api-method-contract: close status=implemented capability=none -->
<!-- api-method-contract: close_forward status=daemon-only capability=forwards.write -->
<!-- api-method-contract: close_session status=daemon-only capability=sessions.write -->
<!-- api-method-contract: close_sftp status=daemon-only capability=sftp.write -->
<!-- api-method-contract: create_connection status=implemented capability=connections.write -->
<!-- api-method-contract: delete_connection status=implemented capability=connections.write -->
<!-- api-method-contract: detach_session status=daemon-only capability=sessions.write -->
<!-- api-method-contract: detach_sftp status=daemon-only capability=sftp.write -->
<!-- api-method-contract: get_capabilities status=implemented capability=none -->
<!-- api-method-contract: get_connection status=implemented capability=connections.read -->
<!-- api-method-contract: get_forward status=daemon-only capability=forwards.read -->
<!-- api-method-contract: get_interaction status=daemon-only capability=interactions.read -->
<!-- api-method-contract: get_session status=daemon-only capability=sessions.read -->
<!-- api-method-contract: get_sftp_service status=daemon-only capability=sftp.read -->
<!-- api-method-contract: get_transfer status=daemon-only capability=transfers.read -->
<!-- api-method-contract: list_connections status=implemented capability=connections.read -->
<!-- api-method-contract: list_forwards status=daemon-only capability=forwards.read -->
<!-- api-method-contract: list_interactions status=daemon-only capability=interactions.read -->
<!-- api-method-contract: list_sessions status=daemon-only capability=sessions.read -->
<!-- api-method-contract: list_sftp_services status=daemon-only capability=sftp.read -->
<!-- api-method-contract: list_transfers status=daemon-only capability=transfers.read -->
<!-- api-method-contract: open_forward status=daemon-only capability=forwards.write -->
<!-- api-method-contract: open_session status=daemon-only capability=sessions.write -->
<!-- api-method-contract: open_sftp status=daemon-only capability=sftp.write -->
<!-- api-method-contract: replay_terminal status=daemon-only capability=terminal.replay -->
<!-- api-method-contract: release_interaction status=daemon-only capability=interactions.respond -->
<!-- api-method-contract: resize_terminal status=daemon-only capability=terminal.resize -->
<!-- api-method-contract: respond_to_interaction status=daemon-only capability=interactions.respond -->
<!-- api-method-contract: send_interaction_secret status=daemon-only capability=interactions.respond -->
<!-- api-method-contract: send_terminal_input status=daemon-only capability=terminal.input -->
<!-- api-method-contract: sftp_chmod status=daemon-only capability=sftp.mutate -->
<!-- api-method-contract: sftp_list_directory status=daemon-only capability=sftp.read -->
<!-- api-method-contract: sftp_lstat status=daemon-only capability=sftp.metadata -->
<!-- api-method-contract: sftp_mkdir status=daemon-only capability=sftp.mutate -->
<!-- api-method-contract: sftp_readlink status=daemon-only capability=sftp.metadata -->
<!-- api-method-contract: sftp_realpath status=daemon-only capability=sftp.metadata -->
<!-- api-method-contract: sftp_remove status=daemon-only capability=sftp.mutate -->
<!-- api-method-contract: sftp_rename status=daemon-only capability=sftp.mutate -->
<!-- api-method-contract: sftp_rmdir status=daemon-only capability=sftp.mutate -->
<!-- api-method-contract: sftp_stat status=daemon-only capability=sftp.metadata -->
<!-- api-method-contract: sftp_symlink status=daemon-only capability=sftp.mutate -->
<!-- api-method-contract: start_transfer status=daemon-only capability=transfers.write -->
<!-- api-method-contract: claim_terminal_input status=daemon-only capability=terminal.input -->
<!-- api-method-contract: release_terminal_input status=daemon-only capability=terminal.input -->
<!-- api-method-contract: subscribe_terminal status=daemon-only capability=terminal.output -->
<!-- api-method-contract: subscribe_events status=implemented capability=connections.events -->
<!-- api-method-contract: update_connection status=implemented capability=connections.write -->

## Daemon wire methods

The dispatcher is an explicit allowlist; it never reflects over Python objects.

| Wire method | Capability | Status |
| --- | --- | --- |
| `system.handshake` | None | Implemented; required exactly once |
| `system.get_capabilities` | None | Implemented after handshake |
| `connections.list` | `connections.read` | Implemented |
| `connections.get` | `connections.read` | Implemented |
| `connections.create` | `connections.write` | Implemented |
| `connections.update` | `connections.write` | Implemented |
| `connections.delete` | `connections.write` | Implemented |
| `interactions.list` | `interactions.read` | Implemented |
| `interactions.get` | `interactions.read` | Implemented |
| `interactions.claim` | `interactions.respond` | Implemented |
| `interactions.release` | `interactions.respond` | Implemented |
| `interactions.respond` | `interactions.respond` | Implemented; metadata only |
| `interactions.cancel` | `interactions.respond` | Implemented |
| `sessions.list` | `sessions.read` | Implemented |
| `sessions.get` | `sessions.read` | Implemented |
| `sessions.open` | `sessions.write` | Implemented |
| `sessions.attach` | `sessions.write` | Implemented |
| `sessions.detach` | `sessions.write` | Implemented |
| `sessions.close` | `sessions.write` | Implemented |
| `terminal.replay` | `terminal.replay` | Implemented |
| `terminal.resize` | `terminal.resize` | Implemented |
| `terminal.claim_input` | `terminal.input` | Implemented |
| `terminal.release_input` | `terminal.input` | Implemented |
| `sftp.list_services` | `sftp.read` | Implemented |
| `sftp.get_service` | `sftp.read` | Implemented |
| `sftp.open` | `sftp.write` | Implemented |
| `sftp.attach` | `sftp.write` | Implemented |
| `sftp.detach` | `sftp.write` | Implemented |
| `sftp.close` | `sftp.write` | Implemented |
| `sftp.list` | `sftp.read` | Implemented |
| `sftp.stat` | `sftp.metadata` | Implemented |
| `sftp.lstat` | `sftp.metadata` | Implemented |
| `sftp.realpath` | `sftp.metadata` | Implemented |
| `sftp.readlink` | `sftp.metadata` | Implemented |
| `sftp.mkdir` | `sftp.mutate` | Implemented |
| `sftp.rmdir` | `sftp.mutate` | Implemented |
| `sftp.rename` | `sftp.mutate` | Implemented |
| `sftp.remove` | `sftp.mutate` | Implemented |
| `sftp.chmod` | `sftp.mutate` | Implemented |
| `sftp.symlink` | `sftp.mutate` | Implemented |
| `transfers.list` | `transfers.read` | Implemented |
| `transfers.get` | `transfers.read` | Implemented |
| `transfers.start` | `transfers.write` | Implemented |
| `transfers.cancel` | `transfers.write` | Implemented |
| `forwards.list` | `forwards.read` | Implemented |
| `forwards.get` | `forwards.read` | Implemented |
| `forwards.open` | `forwards.write` | Implemented |
| `forwards.close` | `forwards.write` | Implemented |

<!-- api-daemon-method: connections.create capability=connections.write -->
<!-- api-daemon-method: connections.delete capability=connections.write -->
<!-- api-daemon-method: connections.get capability=connections.read -->
<!-- api-daemon-method: connections.list capability=connections.read -->
<!-- api-daemon-method: connections.update capability=connections.write -->
<!-- api-daemon-method: forwards.close capability=forwards.write -->
<!-- api-daemon-method: forwards.get capability=forwards.read -->
<!-- api-daemon-method: forwards.list capability=forwards.read -->
<!-- api-daemon-method: forwards.open capability=forwards.write -->
<!-- api-daemon-method: interactions.cancel capability=interactions.respond -->
<!-- api-daemon-method: interactions.claim capability=interactions.respond -->
<!-- api-daemon-method: interactions.get capability=interactions.read -->
<!-- api-daemon-method: interactions.list capability=interactions.read -->
<!-- api-daemon-method: interactions.release capability=interactions.respond -->
<!-- api-daemon-method: interactions.respond capability=interactions.respond -->
<!-- api-daemon-method: sessions.attach capability=sessions.write -->
<!-- api-daemon-method: sessions.close capability=sessions.write -->
<!-- api-daemon-method: sessions.detach capability=sessions.write -->
<!-- api-daemon-method: sessions.get capability=sessions.read -->
<!-- api-daemon-method: sessions.list capability=sessions.read -->
<!-- api-daemon-method: sessions.open capability=sessions.write -->
<!-- api-daemon-method: sftp.attach capability=sftp.write -->
<!-- api-daemon-method: sftp.chmod capability=sftp.mutate -->
<!-- api-daemon-method: sftp.close capability=sftp.write -->
<!-- api-daemon-method: sftp.detach capability=sftp.write -->
<!-- api-daemon-method: sftp.get_service capability=sftp.read -->
<!-- api-daemon-method: sftp.list capability=sftp.read -->
<!-- api-daemon-method: sftp.list_services capability=sftp.read -->
<!-- api-daemon-method: sftp.lstat capability=sftp.metadata -->
<!-- api-daemon-method: sftp.mkdir capability=sftp.mutate -->
<!-- api-daemon-method: sftp.open capability=sftp.write -->
<!-- api-daemon-method: sftp.readlink capability=sftp.metadata -->
<!-- api-daemon-method: sftp.realpath capability=sftp.metadata -->
<!-- api-daemon-method: sftp.remove capability=sftp.mutate -->
<!-- api-daemon-method: sftp.rename capability=sftp.mutate -->
<!-- api-daemon-method: sftp.rmdir capability=sftp.mutate -->
<!-- api-daemon-method: sftp.stat capability=sftp.metadata -->
<!-- api-daemon-method: sftp.symlink capability=sftp.mutate -->
<!-- api-daemon-method: terminal.replay capability=terminal.replay -->
<!-- api-daemon-method: terminal.resize capability=terminal.resize -->
<!-- api-daemon-method: terminal.claim_input capability=terminal.input -->
<!-- api-daemon-method: terminal.release_input capability=terminal.input -->
<!-- api-daemon-method: transfers.cancel capability=transfers.write -->
<!-- api-daemon-method: transfers.get capability=transfers.read -->
<!-- api-daemon-method: transfers.list capability=transfers.read -->
<!-- api-daemon-method: transfers.start capability=transfers.write -->
<!-- api-daemon-method: system.get_capabilities capability=none -->
<!-- api-daemon-method: system.handshake capability=none -->

Unknown wire methods return `unsupported_method`. Terminal output and input use
the negotiated binary frame path; resize and replay metadata use the two
explicit wire methods above. Password/passphrase values use the negotiated
one-use `binary-secret-v1` frame and never an ordinary JSON method.

<!-- api-method: get_capabilities -->
## `get_capabilities`

- **Status / introduced:** Implemented / Protocol v1
- **Purpose:** Discover versions, endpoint identity, compatibility, and
  supported feature groups.
- **Parameters / return:** No parameters; returns `Capabilities`.
- **Errors:** In-process has no expected failure. Daemon construction performs
  handshake and may return documented transport/protocol errors.
- **Events:** None.
- **Cancellation / ordering:** Immediate, not cancellable; returns the same
  immutable value object for the client's lifetime.
- **Threading:** Synchronous and callable from any thread. Daemon requests are
  serialized by one client lock.
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
- **Errors:** `invalid_request` if the in-process client is closed or off its
  owner thread; `internal_error` for mapped manager failures; daemon calls may
  also return documented transport/protocol lifecycle errors.
- **Events:** None directly.
- **Cancellation / ordering:** Not cancellable; preserves manager order. The
  GTK bridge cannot cancel a wire request already in progress, but its request
  token suppresses stale or destroyed-widget delivery.
- **Threading:** `InProcessClient` requires its owner thread. `DaemonClient`
  serializes synchronous requests and uses a finite timeout. Experimental GTK
  daemon mode invokes this method through one application-scoped worker and
  posts presentation updates with `GLib.idle_add`; normal in-process GTK calls
  remain on the owner/main thread.
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
- **Threading:** In-process calls use the owner thread; daemon calls are
  serialized over the persistent socket.
- **Side effects / security:** Reads manager state. The identifier is opaque;
  returned authentication fields are booleans/enums and never secret values.

```python
summary = client.list_connections()[0]
details = client.get_connection(summary.id)
```

<!-- api-method: create_connection -->
## `create_connection`

- **Status / introduced:** Implemented / Protocol v1
- **Capability / purpose:** `connections.write`; create a saved SSH
  connection from `CreateConnectionRequest`.
- **Parameters / return:** Request model; returns
  `ConnectionDetails`.
- **Errors:** `connection_already_exists`, `validation_failed`,
  `persistence_failed`, and daemon transport/protocol errors. A transport
  failure after send becomes `mutation_ambiguous`.
- **Events:** Exactly one `connection.created` after a successful persistence
  change. Response and event may be observed in either order.
- **Cancellation / ordering / threading:** In-process calls use the owner
  thread. Daemon requests are serialized and are never automatically retried.
- **Side effects / security:** Persists only the request's basic metadata
  through `ConnectionManager`. The request has no secret or path fields.

```python
created = client.create_connection(
    CreateConnectionRequest(nickname="example", hostname="example.invalid")
)
```

<!-- api-method: update_connection -->
## `update_connection`

- **Status / introduced:** Implemented / Protocol v1
- **Capability / purpose:** `connections.write`; partial basic-metadata update.
- **Parameters / return:** `connection_id` and `UpdateConnectionRequest`;
  returns `ConnectionDetails`.
- **Errors:** `connection_not_found`, `connection_already_exists`,
  `validation_failed`, `persistence_failed`, and transport/protocol errors.
- **Events:** Exactly one `connection.updated` on success; none on failure.
- **Cancellation / ordering / threading:** No automatic retry after timeout or
  closure. Response/event interleaving is intentionally unordered.
- **Side effects / security:** `None` means unchanged. The adapter preserves
  existing advanced settings internally without exposing them on the wire.
  Renaming and host/user/port changes preserve the stable connection ID; the
  result and event carry that same ID.

```python
client.update_connection(
    connection_id,
    UpdateConnectionRequest(username="user"),
)
```

<!-- api-method: delete_connection -->
## `delete_connection`

- **Status / introduced:** Implemented / Protocol v1
- **Capability / purpose:** `connections.write`; saved-connection
  deletion.
- **Parameters / return:** `DeleteConnectionRequest`; returns
  `DeleteConnectionResult`.
- **Errors:** `connection_not_found`, `persistence_failed`, and
  transport/protocol errors.
- **Events:** Exactly one `connection.deleted` on success; none on failure.
- **Cancellation / ordering / threading:** No automatic retry. An ambiguous
  transport failure requires a fresh snapshot before explicit user action.
- **Side effects / security:** Delegates to the existing manager deletion
  policy; no secret value crosses the request or result.

```python
client.delete_connection(DeleteConnectionRequest(connection_id))
```

<!-- api-method: list_interactions -->
## `list_interactions`

Daemon-only `interactions.read` snapshot of safe interaction metadata visible
to the handshaken client. Secret values and raw OpenSSH prompts are absent.

<!-- api-method: get_interaction -->
## `get_interaction`

Daemon-only lookup by strict `interaction:<uuid>` identifier, scoped to the
requesting client's eligible sessions.

<!-- api-method: claim_interaction -->
## `claim_interaction`

Claims responder ownership and returns a short-lived claim plus one-use nonce.
Claim conflicts are retryable; disconnect releases an unanswered claim.

<!-- api-method: release_interaction -->
## `release_interaction`

Idempotently releases an unanswered claim. A reserved secret response cannot be
released until it is answered or cancelled.

<!-- api-method: respond_to_interaction -->
## `respond_to_interaction`

Submits a typed host-key or secret action. Secret bytes are deliberately absent
and follow separately through `send_interaction_secret`.

<!-- api-method: cancel_interaction -->
## `cancel_interaction`

Cancels one owned pending interaction. Expiry, session closure, process exit,
and daemon shutdown also cancel pending interactions.

<!-- api-method: send_interaction_secret -->
## `send_interaction_secret`

Sends a bounded mutable byte buffer through `binary-secret-v1` after a typed
submit action reserved the slot. The client clears the supplied buffer after
the send attempt; the operation is never retried.

<!-- api-method: list_sftp_services -->
## `list_sftp_services`

Daemon-only `sftp.read` snapshot of open SFTP services visible to the
handshaken client.

<!-- api-method: get_sftp_service -->
## `get_sftp_service`

Daemon-only lookup by strict `sftp:<uuid>` identifier.

<!-- api-method: open_sftp -->
## `open_sftp`

Daemon-only `sftp.write` creation of a daemon-owned SFTP service for one
connection. Returns `SftpServiceSummary`.

<!-- api-method: attach_sftp -->
## `attach_sftp`

Attaches the handshaken client to an existing SFTP service for shared use.

<!-- api-method: detach_sftp -->
## `detach_sftp`

Removes the caller's attachment from an SFTP service without closing it for
other clients.

<!-- api-method: close_sftp -->
## `close_sftp`

Requests bounded closure of an SFTP service. Repeated close is idempotent.

<!-- api-method: sftp_list_directory -->
## `sftp_list_directory`

Lists a remote directory through a ready SFTP service. Returns
`ListDirectoryResult` with typed `RemoteFileEntry` rows.

<!-- api-method: sftp_stat -->
## `sftp_stat`

Follows symlinks and returns a `RemoteFileEntry` for one remote path.

<!-- api-method: sftp_lstat -->
## `sftp_lstat`

Returns metadata for one remote path without following the final symlink.

<!-- api-method: sftp_realpath -->
## `sftp_realpath`

Resolves a remote path to its absolute form and returns the path string.

<!-- api-method: sftp_readlink -->
## `sftp_readlink`

Returns the symlink target string for one remote path.

<!-- api-method: sftp_mkdir -->
## `sftp_mkdir`

Creates a remote directory.

<!-- api-method: sftp_rmdir -->
## `sftp_rmdir`

Removes an empty remote directory.

<!-- api-method: sftp_remove -->
## `sftp_remove`

Removes a remote file or symlink.

<!-- api-method: sftp_rename -->
## `sftp_rename`

Renames or moves a remote path, optionally overwriting when requested.

<!-- api-method: sftp_chmod -->
## `sftp_chmod`

Changes the remote mode bits for one path.

<!-- api-method: sftp_symlink -->
## `sftp_symlink`

Creates a remote symlink from `link_path` to `target_path`.

<!-- api-method: list_transfers -->
## `list_transfers`

Daemon-only `transfers.read` snapshot of transfer records.

<!-- api-method: get_transfer -->
## `get_transfer`

Daemon-only lookup by strict `transfer:<uuid>` identifier.

<!-- api-method: start_transfer -->
## `start_transfer`

Starts a daemon-path upload or download against a ready SFTP service. Direction
also requires `transfers.upload` or `transfers.download`.

<!-- api-method: cancel_transfer -->
## `cancel_transfer`

Requests cancellation of one transfer. Terminal states remain observable via
events and `get_transfer`.

<!-- api-method: list_forwards -->
## `list_forwards`

Daemon-only `forwards.read` snapshot of runtime forwards.

<!-- api-method: get_forward -->
## `get_forward`

Daemon-only lookup by strict `forward:<uuid>` identifier.

<!-- api-method: open_forward -->
## `open_forward`

Opens a local, remote, or dynamic forward. Type also requires
`forwards.local`, `forwards.remote`, or `forwards.dynamic`.

<!-- api-method: close_forward -->
## `close_forward`

Requests bounded closure of one runtime forward.

<!-- api-method: list_sessions -->
## `list_sessions`

- **Status / introduced:** Daemon-only / Protocol v1, API 0.6.
- **Capability / purpose:** `sessions.read`; return one creation-ordered,
  secret-free snapshot including retained closed records.
- **Parameters / return:** None; returns `list[SessionSummary]`.
- **Errors:** `unsupported_capability` from `InProcessClient`; daemon
  transport/protocol lifecycle errors.
- **Threading:** Synchronous; GTK diagnostics submit it through
  `GtkClientBridge`.

<!-- api-method: get_session -->
## `get_session`

- **Status / introduced:** Daemon-only / Protocol v1, API 0.6.
- **Capability / purpose:** `sessions.read`; inspect one daemon-lifetime
  session by strict opaque `SessionId`.
- **Errors:** `session_not_found`, `invalid_request`, and transport errors.
- **Security:** No process handle, command, environment, PTY path, or secret is
  exposed.

<!-- api-method: open_session -->
## `open_session`

- **Status / introduced:** Daemon-only / Protocol v1, API 0.6.
- **Capability / purpose:** `sessions.write`; allocate a daemon-owned session
  record and initiate the configured process runner.
- **Parameters / return:** `OpenSessionRequest(connection_id)`; returns the
  immutable `starting` `SessionSummary` as soon as the startup command is
  accepted by the bounded executor. The response does not wait for PTY
  allocation, OpenSSH launch, host-key/password/passphrase interaction,
  network negotiation, or `running`.
- **Errors:** Missing connection, unsupported protocol, daemon shutdown, or
  transport errors. `server_busy` means the bounded worker admission failed;
  the prepared record is marked `failed` and no misleading `starting` summary
  is returned. A lost response after send becomes non-retryable
  `mutation_ambiguous`; refresh `sessions.list` before user-directed retry.
  Authentication and process-startup deadlines belong to the interaction
  broker and session lifecycle, not the five-second control RPC timeout.
- **Events:** `session.created`, then state changes. Clients may observe
  `starting`→`running` or `starting`→`failed` after the open response.
  Startup failure after acknowledgement is delivered only through session
  state/events, never as a second RPC response for the same open.
- **Threading:** Preparation and admission are bounded on the selector. Runner
  startup executes on the daemon's bounded keyed executor independently of the
  RPC response. A slow or interactive startup does not delay another peer's
  handshake or read requests.
- **Reconciliation follow-up:** A client-generated `client_open_token` for
  idempotent open retry after genuine transport loss is deferred; immediate
  acknowledgement removes the common authentication-timeout ambiguity.
- **Security:** The frontend supplies no argv or environment. Phase 6's
  production runner fails safely until prompt-safe PTY startup exists; it does
  not fake `running`.

```python
session = client.open_session(OpenSessionRequest(connection_id))
assert session.state is SessionState.STARTING
```

<!-- api-method: attach_session -->
## `attach_session`

- **Status / introduced:** Daemon-only / Protocol v1, API 0.6.
- **Capability / purpose:** `sessions.write`; add the handshaken client to the
  session's logical attachment set.
- **Parameters / return:** `AttachSessionRequest(session_id)` returns
  `AttachSessionResult`.
- **Semantics:** Idempotent for one client/session pair. The daemon derives
  client identity; callers cannot attach for another client. `input_owner` is
  always false and no terminal bytes flow in this phase.

<!-- api-method: detach_session -->
## `detach_session`

- **Status / introduced:** Daemon-only / Protocol v1, API 0.6.
- **Capability / purpose:** `sessions.write`; remove the caller's attachment.
- **Semantics:** Repeated detach is safe. A mismatched attachment ID returns
  `permission_denied`. Socket closure detaches that peer automatically and
  never closes the session.

<!-- api-method: close_session -->
## `close_session`

- **Status / introduced:** Daemon-only / Protocol v1, API 0.6.
- **Capability / purpose:** `sessions.write`; request bounded termination of
  the exact owned runtime resource.
- **Semantics:** Repeated close is idempotent. The runtime enters `closing`,
  submits termination on the same session's serial worker lane, escalates only
  for its exact handle, records exit, and emits `session.exited` and
  `session.closed`. The response is completed after that bounded worker step,
  not on acceptance. If both bounded attempts fail, the daemon retains the
  exact handle in `failed`; a later explicit close retries it rather than
  forgetting an owned resource.
- **Errors:** `session_not_found`, `session_termination_failed`, shutdown and
  transport errors. `server_busy` is an immediate retryable admission failure.
  Lost responses are `mutation_ambiguous`; there is no automatic retry.
- **Threading:** Terminate, wait, and kill never run on the selector. A close
  queued behind startup for the same session cannot overtake it; unrelated
  session lanes can progress concurrently.

<!-- api-method: send_terminal_input -->
## `send_terminal_input`

- **Status / introduced:** Daemon-only / Protocol v1, API 0.8.
- **Capability / purpose:** `terminal.input`; enqueue byte input from the
  session's single input-owning attachment.
- **Parameters / return:** `TerminalInput`; returns after the bounded binary
  frame is sent.
- **Errors:** Local capability/lifecycle validation is synchronous. Daemon-side
  attachment, ownership, running-state, and input-backpressure rejection is
  delivered asynchronously to the terminal subscription's safe error callback.
- **Ordering / threading:** Writes are serialized by the client send lock and
  by the daemon's per-session PTY input queue. Partial non-blocking PTY writes
  preserve order.
- **Side effects / security:** Bytes are never decoded or logged.

```python
client.send_terminal_input(TerminalInput(session_id, attachment_id, b"ls\r"))
```

<!-- api-method: claim_terminal_input -->
## `claim_terminal_input`

- **Status / introduced:** Daemon-only / Protocol v1, API 0.9.
- **Capability / purpose:** `terminal.input`; claim input ownership when the
  session currently has no input owner. Forced takeover is rejected.
- **Parameters / return:** `ClaimTerminalInputRequest`; returns `None`.
- **Errors:** `terminal_attachment_required`, `terminal_input_owner_exists`,
  session-state and transport errors.
- **Ordering / threading:** Ownership changes are serialized with attach,
  detach, input, and resize on the session lane.
- **Side effects / security:** Emits an updated session summary. Does not move
  bytes.

```python
client.claim_terminal_input(
    ClaimTerminalInputRequest(session_id=session_id, attachment_id=attachment_id)
)
```

<!-- api-method: release_terminal_input -->
## `release_terminal_input`

- **Status / introduced:** Daemon-only / Protocol v1, API 0.9.
- **Capability / purpose:** `terminal.input`; release input ownership while
  remaining attached as a view-only subscriber.
- **Parameters / return:** `ReleaseTerminalInputRequest`; returns `None`.
- **Errors:** `terminal_attachment_required`, `terminal_input_owner_required`,
  session-state and transport errors.
- **Ordering / threading:** Same session-lane serialization as claim/input.
- **Side effects / security:** Emits an updated session summary. Does not flush
  or echo pending input.

```python
client.release_terminal_input(
    ReleaseTerminalInputRequest(session_id=session_id, attachment_id=attachment_id)
)
```

<!-- api-method: resize_terminal -->
## `resize_terminal`

- **Status / introduced:** Daemon-only / Protocol v1, API 0.8.
- **Capability / purpose:** `terminal.resize`; apply terminal rows and columns
  for the input-owning attachment.
- **Parameters / return:** `ResizeTerminalRequest`; returns `None`.
- **Errors:** Invalid dimensions, missing attachment, input ownership,
  unavailable PTY, and session-state errors.
- **Ordering / threading:** A pre-start resize becomes the latest pending
  initial size. Running sessions use `TIOCSWINSZ`; repeated size is a no-op.
- **Side effects / security:** Dimensions are limited to 1–1000.

```python
client.resize_terminal(
    ResizeTerminalRequest(session_id, attachment_id, TerminalDimensions(24, 80))
)
```

<!-- api-method: replay_terminal -->
## `replay_terminal`

- **Status / introduced:** Daemon-only / Protocol v1, API 0.8.
- **Capability / purpose:** `terminal.replay`; request retained output from an
  absolute per-session byte offset.
- **Parameters / return:** `ReplayRequest` identifies the owned attachment,
  offset, and bounded maximum. `ReplayResult` carries metadata only; raw bytes
  arrive through replay-flagged terminal frames.
- **Errors:** Attachment, replay availability, sequence range, capability, and
  transport errors.
- **Ordering / threading:** The replay snapshot is immutable. Live/replay
  overlap is permitted and deduplicated by byte sequence; silent gaps are not.
- **Side effects / security:** Replay data is raw, bounded, and never logged.

```python
client.replay_terminal(
    ReplayRequest(session_id, attachment_id, after_sequence=42)
)
```

<!-- api-method: subscribe_terminal -->
## `subscribe_terminal`

- **Status / introduced:** Daemon-only / Protocol v1, API 0.8.
- **Capability / purpose:** `terminal.output`; register frontend-neutral raw
  output, continuity-loss, and EOF callbacks for one session.
- **Parameters / return:** Session ID and callbacks; returns an idempotent
  `TerminalSubscription`.
- **Ordering / threading:** One bounded client dispatch thread isolates the
  socket reader from slow subscribers. Output is serialized per session and
  carries absolute byte offsets.
- **Side effects / security:** Callbacks receive immutable byte DTOs; GTK must
  marshal them through its bridge.

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

- **Status / introduced:** Implemented across `InProcessClient` and
  `DaemonClient` / Protocol v1
- **Capability / purpose:** `connections.events` for the implemented
  connection lifecycle stream; subscribe to events that the provider can emit.
- **Parameters / return:** Callable accepting one `CoreEvent`; returns
  `Subscription`.
- **Errors:** `invalid_request` after client close; a non-callable callback
  raises Python `TypeError`.
- **Events:** Both providers emit `connection.created`, `connection.updated`,
  and `connection.deleted`. `DaemonClient` can additionally emit a safe local
  `error.occurred` when transport/event continuity is lost.
- **Cancellation / ordering:** `unsubscribe()`/`close()` is the cancellation
  mechanism and is idempotent. Callbacks run in registration order through a
  publisher-global serial FIFO.
- **Threading:** Registration is thread-safe. In-process publication uses the
  active serial publisher thread. `DaemonClient` callbacks use one dedicated
  serial event dispatcher, never the socket reader, so a slow subscriber cannot
  block response processing. Re-entrant events queue without recursion.
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
- **Capability / purpose:** None; release manager signal handlers/event
  subscribers or daemon socket resources.
- **Parameters / return:** No parameters; returns `None`.
- **Errors / events:** No documented error; emits no event.
- **Cancellation / ordering:** Idempotent. Existing callbacks are removed.
- **Threading:** No owner-thread assertion exists, although production callers
  should close from their composition/GTK owner thread.
- **Side effects / security:** In-process disconnects registered manager
  signals. Daemon shutdown closes only that client's socket. Neither closes
  saved connections, SSH processes, or secrets.

```python
client.close()
client.close()  # idempotent
```

# SSH Pilot API Changelog

All public frontend-neutral API changes are recorded here. Application release
notes remain separate.

## Unreleased

- Direct SFTP RPC failures now use their existing `ErrorCode` values as the
  presentation contract instead of transporting rendered English messages.
  GTK maps those codes to gettext messages and keeps an optional raw SFTP
  server diagnostic separate. The `ErrorData` shape and error-code inventory
  are unchanged, so that direct-error change required no API bump.
- Daemon-only retirement repairs now preserve protected broadcast input
  registration, truthful operation-mode recovery, atomic reconnect publication,
  and cross-process shared-settings transactions. These are implementation
  correctness fixes within the current contract; no downgrade or
  frontend backend fallback is supported.

## API 0.49 (current)

### API 0.49 structured SFTP summary failures

- Bumped `API_IMPLEMENTATION_VERSION` because SFTP lifecycle, recursive
  operation, and upload/download summaries now carry `SftpFailure` instead of
  a rendered English `ServiceFailure`. The strict SFTP wire object contains a
  stable presentation code, the existing machine `ErrorCode`, validated
  parameters, and an optional opaque server diagnostic.
- GTK maps `SftpFailureCode` values to gettext msgids, translates before
  formatting parameters, and appends diagnostics unchanged. SFTP producers do
  not call gettext and no dynamic path or server diagnostic becomes a msgid.
- The generic `ServiceFailure` model and its `{code, message}` wire shape remain
  unchanged for native SCP transfers, identity operations, forwards, and
  broadcast command results. Transfer backend and operation kind select the
  permitted failure model, preventing accidental cross-domain payloads.

## API 0.48

### API 0.48 plugin connection editor data

- Bumped `API_IMPLEMENTATION_VERSION` because `ConnectionDetails` and
  `ConnectionEditorDetails` now carry optional `plugin_data` so non-SSH
  FieldSpec values (serial device, docker container, k8s pod, mosh options,
  …) round-trip through `get_connection` / the connection editor.

## API 0.47

### API 0.47 structured backup/import presentation

- Bumped `API_IMPLEMENTATION_VERSION` because `SecretTransferResult` now
  carries structured `SecretTransferMessage` values for its primary message
  and ordered warnings instead of rendered strings, and backup preview methods
  now return a strict `SecretTransferPreview` rather than an untyped mapping.
- Backup/import producers return stable `SecretTransferMessageCode` values,
  validated JSON-safe parameters, and optional opaque diagnostics. Dynamic
  backup-section identifiers remain stable data and are mapped to localized
  labels only by GTK.
- GTK selects gettext templates, applies plural rules where counts affect the
  sentence, formats parameters after translation, and appends diagnostics
  unchanged. Backup, import, Bitwarden-note, SSH-server-backup, validation, and
  connection-store restore messages share this presentation boundary.

## Historical API entries

### API 0.46 structured secret status presentation

- Bumped `API_IMPLEMENTATION_VERSION` because `SecretUnlockResult`,
  `SecretOperationResult`, `BitwardenStatus`, and `RbwStatus` replace their
  free-text `message` field with required `message_code`,
  `message_parameters`, and `diagnostic` fields. Strict 0.45 decoders reject
  the new shape, while strict 0.46 decoders require it.
- Secret lifecycle producers now return stable `SecretMessageCode` values and
  validated non-secret parameters. Diagnostics returned by external tools such
  as `bw` remain unmodified in the separate `diagnostic` field and are never
  parsed as machine contracts.
- GTK maps only user-visible secret result codes to gettext msgids, translates
  at display time, formats parameters afterward, and appends any external
  diagnostic without translating it. Backend-unavailable `SshPilotError`
  envelopes use `secret_backend_unavailable` plus a structured backend value
  instead of a rendered English sentence.

### API 0.45 structured secret prompt presentation

- Bumped `API_IMPLEMENTATION_VERSION` because password-prompt metadata now
  includes the required `secret_prompt_kind` and `secret_prompt_parameters`
  fields. Strict 0.44 decoders reject those fields, while strict 0.45 decoders
  require them.
- Daemon-owned Bitwarden, KeePass, remembered-password, and backup-passphrase
  prompts now carry a stable `SecretPromptKind` plus validated, non-secret
  display parameters. They no longer use `username` and `hostname` to carry
  rendered English interface text.
- GTK maps each structured prompt kind to gettext msgids, translates at display
  time, and formats the validated parameters afterward. Ordinary SSH password
  prompts retain their existing username/hostname contract and carry a null
  prompt kind with an empty parameter object.

### API 0.44 clearable connection port

- Bumped `API_IMPLEMENTATION_VERSION` because `UpdateConnectionRequest.port`
  now accepts `""` to mean "clear the authored Port so the host inherits
  again". A 0.43 daemon validates the field as an integer in 1-65535 and
  rejects the empty string, so a newer client's clear request is not
  wire-compatible with it.
- This mirrors how an emptied `username` already clears `User`. Without it a
  port could never be un-authored: `None` means preserve for the core identity
  fields, so the editor had no way to express "no Port line", and every saved
  connection carried one — permanently overriding a global `Port`.
- Integer values and `UNSET`/`None` are unchanged; only the empty string is new.

### API 0.43 launch-command introspection

- Bumped `API_IMPLEMENTATION_VERSION` because `connections.get_launch_command`
  is a new method: a 0.42 daemon advertising `terminal.external_launch` does
  not implement it, so capability advertisement alone no longer implies the
  method is present.
- Returns the SSH argv a connection actually runs, as an
  `ExternalTerminalLaunchSpec` (no new model). It resolves with the `normal`
  interaction policy — the one a real in-app session launches with — unlike
  `prepare_external_terminal_launch`, which deliberately uses `none` and so
  adds `BatchMode=yes`/`StrictHostKeyChecking=yes`. That is right for handing a
  session to a terminal emulator but wrong as an answer to "what does this
  connection run", and such a command cannot prompt for a password or an
  unknown host key.
- Carries no secrets: only `SSH_AUTH_SOCK` crosses in the environment, and the
  daemon's private askpass transport is injected later by the interaction
  broker and is deliberately absent from this argv.

### API 0.42 authored-directive evidence

- Bumped `API_IMPLEMENTATION_VERSION` because `ConnectionEditorDetails` now
  carries `authored_directives` on every `connections.get_editor` response. A
  0.41 decoder's strict field-set check rejects unknown fields, so the added
  key is not wire-compatible with it. The field is decoded as optional, so a
  newer client still accepts a payload from a daemon that omits it.
- `authored_directives` lists the lowercased directives the SSH `Host` block
  itself authored. Everything else an editor displays is inherited — OpenSSH
  resolves it from a global block (`Host *`, `Match`) or its own defaults — and
  must not be presented as a value the user set. An empty tuple means "no
  evidence", never "authored nothing".
- This backs a behavior change in the launch path: only authored directives are
  re-emitted as command-line options, so an editor value can no longer be
  silently overridden by an earlier `Host *` block, while an unauthored one
  keeps inheriting. Defaults are never emitted.

### API 0.41 operation-mode file visibility

- Bumped `API_IMPLEMENTATION_VERSION` because `OperationModeResult` gained
  required-shape additive fields (`default_files`, `isolated_files`,
  `app_config_path`, `app_config_exists`) that a 0.40 decoder's strict
  field-set check would reject.
- Added `OperationModeFiles` (`root_config_path`, `known_hosts_path`,
  `imported_fragment_path`, and their `_exists` flags), resolved by the
  daemon for both the default and isolated SSH configuration scopes and
  returned on every `daemon.get_operation_mode`/`daemon.set_operation_mode`
  response, so a frontend can show which real files back each mode instead
  of a generic description.

### API 0.40 daemon-only retirement compatibility boundary

- Bumped `API_IMPLEMENTATION_VERSION` because protected command-input
  transport, session credentials, operation-mode recovery results, and
  unsaved-host request semantics are not wire-compatible with a 0.39 daemon.
- A resident daemon advertising API 0.39 is rejected with a restart/recovery
  result before any incompatible mutation is attempted. The client never
  falls back to plaintext secret transport or a frontend backend.
- Added daemon-owned session-credential replacement and explicit clearing;
  persistent password correction and deletion clear any temporary credential.
- Preserved omitted CLI ports as `None` in unsaved-host requests so OpenSSH
  aliases retain configured `Port` values; explicit `-p 22` remains an
  override, and ephemeral CLI projections do not claim durable IDs.
- Added `connections.clear_session_password` and documented protected-input
  lifecycle limits and ownership.

### API 0.39 (historical snapshot)

This snapshot is preserved exactly for compatibility testing. It predates the
0.40 protected-input, operation-mode-result, and unsaved-host contract changes;
it is not a current daemon/frontend pairing.

The entries below record superseded implementation stages. They are not
current production instructions; the current daemon-only contract is above.

### API 0.38

- Added the daemon-owned `daemon.get_operation_mode` status operation used by
  restore-safety UI.

### API 0.37

- Added the daemon-only client method contract for operation-mode transitions.

### API 0.36

- Bumped `API_IMPLEMENTATION_VERSION` to `0.36`; `PROTOCOL_VERSION` stays
  `1.0`. Added daemon-owned `daemon.set_operation_mode`, with semantic
  Default/Isolated requests, secure target preparation, live resource
  preconditions, rollback, confirmed mode and generation in the result.

- Bumped `API_IMPLEMENTATION_VERSION` to `0.35`; `PROTOCOL_VERSION` stays
  `1.0`. Added daemon-owned `connections.check_unsaved_host`, which compares
  semantic destination facts against the authoritative connection snapshot
  without exposing SSH config paths or running OpenSSH in the frontend.

- Bumped `API_IMPLEMENTATION_VERSION` to `0.34`; `PROTOCOL_VERSION` stays
  `1.0`. Added daemon-owned `connections.get_effective_config`, returning a
  generation-tagged authored-versus-effective OpenSSH comparison. Frontends no
  longer run `ssh -G` or select the active config root for this decision.

- Bumped `API_IMPLEMENTATION_VERSION` to `0.33`; `PROTOCOL_VERSION` stays
  `1.0`. Added the daemon-owned `connections.prepare_external_terminal_launch`
  operation and `ExternalTerminalLaunchSpec`. External terminal launch argv is
  prepared from the daemon's active SSH configuration; only an approved agent
  socket environment addition may cross the API, and secret autofill is
  intentionally unsupported.

- Bumped `API_IMPLEMENTATION_VERSION` to `0.31`; `PROTOCOL_VERSION` stays
  `1.0`. Added optional `force_tty` to open-session requests. When set, the
  daemon forces a remote TTY allocation (`ssh -t`) so interactive remote
  commands such as `docker exec -it <container> sh` receive a PTY on the far
  side instead of failing with "stdin is not a terminal".
- Bumped `API_IMPLEMENTATION_VERSION` to `0.30`; `PROTOCOL_VERSION` stays
  `1.0`. Added optional `remote_command` to open-session requests so daemon
  terminal sessions can run a command inside a connection (for example
  `docker exec -it <container> sh` or `docker logs -f <container>`) instead
  of only a plain interactive shell; advertised as the new
  `sessions.command` capability.
- Bumped `API_IMPLEMENTATION_VERSION` to `0.29`; `PROTOCOL_VERSION` stays
  `1.0`. Added an additive broadcast execution interaction mode so passive
  remote-history autocomplete can use stored authentication without
  publishing user prompts; ordinary broadcast commands remain interactive.
- Bumped `API_IMPLEMENTATION_VERSION` to `0.28`; `PROTOCOL_VERSION` stays
  `1.0`. Completed the Phase 7 plugin ownership migration: remote plugin
  commands, streamed output, namespaced settings, and session reads/writes
  now use daemon-owned API services. Plugin multiplexing remains a deprecated
  compatibility no-op, and protected command input uses the existing binary
  secret-frame boundary.
- Bumped `API_IMPLEMENTATION_VERSION` to `0.27`; `PROTOCOL_VERSION` stays
  `1.0`. Added the daemon-owned, provider-scoped agent-key read
  `identity.provider.keys.get` (client method `list_provider_agent_keys` with
  `ListProviderAgentKeysRequest`). The daemon runs the native `ssh-add -l`
  against the named provider's agent environment (`'auto'` = the system
  ssh-agent inherited by the daemon), so a caller can observe a specific
  provider regardless of which provider is selected.
- Bumped `API_IMPLEMENTATION_VERSION` to `0.26`; `PROTOCOL_VERSION` stays
  `1.0`. Added typed daemon-owned SSH key deletion by opaque `KeyId` and
  `KeyStoreScope` through `Capability.KEYS_WRITE`.
- Bumped `API_IMPLEMENTATION_VERSION` to `0.25`; `PROTOCOL_VERSION` stays
  `1.0`. `GenerateKeyRequest` now carries only an `encrypted` choice and an
  opaque interaction scope, never a passphrase. Added the daemon-owned
  `keys.verify_passphrase` method. Encrypted generation and verification use
  protected interaction secret frames and native askpass. Key-passphrase
  storage now uses the same protected frame boundary, so key passphrases are
  absent from ordinary request JSON, process argv, and process environment.
  `PassphrasePrompt.confirmation_required` lets frontends collect and compare
  a new key passphrase twice while sending it through the protected frame only
  once.
- Bumped `API_IMPLEMENTATION_VERSION` to `0.24`; `PROTOCOL_VERSION` stays `1.0`.
  Added typed `daemon.set_log_level` control for `warning`, `info`, and `debug`.
- Bumped `API_IMPLEMENTATION_VERSION` to `0.23`; `PROTOCOL_VERSION` stays `1.0`.
- Added `terminal.broadcast_input` and `BroadcastTerminalInputRequest` for
  writing a command to the existing daemon-owned interactive sessions. The
  daemon validates session/input ownership and appends the command newline;
  this is separate from `broadcast.*`, which runs one-shot commands against
  saved connection IDs.
- Added typed daemon-owned broadcast execution (`broadcast.start`,
  `broadcast.get`, and `broadcast.cancel`) against saved connection IDs.

Broadcast command execution is daemon-owned one-shot native SSH execution
against saved connection IDs. It does not inject input into existing terminal
sessions and does not use a local shell.

Command Blocks distinguish one-shot execution from explicitly interactive
terminal execution. Commands requiring a PTY, streaming output, or user input
remain terminal-session actions and are rejected by the headless Broadcast
Command action. Saved and ad-hoc custom commands expose the choice explicitly;
interactive insertion continues to honor the `insert_only` preference.

- Bumped `API_IMPLEMENTATION_VERSION` to `0.21`; `PROTOCOL_VERSION` stays `1.0`.
  Closes out the SFTP operation-lifecycle integration:
  - Recursive `sftp.copy`/`sftp.remove` and `sftp.directory_size` now enforce
    the no-follow symlink policy at the tree *root*, not just for entries
    encountered mid-walk: the root is `lstat`-ed (never `stat`-ed), a
    directory-symlink root is rejected for recursive copy/move instead of
    being walked, `directory_size` rejects a non-directory/symlink root, and
    move cleanup now reuses the same lstat-based no-follow walker as
    `sftp.remove` instead of a separate tree-delete helper that could follow a
    symlinked root into its target.
  - `get_operation`/`cancel_operation` and the wire methods `operations.get` /
    `operations.cancel` moved off `identity.read`/`identity.operate` onto new
    generic `operations.read`/`operations.control` capabilities, gated on the
    shared `OperationRuntime` rather than the identity service. An SFTP-only
    daemon with no identity service now correctly advertises the capabilities
    it needs to poll and cancel its own `sftp_directory_size` /
    `sftp_copy_tree` / `sftp_remove_tree` operations.
  - `operations.get`/`operations.cancel` are now owner-gated: a client may
    only inspect or cancel an operation it started (including one recorded
    with no owner at all); any other client gets `service_owner_required`.
  - The GTK file-manager backend (`DaemonSftpManager`) now wires
    `Future.cancel()` on directory-size/recursive-copy-or-move/recursive-remove
    futures through to `operations.cancel` (including the race where Cancel is
    pressed before the start RPC has returned the operation id), and surfaces
    polled operation progress through the existing `progress` signal instead
    of only resolving on the terminal state.

- Bumped `API_IMPLEMENTATION_VERSION` to `0.20`; `PROTOCOL_VERSION` stays `1.0`.
  Recursive remote size, copy, and delete now run through the daemon operation
  lifecycle instead of blocking the per-service SFTP command stream:
  `sftp.directory_size` always starts a `sftp_directory_size` operation and
  `sftp.remove`/`sftp.copy` with `recursive=true` start `sftp_remove_tree` /
  `sftp_copy_tree` operations, each returning an `OperationSummary` immediately.
  The heavy tree walk runs on the shared operation worker with safe progress,
  cooperative cancellation (`operations.cancel`), and the same no-follow
  symlink policy as before. `OperationSummary` gains an optional wire-safe
  `result` payload so a succeeded directory-size operation carries the
  `SftpDirectorySizeResult`; the frontend resolves it from the terminal
  summary. Non-recursive `sftp.remove`/`sftp.copy` keep the plain synchronous
  RPC shape. Recursive copy is now write-gated like recursive removal, and the
  generated API artifacts were regenerated.

- Bumped `API_IMPLEMENTATION_VERSION` to `0.19`; `PROTOCOL_VERSION` stays `1.0`.
  The additive `recursive` field on the existing `sftp.remove` RPC is sent to
  strict daemons only at API `0.19` and newer, so the client now refuses a
  recursive remove against an older API implementation with the canonical
  `api_version_mismatch` restart error before any wire request is sent.

- Added an optional `recursive` field to `SftpPathRequest`. `sftp_remove` with
  `recursive=true` deletes an entire remote directory tree inside the daemon
  (`SftpServiceRuntime`) using lstat-based traversal that never follows
  symlinks; a missing path is idempotent. The field is sent on the wire only
  when true and defaults to `false` when absent, so Protocol v1 strict daemons
  remain compatible. The frontend no longer issues a per-entry remove loop for
  recursive deletion.

- Added the daemon-owned `sftp.directory_size` RPC (`SftpDirectorySizeRequest` /
  `SftpDirectorySizeResult`). The daemon recursively summarises a remote
  directory tree (total bytes plus file/directory counts) with the same
  no-follow symlink policy as recursive transfers, and the GTK file-manager
  backend now issues a single request instead of walking the tree through
  repeated frontend listings. The remote properties dialog reads owner/group
  numerically and fetches mode/uid/gid/mtime through typed daemon metadata
  (`sftp.stat`/`sftp.lstat`) instead of raw frontend SFTP access to
  `/etc/passwd` and `/etc/group`.

- Added daemon-owned recursive directory transfers. `transfers.start` with
  `recursive=true` now walks and copies an entire local/remote directory tree
  inside `TransferRuntime` — per-file atomic temp+rename, cumulative byte
  progress, the per-file conflict policy (`FAIL` / `OVERWRITE` / `SKIP` /
  `RENAME`), and mid-tree cancellation. `DaemonSftpManager.upload_directory` /
  `download_directory` issue a single recursive transfer instead of a
  frontend-owned walk-and-manifest loop, and the frontend no longer carries a
  one-shot `ssh <host> <command>` escape hatch for the SFTP manager.

- Bumped `API_IMPLEMENTATION_VERSION` to `0.18`; `PROTOCOL_VERSION` stays `1.0`.

- Added the semantic `sftp.privileged_file` capability. `sftp_read_file` and
  `sftp_replace_file` accept an `access` (`SftpFileAccess.NORMAL` / `SUDO`)
  field; privileged operations require the new capability and are refused with
  `UNSUPPORTED_CAPABILITY` when the daemon privileged file runner is absent —
  there is no frontend sudo fallback. Sudo file access is daemon-owned:
  passwordless sudo first, stored `sudo_password_spec` secret next, then a
  protected password interaction through the interaction broker. The one-use
  secret is fed directly to the child stdin and never appears in DTOs, events,
  logs, or errors. Privileged replacement is revision-safe under the per-target
  lock and preserves an existing file's owner/mode via `sudo tee`.

- Added the daemon-owned `sftp.create_file` RPC (`SftpCreateFileRequest` /
  `SftpCreateFileResult`). Remote file creation is a daemon-side touch; the
  frontend no longer uploads a temporary file or fakes a transfer. Already-exists,
  permission, and path failures surface as structured errors.

- Removed plaintext connection-password and key-passphrase lookup methods from
  the public client RPC surface. Added metadata-only availability methods and
  explicit reveal methods with separate capabilities. Reveal acknowledgments use
  ordinary JSON, while returned values use a one-use binary secret frame; daemon
  launch preparation and the interaction broker retain daemon-internal lookups.
  Plugin-secret retrieval uses the same binary response path, and the reveal
  capability is advertised only after binary-secret negotiation.

- Added daemon-owned atomic `connections.move` for sidebar drag-and-drop
  placement. It moves one or more connections as a contiguous ordered block,
  supports group/root destinations and optional above/below targets, rejects
  stale snapshot generations, and publishes one refreshed authoritative store
  snapshot. GTK retains gesture, indicator, and preview presentation only.

- Made `groups.place` (sidebar group reorder, reparent, and nest) revision-safe.
  `PlaceGroupRequest` now carries an optional `expected_generation`; the daemon
  repository rejects a stale generation with `STALE_CONNECTION_STATE` before any
  mutation and publishes no changed snapshot. The sidebar captures the
  authoritative projection generation with the drop target and sends it with the
  request; `GroupMutationController` reconciles a stale rejection with exactly
  one refresh, never retries the mutation, and reports the original error. GTK
  retains gesture, indicator, and preview presentation only.

- Bumped `API_IMPLEMENTATION_VERSION` to `0.17`; `PROTOCOL_VERSION` stays `1.0`.
  The additive `expected_generation` wire field on the existing `groups.place`
  RPC must not be sent to strict `0.16` daemons, so clients now treat an old API
  implementation as write-incompatible for group mutations and reject them with
  the canonical restart error before any wire request.

- Added the daemon-only native SCP slice through `start_scp_transfer` and the
  `transfers.scp` capability. SCP uses the shared `TransferRuntime` lifecycle,
  canonical OpenSSH launch/authentication and interaction-broker paths, bounded
  multi-source typed paths, direct native `scp`, and one controlled `-O`
  compatibility retry. GTK retains chooser, portal, browser, progress, and
  cancellation presentation only; it no longer owns SCP subprocesses, VTE
  execution, authentication environments, argv construction, or remote listing.
  SFTP transfer ownership and general remote operations remain separate and
  pending. Native SCP conflict behavior is overwrite-only; non-overwrite
  policies are rejected, and process cancellation/terminal observation are
  daemon-owned.

- Restored the raw SSH config editor with daemon-owned file resolution and
  writing. Added `connections.get_ssh_config_text` and
  `connections.save_ssh_config_text` (`get_ssh_config_text` /
  `save_ssh_config_text` client methods) plus the `SshConfigText` and
  `SaveSshConfigTextRequest` models. The daemon selects the active SSH config
  root (normal or isolated mode), serves exact text with a whole-configuration
  revision, rejects stale saves, and writes through the existing hardened
  atomic writer (one-shot backup, permissions, symlink refusal). A successful
  save reloads connection state synchronously, so the normal connection update
  events fire before the RPC responds; the polling configuration watcher
  continues to handle external edits. `SshConfigText.text` and
  `SaveSshConfigTextRequest.text` are sensitive fields (excluded from model
  reprs and generated safe representations). GTK never resolves or writes the
  file itself.

- Corrected the bounded SFTP file contract: `SftpReadFileResult.content` and
  `SftpReplaceFileRequest.content` are sensitive fields (excluded from model
  reprs and generated safe representations). Replacements serialize the complete
  per-target compare-and-replace sequence (read, revision compare, backup,
  temporary write, atomic replace, publish) so two concurrent replacements with
  the same original revision resolve to exactly one success and one
  `file_revision_conflict`, for both the remote target and the local
  `~/.ssh/authorized_keys` target. Unrelated services and paths are not blocked.

- Completed and reviewed frontend-neutral identity and authorized-key management.
  `IdentityStateService` and `DaemonIdentityService` now own provider state,
  effective identity resolution through `ssh -G`, native `ssh-add` inspection and
  mutation, native `ssh-copy-id` deployment, fixed ordinary-`ssh` authorized-key
  operations, authentication preparation, and shared operation cancellation.
  `identity.read`, `identity.write`, and `identity.operate` are advertised only
  when the production identity service is installed; unsupported clients receive
  canonical errors without a GTK fallback.
- Extended the existing generic SFTP contract with bounded
  `SftpReadFileRequest`/`SftpReadFileResult` and
  `SftpReplaceFileRequest`/`SftpReplaceFileResult`. Reads return daemon-computed
  content revisions; replacements enforce optimistic revision checks, bounded
  content, daemon-selected temporary/backup paths, atomic replacement, backup
  copies, and secure `0700`/`0600` permissions. A constrained daemon-local
  `~/.ssh/authorized_keys` target supports the local editor without exposing
  arbitrary daemon filesystem access.
- Migrated GTK agent discovery, public-key deployment, and the full authorized-key
  editor to typed client calls. GTK retains selection, document editing,
  confirmation, interaction presentation, and progress display; it no longer
  owns SSH/SFTP subprocesses, authentication environments, agent inspection, or
  authorized-key file I/O. Private keys, passphrases, passwords, askpass answers,
  secret records, and full environments remain outside public DTOs/events/logs.

- Completed and reviewed the shared daemon operation infrastructure. The existing
  `OperationRuntime` is the single lifecycle owner for queued/running/terminal
  snapshots, safe progress and typed failures, immutable operation events,
  cooperative cancellation hooks and supervised processes, bounded terminal
  retention, and bounded daemon shutdown. Runtime locking, completion-versus-
  cancellation races, callback reentrancy, event/get consistency, codec parity,
  capability absence, and security leakage are covered by focused tests. The
  existing identity operation producers exercise this runtime, but identity
  state/provider services and identity feature review remain pending and are
  deliberately not closed by this entry.

- Completed the reviewed daemon-owned secret backend phase. `SecretBackendService`
  is authoritative for backend selection, revision-safe configuration and
  selection, lifecycle, and `secrets.*` operations. Preferences, Bitwarden, and
  rbw setup use the typed frontend-neutral API; the daemon runs the existing
  backend implementations rather than rewriting them. Bitwarden password,
  API-key, SSO, 2FA, and authentication-challenge flows are daemon-owned; rbw
  retains its native agent/pinentry lifecycle; KDBX create, unlock, lock, and
  remembered-password support remain daemon-owned through existing
  platform-keyring identities. The daemon also owns `.spbk`, Bitwarden, and
  SSH-server backup export/import and reuses `BackupManager`, `CredentialManager`,
  `backup_archive`, and `backup_backends` with the existing format and merge
  behavior. Protected interactions carry sensitive input; secret values,
  `BW_SESSION`, transformed KDBX keys, and backup manifests do not cross the
  ordinary public API. GTK owns presentation, file selection, and interaction
  presentation only. Explicit backend selection remains exclusive and `auto`
  preserves its existing compatibility behavior. Capabilities are advertised
  only when services are installed; unsupported clients receive canonical
  `unsupported_capability` errors and never fall back to frontend backend code.

- Implemented `get_global_ssh_overrides`, `update_global_ssh_overrides`, and
  `reset_global_ssh_overrides` over the `ssh_overrides.get`, `ssh_overrides.update`,
  and `ssh_overrides.reset` RPCs. The daemon-owned `SshOverridesService` is the
  authoritative source for global SSH overrides: strict validation and
  normalization, deterministic revision tokens, optimistic concurrency control
  on writes, and atomic persistence of the settings file. The daemon advertises
  `ssh_overrides.read` / `ssh_overrides.write` only when that service is
  installed; otherwise clients receive canonical `unsupported_capability`
  errors and never fall back to GTK-owned config handling.
- Published the SSH overrides schema and contract: `GlobalSshOverrides`,
  `UpdateGlobalSshOverridesRequest`, `EDITABLE_FIELDS`, plus the
  `revision_conflict`, `settings_malformed`, and `settings_persistence_failed`
  error codes. `PROTOCOL_VERSION` stays `1.0`.

- Completed the reviewed daemon-backed connection-store phase:
  `connections.snapshot`, group mutation RPCs, and connection metadata/tag
  mutation methods are implemented through `ConnectionRepository` and
  `ConnectionApplicationService`. The published `ConnectionStoreSnapshot`,
  `GroupSummary`, `ConnectionMetadataSummary`, `GroupId`, group mutation
  requests, hardened `UpdateConnectionMetadataRequest`, and
  `connection_store.changed` event are runtime-backed; the daemon publishes
  them according to the documented capability contract. `Host` aliases remain
  connection identity and compatibility lifecycle events are retained.
- Bumped `API_IMPLEMENTATION_VERSION` to `0.16`; `PROTOCOL_VERSION` stays
  `1.0`.

- Implemented `DaemonClient.list_keys`, `DaemonClient.read_public_key`, and
  `DaemonClient.generate_key` over the `keys.list`, `keys.get_public`, and
  `keys.generate` RPCs. Key generation uses a safe mutation-ambiguity
  description (`MUTATION_AMBIGUOUS` when the request may have been sent), and
  `known_hosts.remove` now carries the generic description as well.
- Published the `list_keys`, `read_public_key`, and `generate_key` client
  contract methods plus their models. The contract is daemon-only: local
  clients report the methods as unsupported with canonical
  `unsupported_capability` errors and never fall back to local key I/O.
- Declared `keys.read` and `keys.write` capability enum values and published
  the SSH-key schema: `KeyId`, `KeyStoreScope`, `KeySummary`, `KeyList`,
  `ListKeysRequest`, `ReadPublicKeyRequest`, `PublicKeyResult`,
  `GenerateKeyRequest`, and `GenerateKeyResult`. Added `key_not_found`,
  `key_already_exists`, `key_public_unavailable`, and `key_generation_failed`
  error codes.
- M1 complete: the daemon key service implements `keys.list`, `keys.get_public`,
  and `keys.generate`, and advertises `keys.read` / `keys.write` only when that
  service is installed. `KeyManager` became a GObject adapter over the daemon
  client with no local key I/O.
- Bumped `API_IMPLEMENTATION_VERSION` to `0.15`; `PROTOCOL_VERSION` stays `1.0`.

- Implemented `DaemonClient.list_known_hosts` and
  `DaemonClient.remove_known_host_entries` over the `known_hosts.list` and
  `known_hosts.remove` RPCs. Removal uses an optimistic revision check and
  surfaces `stale_editor` when the file changed since the snapshot.
- Published the `list_known_hosts` and `remove_known_host_entries` client
  contract methods plus their models (`KnownHostsSnapshot`,
  `KnownHostEntrySummary`, `RemoveKnownHostEntriesRequest`,
  `KnownHostsMutationResult`, `KnownHostEntryId`). Both methods are daemon-only;
  local clients keep reporting them as unsupported.
- Declared `known_hosts.read` and `known_hosts.write` capability enum values.
  A daemon advertises them when its known-hosts service is installed; until
  then clients must treat them as unavailable.
- Bumped `API_IMPLEMENTATION_VERSION` to `0.14`; `PROTOCOL_VERSION` stays `1.0`.

- Bumped `API_IMPLEMENTATION_VERSION` to `0.13` for the expanded interaction
  API, including typed generic confirmations.

### GTK daemon migration parity (Added)

- Added daemon-owned connection duplication and saved login-password lookup
  (`connections.duplicate`, `connections.lookup_password`) so GTK callers do
  not bypass the daemon's connection and secret ownership boundaries.
- Added optional `plugin_data` to connection create/update requests, allowing
  plugin connection types to preserve their frontend-neutral custom fields.

### Daemon askpass parity (Added)

- Added typed keyboard-interactive and security-key-presence prompts so daemon
  sessions can route OTP, PIN, PAM, and hardware-presence interactions through
  the same frontend-neutral interaction protocol as passwords and passphrases.
- Daemon-owned SSH children always install the interaction broker so unknown
  host decisions, unstored credentials, MFA/PIN, and hardware presence remain
  reachable; the normal auth resolver still decides stored-secret autofill.

### Phase 11: Daemon lifecycle and management (Added)

- Added `DaemonLifecycleState` and management models (`DaemonStatus`,
  `DaemonDiagnostics`, stop/restart requests/results).
- Added wire methods `daemon.status`, `daemon.diagnostics`, `daemon.stop`,
  and `daemon.restart` with `daemon.status` / `daemon.control` /
  `daemon.events` capabilities.
- Added idle shutdown policy, graceful drain, and runtime askpass cleanup.
- Transfer `QUEUED` now means no worker assigned; `STARTING` means a worker is
  validating; `RUNNING` means bytes are transferring.
- Bumped `API_IMPLEMENTATION_VERSION` to `0.11`.

### Phase 10.1: Production validation and ownership completion (Changed)

- GTK extended-service routing no longer silently falls back to local
  `ssh -s sftp` / SCP / `ssh -N`; explicit legacy settings or in-process mode
  are required (`sshpilot.extended_service_policy`).
- Transfer runtime bounds concurrent workers (default 4) and queue depth
  (default 32); excess `transfers.start` returns `SERVER_BUSY`.
- `transfers.cancel` returns `null` on the wire (matches client codec).
- Forward launch does not use `ClearAllForwardings=yes` (it wiped ad-hoc
  `-L`/`-R`/`-D` on current OpenSSH); isolated config strips static forwards.
- Local/dynamic `ACTIVE` requires a successful local bind probe; remote uses a
  short process-alive window with `ExitOnForwardFailure=yes`.
- Real OpenSSH integration tests cover SFTP, transfers, forwards, and
  lifecycle races against an ephemeral daemon (not the user socket).

### Phase 10: SFTP, Transfers, and Forwards (Added)

- Added daemon-owned SFTP service lifecycle with narrow capabilities
  (`sftp.read`, `sftp.write`, `sftp.events`, `sftp.metadata`, `sftp.mutate`)
  and client methods for open/attach/list/metadata/mutate operations.
- Added daemon-owned transfers with narrow capabilities
  (`transfers.read`, `transfers.write`, `transfers.events`, `transfers.upload`,
  `transfers.download`) for daemon-path upload/download and cancel.
- Added daemon-owned port forwards with narrow capabilities
  (`forwards.read`, `forwards.write`, `forwards.events`, `forwards.local`,
  `forwards.remote`, `forwards.dynamic`) for local/remote/dynamic forwards.
- Added matching CoreEvent types, ErrorCode values, and live DTOs; legacy coarse
  `sftp` and `port_forwarding` capabilities remain schema-only and are never
  advertised.
- `API_IMPLEMENTATION_VERSION` is `0.10`. Protocol remains additive `1.0`.

### Phase 9.3: GUI Transport Stability (Changed)

- PTY autofill uses the canonical `feed_child_data` widget/backend input API.
  Daemon-backed SSH terminals disable PTY autofill; authentication stays on
  interaction dialogs. Local/legacy GTK-owned children keep one-shot sudo /
  residual password fills without logging secrets.
- `DaemonClient` logs structured, payload-free transport timeout diagnostics
  (request id, method, elapsed time, instance id, queue depths, thread
  liveness) and exposes `threads_alive()` / `build_mismatch()` for tests.
- Handshake may include optional `daemon_started_at`, `development_revision`
  (`SSHPILOT_DEV_REVISION`), and `api_implementation_version`. Mismatch is
  surfaced as a safe warning; active sessions are never killed automatically.
- GUI tests isolate `XDG_RUNTIME_DIR` and force
  `SSHPILOT_CLIENT_MODE=in_process` so the suite cannot attach to a developer
  user daemon. Explicit env `in_process` wins over Stage C
  `terminal.daemon_backed_ssh` auto-promotion.
- Daemon session restore lists sessions through `GtkClientBridge` so a blocked
  control RPC cannot stall the GTK main loop behind welcome
  `connections.list`.

### Phase 9.2: Non-Blocking Session Open Acknowledgement (Changed)

- `sessions.open` now returns the accepted `starting` `SessionSummary` as soon
  as the bounded executor admits startup work. It no longer waits for PTY
  allocation, OpenSSH launch, host-key/password/passphrase interaction, or
  `running`.
- Startup failures after acknowledgement are reported only through session
  lifecycle events and `sessions.get`/`sessions.list`, never as a second RPC
  response for the same open.
- Executor admission rejection still returns retryable `server_busy` and marks
  the prepared record `failed` without a misleading `starting` summary.
- GTK `DaemonTerminalSessionController` treats `STARTING` as a successful open,
  attaches immediately, and updates the existing tab from asynchronous
  `failed`/`exited`/`closed` events.
- The normal five-second `DEFAULT_REQUEST_TIMEOUT` is unchanged.
- Follow-up: optional client-generated `client_open_token` for idempotent open
  reconciliation after genuine transport loss.

### Phase 9.1: Strict Terminal Routing (Added)

- Separated SSH terminal route selection (`SshTerminalRoute`) from daemon
  readiness (`DaemonTerminalReadiness`)
- Daemon route failures show clear readiness errors and never silently launch
  local internal SSH
- Secret vault unlock runs only after route resolution (and after daemon
  readiness for the daemon route)
- Preferences wording: “Use legacy local SSH terminals” (explicit mode, not
  automatic failure fallback)

### Phase 9: GTK Terminal Migration (Added)

- Added production GTK daemon SSH terminal path with VTE emulation as default
- Added multi-attachment support allowing multiple GTK tabs per daemon session
- Added exclusive input ownership with `claim_terminal_input` and `release_terminal_input` APIs
- Added session persistence across GTK restarts through detach/reattach mechanism
- Added `DaemonTerminalTabState` for per-tab session state tracking
- Added `TerminalSessionController` interface with `DaemonTerminalSessionController` implementation
- Added session restoration manager with safe metadata persistence (no secrets/output)
- Added live daemon sessions dialog for developer session discovery and reattachment
- Added continuity loss detection with local GTK markers (never sent to daemon)
- Added Stage C rollout: `terminal.daemon_backed_ssh` defaults to `True`
- Added explicit legacy fallback via `terminal.legacy_local_ssh_fallback` setting
- Added daemon terminal close policies: detach (default), terminate, or ask
- Added broadcast command integration limited to input-owning terminals
- Added VTE as unified daemon SSH emulator (PyXtermJS remains for local terminals)

### Phase 9: Behavior Changes

- **Breaking**: `terminal.daemon_backed_ssh` now defaults to `True` (was `False`)
- Changed daemon SSH from experimental to production for SSH terminal sessions
- Changed terminal activation to prefer daemon when capabilities available
- Changed close behavior to detach by default (preserves running sessions)

### Added

- Added daemon-owned typed host-key, password, and private-key-passphrase
  interactions with strict daemon-lifetime IDs, claim ownership, deadlines,
  cancellation, bounded retention, and safe lifecycle events.
- Added capability-gated `binary-secret-v1` one-use responder-bound frames;
  secret values never enter JSON, events, terminal replay, logs, argv, or
  process environment.
- Added a private same-user daemon askpass helper channel, conservative prompt
  classification, bounded attempts, existing selected-backend lookup, and
  remember-after-authentication-success storage.
- Added unknown-host accept/reject handling by routing OpenSSH askpass prompts
  through the typed interaction API. OpenSSH remains responsible for policy.
- Added experimental daemon-mode GTK interaction dialogs on an independent
  bridge lane so authentication UI does not block terminal streaming.
- Added daemon-owned Unix PTYs with exact child/process-group ownership, one
  shared non-blocking PTY I/O owner, bounded input, and final-output draining.
- Added negotiated `binary-terminal-v1` frames, per-session absolute byte
  sequences, bounded 2 MiB replay rings, attach-time replay, and explicit
  slow-peer continuity loss.
- Added truthful `terminal.output`, `terminal.input`, `terminal.resize`, and
  `terminal.replay` capabilities plus daemon terminal subscriptions, input,
  resize, and replay operations.
- Added a development-only VTE feed integration through the bounded GTK bridge;
  the normal terminal launch path remains unchanged.

- Added daemon-owned monitoring for the SSH root, resolved includes, wildcard
  include directories, and JSON-backed connection metadata.
- Added debounced authoritative reload with last-known-good rollback and
  stable-ID semantic diff publication through existing connection events.
- Added single-token stale UUID-marker recognition for rename-safe external
  edits.
- Added `DaemonClient`, the `python -m sshpilot.daemon` development entry point,
  secure per-user Unix-socket lifecycle, strict length-prefixed JSON envelopes,
  Protocol v1 handshake, request correlation, and structured transport errors.

- Added explicit daemon methods `system.handshake`,
  `system.get_capabilities`, `connections.list`, and `connections.get`.
- Added shared connection contracts across `InProcessClient` and
  `DaemonClient`, plus framing, handshake, socket-security, and lifecycle tests.
- Added the experimental `SSHPILOT_CLIENT_MODE=daemon` GTK composition path,
  bounded on-demand daemon launcher, application-scoped GTK worker bridge, and
  safe compatibility-mode fallback.
- Added typed daemon forwarding for `connection.created`,
  `connection.updated`, and `connection.deleted`, with daemon-global sequences,
  bounded per-peer queues, selector-driven partial writes, and explicit
  overflow disconnection.
- Added one persistent `DaemonClient` reader, pending-response correlation,
  bounded event dispatch isolated from socket reads, sequence validation, and
  application-scoped coalesced GTK refreshes.
- Added the truthful `connections.events` capability; experimental GTK daemon
  selection now requires both snapshot reads and live connection events.
- Added Protocol v1 `connections.create`, `connections.update`, and
  `connections.delete`, the truthful `connections.write` capability, strict
  secret-free mutation codecs, and shared write contracts across both clients.
- Added non-retryable `mutation_ambiguous`, `connection_already_exists`, and
  `persistence_failed` errors for deliberate mutation failure handling.
- Added a 4 MiB total per-peer outbound bound covering responses and events.
- Added immutable UUIDv4 identity to every persisted connection, secure
  idempotent upgrade migration, duplicate/malformed identity repair, and
  UUID-based group, metadata, and saved-layout references.
- Added stable `connection:<uuid>` public IDs plus deprecated Protocol v1
  lookup compatibility for the former nickname-derived ID form.
- Added daemon-owned `session:<uuid>` lifecycle records, an explicit
  `created`/`starting`/`running`/`closing`/`exited`/`failed`/`closed` state
  machine, bounded closed-record retention, and logical multi-client
  attachment bookkeeping.
- Added Protocol v1 `sessions.list`, `sessions.get`, `sessions.open`,
  `sessions.attach`, `sessions.detach`, and `sessions.close`, plus truthful
  `sessions.read`, `sessions.write`, and `sessions.events` capabilities.
- Added typed `session.created`, `session.state_changed`, `session.exited`, and
  `session.closed` forwarding on the existing daemon-global event sequence.
- Added a daemon-internal process-runner boundary with exact process ownership,
  one shared reaper, and bounded terminate/kill shutdown; Phase 7 supplies the
  non-interactive PTY runner.
- Added a daemon-scoped four-worker session command executor with a hard
  64-command bound, per-session serialization, stable internal peer tokens,
  selector-owned deferred response completion, and bounded shutdown draining.
- Added retryable `server_busy` for non-blocking session-command admission
  failure.
- Added the schema-only `replay_terminal` client operation and complete
  package-level convenience exports for all documented model types.
- Aligned schema-only `delete_connection` with `DeleteConnectionRequest`.

### Changed

- Daemon connection mutations now share a bounded configuration command lane
  with external reload. Self-write notifications reconcile as semantic no-ops,
  so Protocol v1 methods, capabilities, DTOs, and event names are unchanged.
- Increased `API_IMPLEMENTATION_VERSION` to `0.9`; `PROTOCOL_VERSION` remains
  compatible `1.0`.
- The broker-enabled native SSH launch keeps the canonical builder/auth
  resolver, disables `BatchMode` only when a capable typed responder path
  exists, and keeps strict exact-key verification. Unrestricted
  keyboard-interactive prompts remain unsupported.
- Capability discovery over `DaemonClient` now comes from the negotiated daemon
  response and advertises only contract-tested runtime capabilities.
- Defined publisher-global serial FIFO event delivery, including concurrent,
  re-entrant, unsubscription, and shutdown behaviour.
- Connection DTOs, mutation results, and events now always emit stable
  UUID-backed IDs. Rename and host metadata changes retain identity across
  reload and daemon restart.
- The GTK welcome page now keeps a non-blocking safe fallback visible when a
  structured connection-read error occurs.
- Daemon-backed GTK connection reads now run off the GTK main thread and use
  GLib delivery with refresh/destruction stale-result suppression. In-process
  mode remains the default.
- Daemon event continuity is process-lifetime only. Queue overflow, malformed
  events, sequence gaps, or transport loss close the affected client; no replay
  or automatic reconnect is implied.
- Experimental GTK daemon mode now requires read, event, and write
  capabilities. Basic CRUD runs on the GTK client worker without optimistic
  row changes; unsupported advanced, metadata, and secret edits are rejected
  rather than discarded.
- Renaming through `update_connection` returns and emits the same stable ID.
  Mutation requests are never automatically retried after ambiguous transport
  failure.
- `sessions.open` and `sessions.close` process-runner work no longer executes
  on the daemon selector. Open returns the captured `starting` acceptance
  snapshot as soon as the executor admits startup; later state changes arrive
  as events and never as a second open response. Close responds after bounded
  worker termination. Neither mutation is automatically retried after ambiguous
  transport loss, while logical attach/detach remain idempotent set operations
  on one connection.
- Replaced the pre-runtime schema-only session states with the seven-state
  daemon lifecycle and removed caller-supplied client IDs from open/attach
  requests. This is an API 0.6 Python source change but not a Protocol v1 wire
  break because the former models had no implemented session wire methods.

### Deprecated

- Nickname-derived `connection:v1:<hash>` values are accepted only as current
  lookup aliases during the remaining Protocol v1 compatibility window. They
  are never emitted and are scheduled for removal in Protocol v2.

### Removed

### Fixed

### Security

- UUID migration uses mode-0600 same-directory temporary files, atomic replace,
  one-shot backups, symlink refusal for JSON state, and safe rollback without
  logging raw connection records.
- Restricted daemon endpoints to owned mode-0700 directories and mode-0600
  sockets; stale cleanup verifies type and inode and refuses symlinks or
  non-socket paths.
- The GTK launcher validates endpoint ownership/type/permissions before
  connecting, uses an argv launch with `shell=False`, detaches child output,
  and strips known session-secret environment variables.
- Wire serialization accepts only strict JSON envelopes and explicit public DTO
  codecs with a 1 MiB frame limit; pickle, marshal, arbitrary objects, raw
  exceptions, persistence records, and secret values cannot cross the boundary.
- Excluded terminal output bytes, replay bytes, and plugin operation result
  values from dataclass `repr`; drift tests now enforce this for every field
  classified sensitive.
- Event payloads are now bound to approved public payload types and excluded
  from event `repr`; structured error details accept only validated safe values
  and exclude details from error `repr`.
- Session wire payloads expose only stable IDs, typed state, timestamps, safe
  exit information, sanitised failures, capabilities, and attachment counts;
  command lines, environments, process handles, PTY paths, prompts, and secret
  material remain private or absent.

## Protocol v1 — Initial documented baseline

### Added

- Protocol version `1.0` and API implementation version `0.1`.
- Synchronous `SshPilotClient` protocol.
- `InProcessClient` adapter.
- Capability discovery with stable capability identifiers.
- Implemented `connections.read` operations: connection list and retrieval.
- Secret-free `ConnectionSummary` and `ConnectionDetails` projections.
- Transitional opaque connection IDs derived from protocol and nickname.
- Structured `SshPilotError` envelopes and stable error codes.
- Frontend-neutral `CoreEvent`, subscription, and publisher infrastructure.
- Runtime `connection.created`, `connection.updated`, and
  `connection.deleted` event adaptation from manager signals.
- Schema-only connection-write, session, terminal-byte, replay, interaction,
  transfer, SFTP, port-forward, and plugin models.
- Schema-only session and asynchronous error event identifiers.
- Contract-test foundations and one migrated GTK connection-read path.
- Maintained API reference, compatibility policy, structural catalog,
  documentation drift checks, and public-surface snapshot.

### Security

- Ordinary connection DTOs exclude passwords, passphrases, key/certificate
  paths, provider objects, environments, and internal records.
- Terminal and interaction secret-bearing fields are classified sensitive;
  secret input models suppress values from `repr` where implemented.
- Structured errors exclude raw exceptions and stack traces.

### Not implemented

- Connection writes
- Core-owned runtime sessions, PTYs, terminal input/output, attach, or replay
- Interaction broker
- SFTP, forwarding, plugin, or secret client operations
- Remote access, TCP/WebSocket transport, named pipes, and terminal/session
  event transport

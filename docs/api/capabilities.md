# Capabilities

Broadcast execution advertises `broadcast.read` and `broadcast.write` only when
the daemon has the operation runtime, canonical SSH launch provider, and
protected interaction broker. `broadcast.events` is reserved by the schema but
is not advertised until typed output-event forwarding is implemented. Absence
never enables a frontend fallback.

<!-- api-capability: broadcast.read -->
<!-- api-capability: broadcast.write -->
<!-- api-capability: broadcast.events -->

Capabilities report implemented runtime support. The existence of a method,
event identifier, or schema does not imply support. Clients must check optional
capabilities and handle `unsupported_capability`.

The existing `daemon.control` capability also covers the additive
`daemon.set_log_level` method. It changes the daemon's managed handler levels;
it does not expose daemon log files over the API.

`InProcessClient` advertises exactly the three connection capabilities. The
daemon additionally advertises session lifecycle, narrow terminal/interaction
capabilities, and Phase 10 SFTP/transfer/forward capabilities when the
corresponding runtimes are available and the client negotiated the required
binary frames; `DaemonClient` returns that negotiated response rather than a
hard-coded local assumption.

Stable IDs do not add a capability: `ConnectionId` was already opaque, all
current providers emit the stable form, and clients must not branch on its
syntax.

Experimental GTK daemon composition verifies all three capabilities after the real
handshake and before injecting the client. A snapshot-only older daemon is not
used because GTK would otherwise have no truthful live-refresh guarantee.

<!-- api-runtime-capability: connections.read -->
<!-- api-runtime-capability: connections.events -->
<!-- api-runtime-capability: connections.write -->
<!-- api-runtime-capability: connections.config.read -->
<!-- api-runtime-capability: connections.config.write -->
<!-- api-runtime-capability: connections.secrets.write -->
<!-- api-runtime-capability: connections.metadata.write -->
<!-- api-runtime-capability: connections.groups -->
<!-- api-runtime-capability: connections.split -->
<!-- api-daemon-runtime-capability: connections.read -->
<!-- api-daemon-runtime-capability: connections.events -->
<!-- api-daemon-runtime-capability: connections.write -->
<!-- api-daemon-runtime-capability: connections.config.read -->
<!-- api-daemon-runtime-capability: connections.config.write -->
<!-- api-daemon-runtime-capability: connections.secrets.write -->
<!-- api-daemon-runtime-capability: connections.secrets.status.read -->
<!-- api-daemon-runtime-capability: connections.secrets.reveal -->
<!-- api-daemon-runtime-capability: connections.metadata.write -->
<!-- api-daemon-runtime-capability: connections.groups -->
<!-- api-daemon-runtime-capability: connections.split -->
<!-- api-daemon-runtime-capability: sessions.read -->
<!-- api-daemon-runtime-capability: sessions.write -->
<!-- api-daemon-runtime-capability: sessions.events -->
<!-- api-daemon-runtime-capability: terminal.output -->
<!-- api-daemon-runtime-capability: terminal.input -->
<!-- api-daemon-runtime-capability: terminal.resize -->
<!-- api-daemon-runtime-capability: terminal.replay -->
<!-- api-daemon-runtime-capability: interactions.read -->
<!-- api-daemon-runtime-capability: interactions.respond -->
<!-- api-daemon-runtime-capability: interactions.events -->
<!-- api-daemon-runtime-capability: interactions.host_key -->
<!-- api-daemon-runtime-capability: interactions.password -->
<!-- api-daemon-runtime-capability: interactions.passphrase -->
<!-- api-daemon-runtime-capability: sftp.read -->
<!-- api-daemon-runtime-capability: sftp.write -->
<!-- api-daemon-runtime-capability: sftp.events -->
<!-- api-daemon-runtime-capability: sftp.metadata -->
<!-- api-daemon-runtime-capability: sftp.mutate -->
<!-- api-daemon-runtime-capability: operations.read -->
<!-- api-daemon-runtime-capability: operations.control -->
<!-- api-daemon-runtime-capability: transfers.read -->
<!-- api-daemon-runtime-capability: transfers.write -->
<!-- api-daemon-runtime-capability: transfers.events -->
<!-- api-daemon-runtime-capability: transfers.upload -->
<!-- api-daemon-runtime-capability: transfers.download -->
<!-- api-daemon-runtime-capability: transfers.scp -->
<!-- api-daemon-runtime-capability: forwards.read -->
<!-- api-daemon-runtime-capability: forwards.write -->
<!-- api-daemon-runtime-capability: forwards.events -->
<!-- api-daemon-runtime-capability: forwards.local -->
<!-- api-daemon-runtime-capability: forwards.remote -->
<!-- api-daemon-runtime-capability: forwards.dynamic -->
<!-- api-daemon-runtime-capability: daemon.status -->
<!-- api-daemon-runtime-capability: daemon.control -->
<!-- api-daemon-runtime-capability: daemon.events -->

## Inventory

| Identifier | Meaning | Provider/status | Related methods | Related events | Dependencies | Introduced |
| --- | --- | --- | --- | --- | --- | --- |
| `connections.read` | Read saved connection DTO snapshots | `InProcessClient` and daemon: Implemented | `list_connections`, `get_connection`; wire `connections.list`, `connections.get` | None required | `ConnectionRepository` / `ConnectionApplicationService` on daemon; compatibility adapter for in-process clients | v1 |
| `connections.events` | Subscribe to live connection lifecycle events | `InProcessClient` and daemon: Implemented | `subscribe_events` | `connection.created`, `connection.updated`, `connection.deleted` | Typed event codec and bounded delivery queues | v1 |
| `connections.write` | Create, duplicate, update, and delete saved connections | `InProcessClient` and daemon: Implemented | `create_connection`, `duplicate_connection`, `update_connection`, `delete_connection`; wire `connections.create`, `connections.duplicate`, `connections.update`, `connections.delete` | `connection.created`, `connection.updated`, `connection.deleted` | `ConnectionRepository` / `ConnectionApplicationService` on daemon; compatibility adapter for in-process clients | v1 |
| `sessions.read` | List and inspect daemon-lifetime session records | Daemon: Implemented; in-process unsupported | `list_sessions`, `get_session` | Session lifecycle events | `SessionRuntime` | v1 / API 0.6 |
| `sessions.write` | Open, logically attach/detach, and close sessions | Daemon: Implemented; in-process unsupported | `open_session`, `attach_session`, `detach_session`, `close_session` | Session lifecycle events | `SessionRuntime` and process-runner boundary | v1 / API 0.6 |
| `sessions.events` | Receive daemon session lifecycle events | Daemon: Implemented | `subscribe_events` | `session.created`, `session.state_changed`, `session.exited`, `session.closed` | Existing bounded event multiplexing | v1 / API 0.6 |
| `terminal` | Legacy broad terminal identifier | Deprecated and never advertised | None | None | Replaced by narrow capabilities | v1 |
| `terminal.attach` | Legacy attach identifier | Deprecated and never advertised | None | None | Attachment remains under `sessions.write` | v1 |
| `terminal.output` | Receive raw PTY output | Daemon: Implemented after binary-frame negotiation | `subscribe_terminal`; output-enabled `attach_session` | Dedicated binary frames, not CoreEvents | PTY runner and bounded terminal queues | v1 / API 0.8 |
| `terminal.input` | Send raw bytes to the owned PTY | Daemon: Implemented after binary-frame negotiation | `send_terminal_input` | None | Input-owner attachment and bounded PTY input queue | v1 / API 0.8 |
| `terminal.resize` | Resize the owned PTY | Daemon: Implemented after binary-frame negotiation | `resize_terminal`; wire `terminal.resize` | None | Attached input owner and `TIOCSWINSZ` | v1 / API 0.8 |
| `terminal.replay` | Replay retained terminal bytes | Daemon: Implemented after binary-frame negotiation | `replay_terminal`; wire `terminal.replay` | Dedicated replay binary frames | Bounded per-session replay ring | v1 / API 0.8 |
| `interactions` | Legacy broad interaction identifier | Deprecated and never advertised | None | None | Replaced by narrow interaction capabilities | v1 |
| `interactions.read` | Read safe typed interaction metadata | Daemon implemented after `binary-secret-v2` negotiation | `list_interactions`, `get_interaction` | Interaction lifecycle events | Daemon interaction broker | v1 / API 0.9 |
| `interactions.respond` | Claim and answer eligible interactions | Daemon implemented after `binary-secret-v2` negotiation | claim/release/respond/cancel and one-use secret send | Interaction lifecycle events | Responder-bound nonce and secure Unix socket | v1 / API 0.9 |
| `interactions.events` | Observe safe interaction lifecycle metadata | Daemon implemented | `subscribe_events` | `interaction.created`, `interaction.state_changed` | Bounded daemon event stream | v1 / API 0.9 |
| `interactions.host_key` | Strict unknown-host trust decisions | Daemon implemented | Typed host-key decisions | Interaction lifecycle events | Key scan plus exact session pinning | v1 / API 0.9 |
| `interactions.password` | Typed login-password askpass | Daemon implemented | One-use secret response | Interaction lifecycle events | Daemon askpass helper and selected secret backend | v1 / API 0.9 |
| `interactions.passphrase` | Typed private-key passphrase askpass | Daemon implemented | One-use secret response | Interaction lifecycle events | Daemon askpass helper and selected secret backend | v1 / API 0.9 |
| `sftp` | Legacy broad SFTP identifier | Deprecated and never advertised | None | None | Replaced by narrow `sftp.*` capabilities | v1 |
| `sftp.read` | List SFTP services and remote directories | Daemon: Implemented when SFTP runtime present | `list_sftp_services`, `get_sftp_service`, `sftp_list_directory` | None required | Daemon `SftpRuntime` | v1 / API 0.10 |
| `sftp.write` | Open, attach, detach, and close SFTP services | Daemon: Implemented when SFTP runtime present | `open_sftp`, `attach_sftp`, `detach_sftp`, `close_sftp` | SFTP lifecycle events | Daemon `SftpRuntime` | v1 / API 0.10 |
| `sftp.events` | Observe SFTP service lifecycle | Daemon: Implemented | `subscribe_events` | `sftp.created`, `sftp.state_changed`, `sftp.closed`, `sftp.failed` | Bounded daemon event stream | v1 / API 0.10 |
| `sftp.metadata` | Stat, lstat, realpath, and readlink | Daemon: Implemented when SFTP runtime present | `sftp_stat`, `sftp_lstat`, `sftp_realpath`, `sftp_readlink` | None | Ready SFTP service | v1 / API 0.10 |
| `sftp.mutate` | mkdir, rmdir, remove, rename, chmod, symlink | Daemon: Implemented when SFTP runtime present | `sftp_mkdir`, `sftp_rmdir`, `sftp_remove`, `sftp_rename`, `sftp_chmod`, `sftp_symlink` | None | Ready SFTP service | v1 / API 0.10 |
| `sftp.privileged_file` | Read and replace remote files with elevated (sudo) access | Daemon: Implemented when the privileged file runner is wired | `sftp_read_file`, `sftp_replace_file` with `access=Sudo` | Interaction lifecycle events for the protected sudo-password prompt | Daemon `PrivilegedFileService` over canonical SSH launch | v1 / API 0.18 |
| `transfers.read` | List and inspect transfer records | Daemon: Implemented when transfer runtime present | `list_transfers`, `get_transfer` | None required | Daemon `TransferRuntime` | v1 / API 0.10 |
| `transfers.write` | Start and cancel transfers | Daemon: Implemented when transfer runtime present | `start_transfer`, `cancel_transfer` | Transfer lifecycle events | Daemon `TransferRuntime` and ready SFTP service | v1 / API 0.10 |
| `transfers.events` | Observe transfer lifecycle and progress | Daemon: Implemented | `subscribe_events` | `transfer.*` lifecycle events | Bounded daemon event stream | v1 / API 0.10 |
| `transfers.upload` | Upload direction for `start_transfer` | Daemon: Implemented when transfer runtime present | `start_transfer` with `upload` | Transfer lifecycle events | Daemon path local mode | v1 / API 0.10 |
| `transfers.download` | Download direction for `start_transfer` | Daemon: Implemented when transfer runtime present | `start_transfer` with `download` | Transfer lifecycle events | Daemon path local mode | v1 / API 0.10 |
| `transfers.scp` | Native OpenSSH SCP upload/download | Daemon: Implemented when native SCP backend is installed | `start_scp_transfer`; wire `transfers.scp.start` | Transfer lifecycle events | Native `scp`, canonical SSH launch, interaction broker | v1 / API 0.13 |
| `port_forwarding` | Legacy broad forward identifier | Deprecated and never advertised | None | None | Replaced by narrow `forwards.*` capabilities | v1 |
| `forwards.read` | List and inspect runtime forwards | Daemon: Implemented when forward runtime present | `list_forwards`, `get_forward` | None required | Daemon `ForwardRuntime` | v1 / API 0.10 |
| `forwards.write` | Open and close runtime forwards | Daemon: Implemented when forward runtime present | `open_forward`, `close_forward` | Forward lifecycle events | Daemon `ForwardRuntime` | v1 / API 0.10 |
| `forwards.events` | Observe forward lifecycle | Daemon: Implemented | `subscribe_events` | `forward.*` lifecycle events | Bounded daemon event stream | v1 / API 0.10 |
| `forwards.local` | Local TCP forwards | Daemon: Implemented when forward runtime present | `open_forward` with `local` | Forward lifecycle events | Daemon `ForwardRuntime` | v1 / API 0.10 |
| `forwards.remote` | Remote TCP forwards | Daemon: Implemented when forward runtime present | `open_forward` with `remote` | Forward lifecycle events | Daemon `ForwardRuntime` | v1 / API 0.10 |
| `forwards.dynamic` | Dynamic SOCKS forwards | Daemon: Implemented when forward runtime present | `open_forward` with `dynamic` | Forward lifecycle events | Daemon `ForwardRuntime` | v1 / API 0.10 |
| `daemon.status` | Read daemon lifecycle and diagnostics | Daemon: Implemented | `get_daemon_status`, `get_daemon_diagnostics`; wire `daemon.status`, `daemon.diagnostics` | None required | `DaemonLifecycleController` | v1 / API 0.11 |
| `daemon.control` | Stop or restart the daemon process | Daemon: Implemented | `stop_daemon`, `restart_daemon`; wire `daemon.stop`, `daemon.restart` | None required | Lifecycle drain and bounded cleanup | v1 / API 0.11 |
| `daemon.events` | Observe daemon lifecycle state changes | Daemon: Implemented | `subscribe_events` | `daemon.state_changed` | Bounded daemon event stream | v1 / API 0.11 |
| `secrets.read` | Read daemon-owned secret backend configuration, registry, and lock state | Daemon: Implemented when the secret backend service is installed | `get_secret_configuration`, `get_secret_backends`, `get_secret_state`; wire `secrets.configuration.get`, `secrets.backends.get`, `secrets.state.get` | None defined | Daemon `SecretBackendService` | v1 / API 0.12 |
| `secrets.write` | Update daemon-owned secret settings and backend selection | Daemon: Implemented when the secret backend service is installed | `update_secret_configuration`, `update_secret_selection`; wire `secrets.configuration.update`, `secrets.selection.update` | None defined | Daemon `SecretBackendService` and settings persistence | v1 / API 0.12 |
| `secrets.operate` | Unlock/lock the selected backend and run Bitwarden/rbw lifecycle operations | Daemon: Implemented when the secret backend service is installed | `unlock_secrets`, `lock_secrets`, `bitwarden_*`, `rbw_*`; wire `secrets.unlock`, `secrets.lock`, `secrets.bitwarden.*`, `secrets.rbw.*` | None defined | Daemon `SecretBackendService` and interaction broker | v1 / API 0.12 |
| `secrets.transfer` | Export and import secret backups inside the daemon | Daemon: Implemented when the secret backend service is installed | `export_secret_backup`, `import_secret_backup`; wire `secrets.transfer.export`, `secrets.transfer.import` | None defined | Daemon `SecretBackendService` and backup archive adapters | v1 / API 0.12 |
| `plugins` | Invoke core plugin operations | Schema only; no client method | None | None defined | Split core plugin service | v1 |
| `secrets` | Core-mediated secret operations/interactions | Deprecated; superseded by `secrets.*` capabilities | None | No dedicated event; interaction schemas may be used later | Secret service and permissions | v1 |
| `connections.config.read` | Read full editor state including filesystem paths | Schema only; no client method | None | None defined | Gated by `CONNECTIONS_CONFIG_READ` capability | v1 |
| `connections.config.write` | Write connection config fields beyond nickname/host/user/port | Schema only; no client method | None | None defined | Gated by `CONNECTIONS_CONFIG_WRITE` capability | v1 |
| `connections.secrets.write` | Create, update, and delete passwords, passphrases, and plugin secrets through authorized daemon RPCs | Daemon: Implemented | `store_connection_password`, `delete_connection_password`, `store_key_passphrase`, `delete_key_passphrase`; wire store/delete methods | None defined | Gated by `CONNECTIONS_SECRETS_WRITE` capability | v1 |
| `connections.metadata.write` | Write non-SSH metadata (tags, aliases, WoL settings) | `InProcessClient` and daemon: Implemented | `update_connection_metadata`, `add_tag_to_connections`; wire `connections.update_metadata`, `connections.metadata.add_tag` | None defined | Gated by `CONNECTIONS_METADATA_WRITE` capability | v1 |
| `connections.groups` | Assign and reorder connections within groups | `InProcessClient` and daemon: Implemented | `assign_connection_to_group`, `create_group`, `delete_group`, `rename_group`; wire `connections.assign_to_group`, `connections.create_group`, `connections.delete_group`, `connections.rename_group` | None defined | Gated by `CONNECTIONS_GROUPS` capability | v1 |
| `connections.split` | Split a connection block from a multi-host group | `InProcessClient` and daemon: Implemented | `split_connection`; wire `connections.split` | None defined | Gated by `CONNECTIONS_SPLIT` capability | v1 |

<!-- api-capability: connections.read -->
## `connections.read`

Implemented and contract-tested across both clients. The providers return
equivalent secret-free connection summaries/details. `InProcessClient` also
translates manager connection signals into frontend-neutral events.

<!-- api-capability: connections.events -->
## `connections.events`

Implemented and contract-tested across both clients. It guarantees live
delivery only from the point a subscription and daemon handshake are active;
there is no history or replay. Daemon continuity loss closes the transport, so
clients must obtain a fresh `connections.list` snapshot after a future explicit
reconnect. This capability is separate from `connections.read` so older
snapshot-only daemons cannot be mistaken for live providers.

<!-- api-capability: connections.write -->
## `connections.write`

Implemented and contract-tested across both clients for nickname, hostname,
username, port, and SSH protocol creation. Passwords, passphrases, key paths,
advanced SSH settings, group edits, tags, and Wake-on-LAN metadata are outside
the write DTO and are never silently discarded. Experimental GTK daemon mode
requires this capability together with `connections.read` and
`connections.events`; otherwise it falls back fully to in-process mode.

<!-- api-capability: sessions.read -->
## `sessions.read`

Daemon-only and contract-tested. Records are in-memory, creation-ordered, and
use `session-<n>` identifiers unique for one daemon lifetime. Closed records
are capped at 100 and are not persisted across restart.

<!-- api-capability: sessions.write -->
## `sessions.write`

Daemon-only and contract-tested for lifecycle control and logical attachment
bookkeeping. PTY bytes and typed interaction behaviour remain separately
negotiated through their narrow terminal/interaction capabilities.

<!-- api-capability: sessions.events -->
## `sessions.events`

Daemon-only delivery of four typed lifecycle events through the same global
sequence and bounded queues as connection events. It provides no replay or
cross-restart continuity.

<!-- api-capability: terminal -->
## `terminal`

Deprecated compatibility identifier. It is never advertised; clients must use
the narrow output/input/resize/replay capabilities.

<!-- api-capability: terminal.attach -->
## `terminal.attach`

Deprecated compatibility identifier. Logical attachment remains guarded by
`sessions.write`; binary output requires `terminal.output`.

<!-- api-capability: terminal.output -->
## `terminal.output`

Daemon-only raw PTY output over `binary-terminal-v2`. A client negotiates the
frame type, subscribes locally, and attaches with `want_terminal_output=True`.
Output uses per-session absolute byte offsets and never enters the CoreEvent
queue.

<!-- api-capability: terminal.input -->
## `terminal.input`

Daemon-only raw-byte input from the single daemon-authoritative input owner.
Input is bounded, ordered, never decoded, and never logged. The typed
`terminal.broadcast_input` method uses the same capability to fan out one
command to existing owned sessions; it never starts a new SSH process.

<!-- api-capability: terminal.resize -->
## `terminal.resize`

Daemon-only PTY resizing for the input-owning attachment. Rows and columns are
strictly limited to 1–1000.

<!-- api-capability: terminal.replay -->
## `terminal.replay`

Daemon-only bounded replay. The JSON response carries retained-range and
truncation metadata; replay bytes use the same binary output frames as live
data. `InProcessClient` remains unsupported.

<!-- api-capability: interactions -->
## `interactions`

Deprecated compatibility identifier and never advertised.

<!-- api-capability: interactions.read -->
## `interactions.read`

Lists only public typed metadata visible to the current attached or originating
client. Raw prompts and all secret values are excluded.

<!-- api-capability: interactions.respond -->
## `interactions.respond`

Provides explicit responder claims and typed decisions. Password/passphrase
bytes require the separately negotiated one-use binary frame and are never JSON.

<!-- api-capability: interactions.events -->
## `interactions.events`

Interaction creation/state snapshots share the daemon-global event sequence and
bounded slow-client policy. Events contain no response nonce or secret.

<!-- api-capability: interactions.host_key -->
## `interactions.host_key`

Unknown keys can be accepted once or atomically stored. Changed/revoked keys
remain blocking failures in this phase.

<!-- api-capability: interactions.password -->
## `interactions.password`

OpenSSH login-password askpass is classified conservatively, attempt-bounded,
and brokered without terminal prompt scraping.

<!-- api-capability: interactions.passphrase -->
## `interactions.passphrase`

Private-key passphrases are associated with the exact key prompt and use the
existing selected secret backend. Backend master-password unlock remains a
separate local UI concern.

<!-- api-capability: sftp -->
## `sftp`

Deprecated compatibility identifier. It is never advertised; clients must use
the narrow `sftp.read` / `sftp.write` / `sftp.events` / `sftp.metadata` /
`sftp.mutate` capabilities.

<!-- api-capability: sftp.read -->
## `sftp.read`

Daemon-only listing of SFTP services and remote directories when the SFTP
runtime is present. `InProcessClient` returns `unsupported_capability`.

<!-- api-capability: sftp.write -->
## `sftp.write`

Daemon-only open/attach/detach/close of SFTP services. Service identity is
daemon-lifetime opaque (`sftp-<n>`).

<!-- api-capability: sftp.events -->
## `sftp.events`

Daemon delivery of SFTP service lifecycle events through the shared global
sequence and bounded queues.

<!-- api-capability: sftp.metadata -->
## `sftp.metadata`

Daemon-only path metadata operations against a ready SFTP service: `stat`,
`lstat`, `realpath`, and `readlink`.

<!-- api-capability: sftp.mutate -->
## `sftp.mutate`

Daemon-only mutating remote filesystem operations: `mkdir`, `rmdir`, `remove`,
`rename`, `chmod`, and `symlink`.

<!-- api-capability: sftp.privileged_file -->
## `sftp.privileged_file`

Advertises daemon-owned elevated (sudo) remote file reads and replacements via
`sftp_read_file` / `sftp_replace_file` with `access=Sudo`. The sudo password
never crosses the wire as a DTO: the daemon presents a protected password
interaction through the interaction broker and feeds the one-use secret
directly to the child stdin. Required in addition to `sftp.read` /
`sftp.mutate` for privileged operations.

<!-- api-capability: transfers.read -->
## `transfers.read`

Daemon-only list/get of transfer records when the transfer runtime is present.

<!-- api-capability: transfers.write -->
## `transfers.write`

Daemon-only start/cancel of uploads and downloads. Transfer identity is
daemon-lifetime opaque (`transfer-<n>`).

<!-- api-capability: transfers.events -->
## `transfers.events`

Daemon delivery of transfer lifecycle and progress events through the shared
global sequence.

<!-- api-capability: transfers.upload -->
## `transfers.upload`

Advertises that `start_transfer` accepts the upload direction. Requires
`transfers.write` for the mutation itself.

<!-- api-capability: transfers.download -->
## `transfers.download`

Advertises that `start_transfer` accepts the download direction. Requires
`transfers.write` for the mutation itself.

<!-- api-capability: transfers.scp -->
## `transfers.scp`

Advertises daemon-owned native OpenSSH SCP upload/download through
`start_scp_transfer`. It is present only when the daemon has a usable SCP launch
backend; clients must not fall back to GTK-owned subprocesses.

<!-- api-capability: port_forwarding -->
## `port_forwarding`

Deprecated compatibility identifier. It is never advertised; clients must use
the narrow `forwards.*` capabilities.

<!-- api-capability: forwards.read -->
## `forwards.read`

Daemon-only list/get of runtime forwards when the forward runtime is present.

<!-- api-capability: forwards.write -->
## `forwards.write`

Daemon-only open/close of runtime forwards. Forward identity is daemon-lifetime
opaque (`forward-<n>`).

<!-- api-capability: forwards.events -->
## `forwards.events`

Daemon delivery of forward lifecycle events through the shared global sequence.

<!-- api-capability: forwards.local -->
## `forwards.local`

Advertises local TCP forward support for `open_forward`.

<!-- api-capability: forwards.remote -->
## `forwards.remote`

Advertises remote TCP forward support for `open_forward`.

<!-- api-capability: forwards.dynamic -->
## `forwards.dynamic`

Advertises dynamic SOCKS forward support for `open_forward`.

<!-- api-capability: daemon.status -->
## `daemon.status`

Daemon lifecycle snapshots and diagnostics. Safe for support bundles; no secrets,
paths, or terminal payloads.

<!-- api-capability: daemon.control -->
## `daemon.control`

Graceful stop and restart with optional confirmation when live resources would
be lost. Does not imply automatic client reconnect or resource restoration.

<!-- api-capability: daemon.events -->
## `daemon.events`

Lifecycle state changes via `daemon.state_changed` on the bounded event stream.

<!-- api-capability: plugins -->
## `plugins`

Schema only. Plugin operation models exist; current plugin APIs are separate
from `SshPilotClient`.

<!-- api-capability: secrets -->
## `secrets`

Schema only. Frontends must not interpret this as permission to access
`SecretManager` or its providers directly.

<!-- api-capability: connections.config.read -->
## `connections.config.read`

Schema only. Provides full editor state including filesystem paths, identity
configuration, forwarding rules, and all advanced SSH settings. Gated behind
the `CONNECTIONS_CONFIG_READ` capability in the handshake response.

<!-- api-capability: connections.config.write -->
## `connections.config.write`

Schema only. Enables writing connection config fields beyond the basic
nickname/host/user/port set. Includes forwarding rules, proxy jump, identity
files, X11 forwarding, extra SSH config, and all advanced settings. Gated
behind the `CONNECTIONS_CONFIG_WRITE` capability.

<!-- api-capability: connections.secrets.write -->
## `connections.secrets.write`

Implemented by the daemon. Enables writing passwords, passphrases, and
plugin-scoped secrets through daemon RPCs rather than local GTK writes.
Ensures secrets flow through the daemon-owned secret service and connection
identity-transition saga. Gated behind `CONNECTIONS_SECRETS_WRITE`.

<!-- api-capability: connections.secrets.status.read -->
## `connections.secrets.status.read`

Returns only boolean credential-availability metadata for the connection editor;
no secret value crosses the response envelope. Gated by
`CONNECTIONS_SECRETS_STATUS_READ`.

<!-- api-capability: connections.secrets.reveal -->
## `connections.secrets.reveal`

Explicitly reveals one saved password, key passphrase, or plugin secret only
after a client requests it. The JSON response is an acknowledgment; the value is delivered
through a one-use binary secret frame and never appears in ordinary JSON,
events, or replay. Gated by `CONNECTIONS_SECRETS_REVEAL`.

<!-- api-capability: connections.metadata.write -->
## `connections.metadata.write`

Schema only. Enables writing non-SSH metadata such as tags, aliases, and
Wake-on-LAN settings. Gated behind `CONNECTIONS_METADATA_WRITE`.

<!-- api-capability: connections.groups -->
## `connections.groups`

Schema only. Enables assigning connections to groups and reordering within
groups. Gated behind `CONNECTIONS_GROUPS`.

<!-- api-capability: connections.split -->
## `connections.split`

Implemented. Splits a connection block from a multi-host group into its own
standalone entry. Removes the host token from the original block and appends a
new standalone `Host` block. Gated behind `CONNECTIONS_SPLIT`.

<!-- api-capability: known_hosts.read -->
## `known_hosts.read`

Implemented by the daemon when its known-hosts service is installed. Returns a
revisioned snapshot of displayable entries from the daemon-owned known-hosts
file. Gated behind `KNOWN_HOSTS_READ`.

<!-- api-capability: known_hosts.write -->
## `known_hosts.write`

Implemented by the daemon when its known-hosts service is installed. Removes a
batch of occurrence-specific entry IDs using an optimistic revision check.
Gated behind `KNOWN_HOSTS_WRITE`. Mutation is unavailable while the daemon is
draining.

<!-- api-capability: keys.read -->
## `keys.read`

Implemented when the daemon key service is installed. Lists key metadata from
the daemon-owned selected key store scope (`keys.list`) and returns public-key
text for an opaque key ID (`keys.get_public`). Gated behind `KEYS_READ`.

<!-- api-capability: keys.write -->
## `keys.write`

Implemented when the daemon key service is installed. Generates keypairs and
verifies private-key passphrases through daemon-owned `ssh-keygen`
(`keys.generate`, `keys.verify_passphrase`). Protected input uses interaction
secret frames and never ordinary request JSON or native argv. Gated behind
`KEYS_WRITE`. Both methods are unavailable while the daemon is draining.

<!-- api-capability: identity.read -->
## `identity.read`

Implemented in the daemon identity provider service. Returns provider, identity
state, agent-key, and authorized-key metadata only. This capability is
implemented but pending its separate frontend-neutral phase review.

<!-- api-capability: identity.write -->
## `identity.write`

Implemented in the daemon identity provider service. Updates identity selection
and provider configuration through typed requests. This capability is
implemented but pending its separate frontend-neutral phase review.

<!-- api-capability: identity.operate -->
## `identity.operate`

Implemented in the daemon identity provider service. Runs agent-key mutations,
key deployment, and authorized-key removal. Native OpenSSH behavior remains
authoritative. This capability is implemented but pending its separate
frontend-neutral phase review.

<!-- api-capability: operations.read -->
## `operations.read`

Implemented whenever the daemon's shared `OperationRuntime` is available --
independently of the identity service. Returns a typed `OperationSummary` for
any operation the requesting client owns (`operations.get`). Backs key
deployment/authorized-key removal (identity) as well as SFTP's
`sftp.directory_size` and recursive `sftp.copy`/`sftp.remove`, so an
SFTP-capable daemon with no identity service installed can still poll its own
tree operations.

<!-- api-capability: operations.control -->
## `operations.control`

Implemented whenever the daemon's shared `OperationRuntime` is available --
independently of the identity service. Requests cooperative cancellation of an
operation the requesting client owns (`operations.cancel`). A different
client's operation is neither visible nor cancellable.

<!-- api-capability: ssh_overrides.read -->
## `ssh_overrides.read`

Implemented when the SSH overrides service is installed. Returns the current
global SSH overrides state (`ssh_overrides.get`). Gated behind `SSH_OVERRIDES_READ`.

<!-- api-capability: ssh_overrides.write -->
## `ssh_overrides.write`

Implemented when the SSH overrides service is installed. Partially updates
(`ssh_overrides.update`) or resets (`ssh_overrides.reset`) global SSH overrides.
Gated behind `SSH_OVERRIDES_WRITE`. Write operations use optimistic concurrency
control via `expected_revision`.

<!-- api-capability: secrets.read -->
## `secrets.read`

Implemented when the daemon secret backend service is installed. Returns the
daemon-owned `secrets.*` configuration (`secrets.configuration.get`), the
backend registry (`secrets.backends.get`), and the current backend selection and
lock state (`secrets.state.get`). Only metadata crosses the wire; secret values
never leave the daemon. Gated behind `SECRETS_READ`.

<!-- api-capability: secrets.write -->
## `secrets.write`

Implemented when the daemon secret backend service is installed. Partially
updates the daemon-owned `secrets.*` configuration
(`secrets.configuration.update`) or changes the selected backend
(`secrets.selection.update`). Secret values are never accepted or returned by
these wire methods. Gated behind `SECRETS_WRITE`.

<!-- api-capability: secrets.operate -->
## `secrets.operate`

Implemented when the daemon secret backend service is installed. Unlocks
(`secrets.unlock`) or locks (`secrets.lock`) the selected backend and runs
Bitwarden (`secrets.bitwarden.*`) or rbw (`secrets.rbw.*`) lifecycle operations.
Master passwords, 2FA codes, API-key client secrets, and SSO authentication use
the protected interaction path and never appear in ordinary JSON payloads.
Gated behind `SECRETS_OPERATE`.

<!-- api-capability: secrets.transfer -->
## `secrets.transfer`

Implemented when the daemon secret backend service is installed. Exports
(`secrets.transfer.export`) or imports (`secrets.transfer.import`) secret
backups entirely inside the daemon. Results carry paths, counts, and warnings
only; no secret values are returned. Gated behind `SECRETS_TRANSFER`.

## Frontend behaviour

Check capabilities before displaying or enabling optional actions. A frontend
may hide unavailable features or show an explanatory disabled state. It must
still handle `unsupported_capability`, because a provider can change or an
operation can race with shutdown. Do not infer support from class or schema
presence.

Capabilities should represent meaningful feature groups. Do not create one for
every trivial method; add one when clients need to negotiate a coherent optional
feature.

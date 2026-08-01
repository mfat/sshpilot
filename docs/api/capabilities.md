# Capabilities

Capabilities report implemented runtime support. The existence of a method,
event identifier, or schema does not imply support. Clients must check optional
capabilities and handle `unsupported_capability`.

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
<!-- api-runtime-capability: connections.secrets.write -->
<!-- api-daemon-runtime-capability: connections.read -->
<!-- api-daemon-runtime-capability: connections.events -->
<!-- api-daemon-runtime-capability: connections.write -->
<!-- api-daemon-runtime-capability: connections.config.read -->
<!-- api-daemon-runtime-capability: connections.secrets.write -->
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
<!-- api-daemon-runtime-capability: transfers.read -->
<!-- api-daemon-runtime-capability: transfers.write -->
<!-- api-daemon-runtime-capability: transfers.events -->
<!-- api-daemon-runtime-capability: transfers.upload -->
<!-- api-daemon-runtime-capability: transfers.download -->
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
| `connections.read` | Read saved connection DTO snapshots | `InProcessClient` and daemon: Implemented | `list_connections`, `get_connection`; wire `connections.list`, `connections.get` | None required | Existing `ConnectionManager` through `InProcessClient` | v1 |
| `connections.events` | Subscribe to live connection lifecycle events | `InProcessClient` and daemon: Implemented | `subscribe_events` | `connection.created`, `connection.updated`, `connection.deleted` | Typed event codec and bounded delivery queues | v1 |
| `connections.write` | Create, update, and delete basic saved connection metadata | `InProcessClient` and daemon: Implemented | `create_connection`, `update_connection`, `delete_connection`; wire `connections.create`, `connections.update`, `connections.delete` | `connection.created`, `connection.updated`, `connection.deleted` | Existing `ConnectionManager` through `InProcessClient` | v1 |
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
| `transfers.read` | List and inspect transfer records | Daemon: Implemented when transfer runtime present | `list_transfers`, `get_transfer` | None required | Daemon `TransferRuntime` | v1 / API 0.10 |
| `transfers.write` | Start and cancel transfers | Daemon: Implemented when transfer runtime present | `start_transfer`, `cancel_transfer` | Transfer lifecycle events | Daemon `TransferRuntime` and ready SFTP service | v1 / API 0.10 |
| `transfers.events` | Observe transfer lifecycle and progress | Daemon: Implemented | `subscribe_events` | `transfer.*` lifecycle events | Bounded daemon event stream | v1 / API 0.10 |
| `transfers.upload` | Upload direction for `start_transfer` | Daemon: Implemented when transfer runtime present | `start_transfer` with `upload` | Transfer lifecycle events | Daemon path local mode | v1 / API 0.10 |
| `transfers.download` | Download direction for `start_transfer` | Daemon: Implemented when transfer runtime present | `start_transfer` with `download` | Transfer lifecycle events | Daemon path local mode | v1 / API 0.10 |
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
| `plugins` | Invoke core plugin operations | Schema only; no client method | None | None defined | Split core plugin service | v1 |
| `secrets` | Core-mediated secret operations/interactions | Schema only; no client method | None | No dedicated event; interaction schemas may be used later | Secret service and permissions | v1 |
| `connections.config.read` | Read full editor state including filesystem paths | Schema only; no client method | None | None defined | Gated by `CONNECTIONS_CONFIG_READ` capability | v1 |
| `connections.config.write` | Write connection config fields beyond nickname/host/user/port | Schema only; no client method | None | None defined | Gated by `CONNECTIONS_CONFIG_WRITE` capability | v1 |
| `connections.secrets.write` | Write passwords and passphrases through daemon RPCs | `InProcessClient` and daemon: Implemented | `store_connection_password`, `delete_connection_password`, `store_key_passphrase`, `lookup_key_passphrase`; wire `connections.store_password`, `connections.delete_password`, `connections.store_passphrase`, `connections.lookup_passphrase` | None defined | Gated by `CONNECTIONS_SECRETS_WRITE` capability | v1 |
| `connections.metadata.write` | Write non-SSH metadata (tags, aliases, WoL settings) | Schema only; no client method | None | None defined | Gated by `CONNECTIONS_METADATA_WRITE` capability | v1 |
| `connections.groups` | Assign and reorder connections within groups | Schema only; no client method | None | None defined | Gated by `CONNECTIONS_GROUPS` capability | v1 |
| `connections.split` | Split a connection block from a multi-host group | Schema only; no client method | None | None defined | Gated by `CONNECTIONS_SPLIT` capability | v1 |

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
Input is bounded, ordered, never decoded, and never logged.

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

Schema only. Enables writing passwords and passphrases through daemon RPCs
rather than local GTK writes. Ensures secrets flow through the daemon's
identity-transition saga. Gated behind `CONNECTIONS_SECRETS_WRITE`.

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

Schema only. Enables splitting a connection block from a multi-host group
into its own standalone entry. Gated behind `CONNECTIONS_SPLIT`.

## Frontend behaviour

Check capabilities before displaying or enabling optional actions. A frontend
may hide unavailable features or show an explanatory disabled state. It must
still handle `unsupported_capability`, because a provider can change or an
operation can race with shutdown. Do not infer support from class or schema
presence.

Capabilities should represent meaningful feature groups. Do not create one for
every trivial method; add one when clients need to negotiate a coherent optional
feature.

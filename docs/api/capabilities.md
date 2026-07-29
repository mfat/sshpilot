# Capabilities

Capabilities report implemented runtime support. The existence of a method,
event identifier, or schema does not imply support. Clients must check optional
capabilities and handle `unsupported_capability`.

`InProcessClient` advertises exactly the three connection capabilities. The
daemon additionally advertises session lifecycle and four narrow terminal
capabilities when its PTY runner is available and the client negotiated
`binary-terminal-v1`; `DaemonClient` returns that negotiated response rather
than a hard-coded local assumption.

Stable IDs do not add a capability: `ConnectionId` was already opaque, all
current providers emit the stable form, and clients must not branch on its
syntax.

Experimental GTK daemon composition verifies all three capabilities after the real
handshake and before injecting the client. A snapshot-only older daemon is not
used because GTK would otherwise have no truthful live-refresh guarantee.

<!-- api-runtime-capability: connections.read -->
<!-- api-runtime-capability: connections.events -->
<!-- api-runtime-capability: connections.write -->
<!-- api-daemon-runtime-capability: connections.read -->
<!-- api-daemon-runtime-capability: connections.events -->
<!-- api-daemon-runtime-capability: connections.write -->
<!-- api-daemon-runtime-capability: sessions.read -->
<!-- api-daemon-runtime-capability: sessions.write -->
<!-- api-daemon-runtime-capability: sessions.events -->
<!-- api-daemon-runtime-capability: terminal.output -->
<!-- api-daemon-runtime-capability: terminal.input -->
<!-- api-daemon-runtime-capability: terminal.resize -->
<!-- api-daemon-runtime-capability: terminal.replay -->

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
| `interactions` | Present and answer core-requested user interactions | Unsupported | `respond_to_interaction` | `session.interaction_requested` | Interaction broker and secret-safe frontend bridge | v1 |
| `sftp` | Frontend-neutral remote file operations | Schema only; no client method | None | None defined | Core OpenSSH SFTP service | v1 |
| `port_forwarding` | Manage runtime forwards | Schema only; no client method | None | None defined | Session/forward lifecycle service | v1 |
| `plugins` | Invoke core plugin operations | Schema only; no client method | None | None defined | Split core plugin service | v1 |
| `secrets` | Core-mediated secret operations/interactions | Schema only; no client method | None | No dedicated event; interaction schemas may be used later | Secret service and permissions | v1 |

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
use `session:<uuid>` identifiers unique for one daemon lifetime. Closed records
are capped at 100 and are not persisted across restart.

<!-- api-capability: sessions.write -->
## `sessions.write`

Daemon-only and contract-tested for lifecycle control and logical attachment
bookkeeping. It does not imply successful SSH startup, PTY allocation, terminal
bytes, prompts, replay, or secrets. The production runner currently reports
safe failed startup until those later capabilities exist.

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

Daemon-only raw PTY output over `binary-terminal-v1`. A client negotiates the
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

Unsupported. Interaction request/response schemas exist; the current
credential dialogs and askpass flow have not been migrated to an interaction
broker.

<!-- api-capability: sftp -->
## `sftp`

Schema only. Existing SFTP implementation is not reachable through
`SshPilotClient`; only transport-neutral entry/list request models exist.

<!-- api-capability: port_forwarding -->
## `port_forwarding`

Schema only. Forward summary/state models exist; there are no client methods or
events.

<!-- api-capability: plugins -->
## `plugins`

Schema only. Plugin operation models exist; current plugin APIs are separate
from `SshPilotClient`.

<!-- api-capability: secrets -->
## `secrets`

Schema only. Frontends must not interpret this as permission to access
`SecretManager` or its providers directly.

## Frontend behaviour

Check capabilities before displaying or enabling optional actions. A frontend
may hide unavailable features or show an explanatory disabled state. It must
still handle `unsupported_capability`, because a provider can change or an
operation can race with shutdown. Do not infer support from class or schema
presence.

Capabilities should represent meaningful feature groups. Do not create one for
every trivial method; add one when clients need to negotiate a coherent optional
feature.

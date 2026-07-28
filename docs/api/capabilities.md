# Capabilities

Capabilities report implemented runtime support. The existence of a method,
event identifier, or schema does not imply support. Clients must check optional
capabilities and handle `unsupported_capability`.

`InProcessClient` currently advertises exactly `connections.read`.

<!-- api-runtime-capability: connections.read -->

## Inventory

| Identifier | Meaning | Provider/status | Related methods | Related events | Dependencies | Introduced |
| --- | --- | --- | --- | --- | --- | --- |
| `connections.read` | Read saved connection DTOs | `InProcessClient`: Implemented | `list_connections`, `get_connection` | All `connection.*` events | Existing `ConnectionManager` | v1 |
| `connections.write` | Create, update, and delete saved connections | Unsupported | `create_connection`, `update_connection`, `delete_connection` | Intended `connection.*` | Persistence/validation service | v1 |
| `terminal` | Open/close sessions and send input/resize | Unsupported | Session and terminal methods | Intended session lifecycle/output | Core session, PTY, process, SSH/auth services | v1 |
| `terminal.attach` | Attach/detach clients and assign input ownership | Unsupported | `attach_session`, `detach_session` | No dedicated event currently defined | Runtime session service | v1 |
| `terminal.replay` | Replay retained terminal bytes | Schema only; no client method | None; `ReplayRequest`/`ReplayResult` exist | `session.output` is schema only | Bounded per-session replay buffer | v1 |
| `interactions` | Present and answer core-requested user interactions | Unsupported | `respond_to_interaction` | `session.interaction_requested` | Interaction broker and secret-safe frontend bridge | v1 |
| `sftp` | Frontend-neutral remote file operations | Schema only; no client method | None | None defined | Core OpenSSH SFTP service | v1 |
| `port_forwarding` | Manage runtime forwards | Schema only; no client method | None | None defined | Session/forward lifecycle service | v1 |
| `plugins` | Invoke core plugin operations | Schema only; no client method | None | None defined | Split core plugin service | v1 |
| `secrets` | Core-mediated secret operations/interactions | Schema only; no client method | None | No dedicated event; interaction schemas may be used later | Secret service and permissions | v1 |

<!-- api-capability: connections.read -->
## `connections.read`

Implemented and contract-tested. The provider returns secret-free connection
summaries/details. It also translates the existing manager's
`connection-added`, `connection-updated`, and `connection-removed` GObject
signals into frontend-neutral events.

<!-- api-capability: connections.write -->
## `connections.write`

Unsupported. All three write methods return:

```text
code = unsupported_capability
details.capability = connections.write
```

<!-- api-capability: terminal -->
## `terminal`

Unsupported. Session creation/closure, terminal input, and resize methods fail
with `unsupported_capability`. Existing GTK terminal behaviour is not exposed
through this API yet.

<!-- api-capability: terminal.attach -->
## `terminal.attach`

Unsupported. Attachment and input-ownership schemas exist, but no runtime
session service owns them.

<!-- api-capability: terminal.replay -->
## `terminal.replay`

Schema only. Replay request/result models define bounds and byte preservation,
but `SshPilotClient` currently declares no replay method. This missing method is
recorded in the [implementation audit](implementation-audit.md).

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

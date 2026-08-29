# Architecture reference

sshPilot is a native OpenSSH client with GTK, CLI, and future frontends over a
frontend-neutral API and daemon architecture. The frontend-neutral migration
and final frontend closure are complete: GTK is a client of the system, not
the owner of backend state or remote I/O.

For development workflow, see [running-from-source.md](running-from-source.md)
and [../CONTRIBUTING.md](../CONTRIBUTING.md). For the concrete public contract,
see the [frontend-neutral API reference](api/README.md). The active
cross-session retirement ledger is
[daemon-only-retirement.md](architecture/daemon-only-retirement.md).

## Current architecture boundary

Production frontend-neutral operations follow this boundary:

```text
GTK / CLI / future frontends
        ↓
typed SshPilotClient
        ↓
daemon transport / dispatcher
        ↓
GTK-free application/core services
        ↓
native OpenSSH + existing adapters
```

The daemon is the authoritative owner of backend state, persistence,
subprocesses, PTYs, interactions, secrets, transfers, and forwarding. Clients
negotiate capabilities and receive structured unsupported-capability errors;
they never silently select a second backend implementation.

GTK owns presentation: widgets, navigation, selection, rendering, file
selection, dialogs, interaction presentation, and frontend-local transient
state. A controller may cache DTOs and stage a local edit, but it does not own
the file, process, secret, or service represented by that DTO.

## Ownership boundaries

| Responsibility | Authoritative owner | Frontend boundary |
| --- | --- | --- |
| Connections, groups, ordering, and safe metadata | `ConnectionRepository` through `ConnectionApplicationService` | Typed connection APIs and snapshot controllers |
| Known-host entries | `KnownHostsService` | `known_hosts.*` API and staged GTK editor state |
| SSH key discovery, public-key reads, and generation | `DaemonKeyService` over the core key service | `keys.*` API; private-key contents never cross the API |
| Global SSH overrides | `SshOverridesService` | `ssh_overrides.*` API with revision-safe writes |
| Secret backend selection and lifecycle | `SecretBackendService` | `secrets.*` API and protected interactions |
| Runtime connection secret resolution | `DaemonConnectionSecretProvider` | Daemon session/connection services; no ordinary secret-bearing DTOs |
| Identity provider state, agent inspection, effective identities, and native deployment | `IdentityStateService` and `DaemonIdentityService` | `identity.*` APIs; GTK receives safe metadata and operation snapshots only |
| Authorized-key documents and file replacement | `SftpServiceRuntime` plus `DaemonIdentityService` | Typed bounded file reads/replacements; GTK stages and presents document edits |

`ConnectionPresentationStore` is the only connection object retained by GTK;
it is a read-only DTO projection. The old stateful `ConnectionManager` is gone
from production and survives only as a model-only import shim for the bounded
compatibility window. `BackupManager` and the other core services are composed
inside the daemon, never instantiated by GTK. Production GTK controllers use
`SshPilotClient` and do not perform backend I/O.

## Current subsystem ownership

The following milestones have completed their reviewed ownership migration:

- **SSH keys:** `DaemonKeyService` owns scope resolution, discovery, public-key
  reads, and `ssh-keygen`; `keys.read` and `keys.write` are daemon capabilities.
  GTK retains only public summaries and presentation.
- **Known hosts:** `KnownHostsService` parses and atomically mutates the file;
  `known_hosts.read` and `known_hosts.write` expose revisioned snapshots and
  mutations. GTK stages selection and presents conflicts.
- **Connections, groups, and metadata:** `ConnectionRepository` and
  `ConnectionApplicationService` persist and publish the authoritative store;
  `connections.*` APIs expose explicit DTOs and group/metadata capabilities.
  SSH `Host` aliases remain connection identity, and secret-like metadata is
  rejected.
- **Global SSH overrides:** `SshOverridesService` validates, normalizes,
  revisions, and atomically persists global `ssh.*` settings through
  `ssh_overrides.read` and `ssh_overrides.write`. Preferences submits typed
  requests rather than composing or persisting a separate override list.
- **Secret backend management and secret-bearing backup transfer:**
  `SecretBackendService` owns selection, configuration, lifecycle, protected
  interactions, and daemon-side backup operations. Existing backend and backup
  implementations are reused rather than rewritten.
- **Identity providers and authorized-key management:**
  `IdentityStateService` and `DaemonIdentityService` own provider state, native
  `ssh-add` inspection/mutation, effective identity resolution through
  `ssh -G`, protected authentication preparation, native `ssh-copy-id`, fixed
  ordinary-`ssh` authorized-key operations, and shared operation lifecycle.
  `SftpServiceRuntime` owns bounded file reads and revision-safe atomic
  replacements, including backup and secure permissions for local and remote
  authorized-key documents. Replacements serialize the full compare-and-replace
  sequence per target (a per-target lock, keyed by target kind, service, and
  canonical/validated path) so concurrent same-revision replacements yield one
  success and one `file_revision_conflict` without blocking unrelated targets.
  GTK only stages and presents edits.

Native SCP and SFTP services, browser fallback policy, broadcast/remote
commands, plugin settings/command/session APIs, architecture governance, and
frontend operational SSH cleanup are complete through the daemon/API route.
GTK retains chooser, portal, browser, progress, cancellation, and other
explicitly frontend-local/platform operations only. The closure audit records
the final inventory and approved compatibility/dependency debt:

```text
migration-required identities: 0
semantic migration capabilities: 0
frontend operational SSH fallback: none
```

Remaining M4/M5/M6/M7-style compatibility or dependency debt is not a
frontend migration blocker and does not reopen the completed migration.

## Secret architecture

```text
GTK SecretBackendsController
        ↓
secrets.* typed API
        ↓
SecretBackendService
        ↓
existing SecretManager
        ↓
libsecret / keyring / pass / bw / rbw / KDBX / agent
```

The existing backends were reused rather than rewritten. The daemon owns backend
selection, configuration, lifecycle, availability, unlock, lock, and operation
state. GTK does not unlock Bitwarden or KDBX directly, execute `bw` or `rbw`, or
construct secret-bearing backend state.

Bitwarden password, API-key, SSO, two-factor, and authentication-challenge
flows are daemon-owned. rbw retains its native agent and pinentry behavior.
KDBX create, unlock, and lock are daemon-owned. Remembered master passwords use
the existing platform-keyring identities, not the vault being unlocked.
Sensitive input is collected through protected, one-use interactions.
For daemon-owned secret prompts, the public interaction carries a stable
`SecretPromptKind` and validated non-secret parameters rather than a rendered
English heading/body. GTK owns the mapping to gettext msgids, translates only
when presenting the dialog, and formats dynamic values afterward. The daemon
therefore remains independent of the frontend's selected locale.

Secret lifecycle statuses and operation results follow the same boundary:
their public DTOs carry a stable `SecretMessageCode`, validated parameters, and
an optional opaque backend diagnostic. GTK translates the local template at
display time and appends diagnostics unchanged. No rendered UI sentence is a
daemon/API identifier.

Backup/import results and previews use `SecretTransferMessageCode` and
`SecretTransferMessage` for the same reason. Parameters and ordered warnings
remain structured across RPC, while backend, filesystem, and SSH diagnostics
stay in an opaque field. GTK owns gettext translation, plural selection,
backup-section labels, and formatting; the daemon never selects UI text.

Secret values, `BW_SESSION`, KDBX transformed keys, provider credentials,
private keys, and backup manifests do not cross the ordinary public API. They
are not ordinary DTO fields, events, logs, or diagnostics. GTK receives typed
state and safe operation results, presents interactions, and owns only file
selection and presentation of the resulting status.

Explicit backend selection remains exclusive: `store`, `lookup`, and `delete`
use only the selected backend. `auto` retains its compatibility behavior,
including lookup/deletion across available backends so changing selection does
not orphan existing secrets.

## Backup architecture

Secret-bearing export, preview, listing, and import run inside the daemon while
reusing the existing implementations:

```text
BackupManager
CredentialManager
backup_archive
backup_backends
existing .spbk format and merge behavior
```

The daemon enumerates credentials and applies restores. Encrypted backup
passphrases use protected interactions, and decrypted manifests remain
process-local, short-lived, and one-use. GTK supplies safe operation options and
file selections; it receives only paths, counts, warnings, manifests without
secret values, and completion state. Frontend code must not construct a
secret-bearing backup or enumerate decrypted credentials.

## OpenSSH and native backend rules

Before implementing an SSH feature, check whether OpenSSH already supplies it.
sshPilot supervises and exposes native operations rather than reimplementing
OpenSSH protocols or semantics:

| Operation | Native OpenSSH operation |
| --- | --- |
| Configuration evaluation | `ssh -G` |
| Algorithms | `ssh -Q` |
| Key generation | `ssh-keygen` |
| Key deployment | `ssh-copy-id` |
| Agent loading | `ssh-add` |
| Known hosts | `ssh-keygen -F` / `ssh-keygen -R` and `ssh-keyscan` |
| File copying | `scp` |
| Interactive browsing | Existing SFTP infrastructure |
| Forwards | `ssh -L` / `ssh -R` / `ssh -D` |
| Remote commands | `ssh host command` |
| Multiplex control | `ssh -O` / `ControlMaster` |

New operations extend the existing native OpenSSH builders, authentication
resolver, interaction broker, and daemon services. They do not create a
parallel SSH protocol, command builder, secret environment, or fallback path.

## Connections and SSH configuration

`ConnectionRepository` and `ConnectionApplicationService` own saved connection
records, groups, metadata, SSH configuration persistence, and change events on
the daemon route. A saved connection is identified by its SSH `Host` alias.
Public connection DTOs are explicit and omit passwords, passphrases, private
keys, provider tokens, secret-bearing environment variables, and internal
objects.

`SshOverridesService` is the only production authority for global SSH
overrides. Preferences calls `get_global_ssh_overrides`,
`update_global_ssh_overrides`, and `reset_global_ssh_overrides`; it does not
compose or persist a competing `ssh_overrides` list. Updates are normalized,
revision-checked, transaction-locked, and atomically persisted.

Per-host behavior remains in OpenSSH configuration. The daemon invokes `ssh -G`
when it must inspect effective configuration and returns normalized comparison
DTOs; GTK never chooses `-F`, reads the config root, or runs that probe. Preserve
`Include`, `Match`, `ProxyJump`, `ProxyCommand`, identity, certificate,
forwarding, host-key, and authentication semantics supplied by OpenSSH.

## Shared long-running operations

`OperationRuntime` is the single daemon owner of user-visible long-running
operation lifecycle. It owns opaque operation IDs, immutable queued/running/
terminal snapshots, safe progress and failure metadata, operation events,
cooperative cancellation, bounded terminal retention, and bounded shutdown.
Services do not maintain a second operation registry or terminal state machine.

The approved pattern is:

```text
service starts work
    ↓
registers with OperationRuntime
    ↓
worker reports safe progress
    ↓
runtime owns terminal state and events
```

Operation events are ordinary typed API events. They are published only after
the runtime stores the new snapshot, use the existing bounded event delivery
behavior, and contain no private implementation objects or secret values.
Cancellation is real when a producer registers a supervised process or
cancellation hook; otherwise the producer must report cancellation truthfully
rather than claiming an interrupted mutation. Identity operation producers use
this same runtime and their typed service contracts.

## Sessions, transfers, and interactions

Daemon-backed SSH sessions, PTYs, SFTP services, transfers, forwards, and
interaction records are daemon resources. GTK attaches views to those resources
and renders output, progress, errors, and typed prompts. A daemon failure never
silently falls back to a GTK-owned SSH or backend process.

Local shell tabs and user-selected external terminals are explicit frontend or
external-process exceptions. They are not daemon sessions and must not be used
as an excuse to add a second production implementation.

## Security boundary

- Never store or log plaintext secrets.
- Never expose secret-provider objects, private keys, raw PTYs, subprocesses, or
  secret-bearing environment variables in public DTOs.
- Use the interaction broker for passwords, passphrases, MFA, PIN, host-key
  confirmation, and hardware-presence prompts.
- Preserve ssh-agent behavior and OpenSSH host-key verification.
- Redact diagnostics and user-visible errors.
- Convert unexpected backend failures into safe structured API errors.

## Related architecture references

- [Core boundary](architecture/core-boundary.md)
- [Frontend closure audit](architecture/frontend-closure-audit.md)
- [Dependency direction](architecture/dependency-direction.md)
- [Headless core development](development/headless-core.md)
- [API maintenance](api/maintenance.md)

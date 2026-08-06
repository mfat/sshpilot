# Architecture reference

sshPilot is a native OpenSSH client with a GTK frontend and a
frontend-neutral core, API, and daemon architecture. GTK is a client of the
system, not the owner of backend state or I/O.

For development workflow, see [running-from-source.md](running-from-source.md)
and [../CONTRIBUTING.md](../CONTRIBUTING.md). For the concrete public contract,
see the [frontend-neutral API reference](api/README.md).

## Main boundary

Production frontend-neutral operations follow this boundary:

```text
GTK / CLI / future clients
        ↓
typed SshPilotClient API
        ↓
daemon dispatcher and services
        ↓
GTK-free core/application services
        ↓
native OpenSSH and existing backend adapters
```

The daemon is the authoritative owner of backend state, persistence,
subprocesses, PTYs, interactions, secrets, transfers, and forwarding. Clients
negotiate capabilities and receive structured unsupported-capability errors;
they never silently select a second backend implementation.

GTK owns presentation: widgets, navigation, selection, rendering, file
selection, dialogs, interaction presentation, and frontend-local transient
state. A controller may cache DTOs and stage a local edit, but it does not own
the file, process, secret, or service represented by that DTO.

## Current ownership

| Responsibility | Authoritative owner | Frontend boundary |
| --- | --- | --- |
| Connections, groups, ordering, and safe metadata | `ConnectionRepository` through `ConnectionApplicationService` | Typed connection APIs and snapshot controllers |
| Known-host entries | `KnownHostsService` | `known_hosts.*` API and staged GTK editor state |
| SSH key discovery, public-key reads, and generation | `DaemonKeyService` over the core key service | `keys.*` API; private-key contents never cross the API |
| Global SSH overrides | `SshOverridesService` | `ssh_overrides.*` API with revision-safe writes |
| Secret backend selection and lifecycle | `SecretBackendService` | `secrets.*` API and protected interactions |
| Runtime connection secret resolution | `DaemonConnectionSecretProvider` | Daemon session/connection services; no ordinary secret-bearing DTOs |

`ConnectionManager`, `GroupManager`, `SecretManager`, `BackupManager`, and
other GObject or compatibility adapters may remain behind explicit compatibility
routes. They are not the authoritative production owners when the daemon route
is selected. Production GTK controllers use `SshPilotClient` and do not
instantiate backend services or perform backend I/O.

## Completed frontend-neutral milestones

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

Shared operations and identity state/provider services are implemented on the
development branch but are not declared complete until their separate phase
reviews finish. See [frontend-neutral-migration.md](frontend-neutral-migration.md).

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

Per-host behavior remains in OpenSSH configuration. Use `ssh -G` when code must
inspect effective configuration, and preserve `Include`, `Match`, `ProxyJump`,
`ProxyCommand`, identity, certificate, forwarding, host-key, and authentication
semantics supplied by OpenSSH.

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

- [Daemon ownership](architecture/daemon-ownership.md)
- [Daemon-only production rules](architecture/daemon-only.md)
- [Core boundary](architecture/core-boundary.md)
- [Frontend-neutral migration status](frontend-neutral-migration.md)
- [API maintenance](api/maintenance.md)

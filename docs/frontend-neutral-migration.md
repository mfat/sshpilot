# Frontend-neutral migration status

This is the operational status of the reviewed ownership migration. A capability
is complete only when its daemon owner, typed API contract, frontend boundary,
and compatibility behavior are documented and tested.

## Completed and reviewed

### SSH keys

- **Owner:** `DaemonKeyService` over the GTK-free core key service.
- **Typed API:** `keys.read` and `keys.write`; `keys.list`, `keys.get_public`, and
  `keys.generate`.
- **GTK ownership removed:** no key-directory discovery, `ssh-keygen` execution,
  or private-key access in GTK; controllers retain public DTO state only.
- **Compatibility retained:** semantic key-store scopes, stable opaque key IDs,
  OpenSSH key generation, and explicit unsupported-capability behavior for
  clients without the daemon service.

### Known hosts

- **Owner:** `KnownHostsService` and the core lossless known-host document/file
  services.
- **Typed API:** `known_hosts.read` and `known_hosts.write`; revisioned list and
  batched remove methods.
- **GTK ownership removed:** GTK stages entry IDs and presents conflicts; it does
  not resolve paths, parse files, or write known-hosts files.
- **Compatibility retained:** exact document preservation, atomic writes,
  optimistic revision checks, stable entry IDs, and structured stale-editor
  errors.

### Connections, groups, and metadata

- **Owner:** `ConnectionRepository` through `ConnectionApplicationService`.
- **Typed API:** `connections.read`, `connections.write`,
  `connections.events`, `connections.metadata.write`, `connections.groups`,
  and `connections.split` as applicable to each method.
- **GTK ownership removed:** GTK no longer owns the saved store, groups,
  ordering, metadata persistence, or connection-store events.
- **Compatibility retained:** SSH `Host` aliases remain identity; full store
  snapshots and derived lifecycle events retain their documented ordering;
  metadata merges and removal semantics remain unchanged; secret-like metadata
  is rejected.

### Global SSH overrides

- **Owner:** `SshOverridesService`.
- **Typed API:** `ssh_overrides.read` and `ssh_overrides.write` through get,
  update, and reset methods.
- **GTK ownership removed:** Preferences no longer composes or persists a
  competing global override list; it presents values and submits typed requests.
- **Compatibility retained:** canonical defaults, normalization, deterministic
  revisions, optimistic concurrency, malformed-file protection, atomic writes,
  migration backups, and transaction locking.

### Secret backend management and secret-bearing backup transfer

- **Owner:** `SecretBackendService`, `DaemonConnectionSecretProvider`, and the
  existing `SecretManager`/backup implementation stack inside the daemon.
- **Typed API:** `secrets.read`, `secrets.write`, `secrets.operate`, and
  `secrets.transfer`, including backend configuration/lifecycle and backup
  preview/export/import methods.
- **GTK ownership removed:** Preferences, Bitwarden setup, rbw setup, KDBX
  lifecycle, `bw`/`rbw` execution, secret selection, decrypted credential
  enumeration, and secret-bearing backup construction no longer run in GTK.
- **Compatibility retained:** explicit backend selection remains exclusive;
  `auto` retains cross-backend compatibility reads/deletes; Bitwarden supports
  password/API-key/SSO/2FA/challenge flows; rbw keeps native pinentry; KDBX
  remembered-password identities and the existing `.spbk` format/merge
  behavior remain in place.

### Shared long-running operation infrastructure

- **Owner:** daemon `OperationRuntime`; services register work and do not own a
  second operation registry or terminal state machine.
- **Typed API:** existing operation summaries, `operation.created` and
  `operation.state_changed` events, and `operations.get`/`operations.cancel`
  transport paths using the existing capability contract.
- **Frontend ownership removed:** operation lifecycle, safe progress, failure
  metadata, event publication, cancellation hooks, bounded terminal retention,
  and bounded shutdown remain daemon-owned; clients render immutable snapshots.
- **Compatibility retained:** existing operation IDs, kinds, states, typed
  failure envelope, native producer behavior, in-memory/non-resumable scope,
  and ordinary event queue overflow semantics remain intact.

## Implemented, pending phase review

These implementations are present on `dev` but are not declared complete here:

- identity state and provider services, including identity operation producers;
- authorized-key management and native `ssh-copy-id` deployment as an identity
  feature phase.

Each requires its own phase review before the frontend-neutral migration status
changes to completed.

## Planned

- Authorized-key management and native `ssh-copy-id` deployment.
- SCP path-to-path operations using native `scp`.
- Browser transfer streaming through the existing SFTP infrastructure.
- Broadcast and remote-command execution through native `ssh`.
- Final frontend-boundary closure and a functional CLI/reference client.

## Native-first rules

Before implementing an SSH feature, check whether OpenSSH already supplies it.
SSH Pilot should supervise and expose native operations rather than reimplement
their protocols or semantics:

| Need | Native operation |
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

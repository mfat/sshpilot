# Frontend-neutral migration status

> **Historical completion record.** The frontend-neutral migration and final
> frontend closure are complete. This document preserves the reviewed
> migration status and intermediate evidence; it is not an active roadmap.
> Current architecture is documented in
> [`docs/architecture.md`](../../architecture.md), and final ownership evidence
> is in [`docs/architecture/frontend-closure-audit.md`](../../architecture/frontend-closure-audit.md).

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

### Identity providers and authorized-key management

- **Owner:** `IdentityStateService` for provider selection/environment state and
  `DaemonIdentityService` for agent inspection/mutation, effective identity
  resolution, authorized-key operations, authentication preparation, and native
  key deployment. `SftpServiceRuntime` owns the generic remote/local file
  session and replacement path used by the full authorized-key editor.
- **Typed API:** `identity.read`, `identity.write`, and `identity.operate` for
  provider/state, agent, deployment, and authorized-key methods; existing SFTP
  capabilities now also expose bounded file-content reads and revision-safe
  atomic replacement for the editor.
- **GTK ownership removed:** GTK no longer invokes `ssh-add`, `ssh-copy-id`,
  `ssh-keygen`, `ssh`, or SFTP backends for these workflows; it presents daemon
  DTOs, stages authorized-key documents, confirms mutations, and renders typed
  operation/interactions. Local and remote authorized-key file I/O, backups,
  temporary files, atomic replacement, and permissions are daemon-owned.
- **Compatibility retained:** SSH `Host` aliases, native `ssh -G`, `ssh-add`,
  `ssh-keygen`, `ssh-copy-id`, and the existing OpenSSH SFTP client remain the
  implementation sources of truth. Legacy provider/file-manager adapters may
  remain for unrelated compatibility routes but are not selected by the normal
  daemon-backed identity UI. Unsupported capabilities never trigger a local
  fallback.
- **Security:** identity DTOs contain metadata and bounded public/document text
  only. Private keys, passphrases, passwords, askpass answers, secret records,
  agent protocol handles, and process environments stay in daemon-owned key,
  interaction, and secret-provider paths.

## Completed native SCP slice

- **Owner:** daemon `NativeScpBackend` behind the shared `TransferRuntime`.
- **Typed API:** bounded `start_scp_transfer`, shared transfer summaries/events,
  and the conditional `transfers.scp` capability.
- **GTK ownership removed:** SCP subprocesses, VTE execution, authentication
  environments, argv construction, legacy retry decisions, and local listing
  subprocesses are no longer production responsibilities of GTK.
- **Compatibility retained:** native `scp` is the normal path; one controlled
  `-O` retry is daemon-owned and only follows clear SFTP-subsystem-unavailable
  errors. Flatpak portal path/display separation remains frontend-owned.
- **Remaining:** general SFTP transfer ownership, browser fallback without SFTP,
  broadcast/remote-command execution, architecture governance, and final
  frontend closure remain planned.

## Planned

- General SFTP transfer ownership and browser fallback without SFTP, plus
  broadcast/remote-command execution through native OpenSSH.
- Architecture governance: contributor rules and CI guardrails that reject new
  UI-owned operational features, API bypasses, and unnecessary reimplementations.
- Frontend closure: remaining unrelated frontend backend debt, a reference CLI,
  and the final architecture/security audit.

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

# Credential manager

sshPilot’s credential model represents SSH login passwords, sudo passwords, key
passphrases, and their canonical host identities. In production daemon mode,
connection and runtime secret access goes through daemon-owned providers and
protected interactions. The frontend never enumerates decrypted credentials or
calls a backend directly.

The existing credential and secret implementations remain internal building
blocks reused by the daemon. `CredentialManager` is a GTK-free normalization
layer for daemon backup/export work and compatibility adapters; it is not the
authority for connect-time storage, lookup, backend selection, or vault
lifecycle.

## Layers

| Layer | Module | Role |
| --- | --- | --- |
| Storage | `secret_storage.py` | Existing `SecretManager`, backends, and `SecretSpec` builders reused by the daemon |
| Model | `credential_model.py` | `Credential` dataclass, spec-to-credential translation, host-key helpers, and identity terminology |
| Orchestrator | `credential_manager.py` | Internal credential enumeration and normalization for daemon backup/export operations |
| Adapters | `credential_adapters.py` | Existing `SecretBackendAdapter` and `KdbxAdapter` credential-centric import/export helpers |
| Runtime provider | `DaemonConnectionSecretProvider` | Daemon-owned connection secret lookup and protected interaction integration |
| Backup services | `BackupManager`, `backup_archive`, `backup_backends` | Existing `.spbk` collection, merge, and restore behavior reused inside the daemon |

See [IDENTITY_PROVIDERS.md](IDENTITY_PROVIDERS.md) for the parallel identity
side: which key or agent authenticates a connection. Identity providers and
secret values remain separate concepts.

## Connect-time password keys

SSH login passwords use the existing canonical identity terminology. The
canonical host is always `hostname` → `host` → `nickname`, matching
`Connection.get_effective_host()`, and the backend key remains `user@host`.

The daemon-owned connection secret provider preserves the existing behavior:

- store under the canonical host;
- remove legacy alias copies when storing;
- probe legacy aliases on lookup and migrate a successful match to the
  canonical key;
- retain low-level exact host/user keys for callers such as plugin or sudo
  credentials where that identity is already known.

These operations are reached through typed client methods and daemon services.
They are not implemented by GTK-owned manager instances, and normal API
responses never contain secret values.

## CredentialManager

`CredentialManager` performs the existing normalized, read-only enumeration
needed by daemon backup/export operations:

- connection-derived entries include login passwords, sudo passwords, and key
  passphrases for configured and effective identity files;
- optional orphan enumeration uses backend adapters that support it;
- connection-derived entries take precedence over enumerated orphans using the
  existing `(id, type)` deduplication rule;
- it never prompts, and a locked session-backed vault contributes nothing.

The daemon invokes this internal layer while preparing a backup. GTK receives
only safe metadata such as paths, counts, warnings, manifests without secret
values, and completion state. Frontend code must not construct
`CredentialManager`, call `SecretManager`, enumerate decrypted credentials, or
assemble backup archives.

## Backup and restore

Secret-bearing `.spbk` export, preview, listing, and import execute inside the
daemon through `SecretBackendService`. The daemon reuses:

- `BackupManager` for collection and merge behavior;
- `CredentialManager` for normalized credential enumeration;
- `backup_archive` and `backup_backends` for the existing format and backend
  behavior.

Restore planning and application remain daemon-owned. Backup passphrases are
collected through protected one-use interactions. Decrypted manifests are
short-lived, process-local, and consumed once; they never cross the ordinary
public API.

## Backend selection compatibility

The daemon owns backend selection and lifecycle through `SecretBackendService`.
Explicit selection remains exclusive: normal store, lookup, and delete
operations consult only the selected backend. `auto` preserves the existing
compatibility behavior by consulting available backends for reads and deletes,
so changing selection does not orphan secrets. The existing backend
implementations were reused rather than rewritten.

Session-backed Bitwarden, rbw, and KDBX lifecycle operations are daemon-owned.
Bitwarden password, API-key, SSO, two-factor, and authentication-challenge
flows use protected interactions; rbw retains native agent/pinentry behavior;
KDBX create, unlock, and lock remain daemon operations. Remembered master
passwords use the existing platform-keyring identities.

## Plugin secrets

Plugin tokens retain their existing identity and storage semantics, but runtime
access is mediated by daemon-owned services. They are not included in ordinary
public DTOs and are not exposed through `CredentialManager` unless an explicit
supported backup operation includes them.

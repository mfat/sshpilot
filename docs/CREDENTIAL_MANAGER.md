# Credential manager

sshPilot stores SSH login passwords, sudo passwords, and key passphrases through
the pluggable backends in `secret_storage.py`. The **credential manager** is a
GTK-free normalization layer on top of that storage — it does not replace
`SecretManager` for connect-time store/lookup.

Use it when you need a **flat list of credentials** (backup export, future
vault-to-vault migration), not when opening a connection.

## Layers

| Layer | Module | Role |
| --- | --- | --- |
| Storage | `secret_storage.py` | `SecretManager`, backends, `SecretSpec` builders |
| Model | `credential_model.py` | `Credential` dataclass, spec ↔ credential translation, host-key helpers |
| Orchestrator | `credential_manager.py` | `CredentialManager.list_credentials()` |
| Adapters | `credential_adapters.py` | `SecretBackendAdapter`, `KdbxAdapter` — credential-centric load/save/delete |
| Connect-time API | `connection_manager.py` | `store_connection_password`, `get_connection_password`, … |

See also `IDENTITY_PROVIDERS.md` for the parallel **identity** side (which key /
agent authenticates a connection).

## Connect-time password keys

SSH login passwords are keyed in the backend by `user@host`. The **canonical host**
is always `hostname` → `host` → `nickname` (same as `Connection.get_effective_host()`).

- **Store:** `ConnectionManager.store_connection_password(connection, password)`
  writes under the canonical host and deletes legacy copies stored under older
  aliases.
- **Lookup:** `ConnectionManager.get_connection_password(connection)` probes
  legacy aliases; on hit it **migrates** the secret to the canonical key.
- **Low-level:** `store_password(host, user)` / `get_password(host, user)` remain
  for callers that already know the exact key (plugin secrets, sudo passwords, …).

Host helpers live in `credential_model.py`:

- `canonical_password_host(conn)`
- `password_host_candidates(conn)`

## CredentialManager

```python
from sshpilot.credential_manager import CredentialManager

creds = CredentialManager(connection_manager).list_credentials(
    include_orphans=True,   # default: also enumerate backend orphans
)
```

**Pass 1 — connection-derived:** for each connection, resolve password, sudo
password, and passphrases for `keyfile`, `identity_files`, and
`resolved_identity_files` (from `ssh -G`). Uses `SecretManager.lookup_everywhere`
so secrets are found even if the user switched backends since storing.

**Pass 2 — enumeration** (`include_orphans=True`): backends that implement
`iter_credentials` (libsecret, pass, bitwarden, keepassxc) surface stored secrets
with no matching connection; tagged `metadata['orphan']=True`.

Dedup key: `(id, type)`. Connection-derived entries win over enumerated orphans.

**Never prompts.** A locked session vault contributes nothing.

### Backup (`.spbk`)

`BackupManager._gather_credentials` calls
`list_credentials(include_orphans=False)` for the selected connections only.
Restore uses `credential_to_spec` + `SecretManager.store` (current backend).

## SecretManager public API

- `store` / `lookup` / `delete` — honor backend selection (`auto` vs explicit).
- `lookup_everywhere(spec)` — scan all available backends; used by export.
- `all_available_backends()` — public list for enumeration (credential manager).

## Adapters

`SecretBackendAdapter` wraps any `SecretBackend` with `load_all` / `save` /
`delete` on `Credential` objects. Enumeration requires `iter_credentials` on the
backend (keyring/agent have none).

`KdbxAdapter` is a **standalone** `.kdbx` import/export target (not the
connect-time `KdbxBackend`). Both scope enumeration to the dedicated `sshPilot`
KeePass group.

`watch_changes()` is currently a no-op on all adapters (future work).

## Plugin secrets

Plugin tokens use `connection_manager.store_plugin_secret` under host
`sshpilot-plugin/<plugin_id>`. They are **not** included in `CredentialManager`
today.

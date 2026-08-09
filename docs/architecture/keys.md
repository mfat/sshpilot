# SSH-key daemon ownership (M1 — Complete)

M1 moved SSH-key discovery, generation, public-key reads, and passphrase
verification into the daemon.
GTK no longer instantiates `core.keys.KeyService`, never scans key
directories, never runs `ssh-keygen`, and never directly reads the `.pub`
file of a daemon-discovered key.

## Ownership model

- The **daemon** resolves the active key directory per semantic scope:
  - `KeyStoreScope.DEFAULT` → `get_ssh_dir()`
  - `KeyStoreScope.ISOLATED` → `get_config_dir()`
  The frontend sends only the semantic scope — never a key-directory path.
- The **daemon** creates key directories, recursively discovers private keys,
  runs `ssh-keygen`, and reads public-key files for application features.
- **GTK** renders key metadata, reads public-key text via `keys.get_public`,
  requests generation via `keys.generate`, and requests passphrase verification
  via `keys.verify_passphrase` — through `KeyManager`, a GObject compatibility
  adapter over the daemon-backed `SshPilotClient` (via the GTK-free
  `KeyController`).

## Identity and secrets

- Keys are addressed by **deterministic opaque IDs** (scope + relative POSIX
  path, SHA-256 truncated) — never UUIDs, never absolute paths.
- Private-key contents are never serialized and never cross the API.
- `GenerateKeyRequest` expresses only whether encryption is requested. Neither
  it nor `VerifyKeyPassphraseRequest` contains a passphrase.
- Generation and verification collect passphrases through the existing
  `InteractionBroker` and protected secret-frame channel. Native `ssh-keygen`
  prompting is answered by daemon askpass; non-empty values never appear after
  `-N`/`-P`, in process environment values, or in temporary files.
- Saving a verified key passphrase also uses a protected interaction;
  `StoreKeyPassphraseRequest` carries only the key path and opaque scope.
- The broker retains a generation secret only long enough to answer native
  confirmation and clears it on success, failure, cancellation, timeout, or
  shutdown. GTK clears its password widget/input buffer after submission.
- Public-key text crosses the API only through `keys.get_public`.

## Capabilities and RPCs

- `KEYS_READ` (`keys.list`, `keys.get_public`) and `KEYS_WRITE`
  (`keys.generate`, `keys.verify_passphrase`) are advertised only when the
  daemon key service is installed.
- Generation is unavailable while the daemon is draining; a transport-level
  timeout after send becomes a structured `MUTATION_AMBIGUOUS` error, and the
  UI reloads the key list without an automatic retry.

## Frontend behavior

- `KeyManager` performs no local fallback; when the daemon is unavailable the
  key actions show the daemon-recovery message.
- `ssh-copy-id` existing-key loading and generation run off the GTK thread,
  with repeated clicks rejected and callbacks ignored after window close.
- The authorized-keys local import lists keys through the daemon and reads the
  selected public key via `read_public_key()`; no direct `.pub` read for
  daemon-discovered inventory.
- Path metadata on `KeySummary` (`private_path` / `public_path`) is temporary
  compatibility data for the M7 `ssh-copy-id` subprocess adapter. GTK does not
  derive or scan these paths. User-browsed arbitrary public-key files remain
  explicit frontend input and may keep their existing read path.

## No deletion API

No delete RPC or capability was added because no current GTK key-deletion
workflow exists. `plugins.host.delete_key` returns `False` until deletion is
daemon-owned in a later migration.

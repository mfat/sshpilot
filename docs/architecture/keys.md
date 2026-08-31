# SSH keys

The daemon owns SSH-key discovery, generation, public-key reads, passphrase
verification, and deletion through the typed API.
GTK no longer instantiates `core.keys.KeyService`, never scans key
directories, never runs `ssh-keygen`, and never directly reads the `.pub`
file of a daemon-discovered key.

## Ownership model

- The **daemon** resolves the active key directory per semantic scope:
  - `KeyStoreScope.DEFAULT` → `get_ssh_dir()`
  - `KeyStoreScope.ISOLATED` → `get_config_dir()`
  The frontend sends only the semantic scope — never a key-directory path.
- A scope names **one** directory, and a key id only resolves inside its own
  scope's root. In Isolated Mode the frontend therefore lists *both* stores and
  records which scope each key came from, so a later read, delete, or deploy
  names the store the key actually lives in. Isolated Mode isolates SSH
  *configuration*, not credentials: an isolated connection can name
  `~/.ssh/id_ed25519` as its `IdentityFile` and OpenSSH uses it, so hiding
  those keys only stopped the user selecting keys their connections were
  already using. New keys are still generated into the active scope, and
  Default Mode never lists sshPilot's private key directory.
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
- Path metadata on `KeySummary` (`private_path` / `public_path`) is compatibility
  metadata returned by the daemon for operations that need to identify a key.
  GTK does not derive or scan daemon key paths. User-browsed arbitrary
  public-key files remain explicit frontend input and may keep their existing
  read path.

## Deletion

`keys.delete` is daemon-owned and uses the opaque key identity established by
the daemon key inventory. Plugin compatibility calls match legacy private-path
arguments against daemon-listed metadata and then delete by opaque `KeyId`; no
frontend file operation is involved.

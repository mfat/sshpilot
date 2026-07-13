# Identity Providers

SSHPilot abstracts **how an SSH identity (key / agent) is supplied** behind a
small `IdentityProvider` interface, the identity-side parallel of the credential
backends in `secret_storage.py`.

- **Credential backends** (`secret_storage.py`) answer *"what password/passphrase
  do we use?"* See `docs/CREDENTIAL_MANAGER.md` for the export/backup layer
  (`credential_manager.py`) and canonical SSH password host keys.
- **Identity providers** (`identity.py`) answer *"which SSH key or agent
  authenticates the connection, and what does the spawned process need in its
  environment for that to work?"*

Separating the two means the choices compose as plain configuration — passwords
in libsecret with keys from the system ssh-agent, or passwords in Bitwarden with
a key read from `~/.ssh/id_ed25519`, etc.

## Where things live

| Piece | Location |
| --- | --- |
| `Identity` dataclass, `IdentityProvider` ABC, `IdentityManager`, `get_identity_manager()` | `sshpilot/identity.py` |
| `SystemAgentProvider` | `sshpilot/providers/system_agent.py` |
| `FileKeyProvider` | `sshpilot/providers/file_key.py` |
| Plugin access (`ctx.identities`) | `sshpilot/plugins/api.py` (see `PLUGIN_SDK.md`) |

## The contract

```python
@dataclass
class Identity:
    id: str               # stable within a provider (agent fingerprint, key realpath, …)
    display_name: str     # human label (key comment, basename, …)
    fingerprint: str | None
    provider_name: str    # the producing provider's name

class IdentityProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...                       # stable, lowercase id

    @abstractmethod
    def list_identities(self) -> list[Identity]: ...  # may be empty

    @abstractmethod
    def apply_to_env(self, env: dict) -> dict: ...     # return a COPY, never mutate

    def ssh_config_directives(self) -> list[tuple[str, str]]:  # default: []
        ...                                            # (keyword, value) for Host *

    @abstractmethod
    def is_available(self) -> bool: ...                # readiness, not a hard error
```

### Rules every provider must follow

1. **`apply_to_env` returns a modified copy, for *environmental* values only.** Never
   mutate the argument in place. Inject only genuinely environmental things — chiefly the
   running agent's `SSH_AUTH_SOCK` for the OS/desktop agent (a volatile per-session
   socket). **Per-host identity that ssh config can express — key files, certificates, a
   fixed agent socket, PKCS#11 — does NOT go here.** ssh config is the source of truth
   (`CLAUDE.md`): express it via `ssh_config_directives()` (written to a managed `Host *`
   block) or, per connection, the connection editor's IdentityFile/IdentityAgent fields.
2. **Safe to instantiate when the dependency is missing.** The constructor must
   not raise because an agent is down or a key file is absent — report that via
   `is_available()` returning `False`.
3. **`is_available()` is cheap and side-effect free.** No network calls, no
   prompts, no unlocking. It may be called frequently (e.g. before each spawn).
4. **Never hardcode a credential store.** Passphrase/secret lookups must go
   through the credential backend interface (`askpass_utils.lookup_passphrase`,
   which delegates to `secret_storage.SecretManager`) — not direct libsecret /
   keyring calls. This is what keeps "keys here, passwords there" working.
5. **`list_identities()` must not throw.** Return `[]` on failure; the manager
   logs and continues so one provider can't break aggregation.
6. **`id` is stable within the provider** so callers can refer to an identity
   across listings.

> **Cost note:** unlike `is_available()`, `list_identities()` is *not* guaranteed
> cheap — it may spawn a subprocess or hit a network/agent (e.g.
> `SystemAgentProvider` runs `ssh-add -l`). Call it when you actually need the
> list (UI refresh, plugin query), not on every spawn, and don't treat it as a
> readiness probe — that's what `is_available()` is for.

## The two built-in providers

### `SystemAgentProvider` (`name = "system-agent"`)
Wraps the long-standing behaviour of inheriting `SSH_AUTH_SOCK` from the
environment.

- `is_available()` — true when `SSH_AUTH_SOCK` is set.
- `apply_to_env()` — copies `SSH_AUTH_SOCK` / `SSH_AGENT_PID` from the current
  process into the returned env.
- `list_identities()` — parses `ssh-add -l`.

It does **not** start an agent (`connection_manager._ensure_ssh_agent` does) or
add keys (`askpass_utils.ensure_key_in_agent` does).

### `FileKeyProvider` (`name = "file-key"`)
A single private key on disk (e.g. `~/.ssh/id_ed25519`).

- `is_available()` — true when the key file exists.
- `apply_to_env()` — **no-op.** A key is expressed as `IdentityFile` in
  `~/.ssh/config` (the source of truth — see `CLAUDE.md`); ssh reads no env var for a
  key path, so there is nothing to inject.
- `list_identities()` — one `Identity`; fingerprint from the sibling `.pub`.
- `unlock(lifetime=0)` / `has_stored_passphrase()` — passphrase comes from the
  **credential backend** via the shared askpass path, never libsecret directly.

### `SocketAgentProvider` (e.g. `name = "onepassword"`, `"custom"`)
A fixed-socket ssh-agent (the 1Password agent at `~/.1password/agent.sock`, or a
user-supplied socket), parameterised by `(name, display_name, socket_path)`.

- `is_available()` — true when the socket file exists.
- `apply_to_env()` — **no-op** (config-driven, not env-driven).
- `ssh_config_directives()` — `[("IdentityAgent", socket_path)]`. The selection writes
  this into a managed `Host *` block in `~/.ssh/config` (see below), which ssh honours
  for every invocation and which overrides any inherited `SSH_AUTH_SOCK`.

## Writing a new provider (e.g. Bitwarden Agent, PKCS#11)

1. Add `sshpilot/providers/<your_provider>.py` implementing `IdentityProvider`.
2. Honour the six rules above — especially: return a copy from `apply_to_env`,
   keep `is_available()` cheap, and route any secret lookups through the
   credential backend.
3. Register it (`get_identity_manager().register(YourProvider(...))`) where the
   provider becomes relevant. Once registered, it appears in **Preferences ▸ SSH
   Identity ▸ Identity provider** and can be chosen as the default.
4. Add tests under `tests/` mirroring `tests/test_identity.py` (use fakes /
   monkeypatched subprocess — do not require the real agent/CLI in CI).

### Default-provider selection

`IdentityManager` tracks a *selected* default agent (config `identity.provider`,
propagated as `SSHPILOT_IDENTITY_PROVIDER`; `'auto'` = system ssh-agent). Selection is
surfaced in **Preferences ▸ Security & Credentials ▸ Default SSH agent**
(Automatic / 1Password / Custom socket…).

Two seams carry a selected provider into a connection, by nature of the value:

- **ssh config (source of truth) — fixed-socket agents.** When the selected provider
  returns `ssh_config_directives()` (e.g. 1Password → `IdentityAgent ~/.1password/agent.sock`),
  `connection_manager.apply_global_identity_agent()` writes a **sentinel-delimited managed
  `Host *` block** at the end of `~/.ssh/config` (atomic write + `.bak`, never touching
  other user content). It is idempotent: re-selecting updates it, switching to Automatic
  removes it. End-of-file placement means a per-connection `IdentityAgent` still wins.
- **Environment — the OS/desktop agent.** `get_identity_manager().apply_selected_to_env(env)`
  in `terminal.py` injects `SSH_AUTH_SOCK` for the system agent (Automatic). Its socket is
  a volatile per-session path, so there is nothing to persist to config.

`'auto'`/unknown resolve to the system agent (never silently disabled). The per-connection
key stays the connection's `IdentityFile`; this selection is only the global default.

Sketches (not yet implemented):

- **gpg-agent / KeePassXC** — more `SocketAgentProvider` presets once their socket paths
  are discovered (distro-variable); until then the **Custom socket** field covers them.
- **PKCS#11 / hardware token** — a provider returning
  `[("PKCS11Provider", "/path/to/module.so")]` from `ssh_config_directives()`, written to
  the managed block via the same seam (no env/CLI).

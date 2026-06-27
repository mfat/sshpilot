# Identity Providers

SSHPilot abstracts **how an SSH identity (key / agent) is supplied** behind a
small `IdentityProvider` interface, the identity-side parallel of the credential
backends in `secret_storage.py`.

- **Credential backends** (`secret_storage.py`) answer *"what password/passphrase
  do we use?"*
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

    @abstractmethod
    def is_available(self) -> bool: ...                # readiness, not a hard error
```

### Rules every provider must follow

1. **`apply_to_env` returns a modified copy.** Never mutate the argument in place.
   Inject only what the provider needs (e.g. `SSH_AUTH_SOCK`, `SSH_IDENTITY_FILE`).
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
- `apply_to_env()` — sets the `SSH_IDENTITY_FILE` convention variable. (The
  canonical ssh mechanism remains `IdentityFile` in `~/.ssh/config`; the provider
  does not append CLI flags — see `CLAUDE.md`.)
- `list_identities()` — one `Identity`; fingerprint from the sibling `.pub`.
- `unlock(lifetime=0)` / `has_stored_passphrase()` — passphrase comes from the
  **credential backend** via the shared askpass path, never libsecret directly.

## Writing a new provider (e.g. Bitwarden Agent, PKCS#11)

1. Add `sshpilot/providers/<your_provider>.py` implementing `IdentityProvider`.
2. Honour the six rules above — especially: return a copy from `apply_to_env`,
   keep `is_available()` cheap, and route any secret lookups through the
   credential backend.
3. Register it (`get_identity_manager().register(YourProvider(...))`) where the
   provider becomes relevant. Provider *selection* UI is a separate task.
4. Add tests under `tests/` mirroring `tests/test_identity.py` (use fakes /
   monkeypatched subprocess — do not require the real agent/CLI in CI).

Sketches (not yet implemented):

- **Bitwarden agent** — `is_available()` checks the bw SSH-agent socket;
  `apply_to_env()` injects that socket as `SSH_AUTH_SOCK`; `list_identities()`
  enumerates the vault's SSH keys. Unlock state reuses the session-backed
  credential backend.
- **PKCS#11** — `apply_to_env()` arranges for the module/token to be used (e.g.
  via `PKCS11Provider` config in `~/.ssh/config`); `list_identities()` lists token
  slots/objects.

## My Branch Naming Preference
- Always use: `feature/[description]` or `fix/[description]`
- Description must reflect the actual task — no jokes, puns, or random names

## How the app connects and authenticates (read before touching this)
The full reference is in `AGENTS.md` → **SSH Connection & Authentication
Architecture**. The essentials:

- **MUST reuse the single connection/auth path — do not add new methods.** Any
  feature that connects, runs a remote command, copies a key, or transfers a
  file uses the existing entry points: `Connection.native_connect()` →
  `build_ssh_connection()` to connect, and `resolve_native_auth()` for auth
  (askpass/keyring or sshpass). For external-process commands use
  `build_native_command()`; for explicit raw-host commands (SCP) use
  `_build_base_ssh_command()` + `resolve_native_auth()`. Do **not** hand-roll a
  new `ssh`/`scp` command builder or a new auth env anywhere. If an existing
  function almost fits, extend it; if you think a new path is truly needed,
  confirm with the user first. (The paramiko SFTP file manager is the only
  exception — it doesn't use the ssh command path.)

- **One connection method, native-only.** Every in-app SSH connection goes
  through `Connection.native_connect()` → `build_ssh_connection(ctx)`
  (`ssh_connection_builder.py`). `Connection.connect()` just delegates to
  `native_connect()`. There is no non-native/legacy path and no native toggle —
  don't reintroduce them.
- **`~/.ssh/config` is the source of truth.** The command is minimal:
  `ssh -F <config> [ssh_overrides…] <host> [remote-cmd]`.
  Per-host settings (IdentityFile, port, forwardings, ProxyJump, X11,
  CertificateFile, RemoteCommand, …) are written to `~/.ssh/config`, not put on
  the command line. Add new per-connection SSH settings by persisting them to
  the config, not by appending CLI flags.
- **One auth resolver:** `resolve_native_auth(...)` in `ssh_connection_builder.py`
  is the only place auth is decided, shared by terminal, SCP, and ssh-copy-id.
  Key auth → `SSH_ASKPASS` (REQUIRE=prefer) + keyring autofill (GTK prompt
  fallback); the agent is left intact so SSH uses it when keys are loaded.
  Password → `sshpass` via a write-once FIFO. Keyring autofill and the askpass
  prompt are advertised features — keep them.
- **Callers:** the terminal consumes the prepared command (it does not build
  commands); SCP/ssh-copy-id build explicit commands + `resolve_native_auth`;
  the system/external terminal uses `build_native_command()` (plain, no in-app
  auth); the SFTP file manager uses paramiko in-process (separate — leave it
  alone unless the task is about it).
- **Advanced SSH options** (Preferences ▸ SSH Settings) are saved as `ssh.*`
  keys and composed into a flat `ssh.ssh_overrides` list
  (`preferences.py::save_advanced_ssh_settings`); the native command appends
  `ssh_overrides` verbatim, while `_build_base_ssh_command` (SCP) reads the
  individual keys. **Effective config** is computed with `ssh -G` via
  `get_effective_ssh_config()` when code needs the resolved per-host options.
  **askpass** = `SSH_ASKPASS`/`SSH_ASKPASS_REQUIRE` + the helper in
  `askpass_utils.py` (keyring lookup → GTK prompt); **sshpass** feeds the
  password via a write-once FIFO (`ssh_password_exec.py`). Full detail in
  `AGENTS.md`.
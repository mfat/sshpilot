## My Branch Naming Preference
- Always use: `feature/[description]` or `fix/[description]`
- Description must reflect the actual task — no jokes, puns, or random names

## How the app connects and authenticates (read before touching this)
The full reference is in `AGENTS.md` → **SSH Connection & Authentication
Architecture**. The essentials:

- **One connection method, native-only.** Every in-app SSH connection goes
  through `Connection.native_connect()` → `build_ssh_connection(ctx)`
  (`ssh_connection_builder.py`). `Connection.connect()` just delegates to
  `native_connect()`. There is no non-native/legacy path and no native toggle —
  don't reintroduce them.
- **`~/.ssh/config` is the source of truth.** The command is minimal:
  `ssh -F <config> [ssh_overrides…] [-o IdentityAgent=none] <host> [remote-cmd]`.
  Per-host settings (IdentityFile, port, forwardings, ProxyJump, X11,
  CertificateFile, RemoteCommand, …) are written to `~/.ssh/config`, not put on
  the command line. Add new per-connection SSH settings by persisting them to
  the config, not by appending CLI flags.
- **One auth resolver:** `resolve_native_auth(...)` in `ssh_connection_builder.py`
  is the only place auth is decided, shared by terminal, SCP, and ssh-copy-id.
  Key auth → `SSH_ASKPASS` + keyring autofill (GTK prompt fallback) + agent
  bypass (`-o IdentityAgent=none`, drop `SSH_AUTH_SOCK`) unless ForwardAgent or
  an explicit IdentityAgent is set. Password → `sshpass` via a write-once FIFO.
  Keyring autofill and the askpass prompt are advertised features — keep them.
- **Callers:** the terminal consumes the prepared command (it does not build
  commands); SCP/ssh-copy-id build explicit commands + `resolve_native_auth`;
  the system/external terminal uses `build_native_command()` (plain, no in-app
  auth); the SFTP file manager uses paramiko in-process (separate — leave it
  alone unless the task is about it).
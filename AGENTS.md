# AGENTS.md

## Project Overview

sshPilot is a user-friendly, modern SSH connection manager with an integrated terminal for Linux, macOS, and Windows. It's built with Python, GTK4, and libadwaita, providing a native desktop experience.

### Documentation Resources
- A full catalog of package functions and methods is available in `documentation/function-reference.md`. Update this document with `python3 scripts/generate_function_reference.py` when APIs change.

## SSH Connection & Authentication Architecture

This is the single most important subsystem to understand before changing how
the app connects. **There is one connection method and one auth path — do not
reintroduce alternatives.**

### Native mode is the only mode — `~/.ssh/config` is the source of truth
- Every in-app SSH connection goes through `Connection.native_connect()`
  (`connection_manager.py`), which calls `build_ssh_connection(ctx)`
  (`ssh_connection_builder.py`). `Connection.connect()` is a thin alias that
  delegates to `native_connect()`. There is **no** non-native/legacy command
  path and no native-mode toggle.
- The command is intentionally minimal:
  `ssh -F <config> [ssh_overrides…] [-o IdentityAgent=none] <host> [remote-cmd]`.
- Per-host settings are **not** placed on the command line. sshPilot writes
  `IdentityFile`, `Port`, `LocalForward`/`RemoteForward`/`DynamicForward`,
  `ProxyJump`, `ProxyCommand`, `ForwardX11`, `CertificateFile`, `RemoteCommand`,
  etc. into `~/.ssh/config` (see the config writer in `connection_manager.py`),
  and `ssh -F <config> <host>` reads them. If you add a per-connection SSH
  setting, persist it to the config — do **not** append it to the command.
- App-global options (e.g. `ConnectTimeout`, `ServerAliveInterval`) come from
  the app config as `ssh_overrides` and are appended verbatim.
- Isolated mode: `connection._resolve_config_override_path()` returns the
  isolated config file, which becomes the `-F <file>` argument.

### Authentication is resolved in exactly one place
`resolve_native_auth(connection, connection_manager, app_config)` →
`NativeAuth(env, extra_opts, use_sshpass, password, use_askpass, password_mode)`.
The terminal builder, SCP, and ssh-copy-id all call it so they authenticate
identically. Modes:
- **Password** (`auth_method == 1`, or a stored password exists): use `sshpass`
  with a write-once FIFO to feed the password; clear `SSH_ASKPASS` and set
  `SSH_ASKPASS_REQUIRE=never` so ssh never falls back to askpass.
- **Key-based** (default, askpass enabled): set `SSH_ASKPASS` (REQUIRE=prefer)
  so ssh asks our askpass helper for the key passphrase. Also apply the **agent
  bypass** — add `-o IdentityAgent=none` and drop `SSH_AUTH_SOCK` — *unless* the
  connection forwards the agent (`ForwardAgent`) or pins an explicit
  `IdentityAgent`. The bypass exists because gnome-keyring advertises a locked
  key but refuses to sign it ("agent refused operation"), and ssh will not fall
  back to the on-disk key, so askpass would never fire.
- **Askpass disabled** (the `use-askpass` setting is off): set no `SSH_ASKPASS`;
  ssh prompts natively on the TTY.

Credentials are stored/retrieved via libsecret/keyring
(`connection_manager.get_password` / `askpass_utils.lookup_passphrase`). The
askpass helper (`askpass_utils.py`) is the program ssh invokes; it looks the
passphrase up in the keyring and, failing that, shows a GTK prompt. Keyring
autofill + the askpass prompt are advertised features — keep them working.

### Advanced SSH options (Preferences → command)
Preferences ▸ SSH Settings persists each advanced option under the `ssh.*`
namespace (`ssh.connection_timeout`, `ssh.connection_attempts`,
`ssh.keepalive_interval`, `ssh.keepalive_count_max`,
`ssh.strict_host_key_checking`, `ssh.batch_mode`, `ssh.compression`,
`ssh.verbosity`, `ssh.debug_enabled`) **and** composes them into one flat list,
`ssh.ssh_overrides`, in `preferences.py::save_advanced_ssh_settings` — e.g.
`['-o','ConnectTimeout=10','-o','ServerAliveInterval=30','-C','-v','-o','LogLevel=VERBOSE']`.
- The **native** builder appends `ssh.ssh_overrides` verbatim — this is how
  global Preferences options reach interactive connections.
- The **explicit** builder `_build_base_ssh_command` (SCP, etc.) instead reads
  the individual `ssh.*` keys via `Config.get_ssh_config()` and emits the
  equivalent `-o`/flags itself.
- When adding an advanced option, keep both paths in sync: add the `ssh.*`
  setting, include it in the `ssh_overrides` composition, and (if explicit
  callers need it) in `_build_base_ssh_command`.

### Effective config (`ssh -G`)
`ssh_config_utils.get_effective_ssh_config(host, config_file=None)` runs
`ssh -G <host>` (with `-F <config_file>` in isolated mode) and parses the
fully-resolved per-host options into a dict (lowercased keys; repeated keys such
as `identityfile` become lists). Use it when code needs to *know* what ssh will
actually use — resolving IdentityFile candidates, the connection editor, SCP's
explicit command. The interactive native command does **not** call this: it
stays minimal and lets the spawned `ssh` resolve the config itself at run time.

### askpass mechanics
- `get_ssh_env_with_askpass(require)` (`askpass_utils.py`) returns an env with
  `SSH_ASKPASS=<our helper>`, `SSH_ASKPASS_REQUIRE=<require>`, a `DISPLAY`
  fallback, and the `GNOME_KEYRING_*` control vars cleared (so gnome-keyring
  doesn't intercept) while keeping D-Bus available for libsecret.
- `require` is OpenSSH's `SSH_ASKPASS_REQUIRE`: `prefer` (default — use askpass
  even when a TTY exists, OpenSSH ≥ 8.4), `force`, or `never`.
- ssh invokes our helper (CLI entry `handle_askpass_cli`), which calls
  `lookup_passphrase(key_path)` → keyring; if nothing is stored it shows the
  built-in GTK passphrase dialog (`_run_askpass_dialog`). Helper output is
  streamed into the app log by the askpass log forwarder.
- The `use-askpass` setting (master) and `use-builtin-passphrase-prompt`
  (sub-option) gate this; with askpass off, ssh prompts natively on the TTY.

### sshpass mechanics
The password is fed to ssh via a **write-once FIFO**, never on the command line
or in the environment: `_mk_priv_dir()` creates a 0700 temp dir,
`_write_once_fifo()` (a daemon thread) writes the secret exactly once when ssh
opens the FIFO, and the command is prefixed with `sshpass -f <fifo>`
(`ssh_password_exec.py`; the terminal does the same inline in
`_setup_ssh_terminal`). `SSH_ASKPASS_REQUIRE=never` is set so ssh cannot divert
to askpass for a password.

### Who builds what
- **Interactive terminal** (`terminal.py::_setup_ssh_terminal`): consumes the
  prepared `connection.ssh_connection_cmd` (command + env + auth flags) — it does
  **not** build commands or derive auth. It only does runtime mechanics: the
  sshpass FIFO, askpass log forwarding, `TERM`/`PATH`, and the PTY/spawn.
- **SCP** (`scp_utils.py`): SCP runs against explicit params (raw host, explicit
  keyfile/port — not a config alias), so it builds an explicit `scp` command via
  `_build_base_ssh_command` + the shared `resolve_native_auth`.
- **ssh-copy-id** (`window.py`): builds its own `ssh-copy-id` argv and applies
  `resolve_native_auth` (its `-o` options must precede the target).
- **System / external terminal**: uses `build_native_command()` — a *plain*
  `ssh -F <config> <host>` with **no** in-app auth (`IdentityAgent`/askpass),
  because the external terminal supplies its own TTY and agent.
- **SFTP file manager** (`file_manager_window.py`): uses **paramiko in-process**,
  not the ssh command path. It is a separate subsystem — leave it alone unless
  the task is explicitly about it.

### Key functions/files
- `ssh_connection_builder.py`: `build_ssh_connection` (native-only),
  `resolve_native_auth` (the auth chokepoint), `build_native_command` (plain
  command for external processes), `_build_base_ssh_command` (shared option
  builder used by explicit-command callers like SCP).
- `connection_manager.py`: `Connection.native_connect()`/`connect()`,
  persistence of connections to `~/.ssh/config`, credential storage.
- `askpass_utils.py`: the askpass helper, keyring lookup, and GTK prompt.

When changing this subsystem: keep a **single** connection method and a
**single** auth resolver; prefer writing per-host settings to `~/.ssh/config`
over adding command-line flags.

## Setup Commands

- Install dependencies: `pip install -r requirements.txt`
- Run from source: `python3 run.py`
- Run with verbose debugging: `python3 run.py --verbose`
- Run tests: `pytest`
- Build PyInstaller bundle: `./pyinstaller.sh` (macOS)

## Development Environment

### System Dependencies
Install GTK4/libadwaita/VTE system packages 

**Debian/Ubuntu:**
```bash
sudo apt install python3-gi python3-gi-cairo libgtk-4-1 gir1.2-gtk-4.0 libadwaita-1-0 gir1.2-adw-1 libvte-2.91-gtk4-0 gir1.2-vte-3.91 libgtksourceview-5-0 gir1.2-gtksource-5 libsecret-1-0 gir1.2-secret-1 python3-paramiko python3-cryptography sshpass ssh-askpass gir1.2-webkit-6.0
```

**Fedora/RHEL/CentOS:**
```bash
sudo dnf install python3-gobject gtk4 libadwaita vte291-gtk4 gtksourceview5 libsecret python3-paramiko python3-cryptography sshpass openssh-askpass webkitgtk6
```

### Python Dependencies
- Python >= 3.8
- PyGObject >= 3.42
- pycairo >= 1.20.0
- paramiko >= 3.4
- cryptography >= 42.0
- libsecret (via PyGObject) for credential storage on Linux
- keyring >= 24.3
- psutil >= 5.9.0

## Code Style

- Follow [PEP 8](https://peps.python.org/pep-0008/) for Python code style
- Use type hints where appropriate
- Prefer GTK4/Adwaita components over custom widgets and follow GNOME HIG guidelines. Prefer modern Adwaita UI elements over traditional GTK
- Avoid using deprecated gtk3 methods
- All UI should be defined in code, not UI files

## Testing Instructions

- Run `pytest` to execute the test suite before committing changes
- Add or update tests when modifying code
- Verify keyboard shortcuts work on both platforms

## Platform-Specific Considerations

### macOS
- Use `is_macos()` to detect macOS platform
- Use `<Meta>` key for Command key shortcuts

### Linux
- Use `<primary>` key for Ctrl key shortcuts
- Prefer system package managers for GTK dependencies

## Security Guidelines

- Never store passwords in plain text
- Use `libsecret` (via PyGObject) on Linux for credential storage
- Use `keyring` for cross-platform credential management
- The app uses askpass for private key passphrases and sshpass for ssh passwords
## Build and Packaging


### Flatpak
- Use `io.github.mfat.sshpilot.yaml` manifest
- Never use shell commands to generate scripts in manifest
- Use `type: script` or include files directly

### macOS DMG
- Use version from `__init__.py` for DMG naming

## Git and Release Guidelines
- Project tags follow format `vX.Y.Z` (e.g., `v2.7.1`)

## Common Patterns

### SSH Configuration
- The project uses 2 operation modes: default (loads and saves ~/.ssh/config) and Isolated Mode which stores config in ~/.config/sshpilot
- Connections are native-only and `~/.ssh/config` is the source of truth. See
  **SSH Connection & Authentication Architecture** above for how connecting and
  auth work (one connection method, one auth resolver).

### Terminal Management
- Use VTE for terminal display (default backend)
- Supports PyXterm.js backend (requires WebKit 6.0 system package, Linux only)
  - Note: webkitgtk is Linux-only; PyXterm.js backend not available on macOS
- Supports both built-in terminal and external terminal options

## Debugging
- Use `--verbose` flag for detailed logging


## Memory and Preferences
- Do not add/remove features without user's confirmation
- User prefers no UI files; define all interfaces in code
- User requires explicit permission before modifying/deploying codebase
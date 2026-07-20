# AGENTS.md

## Project Overview

sshPilot is a user-friendly, modern SSH connection manager with an integrated terminal for Linux, macOS, and Windows. It's built with Python, GTK4, and libadwaita, providing a native desktop experience.

## SSH Connection & Authentication Architecture

This is the single most important subsystem to understand before changing how
the app connects. **There is one connection method and one auth path — do not
reintroduce alternatives.**

### MUST: reuse the single connection/auth path — do not add new methods
When any part of the app needs to make an SSH connection, run a remote command,
copy a key, or transfer a file, **reuse the existing entry points. Do NOT write
a new function that assembles its own `ssh`/`scp`/`ssh-copy-id` command line or
its own auth environment.** This unification was deliberate; parallel
command/auth builders are exactly what was removed.

Reuse these (and only these):
- **Open/prepare a connection:** `Connection.native_connect()` →
  `build_ssh_connection(ctx)`. (`Connection.connect()` is just an alias.)
- **Decide authentication (askpass for passphrases and login passwords):**
  `resolve_native_auth(connection, connection_manager, app_config)` — the ONLY
  place auth is decided. Every command-based caller must get its env + extra
  options from here. Do **not** reintroduce sshpass or PTY password autofill
  for SSH login secrets.
- **Build a plain command for an external process** (e.g. system terminal):
  `build_native_command(...)`.
- **Build an explicit command's option list** (raw host/keyfile/port callers
  like SCP): `_build_base_ssh_command(...)`, then layer `resolve_native_auth`.

Rules:
- If an existing function *almost* fits, **extend it** (add a parameter / handle
  the case) rather than cloning a variant. One builder, one auth resolver.
- Never hand-roll `SSH_ASKPASS`, `IdentityAgent`, or a parallel password feeder
  in a new place — call `resolve_native_auth`.
- **Never disable/bypass the ssh-agent.** Do not add `-o IdentityAgent=none` and
  do not drop `SSH_AUTH_SOCK` for key auth (a removed misfeature — see the
  Authentication modes below). The agent is always left intact; askpass is the
  passphrase *and* login-password autofill path.
- Never append per-host SSH settings to a command line — persist them to
  `~/.ssh/config` (see below) and let the native command pick them up.
- If you genuinely believe a new connection path is needed, stop and confirm
  with the user first — don't add one silently.

### Native mode is the only mode — `~/.ssh/config` is the source of truth
- Every in-app SSH connection goes through `Connection.native_connect()`
  (`connection_manager.py`), which calls `build_ssh_connection(ctx)`
  (`ssh_connection_builder.py`). `Connection.connect()` is a thin alias that
  delegates to `native_connect()`. There is **no** non-native/legacy command
  path and no native-mode toggle.
- The command is intentionally minimal:
  `ssh -F <config> [ssh_overrides…] <host> [remote-cmd]`.
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
identically.

**Askpass is used for both key passphrases and login passwords** on the native
path. `SSH_ASKPASS_REQUIRE=prefer` so the helper autofills stored secrets;
interactive MFA (OTP/PIN) is collected via an askpass/main-window dialog (OpenSSH
does not fall back to the TTY when askpass declines). `use_sshpass` is always
`False` here (do not reintroduce it).

Modes:
- **Password method** (`auth_method == 1`) with a stored password → askpass with
  password host/user context (`SSHPILOT_PASSWORD_*`) and optional one-shot
  in-memory session secret (IPC id, or a runtime-dir file when IPC is down).
- **Key-based** (`auth_method == 0`, auto or specific key — askpass enabled):
  - saved key passphrase → askpass autofills passphrase prompts; agent left intact.
  - saved password (and combined auth when the key is loaded into the agent) →
    same askpass env also advertises login-password context; MFA stays on the TTY.
  - nothing saved → no askpass; SSH prompts on the TTY.
- **Askpass disabled** (the `use-askpass` setting is off): set no `SSH_ASKPASS`;
  ssh prompts natively on the TTY.

Credentials are stored/retrieved through a **pluggable secret backend**
(`secret_storage.py`): `connection_manager.get_connection_password` /
`get_password` and `askpass_utils.lookup_passphrase` / `lookup_ssh_password`
delegate to `SecretManager`. The backend is
selectable via the `secrets.backend` setting — `auto` (platform default:
libsecret then keyring on Linux, keyring on macOS), or an explicit `libsecret` /
`keyring` / `pass` (passwordstore.org) / `bitwarden` / `keepassxc` / `agent`, or
a registered custom backend. With **`auto`**, reads/deletes fall through to every
available backend so secrets aren't orphaned when the selection changes; with an
**explicit** backend, `store`/`lookup`/`delete` consult only that backend. The askpass helper
(`askpass_utils.py`) answers passphrase **and** password prompts from the selected
backend (classifying OTP/MFA as decline→TTY) and, for unstored passphrases when
enabled, shows a GTK prompt. Keep that working.

Backend specifics:
- **KeePassXC** can be used two ways: (a) enable its GUI *Secret Service integration* and
  select `libsecret` (same `org.freedesktop.secrets` D-Bus API); or (b) the dedicated
  **`keepassxc`** backend (`KdbxBackend`, `secret_storage.py`) which opens a `.kdbx` file
  **directly** via `pykeepass`. The KDBX format is a static encrypted file (no session), but
  the backend is `session_backed=True`: `unlock(master_password)` opens the file (+ optional
  key file from `secrets.keepassxc.keyfile`), warms a `title→password` cache, and exports the
  derived `transformed_key` as `SSHPILOT_KDBX_KEY` so the askpass subprocess opens the file
  fast (no Argon2) without re-prompting — same env posture as `BW_SESSION`, never persisted,
  dropped on idle/exit. DB + keyfile paths come from `secrets.keepassxc.*` → exported as
  `SSHPILOT_KDBX_DATABASE`/`_KEYFILE`. Read-write: secrets are stored in a dedicated `sshPilot`
  group (entry title = the account; sshPilot type in a custom property). Caveat: don't keep
  the same `.kdbx` open in KeePassXC while sshPilot writes (`kp.save()` can conflict).
- **`bitwarden`** is *session-backed* (`bw` CLI). One backend covers Bitwarden cloud
  **and** self-hosted **Vaultwarden** (and any account) — which server/account the CLI
  talks to is the CLI's own config plus the optional **account/profile**
  `secrets.bitwarden.profile` (a `bw` data dir, exported as `BITWARDENCLI_APPDATA_DIR`
  into the process env so every `bw` spawn and the askpass subprocess use that account).
  It must be unlocked (master password) before secrets resolve; the unlock token is
  cached in-process and exported as `BW_SESSION` so the askpass subprocess can read
  non-interactively. The token is dropped after `secrets.session_timeout` idle minutes
  (propagated as `SSHPILOT_SECRET_SESSION_TIMEOUT` seconds) and on app shutdown. The GTK
  unlock prompt lives in `secret_unlock_dialog.py` (the core module stays GTK-free and
  never prompts); it is driven from Preferences and lazily from
  `terminal_manager.connect_to_host`. Signing in (`bw login`, and `bw config server <url>`
  first for a self-hosted Vaultwarden) is a one-time user step in a terminal — the app
  only runs `bw unlock`. The prompt has an opt-in **"Remember master password"** that
  saves it to the **platform keyring** (libsecret/keyring) — never the vault it unlocks —
  via `SecretManager.store_in_keyring`/`lookup_in_keyring`/`delete_in_keyring` and
  `master_password_spec` (keyed by backend + `BITWARDENCLI_APPDATA_DIR` profile,
  selection-independent). When a saved password exists, `prompt_unlock` auto-unlocks with
  it but still shows the "Unlocking…" spinner; a stale saved password is dropped and the
  manual prompt shown.
- **`agent`** means *don't store secrets at all*: a null backend. When explicitly
  selected, `SecretManager` consults only it (no fallback/fallthrough) so nothing
  is written to or read from other stores; the user relies on ssh-agent (the existing
  key-preload path) and ssh's own prompts. `store` returns success as a no-op;
  `delete` is a no-op on other stores — switch to `auto` or the backend that holds
  the secret to purge it.
- Session-backed backends set `session_backed=True` on `SecretBackend` and implement
  `is_unlocked`/`unlock`/`lock`; passive stores leave these as no-ops.

### Credential manager (export / backup layer)

Connect-time storage uses `SecretManager` directly. For a **normalized list** of
every sshPilot-managed secret (backup `.spbk`, future vault migration), use the
credential manager stack — see `docs/CREDENTIAL_MANAGER.md`.

- **`credential_model.py`** — `Credential` dataclass; `canonical_password_host` /
  `password_host_candidates` (canonical SSH password key =
  `hostname` → `host` → `nickname`).
- **`credential_manager.py`** — `CredentialManager.list_credentials()` gathers
  passwords, sudo passwords, and key passphrases (including `resolved_identity_files`
  from `ssh -G`). Read-only; never prompts; locked vaults contribute nothing.
- **`credential_adapters.py`** — `SecretBackendAdapter` / `KdbxAdapter` for
  credential-centric `load_all` / `save` / `delete` (export targets).

**SSH password API (connect-time):**

- **Store:** `ConnectionManager.store_connection_password(connection, password)`
  — always under the canonical host; clears legacy alias copies.
- **Lookup:** `ConnectionManager.get_connection_password(connection)` — probes
  legacy aliases and **migrates** on hit.
- **Low-level:** `store_password(host, user)` / `get_password(host, user)` for
  callers that already know the exact key (plugin secrets, etc.).

`SecretManager.lookup_everywhere` and `all_available_backends()` support export;
normal `lookup` / `delete` honor backend selection.

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
See also **askpass mechanics (passphrases and login passwords)** below.
- `get_ssh_env_with_askpass(require, …)` (`askpass_utils.py`) returns an env with
  `SSH_ASKPASS=<our helper>`, `SSH_ASKPASS_REQUIRE=<require>`, optional
  `SSHPILOT_PASSWORD_*` / session-password id (or file fallback), a `DISPLAY` fallback, and the
  `GNOME_KEYRING_*` control vars cleared (so gnome-keyring doesn't intercept)
  while keeping D-Bus available for libsecret.
- `require` is OpenSSH's `SSH_ASKPASS_REQUIRE`: `prefer` (default — use askpass
  even when a TTY exists, OpenSSH ≥ 8.4), `force`, or `never`.
- OpenSSH may set `SSH_ASKPASS_PROMPT`: `none` (FIDO touch reminder), `confirm`
  (yes/no), or unset (typed secret). **Presence usually skips askpass** when
  stderr is a tty (`notify_start` writes to the TTY); VTE shows it. Headless
  paths get presence via askpass (`PROMPT=none`).
- Every SSH spawn with **no user-visible TTY** must use
  `apply_headless_askpass_env()` (`ssh_connection_builder.py`) so secrets /
  PIN / OTP / presence go through graphical askpass (`REQUIRE=prefer`).
  Callers: SFTP file manager, SCP `list_remote_files`, plugin `run_command` /
  stream / port-forward, remote history fetch.
- ssh invokes our helper (`handle_askpass_cli`): passphrase →
  `lookup_passphrase`; login password → session file / `lookup_ssh_password`;
  OTP/PIN → user dialog; `PROMPT=none` → touch reminder (no entry).
  Unstored key passphrases return nothing so SSH / the OS / ssh-agent can
  prompt; login-password and MFA prompts use the graphical askpass dialogs.
  Helper output is streamed into the app log by the askpass log forwarder.
- The `use-askpass` setting (default on, no Preferences toggle) gates askpass
  wiring; with askpass off, ssh prompts natively on the TTY.

### In-app password & passphrase dialogs (GUI)

When **your code** (not the `ssh` subprocess) must ask the user for credentials,
use the shared helpers in `window.py`. Do **not** create a one-off password
dialog — secondary windows parented incorrectly hide behind the main window on
Wayland. The shared helper uses `Adw.Dialog` + header bar (Cancel / confirm),
a boxed-list `PasswordEntryRow`, and an optional Store checkbox.

**SSH login password (in-process, blocking, main-thread only)**

- **`show_ssh_password_dialog(...)`** in `window_dialogs.py` (re-exported from
  `window`) — the single entry point.
  Resolves `MainWindow` via `resolve_app_modal_parent(from_widget)`, presents it,
  shows the standard password dialog (optional **Store password** via
  `connection_manager`), and returns the string or `None`.
- Used by: built-in file manager, authorized-keys editor, external SFTP mount
  (`sftp_utils`), and `MainWindow.prompt_ssh_password()`.

Typical call from a secondary window or plugin page:

```python
from sshpilot.window_dialogs import show_ssh_password_dialog
# (also re-exported from sshpilot.window)

password = show_ssh_password_dialog(
    from_widget=self,                    # or your Gtk.Widget / Adw.Window
    connection=connection,               # optional; fills host/user/nickname
    connection_manager=connection_manager,
)
if not password:
    return  # user cancelled
connection.password = password          # then pass to resolve_native_auth / backend
```

**Key passphrase (in-process)**

- **`MainWindow.prompt_ssh_passphrase(key_path)`** — same stacking rules; use when
  you hold a `MainWindow` reference.
- Passphrases needed **by the `ssh` child process** go through askpass IPC
  (`askpass_server.py` → `prompt_ssh_passphrase`), not this API.

**Modal stacking helpers (custom dialogs)**

If you add a non-password modal from a plugin page or secondary window:

1. `parent = resolve_app_modal_parent(from_widget)`
2. `present_for_modal_dialog(parent)`
3. Build `Adw.MessageDialog(transient_for=parent, modal=True, …)` and `present()`

Plugins: see **PLUGIN_SDK.md → Advanced UI — credential dialogs** (`ctx.connection_manager`
for store-password; must run on the UI thread).

### askpass mechanics (passphrases and login passwords)
The native path uses **one** askpass helper for both secrets:
- **Key passphrase** prompts → lookup via `lookup_passphrase` / secret backend;
  optional GTK / main-app IPC when nothing is stored and the builtin prompt is on.
- **Login password** prompts → lookup via `SSHPILOT_PASSWORD_USER` +
  `SSHPILOT_PASSWORD_HOSTS` (and optional `SSHPILOT_SESSION_PASSWORD_ID` via
  askpass IPC for just-typed in-memory secrets — no disk; file fallback only
  when the prompt server is not advertised).
- **Interactive / MFA** prompts (OTP, PIN, yes/no) → ask the user via the
  main-app dialog (or a standalone askpass window). OpenSSH with
  `SSH_ASKPASS_REQUIRE=prefer` does **not** fall back to the TTY when askpass
  declines, so MFA cannot be left on the VTE; the user still types the code
  (it is never autofilled from the vault).

`get_ssh_env_with_askpass(...)` in `askpass_utils.py` sets `SSH_ASKPASS`,
`SSH_ASKPASS_REQUIRE`, and the password-context env vars. Do **not** reintroduce
sshpass or terminal PTY password autofill for SSH login secrets.
(`TerminalWidget.arm_password_pty_autofill` remains only for non-SSH cases such
as remote sudo prompts.)

### Who builds what
- **Interactive terminal** (`terminal.py::_setup_ssh_terminal`): consumes the
  prepared `connection.ssh_connection_cmd` (command + env + auth flags) — it does
  **not** build commands or derive auth. Runtime mechanics only: askpass log
  forwarding, `TERM`/`PATH`, and the PTY/spawn. Login password + passphrase
  come from askpass in the prepared env.
- **SCP UI** (`scp_window.py`): upload and download both run `scp` in a VTE via
  `_start_scp_transfer` / `_show_scp_terminal_window`, applying
  `resolve_native_auth` the same way (askpass for secrets; MFA on the VTE).
  Download browse listing uses `list_remote_files` (`scp_utils.py`) →
  `build_ssh_connection` + askpass (headless; MFA via askpass dialogs).
  Shared argv helpers live in `scp_utils.py` (no headless transfer API).
- **ssh-copy-id** (`sshcopyid_window.py`): builds its own `ssh-copy-id` argv,
  applies `resolve_native_auth`, then `apply_forced_askpass_env`
  (`SSH_ASKPASS_REQUIRE=force`) so passphrase/password/MFA use the graphical
  askpass dialog even though the command runs in a VTE. Plugin
  `copy_key_to_host` uses the same forced-askpass env.
- **System / external terminal**: uses `build_native_command()` — a *plain*
  `ssh -F <config> <host>` with **no** in-app auth (`IdentityAgent`/askpass),
  because the external terminal supplies its own TTY and agent.
- **SFTP file manager** (`file_manager/openssh_backend.py`): PTY-less
  `ssh -F <config> … -s <host> sftp` with `apply_headless_askpass_env`
  (password / passphrase / OTP / FIDO presence via askpass). Rides a live
  ControlMaster when a terminal already opened one; otherwise authenticates
  on the worker itself.

### Key functions/files
- `ssh_connection_builder.py`: `build_ssh_connection` (native-only),
  `resolve_native_auth` (the auth chokepoint), `apply_headless_askpass_env`
  (`REQUIRE=prefer` for no-TTY spawns), `apply_forced_askpass_env`
  (`REQUIRE=force` for ssh-copy-id), `build_native_command` (plain command
  for external processes), `_build_base_ssh_command` (shared option builder
  used by explicit-command callers like SCP).
- `ssh_multiplex.py`: ControlMaster socket policy + `invalidate_master`.
- `connection_manager.py`: `Connection.native_connect()`/`connect()`,
  persistence of connections to `~/.ssh/config`, credential storage
  (`store_connection_password`, `get_connection_password`, …).
- `credential_manager.py` / `credential_model.py` / `credential_adapters.py`:
  normalized credential listing and export (see `docs/CREDENTIAL_MANAGER.md`).
- `askpass_utils.py`: askpass helper for **passphrases and login passwords**,
  prompt classification, keyring lookup, and GTK passphrase prompt.
- `window_dialogs.py`: `show_ssh_password_dialog`, `resolve_app_modal_parent`,
  `present_for_modal_dialog` — in-app SSH password prompts and Wayland-safe modal
  parenting (re-exported from `window`; see **In-app password & passphrase
  dialogs** above).

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
sudo apt install python3-gi python3-gi-cairo libgtk-4-1 gir1.2-gtk-4.0 libadwaita-1-0 gir1.2-adw-1 libvte-2.91-gtk4-0 gir1.2-vte-3.91 libgtksourceview-5-0 gir1.2-gtksource-5 libsecret-1-0 gir1.2-secret-1 python3-cryptography sshpass ssh-askpass gir1.2-webkit-6.0
```

**Fedora/RHEL/CentOS:**
```bash
sudo dnf install python3-gobject gtk4 libadwaita vte291-gtk4 gtksourceview5 libsecret python3-cryptography sshpass openssh-askpass webkitgtk6
```

### Python Dependencies
- Python >= 3.8
- PyGObject >= 3.42
- pycairo >= 1.20.0
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
- Prefer unit tests and controller tests. Do not add GUI tests unless the bug specifically involves widget interaction, focus handling, drag-and-drop, rendering, or event delivery.
- Verify keyboard shortcuts work on both platforms

### Running GUI tests (real GTK)

The default `pytest` suite stubs `gi` and never opens a window, so it stays
headless/CI-safe. Real-GTK GUI tests (marker `gui`) boot the actual
`SshPilotApplication` on a display and drive its `Gio` actions/widgets — useful
for action/dialog/state/preference flows. They are **opt-in** and excluded from
the default run (`addopts = -m "not gui"` in `pytest.ini`):

```bash
SSHPILOT_GUI_TESTS=1 pytest -m gui            # on a display
SSHPILOT_GUI_TESTS=1 xvfb-run -a pytest -m gui  # headless machine
```

Without `SSHPILOT_GUI_TESTS=1` + real PyGObject + a display they **skip**
(never error), so they can never turn CI red. Write them with the harness in
`tests/_gui_harness.py`: **name the file `test_gui_*.py`** (in GUI mode the
conftest collects only `test_gui_*` modules — importing the stub-assuming
modules under real GTK can segfault during collection), call `requires_gui()` at
module top, mark the module `pytest.mark.gui`, and use the `gui` fixture
(`open_local_tabs`, `user_pages`, `message_dialogs`, `activate_action`,
`respond`). See `tests/test_gui_tab_close.py` for examples. They are NOT for pixel-gesture,
drag-and-drop, VTE-scraping, or live-SSH bugs — use unit tests there.

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
- The app uses askpass for private key passphrases **and** stored login
  passwords; MFA/OTP stays on the TTY (`SSH_ASKPASS_REQUIRE=prefer`). Do not
  reintroduce sshpass for the native connection path
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

CLI flags (`sshpilot/main.py::main`):
- `--verbose` / `-v` — detailed (debug) logging; `--quiet` / `-q` — warnings & errors only.
- GTK/GLib/Gdk/Pango/VTE **warnings & criticals are captured into the log files by
  default** via `_install_gtk_log_capture()` (logged under the `gtk` logger; also echoed
  to stderr). It installs **both** interception points because GTK/GLib use two logging
  paths: `GLib.log_set_handler` for legacy `g_log`/`g_warning` (e.g. GLib's child-watch
  warning) **and** `GLib.log_set_writer_func` for GTK4's **structured** logging
  (`g_log_structured`, e.g. `gtk_widget_measure` / `AdwMessageDialog` warnings) which
  bypasses legacy handlers. The writer delegates to `g_log_writer_default` so stderr +
  `G_DEBUG=fatal-warnings` still work. The `Gtk-CRITICAL`/`Gtk-WARNING` lines name the
  exact bad widget/render operation — look here first for UI / widget-lifecycle /
  rendering bugs.
- **Uncaught Python exceptions are logged by default** via `_install_exception_hooks()`
  (`sys.excepthook` — also covers PyGObject GLib/GTK callback exceptions —
  `threading.excepthook`, and `sys.unraisablehook` for `__del__`/finalizer errors).
- `--log-gtk-warnings` — *additionally* capture lower-severity GTK/GLib **info & debug**
  messages (deep GTK tracing); warnings/criticals are captured without it.
- `--fatal-warnings` — `GLib.log_set_always_fatal(WARNING|CRITICAL)` via
  `_enable_fatal_gtk_warnings()`; the resulting `abort()` is caught by faulthandler and the
  exact stack is written to `crash.log`. Aggressive (aborts on benign warnings too) — use
  in a focused repro.
- `--diagnostics` — shorthand for `--verbose --log-gtk-warnings` (use when filing a bug).

Logs live under `platform_utils.get_state_dir()` (`~/.local/state/sshpilot/`, or the
Flatpak path): `sshpilot.log` (master, rotating 10 MB × 5), `app.log`, `ssh.log`, and
`crash.log`. **`crash.log`** is the faulthandler dump, armed in
`SshPilotApplication.__init__` by `_enable_crash_diagnostics()` (all-thread Python
tracebacks + a C stack on Python 3.12+). It is rotated on the next launch: a non-empty
`crash.log` means the previous run crashed, so it is moved to `crash.log.previous` and
surfaced via the startup "closed unexpectedly" dialog and **Help ▸ Report a Problem**
(`window.on_report_problem_action` → `log_viewer.build_report_bundle`). For a crash with
no Python frame (pure GTK), use the `coredumpctl` core + `py-bt` (needs `python3-dbg`);
GTK frames need GTK debug symbols to resolve.

**Help ▸ Export Diagnostics…** (`win.export-diagnostics` →
`window.on_export_diagnostics_action` → `log_viewer.build_diagnostics_zip`) writes a ZIP
with `logs/` (all log files incl. crash reports), `system-info.txt` (`StartupInfo`),
`version.txt`, and a **redacted** `config.json` (`log_viewer._redact_config` strips
password/passphrase/secret/token/credential/api-key/private-key values + PEM blobs).
Saved connections / `ssh_config` are intentionally excluded for privacy.


## Memory and Preferences
- Do not add/remove features without user's confirmation
- User prefers no UI files; define all interfaces in code
- User requires explicit permission before modifying/deploying codebase

## Cursor Cloud specific instructions

These notes cover non-obvious caveats for this VM. Standard commands live in
**Setup Commands** / **Development Environment** above and in `README.md`.

- **Python deps live in the system interpreter, not a venv.** The app uses the
  system `python3-gi` (PyGObject) + GTK/VTE/libadwaita GObject-introspection
  packages, which a fresh virtualenv cannot see. Install pip deps with
  `pip install --break-system-packages -r requirements.txt` (the startup update
  script does this). A plain `python3 -m venv` will fail to import `gi`.
- **`pytest` is required to run the suite** but is not in `requirements.txt`; the
  update script installs it.
- **GUI runs headed on `DISPLAY=:1`** (X11 is already available). Launch with
  `python3 run.py --verbose`; it is a long-running process, so run it in a tmux
  session. `libEGL ... DRI3` warnings are harmless (software rendering).
- **SVG icons need `librsvg2-common`** (provides the gdk-pixbuf SVG loader). It
  is installed in the snapshot; without it the app logs many
  `Failed to load icon ... Unrecognized image file format` warnings (cosmetic
  only — the app still runs).
- **`pytest` runs headless against a stubbed `gi`** (see `tests/conftest.py`,
  which injects dummy GTK modules when `gi` is not already imported). The
  default `pytest` collects only `tests/`. The suite is green (1677 passed, 40
  skipped); CI runs it on every PR in `.github/workflows/tests.yml`, alongside
  Ruff (`lint.yml`), the type check (`typecheck.yml`), and the Meson build
  (`meson.yml`).
- **Nickname field forbids whitespace.** When creating a connection in the GUI,
  the Save button silently stays disabled if the nickname contains a space
  ("no whitespaces allowed"); use e.g. `DemoServer`, not `Demo Server`.
- **The GResource bundle (`src/sshpilot/resources/sshpilot.gresource`) is
  committed** and loaded at startup (the app exits if it cannot load it). Rebuild
  it with `scripts/build_gresource.sh` if you change anything under
  `src/sshpilot/resources/` — including the Blueprint `.blp` sources, which
  compile to the `.ui` files inside the bundle. `lint.yml` fails the PR if the
  committed artifacts drift from their sources. The Meson build compiles the same
  resources itself into `builddir/`, so it never reads the committed bundle; a
  source-tree run does.

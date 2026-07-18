## My Branch Naming Preference
- Always use: `feature/[description]` or `fix/[description]`
- Description must reflect the actual task — no jokes, puns, or random names

## Diagnostics
- CLI flags for capturing logs (`--verbose`, `--log-gtk-warnings`, `--fatal-warnings`,
  `--diagnostics`), the log-file layout, and the auto-captured `crash.log` are documented
  in `AGENTS.md` → **Debugging**. Read that before touching logging or crash handling.

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
  confirm with the user first.

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
  is the only place auth is decided, shared by terminal, SCP, ssh-copy-id, and
  the SFTP file manager (`ssh -s sftp` subprocess).
  **Askpass handles both key passphrases and stored login passwords**
  (`SSH_ASKPASS_REQUIRE=prefer`); OTP/MFA is collected via an askpass dialog
  (OpenSSH does not fall back to the TTY when askpass declines). Do not
  reintroduce sshpass or PTY login password autofill on the native path.
  Keyring autofill + the askpass prompt are advertised features — keep them.
- **Callers:** the terminal consumes the prepared command (it does not build
  commands); SCP UI / ssh-copy-id use VTE + `resolve_native_auth`; the
  system/external terminal uses `build_native_command()` (plain, no in-app
  auth); the SFTP file manager uses master-first PTY auth then the same native
  path over `ssh -s sftp`.
- **Advanced SSH options** (Preferences ▸ SSH Settings) are saved as `ssh.*`
  keys and composed into a flat `ssh.ssh_overrides` list
  (`preferences.py::save_advanced_ssh_settings`); the native command appends
  `ssh_overrides` verbatim, while `_build_base_ssh_command` (SCP) reads the
  individual keys. **Effective config** is computed with `ssh -G` via
  `get_effective_ssh_config()` when code needs the resolved per-host options.
  **askpass** = `SSH_ASKPASS`/`SSH_ASKPASS_REQUIRE` + the helper in
  `askpass_utils.py` (passphrase **and** login-password lookup; MFA → TTY).
  Full detail in `AGENTS.md`.

## Dialogs & Alerts (GTK4/libadwaita — read before adding any dialog)

SSHPilot is a GTK4/libadwaita app. Every dialog, alert, confirmation, or
notification must use **libadwaita** widgets, not raw GTK ones. Plain
`Gtk.MessageDialog` / bare `Gtk.Dialog` is never acceptable — it looks out of
place next to the rest of the Adwaita UI and breaks HIG compliance.

### Don't

- `Gtk.MessageDialog` — deprecated, unstyled, does not follow libadwaita
  visual language.
- `Gtk.Dialog` with manually packed buttons/labels for a "confirm or cancel"
  or "show an error".
- `Gtk.AlertDialog` (raw GTK4 native) for anything needing more than a single
  native OS-style prompt — it can't be themed to match the app.
- Rolling a custom `Gtk.Window` to fake a dialog.

### Do

- **Confirmations / yes-no / destructive → `Adw.AlertDialog`** (libadwaita
  ≥ 1.5). Add 1–3 responses; destructive action (delete, overwrite,
  disconnect-and-lose-data) → `Adw.ResponseAppearance.DESTRUCTIVE`, non-destructive
  confirm → `SUGGESTED`. Always set `default_response` and `close_response` so
  Esc/click-outside behave. Present with a parent widget, never orphaned.
- **Errors → `Adw.AlertDialog` with a single "OK" response.** An error is just
  a 1-button alert — don't invent a separate error-dialog pattern.
- **Transient feedback (saved, copied, connected) → `Adw.Toast`** via the
  window's existing `Adw.ToastOverlay` (check before adding a new one). If it
  needs no decision, it's a toast, not a dialog.
- **Forms / settings / multi-step → `Adw.Dialog`** (with `Adw.ToolbarView`
  inside) or **`Adw.PreferencesDialog`** (`Adw.PreferencesPage` /
  `Adw.PreferencesGroup`), presented via `dialog.present(parent)`.
- **File/color pickers → `Gtk.FileDialog` / `Gtk.ColorDialog`.** These native
  GTK4 APIs are correct and already match the platform.

### Terminal/log widgets attached to dialogs

Do **not** add a log/terminal widget to a dialog unless explicitly requested
for that dialog — attaching one unprompted is a bug, same as reaching for
`Gtk.MessageDialog`. When requested, it must be **hidden on open** and
**revealed via a button/expander**, never shown inline automatically.

- Static text, a few lines → `Gtk.Expander` (collapsed, never pre-expanded)
  wrapping a height-capped `Gtk.ScrolledWindow`, set as `extra_child` on the
  `Adw.AlertDialog`.
- Live/interactive widget (VTE terminal, real-time log, anything needing
  focus/scroll/selection) → `Adw.Dialog` (resizable, real content area), not
  `Adw.AlertDialog`. Reveal via a header-bar toggle / "Show Terminal" button
  that swaps in the widget — it must not be visible on open. If it also needs a
  yes/no decision, put the decision as an action in the `Adw.Dialog` header
  bar / `ToolbarView`, don't cram a live widget into `Adw.AlertDialog`.

### Checklist

1. Blocking + decision → `Adw.AlertDialog`. Just feedback → `Adw.Toast`.
   Form/settings → `Adw.Dialog` / `Adw.PreferencesDialog`.
2. Destructive response marked `DESTRUCTIVE`, confirm marked `SUGGESTED`.
3. Parent set; `default_response` + `close_response` set.
4. Log/terminal widget only if explicitly requested; hidden on open, revealed
   by button. Static → `extra_child` expander; live → `Adw.Dialog`.
5. No raw `Gtk.MessageDialog`, `Gtk.Dialog`, or hand-rolled `Gtk.Window`
   anywhere in the diff.

A plain `Gtk.MessageDialog` or hand-built `Gtk.Dialog` for any of the above is
a bug — reject the diff.
# sshPilot Plugin SDK

Write a plugin that adds protocols, UI pages, background workflows (e.g. VPS
provisioning), and reactions to application activity — using only the public
API. This guide is the contract: everything documented here is stable within
an API major version; everything else under `sshpilot.*` is private and may
change without notice.

> The canonical, maintained guide is **[docs/plugins/writing-plugins.md](docs/plugins/writing-plugins.md)**
> (plus [registry.md](docs/plugins/registry.md) and the
> [template](docs/plugins/template/)). Start there for the **6-step Quickstart**
> and the iterate loop; this file is the deeper API reference.

- **The only import you need:** `sshpilot.plugins.api`.
- **Current API version:** `1.9` (see [Versioning](#versioning)).
- **Worked examples** — two provider archetypes:
  - [`examples/mock_vps/`](sshpilot/plugins/examples/mock_vps/) — an **IP/SSH** provider: provision → get an IP → `add_connection` a normal SSH connection.
  - [`examples/easyenv_workspaces/`](sshpilot/plugins/examples/easyenv_workspaces/) — a **CLI/mesh** provider (real partner [easyenv.io](https://easyenv.io/cli)): a protocol backend whose connection *is* a CLI command, plus a management page. See [CLI-driven plugins](#11-cli-driven-plugins).

---

## 1. What a plugin can do

| Capability | API |
| --- | --- |
| Register a connection protocol (telnet, mosh, …) | `ctx.register_protocol(backend)` |
| Add a UI page (opens as a tab from the **Tools** menu) | `ctx.ui.register_page(...)` / `ctx.ui.open_page(...)` |
| Show a transient notification | `ctx.ui.notify(message)` |
| React to app activity | `ctx.events.subscribe(event, callback)` |
| Create & persist a connection | `ctx.add_connection(data)` |
| Open a terminal for a connection | `ctx.open_connection(nickname)` |
| Generate an SSH key | `ctx.generate_key(name, ...)` |
| Run a one-shot remote command (native SSH/auth path) | `ctx.run_command(nickname, command)` *(1.5)* |
| Stream a remote command line-by-line (native SSH/auth path) | `ctx.run_command_stream(nickname, command, on_line=…)` *(1.13)* |
| Run a one-shot local command (Flatpak-host aware) | `ctx.run_local_command(command)` *(1.11)* |
| Stream a local command line-by-line | `ctx.run_local_command_stream(command, on_line=…)` *(1.13)* |
| Read resolved SSH config (`ssh -G`) | `ctx.get_effective_ssh_config(nickname)` *(1.5)* |
| List/delete keys, deploy a key to a host | `ctx.list_keys()` / `ctx.delete_key(path)` / `ctx.copy_key_to_host(nickname, pub)` *(1.5)* |
| Inspect/drive open terminals | `ctx.list_sessions()` / `ctx.read_terminal(id)` / `ctx.send_terminal(id, text)` *(1.5)* |
| Persist files / make HTTP calls | `ctx.data_dir`, `ctx.files`, `ctx.http` *(1.5)* |
| Open a terminal tab running a one-off command (streamed) | `ctx.open_command_terminal(nickname, remote_command, title=)` *(1.6)* |
| Open a local terminal tab running a command (streamed/interactive) | `ctx.open_local_command_terminal(command, title=)` *(1.11)* |
| Add an item to the connection right-click menu | `ctx.ui.register_connection_action(action_id, label, icon, callback)` *(1.7)* |
| Register a page with no menu entry / custom activation | `ctx.ui.register_page(..., add_menu_item=False, on_activate=cb)` *(1.8)* |
| Keep one SSH connection warm & multiplex calls over it | `ctx.acquire_multiplex(nickname)` / `ctx.release_multiplex(nickname)` *(1.9)* |
| Store/read credentials (keyring) | `ctx.secrets.get/set/delete(key)` |
| Prompt for SSH login password (in-app GUI) | `show_ssh_password_dialog(...)` in `sshpilot.window` *(escape hatch — see [Advanced UI — credential dialogs](#advanced-ui--credential-dialogs))* |
| Store/read plugin settings | `ctx.settings.get/set(key)` |
| Run code back on the UI thread | `ctx.run_on_ui_thread(fn, *args)` |

---

## 2. Quickstart

A plugin is a directory with a manifest and a Python package exposing a
`Plugin` class.

```
my-plugin/
├── plugin.json
└── __init__.py
```

**`plugin.json`**

```json
{
  "id": "acme-vps",
  "name": "ACME VPS Provider",
  "version": "1.0.0",
  "homepage": "https://github.com/yourname/acme-vps",
  "api_version": 1
}
```

- `id` — unique, stable; used for secret/setting namespacing and the enable list. No `/`.
- `name` — shown in Preferences ▸ Plugins.
- `version` — your plugin's version, e.g. `"1.2.0"` (dotted integers, matching your release tag). Recommended: it drives the in-app **update button** (sshPilot compares it to the registry's latest version).
- `api_version` — the API **major** version you target (currently `1`). The loader refuses plugins whose major version doesn't match the running app.
- `homepage` — optional URL of your plugin's source/homepage; shown as a clickable link in the per-plugin info dialog in Preferences ▸ Plugins.
- `permissions` — optional list declaring the capabilities you use
  (`network`, `filesystem`, `keyring`, `connections`, `process`, `ui`,
  `settings`). They are **shown to the user at install/enable for informed
  consent but are NOT enforced** — plugins run in-process with full privileges.
  Declare every capability you actually use. Full table + the manifest schema in
  the [canonical guide](docs/plugins/writing-plugins.md#permissions).

**`__init__.py`**

```python
from sshpilot.plugins.api import SshPilotPlugin, PluginContext, Events

class Plugin(SshPilotPlugin):
    def activate(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        ctx.ui.register_page("home", "ACME", "network-server-symbolic", self._build_page)
        ctx.events.subscribe(Events.APP_STARTED, lambda _p: ctx.ui.notify("ACME ready"))

    def deactivate(self) -> None:
        pass

    def _build_page(self):
        import gi; gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk
        return Gtk.Label(label="Hello from ACME")
```

**Install & enable (user plugin):**

1. Copy the directory to the user plugin dir:
   - Native: `~/.local/share/sshpilot/plugins/acme-vps/`
   - Flatpak: `~/.var/app/io.github.mfat.sshpilot/data/sshpilot/plugins/acme-vps/`
   (Honors `$XDG_DATA_HOME` if set.)
2. Launch sshPilot, open **Preferences ▸ Plugins**, toggle your plugin on.
3. **Restart** sshPilot. User plugins are opt-in — a file on disk never runs code until you enable it by id.

---

## 3. The lifecycle contract (read this)

```
load  →  activate(ctx)        registration only — UI does NOT exist yet
                  │
present │  app_started event  UI is live: open pages, toast, open connections
                  │
   …running…      connection_* / session_* events
                  │
quit    │  app_shutdown event  then deactivate()
```

- **`activate(ctx)` is registration only.** The main window UI is not built yet.
  Do: `register_protocol`, `ui.register_page`, `events.subscribe`, read
  `settings`. **Don't** call `ui.open_page`, `ui.notify`, `open_connection`, or
  `generate_key` here — they need the live window. (Calls made early are
  queued where possible, but don't rely on it; do live work from `app_started`
  or later events / user actions.)
- **`app_started`** fires once the window is presented and bound — your cue
  that live UI/terminal/key calls are safe.
- **`deactivate()`** is best-effort, called at shutdown after `app_shutdown`.

---

## 4. `PluginContext` reference

Passed to `activate`. One context per plugin; `ctx.plugin_id` is your manifest id.

### Registration & data
- `register_protocol(backend)` — register a `ProtocolBackend` (see protocol plugins).
- `add_connection(data: dict) -> ConnectionInfo` — create + persist a connection. Validated; raises `ValueError` on bad/duplicate data. SSH connections go to `~/.ssh/config`; non-SSH protocols persist internally. Returns a read-only [`ConnectionInfo`](#payloads).
- `update_connection(nickname: str, data: dict) -> bool` — update an existing connection in place (rewrites its stored settings and re-stores its password). Returns `False` if no connection with that nickname exists. Pair with `add_connection` to refresh a provisioned host whose address/credentials changed. *(API ≥ 1.3)*
- `open_connection(nickname: str) -> bool` — open a terminal tab for an existing connection. Returns `False` if unknown / UI not ready. *(after `app_started`)*
- `list_connections() -> list[ConnectionInfo]` — read-only snapshot of every saved connection. Safe any time after load. *(API ≥ 1.4)*
- `generate_key(name: str, *, key_type="ed25519", key_size=3072, comment=None, passphrase=None) -> str | None` — generate an SSH key; returns the private-key path or `None`. *(after `app_started`)*

### Remote commands, config & keys *(API ≥ 1.5)*
- `run_command(nickname: str, command: str, *, timeout=30, input=None) -> CommandResult` — run a one-shot command on a saved host and capture `exit_code`/`stdout`/`stderr`. Reuses the app's SSH/auth path (`~/.ssh/config`, ProxyJump, passphrase via askpass; sshpass only when the connection's auth method is password). **Blocking** — call from a worker thread. `exit_code == -1` means it couldn't be launched. Optional `input` is written to the remote command's stdin (e.g. a password for `sudo -S`); the SSH transport itself is non-interactive (no PTY).
- `run_command_stream(nickname: str, command: str, *, on_line, on_done=None, input=None) -> StreamHandle` *(API ≥ 1.13)* — start a long-lived remote command over the same native SSH/auth path and deliver stdout/stderr **lines** to `on_line` (marshalled onto the UI thread). No timeout — call `handle.stop()` when finished (page unmap, selection change, …). `on_done(exit_code)` runs when the process exits. Use this for in-page streaming such as `docker logs -f` or `docker events` when you need lines in a widget rather than a VTE tab.
- `get_effective_ssh_config(nickname: str) -> dict` — resolved `ssh -G` options for the host (keys lowercased; multi-value options are lists).
- `list_keys() -> list[dict]` — `{"private_path", "public_path"}` for keys the app manages. *(after `app_started`)*
- `delete_key(private_path: str) -> bool` — delete a key pair; refuses paths outside the app's key dir. *(after `app_started`)*
- `copy_key_to_host(nickname: str, public_key_path: str) -> bool` — install a public key on a host via the shared ssh-copy-id/auth path. **Blocking.** *(after `app_started`)*
- `open_command_terminal(nickname: str, remote_command: str, *, title=None, pty_prompt=None, pty_response=None) -> bool` *(API ≥ 1.6)* — open a new terminal tab running a one-off command on the host over the single native SSH/auth path. Use this for **streamed/interactive** output that `run_command` (one-shot, captured) can't show — e.g. `docker logs -f`, `docker exec -it`, `top`. Optional `pty_prompt`/`pty_response` arm a one-shot auto-fill: the first time `pty_prompt` appears in the terminal output, `pty_response` (plus a newline) is typed into the PTY — useful to answer a remote `sudo` password prompt without putting the secret on a command line. *(after `app_started`)*

### Local commands *(API ≥ 1.11)*
- `run_local_command(command: str, *, timeout=30, input=None) -> CommandResult` — run a local shell command and capture its output. **Blocking** — call from a worker thread. In Flatpak the command runs on the host via `flatpak-spawn --host`.
- `run_local_command_stream(command: str, *, on_line, on_done=None, input=None) -> StreamHandle` *(API ≥ 1.13)* — long-lived local stream (Flatpak-host aware); same `on_line` / `on_done` / `stop()` semantics as `run_command_stream`.
- `open_local_command_terminal(command: str, *, title=None, pty_prompt=None, pty_response=None) -> bool` — open a local terminal and run a streamed/interactive command. It uses the same host-aware local terminal as the rest of sshPilot. *(after `app_started`)*

Use these only for genuinely local work. Remote commands must use `run_command` /
`run_command_stream` / `open_command_terminal` so they retain sshPilot's unified
SSH and authentication path.

### Connection multiplexing — faster polling *(API ≥ 1.9)*
If your plugin polls a host (repeated `run_command` calls — a dashboard, a stats
refresh, a watch loop), each call otherwise pays a fresh TCP connect **and auth
handshake**. Ask core to keep one SSH **ControlMaster** connection warm for the
host while your surface is open; `run_command` then transparently reuses it (no
re-auth, ~10–50 ms/call instead of hundreds). No socket management or new auth
path on your side — it rides the same `~/.ssh/config`/auth path as everything else.
- `acquire_multiplex(nickname: str) -> None` — start keeping a master warm for the
  host. **Refcounted** and shared process-wide, so two surfaces (or your plugin +
  the global Preferences toggle) share a single master. The master is created
  lazily by the first `run_command` after acquire and is self-healing.
- `release_multiplex(nickname: str) -> None` — drop your reference; when the last
  one goes away core tears the master down (`ssh -O exit`), else `ControlPersist`
  expires it. **Always balance every `acquire`** — the natural place is the page's
  `map`/`unmap` (acquire when shown, release when hidden; swap on host change).
- Older cores lack these methods — guard with `try/except AttributeError` (or check
  the negotiated API minor) and fall back to plain `run_command`.

```python
# In a polling page, tie the warm master to the page being visible:
self.connect("map",   lambda *_: self.ctx.acquire_multiplex(self._nick))
self.connect("unmap", lambda *_: self.ctx.release_multiplex(self._nick))
# …then just call self.ctx.run_command(self._nick, "docker ps …") on your timer.
```

### Terminals & sessions *(API ≥ 1.5, after `app_started`)*
- `list_sessions() -> list[SessionInfo]` — currently open terminal sessions.
- `read_terminal(session_id: str, max_chars=None) -> str | None` — read a session's terminal text.
- `send_terminal(session_id: str, text: str) -> bool` — send input to a session's terminal.

### Files & HTTP *(API ≥ 1.5)*
- `ctx.data_dir -> str` — a private, persistent per-plugin directory (created on access).
- `ctx.files` — sandboxed to `data_dir`: `path(rel)`, `read_text/write_text`, `read_bytes/write_bytes`, `exists(rel)` (escapes rejected).
- `ctx.http` — minimal blocking client: `get(url, *, headers=None, timeout=30)`, `post(url, *, data=None, json=None, headers=None, timeout=30)` → `HttpResponse{status, text, json(), ok}`. Call off the UI thread.

### UI — `ctx.ui`
- `register_page(page_id, title, icon_name, factory, *, add_menu_item=True, on_activate=None)` — `factory` is a zero-arg callable returning a `Gtk.Widget`, built on first open. The page appears under the **Tools** section of the main menu. *(safe in `activate`)* — *(API ≥ 1.8)* pass `add_menu_item=False` for a page opened directly (e.g. one tab per host, no menu clutter), and/or `on_activate=cb` to have the menu entry run a callback instead of opening the page.
- `open_page(page_id)` — open or focus the page as a tab. *(after `app_started`)*
- `register_connection_action(action_id, label, icon_name, callback)` *(API ≥ 1.7)* — add an item to the connection-list **right-click menu**; `callback` receives the connection nickname. *(safe in `activate`)*
- `notify(message, timeout=3)` — transient in-app toast. *(after `app_started`)*

### Events — `ctx.events`
- `subscribe(event, callback)` / `unsubscribe(event, callback)` — see [Events](#5-events). Constants are also on `ctx.events` (e.g. `ctx.events.APP_STARTED`).

### Secrets — `ctx.secrets` (secret-backend-backed, scoped to your plugin id)
- `get(key) -> str | None`, `set(key, value)`, `delete(key) -> bool`. Use for credentials/tokens. No other plugin can read your secrets.
- Storage goes through the app's configurable secret backend (`secrets.backend`): libsecret (also KeePassXC via its Secret Service integration) / OS keychain via keyring / `pass` / Bitwarden / Vaultwarden / a registered custom backend. Your plugin doesn't choose the backend — the user does; the API is the same regardless.
- If the user selects a session-backed backend (Bitwarden/Vaultwarden) that is locked, or the `agent` "don't store" backend, `get`/`set` may return `None`/fail — handle missing secrets gracefully.

### Identities — `ctx.identities` (SSH identity providers, read-only)
- `list() -> list[Identity]` — every SSH identity the configured identity providers currently expose (e.g. keys loaded in the system ssh-agent). `is_agent_available() -> bool` — whether the system ssh-agent is reachable.
- `Identity` fields: `id`, `display_name`, `fingerprint` (str | None), `provider_name`. Import it from `sshpilot.plugins.api`.
- This is the identity-side parallel of `ctx.secrets`: secrets answer *what password/passphrase*, identities answer *which key/agent*. Keeping them separate lets users mix sources (keys via ssh-agent, passwords via libsecret/Bitwarden) as configuration.
- Read-only for plugins — choosing/configuring providers is the user's job. See `IDENTITY_PROVIDERS.md` for the provider contract.

### Settings — `ctx.settings` (app config, scoped to your plugin id)
- `get(key, default=None)`, `set(key, value)`. For non-secret preferences; stored under `plugins.<id>.<key>`.

### Threading
- `run_on_ui_thread(fn, *args)` — run `fn(*args)` on the GTK main thread. Use to return from a background worker before touching UI or calling `add_connection`/`open_connection`.

### Advanced (escape hatch)
- `ctx.config`, `ctx.connection_manager` — live internal objects. Stable enough that the built-in backends use them, but prefer the named APIs; treat these as advanced.

#### Advanced UI — credential dialogs

When your plugin must collect an **SSH login password** in the GUI (e.g. before
calling `run_command` with a password-only host, or provisioning a connection),
use the same shared dialog as core — **do not** build your own password
dialog (`show_ssh_password_dialog` is an `Adw.Dialog` with a header bar).

```python
from sshpilot.window import show_ssh_password_dialog

# On the GTK main thread only (button handler, app_started callback, or inside
# ctx.run_on_ui_thread). Blocks until the user dismisses the dialog.
password = show_ssh_password_dialog(
    from_widget=page_widget,       # your page's root widget
    display_name=info.nickname,
    host=info.host,
    username=info.username,
    connection_manager=self.ctx.connection_manager,  # enables "Store password"
)
if not password:
    return
# Use password for your flow, or persist via ctx.secrets / connection fields.
```

Why this helper exists: on Wayland the dialog must be transient for
`MainWindow`, not your plugin tab. The helper resolves that parent, presents
the main window, and reuses the app's standard copy/storage UX.

| Need | API |
| --- | --- |
| SSH login password (in-app) | `show_ssh_password_dialog(...)` |
| SSH key passphrase (in-app, you have `MainWindow`) | `main_window.prompt_ssh_passphrase(key_path)` — internal; no stable plugin export yet |
| Passphrase for `ssh` subprocess | Handled automatically via askpass — do not prompt manually |
| Custom non-password modal from a plugin page | `resolve_app_modal_parent(widget)` + `present_for_modal_dialog(parent)` then your dialog |

**Threading:** `show_ssh_password_dialog` runs a nested main loop and **must**
run on the UI thread. From a worker thread, marshal with
`ctx.run_on_ui_thread(lambda: show_ssh_password_dialog(...))` and pass the
result back with a queue/future.

**Auth for remote commands:** prefer `ctx.run_command(nickname, …)` so core
applies the shared native auth path (askpass for key passphrases; sshpass only
for password-method connections). Prompt with `show_ssh_password_dialog` only
when you need a password that is not already in the keyring / connection record.

Full reference: **AGENTS.md → In-app password & passphrase dialogs** and
docstrings on `show_ssh_password_dialog` in `sshpilot/window.py`.

---

## 5. Events

Subscribe in `activate`; callbacks run **synchronously on the main thread**.
One callback raising never affects other callbacks, other plugins, or the app.

| Event | Payload | Fires when |
| --- | --- | --- |
| `Events.APP_STARTED` | `None` | window presented & bound (do live UI work from here) |
| `Events.APP_SHUTDOWN` | `None` | app is quitting (before teardown) |
| `Events.CONNECTION_CREATED` | `ConnectionInfo` | a connection is added |
| `Events.CONNECTION_UPDATED` | `ConnectionInfo` | a connection is edited |
| `Events.CONNECTION_DELETED` | `ConnectionInfo` | a connection is removed |
| `Events.SESSION_OPENED` | `SessionInfo` | a terminal session (tab) connects |
| `Events.SESSION_CLOSED` | `SessionInfo` | a terminal session disconnects |

```python
def activate(self, ctx):
    ctx.events.subscribe(Events.CONNECTION_CREATED, self._on_created)

def _on_created(self, info):   # info: ConnectionInfo
    print("new connection:", info.nickname, info.host)
```

> **Note:** creating a connection emits `CONNECTION_UPDATED` (the persist step)
> immediately followed by `CONNECTION_CREATED` for the same connection. If you
> only care about new connections, subscribe to `CONNECTION_CREATED`.
>
> `SESSION_OPENED` does not re-fire on an automatic reconnect of the same tab.
>
> **Rename caveat:** `CONNECTION_UPDATED` carries only the connection's *current*
> nickname — there is no "previous nickname". If you key plugin data by nickname
> (notes, snippets, …) you can't migrate it automatically on rename; instead
> reconcile against `list_connections()` (drop entries whose nickname no longer
> exists), as the `notes` plugin does.

### Payloads

Read-only frozen snapshots — decoupled from internal objects, safe to keep.

```python
ConnectionInfo(nickname: str, host: str, username: str, protocol: str, port: int)
SessionInfo(connection: ConnectionInfo, session_id: str)
CommandResult(exit_code: int, stdout: str, stderr: str)   # .ok == (exit_code == 0); -1 == couldn't launch
HttpResponse(status: int, text: str, headers: dict)        # .json() parses text; .ok == 2xx
```

---

## 6. UI hosting

`register_page` records your page; the **Tools** menu gets an item that opens
it as a tab. The widget is built lazily by your `factory` the first time it
opens, then cached (re-opening focuses the existing tab). If the factory
raises, the error is logged and a toast is shown — it can't crash the app.

```python
def _build_page(self):
    import gi; gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    box.append(Gtk.Label(label="Deploy a server"))
    btn = Gtk.Button(label="Deploy"); btn.connect("clicked", self._on_deploy)
    box.append(btn)
    return box
```

You may use GTK4/libadwaita freely (`gi.repository`) — that's a system
dependency, not a private sshPilot API.

---

## 7. Secrets & settings

Both are auto-scoped by your plugin id — no manual namespacing, no collisions.

```python
ctx.secrets.set("api_token", token)        # configurable backend (libsecret / OS keychain / pass)
token = ctx.secrets.get("api_token")

ctx.settings.set("region", "fra1")          # app config: plugins.<id>.region
region = ctx.settings.get("region", "fra1")
```

Never put credentials in `settings`; use `secrets`.

---

## 8. Threading

Do slow work (HTTP, provisioning, polling) on your own thread, then marshal UI
and connection calls back via `run_on_ui_thread`:

```python
def _on_deploy(self, _btn):
    import threading
    threading.Thread(target=self._worker, daemon=True).start()

def _worker(self):
    ip = provider_api.create_server()          # your code, off the UI thread
    key = self.ctx.generate_key("vps")         # (marshal anything UI-touching)
    self.ctx.run_on_ui_thread(self._finish, ip, key)

def _finish(self, ip, key):                    # back on the UI thread
    info = self.ctx.add_connection({"nickname": f"vps-{ip}", "host": ip,
                                    "hostname": ip, "username": "root",
                                    "protocol": "ssh", "keyfile": key})
    self.ctx.open_connection(info.nickname)
    self.ctx.ui.notify(f"{info.nickname} ready")
```

`add_connection`, `open_connection`, `generate_key`, and all `ctx.ui` calls
must be made on the UI thread.

---

## 9. Versioning

- `API_VERSION = (major, minor)`, currently `(1, 13)`. Your manifest declares the
  **major** you target; the loader skips plugins whose major doesn't match.
- Minor bumps are additive (new methods/events); your plugin keeps working. Note
  the loader checks only the **major**, so a plugin using a newer minor's API on
  an older app build fails at call time, not at load — target the minor you test.
- New since `1.0`: `1.1` plugin secrets; `1.2` events, UI extension,
  `open_connection`, `generate_key`, scoped `ctx.secrets`/`ctx.settings`,
  `ctx.run_on_ui_thread`, `ctx.plugin_id`; `1.3` connection groups
  (`create_group`/`add_connection_to_group`/`add_connection_group`) and
  `update_connection`; `1.4` `list_connections`; `1.5` `run_command`,
  `get_effective_ssh_config`, `copy_key_to_host`, `list_keys`/`delete_key`,
  `list_sessions`/`read_terminal`/`send_terminal`, and `ctx.data_dir`/
  `ctx.files`/`ctx.http`; `1.6` `open_command_terminal` (streamed/interactive
  output); `1.7` `ui.register_connection_action` (connection right-click menu);
  `1.8` `ui.register_page(add_menu_item=…, on_activate=…)`; `1.9`
  `acquire_multiplex`/`release_multiplex` (ControlMaster connection reuse for
  polling plugins — `run_command` reuses the warm master transparently); `1.10`
  `identities`; `1.11` `run_local_command`/`open_local_command_terminal`;
  `1.12` `ensure_local_forward` / `ui.open_web_tab`; `1.13`
  `run_command_stream` / `run_local_command_stream` (line-oriented streams +
  `StreamHandle.stop()`).

---

## 10. Do / Don't

- **Do** import only from `sshpilot.plugins.api`.
- **Do** keep `activate` to registration; do live work after `app_started`.
- **Do** keep network/slow work off the UI thread.
- **Do** call `acquire_multiplex`/`release_multiplex` (balanced) if you poll a host
  with repeated `run_command` calls — it removes a per-call connect+auth handshake.
- **Don't** touch the main window, the internal `Connection` class, private
  modules, or GObject signals directly — use events and `PluginContext`.
- **Don't** store secrets in `settings`.
- **Don't** roll custom password prompts — use `show_ssh_password_dialog`
  (`Adw.Dialog` + header bar; see [Advanced UI — credential dialogs](#advanced-ui--credential-dialogs)).

See the full [`mock_vps`](sshpilot/plugins/examples/mock_vps/) example for all of
the above wired together.

---

## 11. CLI-driven plugins

Many providers ship a CLI that already does the work (provision, list, status,
connect). You don't need an HTTP SDK — shell out to the CLI. Two patterns:

**A. The connection *is* a CLI command (mesh / no raw SSH params).** Some
providers (e.g. [easyenv.io](https://easyenv.io/cli): `easyenv workspace ssh <id>`)
connect over their own mesh and never expose host/port/user/key. Model this as
a **protocol backend** whose `build_spawn` returns the CLI command as argv —
exactly like the built-in telnet backend:

```python
class EasyEnvBackend(ProtocolBackend):
    protocol_id = "easyenv"
    display_name = "EasyEnv Workspace"
    def capabilities(self): return frozenset()   # mesh: no SFTP/forward/copy-key
    def connection_fields(self):
        return [FieldSpec(key="workspace_id", label="Workspace ID", required=True)]
    def build_spawn(self, connection, ctx):
        wsid = (connection.data or {}).get("workspace_id")
        if not wsid: raise ProtocolError("No workspace id.")
        return SpawnSpec(argv=["easyenv", "workspace", "ssh", str(wsid)], env=dict(os.environ))
```

Register it in `activate`, then create connections from your page with
`ctx.add_connection({"protocol": "easyenv", "nickname": name, "workspace_id": id})`
and open them with `ctx.open_connection(name)`. `capabilities() == frozenset()`
hides the SSH-only UI (SFTP, port-forward, ssh-copy-id, system terminal) that
doesn't apply.

**B. Provision, then make a normal SSH connection (IP-based).** If the provider
gives you an IP/host, just `ctx.add_connection({...,"protocol":"ssh","host":ip})`
(see `mock_vps`).

**Rules for both:**
- **The `ctx` in `build_spawn` is a host-less spawn context** (built via
  `PluginContext.for_spawn`): `ctx.secrets` / `ctx.settings` work and are scoped
  to your plugin, but `ctx.ui` and `ctx.events` are `None` — `build_spawn` must be
  stateless, deriving everything from `connection.data` + the environment.
- **Run the CLI off the UI thread** (`threading.Thread` + `subprocess.run(..., timeout=…)`), then marshal results back with `ctx.run_on_ui_thread`. `build_spawn` itself must not block — it only assembles argv.
- **Parse defensively.** Prefer `--output json` / `--json`, but tolerate missing/renamed fields; don't hard-assert a schema.
- **Flatpak:** inside the sandbox the host CLI isn't on `PATH`. Detect `os.path.exists("/.flatpak-info")` and prefix calls with `["flatpak-spawn", "--host"]` — both your page's `subprocess` calls **and** the `build_spawn` argv (the terminal child is sandboxed too). sshPilot's manifest already grants `--talk-name=org.freedesktop.Flatpak`.
- **Let the CLI own its credentials.** If the tool keychains its own token (e.g. `easyenv auth login`), detect state (`auth whoami`) and optionally drive login; don't duplicate the token in `ctx.secrets`.

See [`easyenv_workspaces`](sshpilot/plugins/examples/easyenv_workspaces/) for the
complete pattern (it bundles a local stub so it runs with no account).

# sshPilot Plugin SDK

Write a plugin that adds protocols, UI pages, background workflows (e.g. VPS
provisioning), and reactions to application activity — using only the public
API. This guide is the contract: everything documented here is stable within
an API major version; everything else under `sshpilot.*` is private and may
change without notice.

- **The only import you need:** `sshpilot.plugins.api`.
- **Current API version:** `1.2` (see [Versioning](#versioning)).
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
| Store/read credentials (keyring) | `ctx.secrets.get/set/delete(key)` |
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
  "api_version": 1
}
```

- `id` — unique, stable; used for secret/setting namespacing and the enable list. No `/`.
- `name` — shown in Preferences ▸ Plugins.
- `api_version` — the API **major** version you target (currently `1`). The loader refuses plugins whose major version doesn't match the running app.

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
- `open_connection(nickname: str) -> bool` — open a terminal tab for an existing connection. Returns `False` if unknown / UI not ready. *(after `app_started`)*
- `generate_key(name: str, *, key_type="ed25519", key_size=3072, comment=None, passphrase=None) -> str | None` — generate an SSH key; returns the private-key path or `None`. *(after `app_started`)*

### UI — `ctx.ui`
- `register_page(page_id, title, icon_name, factory)` — `factory` is a zero-arg callable returning a `Gtk.Widget`, built on first open. The page appears under the **Tools** section of the main menu. *(safe in `activate`)*
- `open_page(page_id)` — open or focus the page as a tab. *(after `app_started`)*
- `notify(message, timeout=3)` — transient in-app toast. *(after `app_started`)*

### Events — `ctx.events`
- `subscribe(event, callback)` / `unsubscribe(event, callback)` — see [Events](#5-events). Constants are also on `ctx.events` (e.g. `ctx.events.APP_STARTED`).

### Secrets — `ctx.secrets` (keyring-backed, scoped to your plugin id)
- `get(key) -> str | None`, `set(key, value)`, `delete(key) -> bool`. Use for credentials/tokens. No other plugin can read your secrets.

### Settings — `ctx.settings` (app config, scoped to your plugin id)
- `get(key, default=None)`, `set(key, value)`. For non-secret preferences; stored under `plugins.<id>.<key>`.

### Threading
- `run_on_ui_thread(fn, *args)` — run `fn(*args)` on the GTK main thread. Use to return from a background worker before touching UI or calling `add_connection`/`open_connection`.

### Advanced (escape hatch)
- `ctx.config`, `ctx.connection_manager` — live internal objects. Stable enough that the built-in backends use them, but prefer the named APIs; treat these as advanced.

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

### Payloads

Read-only frozen snapshots — decoupled from internal objects, safe to keep.

```python
ConnectionInfo(nickname: str, host: str, username: str, protocol: str, port: int)
SessionInfo(connection: ConnectionInfo, session_id: str)
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
ctx.secrets.set("api_token", token)        # keyring (libsecret / OS keychain)
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

- `API_VERSION = (major, minor)`, currently `(1, 2)`. Your manifest declares the
  **major** you target; the loader skips plugins whose major doesn't match.
- Minor bumps are additive (new methods/events); your plugin keeps working.
- New since `1.0`: `1.1` plugin secrets; `1.2` events, UI extension,
  `open_connection`, `generate_key`, scoped `ctx.secrets`/`ctx.settings`,
  `ctx.run_on_ui_thread`, `ctx.plugin_id`.

---

## 10. Do / Don't

- **Do** import only from `sshpilot.plugins.api`.
- **Do** keep `activate` to registration; do live work after `app_started`.
- **Do** keep network/slow work off the UI thread.
- **Don't** touch the main window, the internal `Connection` class, private
  modules, or GObject signals directly — use events and `PluginContext`.
- **Don't** store secrets in `settings`.

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
- **Run the CLI off the UI thread** (`threading.Thread` + `subprocess.run(..., timeout=…)`), then marshal results back with `ctx.run_on_ui_thread`. `build_spawn` itself must not block — it only assembles argv.
- **Parse defensively.** Prefer `--output json` / `--json`, but tolerate missing/renamed fields; don't hard-assert a schema.
- **Flatpak:** inside the sandbox the host CLI isn't on `PATH`. Detect `os.path.exists("/.flatpak-info")` and prefix calls with `["flatpak-spawn", "--host"]` — both your page's `subprocess` calls **and** the `build_spawn` argv (the terminal child is sandboxed too). sshPilot's manifest already grants `--talk-name=org.freedesktop.Flatpak`.
- **Let the CLI own its credentials.** If the tool keychains its own token (e.g. `easyenv auth login`), detect state (`auth whoami`) and optionally drive login; don't duplicate the token in `ctx.secrets`.

See [`easyenv_workspaces`](sshpilot/plugins/examples/easyenv_workspaces/) for the
complete pattern (it bundles a local stub so it runs with no account).

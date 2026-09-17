# Writing sshPilot plugins

sshPilot is extensible through plugins. A plugin is a small Python package that
the app loads at startup and lets register new **protocols** (selectable in the
connection dialog, spawned in the terminal) and/or new **UI pages** (a tab opened
from the **Tools** section of the main menu), using a stable, versioned API.

This guide covers what a plugin is, how to write and install one, the API
surface, versioning, and the security model. For a ready-to-fork starting point
use the [**sshpilot-plugin-template**](https://github.com/mfat/sshpilot-plugin-template)
repo ("Use this template") — also mirrored at [`template/`](template/) for a
**protocol** backend and [`template-ui/`](template-ui/) for an **event/UI**
plugin; publish via the [discovery index](registry.md)
([mfat/sshpilot-plugins](https://github.com/mfat/sshpilot-plugins)). For worked
examples read the built-in
`src/sshpilot/plugins/builtin/telnet_protocol/` (a tiny protocol), the shipped
examples `src/sshpilot/plugins/examples/mock_vps/` and `easyenv_workspaces/`, and the
official non-protocol plugins in [the plugins repo](https://github.com/mfat/sshpilot-plugins) (auto-group, notes,
health).

## Quickstart

Zero to a loaded plugin in six steps. Start from the template that matches what
you're building — [`template/`](template/) for a **protocol** (a command run in
the terminal, e.g. a new transport) or [`template-ui/`](template-ui/) for an
**event/UI** plugin (a page, reacting to connections/sessions).

```sh
# 1. Scaffold from a template (pick one).
cp -r docs/plugins/template-ui ~/my-plugin        # or docs/plugins/template

# 2. Edit my-plugin/plugin.json — set a unique "id" (also the install dir name)
#    and "name". Edit my-plugin/__init__.py — keep `class Plugin(SshPilotPlugin)`
#    and replace the body with your page/protocol.

# 3. Install it into the user plugin dir (name the dir after the manifest id).
cp -r ~/my-plugin ~/.local/share/sshpilot/plugins/my-plugin
#    Flatpak: ~/.var/app/io.github.mfat.sshpilot/data/sshpilot/plugins/my-plugin

# 4. Run the tests (no GTK needed — logic is plain Python).
cd ~/my-plugin && pip install pytest \
  && pip install "sshpilot @ git+https://github.com/mfat/sshpilot" --no-deps \
  && pytest -ra
```

5. Launch sshPilot, open **Preferences ▸ Plugins**, toggle your plugin on
   (accept the permissions prompt), and **restart** — user plugins load at
   startup.
6. Your protocol appears in the connection dialog's protocol dropdown, or your
   page appears under **Tools** in the main menu. That's it.

### Developing & iterating

There is **no hot-reload**: the loader imports enabled user plugins once at
startup ([loader.py](../../src/sshpilot/plugins/loader.py)). The dev loop is therefore
**edit → copy into the plugin dir → restart sshPilot**. Keep your pure logic in
module-level functions/classes (no `gi` import there) and import `gi` lazily
inside the page factory — then `pytest` covers most changes without a restart, and
you only restart to exercise the live UI. If your plugin doesn't appear, run the
app from a terminal: a plugin that raises during import/activate is logged and
skipped (it won't crash the app).

## The two tiers

| | Built-in | User / third-party |
|---|---|---|
| Location | `src/sshpilot/plugins/builtin/<id>/` (in the app) | `$XDG_DATA_HOME/sshpilot/plugins/<id>/` |
| Loading | auto-loaded; disable in Preferences | **opt-in**: must be enabled in Preferences |
| Audience | first-party, broadly useful, reviewed | provider/community plugins |
| Ships in | the app package | published/installed by the author/user |

User plugin directory:
- Normal install: `~/.local/share/sshpilot/plugins/<id>/`
  (`$XDG_DATA_HOME/sshpilot/plugins/<id>/` if set).
- Flatpak: `~/.var/app/io.github.mfat.sshpilot/data/sshpilot/plugins/<id>/`.

> Want your plugin in core? See [CONTRIBUTING](../../CONTRIBUTING.md#plugins) for
> the bar a built-in must meet. Otherwise publish it as its own repo (see the
> [template](template/)) and we'll link it from
> [community.md](community.md).

## Anatomy

A plugin is a directory with two files:

```
my-plugin/
├── plugin.json      # manifest (metadata only; no code imported to read it)
└── __init__.py      # exposes `class Plugin(SshPilotPlugin)`
```

### `plugin.json`

```json
{
  "id": "my-plugin",
  "name": "My Plugin",
  "version": "1.0.0",
  "homepage": "https://github.com/myuser/my-plugin",
  "api_version": 1
}
```

Fields (schema: [`plugin.schema.json`](plugin.schema.json)):

- **`id`** (required) — stable unique id, and the keyring/settings namespace.
  It is also the directory name the in-app installer uses; when you copy a plugin
  in by hand, name the directory after the id so the two never drift. It is *not*
  the connection's `protocol` value — that comes from your backend's
  `protocol_id` (see [Protocol plugins](#protocol-plugins--protocolbackend)),
  and one plugin may register several.
- **`api_version`** (required, integer) — the **major** API version you target.
  The app loads the plugin only if this equals its `API_VERSION[0]`; otherwise it
  is skipped and shown as *Incompatible* in Preferences.
- `name` — shown in Preferences ▸ Plugins.
- `version` — your plugin's version, e.g. `"1.2.0"` (dotted integers; match your
  release tag). **Recommended:** it drives the in-app **update button** — sshPilot
  compares it against the registry's latest version and offers an update when
  yours is newer. Omit it and the plugin simply never shows an update prompt.
- `homepage` — URL of your plugin's source/homepage. Shown as a clickable
  link in the per-plugin **info** dialog in Preferences ▸ Plugins.
- `permissions` — capabilities your plugin uses (see below). Declare every one;
  they're shown to the user before they enable/install your plugin.
- `builtin` / `required` — for in-app built-ins only (don't set these in a
  third-party plugin). `entry` is accepted but **ignored** — the loader always
  instantiates the class named `Plugin`.

### Permissions

Declare what your plugin does so users give informed consent (plugins are
unsandboxed — see [Security](#security--trust)). These are **displayed** in
Preferences and at install/enable today; enforcement may come later. Declare
every capability you actually use:

| Permission | Used when your plugin… |
|------------|------------------------|
| `network` | opens network connections (HTTP, sockets) |
| `filesystem` | reads/writes files outside its own directory |
| `keyring` | stores/reads secrets via `ctx.secrets` |
| `connections` | creates/updates/opens sshPilot connections or reads `~/.ssh/config` |
| `process` | spawns external processes / terminal commands (e.g. `build_spawn`) |
| `ui` | adds pages or other UI via `ctx.ui` |
| `settings` | reads/writes app or plugin settings via `ctx.settings` |

```json
{ "id": "my-plugin", "name": "My Plugin", "api_version": 1,
  "permissions": ["network", "keyring"] }
```

### `__init__.py`

```python
from sshpilot.plugins.api import PluginContext, SshPilotPlugin

class Plugin(SshPilotPlugin):
    def activate(self, ctx: PluginContext) -> None:
        # register protocols / pages / event subscriptions here
        ...
```

`activate()` is called once at load with a per-plugin `PluginContext`.

## The API surface (`sshpilot.plugins.api`)

Everything below is imported from `sshpilot.plugins.api`. The module docstring is
the authoritative reference; this is the practical map.

### `PluginContext` (the `ctx`)
- `ctx.plugin_id` — your id.
- `ctx.register_protocol(backend)` — register a `ProtocolBackend`.
- **Connections:** `ctx.add_connection(data)`, `ctx.update_connection(nickname, data)`,
  `ctx.open_connection(nickname)`, `ctx.list_connections()` → a read-only
  `ConnectionInfo` snapshot of every saved connection (API ≥ 1.4).
- **Groups:** `ctx.create_group(name)`, `ctx.add_connection_to_group(nickname, group_id)`,
  `ctx.add_connection_group(...)`.
- **Secrets/settings (per-plugin, namespaced):** `ctx.secrets.get/set/delete`
  (the user's configured secret backend — libsecret/OS keychain, `pass`,
  Bitwarden…, never chosen by the plugin), `ctx.settings.get/set` (app config).
- **Identities (read-only, API ≥ 1.10):** `ctx.identities.list()` → the keys the
  system ssh-agent currently exposes, as `Identity` objects;
  `ctx.identities.is_agent_available()`. Both answer from daemon-owned state and
  degrade quietly — an empty list / `False` when the daemon or agent can't be
  reached, never an exception.
- **UI:** `ctx.ui.register_page(page_id, title, icon_name, factory,
  add_menu_item=True, on_activate=None)`, `ctx.ui.open_page(page_id)`,
  `ctx.ui.notify(message, timeout=3)`;
  `ctx.ui.register_connection_action(action_id, label, icon_name, callback)`
  (API ≥ 1.7) adds an item to a connection's right-click menu in the sidebar —
  `callback` receives the right-clicked connection's nickname (e.g. open a plugin
  page targeting that host); `ctx.ui.open_web_tab(url, title=None)` (API ≥ 1.12)
  shows a URL in an embedded WebKit tab, falling back to the system browser.
  `add_menu_item=False` / `on_activate=` (API ≥ 1.8) are covered under
  [UI-page plugins](#ui-page-plugins).
- **Events:** `ctx.events.subscribe(Events.X, callback)` —
  `APP_STARTED`, `APP_SHUTDOWN`, `CONNECTION_CREATED/UPDATED/DELETED`,
  `SESSION_OPENED/CLOSED`.
- **Helpers:** `ctx.run_on_ui_thread(fn, *args)` (always marshal UI work from
  worker threads), `ctx.generate_key(name, key_type=…, key_size=…, comment=…,
  encrypted=…)`.
- **Remote commands (API ≥ 1.5):** `ctx.run_command(nickname, command)`
  runs a one-shot command on a saved host and returns a `CommandResult`
  (`exit_code`/`stdout`/`stderr`) — it goes through the daemon's own SSH/auth
  path, so `~/.ssh/config`, ProxyJump and stored credentials all apply. Optional
  `input=` writes to the remote command's stdin (e.g. a password for `sudo -S`).
  `run_command` blocks — call it from a worker thread and marshal results back
  with `ctx.run_on_ui_thread`.
- **Streamed/interactive commands (API ≥ 1.6):**
  `ctx.open_command_terminal(nickname, remote_command, title=None,
  pty_prompt=None, pty_response=None)` opens a new terminal tab that runs
  `remote_command` on the host — over the same native SSH/auth path, no new
  transport. Use it for streamed or interactive output `run_command` can't show
  (e.g. `docker logs -f`, `docker exec -it <c> sh`, `top`). Optional
  `pty_prompt`/`pty_response` auto-type a one-shot response when the prompt
  appears in the terminal (e.g. a remote sudo password). Returns `False` if the
  connection is unknown / command empty / UI not ready.
- **Local commands (API ≥ 1.11):** `ctx.run_local_command(command)` (captured,
  blocking) and `ctx.open_local_command_terminal(command, title=…)` (a local
  terminal tab) run on the *local* machine, and are Flatpak-host aware — inside
  the sandbox they go through `flatpak-spawn --host`. Anything remote must keep
  using `run_command` / `open_command_terminal` so it stays on the shared SSH
  and auth path.
- **Line streams (API ≥ 1.13):** `ctx.run_command_stream(nickname, command,
  on_line=…, on_done=…)` and `ctx.run_local_command_stream(command, on_line=…,
  on_done=…)` start a long-lived command and deliver output **lines** to
  `on_line` (already marshalled onto the UI thread). There is no timeout: they
  return a `StreamHandle`, and you call `handle.stop()` when your surface goes
  away (page unmap, host change). Use them when you want the lines in a widget;
  use `open_command_terminal` when a real VTE tab is the right home.
- **Local port forwards (API ≥ 1.12):** `ctx.ensure_local_forward(nickname,
  remote_port)` returns a local TCP port forwarded to `localhost:remote_port` on
  the host, established through the daemon's forwarding service. **Blocking**,
  and raises `RuntimeError` if the forward can't be opened — pair it with
  `ctx.ui.open_web_tab()` to surface a remote web UI.
- **Keys (API ≥ 1.5):** `ctx.list_keys()` → `[{"private_path", "public_path"}]`,
  `ctx.delete_key(private_path)` (resolved against the daemon's key list).
- **Terminals/sessions (API ≥ 1.5):** `ctx.list_sessions()` →
  `SessionInfo` list; `ctx.read_terminal(session_id, max_chars=None)` reads a
  session's text; `ctx.send_terminal(session_id, text)` sends input.
- **Files & HTTP (API ≥ 1.5):** `ctx.data_dir` is a private persistent dir;
  `ctx.files.read_text/write_text/read_bytes/write_bytes/exists/path` are
  sandboxed to it (escapes rejected). `ctx.http.get/post` is a minimal blocking
  HTTP client (call off the UI thread).
- **Return types (API ≥ 1.5):** `run_command` → `CommandResult(exit_code, stdout,
  stderr)` with `.ok` (`exit_code == 0`; `-1` means it couldn't be launched);
  `ctx.http` calls → `HttpResponse(status, text, headers)` with `.json()` and
  `.ok` (2xx).
- **Escape hatch:** `ctx.daemon_client()` returns the live typed daemon client
  (or `None`). Use it only for things the named `ctx` operations don't cover —
  e.g. the protected secret operation behind the password dialog below. Anything
  reachable through a named `ctx` method should go through that method instead.

> **Connection reuse is not yours to manage.** `ctx.acquire_multiplex(nickname)` /
> `ctx.release_multiplex(nickname)` shipped in API 1.9 as an explicit
> ControlMaster hint. Since 1.14 the daemon owns transport reuse, and both are
> retained **only as no-ops** so 1.9-era plugins keep importing and running.
> Repeated `run_command` calls are pooled for you — delete the calls and drop
> the `map`/`unmap` bookkeeping; don't write new code against them.

> **A note on power.** Plugins run in-process with full privileges; the
> declared `permissions` are advisory (shown for transparency, not enforced).
> The real safety boundary is **review/vetting before a plugin enters the
> registry** — declare every capability you use so reviewers and users can see
> it.

### Protocol plugins — `ProtocolBackend`
Subclass and implement:
- `protocol_id` / `display_name` / `default_port` — `protocol_id` is what lands
  in the connection's `protocol` field and is independent of your plugin's
  manifest `id` (the [template](template/) is plugin `example-plugin` registering
  protocol `example`). It must be unique app-wide: the first registration of a
  given id wins, so a plugin cannot shadow a built-in.
- `capabilities() -> frozenset[Capability]` — gates SSH-only UI; return
  `frozenset()` for a plain terminal protocol.
- `connection_fields() -> list[FieldSpec]` — declarative editor fields; the
  dialog renders them and persists values into the connection's data.
- `validate(data) -> list[str]` — human-readable errors (empty = ok).
- `build_spawn(connection, ctx) -> SpawnSpec` — return the command to run in the
  VTE terminal. **Must not block on the network.** Raise `ProtocolError` (e.g.
  when the required binary is missing).

`SpawnSpec(argv, env=..., working_directory=..., extras=...)` — `argv` is the
command + args run inside the terminal; `env` is the child environment.

`FieldSpec(key, label, kind=..., default=..., choices=..., placeholder=...,
required=..., group=...)` — `kind` is one of `text|int|password|file|choice|switch`;
`group` puts fields into a labelled section (e.g. `"advanced"`).

See `builtin/telnet_protocol/__init__.py` (minimal) and
`builtin/{ssh,serial,docker,kubernetes,mosh}_protocol/` for real backends — note
their directory names carry a `_protocol` suffix that the manifest `id` does not
(`docker_protocol/` declares `"id": "docker"`), which built-ins may do because
they are imported as packages rather than found by directory name. A protocol
runs as a **command inside the terminal** — GUI protocols (RDP/VNC) are not
expressible today.

**Declare your `protocol_id`s in the manifest.** Session launch is owned by the daemon,
which runs in its own process and so has to activate your plugin itself to
reach `build_spawn`. Listing them lets it load yours and skip everything else:

```json
{ "id": "acme-ssm", "api_version": 1, "protocols": ["ssm"] }
```

Optional — without it the daemon sweeps enabled plugins in id order until the
protocol resolves, which still works but may import unrelated plugins first.
`activate()` runs a second time in that process, with no window: `ctx.ui`
registrations are accepted and discarded, and daemon-backed calls fail (there is
no client to reach — this process *is* the daemon). Keep `activate()` to
`register_protocol` and your `build_spawn` free of anything that needs the UI.

`build_spawn` gets a host-less context built by `PluginContext.for_spawn`:
`ctx.ui` and `ctx.events` are `None`, and `ctx.secrets` / `ctx.settings` are
**not available** — the daemon builds that context without a backend, so both
raise. Derive everything from `connection.data` (use `connection_fields` for
per-connection options) and the environment. Credentials don't belong in
`connection.data`; let the program you launch obtain its own.

### UI-page plugins
Register a page that builds a GTK widget on demand:
```python
ctx.ui.register_page("dashboard", "My Dashboard",
                     "network-server-symbolic", self._build_page)
```
The page gets an entry in the main menu's **Tools** section that opens it as a
tab. Two keyword arguments (API ≥ 1.8) change that:

- `add_menu_item=False` — register the page with **no** menu entry, for pages you
  open yourself with `ctx.ui.open_page(...)` (e.g. one tab per host, which would
  otherwise clutter the menu).
- `on_activate=callback` — the menu entry calls `callback()` instead of opening
  *this* page; use it to pick a per-host page at click time. `open_page` honours
  it too, so the menu, `open_page` and the Preferences gear all behave alike.

Page ids are namespaced per plugin, so two plugins can both register `"dashboard"`.
The widget is built once, on first open, and then cached — see
[Keeping a page fresh](#keeping-a-page-fresh). A factory that raises is logged and
surfaced as a toast; it can't take the app down.

`examples/easyenv_workspaces/` is a full page-based plugin (sign-in, credit,
a card grid, a two-step create dialog with templates, stacks and sizes, and
connections reached through a jump host), and the built-in `builtin/docker_manager/` ("Docker
Console") is a first-party page plugin with per-host tabs and streaming.

## Event-driven & UI plugins

A plugin doesn't have to add a protocol. It can react to what happens in the app
and contribute pages. The official plugins in [the plugins repo](https://github.com/mfat/sshpilot-plugins) are the
worked examples for this section:

| Plugin | Shows |
|--------|-------|
| `auto-group` | reacting to `CONNECTION_CREATED`; creating/assigning groups; `list_connections()` backfill |
| `inventory-import` | parsing inventory files off-thread; diff vs `list_connections()`; bulk `add_connection` / groups |
| `notes` | structured `ctx.settings`; pruning on `CONNECTION_DELETED` |
| `health` | background workers + `run_on_ui_thread`; clean shutdown |
| `session-log` | `SESSION_OPENED`/`SESSION_CLOSED` bookkeeping; CSV export |
| `runbook` | per-connection settings; clipboard copy; rename reconcile (it copies rather than types; `ctx.send_terminal` would let a plugin push straight into a session) |
| `key-audit` | `~/.ssh` scan + `ssh-keygen` subprocess parsing; startup warning toast |
| `tailscale` | CLI subprocess (`tailscale status --json`, Flatpak host-spawn); add/dedup connections |
| `hetzner` | stdlib `urllib` HTTPS API; token in `ctx.secrets`; sign-in → list → add |

For a **protocol** plugin (not event/UI), see `aws-ssm` in
[the plugins repo](https://github.com/mfat/sshpilot-plugins) (a `ProtocolBackend` that runs `aws ssm
start-session`) alongside the built-in `telnet_protocol` and the protocol
[`template/`](template/).

### Lifecycle: register in `activate`, act in callbacks

`activate(ctx)` runs **before the main window exists and before the daemon
backend is reachable** — do registration only (subscribe to events,
`register_page`). Anything that touches live UI, connections, or keys
(`ui.open_page`/`notify`, `open_connection`, `generate_key`, groups) is valid
only **after the `APP_STARTED` event**; early `ctx.ui.*` calls are queued for
you, but don't, say, open a connection from `activate`. Event callbacks and page
factories always run after the window is up, so they're the right place for real
work.

Daemon-owned state has the same rule: `ctx.settings`, `ctx.secrets`,
`add_connection`/`update_connection` and key operations raise during `activate` —
`BackendUnavailable` from `ctx.settings`, a plain `RuntimeError` from the others,
both subclasses of `RuntimeError`, so catch that if you need to be tolerant.
(`ctx.identities` is the exception: it degrades to an empty list rather than
raising.) `APP_STARTED` is held until the daemon client resolves, so it is the
earliest safe point — read your persisted settings there, not in `activate`.

Key operations need one thing more than the client: the daemon-confirmed
operation mode, which can land a moment *after* `APP_STARTED`. `list_keys()` can
still return `[]` on the event itself, so drive key work from a user action
rather than straight off `APP_STARTED`.

### Events and their payloads

`ctx.events.subscribe(Events.X, callback)`; subscriptions are removed for you on
unload. Callbacks run **synchronously on the UI thread** and are isolated (one
plugin raising won't break others). Payloads are frozen snapshots:

| Event | Payload |
|-------|---------|
| `APP_STARTED` / `APP_SHUTDOWN` | `None` |
| `CONNECTION_CREATED` / `CONNECTION_UPDATED` / `CONNECTION_DELETED` | `ConnectionInfo` (`nickname`, `host`, `username`, `protocol`, `port`) |
| `SESSION_OPENED` / `SESSION_CLOSED` | `SessionInfo` (`.connection`, `.session_id`) |

> **Rename caveat:** `CONNECTION_UPDATED` carries only the connection's *current*
> nickname — there's no "previous nickname". If you key data by nickname (like
> `notes` does), you can't migrate it automatically on rename; reconcile against
> `list_connections()` instead (e.g. drop entries whose nickname no longer
> exists). `notes` does this when its page opens.

### Keeping a page fresh

A registered page's widget is **built once** (the factory is called on first
open) and cached. To reflect changes, update the widget from event callbacks or a
timer rather than rebuilding the page — hold references to the labels/rows you
need to mutate. `health` rebuilds its list rows on each tick; `auto-group`
rebuilds only its rules list when you add/remove a rule.

### Settings & secrets storage

`ctx.settings.get(key, default)` / `set(key, value)` persist under
`plugins.<id>.<key>` in the app config and must be **JSON-serializable** — plain
`dict`/`list`/`str`/`int`/`bool`/`None`. Store structured state as one value
(`notes` keeps a `{nickname: text}` dict) and treat what you read back
defensively (it round-tripped through JSON). `ctx.secrets` is for sensitive
strings (tokens, passwords); never put secrets in `settings`.

`ctx.secrets` writes to whichever secret backend the *user* configured
(`secrets.backend`: libsecret — which also covers KeePassXC via the Secret
Service — an OS keychain, `pass`, Bitwarden/Vaultwarden, or a registered custom
backend). Your plugin doesn't pick one and the API is identical either way, but
a locked session-backed vault (or the "don't store" backend) means `get` can
return `None` and `set` can fail — handle a missing secret gracefully instead of
assuming it round-trips.

Both are daemon-owned, so both raise before `APP_STARTED`.
`ctx.settings.get()` **raises rather than returning your `default`** when the
backend isn't up: a silent default reads as "unset", and a plugin that loads a
whole store at startup and writes it back on the next edit would erase it.

### SSH password prompts (in-app)

If your plugin needs to ask the user for an **SSH login password** in the GUI
(do not build a custom password dialog — it will stack incorrectly on Wayland),
use the shared helper documented in **[PLUGIN_SDK.md § Advanced UI — credential dialogs](../PLUGIN_SDK.md#advanced-ui--credential-dialogs)**:

`from sshpilot.window import show_ssh_password_dialog` — call on the **UI thread
only** (or inside `ctx.run_on_ui_thread`). Pass `from_widget=your_page_widget`
and disable storage in the dialog; if persistence is required, use the typed
daemon client from `ctx.daemon_client()` and its protected secret operation.

### Background work & clean shutdown

Do network or other slow work **off the UI thread**, then marshal results back
with `ctx.run_on_ui_thread(fn, *args)` (it runs inline if already on the main
thread). Any thread you start must stop when the plugin goes away: keep a
`threading.Event` stop flag, set it in **both** `deactivate()` and an
`APP_SHUTDOWN` handler (either may fire first — make stop idempotent), and use
`stop.wait(interval)` instead of `time.sleep` so the loop exits promptly. `health`
is the reference: a `ThreadPoolExecutor` for probes, a monitor thread that wakes
on `stop`, and `_shutdown()` wired to both teardown paths.

### Debugging

Use the stdlib `logging` module (`logger = logging.getLogger(__name__)`); output
goes to sshPilot's log (run the app from a terminal to see it; raise verbosity in
**Preferences ▸ Advanced**). A plugin that raises in `activate` is logged and
skipped without taking the app down, so check the log if your plugin doesn't
appear. Keep the pure logic in module-level functions/classes with no `gi`
import and import `gi` lazily inside the page factory — then you can unit-test the
logic without a display (see each plugin's `tests/`).

## Installing a plugin (as a user)

- **From the registry (easiest):** open **Preferences ▸ Plugins**; registry
  plugins appear under *Available Plugins*. Toggle one on — sshPilot downloads it,
  verifies its SHA-256, shows the permissions/trust prompt, installs and enables
  it. Restart to load. (See [registry.md](registry.md).)
- **Manually:** either copy the plugin directory into the user plugin dir (paths
  above), then enable it in **Preferences ▸ Plugins** and restart; or use
  **Preferences ▸ Plugins ▸ Install plugin…** (folder or `.zip`), which copies it
  to `<user plugin dir>/<manifest id>/`, shows the same permissions prompt and
  **enables it for you** — only the restart is left. Either way the manifest's
  `api_version` major must match the app's, and the id must not collide with a
  built-in.

## API versioning & stability

`API_VERSION = (major, minor)` — exported from `sshpilot.plugins.api` and defined
in `src/sshpilot/core/plugins/contracts.py`. It is currently **`(1, 14)`**; the
per-minor changelog is the comment block at the top of
`src/sshpilot/plugins/api.py`.

- **Minor** bumps are additive (new methods/fields) — existing plugins keep
  working.
- **Major** bumps are breaking. The loader compares your `plugin.json`
  `api_version` to the app's **major** and refuses to load on a mismatch (shown
  *Incompatible* in Preferences), so a broken plugin never silently misbehaves.
- The loader checks **only the major**. Calling a method added in a newer minor
  on an older app therefore fails at call time, not at load — target the minor
  you actually test against, or guard with `hasattr`/`try: except AttributeError`.
- Target the major you build against; bump it when you adopt a new major and
  test against it. Breaking changes are documented in the `api.py` changelog
  comment.

## Security & trust

Plugins run **in-process with full application privileges** — there is no
sandbox. A plugin can do anything the app can (filesystem, network, keyring,
spawning processes).

- **Built-ins** are vetted as part of the app.
- **User plugins are opt-in**: a plugin on disk does nothing until you enable it.
  Only install plugins you trust, from sources you trust.
- Per-plugin `ctx.secrets`/`ctx.settings` are namespaced so one plugin can't read
  another's stored data, but this is organizational, not a security boundary
  against malicious code.

## Testing

Plugin logic (`build_spawn` argv, `validate`, field specs) is plain Python and
unit-testable without GTK — see `tests/test_telnet_plugin.py` for the pattern
(monkeypatch `shutil.which`, assert the argv). The [template](template/) ships a
test + CI workflow you can build on.

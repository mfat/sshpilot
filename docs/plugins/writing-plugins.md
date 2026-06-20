# Writing sshPilot plugins

sshPilot is extensible through plugins. A plugin is a small Python package that
the app loads at startup and lets register new **protocols** (selectable in the
connection dialog, spawned in the terminal) and/or new **UI pages** (a tab under
the app menu), using a stable, versioned API.

This guide covers what a plugin is, how to write and install one, the API
surface, versioning, and the security model. For a ready-to-fork starting point
use the [**sshpilot-plugin-template**](https://github.com/mfat/sshpilot-plugin-template)
repo ("Use this template") — also mirrored at [`template/`](template/) for a
**protocol** backend and [`template-ui/`](template-ui/) for an **event/UI**
plugin; publish via the [discovery index](registry.md)
([mfat/sshpilot-plugins](https://github.com/mfat/sshpilot-plugins)). For worked
examples read the built-in
`sshpilot/plugins/builtin/telnet_protocol/` (a tiny protocol), the shipped
examples `sshpilot/plugins/examples/mock_vps/` and `easyenv_workspaces/`, and the
official non-protocol plugins in [`plugins/`](../../plugins/) (auto-group, notes,
health).

## The two tiers

| | Built-in | User / third-party |
|---|---|---|
| Location | `sshpilot/plugins/builtin/<id>/` (in the app) | `$XDG_DATA_HOME/sshpilot/plugins/<id>/` |
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
  "api_version": 1
}
```

Fields (schema: [`plugin.schema.json`](plugin.schema.json)):

- **`id`** (required) — stable unique id. Also the directory name, the
  `protocol` value for protocol plugins, and the keyring/settings namespace.
- **`api_version`** (required, integer) — the **major** API version you target.
  The app loads the plugin only if this equals its `API_VERSION[0]`; otherwise it
  is skipped and shown as *Incompatible* in Preferences.
- `name` — shown in Preferences ▸ Plugins.
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
  (OS keyring), `ctx.settings.get/set` (app config).
- **UI:** `ctx.ui.register_page(page_id, title, icon_name, factory)`,
  `ctx.ui.open_page(page_id)`, `ctx.ui.notify(message)`.
- **Events:** `ctx.events.subscribe(Events.X, callback)` —
  `APP_STARTED`, `APP_SHUTDOWN`, `CONNECTION_CREATED/UPDATED/DELETED`,
  `SESSION_OPENED/CLOSED`.
- **Helpers:** `ctx.run_on_ui_thread(fn, *args)` (always marshal UI work from
  worker threads), `ctx.generate_key(...)`.

### Protocol plugins — `ProtocolBackend`
Subclass and implement:
- `protocol_id` / `display_name` / `default_port`
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
`builtin/{serial,docker,kubernetes,mosh}_protocol/` for real backends. A protocol
runs as a **command inside the terminal** — GUI protocols (RDP/VNC) are not
expressible today.

### UI-page plugins
Register a page that builds a GTK widget on demand:
```python
ctx.ui.register_page("dashboard", "My Dashboard",
                     "network-server-symbolic", self._build_page)
```
`examples/easyenv_workspaces/` is a full page-based plugin (sign-in, list,
create, open connections).

## Event-driven & UI plugins

A plugin doesn't have to add a protocol. It can react to what happens in the app
and contribute pages. The three official non-protocol plugins in
[`plugins/`](../../plugins/) are the worked examples for this section:

| Plugin | Shows |
|--------|-------|
| `auto-group` | reacting to `CONNECTION_CREATED`; creating/assigning groups; `list_connections()` backfill |
| `notes` | structured `ctx.settings`; pruning on `CONNECTION_DELETED` |
| `health` | background workers + `run_on_ui_thread`; clean shutdown |

### Lifecycle: register in `activate`, act in callbacks

`activate(ctx)` runs **before the main window exists** — do registration only
(subscribe to events, `register_page`, read settings). Anything that touches live
UI, connections, or keys (`ui.open_page`/`notify`, `open_connection`,
`generate_key`, groups) is valid only **after the `APP_STARTED` event**; early
`ctx.ui.*` calls are queued for you, but don't, say, open a connection from
`activate`. Event callbacks and page factories always run after the window is up,
so they're the right place for real work.

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
defensively (it round-tripped through JSON). `ctx.secrets` is the OS keyring for
sensitive strings (tokens, passwords); never put secrets in `settings`.

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
- **Manually:** copy the plugin directory into the user plugin dir (paths above)
  or use **Preferences ▸ Plugins ▸ Install plugin…** (folder or `.zip`), enable
  it, and restart.

## API versioning & stability

`API_VERSION = (major, minor)` in `sshpilot/plugins/api.py`.

- **Minor** bumps are additive (new methods/fields) — existing plugins keep
  working.
- **Major** bumps are breaking. The loader compares your `plugin.json`
  `api_version` to the app's **major** and refuses to load on a mismatch (shown
  *Incompatible* in Preferences), so a broken plugin never silently misbehaves.
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

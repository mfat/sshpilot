# Writing sshPilot plugins

sshPilot is extensible through plugins. A plugin is a small Python package that
the app loads at startup and lets register new **protocols** (selectable in the
connection dialog, spawned in the terminal) and/or new **UI pages** (a tab under
the app menu), using a stable, versioned API.

This guide covers what a plugin is, how to write and install one, the API
surface, versioning, and the security model. For a ready-to-fork starting point
see [`template/`](template/); for worked examples read the built-in
`sshpilot/plugins/builtin/telnet_protocol/` (a tiny protocol) and the shipped
examples `sshpilot/plugins/examples/mock_vps/` and `easyenv_workspaces/`.

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
- `builtin` / `required` — for in-app built-ins only (don't set these in a
  third-party plugin). `entry` is accepted but **ignored** — the loader always
  instantiates the class named `Plugin`.

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
  `ctx.open_connection(nickname)`.
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

## Installing a plugin (as a user)

1. Copy the plugin directory into the user plugin dir (paths above), **or** use
   **Preferences ▸ Plugins ▸ Install plugin…** (pick a folder or `.zip`).
2. Enable it in **Preferences ▸ Plugins**.
3. Restart sshPilot.

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

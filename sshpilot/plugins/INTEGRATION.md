# Wiring the plugin system into sshPilot

The `sshpilot/plugins/` package is self-contained and verified to load
against the current codebase. Four small changes wire it in.

## 1. Load plugins at startup

Wherever the app creates its `ConnectionManager` (before the main window
can spawn a terminal):

```python
from .plugins.loader import load_plugins

self.loaded_plugins = load_plugins(
    app_config=self.config,
    connection_manager=self.connection_manager,
)
```

`load_plugins` never lets a broken plugin crash the app, with one
exception: if plugin zero (SSH) fails it raises, because the app is
useless without it.

## 2. Give Connection a protocol field

In `connection_manager.py`, inside `Connection.__init__` (one line):

```python
self.protocol = data.get('protocol', 'ssh')
```

Defaulting to `'ssh'` means every existing saved connection, every
ssh_config-derived connection, and every code path that constructs a
`Connection` keeps working untouched. Persist the key alongside the
others when connections are saved.

## 3. The terminal seam

In `terminal.py`, `_setup_ssh_terminal()` currently reads
`self.connection.ssh_connection_cmd` directly. Replace that block with a
registry lookup:

```python
from .plugins.registry import protocol_registry
from .plugins.api import ProtocolError

backend = protocol_registry().get(getattr(self.connection, 'protocol', 'ssh'))
try:
    spec = backend.build_spawn(self.connection, self._plugin_ctx)
except ProtocolError as e:
    GLib.idle_add(self._on_connection_failed, str(e))
    return

ssh_cmd = list(spec.argv)
env = dict(spec.env)
use_askpass = bool(spec.extras.get('use_askpass'))
password_value = spec.extras.get('password')
# askpass env (from resolve_native_auth) delivers passphrases AND login passwords;
# MFA/OTP declined by the helper falls back to the VTE TTY.
```

Everything below that point (askpass log forwarding, `TERM`/`PATH`,
`self.backend.spawn_async(argv=ssh_cmd, ...)`) stays owned by `terminal.py`.
For the SSH backend the resulting argv/env come from
`build_ssh_connection()` via the protocol seam — a pure indirection.

`self._plugin_ctx` can be a single `PluginContext` created at startup and
passed down, or rebuilt cheaply per call; it only carries references.

Note the existing naming collision: `self.backend` in terminal.py is a
*terminal* backend (VTE vs. fallback, from `terminal_backends.py`).
Protocol backends are a different axis — keep the names distinct.

## 4. Gate SSH-only UI on capabilities (incremental)

Anywhere the UI assumes SSH (SFTP button, ssh-copy-id menu item, port
forwarding page), replace protocol string checks you would have written
with:

```python
from .plugins.api import Capability
if Capability.FILE_TRANSFER in backend.capabilities():
    ...
```

No urgency while SSH is the only protocol — do it opportunistically as
you touch those files, and mandatorily before the first non-SSH protocol
ships.

## Packaging notes

- **PyInstaller (macOS):** built-in plugins are normal submodules, but the
  loader imports them dynamically and needs `plugin.json` on disk. In
  `sshpilot.spec`: `hiddenimports += collect_submodules('sshpilot.plugins.builtin')`
  and add the manifests via `datas` (`collect_data_files('sshpilot.plugins',
  includes=['**/plugin.json'])`).
- **Flatpak:** nothing to do for built-ins. User plugins resolve to
  `~/.var/app/io.github.mfat.sshpilot/data/sshpilot/plugins` via
  `XDG_DATA_HOME` automatically.
- **setup.py / MANIFEST.in / Debian:** make sure `plugin.json` files are
  included as package data (`[tool.setuptools.package-data]` →
  `"sshpilot.plugins.builtin.*" = ["plugin.json"]`).

## Security posture (matters for the VPS partnership)

- User plugins are **opt-in by id** (`plugins.enabled` config list): a
  file appearing on disk is never enough to execute code.
- Built-in plugins are opt-out (`plugins.disabled`), except those marked
  `"required": true` (SSH).
- `PluginContext.get_secret/set_secret` are namespaced per plugin id —
  wire them to your existing keyring layer in phase 3 so the VPS plugin
  never touches raw keyring APIs or other plugins' secrets.

## Suggested phases

1. **This package + seams 1–3.** Pure refactor, zero behavior change,
   one release of soak time.
2. **Capability-gated UI + protocol selector** in the connection dialog
   (hidden or showing only SSH until a second protocol exists), rendering
   `FieldSpec` rows for non-SSH protocols.
3. **PluginContext secrets + `add_connection` wrapper** on
   ConnectionManager → build the VPS provider plugin as a built-in.
4. **Telnet or serial as the first non-SSH protocol** — doubles as the
   proof that the API is honest, and serial needs only pyserial + a
   handful of FieldSpecs.

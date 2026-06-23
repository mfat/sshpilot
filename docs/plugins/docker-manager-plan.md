# Docker Console plugin — design plan

Branch: `feature-docker-plugin`

## Context
sshpilot already ships a **`docker_protocol`** builtin plugin
(`sshpilot/plugins/builtin/docker_protocol/`) that registers a *connection
type*: opening such a connection runs `docker exec -it <container> <shell>` in a
terminal (Model A — docker runs locally and points at a daemon via `-H`). That is
a single-container terminal seam, not a management surface.

This plan adds a **`docker_manager`** builtin plugin: a full **management UI page**
for the Docker/Podman daemon **on a host you already reach over SSH** (Model B —
sshpilot SSHes into the host and runs `docker` there). It covers the five
requested feature areas: lifecycle, live logs, exec, stats, and image/volume
cleanup.

The two plugins are complementary and stay separate: `docker_protocol` =
connection type; `docker_manager` = management dashboard.

## Should we use docker-py (`docker/docker-py`)? — No.
Recommendation: **use the Docker CLI over the existing SSH connection, not the
docker SDK.** Reasons:

1. **Architecture fit / CLAUDE.md.** sshpilot mandates one connection+auth path
   (`build_ssh_connection` → `resolve_native_auth`, `~/.ssh/config` as source of
   truth, askpass/keyring/sshpass). The plugin API already exposes exactly this
   as **`ctx.run_command(nickname, cmd)`** (API ≥ 1.5), which runs over the host's
   real SSH config (ProxyJump, IdentityFile, port, stored credentials all apply).
   docker-py would instead talk to a daemon via `tcp://` (insecure, requires
   exposing the daemon) or `ssh://` (its **own** paramiko/ssh transport) —
   duplicating and bypassing our auth path. That is the exact thing CLAUDE.md
   forbids.
2. **Zero new dependencies.** docker-py pulls in `requests`/`urllib3`/etc.
   Builtin plugins should stay dependency-light (the example plugins use stdlib
   only). The CLI returns structured data with `--format '{{json .}}'` — parse
   with stdlib `json`, no deps.
3. **Docker *and* Podman for free.** The CLI is drop-in compatible
   (`podman ps`/`podman stats` …); docker-py is docker-daemon specific.
4. **Streaming is simpler via the terminal**, not an SDK socket (see Feature 2/3).

Net: a thin `DockerClient` helper wrapping `ctx.run_command` + JSON parsing is
smaller, safer, and architecturally correct. docker-py earns its weight only for
a local-daemon, event-stream-heavy app — not ours.

## Plugin shape
- **Location:** `sshpilot/plugins/builtin/docker_manager/` (builtin, like
  `docker_protocol`).
- **Files:** `plugin.json`, `__init__.py` (`class Plugin(SshPilotPlugin)`),
  `client.py` (the CLI/SSH data layer), `page.py` (GTK UI).
- **Manifest:** `{"id": "docker-manager", "name": "Docker Console",
  "api_version": 1, "version": "1.0.0", "builtin": true,
  "permissions": ["process", "ui", "connections"]}`.
- **API used:** `ctx.ui.register_page/open_page/notify`,
  `ctx.ui.register_connection_action`, `ctx.run_command`,
  `ctx.open_command_terminal`, `ctx.list_connections`, `ctx.run_on_ui_thread`,
  `ctx.settings`. Requires **API ≥ 1.7** — this work added
  `open_command_terminal` (1.6) and `register_connection_action` (1.7).
- **Entry:** `activate(ctx)` registers one page (icon e.g. `package-x-generic`),
  caches `ctx`, and registers a "Docker Console" connection context-menu action.
  All host calls happen lazily from the page.

### Shipped additions (beyond the original plan)
- **Connection context-menu action** (`ctx.ui.register_connection_action`, API
  1.7): right-click a connection → "Docker Console" opens the page targeting that
  host (`page.select_host(nickname)`).
- **sudo support** for non-root hosts: `DockerClient(use_sudo=…)` prefixes
  captured commands with `sudo -n` and interactive ones with `sudo`; the page
  auto-detects (probe plain, retry `sudo -n` on a permission-denied socket) and
  offers a **sudo** toggle, remembered per host in `ctx.settings`.
- **PTY for interactive commands**: `open_command_terminal` forces `ssh -t` so
  `docker exec -it` / `logs -f` / live `stats` attach to a real terminal.
- **Interactive shell** resolves via the container PATH (prefer bash, fall back
  to sh) instead of hard-coded `/bin/*`, so minimal images still work.

## Data layer — `client.py`
A small, testable wrapper so the UI never builds shell strings inline:

```
class DockerClient:
    def __init__(self, ctx, nickname, runtime="docker"): ...
    def _run(self, args, timeout=30) -> CommandResult       # ctx.run_command(nickname, f"{runtime} {args}")
    def ps(self, all=True) -> list[dict]                     # `ps -a --format '{{json .}}'`  -> parse NDJSON
    def stats(self) -> list[dict]                            # `stats --no-stream --format '{{json .}}'`
    def images(self) -> list[dict]                           # `images --format '{{json .}}'`
    def lifecycle(self, action, cid, force=False)            # start/stop/restart/kill/rm
    def system_prune(self) -> CommandResult                  # `system prune -f`
    def detect_runtime(self) -> str                          # `command -v docker || command -v podman`
```

- Output parsing: Docker/Podman emit **one JSON object per line** with
  `--format '{{json .}}'`; split on newlines, `json.loads` each.
- Runtime (docker vs podman) auto-detected once per host and remembered in
  `ctx.settings` keyed by nickname; user can override in a row selector.
- **All `_run` calls happen on a worker `threading.Thread`** (run_command is
  blocking); results marshalled back with `ctx.run_on_ui_thread`. Pattern copied
  from `examples/easyenv_workspaces`.

## UI — `page.py`
A host **picker** at the top (dropdown from `ctx.list_connections()`, default to
the active session's host) + a `Gtk.Stack`/`Adw.ViewStack` with tabs:

### 1. Containers (lifecycle)
- `Gtk.ColumnView`/`ListBox`: **ID · Name · Image · Status · Ports**, status dot
  colored by Running/Paused/Exited (parsed from `ps -a` `State`/`Status`).
- Per-row inline buttons: **Start / Stop / Restart / Kill** → `client.lifecycle`.
- **Remove (rm)**: button opens an `Adw.MessageDialog` confirm with a **Force**
  `Gtk.CheckButton` → `rm` / `rm -f`. Destructive styling.
- Auto-refresh via `GLib.timeout_add_seconds(3, …)` while the page is visible
  (guarded so it stops when hidden — mirror health plugin's worker pattern).

### 2. Logs
- `Gtk.TextView` (monospace, scrolled). **Timestamps** `Gtk.Switch` (adds `-t`),
  **Clear** button (flush buffer), **Tail** spin (default 100).
- **Snapshot+poll model (no new core API):** `logs --tail=N [-t] <cid>` via
  `run_command`, refreshed on a timer / Refresh button. This satisfies "view
  recent logs" without a PTY.
- **True `-f` streaming (optional, needs core hook):** see "Streaming" below.

### 3. Exec (shell)
- "Open shell" button → interactive `/bin/bash` with `/bin/sh` fallback inside
  the container. This is genuinely interactive and **needs a PTY** (see below).

### 4. Stats
- `Gtk.ColumnView`: **Name · CPU% · Mem usage/limit · Mem% · Net I/O · Block I/O**
  from `stats --no-stream --format '{{json .}}'`, polled every ~2–3 s. Polling a
  `--no-stream` snapshot is lighter and more robust than parsing a live `stats`
  PTY.

### 5. Images & cleanup
- `Gtk.ColumnView`: **Repository · Tag · ID · Size** from `images`.
- Per-row **Remove** (`rmi` / `rmi -f`) with confirm.
- **System Prune** button → `Adw.MessageDialog` confirm → `system prune -f`
  (dangling images, stopped containers, unused networks). Show freed-space from
  command output in a toast (`ctx.ui.notify`).
- (Volumes: `volume ls` + `volume prune -f` as a small sub-section, same pattern.)

## Streaming (`logs -f`, interactive `exec -it`, live `stats`) — DECIDED
The plugin API can run blocking commands (`run_command`) and open a **saved**
connection's terminal (`open_connection`), but there is **no API to open a new
PTY terminal tab running an arbitrary remote command**. Three of the requested
behaviours are genuinely interactive.

**Chosen approach (true streaming):** add one thin host method —

```
ctx.open_command_terminal(nickname, remote_command, *, title=None) -> bool
```

— to the plugin API (`api.py`), the plugin host (`plugins/host.py`), and the
window. It opens a new terminal tab whose **prepared command** is
`ssh -F <config> <host> <remote_command>`, built with the **existing native
command builder** (`build_ssh_connection` / `build_native_command`) and handed to
the terminal's existing "consume a prepared command" seam. **No new SSH/auth path
is introduced** — it reuses the single connection path per CLAUDE.md (same
`~/.ssh/config`, ProxyJump, keyring/sshpass). This is the only core-code change;
everything else is contained in the plugin.

With this hook:
- **Live logs:** `open_command_terminal(host, "docker logs -f --tail=N [-t] <cid>", title="logs: <name>")`.
- **Exec shell:** `open_command_terminal(host, "docker exec -it <cid> /bin/bash || docker exec -it <cid> /bin/sh", title="sh: <name>")`.
- **Live stats (optional):** a streamed `docker stats` tab, in addition to the
  in-page `--no-stream` polled table.

The in-page snapshot/poll views (tail logs, `--no-stream` stats) are still kept as
the lightweight default; the streamed terminals are opened on demand from
buttons ("Follow logs", "Open shell", "Live stats").

### `open_command_terminal` — implementation notes (core hook)
- **`api.py`:** add the public method on `PluginContext` delegating to
  `self._host.open_command_terminal(...)` (mirror `open_connection`); bump
  `API_VERSION` minor to `(1, 6)` and document it in `docs/plugins/writing-plugins.md`.
- **`plugins/host.py`:** resolve the connection by nickname, then open a terminal
  tab on the main window passing the remote command through to the native
  builder — reuse whatever path `open_connection` already uses to create a
  terminal tab, but with an explicit `remote_command` override instead of the
  connection's default. Must run on the UI thread / after `app_started`.
- **window/terminal:** the terminal already accepts a prepared command; the only
  addition is plumbing an optional `remote_command` so the docker command is what
  runs on the host (instead of an interactive login shell). Per-host SSH settings
  still come from `~/.ssh/config`, not CLI flags.

## Host target — DECIDED: host picker
A **host-picker dropdown** at the top of the page, populated from
`ctx.list_connections()` and defaulting to the active session's host (fall back to
the first connection). Explicit and flexible; the chosen nickname is the
`nickname` argument to every `run_command` / `open_command_terminal` call and is
remembered in `ctx.settings`.

## Threading & safety
- Never call `run_command` on the GTK thread. Each action: spawn a short-lived
  `threading.Thread`, then `ctx.run_on_ui_thread(update_fn, result)`.
- Disable a row's buttons while its action is in flight; re-enable on result.
- Destructive actions (`rm`, `rmi`, `system prune`) always go through an
  `Adw.MessageDialog` confirm; `rm`/`rmi` expose a **Force** checkbox.
- Surface non-zero `CommandResult.exit_code` (e.g. "Cannot connect to the Docker
  daemon", permission denied) as an inline error/toast — do not silently drop.

## File layout
```
sshpilot/plugins/builtin/docker_manager/
  plugin.json
  __init__.py        # Plugin(SshPilotPlugin): activate() registers the page
  client.py          # DockerClient (ctx.run_command + JSON parsing)
  page.py            # DockerConsolePage (GTK; host picker + 5 sections)
tests/test_docker_manager_plugin.py   # unit tests, mock ctx.run_command
```

## Testing
- Mirror `tests/test_docker_plugin.py`: construct the plugin, assert manifest
  discovery, page registration, and `DockerClient` command strings.
- `DockerClient` is pure logic over an injectable `run_command` — unit-test
  parsing of real `docker ps/stats/images --format json` fixtures, the
  `rm`/`rm -f` argument building, and runtime detection, with a fake ctx (no
  Docker, no SSH needed). Keeps the suite fully offline like the rest.
- Optional `tests/integration/` test (guarded, like `test_docker_exec.py`) that
  runs against a real local docker if present.

## Verification
- `python3 -m pytest -q tests/test_docker_manager_plugin.py` → green offline.
- Manual: enable the plugin (Preferences ▸ Plugins), open the Docker Console
  page, pick a host that runs docker, confirm: container list + status; start/
  stop/restart/kill/rm (with Force + confirm); logs view with timestamps/clear;
  stats numbers update; images list + rmi; system prune frees space (toast).
- Full suite stays green: `python3 -m pytest -q`.

# Core boundary

sshPilot’s reusable domain logic lives under `src/sshpilot/core/`.
Nothing in this package may import `gi`, Gtk, Gdk, Adw, Vte, GLib, or Gio.

## Layout

```text
src/sshpilot/core/
    errors.py
    package_graph.py   # allowed dependency manifest
    settings/          # defaults, migration, JSON store, ssh_overrides
    validation/        # connection field validation
    forwards/          # forwarding rule validation
    ssh/               # ProcessSpec + SSHLaunchRequest builder
    connections/       # ConnectionService domain store
    interaction/       # askpass / prompt policy (no dialogs)
    keys/              # discovery / generation service
    known_hosts/       # parse, filter, atomic save
    secrets/           # backend selection / unlock policy + protocols
    plugins/           # headless plugin contracts
    import_export/     # schema, plan, merge, atomic write
    transfers/         # transfer request/policy models
    cli.py             # headless proof consumer
```

## Layer roles

| Layer | Role |
| --- | --- |
| `sshpilot.core` | Pure domain models and services |
| `sshpilot.api` | Protocol / IPC models (GTK-free) |
| `sshpilot.daemon` | Runtime ownership (sessions, transfers, broker) |
| `sshpilot.platform.*` | OS adapters (libsecret `gi` load is isolated here) |
| `sshpilot.gtk.*` | GTK contribution surfaces / interaction provider |
| `sshpilot.connection_manager.ConnectionManager` | GObject adapter over `ConnectionService` |
| `sshpilot.ssh_connection_builder` | Runtime askpass/env adapter over `core.ssh` |
| `sshpilot.askpass_utils` | Process askpass helper; classifies via `core.interaction` |

See the [frontend closure audit](frontend-closure-audit.md) for the final
ownership evidence. For headless development, see
[`docs/development/headless-core.md`](../development/headless-core.md), and for
the current API contract see [`docs/api/`](../api/).

## Rules

1. Core never displays dialogs or loads GI directly. It is the bottom layer: it
   must not import `sshpilot.daemon` or `sshpilot.gtk`. Core's own coupling to
   compatibility helpers (`config`, `ssh_connection_builder`, `plugins`) is
   registered debt (`CORE_DEBT`) and is bounded by the dependency ratchet; it
   must not grow or become a frontend ownership path.
2. GTK collects input, calls core/API, renders state, maps `CoreError` to Adw UI.
3. **GTK must not instantiate stateful core services or use core modules to
   perform authoritative I/O.** A module being GTK-free does not mean GTK
   should own an instance of it; the daemon owns all authoritative state and
   I/O. `ConnectionManager`, `SecretManager`, `KeyService`, the known-hosts
   file (M2 **complete** — the editor routes through the daemon client), key
   discovery/generation and `.pub` reads (M1 **complete** — `KeyManager` is a
   client adapter), backup apply, and the SSH command builders are
   daemon-owned.
4. Frontend reaches into `sshpilot.core` only through the explicit allowlist
   in `tests/architecture/test_core_boundary.py` (pure validation /
   classification / naming / formatting) or through reviewed architecture
   exception registries; it never imports `sshpilot.daemon` except the
   enumerated bootstrap/diagnostic utilities.
5. Frontend must not perform backend *operations* (SSH/SCP/SFTP subprocesses,
   secret/key/config mutation, stateful service instantiation) outside the
   per-module `BACKEND_OPS` registry. Launching browsers or external GUI
   applications is frontend-owned and registered with tag `frontend`.
6. Compatibility shims stay thin — see `core-compatibility-shims.md` — and
   must delegate to the daemon API rather than re-owning state.

## Enforcement

Two AST test modules enforce rules 1, 3, 4, 5 and the package direction:

- `tests/architecture/test_core_boundary.py` — scans all GTK-facing modules
  (no package-level allowlists):
  - every frontend core import must be in the explicit `ALLOWED` allowlist or
    the `PENDING_MIGRATIONS` registry,
  - frontend → `sshpilot.daemon` imports are forbidden outside `DAEMON_ALLOWLIST`,
  - frontend backend operations (`subprocess`, SSH-family binaries,
    `SecretManager`/`ConnectionService`/`KeyService`/`BackupManager`
    instantiation, known-hosts load/save) are forbidden outside `BACKEND_OPS`,
  - `core`/`api`/`daemon` contain no direct Gtk/GLib/GI imports,
  - every registry must exactly match the source tree (no stale or phantom
    entries) and `PENDING_MIGRATIONS` may not grow silently.
- `tests/core/test_dependency_boundary.py` — enforces the package graph
  (`core/package_graph.py`) and the daemon's **transitive** edges:
  - core/api/daemon import no UI prefixes,
  - core imports only core/api/runtime_identity/platform.paths (plus `CORE_DEBT`),
  - daemon imports only daemon/api/core/headless helpers (plus `DAEMON_DEBT`),
    rejecting **GObject adapters** such as `Config`, `ConnectionManager`,
    `GroupManager` and `platform_utils`.

The registries are exact, reviewed architecture exceptions: a new backend
call, operation, or dependency edge in frontend/daemon code fails the suite.
Approved compatibility/dependency debt may remain registered when it does not
represent frontend ownership. The [frontend closure audit](frontend-closure-audit.md)
records the final classification; it establishes zero frontend migration
blockers without requiring approved debt registries to be numerically zero.

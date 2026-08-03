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

See also: `core-ownership-matrix.md`, `core-ownership-migration.md`,
`phase13-completion-matrix.md`, `docs/testing/phase13-production-smoke.md`,
`docs/testing/temporary-openssh-fixture.md`.

## Rules

1. Core never displays dialogs or loads GI. It is the bottom layer: it must
   not import `sshpilot.daemon` or `sshpilot.gtk`.
2. GTK collects input, calls core/API, renders state, maps `CoreError` to Adw UI.
3. **GTK must not instantiate stateful core services or use core modules to
   perform authoritative I/O.** A module being GTK-free does not mean GTK
   should own an instance of it; the daemon owns all authoritative state and
   I/O. `ConnectionManager`, `SecretManager`, `KeyService`, the known-hosts
   editor, backup apply, and the SSH command builders are daemon-owned.
4. Frontend reaches into `sshpilot.core` only through the explicit allowlist
   in `tests/architecture/test_core_boundary.py` (pure validation /
   classification / naming / formatting) or through registered pending
   migrations; it never imports `sshpilot.daemon` except the enumerated
   diagnostic/cleanup utilities.
5. Compatibility shims stay thin — see `core-compatibility-shims.md` — and
   must delegate to the daemon API rather than re-owning state.

## Enforcement

`tests/architecture/test_core_boundary.py` statically enforces rules 1, 3 and 4
at the AST level:

- every frontend core import must be in the explicit `ALLOWED` allowlist or the
  `PENDING_MIGRATIONS` registry (no package-level allowlists),
- frontend → `sshpilot.daemon` imports are forbidden outside `DAEMON_ALLOWLIST`,
- `core`/`api`/`daemon` never import Gtk/GLib/GI,
- the registry must exactly match the source tree (no stale or phantom entries).

The pending registry is the migration backlog for
`core-ownership-migration.md`; each migration M1–M8 removes its rows as it
lands, and registering a new backend call in frontend code fails the suite.

Phase 13.2 runtime ownership (sessions/SFTP/transfers/forwards/interactions) remains in `sshpilot.daemon` consuming core models; see `docs/api/` topic guides.

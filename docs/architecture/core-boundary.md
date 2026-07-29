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

## Rules

1. Core never displays dialogs or loads GI.
2. GTK collects input, calls core/API, renders state, maps `CoreError` to Adw UI.
3. Daemon consumes the same core request/policy models as GTK.
4. Compatibility shims stay thin — see `core-compatibility-shims.md`.

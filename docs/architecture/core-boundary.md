# Core boundary

sshPilot’s reusable domain logic lives under `src/sshpilot/core/`.
Nothing in this package may import `gi`, Gtk, Gdk, Adw, Vte, GLib, or Gio.

## Layout

```text
src/sshpilot/core/
    errors.py
    settings/          # defaults, migration, JSON store, ssh_overrides
    validation/        # connection field validation
    forwards/          # forwarding rule validation
    ssh/               # ProcessSpec (launch description)
    keys/              # discovery / generation service
    known_hosts/       # parse, filter, atomic save
    secrets/           # backend selection / unlock policy
    plugins/           # headless plugin contracts
    import_export/     # import payload validation
    cli.py             # minimal headless proof consumer
```

## Adapters

| Layer | Role |
| --- | --- |
| `sshpilot.core` | Pure domain models and services |
| `sshpilot.platform.*` | OS-specific adapters (e.g. libsecret `gi` load) |
| `sshpilot.gtk.*` | GTK contribution surfaces |
| `sshpilot.config.Config` | GObject/GSettings shell over core settings |
| `sshpilot.key_manager.KeyManager` | GObject shell over `KeyService` |
| `sshpilot.plugins.api` | Compatibility re-exports of core plugin contracts |

## Daemon / API relationship

`sshpilot.api` and `sshpilot.daemon` remain stable packages. They may use core
where useful; they must not import GTK. Transport and protocol models stay in
`api/` — do not relocate them merely for naming consistency.

## Migration path

1. Extract pure logic into `core/`.
2. Leave a thin shim or GObject adapter at the historical import path.
3. GTK controllers map structured `CoreError` / `FieldError` to Adw dialogs/toasts.
4. Prefer new headless/CLI code importing `sshpilot.core` directly.

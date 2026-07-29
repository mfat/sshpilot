# Dependency direction

## Allowed

```text
GTK / widgets          → core
GTK                    → api client
daemon                 → core (optional)
api                    → shared pure models / core
platform adapters      → OS APIs (including isolated gi for libsecret)
```

## Forbidden

```text
core    → GTK / gi / Adw / Vte / GLib / Gio
api     → GTK / gi
daemon  → GTK / gi
core    → concrete GTK dialogs / controllers
```

Enforced by:

* `tests/core/test_dependency_boundary.py` (AST)
* `tests/core/test_headless_imports.py` (runtime import with `gi` blocked)

## Platform exception

`sshpilot.platform.linux.libsecret` intentionally imports `gi.repository.Secret`.
It is outside the core/api/daemon boundary and is loaded lazily by
`secret_storage` only when the libsecret backend is used.

# Dependency direction

## Allowed

```text
GTK / widgets          → core, api client, platform
daemon                 → core, api
api                    → core (shared pure models)
platform adapters      → OS APIs (including isolated gi for libsecret)
compatibility shims    → core (no business logic duplication)
```

## Forbidden

```text
core    → GTK / gi / Adw / Vte / GLib / Gio / sshpilot.gtk*
api     → GTK / gi / sshpilot.gtk*
daemon  → GTK / gi / sshpilot.gtk*
```

## Enforcement

Manifest: `sshpilot.core.package_graph` (boundary packages + forbidden UI prefixes).

Tests:

* `tests/core/test_dependency_boundary.py` — AST (absolute, relative, aliased, constant `importlib`)
* `tests/core/test_headless_imports.py` — fresh subprocess with `python -I`, `import gi` hard-fails, no `DISPLAY`

## Platform exception

`sshpilot.platform.linux.libsecret` intentionally imports `gi.repository.Secret`.
It is outside the core/api/daemon boundary and is loaded lazily by
`secret_storage` only when the libsecret backend is used. No silent backend
fallback: `auto` fallthrough is explicit, ordered, and tested via
`core.secrets.resolve_lookup_order`.

The architecture and dependency-boundary suites are the current enforcement
for this policy. Historical phase audits and smoke reports are preserved under
[`docs/history/frontend-neutral-migration/`](../history/frontend-neutral-migration/).

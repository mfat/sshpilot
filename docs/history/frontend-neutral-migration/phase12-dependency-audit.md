"""Dependency audit map for Phase 12 (GTK-free core extraction).

Baseline: ``3834b712c8b094165683ea8d1601f415aace13df`` on ``dev``.

## Classification legend

| Tag | Meaning |
| --- | --- |
| pure | Already GTK-free domain logic |
| daemon/API | Protocol / transport packages |
| platform | OS adapters (may isolate ``gi``) |
| GTK controller / view | Presentation only |
| MIXED | Must split; domain extracted to core |

## Priority map (reusable logic formerly in GTK modules)

| Area | Modules | Classification | Core destination |
| --- | --- | --- | --- |
| Connection validation | `ssh_connection_validator`, dialog mixins | pure + GTK controller | `core.validation` |
| SSH option construction | `preferences.save_advanced_ssh_settings`, builder | MIXED / pure | `core.settings.ssh_overrides`, `core.ssh.ProcessSpec` |
| Key management | `key_manager`, `key_utils` | MIXED / pure | `core.keys` |
| Known hosts | `known_hosts_editor` | GTK view + inline I/O | `core.known_hosts` |
| Settings defaults/migration | `config.Config` | MIXED | `core.settings` |
| Secret backend policy | `secret_storage`, unlock dialog | pure + GTK | `core.secrets` + platform libsecret |
| Plugin contracts | `plugins.api` / `host` | MIXED | `core.plugins` + `gtk.plugins` |
| Forwarding validation | dialog port-forwarding, `port_utils` | MIXED / pure | `core.forwards` |
| Import/export | `backup_manager`, preferences | pure + GTK | `core.import_export` |

See also `core-boundary.md` and `dependency-direction.md`.

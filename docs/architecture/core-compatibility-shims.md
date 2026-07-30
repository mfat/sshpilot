# Core compatibility shims

Thin adapters that preserve historical import paths while domain logic lives in
`sshpilot.core`. Each shim must:

* import or wrap the new implementation
* contain no duplicate business logic
* emit no GTK dependency into core
* carry an explicit removal / deprecation note
* be covered by a compatibility or headless test

## Inventory (Phases 12–13)

| Historical path | Core owner | Notes |
| --- | --- | --- |
| `sshpilot.ssh_connection_validator` | `core.validation` | Re-export / thin wrapper |
| `sshpilot.key_manager.KeyManager` | `core.keys.KeyService` | GObject shell |
| `sshpilot.known_hosts_editor` I/O | `core.known_hosts` | GTK view + core I/O |
| `sshpilot.config.Config` | `core.settings` | GObject/GSettings shell |
| `sshpilot.preferences.save_advanced_ssh_settings` | `core.settings.compose_ssh_overrides` | Composition in core |
| `sshpilot.askpass_utils.classify_prompt` | `core.interaction.classify_prompt` | **Shim** — remove when callers migrate |
| `sshpilot.ssh_connection_builder` | `core.ssh.build_ssh_process_spec` | Runtime askpass adapter; argv policy in core |
| `sshpilot.connection_manager.ConnectionManager` | `core.connections.ConnectionService` | GObject adapter; SSH config I/O remains here |
| `sshpilot.backup_manager._validate_import_data` | `core.import_export` | Validation/planning in core |
| `sshpilot.secret_storage` selection | `core.secrets` | Policy in core; backends in platform/module |
| `sshpilot.plugins.api` | `core.plugins` | Compatibility re-exports |

## Removal guidance

Prefer importing from `sshpilot.core.*` in new headless/CLI/daemon code.
GTK may keep historical imports until a dedicated cleanup phase deletes the
shims after caller migration is complete.

Phase 13.1: shim inventory above remains accurate after ownership wiring
(`ConnectionService` mutations, `build_ssh_process_spec`, askpass/secret policy,
import/export plan, transfer `decide_conflict`).

# Core compatibility shims

Thin adapters that preserve historical import paths while domain logic lives in
`sshpilot.core`. Each shim must:

* import or wrap the new implementation
* contain no duplicate business logic
* emit no GTK dependency into core
* carry an explicit removal / deprecation note
* be covered by a compatibility or headless test

Since the ownership workstream, a shim must also satisfy the boundary rule:
**it delegates authoritative state and I/O to the daemon API and holds no
authoritative state of its own.** A shim that re-owns an instance of a core
service is not a shim — it is a violation (see
`core-ownership-migration.md` and the enforcement test
`tests/architecture/test_core_boundary.py`).

## Inventory (Phases 12–14)

| Historical path | Core owner | Notes |
| --- | --- | --- |
| `sshpilot.ssh_connection_validator` | `core.validation` | Re-export / thin wrapper (pure; allowed) |
| `sshpilot.key_manager.KeyManager` | `core.keys.KeyService` | GObject shell → API adapter (M1) |
| `sshpilot.known_hosts_editor` I/O | `core.known_hosts` | **Complete (M2)** — editor renders daemon API data; no local I/O |
| `sshpilot.config.Config` | `core.settings` | GObject/GSettings shell; daemon owns persistent keys (M4) |
| `sshpilot.preferences.save_advanced_ssh_settings` | `core.settings.compose_ssh_overrides` | Composition in core (pure; allowed) |
| `sshpilot.askpass_utils.classify_prompt` | `core.interaction.classify_prompt` | **Shim** — remove when callers migrate |
| `sshpilot.ssh_connection_builder` | `core.ssh.build_ssh_process_spec` | Runtime askpass adapter; argv policy in core (M7) |
| `sshpilot.connection_manager.ConnectionManager` | `core.connections.ConnectionService` | GObject adapter; config write moves to daemon (M3) |
| `sshpilot.backup_manager._validate_import_data` | `core.import_export` | Validation/planning in core; apply moves to daemon (M6) |
| `sshpilot.secret_storage` selection | `core.secrets` | Policy in core; selection/state moves to daemon (M5) |
| `sshpilot.plugins.api` | `core.plugins` | Compatibility re-exports; spawn runtime moves to daemon (M8) |

## Removal guidance

Prefer importing from `sshpilot.core.*` in new headless/CLI/daemon code.
GTK may keep historical imports until a dedicated cleanup phase deletes the
shims after caller migration is complete — but those shims must delegate to the
daemon API for any authoritative access, and each is tracked in
`core-ownership-migration.md` (M1–M8). Shims that would re-instantiate a
stateful core service in the GTK process are prohibited.

Referenced by `core-ownership-migration.md`, the Phase 13/14 completion matrices,
and API topic guides.

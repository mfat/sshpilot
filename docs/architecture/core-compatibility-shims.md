# Core compatibility shims

Thin adapters that preserve compatibility import paths while domain logic lives
in `sshpilot.core` and authoritative state/I/O remains daemon-owned. Each shim
must:

* import or wrap the new implementation
* contain no duplicate business logic
* emit no GTK dependency into core
* carry an explicit removal / deprecation note
* be covered by a compatibility or headless test

Since the ownership workstream, a shim must also satisfy the boundary rule:
**it delegates authoritative state and I/O to the daemon API and holds no
authoritative state of its own.** A shim that re-owns an instance of a core
service is not a shim — it is a violation (see
the frontend closure audit and the enforcement test
`tests/architecture/test_core_boundary.py`).

## Current inventory

| Historical path | Core owner | Notes |
| --- | --- | --- |
| `sshpilot.ssh_connection_validator` | `core.validation` | Re-export / thin wrapper (pure; allowed) |
| `sshpilot.key_manager.KeyManager` | `core.keys.KeyService` | GObject adapter over `SshPilotClient`; no local key I/O |
| `sshpilot.known_hosts_editor` I/O | `core.known_hosts` | Editor renders daemon API data; no local I/O |
| `sshpilot.config.Config` | `core.settings` | GObject/GSettings compatibility shell; daemon owns persistent keys |
| `sshpilot.preferences.save_advanced_ssh_settings` | `core.settings.compose_ssh_overrides` | Composition in core (pure; allowed) |
| `sshpilot.askpass_utils.classify_prompt` | `core.interaction.classify_prompt` | Process askpass compatibility adapter over shared prompt policy |
| `sshpilot.ssh_connection_builder` | `core.ssh.build_ssh_process_spec` | Native OpenSSH launch compatibility consumed by daemon providers |
| `sshpilot.connection_manager` | `connection_model.Connection` / `ConnectionState` | Deprecated import-only shim for ephemeral projections; the former manager and all I/O are removed |
| `sshpilot.backup_manager._validate_import_data` | `core.import_export` | Pure validation/planning helper; daemon route applies changes |
| `sshpilot.secret_storage` selection | `core.secrets` | Shared policy/provider compatibility consumed by daemon services |
| `sshpilot.plugins.api` | `core.plugins` | Compatibility facade; backend operations use typed daemon APIs |

## Removal guidance

Prefer importing from `sshpilot.core.*` in new headless/CLI/daemon code. Existing
frontends may retain these compatibility paths only where listed above; they
must delegate authoritative access to the daemon API and must not instantiate a
second stateful owner. The `connection_manager` shim is model-only and must not
be used as a manager API.

The [frontend closure audit](frontend-closure-audit.md) records why approved
compatibility/dependency debt is not a frontend migration blocker.

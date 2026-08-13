# Core ownership matrix

Audit of every direct or indirect import of `sshpilot.core` (and the other
GTK-free modules outside `sshpilot.core`) from GTK-facing modules, classified
into the four ownership categories:

1. `PURE_FRONTEND_SAFE` — deterministic, stateless; GTK may call locally.
2. `DAEMON_STATE_REQUIRED` — stateful service or authoritative I/O; daemon owns.
3. `COMPATIBILITY_SHIM` — thin adapter; must delegate to the API, hold no
   authoritative state, and carry a removal note.
4. `MIXED_NEEDS_SPLIT` — exposes both pure helpers and authoritative I/O; split
   the pure part (GTK-local) from the I/O part (daemon).

The boundary rule: **GTK must not instantiate stateful core services or use
core modules to perform authoritative I/O.** A module being GTK-free does not
mean GTK should own an instance of it.

| GTK-facing module | Core import | Classification | What it does today | Target |
| --- | --- | --- | --- | --- |
| `key_manager.py` | none (was `core.keys.KeyService`, `KeyGenerateSpec`, `SSHKeyInfo`) | `DAEMON_STATE_REQUIRED` | **Complete (M1)** — GObject adapter over `SshPilotClient` + `KeyController`; no local key I/O | API adapter; daemon owns key files and `ssh-keygen` |
| `key_utils.py` | `core.keys` (`looks_like_private_key`, `is_private_key`, `SKIPPED_FILENAMES`) | `MIXED_NEEDS_SPLIT` | Headless key discovery used by connection dialog key chooser | Pure sniffing may stay local; discovery over the SSH dir moves to daemon |
| `known_hosts_editor.py` | `sshpilot.api.models.known_hosts` (+ `KnownHostsController`) | `DAEMON_STATE_REQUIRED` | **Complete (M2)** — renders daemon API summaries; stages IDs; batched revision-checked removal | Daemon API list/remove with revision token; GTK renders entries |
| `ssh_connection_validator.py` | `core.validation.connection` | `PURE_FRONTEND_SAFE` | Field validation, hostname/port/username rules | Keep local (form validation stays local) |
| `config.py` | `core.settings` (defaults, migration, store) | `MIXED_NEEDS_SPLIT` | GTK preference store loads/writes the config JSON | GTK keeps visual keys; daemon-owned keys via API |
| `preferences.py` | `core.settings.compose_ssh_overrides` | `PURE_FRONTEND_SAFE` | Pure SSH-overrides composition | Keep local (pure formatting) |
| `connection_manager.py` | `core.connections.ConnectionService` | `DAEMON_STATE_REQUIRED` | Instantiates `ConnectionService` (`_domain`) as in-GTK store | Remove; daemon is the authoritative store |
| `connection_manager.py` | `core.connections.models` (`generate_duplicate_nickname`, `generate_group_slug`) | `PURE_FRONTEND_SAFE` | Pure naming helpers | Keep local |
| `connection_manager.py` | `core.errors.CoreError` | `PURE_FRONTEND_SAFE` | Error mapping | Keep local |
| `ssh_connection_builder.py` | `core.ssh.ProcessSpec`, `build_ssh_process_spec` | `DAEMON_STATE_REQUIRED` | Builds native SSH command + askpass env for GTK-spawned processes | Daemon API one-shot/streaming/session commands |
| `askpass_utils.py` | `core.interaction.classify_prompt`, `build_request_from_prompt`, `decide_headless` | `PURE_FRONTEND_SAFE` | Prompt classification policy | Keep local (pure classification) |
| `askpass_utils.py` | `core.interaction` askpass helper runtime | `DAEMON_STATE_REQUIRED` | GTK askpass helper answering prompts | Keep GTK askpass process but route through interaction broker |
| `backup_manager.py` | `core.import_export` (`plan_import`, `atomic_write_json`, `migrate_payload`) | `DAEMON_STATE_REQUIRED` | Applies restore and writes files from GTK | Daemon backup/restore operations |
| `backup_manager.py` | `core.connections.models.generate_group_slug` | `PURE_FRONTEND_SAFE` | Pure slug generation | Keep local |
| `backup_manager.py` | `core.errors.CoreError` | `PURE_FRONTEND_SAFE` | Error mapping | Keep local |
| `secret_storage.py` | `core.secrets` (`normalize_backend_name`, `platform_default_order`, `decide_unlock`, `SecretDecisionKind`) | `DAEMON_STATE_REQUIRED` | GTK `get_secret_manager()` owns backend selection + vault state | Daemon owns backend/lookup/store; GTK is an interaction presenter |
| `plugins/api.py` | `core.plugins` | `MIXED_NEEDS_SPLIT` | Headless plugin contracts re-exported into GTK plugin context | Daemon plugin runtime for backend ops; GTK extension host keeps contracts |
| `plugins/host.py` | `core.plugins` (contracts, `EventBus`) | `MIXED_NEEDS_SPLIT` | Frontend plugin host emits core events | GTK host keeps UI contributions; backend calls go to API |
| `connection_dialog_port_forwarding.py` | `core.forwards` (`validate_forwarding_rule`, `forwarding_rule_defaults`) | `PURE_FRONTEND_SAFE` | Pure forwarding-rule validation | Keep local |
| `file_manager_window.py` | `core.transfers` (`OverwritePolicy`, `ui_conflict_response_to_policy`) | `PURE_FRONTEND_SAFE` | Pure conflict policy mapping | Keep local |
| `groups.py` | `core.connections.models.generate_group_slug` | `PURE_FRONTEND_SAFE` | Pure slug generation | Keep local |
| `terminal.py` | `core.connection_evidence.classify_connection_evidence` | `PURE_FRONTEND_SAFE` | Pure terminal-output classification | Keep local |
| `gtk/interaction.py` | `core.interaction` | `PURE_FRONTEND_SAFE` | Prompt policy for the interaction provider | Keep local |

## GTK-free modules outside `sshpilot.core`

A module being GTK-free is **not** evidence that GTK should own an instance of
it. Classification below reflects authoritative I/O, not import cleanliness.

| Module | GTK-free? | Classification | Notes |
| --- | --- | --- | --- |
| `ssh_config_document.py` | yes | `DAEMON_STATE_REQUIRED` | SSH-config document model used for authoritative config mutations. GTK must not construct documents for writes. |
| `ssh_config_formatter.py` | yes | `PURE_FRONTEND_SAFE` | Pure formatting (indentation, wrapping). |
| `ssh_config_utils.py` | yes | `MIXED_NEEDS_SPLIT` | `ssh -G` effective-config resolution runs an OpenSSH subprocess — daemon-owned. Pure parsing helpers may stay local. |
| `identity.py` | yes | `DAEMON_STATE_REQUIRED` | Identity-provider selection injects agent env/config — daemon-owned. |
| `effective_config_check.py` | yes | `DAEMON_STATE_REQUIRED` | Launches local `ssh`/`ssh -G` subprocesses — daemon-owned. |
| `authorized_keys_service.py` | yes | `DAEMON_STATE_REQUIRED` | Authorized-keys file mutation and `ssh-copy-id` — daemon-owned. |
| `key_utils.py` | yes | `MIXED_NEEDS_SPLIT` | Pure private-key sniffing local; key discovery over SSH dir daemon-owned. |
| `ssh_multiplex.py` | yes | `DAEMON_STATE_REQUIRED` | ControlMaster socket policy and daemon-owned master expiry. |
| `ssh_key_fingerprint.py` | yes | `DAEMON_STATE_REQUIRED` | Runs `ssh-keygen -lf` / `ssh-add -L` — daemon-owned. |
| `scp_utils.py` | no | `PURE_FRONTEND_SAFE` | Pure SCP operand normalization and error classification; native execution is daemon-owned elsewhere. |
| `sftp_utils.py` | yes | `PURE_FRONTEND_SAFE` | External GVFS/file-manager and `sshfs` presentation only; no frontend SSH verification. |
| `agent_client.py` | yes | `PURE_FRONTEND_SAFE` | Local/PyXterm agent shell process only. |

## Explicitly allowed pure-core frontend dependencies

These stay local and are **not** routed through IPC:

- `core.validation.connection` — field validation.
- `core.forwards` — forwarding-rule validation and defaults.
- `core.interaction.classify_prompt` / `build_request_from_prompt` — prompt
  classification.
- `core.transfers` conflict-policy mapping (`ui_conflict_response_to_policy`,
  `OverwritePolicy`).
- `core.connection_evidence` — terminal-output evidence classification.
- `core.connections.models` pure helpers (`generate_duplicate_nickname`,
  `generate_group_slug`).
- `core.settings.compose_ssh_overrides` and defaults/migrations for
  frontend-only visual settings.
- Immutable API/plugin model construction and formatting that has no
  authoritative filesystem or runtime dependency.
- `core.errors.CoreError` / `ErrorCode` mapping to presentation errors.

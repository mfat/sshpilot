# Frontend closure audit

Status: **not closed** at base `29f1c9a29d8656f3c7fdeff241beeecd2136879`.

This is the Phase 7 inventory of production GTK entry points.  The matrix
records the ownership boundary, rather than treating the current location of
an implementation as evidence that it is frontend-only.  `API/daemon owned`
means that the GTK action reaches a typed `SshPilotClient` method and the
authoritative work is performed by a daemon service/runtime.  `legitimate
frontend-only` is reserved for presentation or OS integration.  The other two
statuses identify work that is still reachable or compatibility code that has
no current product path.

## Operation matrix

| Operation | Frontend entry point | Public API method/capability | Daemon owner | Headless test | Status | Notes |
|---|---|---|---|---|---|---|
| Connection list, create, edit, duplicate, delete | `ConnectionDialog`, `Sidebar`, `ConnectionStore` | `list_connections`, `create_connection`, `update_connection`, `duplicate_connection`, `delete_connection` / `connections.*` | `ConnectionApplicationService`, `ConnectionRepository` | `tests/daemon/test_connection_mutations.py`, `tests/core/test_connection_application_service.py` | API/daemon owned | No GTK persistence fallback is used by the daemon-mode path. |
| Connection passwords and key passphrases | `ConnectionDialog`, `KeyController` | `store_connection_password`, `delete_connection_password`, `store_key_passphrase`, `delete_key_passphrase` / `secrets.operate` | `SecretBackendService`, connection/key services | `tests/daemon/test_secret_dispatch.py`, `tests/test_connection_dialog_passphrase.py` | API/daemon owned | Protected interaction paths carry secret values; GTK no longer invokes the local credential manager for saves. |
| Groups, colors, ordering, and tag/sidebar mutations | `Sidebar`, `GroupStore` | `create_group`, `rename_group`, `move_connection`, `set_group_color`, `add_tag`, `assign_tag`, `delete_tag` / `groups.*` | `GroupApplicationService` and connection state service | `tests/test_group_mutation_controller.py`, `tests/test_tag_groups.py` | API/daemon owned | Sidebar only presents and dispatches typed mutations. |
| SSH config editor | `SshConfigEditor`, `ConnectionDialog` | `get_ssh_config_text`, `save_ssh_config_text` / `connections.config.*` | `SshConfigStore`, connection service | `tests/core/test_ssh_config_store.py`, `tests/core/test_ssh_config_text_editor.py` | API/daemon owned | The editor does not write `~/.ssh/config` directly. |
| Session open, attach, close, input, resize, replay | `TerminalManager`, `TerminalWidget` | `open_session`, `attach_session`, `close_session`, `send_terminal_input`, `resize_session`, `get_terminal_replay` / `sessions.*` | `SessionRuntime`, PTY runtime, daemon launch provider | `tests/daemon/test_session_*`, `tests/test_daemon_terminal_activation_ownership.py` | API/daemon owned | The normal activation path has no local SSH fallback. |
| Local shell tab | `TerminalWidget.setup_local_shell` | None | Local shell is intentionally owned by the terminal presentation | `tests/test_terminal_session_controller.py` | legitimate frontend-only | It is a local presentation feature and does not own SSH Pilot state or a remote operation. |
| Terminal broadcast | `TerminalManager`, broadcast action handlers | `broadcast_terminal_input`, `broadcast_command` / `sessions.broadcast` | `BroadcastService`, session runtime | `tests/daemon/test_broadcast_service.py`, `tests/test_daemon_broadcast_ownership.py` | API/daemon owned | GTK selects targets and renders results only. |
| SFTP connect, browse, stat, mkdir, rename, delete, upload, download | `SftpServiceController`, file-manager panes | `sftp_*`, transfer operations / `sftp.*`, `transfers.*` | `SftpRuntime`, transfer service | `tests/daemon/test_sftp_*`, `tests/integration/test_sftp_phase10.py` | API/daemon owned | `DaemonSftpManager` is the only in-app backend; unavailable daemon mode raises. |
| External GVFS/file-manager presentation | `file_manager_integration`, `sftp_utils.open_remote_in_file_manager` | None | OS file manager/GVFS | `tests/test_file_manager_integration.py` | legitimate frontend-only | The process is an OS presentation integration; it does not become an SSH Pilot-owned SFTP backend. |
| SCP upload/download and transfer cancellation | `ScpWindow`, `ScpWindowController` | `start_scp_transfer`, `cancel_transfer`, `get_operation` / `scp.*`, `operations.*` | Native SCP backend and operation runtime | `tests/daemon/test_native_scp_backend.py`, `tests/test_scp_daemon_routing.py` | API/daemon owned | The GTK transfer dialog has no direct SCP process ownership. |
| Port forwarding open/close | `ForwardServiceController`, connection dialog | `open_forward`, `close_forward`, `list_forwards` / `forwards.*` | Forward service/runtime | `tests/daemon/test_forward_*`, `tests/integration/test_forward_phase10.py` | API/daemon owned | Rule validation may use pure core helpers; runtime ownership is daemon-side. |
| Key listing, generation, fingerprint, passphrase verification | `KeyController`, `KeyManager`, `SshCopyIdWindow` | `list_keys`, `generate_key`, `verify_key_passphrase` / `keys.*` | Daemon key service and protected interaction broker | `tests/daemon/test_key_*`, `tests/test_key_manager_daemon.py` | API/daemon owned | GTK never runs `ssh-keygen` or reads private key material to implement the operation. |
| SSH-agent list/add/remove and identity selection | `KeyController`, preferences | `list_agent_keys`, `add_agent_key`, `remove_agent_key`, identity APIs / `identity.*` | Identity service and agent adapter | `tests/daemon/test_identity_service*`, `tests/test_agent_preload.py` | API/daemon owned | Agent process operations are daemon-owned. |
| Authorized-keys list/edit/deploy | `AuthorizedKeysWindow`, `SshCopyIdWindow` | `list_authorized_keys`, `remove_authorized_key`, `deploy_key` / `keys.authorized.*` | Authorized-keys and key deployment services | `tests/test_authorized_keys_*`, `tests/daemon/test_key_dispatch.py` | API/daemon owned | Remote file mutation is not performed by GTK. |
| Known-hosts list, edit, remove, verify | `KnownHostsController`, known-hosts editor | known-hosts API / `known_hosts.*` | `KnownHostsService` | `tests/daemon/test_known_hosts_*`, `tests/test_known_hosts_editor_client.py` | API/daemon owned | The local editor is a presentation of daemon-owned mutations. |
| Secret backend selection, state, lock/unlock | `SecretBackendsController`, startup unlock, unlock dialog | `get_secret_state`, `update_secret_configuration`, `unlock_secrets`, `lock_secrets` / `secrets.*` | `SecretBackendService` and protected interaction broker | `tests/daemon/test_secret_backend_service.py`, `tests/daemon/test_secret_dispatch.py` | API/daemon owned | Terminal and connection-save gates now read `SecretBackendState` through the controller. |
| Bitwarden, rbw, and KeePassXC lifecycle | preferences and secret-backend controller | backend-specific `secrets.*` methods | `SecretBackendService` | `tests/test_secret_backends_controller.py`, `tests/daemon/test_secret_backend_service.py` | API/daemon owned | GTK presents setup/status UI; it does not invoke vault commands. |
| Bitwarden CLI installation | `bitwarden_setup.run_install` | None | OS package/binary installation | `tests/test_bitwarden_setup.py` | legitimate frontend-only | This is a platform installer presentation path, not secret storage or vault state ownership. |
| Backup preview, export, import, and restore | preferences, backup dialogs | backup/import/export API / `backups.*` | daemon secret-transfer and backup services | `tests/daemon/test_secret_transfer.py`, `tests/test_backup_*` | API/daemon owned | Import planning helpers remain compatibility debt inside the daemon boundary. |
| Global SSH overrides read/update/reset | preferences | `get_global_ssh_overrides`, `update_global_ssh_overrides`, `reset_global_ssh_overrides` / `ssh_overrides.*` | `SshOverridesService` | `tests/core/test_ssh_overrides_service.py`, `tests/daemon/test_ssh_overrides_dispatch.py` | API/daemon owned | ControlMaster expiry is now a daemon-injected post-persistence hook. |
| Daemon status, diagnostics, logs, and restart | help/diagnostics and application lifecycle UI | daemon status, diagnostics, restart APIs | daemon server/lifecycle/diagnostics services | `tests/daemon/test_diagnostics_daemon_section.py`, `tests/daemon/test_daemon_lifecycle.py` | API/daemon owned | Socket-path resolution and rendering are frontend presentation only. |
| Browser URLs, clipboard, notifications, window/layout state | actions, dialogs, window, notification helpers | None | Desktop/OS/UI | `tests/test_window_*`, focused action tests | legitimate frontend-only | No SSH Pilot backend state or remote operation is owned here. |
| Native file/folder chooser presentation | connection, transfer, backup, preferences dialogs | None | GTK/desktop chooser | focused GTK/headless dialog tests | legitimate frontend-only | Selection is UI input; the resulting operation still goes through the API. |
| External system terminal | `WindowActions`, `MainWindow.open_in_system_terminal` | None | OS terminal application | `tests/test_terminal_*`, external-terminal action tests | legitimate frontend-only | This explicitly hands presentation to the OS; in-app SSH sessions remain daemon-owned. |
| Plugin remote command execution (captured) | `PluginContext.run_command`, Docker plugin actions | No typed `SshPilotClient` method | No daemon owner; `PluginContext` builds/owns the process | plugin compatibility tests only; no headless client proof | migration required | Active Docker/plugin actions reach `plugins/api.py` and its OpenSSH subprocess adapter. This is blocker P7-PLUGIN-COMMAND. |
| Plugin remote command streams/log follow | `DockerConsolePage`, `LogsTabMixin` via `run_command_stream` | No typed streaming command method | No daemon owner; `_spawn_stream` owns the process | plugin stream tests are not daemon/headless proof | migration required | This is blocker P7-PLUGIN-STREAM and cannot be classified as terminal presentation because the plugin owns captured remote command execution. |
| Plugin multiplex lifecycle | `DockerConsolePage._acquire_multiplex`, `_release_multiplex` | No typed multiplex lifecycle method | No daemon owner; `PluginContext.release_multiplex` runs `ssh -O exit` | `tests/test_ssh_multiplex.py` covers the local helper only | migration required | `release_multiplex()` is actively reached on Docker page map/unmap/host changes and is blocker P7-PLUGIN-MUX. |
| Plugin local forward active route | `DockerManager` service links via `PluginContext.ensure_local_forward` | `open_forward`, `get_forward` / `forwards.*` | daemon forward runtime | `tests/daemon/test_forward_*`, `tests/integration/test_forward_phase10.py` | API/daemon owned | The active route requires `DaemonClient`; the policy rejects legacy local processes. |
| Plugin legacy local-forward branch | legacy branch inside `PluginContext.ensure_local_forward` | None | None | `tests/test_extended_service_policy.py` | dead/unreachable code | `allow_legacy_local_forward()` is hardcoded false and daemon routing is preferred; the ControlMaster/`ssh -N` branch has no current production route. |
| Plugin key deployment compatibility method | `PluginContext.copy_key_to_host` | `deploy_key` exists, but this method does not use it | None in current production graph | no current production caller; daemon deploy tests cover the replacement API | dead/unreachable code | Retained public plugin compatibility surface; direct `ssh-copy-id` must not be reactivated. |
| Plugin effective-config compatibility method | `PluginContext.get_effective_ssh_config` | No typed plugin-facing method | None in current production graph | no current production caller; SSH config service tests cover authoritative config paths | dead/unreachable code | It delegates to the legacy local `ssh -G` helper and is not an active GTK operation. |
| Legacy `OpenSSHSFTPManager` | `file_manager/openssh_backend.py` | None | None in the current in-app route | daemon SFTP routing tests prove the active route | dead/unreachable code | No graph inbound path from the current file-manager route; retain only until compatibility removal is separately verified. |
| Legacy direct SSH terminal helpers | `TerminalWidget._connect_ssh*`, `_setup_ssh_terminal` | None | None in the current daemon activation route | `tests/test_daemon_terminal_activation_ownership.py` | dead/unreachable code | The normal route is daemon-only; these compatibility helpers are not a valid fallback. |

## Debt identity audit

The Phase 5 debt ratchet remains unchanged and is not being bypassed.  The
following are the individually audited identities at this base.

### `PENDING` (17 identities)

| Identity family | Identities | Reachability and closure decision |
|---|---|---|
| M4 config/settings | `config.py → settings.CONFIG_VERSION`, `ensure_config_defaults`, `get_default_config` | Compatibility/default-cache reads remain reachable from legacy configuration presentation. They do not own the daemon connection/config API path; keep as approved M4 debt. |
| M5 secret policy | `secret_storage.py → secrets.normalize_backend_name`, `platform_default_order`, `decide_unlock`, `SecretDecisionKind` | Shared compatibility policy is used by daemon-owned secret adapters. GTK decision gates were migrated to `SecretBackendState`; keep the daemon compatibility identities until the backend adapter is fully replaced. |
| M6 import/export | `backup_manager.py → import_export.MergeStrategy`, `plan_import`, `atomic_write_json`, `migrate_payload` | Backup execution is daemon-routed. These are compatibility helpers used by the daemon transfer path, not GTK ownership; keep as M6 debt. |
| M7 SSH process model | `ssh_connection_builder.py → ProcessSpec`, `AuthMethod`, `HostKeyMode`, `LaunchMode`, `SSHLaunchRequest`, `build_ssh_process_spec` | Native OpenSSH launch compatibility is consumed by daemon providers and external-terminal presentation. No new GTK backend owner was found; keep as M7 debt. |

### `BACKEND_OPS` debt (18 identities)

| Tag | Identities | Audit decision |
|---|---|---|
| M5 | `secret_storage.py: subprocess`, `secret_storage.py: SecretManager`, `bitwarden_setup.py: subprocess` | The first two are shared daemon/askpass compatibility; Bitwarden installation is a narrow platform installer. No active GTK vault owner remains. |
| M7 | `agent_client.py: subprocess`; `askpass_utils.py: subprocess, ssh_binary`; `autocomplete.py: subprocess`; `file_manager/openssh_backend.py: subprocess`; `providers/system_agent.py: subprocess, ssh_binary`; `scp_utils.py: subprocess`; `sftp_utils.py: subprocess`; `ssh_config_utils.py: subprocess, ssh_binary`; `ssh_multiplex.py: subprocess, ssh_binary`; `terminal.py: subprocess` | These are respectively local helper, daemon askpass/identity compatibility, dead legacy routes, external OS presentation, or shared native-process compatibility. ControlMaster teardown was removed from GTK and moved behind `SshOverridesService`; the remaining helper identity is compatibility debt. |
| M8 | `plugins/api.py: subprocess` | Function-level identities are now audited by the final guard: active `run_command`, `run_command_stream`/`_spawn_stream`, and `release_multiplex` remain closure blockers; `copy_key_to_host`, `get_effective_ssh_config`, and the legacy branch of `ensure_local_forward` are explicitly dead compatibility identities. |

### `DAEMON_DEBT` (16 identities)

The exact importer identities remain the existing 16 entries in
`tests/core/test_dependency_boundary.py`: launch provider →
`ssh_connection_builder`/`plugins`; connection secret provider →
`credential_model`/`secret_storage`/`askpass_utils`; secret backend service →
`secret_storage`; secret transfer → `backup_manager`/`ssh_connection_builder`/
`credential_model`/`secret_storage`; `backup_manager.py → config`; daemon CLI
and launcher → `platform_utils`; `askpass_utils.py` and `credential_model.py`
→ `secret_storage`; and plugin API → `plugins.host`.

Every one is daemon-internal compatibility or a shared helper edge, not a GTK
fallback.  They remain individually registered because the ratchet correctly
prevents silently broadening the exception. `CORE_DEBT` is empty.

## Closure blockers and next semantic APIs

Remaining blockers: **3**.

* `P7-PLUGIN-COMMAND`: add a typed daemon-owned semantic for plugin remote
  command execution, including captured output and exit status.
* `P7-PLUGIN-STREAM`: add the daemon operation/event lifecycle needed for
  long-lived plugin command output and cancellation.
* `P7-PLUGIN-MUX`: move plugin multiplex reference release/ControlMaster
  teardown behind daemon-owned session/forward runtime semantics.

These are intentionally not replaced with a generic `run_command` escape
hatch: that would move the bypass behind another frontend-owned abstraction.
The existing public API therefore remains unchanged until the semantic plugin
contract is designed and tested headlessly.

## Version and evidence

Public API implementation: `0.25`  
Protocol: `1.0`  

The API/daemon routes are proven by the headless daemon/core/API tests named in
the matrix. The reference CLI is supporting evidence only; it is not required
for every operation. The final closure guard is
`tests/architecture/test_frontend_closure.py`; the existing boundary and debt
ratchet tests remain authoritative for the full registries.

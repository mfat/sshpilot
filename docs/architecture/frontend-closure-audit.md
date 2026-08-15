# Historical frontend closure audit

Status: **historical**. This Phase 7 inventory is retained as migration
evidence. The current ownership plan and status are maintained in
[daemon-only-retirement.md](daemon-only-retirement.md).

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
| External file-manager presentation | `file_manager_integration` | daemon-prepared internal SFTP window | daemon SFTP service | file-manager backend tests | historical/changed | The obsolete frontend GVFS/SSH route was removed; remote file-manager views use the daemon SFTP backend. |
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
| Plugin remote command execution (captured) | `PluginContext.run_command`, Docker plugin actions | `start_broadcast_command`/`get_broadcast_command` | `BroadcastCommandService` and `NativeSshCommandRunner` | daemon broadcast/API tests and plugin command tests | API/daemon owned | Single-target plugin commands reuse the daemon broadcast/native SSH path; protected stdin uses binary secret transport. |
| Plugin remote command streams/log follow | `DockerConsolePage`, `LogsTabMixin` via `run_command_stream` | broadcast output events plus cancellation | `BroadcastCommandService` output publisher and native runner | daemon broadcast/API tests and Docker stream tests | API/daemon owned | Plugin callbacks receive daemon output events and stop cancels the daemon operation. |
| Plugin multiplex lifecycle | deprecated plugin compatibility methods | no lifecycle API | daemon transport reuse is an implementation concern | Docker behavior tests | dead/unreachable code | Docker no longer acquires or releases frontend ControlMasters; retained methods are no-ops. |
| Plugin local forward active route | `DockerManager` service links via `PluginContext.ensure_local_forward` | `open_forward`, `get_forward` / `forwards.*` | daemon forward runtime | `tests/daemon/test_forward_*`, `tests/integration/test_forward_phase10.py` | API/daemon owned | The active route requires `DaemonClient`; the policy rejects legacy local processes. |
| Legacy local-forward compatibility | obsolete fallback removed from `PluginContext.ensure_local_forward` | None | None | forward daemon tests | dead/unreachable code | The frontend ControlMaster/`ssh -N` fallback was deleted; the plugin route requires the daemon forward service. |
| Obsolete direct SSH plugin helpers | removed `copy_key_to_host` / `get_effective_ssh_config` methods | daemon key/config APIs | None | API and key/config daemon tests | dead/unreachable code | The direct `ssh-copy-id` and `ssh -G` compatibility implementations were deleted. |
| Obsolete effective-config helper | removed `get_effective_ssh_config` method | daemon SSH config API | None | API config tests | dead/unreachable code | The direct `ssh -G` compatibility implementation was deleted. |
| Legacy `OpenSSHSFTPManager` | `file_manager/openssh_backend.py` | None | None in the current in-app route | daemon SFTP routing tests prove the active route | removed in this pass | The obsolete frontend backend and its exclusive tests are gone; `DaemonSftpManager` is the sole in-app route. |
| Legacy direct SSH terminal helpers | `TerminalWidget._connect_ssh*`, `_setup_ssh_terminal` | None | None in the current daemon activation route | `tests/test_daemon_terminal_activation_ownership.py` | removed in this pass | Reconnect and activation now require the daemon owner; no internal SSH fallback remains. |

## Supported PluginContext and facade inventory

The following is the complete public PluginContext/facade inventory in
`src/sshpilot/plugins/api.py`.  The count in the closing report is the number
of classified public identities (51), not a subprocess-hit count.  A grouped
row still lists every exact identity so that a new or rerouted capability must
update both this audit and `tests/architecture/test_frontend_closure.py`.

| Operation | Frontend entry point | Public API method/capability | Daemon owner | Headless test | Status | Notes |
|---|---|---|---|---|---|---|
| Plugin spawn context and protocol registration | plugin activation; daemon terminal launch dispatch | `PluginContext.for_spawn`, `PluginContext.register_protocol` | `ProtocolRegistry`; daemon launch provider consumes the registered backend | `tests/test_plugin_context_spawn.py`, `tests/test_daemon_terminal_activation_ownership.py` | API/daemon owned | Registration is a registry capability, not a GTK process fallback. Protocol `build_spawn` produces the normal daemon terminal launch specification. |
| Connection create/update/list | plugin connection panels and actions | `PluginContext.add_connection`, `PluginContext.update_connection`, `PluginContext.list_connections` | `DaemonConnectionServices` and `ConnectionApplicationService` through `SshPilotClient` | `tests/test_plugin_connection_services.py`, `tests/daemon/test_connection_mutations.py` | API/daemon owned | These routes already use typed connection requests and daemon-backed plugin-secret storage; no migration is requested here. |
| Connection/session open | plugin actions | `PluginContext.open_connection`, `PluginContext.open_command_terminal` | `SessionRuntime` and daemon terminal launch provider | `tests/test_plugin_host.py`, `tests/test_daemon_terminal_activation_ownership.py` | API/daemon owned | `PluginHost` selects the presentation tab, while `TerminalManager.connect_to_host` uses the current daemon route. |
| Local terminal presentation | plugin local-command actions | `PluginContext.open_local_command_terminal` | Local GTK terminal presentation | `tests/test_plugin_host.py` | legitimate frontend/platform-local | This is an OS-local shell tab, not a remote or SSH Pilot backend operation. |
| Group creation and assignment | plugin sidebar/group actions | `PluginContext.create_group`, `PluginContext.add_connection_to_group`, `PluginContext.add_connection_group` | daemon-backed `GroupMutationController`/`GroupManager` path | `tests/test_group_mutation_controller.py`, `tests/test_tag_groups.py` | API/daemon owned | The current window attaches the client-backed controller; plugin calls do not bypass it. |
| Plugin secrets | plugin credential actions and protocol spawn context | `PluginContext.get_secret`, `PluginContext.set_secret`, `PluginContext.delete_secret`, `_SecretStore.get`, `_SecretStore.set`, `_SecretStore.delete` | `DaemonConnectionServices` and protected secret service through `SshPilotClient` | `tests/daemon/test_secret_dispatch.py`, `tests/test_plugin_connection_services.py` | API/daemon owned | Existing daemon routing is recorded as correct and is not being migrated again. |
| Plugin settings | built-in plugin settings panels and plugin actions | `plugins.settings.get/set` | daemon `PluginSettingsService` and transactional settings store | daemon settings/API tests and built-in plugin tests | API/daemon owned | The daemon enforces `plugin_id` namespacing and JSON-safe values. |
| Plugin settings, operational state | Docker, EasyEnv, and Mock VPS plugin actions | `_SettingStore.get`, `_SettingStore.set` → `plugins.settings.get/set` | daemon `PluginSettingsService` and transactional settings store | daemon settings/API tests and built-in plugin tests | API/daemon owned | The daemon enforces `plugin_id` namespacing and JSON-safe values for all plugin operational state. |
| Identity listing and agent availability | plugin identity-aware actions and protocol spawn context | `_IdentityView.list`, `_IdentityView.is_agent_available` | daemon-owned identity state and native agent inspection through `SshPilotClient` (`identity.provider.keys.get`, `identity.providers.get`) | `tests/daemon/test_socket_identity_api.py`, `tests/daemon/test_identity_service_phase.py`, `tests/test_plugin_host.py` | API/daemon owned | `ctx.identities` is answered from daemon provider state: availability comes from the registry descriptor for the `'auto'` (system ssh-agent) provider even when another provider is selected, and identity listing runs the native `ssh-add -l` daemon-side against that provider's agent environment. The facade never runs `ssh-add` or reads a frontend `IdentityManager`. |
| Key generate/list | plugin key panels | `PluginContext.generate_key`, `PluginContext.list_keys` | daemon-backed `KeyManager`/`KeyController` | `tests/test_key_manager_daemon.py`, `tests/daemon/test_key_dispatch.py` | API/daemon owned | Existing KeyManager calls use the daemon-backed key service; GTK does not invoke `ssh-keygen` for these operations. |
| Key deletion | plugin key panels | `PluginContext.delete_key` → `delete_key` / `keys.delete` (`KEYS_WRITE`) | `DaemonKeyService` → GTK-free `KeyService.delete_key` | `tests/daemon/test_key_service.py`, `tests/daemon/test_key_dispatch.py`, `tests/daemon/test_socket_key_api.py`, plugin compatibility tests | API/daemon owned | The legacy private-path signature is compatibility-only: the host matches it against daemon-listed metadata and deletes by opaque `KeyId`; no frontend file operation remains. |
| Session open and command terminal | plugin actions | `PluginContext.open_connection`, `PluginContext.open_command_terminal` | daemon session runtime and terminal launch provider | `tests/test_daemon_terminal_activation_ownership.py` | API/daemon owned | The opening operation is daemon-owned even though GTK creates/selects the terminal tab. |
| Session list/read/input | plugin session observers and terminal actions | `PluginContext.list_sessions`, `PluginContext.read_terminal`, `PluginContext.send_terminal`; `PluginHost.list_sessions`, `PluginHost.read_terminal`, `PluginHost.send_terminal` | `SshPilotClient` session, replay, and terminal-input APIs | daemon session/replay/input tests and plugin host tests | API/daemon owned | The host projects daemon DTOs and replay bytes; widget references remain presentation/event bookkeeping only. |
| Captured remote commands | Docker/plugin actions | `PluginContext.run_command` → broadcast API | `BroadcastCommandService`/native runner | daemon broadcast/API tests and plugin command tests | API/daemon owned | Protected stdin never enters ordinary request parameters. |
| Streamed remote commands | Docker console/log follow | `PluginContext.run_command_stream` → broadcast output events | daemon broadcast operation and event publisher | daemon broadcast/API tests and Docker stream tests | API/daemon owned | Stop cancels the daemon operation; local stream helpers remain local. |
| Local commands | plugin-local actions | `PluginContext.run_local_command`, `PluginContext.run_local_command_stream` | Local plugin/OS process | plugin local-command tests | legitimate frontend/platform-local | These are explicitly local plugin commands and do not own SSH Pilot remote state. They remain narrow local exceptions in the guard. |
| Multiplex acquire/release | deprecated external plugin compatibility | `PluginContext.acquire_multiplex`, `PluginContext.release_multiplex` | none; daemon transport owns reuse | Docker behavior tests | dead/unreachable code | Both methods are compatibility no-ops and do not run SSH or touch frontend mux state. |
| Local forwarding, active route | Docker service links | `PluginContext.ensure_local_forward` | daemon forward runtime via `open_forward`/`get_forward` | `tests/daemon/test_forward_*`, `tests/integration/test_forward_phase10.py` | API/daemon owned | Current production policy requires the daemon route and rejects silent fallback. |
| Local forwarding, daemon-only route | `PluginContext.ensure_local_forward` | `open_forward` / `get_forward` | daemon forward runtime | daemon forward tests | API/daemon owned | The old frontend process branch was deleted; no ControlMaster or `ssh -N` fallback remains. |
| Plugin-local files and HTTP | plugin data/cache and external provider integrations | `_FilesFacade.path`, `_FilesFacade.exists`, `_FilesFacade.read_text`, `_FilesFacade.read_bytes`, `_FilesFacade.write_text`, `_FilesFacade.write_bytes`, `_HttpFacade.get`, `_HttpFacade.post`, `PluginContext.data_dir` | Plugin-private XDG data and OS/network adapters | plugin facade and built-in plugin tests | legitimate frontend/platform-local | These do not mutate SSH Pilot backend state, remote files, transfers, or daemon-owned configuration. |
| UI and event registration | plugin activation and panels | `_EventsFacade.subscribe`, `_EventsFacade.unsubscribe`, `_UiFacade.register_page`, `_UiFacade.open_page`, `_UiFacade.notify`, `_UiFacade.register_connection_action`, `_UiFacade.open_web_tab`, `PluginContext.run_on_ui_thread` | GTK/UI event bus and desktop presentation | plugin host/UI tests | legitimate frontend/platform-local | Registration, notifications, browser tabs, and UI-thread scheduling are presentation behavior. |

The following machine-readable classification is the synchronization source
checked by the closure guard.  It intentionally contains one row per public
identity; the prose matrix above remains the human-readable operation audit.

<!-- plugin-facade-classification:start -->
`PluginContext.daemon_client` | `API/daemon owned`
`PluginContext.for_spawn` | `API/daemon owned`
`PluginContext.register_protocol` | `API/daemon owned`
`PluginContext.add_connection` | `API/daemon owned`
`PluginContext.update_connection` | `API/daemon owned`
`PluginContext.list_connections` | `API/daemon owned`
`PluginContext.open_connection` | `API/daemon owned`
`PluginContext.open_command_terminal` | `API/daemon owned`
`PluginContext.open_local_command_terminal` | `legitimate frontend/platform-local`
`PluginContext.create_group` | `API/daemon owned`
`PluginContext.add_connection_to_group` | `API/daemon owned`
`PluginContext.add_connection_group` | `API/daemon owned`
`PluginContext.generate_key` | `API/daemon owned`
`PluginContext.list_keys` | `API/daemon owned`
`PluginContext.delete_key` | `API/daemon owned`
`PluginContext.run_command` | `API/daemon owned`
`PluginContext.run_local_command` | `legitimate frontend/platform-local`
`PluginContext.run_command_stream` | `API/daemon owned`
`PluginContext.run_local_command_stream` | `legitimate frontend/platform-local`
`PluginContext.acquire_multiplex` | `dead/unreachable code`
`PluginContext.release_multiplex` | `dead/unreachable code`
`PluginContext.ensure_local_forward` | `API/daemon owned`
`PluginContext.list_sessions` | `API/daemon owned`
`PluginContext.read_terminal` | `API/daemon owned`
`PluginContext.send_terminal` | `API/daemon owned`
`PluginContext.data_dir` | `legitimate frontend/platform-local`
`PluginContext.run_on_ui_thread` | `legitimate frontend/platform-local`
`PluginContext.get_secret` | `API/daemon owned`
`PluginContext.set_secret` | `API/daemon owned`
`PluginContext.delete_secret` | `API/daemon owned`
`_EventsFacade.subscribe` | `legitimate frontend/platform-local`
`_EventsFacade.unsubscribe` | `legitimate frontend/platform-local`
`_UiFacade.register_page` | `legitimate frontend/platform-local`
`_UiFacade.open_page` | `legitimate frontend/platform-local`
`_UiFacade.notify` | `legitimate frontend/platform-local`
`_UiFacade.register_connection_action` | `legitimate frontend/platform-local`
`_UiFacade.open_web_tab` | `legitimate frontend/platform-local`
`_SecretStore.get` | `API/daemon owned`
`_SecretStore.set` | `API/daemon owned`
`_SecretStore.delete` | `API/daemon owned`
`_IdentityView.list` | `API/daemon owned`
`_IdentityView.is_agent_available` | `API/daemon owned`
`_SettingStore.get` | `API/daemon owned`
`_SettingStore.set` | `API/daemon owned`
`_FilesFacade.path` | `legitimate frontend/platform-local`
`_FilesFacade.exists` | `legitimate frontend/platform-local`
`_FilesFacade.read_text` | `legitimate frontend/platform-local`
`_FilesFacade.read_bytes` | `legitimate frontend/platform-local`
`_FilesFacade.write_text` | `legitimate frontend/platform-local`
`_FilesFacade.write_bytes` | `legitimate frontend/platform-local`
`_HttpFacade.get` | `legitimate frontend/platform-local`
`_HttpFacade.post` | `legitimate frontend/platform-local`
<!-- plugin-facade-classification:end -->

Supporting implementation identities are synchronized separately because they
are not part of the public facade count:

<!-- plugin-supporting-classification:start -->
`PluginHost.list_sessions` | `API/daemon owned`
`PluginHost.read_terminal` | `API/daemon owned`
`PluginHost.send_terminal` | `API/daemon owned`
<!-- plugin-supporting-classification:end -->

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

### `BACKEND_OPS` debt (10 identities)

| Tag | Identities | Audit decision |
|---|---|---|
| M5 | `secret_storage.py: subprocess`, `secret_storage.py: SecretManager` | Shared secret-storage compatibility remains consumed by daemon/askpass paths. Bitwarden installation is classified as frontend/platform-local. |
| M7 | `askpass_utils.py: subprocess, ssh_binary`; `providers/system_agent.py: subprocess, ssh_binary`; `ssh_config_utils.py: subprocess, ssh_binary`; `ssh_multiplex.py: subprocess, ssh_binary` | These are daemon/shared native OpenSSH, askpass, effective-config, and ControlMaster compatibility. They remain because the daemon launch path still consumes them. |

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

## Closure blockers

Phase 7 is closed. Remote plugin commands and streams use the existing daemon
broadcast/native SSH service, settings use the daemon transaction store, and
session inspection/input use the typed session and terminal APIs. Multiplex
compatibility methods are inert no-ops.

## Required Phase 7 report

<!-- phase7-plugin-report:start -->
plugin capabilities audited: 52
api/daemon owned: 30
legitimate frontend/platform-local: 20
dead/unreachable compatibility: 2
migration-required public identities: 0
semantic migration capabilities: 0
<!-- phase7-plugin-report:end -->

These counts are derived from the classification registry by the closure
guard and include the two retained deprecated mux no-ops as dead compatibility
identities.

## Version and evidence

Public API implementation: `0.28`
Protocol: `1.0`  

The API/daemon routes are proven by the headless daemon/core/API tests named in
the matrix. The reference CLI is supporting evidence only; it is not required
for every operation. The final closure guard is
`tests/architecture/test_frontend_closure.py`; the existing boundary and debt
ratchet tests remain authoritative for the full registries.

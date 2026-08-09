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
| Plugin multiplex lifecycle | `DockerConsolePage._acquire_multiplex`, `_release_multiplex` | No typed multiplex lifecycle method | No daemon owner; `PluginContext.acquire_multiplex`/`release_multiplex` own the helper calls | `tests/test_ssh_multiplex.py` covers the local helper only | migration required | Both `acquire_multiplex()` and `release_multiplex()` are active Docker paths and are blocker P7-PLUGIN-MUX. |
| Plugin local forward active route | `DockerManager` service links via `PluginContext.ensure_local_forward` | `open_forward`, `get_forward` / `forwards.*` | daemon forward runtime | `tests/daemon/test_forward_*`, `tests/integration/test_forward_phase10.py` | API/daemon owned | The active route requires `DaemonClient`; the policy rejects legacy local processes. |
| Plugin legacy local-forward branch | legacy branch inside `PluginContext.ensure_local_forward` | None | None | `tests/test_extended_service_policy.py` | dead/unreachable code | `allow_legacy_local_forward()` is hardcoded false and daemon routing is preferred; the ControlMaster/`ssh -N` branch has no current production route. |
| Plugin key deployment compatibility method | `PluginContext.copy_key_to_host` | `deploy_key` exists, but this method does not use it | None in current production graph | no current production caller; daemon deploy tests cover the replacement API | dead/unreachable code | Retained public plugin compatibility surface; direct `ssh-copy-id` must not be reactivated. |
| Plugin effective-config compatibility method | `PluginContext.get_effective_ssh_config` | No typed plugin-facing method | None in current production graph | no current production caller; SSH config service tests cover authoritative config paths | dead/unreachable code | It delegates to the legacy local `ssh -G` helper and is not an active GTK operation. |
| Legacy `OpenSSHSFTPManager` | `file_manager/openssh_backend.py` | None | None in the current in-app route | daemon SFTP routing tests prove the active route | dead/unreachable code | No graph inbound path from the current file-manager route; retain only until compatibility removal is separately verified. |
| Legacy direct SSH terminal helpers | `TerminalWidget._connect_ssh*`, `_setup_ssh_terminal` | None | None in the current daemon activation route | `tests/test_daemon_terminal_activation_ownership.py` | dead/unreachable code | The normal route is daemon-only; these compatibility helpers are not a valid fallback. |

## Supported PluginContext and facade inventory

The following is the complete public PluginContext/facade inventory in
`src/sshpilot/plugins/api.py`.  The count in the closing report is the number
of classified public identities (53), not a subprocess-hit count.  A grouped
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
| Plugin settings, presentation state | built-in plugin settings panels | `_SettingStore.get`, `_SettingStore.set` | None for presentation-only keys | `tests/test_plugin_context.py`, built-in plugin tests | legitimate frontend/platform-local | Keys such as `last_host`, `refresh_interval`, `log_tail`, `max_log_lines`, and `show_all_containers` are presentation/local plugin state. The same facade is also used for operational settings below, so the identities remain classified as migration-required overall. |
| Plugin settings, operational state | Docker, EasyEnv, and Mock VPS plugin actions | `_SettingStore.get`, `_SettingStore.set` | No typed daemon settings owner for plugin operational state | built-in plugin tests only; no headless daemon proof | migration required | Active examples include Docker `runtime:<nickname>`, `runtime_mode:<nickname>`, `sudo:<nickname>`, and `controlmaster`, EasyEnv `account_uuid`/`base_url`, and Mock VPS `region`. The exact ownership blocker is `_SettingStore.set` (with `_SettingStore.get` required to read the same state); it is not frontend-only merely because it is namespaced under `plugins.*`. |
| Identity listing and agent availability | plugin identity-aware actions and protocol spawn context | `_IdentityView.list`, `_IdentityView.is_agent_available` | No daemon identity API route; process-wide `IdentityManager` and system-agent provider | `tests/daemon/test_identity_service.py`, `tests/test_agent_preload.py` cover daemon identity behavior, not this facade route | migration required | The public `ctx.identities` surface is usable and therefore is not dead compatibility. Its provider can execute local `ssh-add -l`; both exact facade identities remain blockers until routed through the daemon identity API. |
| Key generate/list | plugin key panels | `PluginContext.generate_key`, `PluginContext.list_keys` | daemon-backed `KeyManager`/`KeyController` | `tests/test_key_manager_daemon.py`, `tests/daemon/test_key_dispatch.py` | API/daemon owned | Existing KeyManager calls use the daemon-backed key service; GTK does not invoke `ssh-keygen` for these operations. |
| Key deletion | plugin key panels | `PluginContext.delete_key` → `delete_key` / `keys.delete` (`KEYS_WRITE`) | `DaemonKeyService` → GTK-free `KeyService.delete_key` | `tests/daemon/test_key_service.py`, `tests/daemon/test_key_dispatch.py`, `tests/daemon/test_socket_key_api.py`, plugin compatibility tests | API/daemon owned | The legacy private-path signature is compatibility-only: the host matches it against daemon-listed metadata and deletes by opaque `KeyId`; no frontend file operation remains. |
| Key deployment compatibility method | legacy plugin callers | `PluginContext.copy_key_to_host` | None in current production graph; `deploy_key` is the authoritative API | daemon key-deployment tests; no production caller for this method | dead/unreachable code | This is the known direct `ssh-copy-id` compatibility path. It is not a replacement for the existing daemon deploy route and must not be reactivated. |
| Session open and command terminal | plugin actions | `PluginContext.open_connection`, `PluginContext.open_command_terminal` | daemon session runtime and terminal launch provider | `tests/test_daemon_terminal_activation_ownership.py` | API/daemon owned | The opening operation is daemon-owned even though GTK creates/selects the terminal tab. |
| Session list/read/input | plugin session observers and terminal actions | `PluginContext.list_sessions`, `PluginContext.read_terminal`, `PluginContext.send_terminal`; `PluginHost.list_sessions`, `PluginHost.read_terminal`, `PluginHost.send_terminal` | No typed client route; GTK terminal-session bookkeeping and widget methods | `tests/test_plugin_send_terminal.py` and plugin host tests cover the widget path only | migration required | These methods are supported and active. They read VTE/widget content or call `feed_child_data`; they are not legitimate presentation-only behavior when exposed as backend session APIs. |
| Captured remote commands | Docker/plugin actions | `PluginContext.run_command` | No daemon owner; plugin builds/owns the remote process | plugin compatibility tests only; no headless client proof | migration required | Exact blocker P7-PLUGIN-COMMAND. |
| Streamed remote commands | Docker console/log follow | `PluginContext.run_command_stream`, `PluginContext._spawn_stream` | No daemon operation/event owner; plugin owns process and stream lifecycle | plugin stream tests only; no headless client proof | migration required | Exact blocker P7-PLUGIN-STREAM. `_finish_stream_early` is a supporting private stream-lifecycle identity, not a separate public capability. |
| Local commands | plugin-local actions | `PluginContext.run_local_command`, `PluginContext.run_local_command_stream` | Local plugin/OS process | plugin local-command tests | legitimate frontend/platform-local | These are explicitly local plugin commands and do not own SSH Pilot remote state. They remain narrow local exceptions in the guard. |
| Multiplex acquire/release | `DockerConsolePage._acquire_multiplex`, `_release_multiplex` | `PluginContext.acquire_multiplex`, `PluginContext.release_multiplex` | No daemon multiplex owner; plugin helper invokes ControlMaster operations | `tests/test_ssh_multiplex.py` covers the helper only | migration required | Exact blocker P7-PLUGIN-MUX covers both identities. The guard recognizes both identities and recognizes `from .. import ssh_multiplex`. |
| Local forwarding, active route | Docker service links | `PluginContext.ensure_local_forward` | daemon forward runtime via `open_forward`/`get_forward` | `tests/daemon/test_forward_*`, `tests/integration/test_forward_phase10.py` | API/daemon owned | Current production policy requires the daemon route and rejects silent fallback. |
| Local forwarding, legacy process branch | obsolete fallback inside forward facade | legacy branch of `PluginContext.ensure_local_forward` | None | `tests/test_extended_service_policy.py` | dead/unreachable code | `allow_legacy_local_forward()` is hardcoded false; the ControlMaster/`ssh -N` branch is separately recorded as dead compatibility, not exempted by the active method’s daemon route. |
| Effective SSH config compatibility method | legacy plugin callers | `PluginContext.get_effective_ssh_config` | None; delegates to local `ssh -G` helper | no current production caller; SSH config service tests cover authoritative paths | dead/unreachable code | No current GTK/plugin call path was found. It remains explicitly classified because it is still public compatibility surface. |
| Plugin-local files and HTTP | plugin data/cache and external provider integrations | `_FilesFacade.path`, `_FilesFacade.exists`, `_FilesFacade.read_text`, `_FilesFacade.read_bytes`, `_FilesFacade.write_text`, `_FilesFacade.write_bytes`, `_HttpFacade.get`, `_HttpFacade.post`, `PluginContext.data_dir` | Plugin-private XDG data and OS/network adapters | plugin facade and built-in plugin tests | legitimate frontend/platform-local | These do not mutate SSH Pilot backend state, remote files, transfers, or daemon-owned configuration. |
| UI and event registration | plugin activation and panels | `_EventsFacade.subscribe`, `_EventsFacade.unsubscribe`, `_UiFacade.register_page`, `_UiFacade.open_page`, `_UiFacade.notify`, `_UiFacade.register_connection_action`, `_UiFacade.open_web_tab`, `PluginContext.run_on_ui_thread` | GTK/UI event bus and desktop presentation | plugin host/UI tests | legitimate frontend/platform-local | Registration, notifications, browser tabs, and UI-thread scheduling are presentation behavior. |

The following machine-readable classification is the synchronization source
checked by the closure guard.  It intentionally contains one row per public
identity; the prose matrix above remains the human-readable operation audit.

<!-- plugin-facade-classification:start -->
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
`PluginContext.run_command` | `migration required`
`PluginContext.run_local_command` | `legitimate frontend/platform-local`
`PluginContext.run_command_stream` | `migration required`
`PluginContext.run_local_command_stream` | `legitimate frontend/platform-local`
`PluginContext.acquire_multiplex` | `migration required`
`PluginContext.release_multiplex` | `migration required`
`PluginContext.ensure_local_forward` | `API/daemon owned`
`PluginContext.get_effective_ssh_config` | `dead/unreachable code`
`PluginContext.copy_key_to_host` | `dead/unreachable code`
`PluginContext.list_sessions` | `migration required`
`PluginContext.read_terminal` | `migration required`
`PluginContext.send_terminal` | `migration required`
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
`_IdentityView.list` | `migration required`
`_IdentityView.is_agent_available` | `migration required`
`_SettingStore.get` | `migration required`
`_SettingStore.set` | `migration required`
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
are not part of the 53 public facade count:

<!-- plugin-supporting-classification:start -->
`PluginHost.list_sessions` | `migration required`
`PluginHost.read_terminal` | `migration required`
`PluginHost.send_terminal` | `migration required`
`PluginContext._spawn_stream` | `migration required`
`PluginContext._finish_stream_early` | `migration required`
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

### `BACKEND_OPS` debt (18 identities)

| Tag | Identities | Audit decision |
|---|---|---|
| M5 | `secret_storage.py: subprocess`, `secret_storage.py: SecretManager`, `bitwarden_setup.py: subprocess` | The first two are shared daemon/askpass compatibility; Bitwarden installation is a narrow platform installer. No active GTK vault owner remains. |
| M7 | `agent_client.py: subprocess`; `askpass_utils.py: subprocess, ssh_binary`; `autocomplete.py: subprocess`; `file_manager/openssh_backend.py: subprocess`; `providers/system_agent.py: subprocess, ssh_binary`; `scp_utils.py: subprocess`; `sftp_utils.py: subprocess`; `ssh_config_utils.py: subprocess, ssh_binary`; `ssh_multiplex.py: subprocess, ssh_binary`; `terminal.py: subprocess` | These are respectively local helper, daemon askpass/identity compatibility, dead legacy routes, external OS presentation, or shared native-process compatibility. ControlMaster teardown was removed from GTK and moved behind `SshOverridesService`; the remaining helper identity is compatibility debt. |
| M8 | `plugins/api.py: subprocess` | Function-level identities are now audited by the final guard: active `run_command`, `run_command_stream`/`_spawn_stream`, and both `acquire_multiplex` and `release_multiplex` remain closure blockers; `copy_key_to_host`, `get_effective_ssh_config`, and the legacy branch of `ensure_local_forward` are explicitly dead compatibility identities. The separate facade inventory also records settings, identity, and widget-backed session blockers that do not contain subprocess calls; key deletion is now daemon-owned. |

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

Remaining migration blockers: **6 semantic capabilities** (**11 public facade
identities**, plus the supporting `PluginHost`/stream implementation
identities listed below).

* `P7-PLUGIN-COMMAND`: add a typed daemon-owned semantic for plugin remote
  command execution, including captured output and exit status. Exact identity:
  `PluginContext.run_command`.
* `P7-PLUGIN-STREAM`: add the daemon operation/event lifecycle needed for
  long-lived plugin command output and cancellation. Exact identities:
  `PluginContext.run_command_stream`, `PluginContext._spawn_stream`, and
  supporting `PluginContext._finish_stream_early`.
* `P7-PLUGIN-MUX`: move plugin multiplex reference release/ControlMaster
  teardown behind daemon-owned session/forward runtime semantics. Exact
  identities: `PluginContext.acquire_multiplex` and
  `PluginContext.release_multiplex`.
* `P7-PLUGIN-SETTINGS`: route operational plugin settings through an
  authoritative typed owner. Exact identities: `_SettingStore.get` and
  `_SettingStore.set`; presentation-only keys are separately allowed, but the
  shared facade cannot currently distinguish ownership at the API boundary.
* `P7-PLUGIN-IDENTITIES`: route the supported plugin identity view through the
  daemon identity API. Exact identities: `_IdentityView.list` and
  `_IdentityView.is_agent_available`; the current system-agent provider can
  execute local `ssh-add -l`.
* `P7-PLUGIN-SESSION-VIEW`: replace widget-backed plugin session inspection
  and input with typed session/terminal API operations. Exact identities:
  `PluginContext.list_sessions`, `PluginContext.read_terminal`,
  `PluginContext.send_terminal`, `PluginHost.list_sessions`,
  `PluginHost.read_terminal`, and `PluginHost.send_terminal`.

These are intentionally not replaced with a generic `run_command` escape
hatch: that would move the bypass behind another frontend-owned abstraction.
The existing public API therefore remains unchanged until the semantic plugin
contract is designed and tested headlessly.

## Required Phase 7 report

<!-- phase7-plugin-report:start -->
plugin capabilities audited: 53
api/daemon owned: 20
legitimate frontend/platform-local: 20
dead/unreachable compatibility: 2
migration-required public identities: 11
semantic migration capabilities: 6
<!-- phase7-plugin-report:end -->

These counts are derived from the classification registry by the closure
guard.  They are identity-based and deliberately include the mixed settings
facade as migration-required because it is used for operational state. No
other plugin migration is implemented in this correction; this slice closes
only `P7-PLUGIN-KEY-DELETE`, and the other six semantic blockers remain.

## Version and evidence

Public API implementation: `0.26`
Protocol: `1.0`  

The API/daemon routes are proven by the headless daemon/core/API tests named in
the matrix. The reference CLI is supporting evidence only; it is not required
for every operation. The final closure guard is
`tests/architecture/test_frontend_closure.py`; the existing boundary and debt
ratchet tests remain authoritative for the full registries.

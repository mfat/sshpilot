# Client methods

Current API implementation version: `0.40`.
Protocol v1 remains `1.0`.
See [CHANGELOG.md](CHANGELOG.md) for version history.

<!-- api-method: start_broadcast_command -->
<!-- api-method: subscribe_broadcast_output -->
<!-- api-method: get_plugin_setting -->
<!-- api-method: prepare_external_terminal_launch -->
<!-- api-method: get_launch_command -->
<!-- api-method: get_effective_config -->
<!-- api-method: check_unsaved_host -->
<!-- api-method: set_operation_mode -->
<!-- api-method: get_operation_mode -->
<!-- api-method: set_plugin_setting -->
<!-- api-method: clear_session_connection_password -->
<!-- api-method-contract: clear_session_connection_password status=implemented capability=connections.secrets.write -->
<!-- api-method: get_broadcast_command -->
<!-- api-method: cancel_broadcast_command -->
<!-- api-method: set_daemon_log_level -->
<!-- api-method: broadcast_terminal_input -->
<!-- api-method-contract: start_broadcast_command status=schema-only capability=broadcast.write -->
<!-- api-method-contract: subscribe_broadcast_output status=daemon-only capability=broadcast.events -->
<!-- api-method-contract: get_broadcast_command status=schema-only capability=broadcast.read -->
<!-- api-method-contract: cancel_broadcast_command status=schema-only capability=broadcast.write -->
<!-- api-method-contract: broadcast_terminal_input status=daemon-only capability=terminal.input -->
<!-- api-daemon-method: broadcast.start capability=broadcast.write -->
<!-- api-daemon-method: plugins.settings.get capability=plugins.settings.read -->
<!-- api-daemon-method: plugins.settings.set capability=plugins.settings.write -->
<!-- api-daemon-method: connections.clear_session_password capability=connections.secrets.write -->
<!-- api-daemon-method: broadcast.get capability=broadcast.read -->
<!-- api-daemon-method: broadcast.cancel capability=broadcast.write -->
<!-- api-daemon-method: terminal.broadcast_input capability=terminal.input -->

`SshPilotClient` is synchronous. Production uses the daemon transport only;
direct core service compositions are test-only and are not client choices.

## Runtime summary

| Method | Status | Capability |
| --- | --- | --- |
| `get_capabilities` | Implemented | Bootstrap; none |
| `list_connections` | Implemented | `connections.read` |
| `get_connection` | Implemented | `connections.read` |
| `create_connection` | Implemented | `connections.write` |
| `duplicate_connection` | Implemented | `connections.write` |
| `update_connection` | Implemented | `connections.write` |
| `delete_connection` | Implemented | `connections.write` |
| `get_connection_editor` | Implemented | `connections.config.read` |
| `set_operation_mode` | Daemon only | `operation.mode` |
| `get_operation_mode` | Daemon only | `operation.mode` |
| `check_unsaved_host` | Implemented | `connections.read` |
| `prepare_external_terminal_launch` | Daemon only | `terminal.external_launch` |
| `get_launch_command` | Implemented | `terminal.external_launch` |
| `get_effective_config` | Implemented | `connections.config.read` |
| `store_connection_password` | Implemented | `connections.secrets.write` |
| `clear_session_connection_password` | Daemon only | `connections.secrets.write` |
| `has_connection_password` | Daemon only | `connections.secrets.status.read` |
| `reveal_connection_password` | Daemon only | `connections.secrets.reveal` |
| `delete_connection_password` | Implemented | `connections.secrets.write` |
| `store_key_passphrase` | Implemented | `connections.secrets.write` |
| `has_key_passphrase` | Daemon only | `connections.secrets.status.read` |
| `reveal_key_passphrase` | Daemon only | `connections.secrets.reveal` |
| `delete_key_passphrase` | Implemented | `connections.secrets.write` |
| `list_known_hosts` | Daemon only | `known_hosts.read` |
| `remove_known_host_entries` | Daemon only | `known_hosts.write` |
| `list_keys` | Daemon only | `keys.read` |
| `delete_key` | Daemon only | `keys.write` |
| `read_public_key` | Daemon only | `keys.read` |
| `generate_key` | Daemon only | `keys.write` |
| `list_sessions` | Daemon only | `sessions.read` |
| `get_session` | Daemon only | `sessions.read` |
| `open_session` | Daemon only | `sessions.write` (+ `sessions.command` when a `remote_command` is supplied) |
| `attach_session` | Daemon only | `sessions.write` |
| `detach_session` | Daemon only | `sessions.write` |
| `close_session` | Daemon only | `sessions.write` |
| `send_terminal_input` | Daemon only | `terminal.input` |
| `broadcast_terminal_input` | Daemon only | `terminal.input` |
| `resize_terminal` | Daemon only | `terminal.resize` |
| `replay_terminal` | Daemon only | `terminal.replay` |
| `claim_terminal_input` | Daemon only | `terminal.input` |
| `release_terminal_input` | Daemon only | `terminal.input` |
| `subscribe_terminal` | Daemon only | `terminal.output` |
| `subscribe_broadcast_output` | Daemon only | `broadcast.events` |
| `get_plugin_setting` | Daemon only | `plugins.settings.read` |
| `set_plugin_setting` | Daemon only | `plugins.settings.write` |
| `list_interactions` | Daemon only | `interactions.read` |
| `get_interaction` | Daemon only | `interactions.read` |
| `claim_interaction` | Daemon only | `interactions.respond` |
| `release_interaction` | Daemon only | `interactions.respond` |
| `respond_to_interaction` | Daemon only | `interactions.respond` |
| `cancel_interaction` | Daemon only | `interactions.respond` |
| `send_interaction_secret` | Daemon only | `interactions.respond` |
| `list_sftp_services` | Daemon only | `sftp.read` |
| `get_sftp_service` | Daemon only | `sftp.read` |
| `open_sftp` | Daemon only | `sftp.write` |
| `attach_sftp` | Daemon only | `sftp.write` |
| `detach_sftp` | Daemon only | `sftp.write` |
| `close_sftp` | Daemon only | `sftp.write` |
| `sftp_list_directory` | Daemon only | `sftp.read` |
| `sftp_stat` | Daemon only | `sftp.metadata` |
| `sftp_directory_size` | Daemon only | `sftp.read` |
| `sftp_lstat` | Daemon only | `sftp.metadata` |
| `sftp_realpath` | Daemon only | `sftp.metadata` |
| `sftp_readlink` | Daemon only | `sftp.metadata` |
| `sftp_mkdir` | Daemon only | `sftp.mutate` |
| `sftp_create_file` | Daemon only | `sftp.mutate` |
| `sftp_rmdir` | Daemon only | `sftp.mutate` |
| `sftp_remove` | Daemon only | `sftp.mutate` |
| `sftp_rename` | Daemon only | `sftp.mutate` |
| `sftp_chmod` | Daemon only | `sftp.mutate` |
| `sftp_symlink` | Daemon only | `sftp.mutate` |
| `list_transfers` | Daemon only | `transfers.read` |
| `get_transfer` | Daemon only | `transfers.read` |
| `start_transfer` | Daemon only | `transfers.write` |
| `start_scp_transfer` | Daemon only | `transfers.scp` |
| `cancel_transfer` | Daemon only | `transfers.write` |
| `list_forwards` | Daemon only | `forwards.read` |
| `get_forward` | Daemon only | `forwards.read` |
| `open_forward` | Daemon only | `forwards.write` |
| `claim_forward` | Daemon only | `forwards.write` |
| `close_forward` | Daemon only | `forwards.write` |
| `get_daemon_status` | Daemon only | `daemon.status` |
| `get_daemon_diagnostics` | Daemon only | `daemon.status` |
| `stop_daemon` | Daemon only | `daemon.control` |
| `restart_daemon` | Daemon only | `daemon.control` |
| `set_daemon_log_level` | Daemon only | `daemon.control` |
| `subscribe_events` | Implemented | Bootstrap; event availability follows capabilities |
| `close` | Implemented | None |

<!-- api-method-contract: get_daemon_diagnostics status=daemon-only capability=daemon.status -->
<!-- api-method-contract: get_daemon_status status=daemon-only capability=daemon.status -->
<!-- api-method-contract: restart_daemon status=daemon-only capability=daemon.control -->
<!-- api-method-contract: stop_daemon status=daemon-only capability=daemon.control -->
<!-- api-method-contract: set_daemon_log_level status=daemon-only capability=daemon.control -->
<!-- api-method-contract: attach_session status=daemon-only capability=sessions.write -->
<!-- api-method-contract: attach_sftp status=daemon-only capability=sftp.write -->
<!-- api-method-contract: cancel_interaction status=daemon-only capability=interactions.respond -->
<!-- api-method-contract: cancel_transfer status=daemon-only capability=transfers.write -->
<!-- api-method-contract: claim_interaction status=daemon-only capability=interactions.respond -->
<!-- api-method-contract: claim_forward status=daemon-only capability=forwards.write -->
<!-- api-method-contract: close status=implemented capability=none -->
<!-- api-method-contract: close_forward status=daemon-only capability=forwards.write -->
<!-- api-method-contract: close_session status=daemon-only capability=sessions.write -->
<!-- api-method-contract: close_sftp status=daemon-only capability=sftp.write -->
<!-- api-method-contract: create_connection status=implemented capability=connections.write -->
<!-- api-method-contract: duplicate_connection status=implemented capability=connections.write -->
<!-- api-method-contract: delete_connection status=implemented capability=connections.write -->
<!-- api-method-contract: delete_connection_password status=implemented capability=connections.secrets.write -->
<!-- api-method-contract: detach_session status=daemon-only capability=sessions.write -->
<!-- api-method-contract: detach_sftp status=daemon-only capability=sftp.write -->
<!-- api-method-contract: get_capabilities status=implemented capability=none -->
<!-- api-method-contract: get_connection status=implemented capability=connections.read -->
<!-- api-method-contract: get_connection_editor status=implemented capability=connections.config.read -->
<!-- api-method-contract: get_plugin_secret status=daemon-only capability=connections.secrets.reveal -->
<!-- api-method-contract: get_plugin_setting status=daemon-only capability=plugins.settings.read -->
<!-- api-method-contract: get_ssh_config_text status=implemented capability=connections.config.read -->
<!-- api-method-contract: prepare_external_terminal_launch status=implemented capability=terminal.external_launch -->
<!-- api-method-contract: get_launch_command status=implemented capability=terminal.external_launch -->
<!-- api-method-contract: get_effective_config status=implemented capability=connections.config.read -->
<!-- api-method-contract: check_unsaved_host status=implemented capability=connections.read -->
<!-- api-method-contract: set_operation_mode status=daemon-only capability=operation.mode -->
<!-- api-method-contract: get_operation_mode status=daemon-only capability=operation.mode -->
<!-- api-method-contract: save_ssh_config_text status=implemented capability=connections.config.write -->
<!-- api-method-contract: get_forward status=daemon-only capability=forwards.read -->
<!-- api-method-contract: get_interaction status=daemon-only capability=interactions.read -->
<!-- api-method-contract: get_session status=daemon-only capability=sessions.read -->
<!-- api-method-contract: get_sftp_service status=daemon-only capability=sftp.read -->
<!-- api-method-contract: get_transfer status=daemon-only capability=transfers.read -->
<!-- api-method-contract: list_connections status=implemented capability=connections.read -->
<!-- api-method-contract: list_forwards status=daemon-only capability=forwards.read -->
<!-- api-method-contract: list_interactions status=daemon-only capability=interactions.read -->
<!-- api-method-contract: list_sessions status=daemon-only capability=sessions.read -->
<!-- api-method-contract: list_sftp_services status=daemon-only capability=sftp.read -->
<!-- api-method-contract: list_transfers status=daemon-only capability=transfers.read -->
<!-- api-method-contract: has_connection_password status=daemon-only capability=connections.secrets.status.read -->
<!-- api-method-contract: has_key_passphrase status=daemon-only capability=connections.secrets.status.read -->
<!-- api-method-contract: reveal_connection_password status=daemon-only capability=connections.secrets.reveal -->
<!-- api-method-contract: reveal_key_passphrase status=daemon-only capability=connections.secrets.reveal -->
<!-- api-method-contract: open_forward status=daemon-only capability=forwards.write -->
<!-- api-method-contract: open_session status=daemon-only capability=sessions.write -->
<!-- api-method-contract: open_sftp status=daemon-only capability=sftp.write -->
<!-- api-method-contract: replay_terminal status=daemon-only capability=terminal.replay -->
<!-- api-method-contract: release_interaction status=daemon-only capability=interactions.respond -->
<!-- api-method-contract: resize_terminal status=daemon-only capability=terminal.resize -->
<!-- api-method-contract: respond_to_interaction status=daemon-only capability=interactions.respond -->
<!-- api-method-contract: send_interaction_secret status=daemon-only capability=interactions.respond -->
<!-- api-method-contract: send_terminal_input status=daemon-only capability=terminal.input -->
<!-- api-method-contract: set_plugin_setting status=daemon-only capability=plugins.settings.write -->
<!-- api-method-contract: sftp_chmod status=daemon-only capability=sftp.mutate -->
<!-- api-method-contract: sftp_list_directory status=daemon-only capability=sftp.read -->
<!-- api-method-contract: sftp_lstat status=daemon-only capability=sftp.metadata -->
<!-- api-method-contract: sftp_mkdir status=daemon-only capability=sftp.mutate -->
<!-- api-method-contract: sftp_create_file status=daemon-only capability=sftp.mutate -->
<!-- api-method-contract: sftp_copy status=daemon-only capability=sftp.mutate -->
<!-- api-method-contract: sftp_read_file status=daemon-only capability=sftp.read -->
<!-- api-method-contract: sftp_readlink status=daemon-only capability=sftp.metadata -->
<!-- api-method-contract: sftp_realpath status=daemon-only capability=sftp.metadata -->
<!-- api-method-contract: sftp_remove status=daemon-only capability=sftp.mutate -->
<!-- api-method-contract: sftp_rename status=daemon-only capability=sftp.mutate -->
<!-- api-method-contract: sftp_replace_file status=daemon-only capability=sftp.mutate -->
<!-- api-method-contract: sftp_rmdir status=daemon-only capability=sftp.mutate -->
<!-- api-method-contract: sftp_stat status=daemon-only capability=sftp.metadata -->
<!-- api-method-contract: sftp_directory_size status=daemon-only capability=sftp.read -->
<!-- api-method-contract: sftp_symlink status=daemon-only capability=sftp.mutate -->
<!-- api-method-contract: start_scp_transfer status=daemon-only capability=transfers.scp -->
<!-- api-method-contract: start_transfer status=daemon-only capability=transfers.write -->
<!-- api-method-contract: store_connection_password status=implemented capability=connections.secrets.write -->
<!-- api-method-contract: set_session_connection_password status=implemented capability=connections.secrets.write -->
<!-- api-method-contract: store_key_passphrase status=implemented capability=connections.secrets.write -->
<!-- api-method-contract: delete_key_passphrase status=implemented capability=connections.secrets.write -->
<!-- api-method-contract: claim_terminal_input status=daemon-only capability=terminal.input -->
<!-- api-method-contract: release_terminal_input status=daemon-only capability=terminal.input -->
<!-- api-method-contract: subscribe_terminal status=daemon-only capability=terminal.output -->
<!-- api-daemon-method: daemon.set_log_level capability=daemon.control -->
<!-- api-method-contract: subscribe_events status=implemented capability=connections.events -->
<!-- api-method-contract: update_connection status=implemented capability=connections.write -->
<!-- api-method-contract: update_connection_metadata status=implemented capability=connections.metadata.write -->
<!-- api-method-contract: add_tag_to_connections status=implemented capability=connections.metadata.write -->
<!-- api-method-contract: assign_connection_to_group status=implemented capability=connections.groups -->
<!-- api-method-contract: move_connections status=daemon-only capability=connections.groups -->
<!-- api-method-contract: create_group status=implemented capability=connections.groups -->
<!-- api-method-contract: delete_group status=implemented capability=connections.groups -->
<!-- api-method-contract: rename_group status=implemented capability=connections.groups -->
<!-- api-method-contract: split_connection status=implemented capability=connections.split -->
<!-- api-method-contract: get_global_ssh_overrides status=implemented capability=ssh_overrides.read -->
<!-- api-method-contract: update_global_ssh_overrides status=implemented capability=ssh_overrides.write -->
<!-- api-method-contract: reset_global_ssh_overrides status=implemented capability=ssh_overrides.write -->
<!-- api-method-contract: bitwarden_api_key_login status=daemon-only capability=secrets.operate -->
<!-- api-method-contract: bitwarden_configure_server status=daemon-only capability=secrets.operate -->
<!-- api-method-contract: bitwarden_lock status=daemon-only capability=secrets.operate -->
<!-- api-method-contract: bitwarden_login status=daemon-only capability=secrets.operate -->
<!-- api-method-contract: bitwarden_logout status=daemon-only capability=secrets.operate -->
<!-- api-method-contract: bitwarden_sso_login status=daemon-only capability=secrets.operate -->
<!-- api-method-contract: bitwarden_status status=daemon-only capability=secrets.operate -->
<!-- api-method-contract: bitwarden_sync status=daemon-only capability=secrets.operate -->
<!-- api-method-contract: bitwarden_unlock status=daemon-only capability=secrets.operate -->
<!-- api-method-contract: export_secret_backup status=daemon-only capability=secrets.transfer -->
<!-- api-method-contract: get_secret_backends status=daemon-only capability=secrets.read -->
<!-- api-method-contract: get_secret_configuration status=daemon-only capability=secrets.read -->
<!-- api-method-contract: get_secret_state status=daemon-only capability=secrets.read -->
<!-- api-method-contract: import_secret_backup status=daemon-only capability=secrets.transfer -->
<!-- api-method-contract: lock_secrets status=daemon-only capability=secrets.operate -->
<!-- api-method-contract: rbw_configure status=daemon-only capability=secrets.operate -->
<!-- api-method-contract: rbw_lock status=daemon-only capability=secrets.operate -->
<!-- api-method-contract: rbw_status status=daemon-only capability=secrets.operate -->
<!-- api-method-contract: rbw_sync status=daemon-only capability=secrets.operate -->
<!-- api-method-contract: rbw_unlock status=daemon-only capability=secrets.operate -->
<!-- api-method-contract: unlock_secrets status=daemon-only capability=secrets.operate -->
<!-- api-method-contract: update_secret_configuration status=daemon-only capability=secrets.write -->
<!-- api-method-contract: update_secret_selection status=daemon-only capability=secrets.write -->
<!-- api-method-contract: forget_master_password status=daemon-only capability=secrets.operate -->
<!-- api-method-contract: import_bitwarden_backup status=daemon-only capability=secrets.transfer -->
<!-- api-method-contract: import_ssh_backup status=daemon-only capability=secrets.transfer -->
<!-- api-method-contract: keepassxc_create_database status=daemon-only capability=secrets.operate -->
<!-- api-method-contract: keepassxc_lock status=daemon-only capability=secrets.operate -->
<!-- api-method-contract: keepassxc_unlock status=daemon-only capability=secrets.operate -->
<!-- api-method-contract: list_bitwarden_backups status=daemon-only capability=secrets.transfer -->
<!-- api-method-contract: list_ssh_backups status=daemon-only capability=secrets.transfer -->
<!-- api-method-contract: preview_backup status=daemon-only capability=secrets.transfer -->
<!-- api-method-contract: preview_bitwarden_backup status=daemon-only capability=secrets.transfer -->
<!-- api-method-contract: preview_ssh_backup status=daemon-only capability=secrets.transfer -->
<!-- api-method-contract: remember_master_password status=daemon-only capability=secrets.operate -->
<!-- api-method: get_connection_store_snapshot -->
<!-- api-method: set_group_color -->
<!-- api-method: place_group -->
<!-- api-method: copy_connection_to_group -->
<!-- api-method: remove_connection_from_group -->
<!-- api-method: reorder_connection -->
<!-- api-method: rename_tag -->
<!-- api-method: add_tag_to_connections -->
<!-- api-method: sftp_copy -->
<!-- api-method-contract: add_agent_key status=daemon-only capability=identity.operate -->
<!-- api-method-contract: cancel_operation status=daemon-only capability=operations.control -->
<!-- api-method-contract: deploy_key status=daemon-only capability=identity.operate -->
<!-- api-method-contract: get_identity_providers status=daemon-only capability=identity.read -->
<!-- api-method-contract: get_identity_state status=daemon-only capability=identity.read -->
<!-- api-method-contract: get_operation status=daemon-only capability=operations.read -->
<!-- api-method-contract: list_agent_keys status=daemon-only capability=identity.read -->
<!-- api-method-contract: list_provider_agent_keys status=daemon-only capability=identity.read -->
<!-- api-method-contract: list_authorized_keys status=daemon-only capability=identity.read -->
<!-- api-method-contract: remove_agent_key status=daemon-only capability=identity.operate -->
<!-- api-method-contract: remove_authorized_key status=daemon-only capability=identity.operate -->
<!-- api-method-contract: update_identity_configuration status=daemon-only capability=identity.write -->
<!-- api-method-contract: update_identity_selection status=daemon-only capability=identity.write -->

## Daemon wire methods

The dispatcher is an explicit allowlist; it never reflects over Python objects.

| Wire method | Capability | Status |
| --- | --- | --- |
| `system.handshake` | None | Implemented; required exactly once |
| `system.get_capabilities` | None | Implemented after handshake |
| `connections.list` | `connections.read` | Implemented |
| `connections.snapshot` | `connections.read` | Implemented; complete immutable store snapshot |
<!-- api-daemon-method: connections.snapshot capability=connections.read -->
| `connections.get` | `connections.read` | Implemented |
| `connections.create` | `connections.write` | Implemented |
| `connections.duplicate` | `connections.write` | Implemented |
| `connections.update` | `connections.write` | Implemented |
| `connections.delete` | `connections.write` | Implemented |
| `connections.get_editor` | `connections.config.read` | Implemented |
| `connections.get_ssh_config_text` | `connections.config.read` | Implemented |
| `connections.save_ssh_config_text` | `connections.config.write` | Implemented |
| `connections.store_password` | `connections.secrets.write` | Implemented |
| `connections.delete_password` | `connections.secrets.write` | Implemented |
| `connections.store_passphrase` | `connections.secrets.write` | Implemented |
| `connections.delete_passphrase` | `connections.secrets.write` | Implemented |
| `connections.has_password` | `connections.secrets.status.read` | Implemented |
| `connections.has_passphrase` | `connections.secrets.status.read` | Implemented |
| `connections.reveal_password` | `connections.secrets.reveal` | Implemented; binary secret response |
| `connections.reveal_passphrase` | `connections.secrets.reveal` | Implemented; binary secret response |
| `connections.store_plugin_secret` | `connections.secrets.write` | Implemented |
| `connections.get_plugin_secret` | `connections.secrets.reveal` | Implemented; binary secret response |
| `connections.delete_plugin_secret` | `connections.secrets.write` | Implemented |
| `connections.update_metadata` | `connections.metadata.write` | Implemented |
| `connections.metadata.update` | `connections.metadata.write` | Implemented |
| `connections.metadata.rename_tag` | `connections.metadata.write` | Implemented |
| `connections.metadata.add_tag` | `connections.metadata.write` | Implemented |
<!-- api-daemon-method: connections.metadata.update capability=connections.metadata.write -->
<!-- api-daemon-method: connections.metadata.rename_tag capability=connections.metadata.write -->
<!-- api-daemon-method: connections.metadata.add_tag capability=connections.metadata.write -->
| `connections.assign_to_group` | `connections.groups` | Implemented |
| `connections.move` | `connections.groups` | Implemented; atomic multi-connection placement |
| `connections.create_group` | `connections.groups` | Implemented |
| `connections.delete_group` | `connections.groups` | Implemented |
| `connections.rename_group` | `connections.groups` | Implemented |
| `groups.create` | `connections.groups` | Implemented |
| `groups.delete` | `connections.groups` | Implemented |
| `groups.rename` | `connections.groups` | Implemented |
| `groups.set_color` | `connections.groups` | Implemented |
| `groups.place` | `connections.groups` | Implemented; revision-safe group placement |
| `groups.copy_connection` | `connections.groups` | Implemented |
| `groups.remove_connection` | `connections.groups` | Implemented |
| `groups.reorder_connection` | `connections.groups` | Implemented |
<!-- api-daemon-method: groups.create capability=connections.groups -->
<!-- api-daemon-method: groups.delete capability=connections.groups -->
<!-- api-daemon-method: groups.rename capability=connections.groups -->
<!-- api-daemon-method: groups.set_color capability=connections.groups -->
<!-- api-daemon-method: groups.place capability=connections.groups -->
<!-- api-daemon-method: groups.copy_connection capability=connections.groups -->
<!-- api-daemon-method: groups.remove_connection capability=connections.groups -->
<!-- api-daemon-method: groups.reorder_connection capability=connections.groups -->
| `connections.split` | `connections.split` | Implemented |
| `interactions.list` | `interactions.read` | Implemented |
| `interactions.get` | `interactions.read` | Implemented |
| `interactions.claim` | `interactions.respond` | Implemented |
| `interactions.release` | `interactions.respond` | Implemented |
| `interactions.respond` | `interactions.respond` | Implemented; metadata only |
| `interactions.cancel` | `interactions.respond` | Implemented |
| `known_hosts.list` | `known_hosts.read` | Implemented |
| `known_hosts.remove` | `known_hosts.write` | Implemented |
| `keys.list` | `keys.read` | Implemented |
| `keys.get_public` | `keys.read` | Implemented |
| `keys.generate` | `keys.write` | Implemented |
| `keys.verify_passphrase` | `keys.write` | Implemented |
| `sessions.list` | `sessions.read` | Implemented |
| `sessions.get` | `sessions.read` | Implemented |
| `sessions.open` | `sessions.write` | Implemented |
| `sessions.attach` | `sessions.write` | Implemented |
| `sessions.detach` | `sessions.write` | Implemented |
| `sessions.close` | `sessions.write` | Implemented |
| `terminal.replay` | `terminal.replay` | Implemented |
| `terminal.resize` | `terminal.resize` | Implemented |
| `terminal.broadcast_input` | `terminal.input` | Implemented |
| `terminal.claim_input` | `terminal.input` | Implemented |
| `terminal.release_input` | `terminal.input` | Implemented |
| `sftp.list_services` | `sftp.read` | Implemented |
| `sftp.get_service` | `sftp.read` | Implemented |
| `sftp.open` | `sftp.write` | Implemented |
| `sftp.attach` | `sftp.write` | Implemented |
| `sftp.detach` | `sftp.write` | Implemented |
| `sftp.close` | `sftp.write` | Implemented |
| `sftp.list` | `sftp.read` | Implemented |
| `sftp.stat` | `sftp.metadata` | Implemented |
| `sftp.directory_size` | `sftp.read` | Implemented |
| `sftp.lstat` | `sftp.metadata` | Implemented |
| `sftp.realpath` | `sftp.metadata` | Implemented |
| `sftp.readlink` | `sftp.metadata` | Implemented |
| `sftp.mkdir` | `sftp.mutate` | Implemented |
| `sftp.create_file` | `sftp.mutate` | Implemented |
| `sftp.copy` | `sftp.mutate` | Implemented |
| `sftp.rmdir` | `sftp.mutate` | Implemented |
| `sftp.rename` | `sftp.mutate` | Implemented |
| `sftp.remove` | `sftp.mutate` | Implemented |
| `sftp.chmod` | `sftp.mutate` | Implemented |
| `sftp.symlink` | `sftp.mutate` | Implemented |
| `transfers.list` | `transfers.read` | Implemented |
| `transfers.get` | `transfers.read` | Implemented |
| `transfers.start` | `transfers.write` | Implemented |
| `transfers.scp.start` | `transfers.scp` | Implemented |
| `transfers.cancel` | `transfers.write` | Implemented |
| `forwards.list` | `forwards.read` | Implemented |
| `forwards.get` | `forwards.read` | Implemented |
| `forwards.open` | `forwards.write` | Implemented |
| `forwards.close` | `forwards.write` | Implemented |
| `daemon.status` | `daemon.status` | Implemented |
| `daemon.diagnostics` | `daemon.status` | Implemented |
| `daemon.stop` | `daemon.control` | Implemented |
| `daemon.restart` | `daemon.control` | Implemented |
| `secrets.configuration.get` | `secrets.read` | Implemented |
| `secrets.configuration.update` | `secrets.write` | Implemented |
| `secrets.backends.get` | `secrets.read` | Implemented |
| `secrets.state.get` | `secrets.read` | Implemented |
| `secrets.selection.update` | `secrets.write` | Implemented |
| `secrets.unlock` | `secrets.operate` | Implemented |
| `secrets.lock` | `secrets.operate` | Implemented |
| `secrets.bitwarden.status` | `secrets.operate` | Implemented |
| `secrets.bitwarden.configure_server` | `secrets.operate` | Implemented |
| `secrets.bitwarden.login` | `secrets.operate` | Implemented |
| `secrets.bitwarden.api_key_login` | `secrets.operate` | Implemented |
| `secrets.bitwarden.sso_login` | `secrets.operate` | Implemented |
| `secrets.bitwarden.unlock` | `secrets.operate` | Implemented |
| `secrets.bitwarden.sync` | `secrets.operate` | Implemented |
| `secrets.bitwarden.lock` | `secrets.operate` | Implemented |
| `secrets.bitwarden.logout` | `secrets.operate` | Implemented |
| `secrets.rbw.status` | `secrets.operate` | Implemented |
| `secrets.rbw.configure` | `secrets.operate` | Implemented |
| `secrets.rbw.unlock` | `secrets.operate` | Implemented |
| `secrets.rbw.sync` | `secrets.operate` | Implemented |
| `secrets.rbw.lock` | `secrets.operate` | Implemented |
| `secrets.transfer.export` | `secrets.transfer` | Implemented |
| `secrets.transfer.import` | `secrets.transfer` | Implemented |

<!-- api-daemon-method: connections.create capability=connections.write -->
<!-- api-daemon-method: connections.duplicate capability=connections.write -->
<!-- api-daemon-method: connections.delete capability=connections.write -->
<!-- api-daemon-method: connections.delete_password capability=connections.secrets.write -->
<!-- api-daemon-method: connections.delete_passphrase capability=connections.secrets.write -->
<!-- api-daemon-method: connections.delete_plugin_secret capability=connections.secrets.write -->
<!-- api-daemon-method: connections.get capability=connections.read -->
<!-- api-daemon-method: connections.get_editor capability=connections.config.read -->
<!-- api-daemon-method: connections.get_ssh_config_text capability=connections.config.read -->
<!-- api-daemon-method: connections.prepare_external_terminal_launch capability=terminal.external_launch -->
<!-- api-daemon-method: connections.get_launch_command capability=terminal.external_launch -->
<!-- api-daemon-method: connections.get_effective_config capability=connections.config.read -->
<!-- api-daemon-method: connections.check_unsaved_host capability=connections.read -->
<!-- api-daemon-method: daemon.set_operation_mode capability=operation.mode -->
<!-- api-daemon-method: daemon.get_operation_mode capability=operation.mode -->
<!-- api-daemon-method: connections.save_ssh_config_text capability=connections.config.write -->
<!-- api-daemon-method: connections.get_plugin_secret capability=connections.secrets.reveal -->
<!-- api-daemon-method: connections.list capability=connections.read -->
<!-- api-daemon-method: connections.has_password capability=connections.secrets.status.read -->
<!-- api-daemon-method: connections.has_passphrase capability=connections.secrets.status.read -->
<!-- api-daemon-method: connections.reveal_password capability=connections.secrets.reveal -->
<!-- api-daemon-method: connections.reveal_passphrase capability=connections.secrets.reveal -->
<!-- api-daemon-method: connections.store_passphrase capability=connections.secrets.write -->
<!-- api-daemon-method: connections.store_password capability=connections.secrets.write -->
<!-- api-daemon-method: connections.set_session_password capability=connections.secrets.write -->
<!-- api-daemon-method: connections.store_plugin_secret capability=connections.secrets.write -->
<!-- api-daemon-method: connections.update capability=connections.write -->
<!-- api-daemon-method: connections.update_metadata capability=connections.metadata.write -->
<!-- api-daemon-method: connections.assign_to_group capability=connections.groups -->
<!-- api-daemon-method: connections.move capability=connections.groups -->
<!-- api-daemon-method: connections.create_group capability=connections.groups -->
<!-- api-daemon-method: connections.delete_group capability=connections.groups -->
<!-- api-daemon-method: connections.rename_group capability=connections.groups -->
<!-- api-daemon-method: connections.split capability=connections.split -->
<!-- api-daemon-method: daemon.diagnostics capability=daemon.status -->
<!-- api-daemon-method: daemon.restart capability=daemon.control -->
<!-- api-daemon-method: daemon.status capability=daemon.status -->
<!-- api-daemon-method: daemon.stop capability=daemon.control -->
<!-- api-daemon-method: forwards.close capability=forwards.write -->
<!-- api-daemon-method: forwards.claim capability=forwards.write -->
<!-- api-daemon-method: forwards.get capability=forwards.read -->
<!-- api-daemon-method: forwards.list capability=forwards.read -->
<!-- api-daemon-method: forwards.open capability=forwards.write -->
<!-- api-daemon-method: interactions.cancel capability=interactions.respond -->
<!-- api-daemon-method: interactions.claim capability=interactions.respond -->
<!-- api-daemon-method: interactions.get capability=interactions.read -->
<!-- api-daemon-method: interactions.list capability=interactions.read -->
<!-- api-daemon-method: interactions.release capability=interactions.respond -->
<!-- api-daemon-method: interactions.respond capability=interactions.respond -->
<!-- api-daemon-method: known_hosts.list capability=known_hosts.read -->
<!-- api-daemon-method: known_hosts.remove capability=known_hosts.write -->
<!-- api-daemon-method: keys.generate capability=keys.write -->
<!-- api-daemon-method: keys.get_public capability=keys.read -->
<!-- api-daemon-method: keys.list capability=keys.read -->
<!-- api-daemon-method: keys.delete capability=keys.write -->
<!-- api-daemon-method: keys.verify_passphrase capability=keys.write -->
<!-- api-daemon-method: sessions.attach capability=sessions.write -->
<!-- api-daemon-method: sessions.close capability=sessions.write -->
<!-- api-daemon-method: sessions.detach capability=sessions.write -->
<!-- api-daemon-method: sessions.get capability=sessions.read -->
<!-- api-daemon-method: sessions.list capability=sessions.read -->
<!-- api-daemon-method: sessions.open capability=sessions.write -->
<!-- api-daemon-method: sftp.attach capability=sftp.write -->
<!-- api-daemon-method: sftp.chmod capability=sftp.mutate -->
<!-- api-daemon-method: sftp.close capability=sftp.write -->
<!-- api-daemon-method: sftp.detach capability=sftp.write -->
<!-- api-daemon-method: sftp.get_service capability=sftp.read -->
<!-- api-daemon-method: sftp.list capability=sftp.read -->
<!-- api-daemon-method: sftp.list_services capability=sftp.read -->
<!-- api-daemon-method: sftp.lstat capability=sftp.metadata -->
<!-- api-daemon-method: sftp.mkdir capability=sftp.mutate -->
<!-- api-daemon-method: sftp.create_file capability=sftp.mutate -->
<!-- api-daemon-method: sftp.copy capability=sftp.mutate -->
<!-- api-daemon-method: sftp.directory_size capability=sftp.read -->
<!-- api-daemon-method: sftp.open capability=sftp.write -->
<!-- api-daemon-method: sftp.read_file capability=sftp.read -->
<!-- api-daemon-method: sftp.readlink capability=sftp.metadata -->
<!-- api-daemon-method: sftp.realpath capability=sftp.metadata -->
<!-- api-daemon-method: sftp.remove capability=sftp.mutate -->
<!-- api-daemon-method: sftp.rename capability=sftp.mutate -->
<!-- api-daemon-method: sftp.replace_file capability=sftp.mutate -->
<!-- api-daemon-method: sftp.rmdir capability=sftp.mutate -->
<!-- api-daemon-method: sftp.stat capability=sftp.metadata -->
<!-- api-daemon-method: sftp.symlink capability=sftp.mutate -->
<!-- api-daemon-method: terminal.replay capability=terminal.replay -->
<!-- api-daemon-method: terminal.resize capability=terminal.resize -->
<!-- api-daemon-method: terminal.claim_input capability=terminal.input -->
<!-- api-daemon-method: terminal.release_input capability=terminal.input -->
<!-- api-daemon-method: transfers.cancel capability=transfers.write -->
<!-- api-daemon-method: transfers.get capability=transfers.read -->
<!-- api-daemon-method: transfers.list capability=transfers.read -->
<!-- api-daemon-method: transfers.scp.start capability=transfers.scp -->
<!-- api-daemon-method: transfers.start capability=transfers.write -->
<!-- api-daemon-method: ssh_overrides.get capability=ssh_overrides.read -->
<!-- api-daemon-method: ssh_overrides.update capability=ssh_overrides.write -->
<!-- api-daemon-method: ssh_overrides.reset capability=ssh_overrides.write -->
<!-- api-daemon-method: system.get_capabilities capability=none -->
<!-- api-daemon-method: system.handshake capability=none -->
<!-- api-daemon-method: secrets.backends.get capability=secrets.read -->
<!-- api-daemon-method: secrets.bitwarden.api_key_login capability=secrets.operate -->
<!-- api-daemon-method: secrets.bitwarden.configure_server capability=secrets.operate -->
<!-- api-daemon-method: secrets.bitwarden.lock capability=secrets.operate -->
<!-- api-daemon-method: secrets.bitwarden.login capability=secrets.operate -->
<!-- api-daemon-method: secrets.bitwarden.logout capability=secrets.operate -->
<!-- api-daemon-method: secrets.bitwarden.sso_login capability=secrets.operate -->
<!-- api-daemon-method: secrets.bitwarden.status capability=secrets.operate -->
<!-- api-daemon-method: secrets.bitwarden.sync capability=secrets.operate -->
<!-- api-daemon-method: secrets.bitwarden.unlock capability=secrets.operate -->
<!-- api-daemon-method: secrets.configuration.get capability=secrets.read -->
<!-- api-daemon-method: secrets.configuration.update capability=secrets.write -->
<!-- api-daemon-method: secrets.lock capability=secrets.operate -->
<!-- api-daemon-method: secrets.rbw.configure capability=secrets.operate -->
<!-- api-daemon-method: secrets.rbw.lock capability=secrets.operate -->
<!-- api-daemon-method: secrets.rbw.status capability=secrets.operate -->
<!-- api-daemon-method: secrets.rbw.sync capability=secrets.operate -->
<!-- api-daemon-method: secrets.rbw.unlock capability=secrets.operate -->
<!-- api-daemon-method: secrets.selection.update capability=secrets.write -->
<!-- api-daemon-method: secrets.state.get capability=secrets.read -->
<!-- api-daemon-method: secrets.transfer.export capability=secrets.transfer -->
<!-- api-daemon-method: secrets.transfer.import capability=secrets.transfer -->
<!-- api-daemon-method: secrets.transfer.preview capability=secrets.transfer -->
<!-- api-daemon-method: secrets.transfer.list_bitwarden capability=secrets.transfer -->
<!-- api-daemon-method: secrets.transfer.import_bitwarden capability=secrets.transfer -->
<!-- api-daemon-method: secrets.transfer.preview_bitwarden capability=secrets.transfer -->
<!-- api-daemon-method: secrets.transfer.list_ssh capability=secrets.transfer -->
<!-- api-daemon-method: secrets.transfer.import_ssh capability=secrets.transfer -->
<!-- api-daemon-method: secrets.transfer.preview_ssh capability=secrets.transfer -->
<!-- api-daemon-method: secrets.keepassxc.create_database capability=secrets.operate -->
<!-- api-daemon-method: secrets.keepassxc.unlock capability=secrets.operate -->
<!-- api-daemon-method: secrets.keepassxc.lock capability=secrets.operate -->
<!-- api-daemon-method: secrets.remember_master_password capability=secrets.operate -->
<!-- api-daemon-method: secrets.forget_master_password capability=secrets.operate -->
<!-- api-daemon-method: secrets.unlock capability=secrets.operate -->
<!-- api-daemon-method: authorized_keys.list capability=identity.read -->
<!-- api-daemon-method: authorized_keys.remove capability=identity.operate -->
<!-- api-daemon-method: identity.agent.key.add capability=identity.operate -->
<!-- api-daemon-method: identity.agent.key.remove capability=identity.operate -->
<!-- api-daemon-method: identity.agent.keys.get capability=identity.read -->
<!-- api-daemon-method: identity.configuration.update capability=identity.write -->
<!-- api-daemon-method: identity.deploy_key capability=identity.operate -->
<!-- api-daemon-method: identity.providers.get capability=identity.read -->
<!-- api-daemon-method: identity.provider.keys.get capability=identity.read -->
<!-- api-daemon-method: identity.selection.update capability=identity.write -->
<!-- api-daemon-method: identity.state.get capability=identity.read -->
<!-- api-daemon-method: operations.cancel capability=operations.control -->
<!-- api-daemon-method: operations.get capability=operations.read -->

Unknown wire methods return `unsupported_method`. Terminal output and input use
the negotiated binary frame path; resize and replay metadata use the two
explicit wire methods above. Password/passphrase values use the negotiated
one-use `binary-secret-v2` frame and never an ordinary JSON method.

<!-- api-method: get_capabilities -->
## `get_capabilities`

- **Status / introduced:** Implemented / Protocol v1
- **Purpose:** Discover versions, endpoint identity, compatibility, and
  supported feature groups.
- **Parameters / return:** No parameters; returns `Capabilities`.
- **Errors:** Direct core invocation has no transport failure. Daemon
  construction performs a handshake and may return documented
  transport/protocol errors.
- **Events:** None.
- **Cancellation / ordering:** Immediate, not cancellable; returns the same
  immutable value object for the client's lifetime.
- **Threading:** Synchronous and callable from any thread. Daemon requests are
  serialized by one client lock.
- **Side effects / security:** None; contains no secrets. It continues to
  return metadata after `close()`.

```python
capabilities = client.get_capabilities()
if capabilities.supports(Capability.CONNECTIONS_READ):
    connections = client.list_connections()
```

<!-- api-method: list_connections -->
## `list_connections`

- **Status / introduced:** Implemented / Protocol v1
- **Capability / purpose:** `connections.read`; return secret-free summaries.
- **Parameters / return:** No parameters; returns `list[ConnectionSummary]`.
- **Errors:** `invalid_request` after client close; `internal_error` for mapped
  daemon failures; daemon calls may
  also return documented transport/protocol lifecycle errors.
- **Events:** None directly.
- **Cancellation / ordering:** Not cancellable; preserves repository order. The
  GTK bridge cannot cancel a wire request already in progress, but its request
  token suppresses stale or destroyed-widget delivery.
- **Threading:** `DaemonClient` serializes synchronous requests and uses a
  finite timeout. GTK invokes this method through one application-scoped
  worker and posts presentation updates with `GLib.idle_add`.
- **Side effects / security:** Reads the current daemon snapshot. It returns DTOs, not
  persistence or GObject instances, and omits secrets and sensitive paths.

```python
for connection in client.list_connections():
    print(connection.nickname, connection.display_target)
```

<!-- api-method: get_connection -->
## `get_connection`

- **Status / introduced:** Implemented / Protocol v1
- **Capability / purpose:** `connections.read`; retrieve one secret-free detail
  snapshot.
- **Parameters / return:** `connection_id: ConnectionId`; returns
  `ConnectionDetails`.
- **Errors:** `connection_not_found`, `invalid_request`, or `internal_error`.
- **Events:** None directly.
- **Cancellation / ordering:** Not cancellable; one point-in-time result.
- **Threading:** Direct core calls use the owner thread; daemon calls are
  serialized over the persistent socket.
- **Side effects / security:** Reads manager state. The identifier is opaque;
  returned authentication fields are booleans/enums and never secret values.

```python
summary = client.list_connections()[0]
details = client.get_connection(summary.id)
```

<!-- api-method: get_connection_editor -->
## `get_connection_editor`

- **Status / introduced:** Implemented / Protocol v1
- **Capability / purpose:** `connections.config.read`; retrieve full editor state
  including filesystem paths, identity configuration, forwarding rules, and
  all advanced SSH settings.
- **Parameters / return:** `connection_id: ConnectionId`; returns
  `ConnectionEditorDetails`.
- **Errors:** `connection_not_found`, `unsupported_capability`, `invalid_request`,
  or `internal_error`.
- **Events:** None directly.
- **Cancellation / ordering:** Not cancellable; one point-in-time result.
- **Threading:** Direct core calls use the owner thread; daemon calls are
  serialized over the persistent socket.
- **Side effects / security:** Reads manager state. Contains filesystem paths
  and complete configuration; not safe for untrusted consumers.

```python
summary = client.list_connections()[0]
editor = client.get_connection_editor(summary.id)
print(editor.identity_files, editor.forwarding_rules)
```

<!-- api-method: get_ssh_config_text -->
## `get_ssh_config_text`

- **Status / introduced:** Implemented / Protocol v1 additive extension
- **Capability / purpose:** `connections.config.read`; return the daemon-selected
  active SSH config text plus its revision, display name, and writability for
  the raw text editor. The daemon resolves the root file (normal or isolated
  mode) and never accepts a filesystem path from the client.
- **Parameters / return:** No parameters; returns `SshConfigText`.
- **Errors:** `unsupported_capability`, `invalid_request`, `persistence_failed`,
  or `internal_error`. Messages are generic and never embed filesystem paths.
- **Events:** None directly.
- **Cancellation / ordering:** Not cancellable; one point-in-time result.
- **Threading:** Direct core calls use the owner thread; daemon calls are
  serialized over the persistent socket.
- **Side effects / security:** Read-only. The returned text may contain the
  user's complete SSH configuration, so the result is excluded from model
  reprs.

```python
ssh_config = client.get_ssh_config_text()
print(ssh_config.display_name, ssh_config.revision, ssh_config.writable)
```

<!-- api-method: save_ssh_config_text -->
## `save_ssh_config_text`

- **Status / introduced:** Implemented / Protocol v1 additive extension
- **Capability / purpose:** `connections.config.write`; replace the
  daemon-selected active SSH config text through the daemon's hardened atomic
  write (revision check, one-shot backup, permissions, symlink refusal). The
  daemon reloads connection state immediately after a successful save so the
  normal connection update events fire without waiting for the polling watcher.
- **Parameters / return:** `SaveSshConfigTextRequest`; returns `SshConfigText`.
- **Errors:** `stale_editor` when any participating file changed since the
  editor loaded it, `validation_failed`, `persistence_failed`, and daemon
  transport/protocol errors.
- **Events:** Normal `connection.created` / `connection.updated` /
  `connection.deleted` events for the reloaded configuration, plus the
  coherent `connection_store.changed` event.
- **Cancellation / ordering:** Not cancellable; one committed write.
- **Threading:** Direct core calls use the owner thread; daemon calls are
  serialized over the persistent socket.
- **Side effects / security:** The text is written exactly as provided (no
  reformatting) to the daemon-selected file only. A failed reload rolls the
  file back; failed writes leave the previous bytes untouched.

```python
result = client.save_ssh_config_text(
    SaveSshConfigTextRequest(text=edited, expected_revision=ssh_config.revision)
)
print(result.revision)
```

<!-- api-method: create_connection -->
## `create_connection`

- **Status / introduced:** Implemented / Protocol v1
- **Capability / purpose:** `connections.write`; create a saved SSH
  connection from `CreateConnectionRequest`.
- **Parameters / return:** Request model; returns
  `ConnectionDetails`.
- **Errors:** `connection_already_exists`, `validation_failed`,
  `persistence_failed`, and daemon transport/protocol errors. A transport
  failure after send becomes `mutation_ambiguous`.
- **Events:** Exactly one `connection.created` after a successful persistence
  change. Response and event may be observed in either order.
- **Cancellation / ordering / threading:** Direct core calls use the owner
  thread. Daemon requests are serialized and are never automatically retried.
- **Side effects / security:** Persists only the request's basic metadata
  through `ConnectionRepository` and `ConnectionApplicationService`. The request
  has no secret or path fields.

```python
created = client.create_connection(
    CreateConnectionRequest(nickname="example", hostname="example.invalid")
)
```

<!-- api-method: duplicate_connection -->
## `duplicate_connection`

- **Status / introduced:** Implemented / Protocol v1 additive extension
- **Capability / purpose:** `connections.write`; duplicate a saved connection
  through the core persistence owner.
- **Parameters / return:** `connection_id`; returns `ConnectionMutationResult`
  for the new connection.
- **Errors:** `connection_not_found`, `persistence_failed`, and daemon
  transport/protocol errors.
- **Events:** Exactly one `connection.created` on success.
- **Side effects / security:** Copies connection configuration but not runtime
  session ownership; the core assigns a unique nickname and stable ID.

<!-- api-method: update_connection -->
## `update_connection`

- **Status / introduced:** Implemented / Protocol v1
- **Capability / purpose:** `connections.write`; partial basic-metadata update.
- **Parameters / return:** `connection_id` and `UpdateConnectionRequest`;
  returns `ConnectionDetails`.
- **Errors:** `connection_not_found`, `connection_already_exists`,
  `validation_failed`, `persistence_failed`, and transport/protocol errors.
- **Events:** Exactly one `connection.updated` on success; none on failure.
- **Cancellation / ordering / threading:** No automatic retry after timeout or
  closure. Response/event interleaving is intentionally unordered.
- **Side effects / security:** `None` means unchanged. The adapter preserves
  existing advanced settings internally without exposing them on the wire.
  Renaming and host/user/port changes preserve the stable connection ID; the
  result and event carry that same ID.

```python
client.update_connection(
    connection_id,
    UpdateConnectionRequest(username="user"),
)
```

<!-- api-method: update_connection_metadata -->
## `update_connection_metadata`

- **Status / introduced:** Implemented / Protocol v1
- **Capability / purpose:** `connections.metadata.write`; update non-SSH
  metadata (tags, Wake-on-LAN settings) for a saved connection.
- **Parameters / return:** `connection_id` and a metadata dict;
  returns `bool`.
- **Errors:** Transport/protocol errors only.
- **Side effects / security:** Persists through
  `ConnectionRepository.update_connection_metadata`; metadata is merged,
  revisioned with the connection store, and safe metadata keys are validated.

```python
client.update_connection_metadata(
    connection_id,
    {"tags": ["production", "us-east"], "wol_mac": "AA:BB:CC:DD:EE:FF"},
)
```

<!-- api-method: move_connections -->
## `move_connections`

Daemon-only atomic placement for one or more connections. It exclusively moves
sources to a group or root, optionally inserts the selected block above/below a
target, preserves request order, and refreshes the authoritative snapshot once.
An optional snapshot generation rejects stale drag targets without applying the
mutation.

```python
client.move_connections(request)
```

<!-- api-method: assign_connection_to_group -->
## `assign_connection_to_group`

- **Status / introduced:** Implemented / Protocol v1
- **Capability / purpose:** `connections.groups`; move a connection to a
  group (or root if group_id is empty).
- **Parameters / return:** `connection_id` and `group_id: str`;
  returns `bool`.
- **Errors:** Transport/protocol errors only.
- **Side effects / security:** Persists through the repository-owned group
  mutation service; no frontend manager instance is authoritative.

```python
client.assign_connection_to_group(connection_id, "group-production")
```

<!-- api-method: create_group -->
## `create_group`

- **Status / introduced:** Implemented / Protocol v1
- **Capability / purpose:** `connections.groups`; create a new group.
- **Parameters / return:** `name`, optional `parent_id` and `color`;
  returns the new group ID as `Optional[str]`.
- **Errors:** Transport/protocol errors only.
- **Side effects / security:** Persists through the repository-owned group
  mutation service; no frontend manager instance is authoritative.

```python
group_id = client.create_group("Production Servers", color="#4CAF50")
```

<!-- api-method: delete_group -->
## `delete_group`

- **Status / introduced:** Implemented / Protocol v1
- **Capability / purpose:** `connections.groups`; delete a group.
- **Parameters / return:** `group_id: str`; returns `bool`.
- **Errors:** Transport/protocol errors only.
- **Side effects / security:** Persists through the repository-owned group
  mutation service; no frontend manager instance is authoritative.

```python
client.delete_group("group-production")
```

<!-- api-method: rename_group -->
## `rename_group`

- **Status / introduced:** Implemented / Protocol v1
- **Capability / purpose:** `connections.groups`; rename a group.
- **Parameters / return:** `group_id` and `new_name: str`;
  returns `bool`.
- **Errors:** Transport/protocol errors only.
- **Side effects / security:** Persists through the repository-owned group
  mutation service; no frontend manager instance is authoritative.

```python
client.rename_group("group-production", "Staging Servers")
```

<!-- api-method: split_connection -->
## `split_connection`

- **Status / introduced:** Implemented / Protocol v1
- **Capability / purpose:** `connections.split`; split a connection out
  of a multi-host SSH config block.
- **Parameters / return:** `SplitConnectionRequest` with
  `connection_id`, `original_host_token`, `source_config_path`,
  `config_patch`, and optional `expected_generation`;
  returns `bool`.
- **Errors:** Transport/protocol errors only.
- **Side effects / security:** Removes the host token from the
  original multi-host block and appends a new standalone `Host` block
  to the config file.

```python
client.split_connection(SplitConnectionRequest(
    connection_id="abc123",
    original_host_token="server1",
    source_config_path="/home/user/.ssh/config",
    config_patch={"hostname": "10.0.0.1", "port": 22},
))
```

<!-- api-method: delete_connection -->
## `delete_connection`

- **Status / introduced:** Implemented / Protocol v1
- **Capability / purpose:** `connections.write`; saved-connection
  deletion.
- **Parameters / return:** `DeleteConnectionRequest`; returns
  `DeleteConnectionResult`.
- **Errors:** `connection_not_found`, `persistence_failed`, and
  transport/protocol errors.
- **Events:** Exactly one `connection.deleted` on success; none on failure.
- **Cancellation / ordering / threading:** No automatic retry. An ambiguous
  transport failure requires a fresh snapshot before explicit user action.
- **Side effects / security:** Delegates to the existing manager deletion
  policy; no secret value crosses the request or result.

```python
client.delete_connection(DeleteConnectionRequest(connection_id))
```

<!-- api-method: store_connection_password -->
## `store_connection_password`

- **Status / introduced:** Implemented / Protocol v1
- **Capability / purpose:** `connections.secrets.write`; store or update
  a login password for a saved connection.
- **Parameters / return:** `StoreConnectionPasswordRequest` (connection_id,
  password); returns `bool`.
- **Errors:** Transport/protocol errors only.
- **Side effects / security:** Delegates to the daemon's
  `DaemonConnectionSecretProvider`; passwords never appear in ordinary DTOs or
  cross the wire as plaintext.

```python
client.store_connection_password(
    StoreConnectionPasswordRequest(connection_id, password)
)
```

<!-- api-method: set_session_connection_password -->
## `set_session_connection_password`

- **Status / introduced:** Implemented / Protocol v1
- **Capability / purpose:** `connections.secrets.write`; retain a password for
  the current daemon session without persisting it.
- **Parameters / return:** `SetSessionConnectionPasswordRequest`
  (connection_id) plus a protected mutable password input; returns `bool`.
- **Errors:** Transport/protocol errors and connection validation errors.
- **Side effects / security:** The password is delivered only through the
  protected command-input frame and is held in daemon memory with a bounded
  session lifetime. It is never written to the repository, returned in a DTO,
  placed in argv/environment, or logged. Persistent storage remains a separate
  explicit `store_connection_password` operation.

```python
client.set_session_connection_password(
    SetSessionConnectionPasswordRequest(connection_id),
    bytearray(password.encode()),
)
```

<!-- api-method: has_connection_password -->
## `has_connection_password`

Daemon-only metadata query under `connections.secrets.status.read`. Returns a
boolean indicating whether a saved login password is available; no secret
value crosses the response envelope.

```python
saved = client.has_connection_password(connection_id)
```

<!-- api-method: reveal_connection_password -->
## `reveal_connection_password`

Daemon-only explicit reveal under `connections.secrets.reveal`. The JSON result
is only an acknowledgment; the saved password is delivered in a one-use binary
secret frame and returned to the caller as a mutable `bytearray`.

```python
password = client.reveal_connection_password(connection_id)
```

<!-- api-method: delete_connection_password -->
## `delete_connection_password`

- **Status / introduced:** Implemented / Protocol v1
- **Capability / purpose:** `connections.secrets.write`; delete all stored
  login passwords for a saved connection.
- **Parameters / return:** `DeleteConnectionPasswordRequest` (connection_id);
  returns `bool`.
- **Errors:** Transport/protocol errors only.
- **Side effects / security:** Delegates to the daemon's
  `DaemonConnectionSecretProvider`; deletion is idempotent and secret values
  are not returned.

```python
client.delete_connection_password(
    DeleteConnectionPasswordRequest(connection_id)
)
```

<!-- api-method: store_key_passphrase -->
## `store_key_passphrase`

- **Status / introduced:** Implemented / Protocol v1
- **Capability / purpose:** `connections.secrets.write`; store or update a
  key passphrase.
- **Parameters / return:** `StoreKeyPassphraseRequest` (key_path,
  interaction_scope_id); returns `bool`.
- **Errors:** Transport/protocol errors only.
- **Side effects / security:** The request carries only metadata. The frontend
  supplies the value through the protected interaction secret-frame channel;
  the daemon then delegates to its secret provider.

```python
client.store_key_passphrase(
    StoreKeyPassphraseRequest(
        key_path="/home/user/.ssh/id_rsa",
        interaction_scope_id="key-operation-store-1",
    )
)
```

<!-- api-method: delete_key_passphrase -->
## `delete_key_passphrase`

- **Status / introduced:** Implemented / Protocol v1
- **Capability / purpose:** `connections.secrets.write`; delete a stored key passphrase.
- **Parameters / return:** `DeleteKeyPassphraseRequest` (key_path); returns `bool`.
- **Errors:** Transport/protocol errors only.
- **Side effects / security:** Delegates to the daemon-owned secret provider;
  deletion is idempotent and no passphrase is returned.

```python
client.delete_key_passphrase(
    DeleteKeyPassphraseRequest(key_path="/home/user/.ssh/id_rsa")
)
```

<!-- api-method: has_key_passphrase -->
## `has_key_passphrase`

Daemon-only metadata query under `connections.secrets.status.read`. Returns a
boolean indicating whether a saved key passphrase is available; no secret value
crosses the response envelope.

```python
saved = client.has_key_passphrase("/home/user/.ssh/id_rsa")
```

<!-- api-method: reveal_key_passphrase -->
## `reveal_key_passphrase`

Daemon-only explicit reveal under `connections.secrets.reveal`. The JSON result
is only an acknowledgment; the saved passphrase is delivered in a one-use
binary secret frame and returned as a mutable `bytearray`.

```python
passphrase = client.reveal_key_passphrase("/home/user/.ssh/id_rsa")
```

<!-- api-method: get_plugin_secret -->
## `get_plugin_secret`

Daemon-only explicit plugin-secret retrieval under `connections.secrets.reveal`.
The JSON response is only an acknowledgment; the value is delivered through a
one-use binary secret frame and returned as a string to the plugin surface.

```python
secret = client.get_plugin_secret("example.plugin", "token")
```

<!-- api-method: list_interactions -->
## `list_interactions`

Daemon-only `interactions.read` snapshot of safe interaction metadata visible
to the handshaken client. Secret values and raw OpenSSH prompts are absent.

<!-- api-method: get_interaction -->
## `get_interaction`

Daemon-only lookup by opaque `interaction-<n>` identifier, scoped to the
requesting client's eligible sessions.

<!-- api-method: claim_interaction -->
## `claim_interaction`

Claims responder ownership and returns a short-lived claim plus one-use nonce.
Claim conflicts are retryable; disconnect releases an unanswered claim.

<!-- api-method: release_interaction -->
## `release_interaction`

Idempotently releases an unanswered claim. A reserved secret response cannot be
released until it is answered or cancelled.

<!-- api-method: respond_to_interaction -->
## `respond_to_interaction`

Submits a typed host-key or secret action. Secret bytes are deliberately absent
and follow separately through `send_interaction_secret`.

<!-- api-method: cancel_interaction -->
## `cancel_interaction`

Cancels one owned pending interaction. Expiry, session closure, process exit,
and daemon shutdown also cancel pending interactions.

<!-- api-method: send_interaction_secret -->
## `send_interaction_secret`

Sends a bounded mutable byte buffer through `binary-secret-v2` after a typed
submit action reserved the slot. The client clears the supplied buffer after
the send attempt; the operation is never retried.

<!-- api-method: list_sftp_services -->
## `list_sftp_services`

Daemon-only `sftp.read` snapshot of open SFTP services visible to the
handshaken client.

<!-- api-method: get_sftp_service -->
## `get_sftp_service`

Daemon-only lookup by opaque `sftp-<n>` identifier.

<!-- api-method: open_sftp -->
## `open_sftp`

Daemon-only `sftp.write` creation of a daemon-owned SFTP service for one
connection. Returns `SftpServiceSummary`.

<!-- api-method: attach_sftp -->
## `attach_sftp`

Attaches the handshaken client to an existing SFTP service for shared use.

<!-- api-method: detach_sftp -->
## `detach_sftp`

Removes the caller's attachment from an SFTP service without closing it for
other clients.

<!-- api-method: close_sftp -->
## `close_sftp`

Requests bounded closure of an SFTP service. Repeated close is idempotent.

<!-- api-method: sftp_list_directory -->
## `sftp_list_directory`

Lists a remote directory through a ready SFTP service. Returns
`ListDirectoryResult` with typed `RemoteFileEntry` rows.

<!-- api-method: sftp_stat -->
## `sftp_stat`

Follows symlinks and returns a `RemoteFileEntry` for one remote path.

<!-- api-method: sftp_directory_size -->
## `sftp_directory_size`

Daemon-only `sftp.read`. Recursively summarises a remote directory tree within
one ready SFTP service, returning `SftpDirectorySizeResult` with total bytes
plus file/directory counts. Symlinked entries are never descended into.

<!-- api-method: sftp_lstat -->
## `sftp_lstat`

Returns metadata for one remote path without following the final symlink.

<!-- api-method: sftp_realpath -->
## `sftp_realpath`

Resolves a remote path to its absolute form and returns the path string.

<!-- api-method: sftp_readlink -->
## `sftp_readlink`

Returns the symlink target string for one remote path.

<!-- api-method: sftp_read_file -->
## `sftp_read_file`

Daemon-only `sftp.read`. Reads a bounded remote file through a ready SFTP
service, or the daemon-local `~/.ssh/authorized_keys` target. Returns
`SftpReadFileResult` with a daemon-computed content revision.

<!-- api-method: sftp_replace_file -->
## `sftp_replace_file`

Daemon-only `sftp.mutate`. Replaces a bounded remote file (or the daemon-local
`~/.ssh/authorized_keys` target) with atomic, revision-checked content,
returning `SftpReplaceFileResult` with a new revision and optional backup path.

<!-- api-method: sftp_mkdir -->
## `sftp_mkdir`

Creates a remote directory.

<!-- api-method: sftp_rmdir -->
## `sftp_rmdir`

Removes an empty remote directory.

<!-- api-method: sftp_remove -->
## `sftp_remove`

Removes a remote file or symlink; with `recursive=true` removes a directory
tree. Recursive deletion is daemon-owned and lstat-based so symlinks are
removed as links and never followed; a missing path is idempotent.

<!-- api-method: sftp_rename -->
## `sftp_rename`

Renames or moves a remote path, optionally overwriting when requested.

<!-- api-method: sftp_chmod -->
## `sftp_chmod`

Changes the remote mode bits for one path.

<!-- api-method: sftp_symlink -->
## `sftp_symlink`

Creates a remote symlink from `link_path` to `target_path`.

<!-- api-method: list_transfers -->
## `list_transfers`

Daemon-only `transfers.read` snapshot of transfer records.

<!-- api-method: get_transfer -->
## `get_transfer`

Daemon-only lookup by opaque `transfer-<n>` identifier.

<!-- api-method: start_scp_transfer -->
## `start_scp_transfer`

- **Status / introduced:** Daemon only / Protocol v1 additive extension
- **Capability / purpose:** `transfers.scp`; start one daemon-owned native OpenSSH SCP upload or download.
- **Parameters / return:** `StartScpTransferRequest`; returns the shared
  `TransferSummary` lifecycle DTO, with `ScpFailure` on terminal failure.
- **Errors:** `unsupported_capability`, `invalid_request`, `server_busy`, typed transfer failure, or transport errors.
- **Behavior:** native SCP is overwrite-only; `fail`, `skip`, and `rename` conflict policies are rejected. GTK observes daemon transfer state through terminal completion.
- **Security:** sources and destination are bounded typed paths; argv, environment, passwords, passphrases, askpass data, and process handles never cross the API.

```python
summary = client.start_scp_transfer(request)
```

<!-- api-method: start_transfer -->
## `start_transfer`

Starts a daemon-path upload or download against a ready SFTP service. Direction
also requires `transfers.upload` or `transfers.download`.

<!-- api-method: cancel_transfer -->
## `cancel_transfer`

Requests cancellation of one transfer. Terminal states remain observable via
events and `get_transfer`.

<!-- api-method: list_forwards -->
## `list_forwards`

Daemon-only `forwards.read` snapshot of runtime forwards.

<!-- api-method: get_forward -->
## `get_forward`

Daemon-only lookup by opaque `forward-<n>` identifier.

<!-- api-method: open_forward -->
## `open_forward`

Opens a local, remote, or dynamic forward. Type also requires
`forwards.local`, `forwards.remote`, or `forwards.dynamic`.

<!-- api-method: claim_forward -->
## `claim_forward`

Daemon-only request to become the owning client of an existing forward.

<!-- api-method: close_forward -->
## `close_forward`

Requests bounded closure of one runtime forward.

<!-- api-method: list_known_hosts -->
## `list_known_hosts`

- **Status / introduced:** Daemon only / Protocol v1, API 0.14.
- **Capability / purpose:** `known_hosts.read`; return a revisioned snapshot
  of the daemon-owned known-hosts file without exposing the path.

<!-- api-method: remove_known_host_entries -->
## `remove_known_host_entries`

- **Status / introduced:** Daemon only / Protocol v1, API 0.14.
- **Capability / purpose:** `known_hosts.write`; batch-remove entries by ID
  with an optimistic revision check against the daemon-owned file.

<!-- api-method: list_keys -->
## `list_keys`

- **Status / introduced:** Daemon only / Protocol v1, API 0.15.
- **Capability / purpose:** `keys.read`; list key metadata from the
  daemon-owned selected key store scope. Daemon RPC `keys.list`.

<!-- api-method: delete_key -->
## `delete_key`

- **Status / introduced:** Daemon only / Protocol v1, API 0.26.
- **Capability / purpose:** `keys.write`; delete a daemon-known key pair by
  opaque `KeyId` and semantic `KeyStoreScope`. The request accepts no caller-
  supplied filesystem path. Daemon RPC `keys.delete`.
- **Result:** `DeleteKeyResult` confirms the deleted opaque key ID.
- **Errors:** `key_not_found`, `key_deletion_failed`, or transport/protocol
  errors.

<!-- api-method: read_public_key -->
## `read_public_key`

- **Status / introduced:** Daemon only / Protocol v1, API 0.15.
- **Capability / purpose:** `keys.read`; return public-key text for an opaque
  key ID from the daemon-owned key store. Daemon RPC `keys.get_public`.

<!-- api-method: generate_key -->
## `generate_key`

- **Status / introduced:** Daemon only / Protocol v1, API 0.15; protected
  passphrase contract revised in API 0.25.
- **Capability / purpose:** `keys.write`; generate a keypair in the
  daemon-owned selected key store scope. An encrypted request contains only an
  opaque interaction scope; the passphrase uses a protected secret frame and
  native askpass. Daemon RPC `keys.generate`.

<!-- api-method: verify_key_passphrase -->
## `verify_key_passphrase`

- **Status / introduced:** Daemon only / Protocol v1, API 0.25.
- **Capability / purpose:** `keys.write`; verify a selected private key through
  daemon-owned `ssh-keygen` prompting. The ordinary request contains the key
  path and opaque interaction scope only; the passphrase uses the protected
  secret-frame channel. Daemon RPC `keys.verify_passphrase`.

<!-- api-method: get_daemon_status -->
## `get_daemon_status`

- **Status / introduced:** Daemon-only / Protocol v1, API 0.11.
- **Capability / purpose:** `daemon.status`; return lifecycle state, versions,
  resource counts, and idle/shutdown diagnostics without secrets.
- **Errors:** `unsupported_capability` or daemon transport errors.

<!-- api-method: get_daemon_diagnostics -->
## `get_daemon_diagnostics`

- **Status / introduced:** Daemon-only / Protocol v1, API 0.11.
- **Capability / purpose:** `daemon.status`; broader safe diagnostics snapshot
  (threads, FDs, RSS where available). No secrets, paths, or terminal data.

<!-- api-method: stop_daemon -->
## `stop_daemon`

- **Status / introduced:** Daemon-only / Protocol v1, API 0.11.
- **Capability / purpose:** `daemon.control`; drain and stop. Rejects when live
  resources exist unless confirmation or force is supplied.

<!-- api-method: restart_daemon -->
## `restart_daemon`

- **Status / introduced:** Daemon-only / Protocol v1, API 0.11.
- **Capability / purpose:** `daemon.control`; drain, stop, and request a new
  instance. Live processes do not survive restart.

<!-- api-method: list_sessions -->
## `list_sessions`

- **Status / introduced:** Daemon-only / Protocol v1, API 0.6.
- **Capability / purpose:** `sessions.read`; return one creation-ordered,
  secret-free snapshot including retained closed records.
- **Parameters / return:** None; returns `list[SessionSummary]`.
- **Errors:** `unsupported_capability` or daemon
  transport/protocol lifecycle errors.
- **Threading:** Synchronous; GTK diagnostics submit it through
  `GtkClientBridge`.

<!-- api-method: get_session -->
## `get_session`

- **Status / introduced:** Daemon-only / Protocol v1, API 0.6.
- **Capability / purpose:** `sessions.read`; inspect one daemon-lifetime
  session by strict opaque `SessionId`.
- **Errors:** `session_not_found`, `invalid_request`, and transport errors.
- **Security:** No process handle, command, environment, PTY path, or secret is
  exposed.

<!-- api-method: open_session -->
## `open_session`

- **Status / introduced:** Daemon-only / Protocol v1, API 0.6.
- **Capability / purpose:** `sessions.write` (plus `sessions.command` when a
  `remote_command` is supplied); allocate a daemon-owned session
  record and initiate the configured process runner.
- **Parameters / return:** `OpenSessionRequest(connection_id, remote_command?, force_tty?)`;
  returns the immutable `starting` `SessionSummary` as soon as the startup
  command is accepted by the bounded executor. The response does not wait for
  PTY allocation, OpenSSH launch, host-key/password/passphrase interaction,
  network negotiation, or `running`. When `remote_command` is set the daemon
  runs `<ssh> <alias> <remote_command>` inside the connection instead of a
  plain interactive shell (for example `docker exec -it <container> sh` or
  `docker logs -f <container>`). When `force_tty` is set the daemon adds `-t`
  so OpenSSH allocates a remote PTY, which interactive commands such as
  `docker exec -it` require.
- **Errors:** Missing connection, unsupported protocol, daemon shutdown, or
  transport errors. `server_busy` means the bounded worker admission failed;
  the prepared record is marked `failed` and no misleading `starting` summary
  is returned. A lost response after send becomes non-retryable
  `mutation_ambiguous`; refresh `sessions.list` before user-directed retry.
  Authentication and process-startup deadlines belong to the interaction
  broker and session lifecycle, not the five-second control RPC timeout.
- **Events:** `session.created`, then state changes. Clients may observe
  `starting`→`running` or `starting`→`failed` after the open response.
  Startup failure after acknowledgement is delivered only through session
  state/events, never as a second RPC response for the same open.
- **Threading:** Preparation and admission are bounded on the selector. Runner
  startup executes on the daemon's bounded keyed executor independently of the
  RPC response. A slow or interactive startup does not delay another peer's
  handshake or read requests.
- **Reconciliation follow-up:** A client-generated `client_open_token` for
  idempotent open retry after genuine transport loss is deferred; immediate
  acknowledgement removes the common authentication-timeout ambiguity.
- **Security:** The frontend supplies no argv or environment. Phase 6's
  production runner fails safely until prompt-safe PTY startup exists; it does
  not fake `running`.

```python
session = client.open_session(OpenSessionRequest(connection_id))
assert session.state is SessionState.STARTING
```

<!-- api-method: attach_session -->
## `attach_session`

- **Status / introduced:** Daemon-only / Protocol v1, API 0.6.
- **Capability / purpose:** `sessions.write`; add the handshaken client to the
  session's logical attachment set.
- **Parameters / return:** `AttachSessionRequest(session_id)` returns
  `AttachSessionResult`.
- **Semantics:** Idempotent for one client/session pair. The daemon derives
  client identity; callers cannot attach for another client. `input_owner` is
  always false and no terminal bytes flow in this phase.

<!-- api-method: detach_session -->
## `detach_session`

- **Status / introduced:** Daemon-only / Protocol v1, API 0.6.
- **Capability / purpose:** `sessions.write`; remove the caller's attachment.
- **Semantics:** Repeated detach is safe. A mismatched attachment ID returns
  `permission_denied`. Socket closure detaches that peer automatically and
  never closes the session.

<!-- api-method: close_session -->
## `close_session`

- **Status / introduced:** Daemon-only / Protocol v1, API 0.6.
- **Capability / purpose:** `sessions.write`; request bounded termination of
  the exact owned runtime resource.
- **Semantics:** Repeated close is idempotent. The runtime enters `closing`,
  submits termination on the same session's serial worker lane, escalates only
  for its exact handle, records exit, and emits `session.exited` and
  `session.closed`. The response is completed after that bounded worker step,
  not on acceptance. If both bounded attempts fail, the daemon retains the
  exact handle in `failed`; a later explicit close retries it rather than
  forgetting an owned resource.
- **Errors:** `session_not_found`, `session_termination_failed`, shutdown and
  transport errors. `server_busy` is an immediate retryable admission failure.
  Lost responses are `mutation_ambiguous`; there is no automatic retry.
- **Threading:** Terminate, wait, and kill never run on the selector. A close
  queued behind startup for the same session cannot overtake it; unrelated
  session lanes can progress concurrently.

<!-- api-method: send_terminal_input -->
## `send_terminal_input`

- **Status / introduced:** Daemon-only / Protocol v1, API 0.8.
- **Capability / purpose:** `terminal.input`; enqueue byte input from the
  session's single input-owning attachment.
- **Parameters / return:** `TerminalInput`; returns after the bounded binary
  frame is sent.
- **Errors:** Local capability/lifecycle validation is synchronous. Daemon-side
  attachment, ownership, running-state, and input-backpressure rejection is
  delivered asynchronously to the terminal subscription's safe error callback.
- **Ordering / threading:** Writes are serialized by the client send lock and
  by the daemon's per-session PTY input queue. Partial non-blocking PTY writes
  preserve order.
- **Side effects / security:** Bytes are never decoded or logged.

```python
client.send_terminal_input(TerminalInput(session_id, attachment_id, b"ls\r"))
```

<!-- api-method: broadcast_terminal_input -->
## `broadcast_terminal_input`

- **Status / introduced:** Daemon-only / Protocol v1, API 0.23.
- **Capability / purpose:** `terminal.input`; write one command to the
  existing interactive sessions identified by `session_ids`.
- **Parameters / return:** `BroadcastTerminalInputRequest`; returns `None`
  after all eligible PTY writes are accepted.
- **Errors:** `session_not_found`, `terminal_input_owner_required`,
  `terminal_attachment_required`, session-state, PTY availability, input
  backpressure, and transport errors. Validation happens in the daemon for
  the calling client; there is no one-shot SSH launch or output polling.
- **Ordering / threading:** The daemon validates every target against its
  current input owner, then writes `command.encode("utf-8") + b"\n"` to each
  existing session PTY in request order.
- **Side effects / security:** This mutates the existing shell session, so
  stateful commands such as `cd` and `export` persist there. The command is
  not logged or returned as command output.

```python
client.broadcast_terminal_input(
    BroadcastTerminalInputRequest(
        session_ids=(session_id_a, session_id_b),
        command="uptime",
    )
)
```

<!-- api-method: claim_terminal_input -->
## `claim_terminal_input`

- **Status / introduced:** Daemon-only / Protocol v1, API 0.9.
- **Capability / purpose:** `terminal.input`; claim input ownership when the
  session currently has no input owner. Forced takeover is rejected.
- **Parameters / return:** `ClaimTerminalInputRequest`; returns `None`.
- **Errors:** `terminal_attachment_required`, `terminal_input_owner_exists`,
  session-state and transport errors.
- **Ordering / threading:** Ownership changes are serialized with attach,
  detach, input, and resize on the session lane.
- **Side effects / security:** Emits an updated session summary. Does not move
  bytes.

```python
client.claim_terminal_input(
    ClaimTerminalInputRequest(session_id=session_id, attachment_id=attachment_id)
)
```

<!-- api-method: release_terminal_input -->
## `release_terminal_input`

- **Status / introduced:** Daemon-only / Protocol v1, API 0.9.
- **Capability / purpose:** `terminal.input`; release input ownership while
  remaining attached as a view-only subscriber.
- **Parameters / return:** `ReleaseTerminalInputRequest`; returns `None`.
- **Errors:** `terminal_attachment_required`, `terminal_input_owner_required`,
  session-state and transport errors.
- **Ordering / threading:** Same session-lane serialization as claim/input.
- **Side effects / security:** Emits an updated session summary. Does not flush
  or echo pending input.

```python
client.release_terminal_input(
    ReleaseTerminalInputRequest(session_id=session_id, attachment_id=attachment_id)
)
```

<!-- api-method: resize_terminal -->
## `resize_terminal`

- **Status / introduced:** Daemon-only / Protocol v1, API 0.8.
- **Capability / purpose:** `terminal.resize`; apply terminal rows and columns
  for the input-owning attachment.
- **Parameters / return:** `ResizeTerminalRequest`; returns `None`.
- **Errors:** Invalid dimensions, missing attachment, input ownership,
  unavailable PTY, and session-state errors.
- **Ordering / threading:** A pre-start resize becomes the latest pending
  initial size. Running sessions use `TIOCSWINSZ`; repeated size is a no-op.
- **Side effects / security:** Dimensions are limited to 1–1000.

```python
client.resize_terminal(
    ResizeTerminalRequest(session_id, attachment_id, TerminalDimensions(24, 80))
)
```

<!-- api-method: replay_terminal -->
## `replay_terminal`

- **Status / introduced:** Daemon-only / Protocol v1, API 0.8.
- **Capability / purpose:** `terminal.replay`; request retained output from an
  absolute per-session byte offset.
- **Parameters / return:** `ReplayRequest` identifies the owned attachment,
  offset, and bounded maximum. `ReplayResult` carries metadata only; raw bytes
  arrive through replay-flagged terminal frames.
- **Errors:** Attachment, replay availability, sequence range, capability, and
  transport errors.
- **Ordering / threading:** The replay snapshot is immutable. Live/replay
  overlap is permitted and deduplicated by byte sequence; silent gaps are not.
- **Side effects / security:** Replay data is raw, bounded, and never logged.

```python
client.replay_terminal(
    ReplayRequest(session_id, attachment_id, after_sequence=42)
)
```

<!-- api-method: subscribe_terminal -->
## `subscribe_terminal`

- **Status / introduced:** Daemon-only / Protocol v1, API 0.8.
- **Capability / purpose:** `terminal.output`; register frontend-neutral raw
  output, continuity-loss, and EOF callbacks for one session.
- **Parameters / return:** Session ID and callbacks; returns an idempotent
  `TerminalSubscription`.
- **Ordering / threading:** One bounded client dispatch thread isolates the
  socket reader from slow subscribers. Output is serialized per session and
  carries absolute byte offsets.
- **Side effects / security:** Callbacks receive immutable byte DTOs; GTK must
  marshal them through its bridge.

<!-- api-method: respond_to_interaction -->
## `respond_to_interaction`

- **Status / introduced:** Unsupported / Protocol v1 schema
- **Capability / purpose:** `interactions`; intended answer/cancel/reject of a
  core-requested interaction.
- **Parameters / return:** `InteractionResponse`; intended return is `None`.
- **Errors:** Always `unsupported_capability` for `interactions`.
- **Events:** None now.
- **Cancellation / ordering / threading:** Immediate unsupported failure.
- **Side effects / security:** No secret is consumed. `value` is excluded from
  `repr`; callers must also avoid logs and event histories.

```python
response = InteractionResponse(
    interaction_id=interaction_id,
    status=InteractionStatus.CANCELLED,
)
client.respond_to_interaction(response)
```

<!-- api-method: subscribe_events -->
## `subscribe_events`

- **Status / introduced:** Implemented by `DaemonClient` / Protocol v1
- **Capability / purpose:** `connections.events` for the implemented
  connection lifecycle stream; subscribe to events that the provider can emit.
- **Parameters / return:** Callable accepting one `CoreEvent`; returns
  `Subscription`.
- **Errors:** `invalid_request` after client close; a non-callable callback
  raises Python `TypeError`.
- **Events:** Both providers emit `connection.created`, `connection.updated`,
  and `connection.deleted`. `DaemonClient` can additionally emit a safe local
  `error.occurred` when transport/event continuity is lost.
- **Cancellation / ordering:** `unsubscribe()`/`close()` is the cancellation
  mechanism and is idempotent. Callbacks run in registration order through a
  publisher-global serial FIFO.
- **Threading:** Registration is thread-safe. Direct core publication uses the
  active serial publisher thread. `DaemonClient` callbacks use one dedicated
  serial event dispatcher, never the socket reader, so a slow subscriber cannot
  block response processing. Re-entrant events queue without recursion.
- **Side effects / security:** Retains the callback until unsubscribe or client
  close. Subscriber exceptions are logged and isolated from other subscribers.

```python
subscription = client.subscribe_events(handle_event)
try:
    run_frontend()
finally:
    subscription.close()
```

<!-- api-method: get_identity_providers -->
## `get_identity_providers`

- **Status / review:** Implemented in the daemon identity service; pending the
  separate identity phase review.
- **Capability / purpose:** `identity.read`; return provider descriptors and
  safe availability metadata only.
- **Parameters / return:** None; returns `IdentityProviderRegistry`.
- **Security:** Provider credentials and private key material are omitted.

<!-- api-method: get_identity_state -->
## `get_identity_state`

- **Status / review:** Implemented in the daemon identity service; pending the
  separate identity phase review.
- **Capability / purpose:** `identity.read`; return current identity selection
  and safe state metadata.
- **Parameters / return:** None; returns `IdentityState`.

<!-- api-method: update_identity_selection -->
## `update_identity_selection`

- **Status / review:** Implemented in the daemon identity service; pending the
  separate identity phase review.
- **Capability / purpose:** `identity.write`; update the selected identity
  provider/configuration reference.
- **Parameters / return:** `UpdateIdentitySelectionRequest`; returns
  `IdentityState`.

<!-- api-method: update_identity_configuration -->
## `update_identity_configuration`

- **Status / review:** Implemented in the daemon identity service; pending the
  separate identity phase review.
- **Capability / purpose:** `identity.write`; update typed provider
  configuration without exposing provider secrets in ordinary DTOs.
- **Parameters / return:** `UpdateIdentityConfigurationRequest`; returns
  `IdentityState`.

<!-- api-method: list_agent_keys -->
## `list_agent_keys`

- **Status / review:** Implemented in the daemon identity service; pending the
  separate identity phase review.
- **Capability / purpose:** `identity.read`; list safe SSH-agent key metadata.
- **Parameters / return:** None; returns `AgentKeyList`.

<!-- api-method: list_provider_agent_keys -->
## `list_provider_agent_keys`

- **Status / review:** Implemented in the daemon identity service.
- **Capability / purpose:** `identity.read`; list the keys loaded in one named
  provider's agent (native `ssh-add -l`), scoped to a registry provider id
  (`'auto'` = the system ssh-agent) regardless of the current selection.
- **Parameters / return:** `ListProviderAgentKeysRequest`; returns
  `AgentKeyList`.

<!-- api-method: add_agent_key -->
## `add_agent_key`

- **Status / review:** Implemented in the daemon identity service; pending the
  separate identity phase review.
- **Capability / purpose:** `identity.operate`; load a key through native
  `ssh-add` supervision.
- **Parameters / return:** `AgentKeyMutationRequest`; returns `AgentKeyList`.

<!-- api-method: remove_agent_key -->
## `remove_agent_key`

- **Status / review:** Implemented in the daemon identity service; pending the
  separate identity phase review.
- **Capability / purpose:** `identity.operate`; remove an agent key through
  native agent control.
- **Parameters / return:** `AgentKeyMutationRequest`; returns `AgentKeyList`.

<!-- api-method: deploy_key -->
## `deploy_key`

- **Status / review:** Implemented in the daemon identity service; pending the
  separate identity phase review.
- **Capability / purpose:** `identity.operate`; supervise native `ssh-copy-id`
  deployment and return an operation summary.
- **Parameters / return:** `DeployKeyRequest`; returns `OperationSummary`, with
  `IdentityFailure` on terminal failure.

<!-- api-method: list_authorized_keys -->
## `list_authorized_keys`

- **Status / review:** Implemented in the daemon identity service; pending the
  separate identity phase review.
- **Capability / purpose:** `identity.read`; list safe authorized-key metadata.
- **Parameters / return:** `ListAuthorizedKeysRequest`; returns
  `AuthorizedKeyList`.

<!-- api-method: remove_authorized_key -->
## `remove_authorized_key`

- **Status / review:** Implemented in the daemon identity service; pending the
  separate identity phase review.
- **Capability / purpose:** `identity.operate`; remove an authorized key through
  the daemon operation service.
- **Parameters / return:** `RemoveAuthorizedKeyRequest`; returns
  `OperationSummary`.

<!-- api-method: get_operation -->
## `get_operation`

- **Status / review:** Shared `OperationRuntime` lifecycle is completed and
  reviewed; identity operation producers remain pending their separate phase
  review.
- **Capability / purpose:** `operations.read`; inspect safe operation state.
  Gated on the shared `OperationRuntime`, never on the identity service, so an
  SFTP-only daemon (directory size, recursive copy/move/remove) can poll its
  own operations without identity support. Only the operation's owning client
  may inspect it; any other client gets `service_owner_required`.
- **Parameters / return:** `OperationId`; returns `OperationSummary`.

<!-- api-method: cancel_operation -->
## `cancel_operation`

- **Status / review:** Shared `OperationRuntime` lifecycle is completed and
  reviewed; identity operation producers remain pending their separate phase
  review.
- **Capability / purpose:** `operations.control`; cancel a cancellable
  operation. Gated on the shared `OperationRuntime`, never on the identity
  service. Only the operation's owning client may cancel it; any other client
  gets `service_owner_required`.
- **Parameters / return:** `OperationId`; returns `OperationSummary`.

<!-- api-method: get_global_ssh_overrides -->
## `get_global_ssh_overrides`

- **Status / introduced:** Implemented through `DaemonClient` when the SSH
  overrides service is installed / Protocol v1
- **Capability / purpose:** `ssh_overrides.read`; read the authoritative
  global SSH overrides state including a deterministic revision token.
- **Parameters / return:** No parameters; returns `GlobalSshOverrides`.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the SSH overrides
  service is not installed.
- **Cancellation / ordering:** Read-only; no ordering constraints.
- **Threading:** Safe from any thread.
- **Side effects / security:** Loads the settings file; no mutations.

```python
overrides = client.get_global_ssh_overrides()
print(overrides.connect_timeout, overrides.revision)
```

<!-- api-method: update_global_ssh_overrides -->
## `update_global_ssh_overrides`

- **Status / introduced:** Implemented through `DaemonClient` when the SSH
  overrides service is installed / Protocol v1
- **Capability / purpose:** `ssh_overrides.write`; partially update one or
  more SSH override fields with optimistic concurrency control.
- **Parameters / return:** `UpdateGlobalSshOverridesRequest` with a `patch`
  mapping and optional `expected_revision`; returns `GlobalSshOverrides`.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed. `VALIDATION_FAILED` with `revision_conflict` when
  `expected_revision` does not match the current revision.
- **Cancellation / ordering:** Mutations are serialized per daemon; concurrent
  stale writes are rejected.
- **Threading:** Thread-safe via internal lock.
- **Side effects / security:** Atomically persists the updated settings file.

```python
from sshpilot.api.models.settings import UpdateGlobalSshOverridesRequest

result = client.update_global_ssh_overrides(
    UpdateGlobalSshOverridesRequest(
        patch={"connect_timeout": 30, "compression": True},
        expected_revision=overrides.revision,
    )
)
```

<!-- api-method: reset_global_ssh_overrides -->
## `reset_global_ssh_overrides`

- **Status / introduced:** Implemented through `DaemonClient` when the SSH
  overrides service is installed / Protocol v1
- **Capability / purpose:** `ssh_overrides.write`; reset SSH override fields
  to application defaults.
- **Parameters / return:** Optional `expected_revision: str`; returns
  `GlobalSshOverrides`.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed. `VALIDATION_FAILED` with `revision_conflict` when
  `expected_revision` does not match.
- **Cancellation / ordering:** Serialized per daemon.
- **Threading:** Thread-safe via internal lock.
- **Side effects / security:** Atomically persists defaults to the settings
  file.

```python
result = client.reset_global_ssh_overrides(
    expected_revision=overrides.revision,
)
```

<!-- api-method: bitwarden_api_key_login -->
## `bitwarden_api_key_login`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.operate`; begin a Bitwarden API-key login.
  The client secret is never a parameter — the daemon prompts for it through the
  protected interaction path.
- **Parameters / return:** `client_id: str`; returns `BitwardenStatus`.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed. `INTERACTION_REQUIRED` / `INTERACTION_CANCELLED` for the client
  secret and 2FA steps.
- **Cancellation / ordering:** Mutations serialized per daemon.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** Runs `bw` inside the daemon; the client secret
  never crosses the wire.

```python
status = client.bitwarden_api_key_login(client_id="user.xxxx")
```

<!-- api-method: bitwarden_configure_server -->
## `bitwarden_configure_server`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.operate`; set the Bitwarden server URL.
- **Parameters / return:** `url: str`; returns `BitwardenStatus`.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed. `VALIDATION_FAILED` for a malformed URL.
- **Cancellation / ordering:** Mutations serialized per daemon.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** Persists the server URL in daemon-owned
  `secrets.*` configuration.

```python
status = client.bitwarden_configure_server("https://bitwarden.example.com")
```

<!-- api-method: bitwarden_lock -->
## `bitwarden_lock`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.operate`; lock the Bitwarden vault session.
- **Parameters / return:** No parameters; returns `BitwardenStatus`.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed.
- **Cancellation / ordering:** Mutations serialized per daemon.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** Runs `bw lock` inside the daemon.

```python
status = client.bitwarden_lock()
```

<!-- api-method: bitwarden_login -->
## `bitwarden_login`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.operate`; begin a Bitwarden master-password
  login. The master password is never a parameter — the daemon prompts for it
  through the protected interaction path.
- **Parameters / return:** `email: str`, optional `twofa_method: str`; returns
  `BitwardenStatus`.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed. `INTERACTION_REQUIRED` / `INTERACTION_CANCELLED` for the master
  password and 2FA code steps.
- **Cancellation / ordering:** Mutations serialized per daemon.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** Runs `bw login` inside the daemon; the master
  password and 2FA code never cross the wire.

```python
status = client.bitwarden_login(email="user@example.com")
```

<!-- api-method: bitwarden_logout -->
## `bitwarden_logout`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.operate`; log out of the Bitwarden account.
- **Parameters / return:** No parameters; returns `BitwardenStatus`.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed.
- **Cancellation / ordering:** Mutations serialized per daemon.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** Runs `bw logout` inside the daemon.

```python
status = client.bitwarden_logout()
```

<!-- api-method: bitwarden_sso_login -->
## `bitwarden_sso_login`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.operate`; begin a Bitwarden SSO login.
  Authentication secrets are handled by the protected interaction path.
- **Parameters / return:** No parameters; returns `BitwardenStatus`.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed. `INTERACTION_REQUIRED` / `INTERACTION_CANCELLED` for the
  authentication challenge.
- **Cancellation / ordering:** Mutations serialized per daemon.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** Runs the SSO flow inside the daemon; no secret
  value crosses the wire.

```python
status = client.bitwarden_sso_login()
```

<!-- api-method: bitwarden_status -->
## `bitwarden_status`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.operate`; read the Bitwarden vault status
  (metadata only — never secret values).
- **Parameters / return:** Optional `force_refresh: bool`; returns
  `BitwardenStatus`.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed.
- **Cancellation / ordering:** Read-only.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** No secrets; does not persist state.

```python
status = client.bitwarden_status()
```

<!-- api-method: bitwarden_sync -->
## `bitwarden_sync`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.operate`; sync the Bitwarden vault.
- **Parameters / return:** No parameters; returns `BitwardenStatus`.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed.
- **Cancellation / ordering:** Mutations serialized per daemon.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** Runs `bw sync` inside the daemon.

```python
status = client.bitwarden_sync()
```

<!-- api-method: bitwarden_unlock -->
## `bitwarden_unlock`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.operate`; unlock the Bitwarden vault. The
  master password is never a parameter — the daemon prompts for it through the
  protected interaction path.
- **Parameters / return:** No parameters; returns `BitwardenStatus`.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed. `INTERACTION_REQUIRED` / `INTERACTION_CANCELLED` for the master
  password.
- **Cancellation / ordering:** Mutations serialized per daemon.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** Runs `bw unlock` inside the daemon; the master
  password never crosses the wire.

```python
status = client.bitwarden_unlock()
```

<!-- api-method: export_secret_backup -->
## `export_secret_backup`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.transfer`; export secret backups entirely
  inside the daemon.
- **Parameters / return:** Keyword-only `destination: str`, optional
  `connection_ids`, `options`, and `mirror_logins`; returns
  `SecretTransferResult` with counts and structured presentation messages.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed. `INTERACTION_REQUIRED` / `INTERACTION_CANCELLED` when the
  destination is encrypted.
- **Cancellation / ordering:** Mutations serialized per daemon.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** Reads the selected secret backend and writes the
  backup archive inside the daemon; no secret values are returned.

```python
result = client.export_secret_backup(destination="/home/me/secrets.spbk")
```

<!-- api-method: get_secret_backends -->
## `get_secret_backends`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.read`; return the backend registry with
  availability and lock metadata — never secret values.
- **Parameters / return:** No parameters; returns `SecretBackendRegistry`.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed.
- **Cancellation / ordering:** Read-only.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** Metadata only.

```python
registry = client.get_secret_backends()
```

<!-- api-method: get_secret_configuration -->
## `get_secret_configuration`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.read`; return the daemon-owned `secrets.*`
  configuration snapshot.
- **Parameters / return:** No parameters; returns `SecretConfiguration`.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed.
- **Cancellation / ordering:** Read-only.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** Loads the daemon settings file; no secrets in the
  payload.

```python
configuration = client.get_secret_configuration()
```

<!-- api-method: get_secret_state -->
## `get_secret_state`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.read`; return the current backend selection
  and lock state — metadata only.
- **Parameters / return:** No parameters; returns `SecretBackendState`.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed.
- **Cancellation / ordering:** Read-only.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** No secrets in the payload.

```python
state = client.get_secret_state()
```

<!-- api-method: import_secret_backup -->
## `import_secret_backup`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.transfer`; import a secret backup entirely
  inside the daemon.
- **Parameters / return:** Keyword-only `source: str`, optional `options`; returns
  `SecretTransferResult` with counts and structured presentation messages.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed. `INTERACTION_REQUIRED` / `INTERACTION_CANCELLED` when the source is
  encrypted.
- **Cancellation / ordering:** Mutations serialized per daemon.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** Reads the backup archive and stores secrets into
  the selected backend inside the daemon; no secret values are returned.

```python
result = client.import_secret_backup(source="/home/me/secrets.spbk")
```

<!-- api-method: lock_secrets -->
## `lock_secrets`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.operate`; lock the selected secret backend.
- **Parameters / return:** No parameters; returns `SecretBackendState`.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed.
- **Cancellation / ordering:** Mutations serialized per daemon.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** Locks the daemon-owned backend session.

```python
state = client.lock_secrets()
```

<!-- api-method: rbw_configure -->
## `rbw_configure`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.operate`; configure the rbw Bitwarden CLI.
- **Parameters / return:** `email: str`, `base_url: str`; returns `RbwStatus`.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed.
- **Cancellation / ordering:** Mutations serialized per daemon.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** Persists rbw settings in daemon-owned
  configuration; the master password is handled by the protected interaction
  path on unlock.

```python
status = client.rbw_configure(email="user@example.com", base_url="bitwarden.example.com")
```

<!-- api-method: rbw_lock -->
## `rbw_lock`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.operate`; lock the rbw session.
- **Parameters / return:** No parameters; returns `RbwStatus`.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed.
- **Cancellation / ordering:** Mutations serialized per daemon.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** Runs `rbw lock` inside the daemon.

```python
status = client.rbw_lock()
```

<!-- api-method: rbw_status -->
## `rbw_status`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.operate`; read the rbw status — metadata
  only.
- **Parameters / return:** No parameters; returns `RbwStatus`.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed.
- **Cancellation / ordering:** Read-only.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** No secrets in the payload.

```python
status = client.rbw_status()
```

<!-- api-method: rbw_sync -->
## `rbw_sync`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.operate`; sync the rbw vault.
- **Parameters / return:** No parameters; returns `RbwStatus`.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed.
- **Cancellation / ordering:** Mutations serialized per daemon.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** Runs `rbw sync` inside the daemon.

```python
status = client.rbw_sync()
```

<!-- api-method: rbw_unlock -->
## `rbw_unlock`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.operate`; unlock the rbw vault. The master
  password is never a parameter — the daemon prompts for it through the
  protected interaction path.
- **Parameters / return:** No parameters; returns `RbwStatus`.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed. `INTERACTION_REQUIRED` / `INTERACTION_CANCELLED` for the master
  password.
- **Cancellation / ordering:** Mutations serialized per daemon.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** Runs `rbw unlock` inside the daemon; the master
  password never crosses the wire.

```python
status = client.rbw_unlock()
```

<!-- api-method: unlock_secrets -->
## `unlock_secrets`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.operate`; unlock the selected secret
  backend. The secret is never a parameter — the daemon prompts for it through
  the protected interaction path.
- **Parameters / return:** No parameters; returns `SecretUnlockResult`.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed. `INTERACTION_REQUIRED` / `INTERACTION_CANCELLED` for the secret.
- **Cancellation / ordering:** Mutations serialized per daemon.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** Unlocks the daemon-owned backend session; the
  secret never crosses the wire.

```python
result = client.unlock_secrets()
```

<!-- api-method: update_secret_configuration -->
## `update_secret_configuration`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.write`; partially update the daemon-owned
  `secrets.*` configuration.
- **Parameters / return:** `UpdateSecretConfigurationRequest`; returns
  `SecretConfiguration`.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed. `VALIDATION_FAILED` for invalid values; `revision_conflict` when an
  `expected_revision` does not match.
- **Cancellation / ordering:** Mutations serialized per daemon; stale writes are
  rejected.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** Atomically persists the daemon settings file; no
  secret values are accepted or returned.

```python
from sshpilot.api.models.secrets import UpdateSecretConfigurationRequest

configuration = client.update_secret_configuration(
    UpdateSecretConfigurationRequest(patch={"session_timeout": 30})
)
```

<!-- api-method: update_secret_selection -->
## `update_secret_selection`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.write`; change the selected secret backend.
- **Parameters / return:** `backend: str`; returns `SecretBackendState`.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed. `VALIDATION_FAILED` for an unknown backend name.
- **Cancellation / ordering:** Mutations serialized per daemon.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** Persists the selection in daemon-owned
  configuration.

```python
state = client.update_secret_selection(backend="bitwarden")
```

<!-- api-method: preview_backup -->
## `preview_backup`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.transfer`; inspect a backup file's kind,
  encryption flag and included categories without exposing its contents.
- **Parameters / return:** Keyword-only `source: str`; returns a strict
  `SecretTransferPreview` (`kind`, `encrypted`, `included`, optional structured
  `error`) — never manifest contents.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed. `INTERACTION_REQUIRED` / `INTERACTION_CANCELLED` when the archive
  is encrypted.
- **Cancellation / ordering:** Reads serialized per daemon.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** Decryption happens inside the daemon; the
  decrypted manifest is cached briefly so the following import never re-prompts
  for the passphrase.

```python
preview = client.preview_backup(source="/home/me/secrets.spbk")
```

<!-- api-method: list_bitwarden_backups -->
## `list_bitwarden_backups`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.transfer`; list Bitwarden backup-note
  metadata (id/name/date only).
- **Parameters / return:** None; returns a list of metadata dicts.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed.
- **Cancellation / ordering:** Reads serialized per daemon.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** The Bitwarden backend is touched only inside the
  daemon; note contents are never returned.

```python
entries = client.list_bitwarden_backups()
```

<!-- api-method: import_bitwarden_backup -->
## `import_bitwarden_backup`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.transfer`; restore one Bitwarden backup
  note entirely inside the daemon.
- **Parameters / return:** Keyword-only `entry_id: str`, optional `options`;
  returns `SecretTransferResult` with counts and structured presentation
  messages.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed.
- **Cancellation / ordering:** Mutations serialized per daemon.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** Reads the note and restores secrets inside the
  daemon; no decrypted values are returned.

```python
result = client.import_bitwarden_backup(entry_id="abc123")
```

<!-- api-method: preview_bitwarden_backup -->
## `preview_bitwarden_backup`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.transfer`; inspect one Bitwarden backup
  note's included categories (metadata only).
- **Parameters / return:** Keyword-only `entry_id: str`; returns a strict
  `SecretTransferPreview` — never manifest contents.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed.
- **Cancellation / ordering:** Reads serialized per daemon.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** Note contents are read only inside the daemon;
  the decrypted manifest is cached briefly for the following import.

```python
preview = client.preview_bitwarden_backup(entry_id="abc123")
```

<!-- api-method: list_ssh_backups -->
## `list_ssh_backups`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.transfer`; list sshPilot backups stored in
  a directory on one of the user's SSH servers.
- **Parameters / return:** Keyword-only `connection_id: str`,
  `remote_dir: str`; returns a list of metadata dicts (id/name/date).
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed.
- **Cancellation / ordering:** Reads serialized per daemon.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** The SSH transfer runs inside the daemon; archive
  bytes never leave the daemon.

```python
entries = client.list_ssh_backups(connection_id="srv", remote_dir="~/sshpilot-backups")
```

<!-- api-method: import_ssh_backup -->
## `import_ssh_backup`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.transfer`; download and restore one
  SSH-stored backup entirely inside the daemon.
- **Parameters / return:** Keyword-only `connection_id: str`,
  `remote_dir: str`, `entry_id: str`, optional `options`; returns
  `SecretTransferResult` with counts and structured presentation messages.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed.
- **Cancellation / ordering:** Mutations serialized per daemon.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** The archive is read and applied inside the
  daemon; no decrypted values are returned.

```python
result = client.import_ssh_backup(
    connection_id="srv", remote_dir="~/sshpilot-backups", entry_id="b1")
```

<!-- api-method: preview_ssh_backup -->
## `preview_ssh_backup`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.transfer`; inspect one SSH-stored
  backup's included categories (metadata only).
- **Parameters / return:** Keyword-only `connection_id: str`,
  `remote_dir: str`, `entry_id: str`; returns a strict
  `SecretTransferPreview` — never manifest contents.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed.
- **Cancellation / ordering:** Reads serialized per daemon.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** The archive is read only inside the daemon; the
  decrypted manifest is cached briefly for the following import.

```python
preview = client.preview_ssh_backup(
    connection_id="srv", remote_dir="~/sshpilot-backups", entry_id="b1")
```

<!-- api-method: keepassxc_create_database -->
## `keepassxc_create_database`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.operate`; create a KeePassXC database at
  the configured path and unlock it.
- **Parameters / return:** Keyword-only `path: str`, optional `keyfile`; returns
  `SecretOperationResult`.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed. `INTERACTION_REQUIRED` / `INTERACTION_CANCELLED` when the daemon
  collects the new database password.
- **Cancellation / ordering:** Mutations serialized per daemon.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** The master password is collected by a protected
  interaction inside the daemon, never through RPC parameters.

```python
result = client.keepassxc_create_database(path="/home/me/Secrets.kdbx")
```

<!-- api-method: keepassxc_unlock -->
## `keepassxc_unlock`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.operate`; unlock the configured KeePassXC
  database.
- **Parameters / return:** None; returns `SecretOperationResult`.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed. `INTERACTION_REQUIRED` / `INTERACTION_CANCELLED` when the daemon
  collects the master password.
- **Cancellation / ordering:** Mutations serialized per daemon.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** The master password never crosses the RPC
  surface.

```python
result = client.keepassxc_unlock()
```

<!-- api-method: keepassxc_lock -->
## `keepassxc_lock`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.operate`; lock the KeePassXC database.
- **Parameters / return:** None; returns `SecretOperationResult`.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed.
- **Cancellation / ordering:** Mutations serialized per daemon.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** Locking happens inside the daemon.

```python
result = client.keepassxc_lock()
```

<!-- api-method: remember_master_password -->
## `remember_master_password`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.operate`; store the master password in the
  platform keyring for later automatic unlocks.
- **Parameters / return:** None; returns `SecretOperationResult`.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed.
- **Cancellation / ordering:** Mutations serialized per daemon.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** Uses the existing platform-keyring helpers and
  master-password identity inside the daemon.

```python
result = client.remember_master_password()
```

<!-- api-method: forget_master_password -->
## `forget_master_password`

- **Status / introduced:** Daemon only when the secret backend service is
  installed / Protocol v1
- **Capability / purpose:** `secrets.operate`; remove the remembered master
  password from the platform keyring.
- **Parameters / return:** None; returns `SecretOperationResult`.
- **Errors / events:** `UNSUPPORTED_CAPABILITY` when the service is not
  installed.
- **Cancellation / ordering:** Mutations serialized per daemon.
- **Threading:** Thread-safe via the client's request lock.
- **Side effects / security:** Keyring access happens inside the daemon.

```python
result = client.forget_master_password()
```

<!-- api-method: close -->
## `close`

- **Status / introduced:** Implemented / Protocol v1
- **Capability / purpose:** None; release manager signal handlers/event
  subscribers or daemon socket resources.
- **Parameters / return:** No parameters; returns `None`.
- **Errors / events:** No documented error; emits no event.
- **Cancellation / ordering:** Idempotent. Existing callbacks are removed.
- **Threading:** No owner-thread assertion exists, although production callers
  should close from their composition/GTK owner thread.
- **Side effects / security:** Direct core teardown unregisters service
  signals. Daemon shutdown closes only that client's socket. Neither closes
  saved connections, SSH processes, or secrets.

```python
client.close()
client.close()  # idempotent
```

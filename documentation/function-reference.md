# Function and Method Reference

This document enumerates the functions and methods available in the `sshpilot` package. Each entry includes its signature and a brief description.

## Module: `sshpilot.actions`

### Functions

- **`register_window_actions(window)`** — Register SimpleActions with the provided main window.

### Class: `WindowActions`

- **`_update_sidebar_accelerators()`** — Apply sidebar accelerators respecting pass-through settings.

- **`on_broadcast_command_action(action, param=None)`** — Handle broadcast command action - shows dialog to input command

- **`on_create_group_action(action, param=None)`** — Handle create group action

- **`on_delete_connection_action(action, param=None)`** — Handle delete connection action from context menu

- **`on_delete_group_action(action, param=None)`** — Handle delete group action

- **`on_duplicate_connection_action(action, param=None)`** — Duplicate the currently selected connection.

- **`on_edit_connection_action(action, param=None)`** — Handle edit connection action from context menu

- **`on_edit_group_action(action, param=None)`** — Handle edit group action

- **`on_edit_known_hosts_action(action, param=None)`** — Open the known hosts editor window.

- **`on_manage_files_action(action, param=None)`** — Handle manage files action from context menu

- **`on_move_to_group_action(action, param=None)`** — Handle move to group action

- **`on_move_to_ungrouped_action(action, param=None)`** — Handle move to ungrouped action

- **`on_open_in_system_terminal_action(action, param=None)`** — Handle open in system terminal action from context menu

- **`on_open_new_connection_action(action, param=None)`** — Open a new tab for the selected connection via context menu.

- **`on_open_new_connection_tab_action(action, param=None)`** — Open a new tab for the selected connection via global shortcut (Ctrl/⌘+Alt+N).

- **`on_toggle_sidebar_action(action, param)`** — Handle sidebar toggle action (for keyboard shortcuts)



## Module: `sshpilot.agent_client`

### Functions

- **`create_agent_launcher_script()`** — Create a standalone launcher script for the agent.

### Class: `AgentClient`

- **`__init__()`** — Handles init.

- **`_build_flatpak_agent_command(rows=24, cols=80, cwd=None, verbose=False)`** — Build agent command for Flatpak environment.

- **`build_agent_command(rows=24, cols=80, cwd=None, verbose=False)`** — Build the command to launch the agent.

- **`find_agent()`** — Find the agent script and Python interpreter.

- **`get_agent_fds(process)`** — Get file descriptors for agent communication.

- **`launch_agent(rows=24, cols=80, cwd=None, verbose=False)`** — Launch the agent process.

- **`wait_for_ready(process, timeout=5.0)`** — Wait for agent to signal ready.



## Module: `sshpilot.askpass_utils`

### Functions

- **`_askpass_log_forwarder_loop()`** — Background loop that forwards askpass logs to the module logger.

- **`_get_key_path_lookup_candidates(key_path)`** — Return normalized key path variants for lookup and compatibility.

- **`_home_alias_for_path(path)`** — Return a home-relative alias (~/...) for *path* when applicable.

- **`_normalize_key_path_for_storage(key_path)`** — Return a canonical representation for storing passphrases.

- **`clear_passphrase(key_path)`** — Remove a stored key passphrase using keyring (macOS) or libsecret (Linux).

- **`connect_ssh_with_key(host, username, key_path, command=None)`** — Connect via SSH with proper key handling

- **`ensure_askpass_log_forwarder()`** — Ensure a background thread is forwarding askpass logs to the logger.

- **`ensure_askpass_script()`** — Ensure the askpass script is available for passphrase handling

- **`ensure_key_in_agent(key_path)`** — Ensure SSH key is loaded in ssh-agent with passphrase

- **`ensure_passphrase_askpass()`** — Ensure the askpass script exists and return its path

- **`force_regenerate_askpass_script()`** — Force regeneration of the askpass script

- **`forward_askpass_log_to_logger(log, include_existing=False)`** — Forward askpass log lines into the main application logger when debug logging is enabled.

- **`get_askpass_log_path()`** — Return the path to the askpass log file.

- **`get_scp_ssh_options()`** — Get SSH options for SCP operations with passphrased keys

- **`get_secret_schema()`** — Return the shared Secret.Schema for stored secrets.

- **`get_ssh_env_with_askpass(require='prefer')`** — Get SSH environment with askpass for passphrase handling

- **`get_ssh_env_with_askpass_for_password(host, username)`** — Return a copy of the environment without SSH_ASKPASS variables.

- **`get_ssh_env_with_forced_askpass()`** — Get SSH environment with forced askpass for passphrase handling

- **`is_macos()`** — Checks whether macos.

- **`lookup_passphrase(key_path)`** — Look up a key passphrase using keyring (macOS) or libsecret (Linux).

- **`prepare_key_for_connection(key_path)`** — Prepare SSH key for connection by ensuring it's in ssh-agent

- **`read_new_askpass_log_lines(include_existing=False)`** — Read newly appended askpass log lines.

- **`stop_askpass_log_forwarder()`** — Stop the background askpass log forwarder thread.

- **`store_passphrase(key_path, passphrase)`** — Store a key passphrase using keyring (macOS) or libsecret (Linux).



## Module: `sshpilot.config`

### Class: `Config`

- **`__init__()`** — Handles init.

- **`_clone_shortcut_overrides(overrides)`** — Return a shallow copy of the override mapping with cloned accelerator lists.

- **`_ensure_config_defaults(config)`** — Ensure newly added keys exist in the provided config dict.

- **`add_custom_theme(name, theme_data)`** — Add a custom theme

- **`clear_shortcut_overrides()`** — Remove all stored shortcut overrides.

- **`export_config(file_path)`** — Export configuration to file

- **`get_available_themes()`** — Get list of available themes

- **`get_connection_meta(key)`** — Return stored metadata for a connection keyed by nickname (or unique key).

- **`get_default_config()`** — Get default configuration values

- **`get_file_manager_config()`** — Return configuration relevant to the built-in SFTP file manager.

- **`get_security_config()`** — Get security configuration

- **`get_setting(key, default=None)`** — Get a setting value

- **`get_shortcut_override(action_name)`** — Return the stored shortcut override for an action.

- **`get_shortcut_override(action_name)`** — Return the stored accelerators for the given action, if any.

- **`get_shortcut_overrides()`** — Return the stored shortcut overrides mapping.

- **`get_shortcut_overrides()`** — Return a mapping of action names to user-defined shortcut overrides.

- **`get_ssh_config()`** — Get SSH configuration values with sensible defaults.

- **`get_terminal_profile(theme_name=None)`** — Get terminal theme profile

- **`get_window_geometry()`** — Get saved window geometry

- **`import_config(file_path)`** — Import configuration from file

- **`load_builtin_themes()`** — Load built-in terminal themes

- **`load_json_config()`** — Load configuration from JSON file

- **`on_setting_changed(settings, key)`** — Handle GSettings change

- **`remove_custom_theme(name)`** — Remove a custom theme

- **`reset_to_defaults()`** — Reset all settings to defaults

- **`save_json_config(config_data=None)`** — Save configuration to JSON file

- **`save_window_geometry(width, height, sidebar_width=None)`** — Save window geometry

- **`set_connection_meta(key, meta)`** — Store metadata for a connection.

- **`set_setting(key, value)`** — Set a setting value

- **`set_shortcut_override(action_name, accelerators)`** — Persist a shortcut override.

- **`set_shortcut_override(action_name, shortcuts)`** — Persist user-defined accelerators for a specific action.



## Module: `sshpilot.connection_dialog`

### Class: `ConnectionDialog`

- **`__init__(parent, connection=None, connection_manager=None, force_split_from_group=False, split_group_source=None, split_original_nickname=None)`** — Handles init.

- **`_apply_validation_to_row(row, result)`** — Handles apply validation to row.

- **`_auto_select_matching_certificate(key_path)`** — Auto-select certificate that matches the selected key

- **`_autosave_forwarding_changes()`** — Disabled autosave to avoid log floods; saving occurs on dialog Save.

- **`_connect_row_validation(row, validator_callable)`** — Connects row validation.

- **`_focus_row(row)`** — Handles focus row.

- **`_generate_ssh_config_from_settings()`** — Generate SSH config block from current connection settings

- **`_install_inline_validators()`** — Handles install inline validators.

- **`_is_nickname_taken(name)`** — Checks whether nickname taken.

- **`_open_rule_editor(existing_rule=None)`** — Open an Adw.Window to add/edit a forwarding rule.

- **`_populate_detected_certificates()`** — Populate certificate dropdown with detected certificate files.

- **`_populate_detected_keys()`** — Populate key dropdown with detected private keys and a Browse item (reuse KeyManager.discover_keys).

- **`_refresh_connection_data_from_ssh_config()`** — Refresh connection data from the updated SSH config file

- **`_row_clear_message(row)`** — Handles row clear message.

- **`_row_set_message(row, message, is_error=True)`** — Handles row set message.

- **`_run_initial_validation()`** — Handles run initial validation.

- **`_sanitize_forwarding_rules(rules)`** — Validate and normalize forwarding rules before saving.

- **`_save_rule_from_editor(existing_rule, type_row, listen_addr_row, listen_port_row, remote_host_row, remote_port_row)`** — Saves rule from editor.

- **`_show_port_info_dialog()`** — Show a window with current port information

- **`_sync_cert_dropdown_with_current_cert()`** — Sync the certificate dropdown selection with the current certificate path

- **`_sync_key_dropdown_with_current_keyfile()`** — Sync the key dropdown selection with the current keyfile path

- **`_update_existing_names_in_validator()`** — Updates existing names in validator.

- **`_update_passphrase_for_key(key_path)`** — Update the passphrase field when a different key is selected

- **`_update_save_buttons()`** — Updates save buttons.

- **`_validate_all_required_for_save()`** — Validate all visible fields; return the first invalid row (or None).

- **`_validate_field_row(field_name, row, context='SSH')`** — Handles validate field row.

- **`_validate_host_row(row, allow_empty=False)`** — Handles validate host row.

- **`_validate_nickname_row(row)`** — Handles validate nickname row.

- **`_validate_port_row(row, label_text='Port')`** — Handles validate port row.

- **`_validate_required_row(row, label_text)`** — Handles validate required row.

- **`browse_for_certificate_file()`** — Open file chooser to browse for SSH certificate file using portal-aware API.

- **`browse_for_key_file()`** — Open file chooser to browse for SSH key file using portal-aware API.

- **`build_commands_group()`** — Build PreferencesGroup for configuring connection commands

- **`build_connection_groups()`** — Build PreferencesGroups for the General page

- **`build_port_forwarding_groups()`** — Build PreferencesGroups for the Advanced page (Port Forwarding first, X11 last)

- **`create_bottom_section()`** — Create the pinned bottom section with save/cancel buttons

- **`create_preferences_content()`** — Create the preferences content with all pages

- **`load_connection_data()`** — Load connection data into the dialog fields

- **`load_port_forwarding_rules()`** — Load port forwarding rules from the connection and update UI

- **`on_add_forwarding_rule_clicked(button)`** — Handle add port forwarding rule button click

- **`on_auth_method_changed(combo_row, param)`** — Handle authentication method change

- **`on_cancel_clicked(button)`** — Handle cancel button click

- **`on_certificate_file_selected(dialog, result)`** — Handle selected certificate file from file chooser

- **`on_delete_forwarding_rule_clicked(button, rule)`** — Handle delete port forwarding rule button click

- **`on_edit_forwarding_rule_clicked(button, rule)`** — Handle edit port forwarding rule button click

- **`on_forwarding_toggled(switch, param, settings_box)`** — Handle toggling of port forwarding settings visibility and state

- **`on_key_file_selected(dialog, result)`** — Handle selected key file from file chooser

- **`on_key_select_changed(combo_row, param)`** — Enable browse button only when 'Use a specific key' is selected.

- **`on_save_clicked(*_args)`** — Handle save button click or dialog save response

- **`on_view_port_info_clicked(button)`** — Handle view port info button click

- **`setup_keyboard_shortcuts()`** — Setup keyboard shortcuts for common actions

- **`setup_ui()`** — Set up the dialog UI with pinned buttons

- **`show_error(message)`** — Show error message

- **`validate_ssh_config_syntax(config_text)`** — Basic SSH config syntax validation

### Class: `SSHConfigAdvancedTab`

- **`__init__(connection_manager, parent_dialog=None)`** — Handles init.

- **`_get_dropdown_selected_text(dropdown)`** — Get the selected text from a dropdown

- **`_on_editor_saved()`** — Refresh preview and parent dialog after editor saves.

- **`_set_dropdown_to_option(dropdown, option_name)`** — Set dropdown to a specific SSH option

- **`_update_parent_connection()`** — Update the parent connection object with current advanced tab data

- **`create_config_entry_row()`** — Create a new config entry row

- **`generate_ssh_config(hostname='your-host-name')`** — Generate SSH config block

- **`get_config_entries()`** — Get all valid config entries

- **`get_extra_ssh_config()`** — Get extra SSH config as a string for saving

- **`on_add_option(button)`** — Add a new SSH option entry

- **`on_edit_ssh_config_clicked(button)`** — Open raw editor for the SSH config file.

- **`on_entry_changed(widget, pspec=None)`** — Handle entry text changes

- **`on_remove_option(button, row_grid)`** — Remove a SSH option entry

- **`on_value_entry_activate(entry, row_grid)`** — Handle Enter key press in value entry - move to next row or add new one

- **`set_config_entries(entries)`** — Set config entries from saved data

- **`set_extra_ssh_config(config_string)`** — Set extra SSH config from a string

- **`setup_config_preview()`** — Setup the SSH config preview section

- **`setup_ui()`** — Setup the user interface

- **`update_config_preview()`** — Update the SSH config preview

### Class: `SSHConfigEntry`

- **`__init__(key='', value='')`** — Handles init.

### Class: `SSHConnectionValidator`

- **`__init__()`** — Handles init.

- **`_validate_hostname(hostname)`** — Handles validate hostname.

- **`_validate_ip_address(ip_str)`** — Handles validate ip address.

- **`set_existing_names(names)`** — Sets existing names.

- **`validate_connection_name(name)`** — Handles validate connection name.

- **`validate_hostname(hostname, allow_empty=False)`** — Handles validate hostname.

- **`validate_port(port, context='SSH')`** — Handles validate port.

- **`validate_username(username)`** — Handles validate username.

- **`verify_key_passphrase(key_path, passphrase)`** — Verify that the passphrase matches the private key using ssh-keygen -y

### Class: `ValidationResult`

- **`__init__(is_valid=True, message='', severity='info')`** — Handles init.

### Class: `_DummyGIMeta`

- **`__call__(*args, **kwargs)`** — Handles call.

- **`__getattr__(name)`** — Handles getattr.

### Class: `_DummyGLib`

- **`idle_add(*args, **kwargs)`** — Handles idle add.



## Module: `sshpilot.connection_display`

### Functions

- **`format_connection_host_display(connection, include_port=False)`** — Create a user-facing string describing host/alias details for a connection.

- **`get_connection_alias(connection)`** — Return the alias/nickname used to identify the connection in SSH config.

- **`get_connection_host(connection)`** — Return the configured hostname for a connection when available.



## Module: `sshpilot.connection_manager`

### Class: `Connection`

- **`__init__(data)`** — Handles init.

- **`__str__()`** — Handles str.

- **`_forward_data(reader, writer, label)`** — Helper method to forward data between two streams

- **`_resolve_config_override_path()`** — Return an absolute path to the SSH config override, if any.

- **`_update_identity_agent_state(directive)`** — Update cached IdentityAgent directive information for the connection.

- **`_update_properties_from_data(data)`** — Update instance properties from data dictionary

- **`collect_identity_file_candidates(effective_cfg=None)`** — Return resolved identity file paths that exist on disk for this host.

- **`connect()`** — Prepare SSH command for later use (no preflight echo).

- **`disconnect()`** — Close the SSH connection and clean up

- **`get_effective_host()`** — Return the hostname used for operations, falling back to aliases.

- **`native_connect()`** — Prepare a minimal SSH command deferring to the user's SSH configuration.

- **`resolve_host_identifier()`** — Return the preferred host alias used for launching native SSH commands.

- **`setup_forwarding()`** — Set up all forwarding rules

- **`source_file()`** — Return path to the config file where this host is defined.

- **`start_dynamic_forwarding(listen_addr, listen_port)`** — Start dynamic port forwarding (SOCKS proxy) using system SSH client

- **`start_local_forwarding(listen_addr, listen_port, remote_host, remote_port)`** — Start local port forwarding using system SSH client

- **`start_remote_forwarding(listen_addr, listen_port, remote_host, remote_port)`** — Start remote port forwarding using system SSH client

- **`update_data(new_data)`** — Update connection data while preserving object identity

### Class: `ConnectionManager`

- **`__init__(config, isolated_mode=False)`** — Handles init.

- **`_ensure_secure_permissions(path, mode)`** — Best effort at applying restrictive permissions to files/directories.

- **`_ensure_ssh_agent()`** — Ensure ssh-agent is running and export environment variables

- **`_get_active_connection_key(connection)`** — Returns active connection key.

- **`_get_active_connection_key(connection, prefer_stored=True)`** — Return the key used to track a connection's keepalive task.

- **`_get_active_connection_key(connection)`** — Return the dictionary key used to track active connection tasks.

- **`_get_keyring_backend_name()`** — Return a descriptive name for the active keyring backend.

- **`_post_init_slow_path()`** — Run slower initialization steps after UI is responsive.

- **`_should_use_keyring_fallback(force=False)`** — Return True when we should consult the cross-platform keyring.

- **`_split_host_block(original_host, new_data, target_path)`** — Remove *original_host* from its group and append a new block.

- **`add_key_to_agent(key_path)`** — Add SSH key to ssh-agent using secure SSH_ASKPASS script

- **`connect(connection)`** — Connect to an SSH host asynchronously

- **`delete_key_passphrase(key_path)`** — Delete stored key passphrase from system keyring

- **`delete_password(host, username)`** — Delete stored password for host/user from system keyring

- **`disconnect(connection)`** — Disconnect from SSH host and clean up resources asynchronously

- **`find_connection_by_nickname(nickname)`** — Find connection by nickname

- **`format_ssh_config_entry(data)`** — Format connection data as SSH config entry

- **`get_connections()`** — Get list of all connections

- **`get_host_block_details(host_identifier, source=None)`** — Return details for the first Host block matching *host_identifier*.

- **`get_key_passphrase(key_path)`** — Retrieve key passphrase from system keyring

- **`get_password(host, username)`** — Retrieve password from system keyring

- **`invalidate_cached_commands()`** — Clear cached SSH commands so future launches pick up new settings.

- **`load_ssh_config()`** — Load connections from SSH config file

- **`load_ssh_keys()`** — Auto-detect SSH keys in configured SSH directories.

- **`parse_host_config(config, source=None)`** — Parse host configuration from SSH config

- **`prepare_key_for_connection(key_path)`** — Prepare SSH key for connection by adding it to ssh-agent

- **`remove_connection(connection)`** — Remove connection from config and list

- **`remove_ssh_config_entry(host_nickname, source=None)`** — Remove a host label from SSH config, or entire block if it's the only label.

- **`set_isolated_mode(isolated)`** — Switch between standard and isolated SSH configuration

- **`store_key_passphrase(key_path, passphrase)`** — Store key passphrase securely in system keyring

- **`store_password(host, username, password)`** — Store password securely in system keyring

- **`update_connection(connection, new_data)`** — Update an existing connection

- **`update_connection_status(connection, is_connected)`** — Update connection status in the manager

- **`update_ssh_config_file(connection, new_data, original_nickname=None)`** — Update SSH config file with new connection data

### Class: `GLibEventLoopPolicy`

- **`new_event_loop()`** — Handles new event loop.



## Module: `sshpilot.file_manager_integration`

### Functions

- **`create_internal_file_manager_tab(user, host, port=None, nickname=None, parent_window=None, connection=None, connection_manager=None, ssh_config=None)`** — Create an embedded file manager suitable for use inside a tab.

- **`has_internal_file_manager()`** — Return True when the built-in file manager window is available.

- **`has_native_gvfs_support()`** — Return True when the platform supports GVFS based file management.

- **`launch_remote_file_manager(user, host, port=None, nickname=None, parent_window=None, error_callback=None, connection=None, connection_manager=None, ssh_config=None)`** — Launch the appropriate file manager for the supplied connection.

- **`open_internal_file_manager(user, host, port=None, parent_window=None, nickname=None, connection=None, connection_manager=None, ssh_config=None)`** — Instantiate and present the built-in file manager window.

### Class: `FileManagerTabEmbed`

- **`__init__(controller, content)`** — Handles init.

- **`__init__(controller, content)`** — Handles init.

- **`_on_destroy(*_args)`** — Handles destroy.

- **`append(*_args, **_kwargs)`** — Handles append.

- **`connect(signal, callback)`** — Handles connect.

- **`destroy()`** — Handles destroy.

- **`set_hexpand(*_args, **_kwargs)`** — Sets hexpand.

- **`set_vexpand(*_args, **_kwargs)`** — Sets vexpand.



## Module: `sshpilot.file_manager_window`

### Functions

- **`_ensure_cfg_dir()`** — Ensure the config directory exists.

- **`_get_docs_json_path()`** — Get the path to the granted folders config file.

- **`_grant_persistent_access(gfile)`** — Grant persistent access to a file via the Document portal (Flatpak only).

- **`_human_size(n)`** — Convert bytes to human readable format.

- **`_human_time(ts)`** — Convert timestamp to human readable format.

- **`_load_doc_config()`** — Load the granted folders configuration file.

- **`_load_first_doc_path()`** — Load the first valid document portal path from saved config.

- **`_lookup_doc_entry(doc_id)`** — Return the stored configuration entry for the given document ID.

- **`_lookup_document_path(doc_id)`** — Look up the current path for a document ID.

- **`_lookup_path_from_config(doc_id)`** — Look up the original path from our config.

- **`_mode_to_str(mode)`** — Convert file mode to string representation like -rw-r--r--.

- **`_portal_doc_path(doc_id)`** — Get the portal mount path for a document ID.

- **`_pretty_path_for_display(path)`** — Convert a filesystem path to a human-friendly display string.

- **`_save_doc(folder_path, doc_id)`** — Save document ID, display name, and actual path to JSON config.

- **`_sftp_path_exists(sftp, path)`** — Return ``True`` if *path* exists on the remote SFTP server.

- **`launch_file_manager_window(host, username, port=22, path='~', parent=None, transient_for_parent=True, nickname=None, connection=None, connection_manager=None, ssh_config=None)`** — Create and present the :class:`FileManagerWindow`.

- **`stat_isdir(attr)`** — Return ``True`` when the attribute represents a directory.

- **`walk_remote(sftp, root)`** — Yield a remote directory tree similar to :func:`os.walk`.

### Class: `AsyncSFTPManager`

- **`__init__(host, username, port=22, password=None, dispatcher=None, connection=None, connection_manager=None, ssh_config=None)`** — Handles init.

- **`_connect_impl()`** — Connects impl.

- **`_create_proxy_jump_socket(jump_entries, config_override, policy, known_hosts_path, allow_agent, look_for_keys, key_filename, passphrase, resolved_host, resolved_port, base_username, connect_timeout=None)`** — Create a socket by chaining SSH connections through jump hosts.

- **`_format_size(size_bytes)`** — Format file size for display

- **`_parse_proxy_jump_entry(entry)`** — Parse a ``ProxyJump`` token into host, optional user, and port.

- **`_select_host_key_policy(strict_host, auto_add)`** — Return an appropriate Paramiko host key policy based on settings.

- **`_start_keepalive_worker()`** — Handles start keepalive worker.

- **`_stop_keepalive_worker()`** — Handles stop keepalive worker.

- **`_submit(func, on_success=None, on_error=None)`** — Handles submit.

- **`close()`** — Handles close.

- **`connect_to_server()`** — Connects to server.

- **`download(source, destination)`** — Handles download.

- **`download_directory(source, destination)`** — Handles download directory.

- **`listdir(path)`** — Handles listdir.

- **`mkdir(path)`** — Handles mkdir.

- **`path_exists(path)`** — Return a future that resolves to whether *path* exists remotely.

- **`remove(path)`** — Handles remove.

- **`rename(source, target)`** — Handles rename.

- **`upload(source, destination)`** — Handles upload.

- **`upload_directory(source, destination)`** — Handles upload directory.

### Class: `FileManagerWindow`

- **`__init__(application, host, username, port=22, initial_path='~', nickname=None, connection=None, connection_manager=None, ssh_config=None)`** — Handles init.

- **`_apply_pending_highlight(pane)`** — Handles apply pending highlight.

- **`_attach_refresh(future, refresh_remote=None, refresh_local_path=None, highlight_name=None)`** — Handles attach refresh.

- **`_check_file_conflicts(files_to_transfer, operation_type, callback)`** — Check for file conflicts and show resolution dialog if needed.

- **`_clear_clipboard()`** — Handles clear clipboard.

- **`_clear_progress_toast()`** — Clear the progress dialog safely.

- **`_compute_effective_split_width()`** — Determine the appropriate width to use when sizing the split view.

- **`_copy_remote_directory(sftp, source_path, destination_path)`** — Handles copy remote directory.

- **`_copy_remote_file(sftp, source_path, destination_path)`** — Handles copy remote file.

- **`_ensure_remote_directory(sftp, path)`** — Handles ensure remote directory.

- **`_force_refresh_pane(pane, highlight_name=None)`** — Force refresh a pane by directly calling listdir and updating UI

- **`_is_remote_descendant(source_path, destination_path)`** — Checks whether remote descendant.

- **`_load_local(path)`** — Load local directory contents into the left pane.

- **`_normalize_local_path(path)`** — Handles normalize local path.

- **`_on_connected(*_args)`** — Handles connected.

- **`_on_connection_error(_manager, message)`** — Handle connection error with toast.

- **`_on_content_size_allocate(_widget, allocation)`** — Adjust split position based on the actual allocated width of the content.

- **`_on_directory_loaded(_manager, path, entries)`** — Handles directory loaded.

- **`_on_local_pane_toggle(toggle_button)`** — Handle local pane toggle button.

- **`_on_operation_error(_manager, message)`** — Handle operation error with toast.

- **`_on_panes_size_changed(panes, pspec)`** — Handle panes widget size changes to maintain proportional split.

- **`_on_path_changed(pane, path, user_data=None)`** — Handles path changed.

- **`_on_progress(_manager, fraction, message)`** — Handles progress.

- **`_on_request_operation(pane, action, payload, user_data=None)`** — Handles request operation.

- **`_on_window_resize(window, pspec)`** — Maintain proportional paned split when window is resized following GNOME HIG

- **`_perform_local_clipboard_operation(entries, source_dir, destination_dir, move)`** — Handles perform local clipboard operation.

- **`_perform_local_to_remote_clipboard_operation(entries, source_dir, destination_dir, move)`** — Handles perform local to remote clipboard operation.

- **`_perform_remote_clipboard_operation(entries, source_dir, destination_dir, move)`** — Handles perform remote clipboard operation.

- **`_refresh_local_listing(path)`** — Refreshes local listing.

- **`_refresh_remote_listing(pane)`** — Legacy method - use _force_refresh_pane instead

- **`_remote_path_exists(sftp, path)`** — Handles remote path exists.

- **`_resolve_local_entry_path(directory, entry)`** — Handles resolve local entry path.

- **`_resolve_remote_entry_path(directory, entry)`** — Handles resolve remote entry path.

- **`_restore_flatpak_folder()`** — Restore Flatpak folder access after window initialization is complete.

- **`_schedule_local_move_cleanup(future, source_path, source_dir)`** — Handles schedule local move cleanup.

- **`_schedule_remote_move_cleanup(future, source_path, pane)`** — Handles schedule remote move cleanup.

- **`_set_initial_split_position()`** — Set the initial proportional split position after the widget is realized.

- **`_show_progress(fraction, message)`** — Update progress dialog if active.

- **`_show_progress_dialog(operation_type, filename, future)`** — Show and manage the progress dialog for a file operation.

- **`_update_paste_targets()`** — Updates paste targets.

- **`_update_split_position(width=None)`** — Update the split position, preserving user adjustments where possible.

- **`detach_for_embedding(parent=None)`** — Detach the window content for embedding in another container.

- **`enable_embedding_mode()`** — Adjust the window chrome for embedded usage.

### Class: `FilePane`

- **`__init__(label)`** — Handles init.

- **`_add_context_controller(widget)`** — Adds context controller.

- **`_apply_entry_filter(preserve_selection)`** — Handles apply entry filter.

- **`_attach_shortcuts(view)`** — Handles attach shortcuts.

- **`_build_properties_details(entry)`** — Builds properties details.

- **`_create_context_menu_model()`** — Create context menu model based on current selection state.

- **`_create_menu_model()`** — Creates menu model.

- **`_current_time()`** — Handles current time.

- **`_dialog_dismissed(error)`** — Handles dialog dismissed.

- **`_emit_entry_operation(action)`** — Handles emit entry operation.

- **`_emit_paste_operation(force_move=False)`** — Handles emit paste operation.

- **`_find_prefix_match(prefix, start_index)`** — Handles find prefix match.

- **`_format_size(size_bytes)`** — Handles format size.

- **`_get_file_manager_window()`** — Return the controlling FileManagerWindow if available.

- **`_get_primary_selection_index()`** — Returns primary selection index.

- **`_get_selected_indices()`** — Returns selected indices.

- **`_handle_download_from_drag(source_path, entry)`** — Handle download operation from drag and drop.

- **`_handle_upload_from_drag(source_path, entry)`** — Handle upload operation from drag and drop.

- **`_hide_request_access_button()`** — Hide the Request Access button after access has been granted.

- **`_navigate_to_entry(position)`** — Handles navigate to entry.

- **`_on_back_clicked(_button)`** — Handles back clicked.

- **`_on_download_clicked(_button)`** — Handles download clicked.

- **`_on_drag_begin(drag_source, drag)`** — Called when drag operation begins - set drag icon.

- **`_on_drag_end(drag_source, drag, delete_data)`** — Called when drag operation ends.

- **`_on_drag_prepare(drag_source, x, y)`** — Prepare drag data when drag operation starts.

- **`_on_drop_enter(drop_target, x, y)`** — Called when drag enters drop target.

- **`_on_drop_leave(drop_target)`** — Called when drag leaves drop target.

- **`_on_drop_string(drop_target, value, x, y)`** — Handle dropped files from string data.

- **`_on_grid_activate(_grid_view, position)`** — Handles grid activate.

- **`_on_grid_bind(factory, item)`** — Handles grid bind.

- **`_on_grid_cell_pressed(gesture, n_press, _x, _y, button)`** — Handles grid cell pressed.

- **`_on_grid_setup(factory, item)`** — Handles grid setup.

- **`_on_grid_unbind(factory, item)`** — Handles grid unbind.

- **`_on_list_activate(_list_view, position)`** — Handles list activate.

- **`_on_list_bind(factory, item)`** — Handles list bind.

- **`_on_list_setup(factory, item)`** — Handles list setup.

- **`_on_list_unbind(factory, item)`** — Handles list unbind.

- **`_on_menu_download()`** — Handles menu download.

- **`_on_menu_properties()`** — Handles menu properties.

- **`_on_menu_upload()`** — Handles menu upload.

- **`_on_path_entry(entry)`** — Handles path entry.

- **`_on_refresh_clicked(_button)`** — Handles refresh clicked.

- **`_on_request_access_clicked()`** — Handle Request Access button click in Flatpak environment.

- **`_on_selection_changed(model, position, n_items)`** — Handles selection changed.

- **`_on_sort_by(sort_key)`** — Handle sort by selection from menu.

- **`_on_sort_direction(descending)`** — Handle sort direction selection from menu.

- **`_on_toolbar_show_hidden_toggled(_toolbar, show_hidden)`** — Handles toolbar show hidden toggled.

- **`_on_typeahead_key_pressed(_controller, keyval, _keycode, state)`** — Handles typeahead key pressed.

- **`_on_up_clicked(_button)`** — Handles up clicked.

- **`_on_upload_clicked(_button)`** — Handles upload clicked.

- **`_on_view_toggle(toolbar, view_name)`** — Handles view toggle.

- **`_refresh_sorted_entries(preserve_selection)`** — Refreshes sorted entries.

- **`_scroll_to_position(position)`** — Handles scroll to position.

- **`_set_current_pathbar_text(path)`** — Set the path bar text with human-friendly display formatting.

- **`_setup_sorting_actions()`** — Set up sorting actions for the split button menu.

- **`_shortcut_delete()`** — Handles shortcut delete.

- **`_shortcut_focus_path_entry()`** — Handles shortcut focus path entry.

- **`_shortcut_operation(action, force_move=False)`** — Handles shortcut operation.

- **`_shortcut_refresh()`** — Handles shortcut refresh.

- **`_show_context_menu(widget, x, y)`** — Shows context menu.

- **`_show_fallback_properties_dialog(entry, details, window)`** — Fallback to simple properties dialog if modern dialog fails.

- **`_show_folder_picker()`** — Show a portal-aware folder picker for Flatpak with persistent access.

- **`_show_properties_dialog(entry, details)`** — Show modern properties dialog.

- **`_sort_entries(entries)`** — Handles sort entries.

- **`_update_grid_selection_for_press(position, gesture)`** — Updates grid selection for press.

- **`_update_menu_state()`** — Updates menu state.

- **`_update_selection_for_menu(widget, x, y)`** — Updates selection for menu.

- **`_update_sort_direction_states()`** — Update the radio button states for sort direction.

- **`_update_view_button_icon()`** — Update the split button icon based on current view mode.

- **`dismiss_toasts()`** — Dismiss all toasts from the overlay.

- **`get_selected_entries()`** — Returns selected entries.

- **`get_selected_entry()`** — Returns selected entry.

- **`highlight_entry(name)`** — Handles highlight entry.

- **`pop_history()`** — Handles pop history.

- **`push_history(path)`** — Handles push history.

- **`restore_persisted_folder()`** — Restore access to a previously granted folder on app launch (Flatpak only).

- **`set_can_paste(can_paste)`** — Sets can paste.

- **`set_file_manager_window(window)`** — Associate this pane with its owning file manager window.

- **`set_partner_pane(partner)`** — Sets partner pane.

- **`set_show_hidden(show_hidden, preserve_selection=True)`** — Update the hidden file visibility state and refresh entries.

- **`show_entries(path, entries)`** — Shows entries.

- **`show_toast(text, timeout=-1)`** — Show a toast message safely.

### Class: `PaneControls`

- **`__init__()`** — Handles init.

### Class: `PaneToolbar`

- **`__init__()`** — Handles init.

- **`_create_sort_split_button()`** — Creates sort split button.

- **`_on_show_hidden_toggled(button)`** — Handles show hidden toggled.

- **`_on_view_toggle_clicked(*_)`** — Handles view toggle clicked.

- **`_update_show_hidden_icon(show_hidden)`** — Updates show hidden icon.

- **`get_header_bar()`** — Get the actual header bar for toolbar view.

- **`set_show_hidden_state(show_hidden)`** — Sets show hidden state.

### Class: `PathEntry`

- **`__init__()`** — Handles init.

### Class: `PropertiesDialog`

- **`__init__(entry, current_path, parent)`** — Handles init.

- **`_build_dialog()`** — Build the Nautilus-style properties dialog content.

- **`_calculate_folder_size(path)`** — Recursively calculates the size of a folder.

- **`_create_created_row()`** — Create the created date row (if available).

- **`_create_header_block()`** — Create the header block with icon, name, and summary.

- **`_create_modified_row()`** — Create the modified date row.

- **`_create_parent_folder_row()`** — Create the parent folder row.

- **`_create_permissions_row()`** — Create the permissions row.

- **`_create_size_row()`** — Create the size row.

- **`_is_remote_file()`** — Check if this is a remote file (from SFTP).

- **`_on_open_parent(*_)`** — Open parent directory in system file manager.

- **`_start_folder_size_calculation()`** — Start calculating folder size in background thread.

- **`_update_folder_size_ui(total_size)`** — Updates the size row with the final folder size.

### Class: `SFTPProgressDialog`

- **`__init__(parent=None, operation_type='transfer')`** — Handles init.

- **`_build_ui()`** — Build the modern GNOME HIG-compliant UI

- **`_format_size(size_bytes)`** — Format file size for display

- **`_format_time(seconds)`** — Format time remaining for display

- **`_increment_file_count_ui()`** — Update file counter (must be called from main thread)

- **`_on_response(dialog, response)`** — Handle dialog response

- **`_show_completion_ui(success, error_message)`** — Update UI to show completion state

- **`_update_progress_ui(fraction, message, current_file)`** — Update UI elements (must be called from main thread)

- **`increment_file_count()`** — Increment completed file counter

- **`set_future(future)`** — Set the current operation future for cancellation

- **`set_operation_details(total_files, filename=None)`** — Set the operation details

- **`set_total_bytes(total_bytes)`** — Set the total bytes for the operation

- **`show_completion(success=True, error_message=None)`** — Show completion state

- **`update_progress(fraction, message=None, current_file=None)`** — Update progress bar and status

### Class: `_MainThreadDispatcher`

- **`dispatch(func, *args, **kwargs)`** — Handles dispatch.



## Module: `sshpilot.groups`

### Class: `GroupManager`

- **`__init__(config)`** — Handles init.

- **`_load_groups()`** — Load groups from configuration

- **`_save_groups()`** — Save groups to configuration

- **`_update_group_orders(groups_list, parent_id)`** — Update the order field for groups at a given level

- **`create_group(name, parent_id=None, color=None)`** — Create a new group and return its ID

- **`delete_group(group_id)`** — Delete a group and move its contents to parent or root

- **`get_all_groups()`** — Get all groups as a flat list for selection dialogs

- **`get_connection_group(connection_nickname)`** — Get the group ID for a connection

- **`get_group_hierarchy()`** — Get the complete group hierarchy

- **`group_name_exists(name)`** — Check if a group name already exists

- **`move_connection(connection_nickname, target_group_id=None)`** — Move a connection to a different group

- **`rename_connection(old_nickname, new_nickname)`** — Rename a connection while preserving its group membership.

- **`reorder_connection_in_group(connection_nickname, target_connection_nickname, position)`** — Reorder a connection within the same group relative to another connection

- **`reorder_group(source_group_id, target_group_id, position)`** — Reorder a group relative to another group at the same level

- **`set_group_color(group_id, color)`** — Update a group's color and persist the change.

- **`set_group_expanded(group_id, expanded)`** — Set whether a group is expanded



## Module: `sshpilot.key_manager`

### Class: `KeyManager`

- **`__init__(ssh_dir=None)`** — Handles init.

- **`_is_private_key(file_path)`** — Return True if the path looks like a private SSH key.

- **`discover_keys()`** — Discover known SSH keys within the configured SSH directory.

- **`generate_key(key_name, key_type='ed25519', key_size=3072, comment=None, passphrase=None)`** — Single, unified generator using `ssh-keygen`.

### Class: `SSHKey`

- **`__init__(private_path)`** — Handles init.

- **`__str__()`** — Handles str.



## Module: `sshpilot.key_utils`

### Functions

- **`_is_private_key(file_path, cache=None, skipped_filenames=None)`** — Return ``True`` when *file_path* looks like a private SSH key.



## Module: `sshpilot.known_hosts_editor`

### Class: `KnownHostsEditorWindow`

- **`__init__(parent, connection_manager, on_saved=None)`** — Handles init.

- **`_display_entries(entries)`** — Display the given entries in the listbox.

- **`_load_entries()`** — Load known_hosts entries into the listbox.

- **`_on_remove_clicked(_btn, row)`** — Handles remove clicked.

- **`_on_save_clicked(_btn)`** — Handles save clicked.

- **`_on_search_changed(search_entry)`** — Handle search text changes.



## Module: `sshpilot.main`

### Functions

- **`load_resources()`** — Loads resources.

- **`main()`** — Main entry point

### Class: `SshPilotApplication`

- **`__init__(verbose=False, isolated=False, native_connect=False)`** — Handles init.

- **`_apply_shortcut_for_action(name)`** — Handles apply shortcut for action.

- **`_on_config_setting_changed(_config, key, value)`** — Handles config setting changed.

- **`_refresh_window_accelerators()`** — Notify windows to refresh accelerator state.

- **`_update_accelerators_enabled_flag()`** — Update the exposed accelerator enabled flag considering focus state.

- **`apply_color_overrides(config)`** — Apply color overrides to the application

- **`apply_shortcut_overrides()`** — Reapply all shortcut overrides to the registered actions.

- **`create_action(name, callback, shortcuts=None)`** — Create a GAction with optional keyboard shortcuts

- **`do_activate()`** — Called when the application is activated

- **`get_registered_action_order()`** — Return the order in which actions were registered.

- **`get_registered_shortcut_defaults()`** — Return a mapping of action names to their default accelerators.

- **`on_about(action, param)`** — Handle about dialog action

- **`on_activate(app)`** — Handle application activation

- **`on_broadcast_command(action, param)`** — Handle broadcast command action (Ctrl/⌘+Shift+B)

- **`on_edit_ssh_config(action=None, param=None)`** — Handle SSH config editor action.

- **`on_help(action, param)`** — Handle help action

- **`on_local_terminal(action, param)`** — Handle local terminal action

- **`on_manage_files(action, param)`** — Handle manage files shortcut.

- **`on_new_connection(action, param)`** — Handle new connection action

- **`on_new_key(action, param)`** — Handle new SSH key action

- **`on_open_new_connection_tab(action, param)`** — Handle open new connection tab action (Ctrl/⌘+Alt+N)

- **`on_preferences(action, param)`** — Handle preferences action

- **`on_quick_connect(action, param)`** — Open quick connect dialog

- **`on_quit_action(action=None, param=None)`** — Handle Ctrl (⌘ on macOS)+Q by routing through the application quit path.

- **`on_search(action, param)`** — Handle search action

- **`on_shortcuts(action, param)`** — Handle keyboard shortcuts overlay action

- **`on_shutdown(app)`** — Clean up all resources when application is shutting down

- **`on_tab_close(action, param)`** — Close the currently selected tab

- **`on_tab_next(action, param)`** — Switch to next tab

- **`on_tab_overview(action, param)`** — Toggle tab overview

- **`on_tab_prev(action, param)`** — Switch to previous tab

- **`on_terminal_search(action, param)`** — Toggle the search overlay for the active terminal.

- **`on_toggle_list(action, param)`** — Handle toggle list focus action

- **`quit()`** — Request application shutdown, showing confirmation if needed.

- **`setup_logging()`** — Set up logging configuration



## Module: `sshpilot.platform_utils`

### Functions

- **`get_config_dir()`** — Return the per-user configuration directory for sshPilot.

- **`get_data_dir()`** — Return the per-user data directory for sshPilot.

- **`get_ssh_dir()`** — Return the user's SSH directory.

- **`is_flatpak()`** — Return True if running inside a Flatpak sandbox.

- **`is_macos()`** — Return True if running on macOS.



## Module: `sshpilot.port_utils`

### Functions

- **`check_port_conflicts(ports, address='127.0.0.1')`** — Check for port conflicts

- **`find_available_port(preferred_port, address='127.0.0.1')`** — Find an available port near the preferred port

- **`get_listening_ports()`** — Get all listening ports

- **`get_port_checker()`** — Get the global PortChecker instance

- **`is_port_available(port, address='127.0.0.1')`** — Check if a port is available

### Class: `PortChecker`

- **`__init__()`** — Handles init.

- **`_find_process_by_inode(inode)`** — Find process PID and name by socket inode

- **`_get_ports_via_netstat()`** — Fallback method using netstat command or /proc/net/tcp parsing

- **`_get_ports_via_proc()`** — Parse /proc/net/tcp and /proc/net/tcp6 for listening ports

- **`_get_process_name(pid)`** — Get process name for a given PID using multiple methods

- **`find_available_port(preferred_port, address='127.0.0.1', port_range=(1024, 65535))`** — Find an available port, starting with the preferred port

- **`get_listening_ports(refresh=False)`** — Get all currently listening ports

- **`get_port_conflicts(ports_to_check, address='127.0.0.1')`** — Check for conflicts with a list of ports

- **`is_port_available(port, address='127.0.0.1', protocol='tcp')`** — Check if a port is available for binding

### Class: `PortInfo`

- **`__init__(port, protocol='tcp', pid=None, process_name=None, address='0.0.0.0')`** — Handles init.

- **`__str__()`** — Handles str.

- **`to_dict()`** — Handles to dict.



## Module: `sshpilot.preferences`

### Functions

- **`_install_group_display_preview_css()`** — Handles install group display preview css.

- **`macos_third_party_terminal_available()`** — Check if a third-party terminal is available on macOS.

- **`should_hide_external_terminal_options()`** — Check if external terminal options should be hidden.

- **`should_hide_file_manager_options()`** — Check if file manager options should be hidden.

- **`should_show_force_internal_file_manager_toggle()`** — Return True when the built-in toggle for forcing the internal manager should be shown.

### Class: `MonospaceFontDialog`

- **`__init__(parent=None, current_font='Monospace 12')`** — Handles init.

- **`filter_fonts(model, iter, data)`** — Handles filter fonts.

- **`on_cancel(button)`** — Handles cancel.

- **`on_search_changed(entry)`** — Handles search changed.

- **`on_select(button)`** — Handles select.

- **`on_selection_changed(selection)`** — Handles selection changed.

- **`on_size_changed(spin)`** — Handles size changed.

- **`populate_fonts()`** — Handles populate fonts.

- **`select_current_font()`** — Handles select current font.

- **`set_callback(callback)`** — Set callback function that receives the selected font string

- **`setup_ui()`** — Handles setup ui.

- **`update_preview(font_desc)`** — Updates preview.

### Class: `PreferencesWindow`

- **`__init__(parent_window, config)`** — Handles init.

- **`_apply_default_advanced_settings()`** — Restore advanced SSH settings to defaults and update the UI.

- **`_collect_supported_encodings()`** — Handles collect supported encodings.

- **`_create_group_display_preview(mode, title)`** — Create a small sample widget that illustrates the layout mode.

- **`_handle_invalid_encoding_selection(requested, fallback)`** — Handles invalid encoding selection.

- **`_initialize_encoding_selector(appearance_group)`** — Handles initialize encoding selector.

- **`_is_internal_file_manager_enabled()`** — Return ``True`` when the application uses the built-in file manager.

- **`_on_config_setting_changed(_config, key, value)`** — Handles config setting changed.

- **`_on_destroy(*_args)`** — Handles destroy.

- **`_populate_terminal_dropdown()`** — Populate the terminal dropdown with available terminals

- **`_set_color_button(button, row, setting_name, default_rgba, default_subtitle)`** — Sets color button.

- **`_set_connection_mode_switches(native_active)`** — Synchronize native/legacy connection switches without recursion.

- **`_set_shortcut_controls_enabled(enabled)`** — Sets shortcut controls enabled.

- **`_set_terminal_dropdown_selection(terminal_name)`** — Set the dropdown selection to the specified terminal

- **`_show_toast(message)`** — Shows toast.

- **`_sync_encoding_row_selection(encoding, notify_user=False)`** — Handles sync encoding row selection.

- **`_sync_group_color_display_row(value)`** — Handles sync group color display row.

- **`_sync_group_display_toggle_group(value)`** — Handles sync group display toggle group.

- **`_sync_group_tab_color_switch(value)`** — Handles sync group tab color switch.

- **`_sync_group_terminal_color_switch(value)`** — Handles sync group terminal color switch.

- **`_sync_use_group_color_in_tab(value)`** — Handles sync use group color in tab.

- **`_sync_use_group_color_in_terminal(value)`** — Handles sync use group color in terminal.

- **`_trigger_sidebar_refresh()`** — Handles trigger sidebar refresh.

- **`_trigger_terminal_style_refresh()`** — Handles trigger terminal style refresh.

- **`_update_encoding_config_if_needed(target_code)`** — Updates encoding config if needed.

- **`_update_external_file_manager_row()`** — Sync the external window preference with the current availability.

- **`_update_group_display_preview(active_mode)`** — Updates group display preview.

- **`_update_header_title(page_title=None)`** — Update the header and window title to reflect the active page.

- **`_update_operation_mode_styles()`** — Visually de-emphasize the inactive operation mode

- **`add_page_to_layout(title, icon_name, page)`** — Add a page to the custom layout

- **`apply_color_overrides()`** — Apply color overrides to the application

- **`apply_color_scheme_to_terminals(scheme_key)`** — Apply color scheme to all active terminal widgets

- **`apply_font_to_terminals(font_string)`** — Apply font to all active terminal widgets

- **`draw_color_preview(drawing_area, cr, width, height)`** — Draw a preview of the selected color scheme

- **`get_color_scheme_colors(scheme_key)`** — Get colors for a specific color scheme

- **`get_reverse_theme_mapping()`** — Get mapping from config keys to display names

- **`get_theme_name_mapping()`** — Get mapping between display names and config keys

- **`hex_to_rgba(hex_color)`** — Convert hex color to RGBA values (0-1 range)

- **`on_accent_color_changed(color_button)`** — Handle accent color change

- **`on_close_request(*args)`** — Persist settings when the preferences window closes

- **`on_color_scheme_changed(combo_row, param)`** — Handle terminal color scheme change

- **`on_confirm_disconnect_changed(switch, *args)`** — Handle confirm disconnect setting change

- **`on_custom_terminal_path_changed(entry, *args)`** — Handle custom terminal path entry change

- **`on_encoding_selection_changed(combo_row, _param)`** — Handles encoding selection changed.

- **`on_font_button_clicked(button)`** — Handle font button click

- **`on_force_internal_file_manager_changed(switch, *args)`** — Persist the preference for forcing the in-app file manager.

- **`on_group_color_display_changed(combo_row, _param)`** — Persist sidebar group color display preference changes.

- **`on_group_row_display_changed(toggle_group, _param)`** — Persist sidebar group display layout preference.

- **`on_legacy_connection_mode_toggled(switch, *args)`** — Ensure legacy mode toggle keeps native switch in sync.

- **`on_native_connection_mode_toggled(switch, *args)`** — Ensure native mode toggle keeps legacy switch in sync.

- **`on_open_file_manager_externally_changed(switch, *args)`** — Persist whether the file manager should open in a separate window.

- **`on_operation_mode_toggled(button)`** — Handle switching between default and isolated SSH modes

- **`on_pass_through_mode_toggled(switch, _pspec)`** — Persist changes to the terminal pass-through preference.

- **`on_reset_advanced_ssh(*args)`** — Reset only advanced SSH keys to defaults and update UI.

- **`on_reset_colors_clicked(button)`** — Reset color overrides to default

- **`on_sidebar_row_selected(listbox, row)`** — Handle sidebar row selection

- **`on_startup_behavior_changed(radio_button, *args)`** — Handle startup behavior radio button change

- **`on_terminal_choice_changed(radio_button, *args)`** — Handle terminal choice radio button change

- **`on_terminal_dropdown_changed(dropdown, *args)`** — Handle terminal dropdown selection change

- **`on_theme_changed(combo_row, param)`** — Handle theme selection change

- **`on_use_group_color_in_tab_toggled(switch_row, _param)`** — Handles use group color in tab toggled.

- **`on_use_group_color_in_terminal_toggled(switch_row, _param)`** — Handles use group color in terminal toggled.

- **`on_view_shortcuts_clicked(_button)`** — Open the standalone shortcuts window from preferences.

- **`refresh_color_buttons()`** — Update color button appearance to reflect settings

- **`remove_color_override_provider()`** — Remove color override CSS provider

- **`save_advanced_ssh_settings()`** — Persist advanced SSH settings from the preferences UI

- **`setup_navigation_layout()`** — Configure split view layout mirroring GNOME Settings.

- **`setup_preferences()`** — Set up preferences UI with current values

### Class: `_GroupDisplayToggleFallback`

- **`__init__(buttons, default='fullwidth')`** — Handles init.

- **`_emit_changed()`** — Handles emit changed.

- **`_on_button_toggled(button, name)`** — Handles button toggled.

- **`connect(callback)`** — Handles connect.

- **`get_active_name()`** — Returns active name.

- **`set_active_name(name)`** — Sets active name.



## Module: `sshpilot.scp_utils`

### Functions

- **`_extract_host(target)`** — Handles extract host.

- **`_normalize_remote_sources(target, sources)`** — Handles normalize remote sources.

- **`_strip_brackets(value)`** — Handles strip brackets.

- **`assemble_scp_transfer_args(target, sources, destination, direction)`** — Return normalized scp sources and destination arguments for a transfer.



## Module: `sshpilot.search_utils`

### Functions

- **`connection_matches(connection, query)`** — Return True if connection matches the search query.



## Module: `sshpilot.sftp_utils`

### Functions

- **`_create_mount_progress_dialog(user, host)`** — Create a progress dialog for SFTP mount operation

- **`_find_gvfs_mount_point(user, host)`** — Find the actual GVFS mount point for the SFTP connection

- **`_gvfs_supports_sftp()`** — Heuristic detection of whether GVFS/GIO can handle SFTP mounts.

- **`_launch_terminal_sftp(user, host, port, error_callback=None)`** — Launch terminal-based SFTP client as last resort

- **`_mount_and_open_sftp(uri, user, host, error_callback=None, progress_dialog=None)`** — Mount SFTP location and open in file manager

- **`_mount_and_open_sftp_native(uri, user, host, error_callback, parent_window=None)`** — Original native GVFS mounting method

- **`_open_sftp_flatpak_compatible(uri, user, host, port, error_callback, progress_dialog=None, parent_window=None)`** — Open SFTP using Flatpak-compatible methods with proper portal usage

- **`_open_sftp_native(uri, user, host, error_callback, parent_window=None)`** — Native installation SFTP opening with GVFS

- **`_should_use_in_app_file_manager()`** — Return ``True`` when the libadwaita based file manager should be used.

- **`_show_manual_connection_dialog(user, host, port, uri)`** — Show dialog with manual connection instructions

- **`_try_alternative_approaches(uri, user, host)`** — Try alternative approaches when direct URI opening fails

- **`_try_command_line_mount(user, host, port, error_callback=None)`** — Try mounting via command line utilities

- **`_try_dbus_gvfs_mount(uri, user, host)`** — Try to mount GVFS via gio command and open directly

- **`_try_external_file_managers(uri, user, host)`** — Try launching external file managers that handle SFTP

- **`_try_flatpak_compatible_mount(uri, user, host, port, error_callback=None)`** — Try various methods to open SFTP in Flatpak environment

- **`_try_host_gvfs_access(uri, user, host, port)`** — Try accessing GVFS mounts on the host system

- **`_try_portal_file_access(uri, user, host)`** — Try to access SFTP location via XDG Desktop Portal - skip file chooser

- **`_try_specific_file_managers_with_uri(uri)`** — Try specific file managers that handle SFTP URIs properly

- **`_verify_ssh_connection(user, host, port)`** — Verify SSH connection without full mount

- **`_verify_ssh_connection_async(user, host, port, callback)`** — Verify SSH connection on a background thread and invoke callback with the result

- **`open_remote_in_file_manager(user, host, port=None, path=None, error_callback=None, parent_window=None, connection=None, connection_manager=None, ssh_config=None)`** — Open remote server in file manager using SFTP URI with asynchronous verification

- **`should_use_in_app_file_manager()`** — Return ``True`` when the libadwaita based file manager should be used.

### Class: `MountProgressDialog`

- **`__init__(user, host, parent_window=None)`** — Handles init.

- **`_on_cancel(button)`** — Cancel mount operation

- **`_update_progress_simulation()`** — Simulate mounting progress

- **`close(widget=None)`** — Close the dialog

- **`show_error(error_text)`** — Show error state

- **`start_progress_updates()`** — Start simulated progress updates

- **`update_progress(fraction, text)`** — Update progress bar and status

### Class: `SftpConnectionDialog`

- **`__init__(user, host, port, uri)`** — Handles init.

- **`_copy_uri()`** — Copy URI to clipboard

- **`_create_option_box(title, description, icon_name, callback)`** — Create an option box with icon, text, and button

- **`_open_terminal()`** — Open SFTP in terminal

- **`_try_file_manager()`** — Try opening in file manager



## Module: `sshpilot.shortcut_editor`

### Functions

- **`_get_action_label(name)`** — Returns action label.

### Class: `PreferencesPageBase`

- **`__init__(*args, **kwargs)`** — Handles init.

- **`add(child)`** — Handles add.

- **`add_css_class(*_args)`** — Adds css class.

- **`append(child)`** — Handles append.

- **`set_icon_name(*_args)`** — Sets icon name.

- **`set_title(*_args)`** — Sets title.

### Class: `ShortcutEditorWindow`

- **`__init__(parent_window)`** — Handles init.

- **`_on_close_request(*_args)`** — Handles close request.

- **`_on_pass_through_switch_toggled(switch, _pspec)`** — Handles pass through switch toggled.

### Class: `ShortcutsPreferencesPage`

- **`__init__(parent_widget, app=None, config=None, owner_window=None)`** — Handles init.

- **`_add_group_widget(group)`** — Adds group widget.

- **`_apply_pass_through_state_to_row(action_name)`** — Handles apply pass through state to row.

- **`_apply_shortcuts()`** — Handles apply shortcuts.

- **`_attempt_set_override(action_name, accelerators)`** — Handles attempt set override.

- **`_build_groups()`** — Builds groups.

- **`_collect_actions()`** — Handles collect actions.

- **`_create_pass_through_notice_widget()`** — Creates pass through notice widget.

- **`_find_conflict(action_name, accelerator)`** — Handles find conflict.

- **`_format_accelerators(accelerators)`** — Handles format accelerators.

- **`_get_effective_shortcuts(action_name)`** — Returns effective shortcuts.

- **`_on_assign_clicked(_button, action_name)`** — Handles assign clicked.

- **`_on_reset_clicked(_button, action_name)`** — Handles reset clicked.

- **`_on_switch_toggled(switch, _pspec, action_name)`** — Handles switch toggled.

- **`_show_conflict_dialog(conflict_action)`** — Shows conflict dialog.

- **`_update_row_display(action_name)`** — Updates row display.

- **`create_editor_widget()`** — Creates editor widget.

- **`flush_changes()`** — Flush pending overrides to the application.

- **`get_pass_through_notice_widget()`** — Returns pass through notice widget.

- **`get_shortcuts_container()`** — Returns shortcuts container.

- **`iter_groups()`** — Yield the preference groups managed by this page.

- **`set_pass_through_enabled(enabled)`** — Sets pass through enabled.

### Class: `_ShortcutCaptureDialog`

- **`__init__(parent, on_selected)`** — Handles init.

- **`_on_key_pressed(_controller, keyval, keycode, state)`** — Handles key pressed.



## Module: `sshpilot.shortcut_utils`

### Functions

- **`get_primary_modifier_label()`** — Return the label for the primary modifier key.



## Module: `sshpilot.shutdown`

### Functions

- **`_disconnect_terminal_safely(terminal)`** — Safely disconnect a terminal.

- **`_hide_cleanup_progress(window)`** — Hide cleanup progress dialog.

- **`_perform_cleanup_and_quit(window, connections_to_disconnect)`** — Disconnect terminals with UI progress, then quit. Processes one terminal per call.

- **`_show_cleanup_progress(window, total_connections)`** — Show cleanup progress dialog.

- **`_update_cleanup_progress(window, completed, total)`** — Update cleanup progress.

- **`cleanup_and_quit(window)`** — Clean up all connections and quit.

- **`hide_reconnecting_message(window)`** — Hide the reconnection progress dialog if shown.

- **`show_reconnecting_message(window, connection)`** — Show a small modal indicating reconnection is in progress.



## Module: `sshpilot.sidebar`

### Functions

- **`_calculate_autoscroll_velocity(distance, margin, max_velocity)`** — Scale the autoscroll velocity based on how deep the pointer is in the margin.

- **`_clear_drop_indicator(window)`** — Handles clear drop indicator.

- **`_connection_autoscroll_step(window)`** — Handles connection autoscroll step.

- **`_create_ungrouped_area(window)`** — Creates ungrouped area.

- **`_fill_rgba(rgba)`** — Handles fill rgba.

- **`_get_color_class(rgba)`** — Returns color class.

- **`_get_color_display_mode(config)`** — Returns color display mode.

- **`_get_target_group_at_position(window, x, y)`** — Returns target group at position.

- **`_hide_ungrouped_area(window)`** — Handles hide ungrouped area.

- **`_install_sidebar_color_css()`** — Handles install sidebar color css.

- **`_move_group(window, group_id, target_parent_id)`** — Handles move group.

- **`_on_connection_list_drop(window, target, value, x, y)`** — Handles connection list drop.

- **`_on_connection_list_leave(window, target)`** — Handles connection list leave.

- **`_on_connection_list_motion(window, target, x, y)`** — Handles connection list motion.

- **`_parse_color(value)`** — Handles parse color.

- **`_set_tint_card_color(row, rgba)`** — Sets tint card color.

- **`_show_drop_indicator(window, row, position)`** — Shows drop indicator.

- **`_show_drop_indicator_on_group(window, row)`** — Show a special indicator when dropping a connection onto a group (adds to group)

- **`_show_ungrouped_area(window)`** — Shows ungrouped area.

- **`_start_connection_autoscroll(window, velocity)`** — Ensure an autoscroll timeout is active with the requested velocity.

- **`_stop_connection_autoscroll(window)`** — Cancel any active autoscroll timeout and reset state.

- **`_update_connection_autoscroll(window, y)`** — Update autoscroll velocity based on pointer position within the viewport.

- **`build_sidebar(window)`** — Set up sidebar behaviour for ``window``.

- **`setup_connection_list_dnd(window)`** — Set up drag and drop for the window's connection list.

### Class: `ConnectionRow`

- **`__init__(connection, group_manager, config)`** — Handles init.

- **`_apply_group_color_style()`** — Handles apply group color style.

- **`_apply_group_display_mode()`** — Handles apply group display mode.

- **`_apply_host_label_text(include_port=None)`** — Handles apply host label text.

- **`_get_group_display_mode()`** — Returns group display mode.

- **`_install_pf_css()`** — Handles install pf css.

- **`_on_drag_begin(source, drag)`** — Handles drag begin.

- **`_on_drag_end(source, drag, delete_data)`** — Handles drag end.

- **`_on_drag_prepare(source, x, y)`** — Handles drag prepare.

- **`_resolve_group_color()`** — Handles resolve group color.

- **`_setup_drag_source()`** — Handles setup drag source.

- **`_update_forwarding_indicators()`** — Updates forwarding indicators.

- **`apply_hide_hosts(hide)`** — Handles apply hide hosts.

- **`hide_drop_indicators()`** — Hide all drop indicator lines

- **`refresh_group_display_mode(new_mode=None)`** — Refresh indentation styling when the preference changes.

- **`set_indentation(level)`** — Set indentation level for grouped connections

- **`show_drop_indicator(top)`** — Show drop indicator line

- **`update_display()`** — Updates display.

- **`update_status()`** — Updates status.

### Class: `DragIndicator`

- **`__init__()`** — Handles init.

- **`do_snapshot(snapshot)`** — Draw the horizontal line

### Class: `GroupRow`

- **`__init__(group_info, group_manager, connections_dict=None)`** — Handles init.

- **`_apply_group_color_style()`** — Handles apply group color style.

- **`_on_double_click(gesture, n_press, x, y)`** — Handles double click.

- **`_on_drag_begin(source, drag)`** — Handles drag begin.

- **`_on_drag_end(source, drag, delete_data)`** — Handles drag end.

- **`_on_drag_prepare(source, x, y)`** — Handles drag prepare.

- **`_on_expand_clicked(button)`** — Handles expand clicked.

- **`_setup_double_click_gesture()`** — Handles setup double click gesture.

- **`_setup_drag_source()`** — Handles setup drag source.

- **`_toggle_expand()`** — Toggles expand.

- **`_update_color_badge(rgba)`** — Updates color badge.

- **`_update_display()`** — Updates display.

- **`hide_drop_indicators()`** — Hide all drop indicator lines

- **`show_drop_indicator(top)`** — Show drop indicator line

- **`show_group_highlight(show)`** — Show/hide group highlight for 'add to group' drop indication



## Module: `sshpilot.ssh_config_editor`

### Class: `SSHConfigEditorWindow`

- **`__init__(parent, connection_manager, on_saved=None)`** — Handles init.

- **`_apply_highlighting()`** — Handles apply highlighting.

- **`_on_buffer_changed(_buffer)`** — Handles buffer changed.

- **`_on_save_clicked(_btn)`** — Handles save clicked.



## Module: `sshpilot.ssh_config_utils`

### Functions

- **`get_effective_ssh_config(host, config_file=None)`** — Return effective SSH options for *host* using ``ssh -G``.

- **`resolve_ssh_config_files(main_path, max_depth=32)`** — Return a list of SSH config files including those referenced by Include.



## Module: `sshpilot.ssh_password_exec`

### Functions

- **`_mk_priv_dir(prefix='sshpilot-pass-')`** — Handles mk priv dir.

- **`_write_once_fifo(path, secret)`** — Handles write once fifo.

- **`run_scp_with_password(host, user, password, sources, destination, direction='upload', port=22, known_hosts_path=None, extra_ssh_opts=None, inherit_env=None, use_publickey=False)`** — Handles run scp with password.

- **`run_ssh_with_password(host, user, password, port=22, argv_tail=None, known_hosts_path=None, extra_ssh_opts=None, inherit_env=None, use_publickey=False)`** — Launch `ssh` using sshpass -f <FIFO> safely.



## Module: `sshpilot.ssh_utils`

### Functions

- **`build_connection_ssh_options(connection, config=None, for_ssh_copy_id=False)`** — Build SSH options that match the exact connection settings used for SSH.

- **`ensure_writable_ssh_home(env)`** — Ensure ssh-copy-id has a writable HOME when running in Flatpak.



## Module: `sshpilot.sshcopyid_window`

### Class: `SshCopyIdWindow`

- **`__init__(parent, connection, key_manager, connection_manager)`** — Handles init.

- **`_do_copy_existing()`** — Handles do copy existing.

- **`_do_generate_and_copy()`** — Handles do generate and copy.

- **`_error(title, body, detail='')`** — Handles error.

- **`_info(title, body)`** — Handles info.

- **`_on_close_clicked(*_)`** — Handles close clicked.

- **`_on_key_type_changed(*_)`** — Handles key type changed.

- **`_on_mode_toggled(*_)`** — Handles mode toggled.

- **`_on_ok_clicked(*_)`** — Handles ok clicked.

- **`_on_pass_toggle(*_)`** — Handles pass toggle.

- **`_reload_existing_keys()`** — Handles reload existing keys.



## Module: `sshpilot.sshpilot_agent`

### Functions

- **`handle_resize_signal(signum, frame)`** — Handle SIGWINCH for terminal resize

- **`main()`** — Main entry point for the agent

### Class: `PTYAgent`

- **`__init__()`** — Handles init.

- **`_send_status(status_type, **payload)`** — Send a structured status message to stderr for the caller.

- **`cleanup()`** — Clean up resources

- **`create_pty()`** — Create a PTY master/slave pair with proper flags.

- **`discover_shell()`** — Discover the user's preferred shell on the host system

- **`io_loop()`** — Main I/O loop: relay data between master PTY and stdin/stdout.

- **`run(rows=24, cols=80, cwd=None)`** — Main entry point: create PTY, spawn shell, run I/O loop

- **`set_pty_size(rows, cols)`** — Set the PTY size

- **`spawn_shell(shell, cwd=None)`** — Spawn the user's shell with proper PTY setup.



## Module: `sshpilot.terminal`

### Class: `SSHProcessManager`

- **`__new__()`** — Handles new.

- **`_cleanup_loop()`** — Background cleanup loop

- **`_cleanup_orphaned_processes()`** — Clean up processes not tracked by active terminals

- **`_start_cleanup_thread()`** — Start background cleanup thread

- **`_terminate_process_by_pid(pid)`** — Terminate a process by PID

- **`cleanup_all()`** — Clean up all managed processes

- **`register_terminal(terminal)`** — Register a terminal for tracking

### Class: `TerminalWidget`

- **`__init__(connection, config, connection_manager, group_color=None)`** — Handles init.

- **`_apply_cursor_and_selection_colors()`** — Handles apply cursor and selection colors.

- **`_apply_pass_through_mode(enabled)`** — Enable or disable custom shortcut handling based on configuration.

- **`_apply_terminal_encoding(encoding_value, update_config_on_fallback=True)`** — Handles apply terminal encoding.

- **`_apply_terminal_encoding_idle(encoding_value)`** — Handles apply terminal encoding idle.

- **`_calculate_luminance(rgba)`** — Handles calculate luminance.

- **`_cleanup_process(pid)`** — Clean up a process by PID

- **`_clear_search_pattern()`** — Clear any active search pattern from the terminal.

- **`_clone_rgba(rgba)`** — Handles clone rgba.

- **`_connect_ssh()`** — Connect to SSH host

- **`_connect_ssh_thread()`** — SSH connection thread: directly spawn SSH and rely on its output for errors.

- **`_contrast_color(rgba)`** — Handles contrast color.

- **`_enable_askpass_log_forwarding(include_existing=False)`** — Start forwarding askpass log lines into the application logger when debug logging is enabled.

- **`_ensure_opaque(rgba)`** — Handles ensure opaque.

- **`_ensure_search_key_controller()`** — Attach the search shortcut controller to the terminal if needed.

- **`_fallback_hide_spinner()`** — Fallback method to hide spinner if spawn completion doesn't fire

- **`_fallback_to_askpass(ssh_cmd, env_list)`** — Fallback when sshpass fails - allow interactive prompting

- **`_get_contrast_color(background)`** — Returns contrast color.

- **`_get_group_color_rgba()`** — Returns group color rgba.

- **`_get_supported_encodings()`** — Returns supported encodings.

- **`_get_terminal_pid()`** — Get the PID of the terminal's child process

- **`_handle_child_exit_cleanup(status)`** — Handle the actual cleanup work for child process exit (called from main thread)

- **`_hide_search_overlay()`** — Hide the search overlay and return focus to the terminal.

- **`_install_shortcuts()`** — Install custom keyboard shortcuts for terminal operations.

- **`_is_local_terminal()`** — Check if this is a local terminal (not SSH)

- **`_is_terminal_idle_pty()`** — Shell-agnostic check using PTY FD and POSIX job control.

- **`_mix_rgba(base, other, ratio)`** — Handles mix rgba.

- **`_mix_with_white(rgba, ratio=0.35)`** — Handles mix with white.

- **`_notify_invalid_encoding(requested, fallback)`** — Handles notify invalid encoding.

- **`_on_agent_spawn_complete(terminal, pid, error, user_data)`** — Callback when agent spawn completes

- **`_on_config_setting_changed(_config, key, value)`** — Handles config setting changed.

- **`_on_connection_established()`** — Handle successful SSH connection

- **`_on_connection_failed(error_message)`** — Handle connection failure (called from main thread)

- **`_on_connection_lost(message=None)`** — Handle SSH connection loss

- **`_on_connection_updated(connection)`** — Called when connection settings are updated

- **`_on_connection_updated_signal(sender, connection)`** — Signal handler for connection-updated signal

- **`_on_destroy(widget)`** — Handle widget destruction

- **`_on_reconnect_clicked(*args)`** — User clicked reconnect on the banner

- **`_on_search_entry_activate(entry)`** — Handle Enter key in the search entry.

- **`_on_search_entry_changed(entry)`** — React to text edits in the search entry.

- **`_on_search_entry_key_pressed(controller, keyval, keycode, state)`** — Handle additional shortcuts while the search entry is focused.

- **`_on_search_entry_stop(entry)`** — Handle stop-search events (Escape or clear button).

- **`_on_search_next(*_args)`** — Navigate to the next search match.

- **`_on_search_previous(*_args)`** — Navigate to the previous search match.

- **`_on_spawn_complete(terminal, pid, error, user_data=None)`** — Called when terminal spawn is complete

- **`_on_ssh_disconnected(exc)`** — Called when SSH connection is lost

- **`_on_terminal_input(widget, text, size)`** — Handle input from the terminal (handled automatically by VTE)

- **`_on_terminal_resize(widget, width, height)`** — Handle terminal resize events

- **`_on_termprops_changed(terminal, ids, user_data=None)`** — Handle terminal properties changes for job detection (local terminals only)

- **`_on_vte_search_key_pressed(controller, keyval, keycode, state)`** — Handle global terminal search shortcuts on the VTE widget.

- **`_parse_group_color()`** — Handles parse group color.

- **`_prepare_key_for_native_mode()`** — Ensure explicit keys are unlocked when native SSH mode is active.

- **`_relative_luminance(rgba)`** — Handles relative luminance.

- **`_remove_custom_shortcut_controllers()`** — Detach any custom shortcut or scroll controllers from the VTE widget.

- **`_resolve_native_identity_candidates()`** — Return identity file candidates for native SSH preload attempts.

- **`_run_search(forward=True, update_entry=False)`** — Execute search navigation in the requested direction.

- **`_set_connecting_overlay_visible(visible)`** — Sets connecting overlay visible.

- **`_set_disconnected_banner_visible(visible, message=None)`** — Sets disconnected banner visible.

- **`_set_search_error_state(has_error)`** — Toggle error styling on the search entry when matches are not found.

- **`_set_search_navigation_sensitive(active)`** — Enable or disable navigation buttons based on active search state.

- **`_setup_context_menu()`** — Set up a robust per-terminal context menu and actions.

- **`_setup_local_shell_direct()`** — Set up local shell using direct spawn (legacy approach).

- **`_setup_mouse_wheel_zoom()`** — Set up mouse wheel zoom functionality with Cmd+MouseWheel.

- **`_setup_process_group(spawn_data)`** — Setup function called after fork but before exec

- **`_setup_ssh_terminal()`** — Set up terminal with direct SSH command (called from main thread)

- **`_show_forwarding_error_dialog(message)`** — Shows forwarding error dialog.

- **`_show_search_overlay(select_all=False)`** — Reveal the terminal search overlay and focus the search entry.

- **`_terminate_process_tree(pid)`** — Terminate a process and all its children

- **`_try_agent_based_shell()`** — Try to set up local shell using the agent (Ptyxis-style).

- **`_update_search_pattern(text, case_sensitive=False, regex=False, move_forward=True, update_entry=False)`** — Apply or update the search pattern on the VTE widget.

- **`apply_theme(theme_name=None)`** — Apply terminal theme and font settings

- **`copy_text()`** — Copy selected text to clipboard

- **`disconnect()`** — Close the SSH connection and clean up resources

- **`force_style_refresh()`** — Force a style refresh of the terminal widget.

- **`get_connection_info()`** — Get connection information

- **`get_job_status()`** — Get the current job status of the terminal.

- **`has_active_job()`** — Check if the terminal has an active job running.

- **`is_terminal_idle()`** — Check if the terminal is idle (no active job running).

- **`on_bell(terminal)`** — Handle terminal bell

- **`on_child_exited(terminal, status)`** — Handle terminal child process exit

- **`on_title_changed(terminal)`** — Handle terminal title change

- **`paste_text()`** — Paste text from clipboard

- **`reconnect()`** — Reconnect the terminal with updated connection settings

- **`reset_and_clear()`** — Reset and clear terminal

- **`reset_terminal()`** — Reset terminal

- **`reset_zoom()`** — Reset terminal zoom to default (1.0x)

- **`search_text(text, case_sensitive=False, regex=False)`** — Search for text in terminal

- **`select_all()`** — Select all text in terminal

- **`set_group_color(color)`** — Update the stored group color and refresh the theme if needed.

- **`set_group_color(color_value, force=False)`** — Sets group color.

- **`setup_local_shell()`** — Set up the terminal for local shell (not SSH)

- **`setup_terminal()`** — Initialize the VTE terminal with appropriate settings.

- **`zoom_in()`** — Zoom in the terminal font

- **`zoom_out()`** — Zoom out the terminal font



## Module: `sshpilot.terminal_manager`

### Class: `TerminalManager`

- **`__init__(window)`** — Handles init.

- **`_add_terminal_tab(terminal_widget, title)`** — Adds terminal tab.

- **`_apply_tab_css_color(page, rgba)`** — Apply CSS color to the tab view to color indicator icons

- **`_apply_tab_group_color(page, color_value, tooltip=None)`** — Handles apply tab group color.

- **`_apply_tab_icon_color(page, rgba, tooltip_text=None)`** — Apply color to tab icon by setting a colored icon

- **`_clear_tab_group_color(page)`** — Handles clear tab group color.

- **`_create_colored_tab_icon(rgba)`** — Create a simple colored icon for the tab indicator

- **`_create_tab_color_icon(rgba)`** — Creates tab color icon.

- **`_on_disconnect_confirmed(dialog, response_id, connection)`** — Handles disconnect confirmed.

- **`_resolve_group_color(connection)`** — Handles resolve group color.

- **`_resolve_group_color_and_name(connection)`** — Handles resolve group color and name.

- **`broadcast_command(command)`** — Handles broadcast command.

- **`connect_to_host(connection, force_new=False)`** — Connects to host.

- **`disconnect_from_host(connection)`** — Handles disconnect from host.

- **`get_all_terminal_job_statuses()`** — Get job status for all active terminals.

- **`get_terminal_job_status(terminal)`** — Get the job status of a terminal.

- **`on_terminal_connected(terminal)`** — Handles terminal connected.

- **`on_terminal_disconnected(terminal)`** — Handles terminal disconnected.

- **`on_terminal_title_changed(terminal, title)`** — Handles terminal title changed.

- **`restyle_open_terminals()`** — Handles restyle open terminals.

- **`show_local_terminal()`** — Shows local terminal.



## Module: `sshpilot.welcome_page`

### Class: `QuickConnectDialog`

- **`__init__(parent_window)`** — Handles init.

- **`_parse_ssh_command(command_text)`** — Parse SSH command text and extract connection parameters

- **`on_connect(*args)`** — Handle connect button or Enter key

- **`on_response(dialog, response)`** — Handle dialog response

### Class: `WelcomePage`

- **`__init__(window)`** — Handles init.

- **`_format_accelerator_display(accel)`** — Convert a GTK accelerator like '<primary><Shift>comma' to a

- **`_get_action_accel_display(shortcuts, action_name)`** — Get the first accelerator for an action and format it for display.

- **`_get_safe_current_shortcuts()`** — Safely get current shortcuts including user customizations from the app.

- **`_parse_ssh_command(command_text)`** — Parse SSH command text and extract connection parameters

- **`create_card(title, tooltip_text, icon_name, callback)`** — Create an activatable card with icon and title

- **`on_quick_connect_clicked(button)`** — Open quick connect dialog

- **`open_online_help()`** — Open online help documentation



## Module: `sshpilot.window`

### Functions

- **`_format_ssh_target(host, user)`** — Handles format ssh target.

- **`_normalize_remote_path(path)`** — Handles normalize remote path.

- **`_quote_remote_path_for_shell(path)`** — Handles quote remote path for shell.

- **`_remote_join(base, child)`** — Handles remote join.

- **`_remote_parent(path)`** — Handles remote parent.

- **`_resolve_ssh_copy_id_askpass_env(connection, ssh_key, connection_manager)`** — Return askpass environment and force status for ssh-copy-id launches.

- **`download_file(host, user, remote_file, local_path, recursive=False, port=22, password=None, known_hosts_path=None, extra_ssh_opts=None, use_publickey=False, inherit_env=None, saved_passphrase=None, keyfile=None, key_mode=None)`** — Download a remote file (or directory when ``recursive``) via SCP.

- **`list_remote_files(host, user, remote_path, port=22, password=None, known_hosts_path=None, extra_ssh_opts=None, use_publickey=False, inherit_env=None)`** — List remote files via SSH for the provided path.

- **`maybe_set_native_controls(header_bar, value=False)`** — Safely set native controls on header bar, with fallback for older GTK versions.

### Class: `MainWindow`

- **`__init__(*args, isolated=False, **kwargs)`** — Handles init.

- **`_add_fallback_shortcuts(section, primary)`** — Add fallback static shortcuts if dynamic generation fails

- **`_add_safe_current_shortcuts(section, primary)`** — Add shortcuts with current customizations using a safe approach

- **`_append_scp_option_pair(options, flag, value)`** — Append a flag/value pair to ``options`` if it is not already present.

- **`_build_grouped_list(hierarchy, connections_dict, level)`** — Recursively build the grouped connection list

- **`_build_scp_argv(connection, sources, destination, direction, known_hosts_path=None)`** — Builds scp argv.

- **`_build_scp_connection_profile(connection)`** — Builds scp connection profile.

- **`_build_shortcuts_window()`** — Builds shortcuts window.

- **`_build_ssh_copy_id_argv(connection, ssh_key, force=False, known_hosts_path=None)`** — Construct argv for ssh-copy-id honoring saved UI auth preferences.

- **`_cancel_broadcast_hide_timeout()`** — Cancel any pending hide timeout for the broadcast banner

- **`_close_tab(tab_view, page)`** — Close the tab and clean up resources

- **`_connection_row_for_coordinate(coord)`** — Return the listbox row whose allocation includes the given list-space coordinate.

- **`_connections_from_rows(rows)`** — Return unique connections represented by the provided rows.

- **`_create_file_manager_placeholder_tab(nickname, host_value)`** — Create and show a placeholder tab while the embedded manager loads.

- **`_cycle_connection_tabs_or_open(connection)`** — If there are open tabs for this server, cycle to the next one (wrap).

- **`_determine_neighbor_connection_row(target_rows)`** — Find the closest remaining connection row after deleting target_rows.

- **`_disconnect_connection_terminals(connection)`** — Disconnect all tracked terminals for a connection.

- **`_do_quit()`** — Actually quit the application - FINAL STEP

- **`_error_dialog(heading, body, detail='')`** — Handles error dialog.

- **`_extend_scp_options_from_connection(connection, options)`** — Augment ``options`` with connection-specific SSH arguments.

- **`_focus_active_terminal_tab()`** — Focus the currently active terminal tab

- **`_focus_connection_list_first_row()`** — Focus the connection list and ensure the first row is selected (startup only).

- **`_focus_most_recent_tab(connection)`** — Focus the most recent tab for a connection if one exists.

- **`_focus_most_recent_tab_or_open_new(connection)`** — If there are open tabs for this server, focus the most recent one.

- **`_focus_terminal_widget(terminal)`** — Request focus for a terminal widget, retrying on idle if needed.

- **`_generate_duplicate_nickname(base_nickname)`** — Generate a unique nickname for a duplicated connection.

- **`_get_active_terminal_widget()`** — Return the TerminalWidget for the currently selected tab, if any.

- **`_get_default_terminal_command()`** — Get the default terminal command from desktop environment

- **`_get_safe_current_shortcuts()`** — Safely get current shortcuts including customizations

- **`_get_selected_connection_rows()`** — Return all selected rows that represent connections.

- **`_get_selected_group_rows()`** — Return all selected rows that represent groups.

- **`_get_sidebar_width()`** — Returns sidebar width.

- **`_get_target_connection_rows(prefer_context=False)`** — Return rows targeted by the current action, respecting context menus.

- **`_get_target_connections(prefer_context=False)`** — Return connection objects targeted by the current action.

- **`_get_user_preferred_terminal()`** — Get the user's preferred terminal from settings

- **`_handle_file_manager_placeholder_error(placeholder_info, display_name, message)`** — Update placeholder tab to reflect an error state.

- **`_info_dialog(heading, body)`** — Handles info dialog.

- **`_install_sidebar_css()`** — Install sidebar focus CSS

- **`_on_config_setting_changed(_config, key, value)`** — Synchronize runtime state when configuration values change.

- **`_on_connection_list_key_pressed(controller, keyval, keycode, state)`** — Handle key presses in the connection list

- **`_on_group_toggled(group_row, group_id, expanded)`** — Handle group expand/collapse

- **`_on_reconnect_response(dialog, response_id, connection)`** — Handle response from reconnect prompt

- **`_on_search_entry_key_pressed(controller, keyval, keycode, state)`** — Handle key presses in search entry.

- **`_on_ssh_config_editor_saved()`** — Handles ssh config editor saved.

- **`_on_startup_complete()`** — Called when startup is complete - process any pending focus operations

- **`_on_tab_close_confirmed(dialog, response_id, tab_view, page)`** — Handle response from tab close confirmation dialog

- **`_on_tab_close_response(dialog, response_id)`** — Handle the response from the close confirmation dialog.

- **`_on_upload_files_chosen(dialog, result, connection)`** — Handles upload files chosen.

- **`_open_connection_in_external_terminal(connection)`** — Open the connection in the user's preferred external terminal

- **`_open_manage_files_for_connection(connection)`** — Open files for the supplied connection using the best integration.

- **`_open_ssh_config_editor()`** — Opens ssh config editor.

- **`_open_system_terminal(terminal_command, ssh_command)`** — Launch a terminal command with an SSH command.

- **`_prompt_delete_connections(connections, neighbor_row=None)`** — Show a confirmation dialog for deleting one or more connections.

- **`_prompt_group_edit_options(connection, block_info)`** — Present options when editing a grouped host

- **`_prompt_reconnect(connection)`** — Show a dialog asking if user wants to reconnect with new settings

- **`_prompt_scp_download(connection)`** — Show a simple file picker that downloads selected remote files via scp.

- **`_queue_focus_operation(focus_func)`** — Queue a focus operation to be executed after startup is complete

- **`_rebuild_connections_list()`** — Rebuild the sidebar connections list from manager state, avoiding duplicates.

- **`_reconnect_terminal(connection)`** — Reconnect a terminal with updated connection settings

- **`_register_file_manager_tab(widget, controller, nickname, host_value, page=None, container=None)`** — Add an embedded file manager tab to the tab view.

- **`_replace_placeholder_tab_content(container, widget)`** — Replace placeholder children with the real widget.

- **`_reset_controlled_reconnect()`** — Reset the controlled reconnect flag

- **`_resolve_connection_list_event(x, y, scrolled_window=None)`** — Resolve the target row and viewport coordinates for a pointer event on the connection list.

- **`_return_to_tab_view_if_welcome()`** — Switch back to tab view if the welcome view is currently visible.

- **`_save_window_state()`** — Save window state before quitting

- **`_schedule_broadcast_hide_timeout(timeout_ms=5000)`** — Schedule hiding the broadcast banner after a delay

- **`_schedule_startup_tasks()`** — Schedule one-time startup behaviors such as focus and welcome state.

- **`_select_only_row(row)`** — Select only the provided row, clearing any other selections.

- **`_select_tab_relative(delta)`** — Select tab relative to current index, wrapping around.

- **`_set_content_widget(widget)`** — Sets content widget.

- **`_set_sidebar_widget(widget)`** — Sets sidebar widget.

- **`_show_duplicate_connection_error(connection, error)`** — Display an error dialog when duplication fails.

- **`_show_manage_files_error(connection_name, error_message)`** — Show error dialog for manage files failure

- **`_show_reconnect_error(connection, error_message=None)`** — Show an error message when reconnection fails

- **`_show_scp_terminal_window(connection, sources, destination, direction)`** — Shows scp terminal window.

- **`_show_ssh_copy_id_terminal_using_main_widget(connection, ssh_key, force=False)`** — Show a window with header bar and embedded terminal running ssh-copy-id.

- **`_show_terminal_error_dialog()`** — Show error dialog when no terminal is found

- **`_start_scp_transfer(connection, sources, destination, direction)`** — Run scp using the same terminal window layout as ssh-copy-id.

- **`_start_scp_upload_flow(connection)`** — Kick off the upload flow using a portal-aware file chooser.

- **`_toggle_class(widget, name, on)`** — Helper to toggle CSS class on a widget

- **`_toggle_sidebar_visibility(is_visible)`** — Helper method to toggle sidebar visibility

- **`_track_internal_file_manager_window(window, widget=None)`** — Keep a reference to in-app file manager controllers to prevent GC.

- **`_update_tab_button_visibility()`** — Update TabButton visibility based on number of tabs

- **`_update_tab_titles()`** — Update tab titles

- **`add_connection_row(connection, indent_level=0)`** — Add a connection row to the list with optional indentation

- **`create_menu()`** — Create application menu

- **`duplicate_connection(connection)`** — Duplicate an existing connection, persist it, and select the new entry.

- **`focus_connection_list()`** — Focus the connection list and show a toast notification.

- **`focus_search_entry()`** — Toggle search on/off and show appropriate toast notification.

- **`get_available_groups()`** — Get list of available groups for selection

- **`hide_broadcast_banner()`** — Hide the broadcast banner

- **`move_connection_to_group(connection_nickname, target_group_id=None)`** — Move a connection to a specific group

- **`on_activate_connection(action, param)`** — Handle the activate-connection action

- **`on_add_connection_clicked(button)`** — Handle add connection button click

- **`on_broadcast_banner_key_pressed(controller, keyval, keycode, state)`** — Handle key presses on the entire broadcast banner

- **`on_broadcast_cancel_clicked(button)`** — Handle broadcast banner cancel button click

- **`on_broadcast_command_action(action, param=None)`** — Handle broadcast command action - shows banner to input command

- **`on_broadcast_entry_activate(entry)`** — Handle Enter key press in broadcast entry

- **`on_broadcast_entry_changed(entry)`** — Track user edits to the broadcast entry

- **`on_broadcast_entry_focus_enter(controller, *args)`** — Cancel hide timeout when the entry gains focus

- **`on_broadcast_entry_focus_leave(controller, *args)`** — Schedule hiding when the entry loses focus

- **`on_broadcast_entry_key_pressed(controller, keyval, keycode, state)`** — Handle key presses in broadcast entry

- **`on_broadcast_send_clicked(button)`** — Handle broadcast banner send button click

- **`on_close_request(window)`** — Handle window close request - MAIN ENTRY POINT

- **`on_connection_activate(list_box, row)`** — Handle connection activation (Enter key or double-click)

- **`on_connection_activated(list_box, row)`** — Handle connection activation (Enter key)

- **`on_connection_added(manager, connection)`** — Handle new connection added

- **`on_connection_click(gesture, n_press, x, y)`** — Handle clicks on the connection list

- **`on_connection_removed(manager, connection)`** — Handle connection removed from the connection manager

- **`on_connection_saved(dialog, connection_data)`** — Handle connection saved from dialog

- **`on_connection_selected(list_box, row)`** — Handle connection list selection change

- **`on_connection_status_changed(manager, connection, is_connected)`** — Handle connection status change

- **`on_copy_key_to_server_clicked(_button)`** — Handles copy key to server clicked.

- **`on_create_group_action(action, param=None)`** — Handle create group action

- **`on_delete_connection_action(action, param=None)`** — Handle delete connection action from context menu

- **`on_delete_connection_clicked(button)`** — Handle delete connection button click

- **`on_delete_connection_response(dialog, response, payload)`** — Handle delete connection dialog response

- **`on_delete_group_clicked(button)`** — Handle delete group button click

- **`on_edit_connection_action(action, param=None)`** — Handle edit connection action from context menu

- **`on_edit_connection_clicked(button)`** — Handle edit connection button click

- **`on_edit_group_action(action, param=None)`** — Handle edit group action

- **`on_local_terminal_button_clicked(button)`** — Handle local terminal button click

- **`on_manage_files_action(action, param=None)`** — Handle manage files action from context menu

- **`on_manage_files_button_clicked(button)`** — Handle manage files button click from toolbar

- **`on_move_to_group_action(action, param=None)`** — Handle move to group action

- **`on_move_to_ungrouped_action(action, param=None)`** — Handle move to ungrouped action

- **`on_open_in_system_terminal_action(action, param=None)`** — Handle open in system terminal action from context menu

- **`on_open_new_connection_action(action, param=None)`** — Open a new tab for the selected connection via context menu.

- **`on_open_new_connection_tab_action(action, param=None)`** — Open a new tab for the selected connection via global shortcut (Ctrl/⌘+Alt+N).

- **`on_quit_confirmation_response(dialog, response)`** — Handle quit confirmation dialog response

- **`on_rename_group_clicked(button)`** — Handle rename group button click

- **`on_scp_button_clicked(button)`** — Prompt the user to choose between uploading or downloading with scp.

- **`on_search_changed(entry)`** — Handle search text changes and update connection list.

- **`on_search_stopped(entry)`** — Handle search stop (Esc key).

- **`on_setting_changed(config, key, value)`** — Handle configuration setting change

- **`on_sidebar_toggle(button)`** — Handle sidebar toggle button click

- **`on_system_terminal_button_clicked(button)`** — Handle system terminal button click from toolbar

- **`on_tab_attached(tab_view, page, position)`** — Handle tab attached

- **`on_tab_button_clicked(button)`** — Handle tab button click to open/close tab overview and switch to tab view

- **`on_tab_close(tab_view, page)`** — Handle tab close - THE KEY FIX: Never call close_page ourselves

- **`on_tab_detached(tab_view, page, position)`** — Handle tab detached

- **`on_tab_selected(tab_view, _pspec=None)`** — Update active terminal mapping when the user switches tabs.

- **`on_view_toggle_clicked(button)`** — Handle view toggle button click to switch between welcome and tabs

- **`on_window_size_changed(window, param)`** — Handle window size changes and save the new dimensions

- **`on_window_size_changed(window, param)`** — Handle window size change

- **`open_help_url()`** — Open the SSH Pilot wiki using a portal-friendly launcher.

- **`open_in_system_terminal(connection)`** — Open the connection in the system's default terminal

- **`rebuild_connection_list()`** — Rebuild the connection list with groups

- **`setup_connections()`** — Load and display existing connections with grouping

- **`setup_content_area()`** — Set up the main content area with stack for tabs and welcome view

- **`setup_sidebar()`** — Set up the sidebar with connection list

- **`setup_signals()`** — Connect to manager signals

- **`setup_ui()`** — Set up the user interface

- **`setup_window()`** — Configure main window properties

- **`show_about_dialog()`** — Show about dialog

- **`show_broadcast_banner()`** — Show the broadcast banner

- **`show_connection_dialog(connection=None, skip_group_warning=False, force_split_from_group=False, split_group_source=None, split_original_nickname=None)`** — Show connection dialog for adding/editing connections

- **`show_connection_selection_for_ssh_copy()`** — Show a dialog to select a connection for SSH key copy

- **`show_key_dialog(on_success=None)`** — Single key generation dialog (Adw). Optional passphrase.

- **`show_known_hosts_editor()`** — Show known hosts editor window

- **`show_preferences()`** — Show preferences dialog

- **`show_quit_confirmation_dialog()`** — Show confirmation dialog when quitting with active connections

- **`show_shortcuts_window()`** — Display keyboard shortcuts using Gtk.ShortcutsWindow

- **`show_tab_view()`** — Show the tab view when connections are active

- **`show_welcome_view()`** — Show the welcome/help view when no connections are active

- **`simple_close_handler(window)`** — Handle window close - distinguish between tab close and window close

- **`toggle_list_focus()`** — Toggle focus between connection list and terminal

- **`toggle_terminal_search_overlay(select_all=False)`** — Toggle the search overlay for the currently focused terminal tab.

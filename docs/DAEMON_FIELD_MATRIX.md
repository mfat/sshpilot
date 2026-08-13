# Daemon Migration — Connection Dialog Field Matrix

This document is the definitive reference for the daemon connection-mutation
migration. Every phase of the implementation references it by row number.

## Legend

| Column | Meaning |
|--------|---------|
| # | Row ID for cross-referencing |
| Widget | GTK/Adwaita widget in the connection dialog |
| data key | Key in the `connection_data` dict from `on_save_clicked` |
| Conn attr | Attribute set on the `Connection` object |
| SSH directive | What appears in `~/.ssh/config` |
| Formatter | Function that writes the directive (`ssh_config_formatter.py`) |
| Parser | Function that reads it back (`connection_manager.py`) |
| Domain | `config` = SSH config, `secret` = secret backend, `metadata` = app config, `transient` = save-time only |
| Guarded | Whether `prepare_connection_save_for_client` blocks this field in daemon mode |

## Core Identity

| # | Widget | data key | Conn attr | SSH directive | Formatter | Parser | Domain | Guarded |
|---|--------|----------|-----------|---------------|-----------|--------|--------|---------|
| 1 | `nickname_row` | `nickname` | `nickname` | `Host <token>` | `f"Host {primary_token}"` | `host_token` → `nickname` | config | No |
| 2 | `hostname_row` | `hostname` | `hostname` | `HostName <value>` | `f"HostName {host}"` (omitted when empty) | `config['hostname']` | config | No |
| 3 | `username_row` | `username` | `username` | `User <value>` | `f"User {username}"` (omitted when empty) | `config.get('user')` | config | No |
| 4 | `port_row` | `port` | `port` | `Port <value>` | `f"Port {port}"` (omitted when 22) | `_safe_int(config.get('port', 22))` | config | No |

## Authentication

| # | Widget | data key | Conn attr | SSH directive | Formatter | Parser | Domain | Guarded |
|---|--------|----------|-----------|---------------|-----------|--------|--------|---------|
| 5 | `auth_toggle` | `auth_method` | `auth_method` | Derived: `PreferredAuthentications` ordering | Emits `PreferredAuthentications` based on value (0=key, 1=password) | `_resolve_auth_method_from_config` | config (derived) | **Yes** |
| 6 | `key_editor` | `identity_files` | `identity_files` | `IdentityFile <path>` (one per entry) | `for kf in identity_files: ...` (only when `auth_method==0` and `dedicated_key`) | `config.get('identityfile')` → expanded list | config | **Yes** |
| 7 | `key_editor` | `keyfile` | `keyfile` | `IdentityFile <path>` (primary) | Same as `identity_files[0]` | `identity_files[0] if identity_files else ''` | config | **Yes** |
| 8 | `cert_editor` | `certificate_files` | `certificate_files` | `CertificateFile <path>` (one per entry) | `for cert in certificate_files: ...` | `config.get('certificatefile')` → expanded list | config | **Yes** |
| 9 | `cert_editor` | `certificate` | `certificate` | `CertificateFile <path>` (primary) | Same as `certificate_files[0]` | `certificate_files[0] if certificate_files else ''` | config | **Yes** |
| 10 | `key_select_row` + `key_only_row` | `key_select_mode` | `key_select_mode` | `IdentitiesOnly yes` (mode 1 only) | Emits `IdentitiesOnly yes` when `key_select_mode == 1` | `_resolve_key_select_mode` from `identitiesonly` + `identityfile` presence | config (derived) | **Yes** |
| 11 | `password_row` | `password` | `password` | **None** — never in SSH config | Not written. Stored via `store_connection_password()` | Fetched async from secret backend | **secret** | Special |
| 12 | `password_row` | `password_changed` | *(transient)* | **None** | Not written. Used by `_store_secrets_then_save` | N/A | transient | Special |
| 13 | `pubkey_auth_row` | `pubkey_auth_no` | `pubkey_auth_no` | `PubkeyAuthentication no` | Emits when `auth_method != 0` and `pubkey_auth_no` | `pubkey_auth == 'no'` | config | **Yes** |

## Agent & Hardware Key

| # | Widget | data key | Conn attr | SSH directive | Formatter | Parser | Domain | Guarded |
|---|--------|----------|-----------|---------------|-----------|--------|--------|---------|
| 14 | `identity_agent_row` | `identity_agent` | `identity_agent` | `IdentityAgent <value>` | `f"    IdentityAgent {ident_agent}"` (when `auth_method==0`) | `config.get('identityagent')` | config | **Yes** |
| 15 | `add_keys_to_agent_row` | `add_keys_to_agent` | `add_keys_to_agent` | `AddKeysToAgent <value>` | `f"    AddKeysToAgent {add_keys}"` (when `auth_method==0`) | `config.get('addkeystoagent')` | config | **Yes** |
| 16 | `pkcs11_provider_row` | `pkcs11_provider` | `pkcs11_provider` | `PKCS11Provider <value>` | `f"    PKCS11Provider {pkcs11}"` (when `auth_method==0`) | `config.get('pkcs11provider')` | config | **Yes** |
| 17 | `security_key_provider_row` | `security_key_provider` | `security_key_provider` | `SecurityKeyProvider <value>` | `f"    SecurityKeyProvider {sk_provider}"` (when `auth_method==0`) | `config.get('securitykeyprovider')` | config | **Yes** |

## Routing & Forwarding

| # | Widget | data key | Conn attr | SSH directive | Formatter | Parser | Domain | Guarded |
|---|--------|----------|-----------|---------------|-----------|--------|--------|---------|
| 18 | `x11_row` | `x11_forwarding` | `x11_forwarding` | `ForwardX11 yes` | `lines.append("    ForwardX11 yes")` (when True) | `forwardx11 in ('yes','true','1','on')` | config | **Yes** |
| 19 | `proxy_jump_row` | `proxy_jump` | `proxy_jump` | `ProxyJump <h1,h2,...>` | `f"    ProxyJump {','.join(proxy_jump)}"` | `config.get('proxyjump')` → split by `[\s,]+` | config | **Yes** |
| 20 | `forward_agent_row` | `forward_agent` | `forward_agent` | `ForwardAgent yes` | `f"    ForwardAgent {target or 'yes'}"` | `config.get('forwardagent')` → boolean | config | **Yes** |

## Commands

| # | Widget | data key | Conn attr | SSH directive | Formatter | Parser | Domain | Guarded |
|---|--------|----------|-----------|---------------|-----------|--------|--------|---------|
| 21 | `pre_command_row` | `pre_command` | `pre_command` | `# sshpilot:PreCommand <value>` (comment) | `f"    # sshpilot:PreCommand {pre_cmd}"` | Comment prefix parsed in loader | config (comment) | **Yes** |
| 22 | `local_command_row` | `local_command` | `local_command` | `PermitLocalCommand yes` + `LocalCommand <value>` | Both emitted when non-empty | `config.get('localcommand')` | config | **Yes** |
| 23 | `remote_command_row` | `remote_command` | `remote_command` | `RemoteCommand <value>`; `RequestTTY <token>` only when explicitly selected/authored | Preserved exactly as entered | `config.get('remotecommand')` + `config.get('requesttty')` | config | **Yes** |

## Advanced

| # | Widget | data key | Conn attr | SSH directive | Formatter | Parser | Domain | Guarded |
|---|--------|----------|-----------|---------------|-----------|--------|--------|---------|
| 24 | `advanced_tab` | `extra_ssh_config` | `extra_ssh_config` | Any non-managed directives | Verbatim, indented | Non-`MANAGED_HOST_OPTIONS` keys collected | config (verbatim) | **Yes** |

## Forwarding Rules

| # | Widget | data key | Conn attr | SSH directive | Formatter | Parser | Domain | Guarded |
|---|--------|----------|-----------|---------------|-----------|--------|--------|---------|
| 25 | forwarding rule editor | `forwarding_rules` | `forwarding_rules` | `LocalForward`, `RemoteForward`, `DynamicForward` | Loop over rules, emit per type | `_parse_forwarding_rules_from_config` | config | **Yes** |

### Forwarding Rule Dict Schema (Exact)

**Local:**
```python
{'type': 'local', 'listen_addr': 'localhost', 'listen_port': 8080,
 'remote_host': 'localhost', 'remote_port': 22, 'enabled': True}
```

**Remote:**
```python
{'type': 'remote', 'listen_addr': '', 'listen_port': 8080,
 'local_host': '127.0.0.1', 'local_port': 80, 'enabled': True}
```

**Remote SOCKS:**
```python
{'type': 'remote', 'listen_addr': '', 'listen_port': 1080,
 'socks': True, 'enabled': True}
```

**Dynamic:**
```python
{'type': 'dynamic', 'listen_addr': 'localhost', 'listen_port': 1080,
 'enabled': True}
```

## Metadata (App-Only, Not SSH Config)

| # | Widget | data key | Conn attr | SSH directive | Formatter | Parser | Domain | Guarded |
|---|--------|----------|-----------|---------------|-----------|--------|--------|---------|
| 26 | `wol_mac_row` | `__meta.wol_mac` | *(none)* | **None** | N/A | N/A | **metadata** | Separate guard |
| 27 | `wol_broadcast_row` | `__meta.wol_broadcast_ip` | *(none)* | **None** | N/A | N/A | **metadata** | Separate guard |
| 28 | `wol_port_row` | `__meta.wol_port` | *(none)* | **None** | N/A | N/A | **metadata** | Separate guard |
| 29 | `tags_row` | `__meta.tags` | *(none)* | **None** | N/A | N/A | **metadata** | Separate guard |

## Internal / Transient

| # | Widget | data key | Conn attr | SSH directive | Domain | Guarded |
|---|--------|----------|-----------|---------------|--------|---------|
| 30 | *(set by on_save)* | `aliases` | `aliases` | Multiple tokens on `Host` line | config | **Yes** |
| 31 | *(internal)* | `__split_from_group` | *(none)* | **None** | transient | Separate guard |
| 32 | *(internal)* | `__split_source` | *(none)* | **None** | transient | Separate guard |
| 33 | *(internal)* | `__split_original_nickname` | *(none)* | **None** | transient | Separate guard |
| 34 | *(internal)* | `__previous_secret_identity` | *(none)* | **None** | transient | N/A |
| 35 | *(internal)* | `__meta` | *(none)* | **None** | metadata | Separate guard |
| 36 | *(internal)* | `__save_completion` | *(none)* | **None** | transient | N/A |

## Preserved-Only Fields (No Dialog Widget)

These are parsed from SSH config but have no dialog widget. They survive edits
via `_preserve_multivalue_on_update` when the save payload omits them.

| # | data key | Conn attr | SSH directive | Formatter | Parser | Domain |
|---|----------|-----------|---------------|-----------|--------|--------|
| 38 | `proxy_command` | `proxy_command` | `ProxyCommand <value>` | `f"    ProxyCommand {proxy_command}"` | `config['proxycommand']` | config |
| 39 | `request_tty` | `request_tty` | `RequestTTY <token>` | Emitted only when explicitly selected/authored | `config.get('requesttty')` → normalized token | config |
| 40 | `forward_agent_target` | `forward_agent_target` | Part of `ForwardAgent` value | `f"    ForwardAgent {target}"` | Extracted from `forwardagent` when not plain yes/no | config |
| 41 | `identity_file_none` | `identity_file_none` | `IdentityFile none` | `_clean_list` filters `none` | Detected as `identity_suppressed` flag | config (derived) |
| 42 | `preferred_authentications` | *(none)* | `PreferredAuthentications <list>` | Derived from `auth_method`, not read from data | `parsed['preferred_authentications']` | config (derived) |
| 43 | `host` | `host` | Same as `Host` token | N/A | `host = host_token` | config |

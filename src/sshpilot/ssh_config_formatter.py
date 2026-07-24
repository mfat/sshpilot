"""Serialization of connection data to ssh_config text.

Pure functions — no filesystem, no GObject, no manager state. The
ConnectionManager owns reading/writing files (atomic writes, signals) and
delegates rendering here:

- ``format_ssh_config_entry(data)`` renders a complete Host block from a
  connection-data dict (managed directives + extra_ssh_config verbatim).
- ``merged_block_lines(old_block, data)`` renders a *surgical edit* of an
  existing block: managed directives are re-emitted from data while comments,
  blank lines, and unknown directives keep their authored form and position.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from .ssh_config_document import HostBlock, _split_config_option, _split_keyword

# Directives the app owns end-to-end: parsed into typed Connection fields and
# re-emitted from data by format_ssh_config_entry. Anything else in a Host
# block is "unknown" — surfaced through extra_ssh_config for editing and
# preserved verbatim (authored form) by the surgical block merge on save.
# (``__host_tokens``/``__pre_command``-style dunder keys are parse artifacts
# in the data dict, not directives.)
MANAGED_HOST_OPTIONS = frozenset({
    'host', 'hostname', 'aliases', 'port', 'user', 'identityfile', 'certificatefile',
    'forwardx11', 'localforward', 'remoteforward', 'dynamicforward',
    'proxycommand', 'proxyjump', 'forwardagent', 'localcommand', 'remotecommand', 'requesttty',
    'identitiesonly', 'permitlocalcommand',
    'preferredauthentications', 'pubkeyauthentication',
    'identityagent', 'addkeystoagent', 'pkcs11provider', 'securitykeyprovider',
})


def format_ssh_config_entry(data: Dict[str, Any]) -> str:
    """Format connection data as SSH config entry"""
    def _quote_token(token: str) -> str:
        if not token:
            return '""'
        if any(c.isspace() for c in token):
            return f'"{token}"'
        return token

    def _format_forward_host(host: str) -> str:
        host = (host or '').strip()
        if not host:
            return host
        if ':' in host and not (host.startswith('[') and host.endswith(']')):
            return f"[{host}]"
        return host

    host = data.get('hostname') or data.get('host', '')
    nickname = data.get('nickname') or host
    primary_token = _quote_token(nickname)
    lines = [f"Host {primary_token}"]

    # Add basic connection info
    if host and host != nickname:
        lines.append(f"    HostName {host}")
    # Omit User when empty: a bare "User" line is a fatal ssh_config parse
    # error that makes ssh reject the ENTIRE file, and ssh defaults to the
    # local user anyway (as does parse_host_config on read-back).
    username = str(data.get('username', '') or '').strip()
    if username:
        lines.append(f"    User {username}")

    # Add port if specified and not default
    port = data.get('port')
    if port and port != 22:  # Only add port if it's not the default 22
        lines.append(f"    Port {port}")

    # Proxy settings
    proxy_jump = data.get('proxy_jump') or []
    if isinstance(proxy_jump, str):
        proxy_jump = [h.strip() for h in re.split(r'[\s,]+', proxy_jump) if h.strip()]
    if proxy_jump:
        lines.append(f"    ProxyJump {','.join(proxy_jump)}")
    proxy_command = (data.get('proxy_command') or '').strip()
    if proxy_command:
        lines.append(f"    ProxyCommand {proxy_command}")
    if data.get('forward_agent'):
        # ForwardAgent also accepts a socket path / $ENV per ssh_config(5).
        forward_target = str(data.get('forward_agent_target') or '').strip()
        lines.append(f"    ForwardAgent {forward_target or 'yes'}")

    # Add IdentityFile/IdentitiesOnly per selection when auth is key-based
    keyfile = data.get('keyfile') or data.get('private_key')
    auth_method = int(data.get('auth_method', 0) or 0)
    key_select_mode = int(data.get('key_select_mode', 0) or 0)
    dedicated_key = key_select_mode in (1, 2)

    def _quote_if_spaced(value: str) -> str:
        if ' ' in value and not (value.startswith('"') and value.endswith('"')):
            return f'"{value}"'
        return value

    def _clean_list(values, placeholder_prefix):
        cleaned = []
        for value in values:
            if not isinstance(value, str):
                continue
            stripped = value.strip()
            if not stripped or stripped.lower().startswith(placeholder_prefix):
                continue
            if stripped not in cleaned:
                cleaned.append(stripped)
        return cleaned

    if auth_method == 0:
        # ssh_config(5) allows multiple IdentityFile/CertificateFile entries;
        # write the full list when present, falling back to the primary key.
        identity_files = _clean_list(
            data.get('identity_files') or ([keyfile] if keyfile else []),
            'select key file',
        )
        # Only write IdentityFile when using a dedicated key mode
        if dedicated_key and identity_files:
            for kf in identity_files:
                lines.append(f"    IdentityFile {_quote_if_spaced(kf)}")

            if key_select_mode == 1:
                lines.append("    IdentitiesOnly yes")

            # Add certificate(s) if specified (exclude placeholder text)
            certificate_files = _clean_list(
                data.get('certificate_files') or ([data.get('certificate')] if data.get('certificate') else []),
                'select certificate',
            )
            for cert in certificate_files:
                lines.append(f"    CertificateFile {_quote_if_spaced(cert)}")

        # Agent / hardware key sources — valid in both automatic and
        # specific-key modes (the key may come from an agent socket, a
        # PKCS#11 smartcard, or a FIDO security key rather than a file).
        ident_agent = (data.get('identity_agent') or '').strip()
        if ident_agent:
            lines.append(f"    IdentityAgent {_quote_if_spaced(ident_agent)}")
        add_keys = (data.get('add_keys_to_agent') or '').strip()
        if add_keys:
            lines.append(f"    AddKeysToAgent {add_keys}")
        pkcs11 = (data.get('pkcs11_provider') or '').strip()
        if pkcs11:
            lines.append(f"    PKCS11Provider {_quote_if_spaced(pkcs11)}")
        sk_provider = (data.get('security_key_provider') or '').strip()
        if sk_provider:
            lines.append(f"    SecurityKeyProvider {_quote_if_spaced(sk_provider)}")
        # Include password-based fallback if a password is provided
        if data.get('password'):
            lines.append(
                "    PreferredAuthentications gssapi-with-mic,hostbased,publickey,keyboard-interactive,password"
            )
    else:
        # Password-based authentication. Include keyboard-interactive so
        # PAM/2FA hosts (which often disable the raw "password" method)
        # still negotiate; order prefers kbd-int first.
        lines.append(
            "    PreferredAuthentications keyboard-interactive,password"
        )
        if data.get('pubkey_auth_no'):
            lines.append("    PubkeyAuthentication no")

    # Add X11 forwarding if enabled
    if data.get('x11_forwarding', False):
        lines.append("    ForwardX11 yes")

    # Add PreCommand (sshpilot-specific, stored as a comment)
    pre_cmd = (data.get('pre_command') or '').strip()
    if pre_cmd:
        lines.append(f"    # sshpilot:PreCommand {pre_cmd}")

    # Add LocalCommand if specified, ensure PermitLocalCommand (write exactly as provided)
    local_cmd = (data.get('local_command') or '').strip()
    if local_cmd:
        lines.append("    PermitLocalCommand yes")
        lines.append(f"    LocalCommand {local_cmd}")

    # Preserve an authored RequestTTY token (yes/no/force/auto have distinct
    # ssh semantics); legacy bool True from older stored data degrades to yes.
    tty_token = data.get('request_tty')
    if isinstance(tty_token, str) and tty_token.strip().lower() in ('yes', 'no', 'force', 'auto'):
        tty_token = tty_token.strip().lower()
    elif tty_token:
        tty_token = 'yes'
    else:
        tty_token = ''

    # Add RemoteCommand and RequestTTY if specified (ensure shell stays active)
    remote_cmd = (data.get('remote_command') or '').strip()
    if remote_cmd:
        # Ensure we keep an interactive shell after the command
        remote_cmd_aug = remote_cmd if 'exec $SHELL' in remote_cmd else f"{remote_cmd} ; exec $SHELL -l"
        # Write RemoteCommand first, then RequestTTY (order for readability).
        # The interactive shell needs a TTY, so default to yes — but an
        # explicitly authored token still wins.
        lines.append(f"    RemoteCommand {remote_cmd_aug}")
        lines.append(f"    RequestTTY {tty_token or 'yes'}")
    elif tty_token:
        lines.append(f"    RequestTTY {tty_token}")

    # Add port forwarding rules if any (ensure sane defaults)
    for rule in data.get('forwarding_rules', []):
        listen_addr = (rule.get('listen_addr') or '').strip()
        listen_port = rule.get('listen_port', '')
        if not listen_port:
            continue
        # An empty bind address is written without a host prefix (omitted), so
        # ssh/GatewayPorts decides the bind. local/dynamic always carry a
        # localhost default, so only an empty remote bind drops the prefix.
        listen_host = _format_forward_host(listen_addr)
        listen_spec = f"{listen_host}:{listen_port}" if listen_host else f"{listen_port}"

        if rule.get('type') == 'local':
            dest_host = rule.get('remote_host', '')
            dest_spec = f"{_format_forward_host(dest_host) or dest_host}:{rule.get('remote_port', '')}"
            lines.append(f"    LocalForward {listen_spec} {dest_spec}")
        elif rule.get('type') == 'remote':
            # Single-argument (SOCKS) form has no destination. A destination
            # needs both a host and a port; if either is missing fall back to
            # the SOCKS form rather than emitting a malformed "host:" spec.
            dest_host = rule.get('local_host') or rule.get('remote_host', '')
            dest_port = rule.get('local_port') or rule.get('remote_port')
            if rule.get('socks') or not dest_host or not dest_port:
                lines.append(f"    RemoteForward {listen_spec}")
            else:
                # For RemoteForward we forward remote listen -> local destination
                dest_spec = f"{_format_forward_host(dest_host) or dest_host}:{dest_port}"
                lines.append(f"    RemoteForward {listen_spec} {dest_spec}")
        elif rule.get('type') == 'dynamic':
            lines.append(f"    DynamicForward {listen_spec}")

    # Add extra SSH config parameters if provided
    extra_config = data.get('extra_ssh_config', '').strip()
    if extra_config:
        # Split by lines and add each line as a separate config option
        for line in extra_config.split('\n'):
            line = line.strip()
            if line and not line.startswith('#'):  # Skip empty lines and comments
                # Ensure proper indentation
                if not line.startswith('    '):
                    line = f"    {line}"
                lines.append(line)

    # Remove duplicate or unwanted auth lines
    cleaned_lines: List[str] = []
    seen_auth_lines = set()
    auth_keys = {
        "preferredauthentications password",
        "pubkeyauthentication no",
    }
    for line in lines:
        key = line.strip().lower()
        if auth_method == 0 and key in auth_keys:
            # Strip password-only directives when using key-based auth
            continue
        if auth_method != 0 and key in auth_keys:
            if key in seen_auth_lines:
                # Avoid duplicates for password auth
                continue
            seen_auth_lines.add(key)
        cleaned_lines.append(line)

    return '\n'.join(cleaned_lines)


def merged_block_lines(old_block: Optional[HostBlock],
                       new_data: Dict[str, Any]) -> List[str]:
    """Render the edited Host block surgically instead of wholesale.

    Managed directives (MANAGED_HOST_OPTIONS) are re-emitted from *new_data*
    at the position of the first managed line in the old block. Comments,
    blank lines, and unknown directives keep their authored form and
    position. Unknown directives are reconciled against the payload's
    ``extra_ssh_config``: an entry still present there keeps its authored
    line; an entry the user removed in the editor is dropped; entries the
    user added are appended. A payload with no ``extra_ssh_config`` key
    (programmatic callers) preserves every authored unknown line.
    """
    if old_block is None:
        # New block: the full formatter already handles extras (verbatim,
        # indented) and its auth-directive cleanup pass.
        formatted = format_ssh_config_entry(new_data).split('\n')
        return [ln + '\n' for ln in formatted]

    managed_only = dict(new_data)
    managed_only['extra_ssh_config'] = ''
    formatted = format_ssh_config_entry(managed_only).split('\n')
    header, managed_body = formatted[0] + '\n', [ln + '\n' for ln in formatted[1:]]

    has_extra_key = 'extra_ssh_config' in new_data
    # (normalized (key, value), authored line) — match on the former, emit
    # the latter so casing/spelling is never rewritten.
    remaining_extras: List[Tuple[Tuple[str, str], str]] = []
    for line in str(new_data.get('extra_ssh_config') or '').splitlines():
        stripped_extra = line.strip()
        key, value = _split_config_option(stripped_extra)
        if key is not None:
            remaining_extras.append(((key.lower(), value), stripped_extra))

    out_body: List[str] = []
    managed_inserted = False

    def _insert_managed():
        nonlocal managed_inserted
        if not managed_inserted:
            out_body.extend(managed_body)
            managed_inserted = True

    for raw in old_block.lines[1:]:
        stripped = raw.strip()
        if not stripped:
            out_body.append(raw)
            continue
        if stripped.startswith('#'):
            if stripped.startswith('# sshpilot:PreCommand '):
                _insert_managed()  # re-emitted from data
            else:
                out_body.append(raw)
            continue
        key = _split_keyword(stripped)[0]
        if key in MANAGED_HOST_OPTIONS:
            _insert_managed()
            continue
        # Unknown directive: keep the authored line when the payload still
        # carries it (or carries no extras at all).
        if not has_extra_key:
            out_body.append(raw)
            continue
        parsed_key, parsed_value = _split_config_option(stripped)
        entry = (parsed_key.lower(), parsed_value) if parsed_key else None
        match = next((item for item in remaining_extras if item[0] == entry), None)
        if match is not None:
            remaining_extras.remove(match)
            out_body.append(raw)
        # else: removed in the editor — drop it

    _insert_managed()
    out_body.extend(f"    {authored}\n" for _entry, authored in remaining_extras)
    return [header] + out_body

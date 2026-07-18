"""Extended coverage for the NATIVE-ONLY ssh_connection_builder and connect().

The builder no longer emits per-host SSH settings into the command (port
forwarding, ProxyCommand/Jump, ForwardAgent flag, CertificateFile,
IdentitiesOnly, PreferredAuthentications, extra_ssh_config, known_hosts,
ExitOnForwardFailure, ...) — those now come from ~/.ssh/config. These tests
exercise the native contract: command shape, host token, ssh_overrides, batch
mode, and the auth env/options resolved by resolve_native_auth.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional


from sshpilot.connection_manager import Connection, ConnectionManager
from sshpilot.ssh_connection_builder import (
    ConnectionContext,
    NativeAuth,
    build_ssh_connection,
    build_native_command,
    resolve_native_auth,
)

asyncio.set_event_loop(asyncio.new_event_loop())


def _host_index(cmd: List[str]) -> int:
    idx = 1
    takes_value = {'-o', '-i', '-F', '-p', '-P', '-D', '-L', '-R'}
    while idx < len(cmd):
        token = cmd[idx]
        if token in takes_value and idx + 1 < len(cmd):
            idx += 2
            continue
        if token.startswith('-'):
            idx += 1
            continue
        return idx
    return -1


def _flag_index(cmd: List[str], flag: str) -> int:
    try:
        return cmd.index(flag)
    except ValueError:
        return -1


def _has_o_option(cmd: List[str], option: str) -> bool:
    needle = option if '=' in option else f'{option}='
    for i, token in enumerate(cmd):
        if token == '-o' and i + 1 < len(cmd):
            value = cmd[i + 1]
            if value == option or value.startswith(needle):
                return True
    return False


def _build(
    conn_data: dict,
    *,
    command_type: str = 'ssh',
    connection_manager=None,
    config=None,
    **ctx_kwargs,
) -> tuple[list[str], object]:
    conn = Connection(conn_data)
    ctx = ConnectionContext(
        connection=conn,
        connection_manager=connection_manager,
        config=config,
        command_type=command_type,
        **ctx_kwargs,
    )
    result = build_ssh_connection(ctx)
    return result.command, result


def _build_from(conn, *, command_type: str = 'ssh', connection_manager=None, config=None):
    """Like _build but for a pre-constructed Connection (so the test can set
    attributes such as resolved_identity_files first)."""
    ctx = ConnectionContext(
        connection=conn,
        connection_manager=connection_manager,
        config=config,
        command_type=command_type,
    )
    result = build_ssh_connection(ctx)
    return result.command, result


class _ConfigStub:
    def __init__(self, ssh_cfg: Optional[dict] = None, settings: Optional[dict] = None):
        self._ssh_cfg = ssh_cfg or {}
        self._settings = settings or {}

    def get_ssh_config(self) -> dict:
        return dict(self._ssh_cfg)

    def get_setting(self, key, default=None):
        return self._settings.get(key, default)


# --- argv shape: per-host settings are NOT emitted (live in ~/.ssh/config) ---


def test_port_forwarding_rules_not_emitted_to_command():
    # Port forwarding now lives in ~/.ssh/config; rules are vestigial.
    rules = [
        {'type': 'local', 'enabled': True, 'listen_addr': '127.0.0.1',
         'listen_port': 8080, 'remote_host': 'internal', 'remote_port': 80},
        {'type': 'remote', 'enabled': True, 'listen_addr': '0.0.0.0',
         'listen_port': 9000, 'local_host': 'db.local', 'local_port': 5432},
        {'type': 'dynamic', 'enabled': True, 'listen_addr': '127.0.0.1',
         'listen_port': 1080},
    ]
    cmd, _ = _build(
        {'host': 'fw.example', 'hostname': 'fw.example', 'username': 'u'},
        port_forwarding_rules=rules,
    )
    assert '-L' not in cmd
    assert '-R' not in cmd
    assert '-D' not in cmd


def test_local_command_not_emitted_to_command():
    # LocalCommand/PermitLocalCommand now live in ~/.ssh/config.
    cmd, _ = _build(
        {'host': 'lc.example', 'hostname': 'lc.example'},
        local_command='echo local-ready',
    )
    assert not _has_o_option(cmd, 'PermitLocalCommand=yes')
    assert not _has_o_option(cmd, 'LocalCommand')


# --- authentication ---


def test_password_auth_with_stored_password_uses_askpass():
    cmd, result = _build(
        {
            'host': 'pw.example',
            'hostname': 'pw.example',
            'username': 'u',
            'auth_method': 1,
            'password': 'secret',
        },
    )
    assert result.use_sshpass is False
    assert result.password == 'secret'
    assert result.use_askpass is True
    assert result.env.get('SSH_ASKPASS_REQUIRE') == 'prefer'
    # PreferredAuthentications/PubkeyAuthentication now come from ~/.ssh/config.
    assert not _has_o_option(cmd, 'PreferredAuthentications')
    # No agent-bypass for password mode.
    assert 'IdentityAgent=none' not in cmd


def test_key_auth_with_stored_password_uses_askpass_not_sshpass():
    # Key auth + a stored password (no saved key passphrase) -> askpass for the
    # login password; MFA declined by the helper falls back to the TTY.
    conn = Connection({
        'host': 'combo.example',
        'hostname': 'combo.example',
        'username': 'u',
        'auth_method': 0,
        'password': 'backup',
    })
    conn.resolved_identity_files = []  # no saved passphrase
    cmd, result = _build_from(conn)
    assert result.use_sshpass is False
    assert result.password == 'backup'
    assert result.use_askpass is True
    assert result.env.get('SSH_ASKPASS')
    assert result.env.get('SSH_ASKPASS_REQUIRE') == 'prefer'
    assert not _has_o_option(cmd, 'PreferredAuthentications')


def test_password_auth_without_stored_password_not_sshpass():
    cmd, result = _build(
        {
            'host': 'tty.example',
            'hostname': 'tty.example',
            'auth_method': 1,
        },
    )
    # auth_method=1 with no stored password => password_mode, but nothing to feed.
    assert result.use_sshpass is False
    assert result.password is None
    assert result.use_askpass is False
    # No -t injected by native builder.
    assert '-t' not in cmd and '-tt' not in cmd


def test_in_memory_password_used_when_password_auth_selected():
    cmd, result = _build(
        {
            'host': 'mem.example',
            'hostname': 'mem.example',
            'username': 'u',
            'auth_method': 1,
            'password': 'inline-secret',
        },
    )
    assert result.use_sshpass is False
    assert result.use_askpass is True
    assert result.password == 'inline-secret'
    # Staged for askpass: IPC id when the prompt server is up, else a temp file.
    assert (
        result.env.get('SSHPILOT_SESSION_PASSWORD_ID')
        or result.env.get('SSHPILOT_SESSION_PASSWORD_FILE')
    )


# --- resolve_native_auth modes ---


def test_resolve_native_auth_password_mode():
    conn = Connection({
        'host': 'h', 'hostname': 'h', 'username': 'u',
        'auth_method': 1, 'password': 'p',
    })
    auth = resolve_native_auth(conn)
    assert isinstance(auth, NativeAuth)
    assert auth.password_mode is True
    assert auth.use_sshpass is False
    assert auth.password == 'p'
    assert auth.use_askpass is True
    assert auth.extra_opts == []
    assert auth.env.get('SSH_ASKPASS')
    assert auth.env.get('SSH_ASKPASS_REQUIRE') == 'prefer'


def test_resolve_native_auth_askpass_disabled():
    conn = Connection({'host': 'h', 'hostname': 'h', 'auth_method': 0})
    auth = resolve_native_auth(conn, None, _ConfigStub(settings={'use-askpass': False}))
    assert auth.use_askpass is False
    assert auth.use_sshpass is False
    assert auth.extra_opts == []
    assert 'SSH_ASKPASS' not in auth.env
    assert 'SSH_ASKPASS_REQUIRE' not in auth.env


def test_resolve_native_auth_key_mode_saved_passphrase_uses_askpass(monkeypatch):
    import sshpilot.ssh_connection_builder as scb
    monkeypatch.setattr(scb, 'lookup_passphrase', lambda _p: 'pp')
    conn = Connection({'host': 'h', 'hostname': 'h', 'auth_method': 0})
    conn.resolved_identity_files = ['/home/u/.ssh/k']
    auth = resolve_native_auth(conn)
    assert auth.use_askpass is True
    assert auth.use_sshpass is False
    assert auth.extra_opts == []
    assert auth.env.get('SSH_ASKPASS')


def test_resolve_native_auth_key_mode_nothing_saved_no_askpass(monkeypatch):
    import sshpilot.ssh_connection_builder as scb
    monkeypatch.setattr(scb, 'lookup_passphrase', lambda _p: '')
    monkeypatch.setattr(scb, '_get_stored_password', lambda _c, _m=None: None)
    conn = Connection({'host': 'h', 'hostname': 'h', 'auth_method': 0})
    conn.resolved_identity_files = ['/home/u/.ssh/k']
    auth = resolve_native_auth(conn)
    assert auth.use_askpass is False
    assert auth.use_sshpass is False
    assert 'SSH_ASKPASS' not in auth.env
    assert 'SSH_ASKPASS_REQUIRE' not in auth.env


def test_resolve_native_auth_key_mode_probe_error_failsafe_askpass(monkeypatch):
    import sshpilot.ssh_connection_builder as scb

    def boom(_p):
        raise RuntimeError("keyring down")

    monkeypatch.setattr(scb, 'lookup_passphrase', boom)
    conn = Connection({'host': 'h', 'hostname': 'h', 'auth_method': 0})
    conn.resolved_identity_files = ['/home/u/.ssh/k']
    auth = resolve_native_auth(conn)
    # Fail-safe: can't tell -> keep askpass on (never regress autofill).
    assert auth.use_askpass is True


# --- proxy / agent: now sourced from ~/.ssh/config, not the command ---


def test_proxy_and_agent_settings_not_emitted_to_command():
    # ProxyCommand/ProxyJump/ForwardAgent flags now live in ~/.ssh/config.
    cmd, _ = _build(
        {
            'host': 'jump.example',
            'hostname': 'jump.example',
            'proxy_command': 'ssh -W %h:%p bastion',
            'proxy_jump': ['b1', 'b2'],
            'forward_agent': True,
        },
    )
    assert not _has_o_option(cmd, 'ProxyCommand')
    assert not _has_o_option(cmd, 'ProxyJump')
    assert not _has_o_option(cmd, 'ForwardAgent')
    assert '-A' not in cmd
    # forward_agent still suppresses the agent bypass.
    assert 'IdentityAgent=none' not in cmd


# --- modes ---


def test_native_mode_resolves_host_identifier_and_overrides():
    cmd, result = _build(
        {
            'host': 'real.host',
            'hostname': 'real.host',
            'nickname': 'alias',
        },
        native_mode=True,
        config=_ConfigStub({'ssh_overrides': ['-o', 'ConnectTimeout=5']}),
    )
    # Host token is resolved via resolve_host_identifier() => 'real.host'.
    assert cmd[-1] == 'real.host'
    assert 'ConnectTimeout=5' in cmd
    # ssh_overrides precede the host.
    assert cmd.index('ConnectTimeout=5') < _host_index(cmd)


def test_extra_args_precede_host():
    cmd, _ = _build(
        {'host': 'ea.example', 'hostname': 'ea.example'},
        extra_args=['-N', '-f'],
    )
    host_idx = _host_index(cmd)
    assert '-N' in cmd and cmd.index('-N') < host_idx
    assert '-f' in cmd and cmd.index('-f') < host_idx


# --- build_native_command plain shape (no auth applied) ---


def test_build_native_command_plain_ssh():
    conn = Connection({'host': 'plain.example', 'hostname': 'plain.example', 'username': 'u'})
    cmd = build_native_command(conn)
    assert cmd == ['ssh', 'plain.example']
    assert 'IdentityAgent=none' not in cmd


def test_build_native_command_binary_selection():
    conn = Connection({'host': 'b.example', 'hostname': 'b.example'})
    assert build_native_command(conn, command_type='scp')[0] == 'scp'
    assert build_native_command(conn, command_type='sftp')[0] == 'sftp'
    assert build_native_command(conn, command_type='ssh-copy-id')[0] == 'ssh-copy-id'
    assert build_native_command(conn, command_type='unknown')[0] == 'ssh'


def test_build_native_command_overrides_and_remote_command():
    conn = Connection({'host': 'o.example', 'hostname': 'o.example'})
    cmd = build_native_command(
        conn,
        app_config=_ConfigStub({'ssh_overrides': ['-o', 'ConnectTimeout=7']}),
        remote_command='uptime',
        extra_args=['-N'],
    )
    assert cmd[0] == 'ssh'
    assert 'ConnectTimeout=7' in cmd
    assert '-N' in cmd
    # remote command is the final raw token.
    assert cmd[-1] == 'uptime'
    host_idx = _host_index(cmd)
    assert cmd.index('ConnectTimeout=7') < host_idx
    assert cmd.index('-N') < host_idx


# --- app config: batch mode + overrides ---


def test_app_config_batch_mode_added_for_key_auth():
    cfg = _ConfigStub({'batch_mode': True})
    cmd, _ = _build(
        {'host': 'appcfg.example', 'hostname': 'appcfg.example', 'auth_method': 0},
        config=cfg,
    )
    assert _has_o_option(cmd, 'BatchMode=yes')


def test_app_config_batch_mode_skipped_for_password_mode():
    cfg = _ConfigStub({'batch_mode': True})
    cmd, _ = _build(
        {
            'host': 'pwbatch.example',
            'hostname': 'pwbatch.example',
            'auth_method': 1,
            'password': 'p',
        },
        config=cfg,
    )
    # BatchMode must never be added for password mode (it needs to prompt).
    assert not _has_o_option(cmd, 'BatchMode=yes')


def test_app_config_batch_mode_skipped_for_combined_auth_pty_password(monkeypatch):
    import sshpilot.ssh_connection_builder as scb

    cfg = _ConfigStub({'batch_mode': True})
    conn = Connection({
        'host': 'bothbatch.example',
        'hostname': 'bothbatch.example',
        'auth_method': 0,
        'password': 'account-password',
    })
    conn.resolved_identity_files = ['/home/u/.ssh/k']
    monkeypatch.setattr(scb, 'lookup_passphrase', lambda _p: 'key-passphrase')
    monkeypatch.setattr(scb, 'ensure_key_in_agent', lambda _p, *, force=False, lifetime=0: True)

    cmd, result = _build_from(conn, config=cfg)

    assert result.use_sshpass is False
    assert result.use_askpass is True
    assert result.password == 'account-password'
    # Askpass password delivery needs prompts enabled (BatchMode would skip them).
    assert not _has_o_option(cmd, 'BatchMode=yes')


def test_keepalive_and_timeout_only_via_ssh_overrides():
    # Native mode no longer derives ServerAlive*/ConnectTimeout from app config
    # keys; they must be passed verbatim via ssh_overrides (written to config).
    cfg = _ConfigStub({'keepalive_interval': 33, 'connection_timeout': 12})
    cmd, _ = _build(
        {'host': 'ka.example', 'hostname': 'ka.example'},
        config=cfg,
    )
    assert not _has_o_option(cmd, 'ServerAliveInterval')
    assert not _has_o_option(cmd, 'ConnectTimeout')


# --- connect() integration ---


def test_connect_stores_ssh_cmd_and_env(monkeypatch):
    captured = {}

    def fake_build(ctx):
        captured['manager'] = ctx.connection_manager
        from sshpilot.ssh_connection_builder import SSHConnectionCommand

        return SSHConnectionCommand(
            command=['ssh', '-o', 'BatchMode=yes', 'myhost'],
            env={'SSH_ASKPASS': '/tmp/askpass', 'SSH_ASKPASS_REQUIRE': 'prefer'},
            use_sshpass=False,
            use_askpass=True,
        )

    monkeypatch.setattr('sshpilot.connection_manager.build_ssh_connection', fake_build)
    monkeypatch.setattr('sshpilot.config.Config', lambda: _ConfigStub())

    manager = ConnectionManager.__new__(ConnectionManager)
    manager.known_hosts_path = ''
    manager.connections = []
    conn = Connection({'host': 'myhost', 'hostname': 'myhost'})
    manager._register_connection(conn)

    loop = asyncio.get_event_loop()
    assert loop.run_until_complete(conn.connect()) is True
    assert conn.ssh_cmd == ['ssh', '-o', 'BatchMode=yes', 'myhost']
    assert conn.ssh_env.get('SSH_ASKPASS') == '/tmp/askpass'
    assert captured['manager'] is manager


def test_connect_builds_native_command(tmp_path, monkeypatch):
    # known_hosts is no longer injected into the command in native mode; it lives
    # in ~/.ssh/config. connect() should still produce a minimal native command.
    monkeypatch.setattr('sshpilot.config.Config', lambda: _ConfigStub())

    manager = ConnectionManager.__new__(ConnectionManager)
    manager.known_hosts_path = ''
    manager.connections = []

    conn = Connection({'host': 'khhost', 'hostname': 'khhost', 'auth_method': 0})
    manager._register_connection(conn)

    loop = asyncio.get_event_loop()
    assert loop.run_until_complete(conn.connect()) is True
    assert conn.ssh_cmd[0] == 'ssh'
    assert conn.ssh_cmd[-1] == 'khhost'
    # No known_hosts injected into the command.
    assert not any('UserKnownHostsFile' in part for part in conn.ssh_cmd)


# --- _build_base_ssh_command: per-binary option guards ---


def _base_cmd(command_type, effective_config=None, app_config=None, conn_data=None):
    from sshpilot.ssh_connection_builder import _build_base_ssh_command

    conn = Connection(conn_data or {'host': 'h', 'hostname': 'h'})
    return _build_base_ssh_command(conn, effective_config or {}, app_config, command_type)


def test_base_command_scp_and_copy_id_clear_forwardings():
    assert _has_o_option(_base_cmd('scp'), 'ClearAllForwardings=yes')
    assert _has_o_option(_base_cmd('ssh-copy-id'), 'ClearAllForwardings=yes')
    assert not _has_o_option(_base_cmd('ssh'), 'ClearAllForwardings')
    assert not _has_o_option(_base_cmd('sftp'), 'ClearAllForwardings')


def test_base_command_limits_password_prompts_for_transfers_only():
    # SCP / ssh-copy-id: cancel-once askpass. Interactive ssh keeps OpenSSH default.
    assert _has_o_option(_base_cmd('scp'), 'NumberOfPasswordPrompts=1')
    assert _has_o_option(_base_cmd('ssh-copy-id'), 'NumberOfPasswordPrompts=1')
    assert not _has_o_option(_base_cmd('ssh'), 'NumberOfPasswordPrompts')
    assert not _has_o_option(_base_cmd('sftp'), 'NumberOfPasswordPrompts')


def test_native_sftp_limits_password_prompts_terminal_does_not():
    # File manager uses command_type=sftp on the native builder.
    sftp_cmd, _ = _build(
        {'host': 'fm.example', 'hostname': 'fm.example'},
        command_type='sftp',
    )
    term_cmd, _ = _build(
        {'host': 'term.example', 'hostname': 'term.example'},
        command_type='ssh',
    )
    assert _has_o_option(sftp_cmd, 'NumberOfPasswordPrompts=1')
    assert not _has_o_option(term_cmd, 'NumberOfPasswordPrompts')


def test_base_command_copy_id_skips_unsupported_flags():
    # ssh-copy-id rejects -v/-C/-A and BatchMode defeats its interactive purpose.
    cfg = _ConfigStub({'verbosity': 2, 'compression': True, 'batch_mode': True})
    cmd = _base_cmd('ssh-copy-id', app_config=cfg,
                    effective_config={'forwardagent': 'yes', 'forwardx11': 'yes'})
    assert '-v' not in cmd
    assert '-C' not in cmd
    assert '-A' not in cmd
    assert '-X' not in cmd
    assert not _has_o_option(cmd, 'BatchMode')
    # ...while scp still honors the app-level flags.
    scp_cmd = _base_cmd('scp', app_config=cfg)
    assert '-v' in scp_cmd
    assert '-C' in scp_cmd
    assert _has_o_option(scp_cmd, 'BatchMode=yes')


def test_base_command_copy_id_never_injects_identity(tmp_path):
    # -i for ssh-copy-id selects the key to INSTALL; the config identity must
    # not leak in or it would change which key gets copied.
    key = tmp_path / 'id_test'
    key.write_text('k')
    effective = {'identityfile': [str(key)], 'identitiesonly': 'yes'}
    cmd = _base_cmd('ssh-copy-id', effective_config=effective)
    assert '-i' not in cmd
    assert not _has_o_option(cmd, 'IdentitiesOnly')
    scp_cmd = _base_cmd('scp', effective_config=effective)
    assert '-i' in scp_cmd and str(key) in scp_cmd


def test_base_command_x11_flag_only_for_ssh():
    # scp -X selects the transfer protocol and sftp -X sets an sftp option, so
    # a bare -X must only ever be emitted for ssh.
    effective = {'forwardx11': 'yes'}
    assert '-X' in _base_cmd('ssh', effective_config=effective)
    assert '-X' not in _base_cmd('scp', effective_config=effective)
    assert '-X' not in _base_cmd('sftp', effective_config=effective)
    assert '-X' not in _base_cmd('ssh-copy-id', effective_config=effective)

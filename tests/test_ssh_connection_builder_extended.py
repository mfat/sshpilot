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

import pytest

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


def test_password_auth_with_stored_password_uses_sshpass():
    cmd, result = _build(
        {
            'host': 'pw.example',
            'hostname': 'pw.example',
            'auth_method': 1,
            'password': 'secret',
        },
    )
    assert result.use_sshpass is True
    assert result.password == 'secret'
    assert result.use_askpass is False
    # PreferredAuthentications/PubkeyAuthentication now come from ~/.ssh/config.
    assert not _has_o_option(cmd, 'PreferredAuthentications')
    # No agent-bypass for password mode.
    assert 'IdentityAgent=none' not in cmd


def test_key_auth_ignores_stored_password_and_uses_askpass():
    # Key-based auth is authoritative: a stored/leftover password is irrelevant
    # and must not divert key auth into sshpass (which would suppress askpass).
    cmd, result = _build(
        {
            'host': 'combo.example',
            'hostname': 'combo.example',
            'auth_method': 0,
            'password': 'backup',
        },
    )
    assert result.use_sshpass is False
    assert result.password is None
    assert result.use_askpass is True
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
            'auth_method': 1,
            'password': 'inline-secret',
        },
    )
    assert result.use_sshpass is True
    assert result.password == 'inline-secret'


# --- resolve_native_auth modes ---


def test_resolve_native_auth_password_mode():
    conn = Connection({'host': 'h', 'hostname': 'h', 'auth_method': 1, 'password': 'p'})
    auth = resolve_native_auth(conn)
    assert isinstance(auth, NativeAuth)
    assert auth.password_mode is True
    assert auth.use_sshpass is True
    assert auth.password == 'p'
    assert auth.use_askpass is False
    assert auth.extra_opts == []
    assert 'SSH_ASKPASS' not in auth.env
    assert auth.env.get('SSH_ASKPASS_REQUIRE') == 'never'


def test_resolve_native_auth_askpass_disabled():
    conn = Connection({'host': 'h', 'hostname': 'h', 'auth_method': 0})
    auth = resolve_native_auth(conn, None, _ConfigStub(settings={'use-askpass': False}))
    assert auth.use_askpass is False
    assert auth.use_sshpass is False
    assert auth.extra_opts == []
    assert 'SSH_ASKPASS' not in auth.env
    assert 'SSH_ASKPASS_REQUIRE' not in auth.env


def test_resolve_native_auth_key_mode_uses_askpass():
    conn = Connection({'host': 'h', 'hostname': 'h', 'auth_method': 0})
    auth = resolve_native_auth(conn)
    assert auth.use_askpass is True
    assert auth.use_sshpass is False
    assert auth.extra_opts == []
    assert auth.env.get('SSH_ASKPASS')


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

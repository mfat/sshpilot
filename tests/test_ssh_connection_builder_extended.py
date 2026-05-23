"""Extended coverage for ssh_connection_builder and Connection.connect()."""

from __future__ import annotations

import asyncio
import os
import shlex
from typing import List, Optional

import pytest

from sshpilot.connection_manager import Connection, ConnectionManager
from sshpilot.ssh_connection_builder import ConnectionContext, build_ssh_connection

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
    def __init__(self, ssh_cfg: Optional[dict] = None):
        self._ssh_cfg = ssh_cfg or {}

    def get_ssh_config(self) -> dict:
        return dict(self._ssh_cfg)


# --- argv ordering ---


@pytest.mark.parametrize(
    'rule,flag,spec_fragment',
    [
        (
            {
                'type': 'local',
                'enabled': True,
                'listen_addr': '127.0.0.1',
                'listen_port': 8080,
                'remote_host': 'internal',
                'remote_port': 80,
            },
            '-L',
            '127.0.0.1:8080:internal:80',
        ),
        (
            {
                'type': 'remote',
                'enabled': True,
                'listen_addr': '0.0.0.0',
                'listen_port': 9000,
                'local_host': 'db.local',
                'local_port': 5432,
            },
            '-R',
            '0.0.0.0:9000:db.local:5432',
        ),
        (
            {
                'type': 'dynamic',
                'enabled': True,
                'listen_addr': '127.0.0.1',
                'listen_port': 1080,
            },
            '-D',
            '127.0.0.1:1080',
        ),
    ],
)
def test_port_forwarding_flags_precede_host(rule, flag, spec_fragment):
    conn_data = {'host': 'fw.example', 'hostname': 'fw.example', 'username': 'u'}
    cmd, _ = _build(
        conn_data,
        port_forwarding_rules=[rule],
    )
    host_idx = _host_index(cmd)
    flag_idx = _flag_index(cmd, flag)
    assert host_idx >= 0
    assert flag_idx >= 0
    assert flag_idx < host_idx
    assert any(spec_fragment in part for part in cmd)


def test_port_forwarding_ipv6_listen_addr_is_bracketed():
    rule = {
        'type': 'dynamic',
        'enabled': True,
        'listen_addr': '::1',
        'listen_port': 1081,
    }
    cmd, _ = _build(
        {'host': 'v6.example', 'hostname': 'v6.example'},
        port_forwarding_rules=[rule],
    )
    assert any('[::1]:1081' in part for part in cmd)


def test_disabled_port_forward_rule_is_skipped():
    rules = [
        {
            'type': 'local',
            'enabled': False,
            'listen_addr': '127.0.0.1',
            'listen_port': 9999,
            'remote_host': 'x',
            'remote_port': 1,
        },
        {
            'type': 'dynamic',
            'enabled': True,
            'listen_addr': 'localhost',
            'listen_port': 1082,
        },
    ]
    cmd, _ = _build(
        {'host': 'mix.example', 'hostname': 'mix.example'},
        port_forwarding_rules=rules,
    )
    assert '-L' not in cmd
    assert '-D' in cmd


def test_local_command_options_precede_host():
    cmd, _ = _build(
        {'host': 'lc.example', 'hostname': 'lc.example'},
        local_command='echo local-ready',
    )
    host_idx = _host_index(cmd)
    permit_idx = _flag_index(cmd, '-o')
    assert _has_o_option(cmd, 'PermitLocalCommand=yes')
    assert _has_o_option(cmd, 'LocalCommand=echo local-ready')
    assert permit_idx < host_idx


# --- authentication ---


def test_password_auth_forces_password_preferred_and_optional_pubkey_off():
    cmd, result = _build(
        {
            'host': 'pw.example',
            'hostname': 'pw.example',
            'auth_method': 1,
            'pubkey_auth_no': True,
            'password': 'secret',
        },
    )
    assert result.use_sshpass is True
    assert _has_o_option(cmd, 'PreferredAuthentications=password')
    assert _has_o_option(cmd, 'PubkeyAuthentication=no')


def test_key_auth_with_stored_password_adds_fallback_auths_not_sshpass():
    cmd, result = _build(
        {
            'host': 'combo.example',
            'hostname': 'combo.example',
            'auth_method': 0,
            'password': 'backup',
        },
    )
    assert result.use_sshpass is False
    assert _has_o_option(
        cmd,
        'PreferredAuthentications=gssapi-with-mic,hostbased,publickey,keyboard-interactive,password',
    )


def test_password_auth_without_stored_password_adds_interactive_tty():
    cmd, result = _build(
        {
            'host': 'tty.example',
            'hostname': 'tty.example',
            'auth_method': 1,
        },
    )
    assert result.use_sshpass is False
    host_idx = _host_index(cmd)
    assert '-t' in cmd[:host_idx]


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
    assert _has_o_option(cmd, 'PreferredAuthentications=password')


# --- proxy / agent / certs ---


def test_connection_level_proxy_command_merged():
    cmd, _ = _build(
        {
            'host': 'jump.example',
            'hostname': 'jump.example',
            'proxy_command': 'ssh -W %h:%p bastion',
        },
    )
    assert _has_o_option(cmd, 'ProxyCommand=ssh -W %h:%p bastion')


def test_connection_level_proxy_jump_list_merged():
    cmd, _ = _build(
        {
            'host': 'pj.example',
            'hostname': 'pj.example',
            'proxy_jump': ['b1', 'b2'],
        },
    )
    assert _has_o_option(cmd, 'ProxyJump=b1,b2')


def test_forward_agent_adds_flag_and_option():
    cmd, _ = _build(
        {
            'host': 'agent.example',
            'hostname': 'agent.example',
            'forward_agent': True,
        },
    )
    host_idx = _host_index(cmd)
    assert '-A' in cmd[:host_idx]
    assert _has_o_option(cmd, 'ForwardAgent=yes')


def test_certificate_file_option_before_host(tmp_path):
    key_path = tmp_path / 'id_ed25519'
    cert_path = tmp_path / 'id_ed25519-cert.pub'
    key_path.write_text('not-a-real-key')
    cert_path.write_text('not-a-real-cert')

    cmd, _ = _build(
        {
            'host': 'cert.example',
            'hostname': 'cert.example',
            'keyfile': str(key_path),
            'certificate': str(cert_path),
            'key_select_mode': 2,
            'auth_method': 0,
        },
    )
    host_idx = _host_index(cmd)
    cert_idx = next(
        i for i, part in enumerate(cmd) if part.startswith('CertificateFile=')
    )
    assert cert_idx < host_idx


def test_explicit_key_with_identities_only_mode(tmp_path):
    key_path = tmp_path / 'only'
    key_path.write_text('fake-key')

    cmd, _ = _build(
        {
            'host': 'key.example',
            'hostname': 'key.example',
            'keyfile': str(key_path),
            'key_select_mode': 1,
            'auth_method': 0,
        },
    )
    assert str(key_path) in cmd
    assert _has_o_option(cmd, 'IdentitiesOnly=yes')


# --- modes ---


def test_quick_connect_uses_verbatim_command_without_askpass():
    command = 'ssh -p 2222 -J bastion user@target'
    cmd, result = _build(
        {
            'host': 'target',
            'quick_connect_command': command,
        },
        quick_connect_mode=True,
        quick_connect_command=command,
    )
    assert cmd == shlex.split(command)
    assert result.use_askpass is False
    assert 'SSH_ASKPASS' not in result.env


def test_native_mode_resolves_host_identifier():
    cmd, result = _build(
        {
            'host': 'real.host',
            'hostname': 'real.host',
            'nickname': 'alias',
        },
        native_mode=True,
        config=_ConfigStub({'ssh_overrides': ['-o', 'ConnectTimeout=5']}),
    )
    assert result.use_askpass is False
    assert cmd[-1] == 'real.host'
    assert 'ConnectTimeout=5' in cmd


def test_scp_command_type_uses_scp_binary():
    cmd, _ = _build(
        {'host': 'scp.example', 'hostname': 'scp.example'},
        command_type='scp',
    )
    assert cmd[0] == 'scp'


def test_ssh_copy_id_target_format():
    cmd, _ = _build(
        {
            'host': 'copyid',
            'hostname': 'copyid.example',
            'username': 'alice',
        },
        command_type='ssh-copy-id',
    )
    assert cmd[0] == 'ssh-copy-id'
    assert cmd[-1] == 'alice@copyid.example'


# --- config injection ---


def test_extra_ssh_config_lines_become_options():
    extra = 'Compression yes\nServerAliveInterval 30'
    cmd, _ = _build(
        {'host': 'extra.example', 'hostname': 'extra.example'},
        extra_ssh_config=extra,
    )
    assert _has_o_option(cmd, 'Compression=yes')
    assert _has_o_option(cmd, 'ServerAliveInterval=30')


def test_known_hosts_path_injected(tmp_path):
    kh = tmp_path / 'known_hosts'
    kh.write_text('')

    cmd, _ = _build(
        {'host': 'kh.example', 'hostname': 'kh.example'},
        known_hosts_path=str(kh),
    )
    assert _has_o_option(cmd, f'UserKnownHostsFile={kh}')


def test_app_config_batch_mode_and_keepalive(tmp_path, monkeypatch):
    cfg = _ConfigStub(
        {
            'batch_mode': True,
            'keepalive_interval': 33,
            'keepalive_count_max': 4,
            'connection_timeout': 12,
        }
    )
    cmd, _ = _build(
        {'host': 'appcfg.example', 'hostname': 'appcfg.example'},
        config=cfg,
    )
    assert _has_o_option(cmd, 'BatchMode=yes')
    assert _has_o_option(cmd, 'ServerAliveInterval=33')
    assert _has_o_option(cmd, 'ServerAliveCountMax=4')
    assert _has_o_option(cmd, 'ConnectTimeout=12')


def test_exit_on_forward_failure_always_present():
    cmd, _ = _build({'host': 'eof.example', 'hostname': 'eof.example'})
    assert _has_o_option(cmd, 'ExitOnForwardFailure=yes')


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


def test_connect_passes_known_hosts_from_manager(tmp_path, monkeypatch):
    kh = tmp_path / 'known_hosts'
    kh.write_text('')

    monkeypatch.setattr('sshpilot.config.Config', lambda: _ConfigStub())

    manager = ConnectionManager.__new__(ConnectionManager)
    manager.known_hosts_path = str(kh)
    manager.connections = []

    conn = Connection({'host': 'khhost', 'hostname': 'khhost'})
    manager._register_connection(conn)

    loop = asyncio.get_event_loop()
    loop.run_until_complete(conn.connect())

    assert any(f'UserKnownHostsFile={kh}' in part for part in conn.ssh_cmd)

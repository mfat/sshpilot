"""Tests for unified SSH command construction."""

import asyncio

from sshpilot.connection_manager import Connection
from sshpilot.ssh_connection_builder import ConnectionContext, build_ssh_connection


asyncio.set_event_loop(asyncio.new_event_loop())


def _host_index(cmd):
    """Index of the SSH target host token (first non-option after ssh binary)."""
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


def test_ssh_options_precede_host_and_remote_command():
    conn = Connection(
        {
            'host': 'example.com',
            'hostname': 'example.com',
            'username': 'alice',
            'remote_command': 'uptime',
        }
    )
    ctx = ConnectionContext(
        connection=conn,
        connection_manager=None,
        config=None,
        command_type='ssh',
        remote_command='uptime',
    )
    result = build_ssh_connection(ctx)
    cmd = result.command
    host_idx = _host_index(cmd)
    assert host_idx >= 0
    assert cmd[host_idx] in ('example.com', 'example')
    assert cmd[host_idx + 1] == 'uptime ; exec $SHELL -l'
    t_positions = [i for i, t in enumerate(cmd) if t in ('-t', '-tt')]
    assert t_positions, 'remote command should request a TTY'
    assert max(t_positions) < host_idx


def test_sshpass_only_when_password_auth_selected():
    conn = Connection(
        {
            'host': 'example.com',
            'hostname': 'example.com',
            'username': 'alice',
            'auth_method': 0,
            'password': 'secret',
        }
    )
    ctx = ConnectionContext(connection=conn, command_type='ssh')
    result = build_ssh_connection(ctx)
    assert result.use_sshpass is False

    conn.auth_method = 1
    ctx = ConnectionContext(connection=conn, command_type='ssh')
    result = build_ssh_connection(ctx)
    assert result.use_sshpass is True
    assert result.password == 'secret'


def test_build_ssh_connection_reads_password_via_manager(monkeypatch):
    from sshpilot.connection_manager import ConnectionManager

    class DummyConfig:
        def get_ssh_config(self):
            return {}

    manager = ConnectionManager.__new__(ConnectionManager)
    manager.config = DummyConfig()
    manager.connections = []
    manager.known_hosts_path = ''
    monkeypatch.setattr(
        manager,
        'get_password',
        lambda host, user: 'from-vault' if host and user == 'bob' else None,
    )

    conn = Connection(
        {
            'host': 'vault.example',
            'hostname': 'vault.example',
            'username': 'bob',
            'auth_method': 1,
        }
    )
    manager._register_connection(conn)

    ctx = ConnectionContext(
        connection=conn,
        connection_manager=conn._connection_manager,
        command_type='ssh',
    )
    built = build_ssh_connection(ctx)
    assert built.use_sshpass is True
    assert built.password == 'from-vault'

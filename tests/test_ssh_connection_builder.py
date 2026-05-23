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
    # With no SSH config file, builder falls back to user@hostname form.
    assert cmd[host_idx] in ('example.com', 'alice@example.com')
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


def _forwarding_conn():
    return Connection({'host': 'srv', 'hostname': 'srv', 'username': 'u'})


def _build_forwarding(rules=None, extra_args=None):
    ctx = ConnectionContext(
        connection=_forwarding_conn(),
        connection_manager=None,
        config=None,
        command_type='ssh',
        port_forwarding_rules=rules or [],
        extra_args=extra_args or [],
    )
    return build_ssh_connection(ctx).command


def test_dynamic_forwarding_flag_before_host():
    cmd = _build_forwarding([
        {'type': 'dynamic', 'listen_addr': 'localhost', 'listen_port': 1080, 'enabled': True},
    ])
    host_idx = _host_index(cmd)
    d_idx = [i for i, t in enumerate(cmd) if t == '-D']
    assert d_idx, f"no -D in command: {cmd}"
    assert all(i < host_idx for i in d_idx), f"-D after host in: {cmd}"
    assert any(cmd[i + 1] == 'localhost:1080' for i in d_idx)


def test_local_forwarding_flag_before_host():
    cmd = _build_forwarding([
        {'type': 'local', 'listen_addr': 'localhost', 'listen_port': 8080,
         'remote_host': 'db', 'remote_port': 5432, 'enabled': True},
    ])
    host_idx = _host_index(cmd)
    l_idx = [i for i, t in enumerate(cmd) if t == '-L']
    assert l_idx, f"no -L in command: {cmd}"
    assert all(i < host_idx for i in l_idx), f"-L after host in: {cmd}"
    assert any('8080' in cmd[i + 1] and '5432' in cmd[i + 1] for i in l_idx)


def test_remote_forwarding_flag_before_host():
    cmd = _build_forwarding([
        {'type': 'remote', 'listen_addr': 'localhost', 'listen_port': 9090,
         'local_host': 'localhost', 'local_port': 9090, 'enabled': True},
    ])
    host_idx = _host_index(cmd)
    r_idx = [i for i, t in enumerate(cmd) if t == '-R']
    assert r_idx, f"no -R in command: {cmd}"
    assert all(i < host_idx for i in r_idx), f"-R after host in: {cmd}"
    assert any('9090' in cmd[i + 1] for i in r_idx)


def test_forwarding_via_extra_args_before_host():
    """Flags passed via extra_args (as start_local/remote_forwarding does) land before host."""
    for flag, spec in [('-L', 'localhost:8080:db:5432'), ('-R', 'localhost:9090:localhost:9090')]:
        cmd = _build_forwarding(extra_args=['-N', flag, spec, '-f'])
        host_idx = _host_index(cmd)
        flag_positions = [i for i, t in enumerate(cmd) if t == flag]
        assert flag_positions, f"no {flag} in command: {cmd}"
        assert all(p < host_idx for p in flag_positions), f"{flag} after host in: {cmd}"
        n_positions = [i for i, t in enumerate(cmd) if t == '-N']
        assert n_positions and all(p < host_idx for p in n_positions), f"-N after host in: {cmd}"


def test_disabled_forwarding_rule_excluded():
    cmd = _build_forwarding([
        {'type': 'local', 'listen_addr': 'localhost', 'listen_port': 8080,
         'remote_host': 'db', 'remote_port': 5432, 'enabled': False},
    ])
    assert '-L' not in cmd


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

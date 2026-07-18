"""Tests for unified (native-only) SSH command construction.

The connection builder is NATIVE-ONLY: per-host settings (port forwarding,
ProxyCommand/Jump, X11, CertificateFile, known_hosts, PreferredAuthentications,
ExitOnForwardFailure, ...) now live in ~/.ssh/config and are NOT emitted to the
command. The builder only owns runtime concerns: app-level ssh_overrides, batch
mode, the host token, an optional raw remote command, and the auth env/options
(askpass + optional agent bypass, or sshpass for a stored password).
"""

import asyncio

from sshpilot.connection_manager import Connection
from sshpilot.ssh_connection_builder import (
    ConnectionContext,
    build_ssh_connection,
    build_native_command,
)


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


def test_ssh_options_precede_host_and_raw_remote_command():
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
    # Host token comes from resolve_host_identifier() (the SSH config alias).
    assert cmd[host_idx] == 'example.com'
    # Remote command is appended RAW after the host: no "; exec $SHELL -l".
    assert cmd[host_idx + 1] == 'uptime'
    assert host_idx + 1 == len(cmd) - 1
    # Native mode never injects -t/-tt; that lives in ~/.ssh/config if wanted.
    assert '-t' not in cmd and '-tt' not in cmd


def test_sshpass_when_password_present_even_for_key_auth():
    """Combined auth: key-auth + stored password returns the password for PTY
    delivery (no sshpass — residual keyboard-interactive must stay visible)."""
    conn = Connection(
        {
            'host': 'example.com',
            'hostname': 'example.com',
            'username': 'alice',
            'auth_method': 0,
            'password': 'secret',
        }
    )
    conn.resolved_identity_files = []  # no key -> no saved passphrase
    ctx = ConnectionContext(connection=conn, command_type='ssh')
    result = build_ssh_connection(ctx)
    # auth_method=0 + stored password -> password for PTY, never sshpass.
    assert result.use_sshpass is False
    assert result.password == 'secret'
    assert result.use_askpass is False
    assert 'SSH_ASKPASS' not in result.env

    # Password auth (auth_method=1) with the same stored password uses sshpass.
    conn.auth_method = 1
    ctx = ConnectionContext(connection=conn, command_type='ssh')
    result = build_ssh_connection(ctx)
    assert result.use_sshpass is True
    assert result.password == 'secret'


def test_key_auth_without_anything_saved_uses_native_prompts():
    # Key auth, no saved passphrase and no saved password -> neither askpass nor
    # sshpass; SSH prompts on the TTY (and can fall back to password naturally).
    conn = Connection(
        {
            'host': 'example.com',
            'hostname': 'example.com',
            'username': 'alice',
            'auth_method': 0,
        }
    )
    conn.resolved_identity_files = []
    result = build_ssh_connection(ConnectionContext(connection=conn))
    assert result.use_sshpass is False
    assert result.password is None
    assert result.use_askpass is False
    assert 'SSH_ASKPASS' not in result.env


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


def test_port_forwarding_rules_are_not_emitted_to_command():
    """port_forwarding_rules is vestigial in native mode; forwarding lives in
    ~/.ssh/config, so no -D/-L/-R is added from rules."""
    cmd = _build_forwarding([
        {'type': 'dynamic', 'listen_addr': 'localhost', 'listen_port': 1080, 'enabled': True},
        {'type': 'local', 'listen_addr': 'localhost', 'listen_port': 8080,
         'remote_host': 'db', 'remote_port': 5432, 'enabled': True},
        {'type': 'remote', 'listen_addr': 'localhost', 'listen_port': 9090,
         'local_host': 'localhost', 'local_port': 9090, 'enabled': True},
    ])
    assert '-D' not in cmd
    assert '-L' not in cmd
    assert '-R' not in cmd


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


# --- new surface: native command shape + auth resolution ---


def test_native_command_minimal_shape():
    """build_native_command yields a plain `ssh <host>` with no auth options."""
    conn = Connection({'host': 'plain.example', 'hostname': 'plain.example', 'username': 'u'})
    cmd = build_native_command(conn)
    assert cmd == ['ssh', 'plain.example']
    # Plain command applies NO auth: no agent bypass.
    assert 'IdentityAgent=none' not in cmd


def test_key_auth_does_not_add_identity_agent_bypass():
    conn = Connection({'host': 'k.example', 'hostname': 'k.example', 'auth_method': 0})
    cmd = build_ssh_connection(ConnectionContext(connection=conn)).command
    assert 'IdentityAgent=none' not in cmd

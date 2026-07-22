"""Tests for CLI connect argv parsing / resolution."""

from types import SimpleNamespace

from sshpilot.cli_connect import (
    CLI_CONNECT_FLAG,
    build_ssh_argv,
    parse_sshpilot_cli,
    resolve_cli_connect,
)


def test_build_ssh_argv_prepends_ssh():
    assert build_ssh_argv(['root@host']) == ['ssh', 'root@host']
    assert build_ssh_argv(['ssh', '-p', '22', 'h']) == ['ssh', '-p', '22', 'h']


def test_parse_leaves_ssh_options_in_remainder():
    opts = parse_sshpilot_cli(['--isolated', '-p', '2222', 'root@example.com'])
    assert opts.isolated is True
    assert opts.ssh_tokens == ['-p', '2222', 'root@example.com']


def test_parse_verbose_and_destination():
    opts = parse_sshpilot_cli(['-v', 'myhost'])
    assert opts.verbose is True
    assert opts.ssh_tokens == ['myhost']


def test_resolve_existing_alias():
    existing = SimpleNamespace(nickname='web', protocol='ssh')
    mgr = SimpleNamespace(
        find_connection_by_nickname=lambda name: existing if name == 'web' else None,
    )
    resolved = resolve_cli_connect(['web'], mgr)
    assert resolved.existing is True
    assert resolved.connection is existing


def test_resolve_user_at_host_is_ephemeral():
    mgr = SimpleNamespace(
        find_connection_by_nickname=lambda name: None,
    )
    resolved = resolve_cli_connect(['root@example.com'], mgr)
    assert resolved.existing is False
    assert resolved.connection.username == 'root'
    assert resolved.connection.hostname == 'example.com'
    assert resolved.connection.data.get(CLI_CONNECT_FLAG) is True
    assert resolved.ssh_argv == ['ssh', 'root@example.com']
    assert list(resolved.connection.ssh_cmd) == ['ssh', 'root@example.com']


def test_validate_rejects_scp_and_empty():
    from sshpilot.cli_connect import validate_cli_tokens
    assert validate_cli_tokens([]) is not None
    assert validate_cli_tokens(['scp', 'a', 'b']) is not None
    assert validate_cli_tokens(['root@host']) is None
    assert validate_cli_tokens(['myalias']) is None


def test_resolve_rejects_non_ssh_like_command():
    mgr = SimpleNamespace(find_connection_by_nickname=lambda name: None)
    try:
        resolve_cli_connect(['scp', 'file', 'user@host:/tmp'], mgr)
        assert False, 'expected ValueError'
    except ValueError as exc:
        assert 'SSH' in str(exc) or 'ssh' in str(exc).lower()


def test_resolve_rejects_empty():
    mgr = SimpleNamespace(find_connection_by_nickname=lambda name: None)
    try:
        resolve_cli_connect([], mgr)
        assert False, 'expected ValueError'
    except ValueError:
        pass


def test_resolve_full_ssh_options():
    mgr = SimpleNamespace(find_connection_by_nickname=lambda name: None)
    resolved = resolve_cli_connect(['-p', '2222', '-i', '~/.ssh/id_ed25519', 'user@host'], mgr)
    assert resolved.connection.port == 2222
    assert resolved.ssh_argv[0] == 'ssh'
    assert '-p' in resolved.ssh_argv

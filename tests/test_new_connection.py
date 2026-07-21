"""Tests for shared new-connection detection."""

from types import SimpleNamespace

from sshpilot.new_connection import (
    SavePromptDismissals,
    identity_key,
    is_new_connection,
    resolve_connection_host_user,
)


def _conn(**kwargs):
    data = dict(kwargs)
    data.setdefault('protocol', 'ssh')
    return SimpleNamespace(
        data=data,
        protocol=data.get('protocol', 'ssh'),
        hostname=data.get('hostname', ''),
        host=data.get('host', data.get('hostname', '')),
        nickname=data.get('nickname', data.get('host', '')),
        username=data.get('username', ''),
        get_effective_host=lambda: (
            data.get('hostname') or data.get('host') or data.get('nickname') or ''
        ),
        resolve_host_identifier=lambda: (
            data.get('host') or data.get('nickname') or data.get('hostname') or ''
        ),
    )


class _Mgr:
    def __init__(self, connections, ssh_config_path='/tmp/fake-ssh-config'):
        self._connections = list(connections)
        self.ssh_config_path = ssh_config_path

    def get_connections(self):
        return list(self._connections)


def test_identity_key_format():
    assert identity_key('Host', 'User') == 'host|User'


def test_is_new_when_manager_empty(monkeypatch):
    monkeypatch.setattr(
        'sshpilot.new_connection.get_effective_ssh_config',
        lambda host, config_file=None: {'hostname': '1.2.3.4', 'user': 'alice'},
    )
    target = _conn(hostname='1.2.3.4', host='1.2.3.4', username='alice')
    assert is_new_connection(target, _Mgr([])) is True


def test_not_new_when_same_host_and_user(monkeypatch):
    monkeypatch.setattr(
        'sshpilot.new_connection.get_effective_ssh_config',
        lambda host, config_file=None: {
            'hostname': 'example.com',
            'user': 'alice',
        },
    )
    saved = _conn(hostname='example.com', host='prod', username='alice', nickname='prod')
    target = _conn(hostname='example.com', host='example.com', username='alice')
    assert is_new_connection(target, _Mgr([saved])) is False


def test_new_when_same_host_different_user(monkeypatch):
    def fake_g(host, config_file=None):
        if host in ('prod', 'example.com'):
            # Saved connection resolves to example.com as alice via its alias;
            # target uses explicit root@example.com.
            if host == 'prod':
                return {'hostname': 'example.com', 'user': 'alice'}
            return {'hostname': 'example.com', 'user': 'root'}
        return {}

    monkeypatch.setattr('sshpilot.new_connection.get_effective_ssh_config', fake_g)
    saved = _conn(hostname='example.com', host='prod', username='alice', nickname='prod')
    target = _conn(hostname='example.com', host='example.com', username='root')
    assert is_new_connection(target, _Mgr([saved])) is True


def test_new_when_hostname_missing_from_config(monkeypatch):
    monkeypatch.setattr(
        'sshpilot.new_connection.get_effective_ssh_config',
        lambda host, config_file=None: {'hostname': host, 'user': 'bob'},
    )
    saved = _conn(hostname='a.example', host='a', username='bob', nickname='a')
    target = _conn(hostname='b.example', host='b.example', username='bob')
    assert is_new_connection(target, _Mgr([saved])) is True


def test_dismissals_are_session_scoped(monkeypatch):
    monkeypatch.setattr(
        'sshpilot.new_connection.get_effective_ssh_config',
        lambda host, config_file=None: {'hostname': 'h', 'user': 'u'},
    )
    d = SavePromptDismissals()
    conn = _conn(hostname='h', host='h', username='u')
    assert d.is_connection_dismissed(conn) is False
    d.dismiss_connection(conn)
    assert d.is_connection_dismissed(conn) is True


def test_resolve_falls_back_to_connection_fields(monkeypatch):
    monkeypatch.setattr(
        'sshpilot.new_connection.get_effective_ssh_config',
        lambda host, config_file=None: {},
    )
    conn = _conn(hostname='Raw.Host', host='alias', username='sam')
    host, user = resolve_connection_host_user(conn)
    assert host == 'raw.host'
    assert user == 'sam'

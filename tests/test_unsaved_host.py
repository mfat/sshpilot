"""Tests for unsaved-host detection (not connection creation)."""

from types import SimpleNamespace

from sshpilot.unsaved_host import (
    SavePromptDismissals,
    identity_key,
    is_unsaved_host,
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


def test_is_unsaved_when_manager_empty(monkeypatch):
    monkeypatch.setattr(
        'sshpilot.unsaved_host.get_effective_ssh_config',
        lambda host, config_file=None: {'hostname': '1.2.3.4', 'user': 'alice'},
    )
    target = _conn(hostname='1.2.3.4', host='1.2.3.4', username='alice')
    assert is_unsaved_host(target, _Mgr([])) is True


def test_not_unsaved_when_same_host_and_user(monkeypatch):
    monkeypatch.setattr(
        'sshpilot.unsaved_host.get_effective_ssh_config',
        lambda host, config_file=None: {
            'hostname': 'example.com',
            'user': 'alice',
        },
    )
    saved = _conn(hostname='example.com', host='prod', username='alice', nickname='prod')
    target = _conn(hostname='example.com', host='example.com', username='alice')
    assert is_unsaved_host(target, _Mgr([saved])) is False


def test_unsaved_when_same_host_different_user(monkeypatch):
    def fake_g(host, config_file=None):
        if host == 'prod':
            return {'hostname': 'example.com', 'user': 'alice'}
        return {'hostname': 'example.com', 'user': 'root'}

    monkeypatch.setattr('sshpilot.unsaved_host.get_effective_ssh_config', fake_g)
    saved = _conn(hostname='example.com', host='prod', username='alice', nickname='prod')
    target = _conn(hostname='example.com', host='example.com', username='root')
    assert is_unsaved_host(target, _Mgr([saved])) is True


def test_unsaved_when_hostname_missing_from_config(monkeypatch):
    monkeypatch.setattr(
        'sshpilot.unsaved_host.get_effective_ssh_config',
        lambda host, config_file=None: {'hostname': host, 'user': 'bob'},
    )
    saved = _conn(hostname='a.example', host='a', username='bob', nickname='a')
    target = _conn(hostname='b.example', host='b.example', username='bob')
    assert is_unsaved_host(target, _Mgr([saved])) is True


def test_cli_user_at_ip_matches_saved_alias(monkeypatch):
    # `sshpilot root@192.168.8.1` must NOT prompt to save when a saved alias
    # (Host GoogleRouter / HostName 192.168.8.1 / User root) already covers it.
    # `ssh -G 192.168.8.1` can't match the alias block, so it returns the local
    # default user — which must not clobber the explicit `root`.
    def fake_g(host, config_file=None):
        if host == 'GoogleRouter':
            return {'hostname': '192.168.8.1', 'user': 'root'}
        return {'hostname': '192.168.8.1', 'user': 'localdefault'}  # ssh -G <ip>

    monkeypatch.setattr('sshpilot.unsaved_host.get_effective_ssh_config', fake_g)
    saved = _conn(hostname='192.168.8.1', host='GoogleRouter',
                  nickname='GoogleRouter', username='root')
    target = _conn(hostname='192.168.8.1', host='192.168.8.1', username='root')
    assert resolve_connection_host_user(target)[1] == 'root'  # explicit user kept
    assert is_unsaved_host(target, _Mgr([saved])) is False


def test_dismissals_are_session_scoped(monkeypatch):
    monkeypatch.setattr(
        'sshpilot.unsaved_host.get_effective_ssh_config',
        lambda host, config_file=None: {'hostname': 'h', 'user': 'u'},
    )
    d = SavePromptDismissals()
    conn = _conn(hostname='h', host='h', username='u')
    assert d.is_connection_dismissed(conn) is False
    d.dismiss_connection(conn)
    assert d.is_connection_dismissed(conn) is True


def test_resolve_falls_back_to_connection_fields(monkeypatch):
    monkeypatch.setattr(
        'sshpilot.unsaved_host.get_effective_ssh_config',
        lambda host, config_file=None: {},
    )
    conn = _conn(hostname='Raw.Host', host='alias', username='sam')
    host, user = resolve_connection_host_user(conn)
    assert host == 'raw.host'
    assert user == 'sam'

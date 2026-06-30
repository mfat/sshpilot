"""Tests for canonical SSH password host keys and legacy alias migration."""

import pytest

import sshpilot.secret_storage as ss
from sshpilot.credential_model import canonical_password_host, password_host_candidates
from sshpilot.secret_storage import SecretManager, password_spec


class FakeConn:
    def __init__(self, nickname='', hostname='', host='', username=''):
        self.nickname = nickname
        self.hostname = hostname
        self.host = host
        self.username = username

    def get_effective_host(self):
        return self.hostname or self.host or self.nickname


class FakeBackend(ss.SecretBackend):
    def __init__(self, name):
        self.name = name
        self.data = {}

    def is_available(self):
        return True

    def store(self, spec, secret):
        self.data[spec.keyring_account] = secret
        return True

    def lookup(self, spec):
        return self.data.get(spec.keyring_account)

    def delete(self, spec):
        return self.data.pop(spec.keyring_account, None) is not None


@pytest.fixture
def conn_mgr(monkeypatch):
    monkeypatch.setattr(ss, 'is_macos', lambda: False)
    from sshpilot.connection_manager import ConnectionManager

    mgr = SecretManager()
    backend = FakeBackend('libsecret')
    mgr._backends = {'libsecret': backend, 'keyring': FakeBackend('keyring')}
    monkeypatch.setattr(ss, 'get_secret_manager', lambda: mgr)

    cm = ConnectionManager.__new__(ConnectionManager)
    return cm, mgr, backend


def test_canonical_password_host_order():
    assert canonical_password_host(FakeConn('nick', hostname='host.example')) == 'host.example'
    assert canonical_password_host(FakeConn('nick', host='alias')) == 'alias'
    assert canonical_password_host(FakeConn('nick')) == 'nick'


def test_password_host_candidates_dedupes():
    conn = FakeConn('nick', hostname='host.example', host='alias')
    assert password_host_candidates(conn) == ['host.example', 'alias', 'nick']


def test_store_connection_password_uses_canonical_and_clears_aliases(conn_mgr):
    cm, _mgr, backend = conn_mgr
    conn = FakeConn('bnick', hostname='b.example', username='bob')
    backend.data[password_spec('bnick', 'bob').keyring_account] = 'legacy'

    assert cm.store_connection_password(conn, 'new-pw') is True
    assert backend.data['bob@b.example'] == 'new-pw'
    assert 'bob@bnick' not in backend.data


def test_get_connection_password_migrates_legacy_alias(conn_mgr):
    cm, _mgr, backend = conn_mgr
    conn = FakeConn('bnick', hostname='b.example', username='bob')
    backend.data[password_spec('bnick', 'bob').keyring_account] = 'legacy'

    assert cm.get_connection_password(conn) == 'legacy'
    assert backend.data['bob@b.example'] == 'legacy'
    assert 'bob@bnick' not in backend.data


def test_delete_connection_passwords_clears_all_aliases(conn_mgr):
    cm, _mgr, backend = conn_mgr
    conn = FakeConn('bnick', hostname='b.example', username='bob')
    backend.data[password_spec('bnick', 'bob').keyring_account] = 'x'
    backend.data[password_spec('b.example', 'bob').keyring_account] = 'y'

    assert cm.delete_connection_passwords(conn) is True
    assert backend.data == {}

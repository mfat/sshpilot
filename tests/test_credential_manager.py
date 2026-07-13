"""Tests for the credential manager normalization layer."""

import os

import pytest

import sshpilot.secret_storage as ss
from sshpilot.secret_storage import (
    SecretManager,
    password_spec,
    passphrase_spec,
    sudo_password_spec,
)
from sshpilot.credential_manager import (
    CredentialManager,
    TYPE_PASSWORD,
    TYPE_SUDO,
    TYPE_KEY,
)


class FakeBackend(ss.SecretBackend):
    def __init__(self, name, available=True):
        self.name = name
        self._available = available
        self.data = {}
        self.rows = []          # (attributes, value) pairs returned by iter_credentials

    def is_available(self):
        return self._available

    def store(self, spec, secret):
        self.data[spec.keyring_account] = secret
        return True

    def lookup(self, spec):
        return self.data.get(spec.keyring_account)

    def delete(self, spec):
        return self.data.pop(spec.keyring_account, None) is not None

    def iter_credentials(self):
        return list(self.rows)


class FakeConn:
    def __init__(self, nickname, hostname='', host='', username='', port=22,
                 keyfile='', identity_files=None):
        self.nickname = nickname
        self.hostname = hostname
        self.host = host
        self.username = username
        self.port = port
        self.keyfile = keyfile
        self.identity_files = identity_files or []

    def get_effective_host(self):
        return self.hostname or self.host or self.nickname


class FakeConnManager:
    def __init__(self, conns):
        self._conns = conns

    def get_connections(self):
        return list(self._conns)


@pytest.fixture
def secrets(monkeypatch):
    monkeypatch.setattr(ss, 'is_macos', lambda: False)   # deterministic libsecret,keyring order
    mgr = SecretManager()
    libsecret = FakeBackend('libsecret')
    keyring = FakeBackend('keyring')
    mgr._backends = {'libsecret': libsecret, 'keyring': keyring}
    return mgr, libsecret, keyring


def test_lists_password_sudo_and_key(secrets):
    mgr, libsecret, keyring = secrets
    a = FakeConn('A', hostname='a.example', username='alice', port=2222,
                 keyfile='/home/u/.ssh/id_a')
    b = FakeConn('bnick', hostname='b.example', username='bob')

    libsecret.store(password_spec('a.example', 'alice'), 'pw-a')
    keyring.store(password_spec('bnick', 'bob'), 'pw-b')          # under the NICKNAME, in keyring
    libsecret.store(sudo_password_spec('b.example', 'bob'), 'sudo-b')
    kp = os.path.realpath(os.path.expanduser('/home/u/.ssh/id_a'))
    libsecret.store(passphrase_spec(kp), 'pass-a')

    cm = CredentialManager(FakeConnManager([a, b]), secret_manager=mgr)
    creds = {(c.type, c.id): c for c in cm.list_credentials()}

    pa = creds[(TYPE_PASSWORD, 'alice@a.example')]
    assert pa.host == 'a.example' and pa.username == 'alice' and pa.secret == 'pw-a'
    assert pa.metadata['backend'] == 'libsecret'
    assert pa.metadata['uri'] == 'ssh://alice@a.example:2222'
    assert pa.metadata['raw_type'] == 'ssh_password'

    pb = creds[(TYPE_PASSWORD, 'bob@bnick')]                       # found under host-variant
    assert pb.secret == 'pw-b' and pb.metadata['backend'] == 'keyring'

    sb = creds[(TYPE_SUDO, 'sudo:bob@b.example')]
    assert sb.type == TYPE_SUDO and sb.secret == 'sudo-b' and sb.username == 'bob'

    key = creds[(TYPE_KEY, kp)]
    assert key.host is None and key.username is None and key.secret == 'pass-a'
    assert key.metadata['key_path'] == kp
    assert 'A' in key.metadata['connections']
    assert 'uri' not in key.metadata


def test_extra_key_paths_without_connection(secrets):
    mgr, libsecret, keyring = secrets
    kp = os.path.realpath(os.path.expanduser('/tmp/orphan_key'))
    libsecret.store(passphrase_spec(kp), 'orphan-pass')

    cm = CredentialManager(FakeConnManager([]), secret_manager=mgr,
                           extra_key_paths=['/tmp/orphan_key'])
    creds = cm.list_credentials()
    assert len(creds) == 1
    assert creds[0].type == TYPE_KEY and creds[0].secret == 'orphan-pass'
    assert creds[0].metadata.get('connections') == []


def test_gathers_home_relative_and_legacy_passphrase(secrets, monkeypatch, tmp_path):
    """Export must find a passphrase whether it was stored under the portable ``~`` title
    (post home-relative change) or a legacy absolute title, and export both under the ``~`` id."""
    mgr, libsecret, _keyring = secrets
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    keyfile = str(home / ".ssh" / "id_ed25519")
    conn = FakeConn("box", hostname="h", username="me", keyfile=keyfile)

    # New portable (~) title.
    libsecret.data[passphrase_spec("~/.ssh/id_ed25519").keyring_account] = "pw-new"
    keys = [c for c in CredentialManager(FakeConnManager([conn]), secret_manager=mgr)
            .list_credentials(include_orphans=False) if c.type == TYPE_KEY]
    assert [(c.id, c.secret) for c in keys] == [("~/.ssh/id_ed25519", "pw-new")]

    # Legacy absolute title — still found, still exported under the portable id.
    libsecret.data.clear()
    libsecret.data[os.path.realpath(keyfile)] = "pw-old"
    keys = [c for c in CredentialManager(FakeConnManager([conn]), secret_manager=mgr)
            .list_credentials(include_orphans=False) if c.type == TYPE_KEY]
    assert [(c.id, c.secret) for c in keys] == [("~/.ssh/id_ed25519", "pw-old")]


def test_no_stored_secret_yields_no_credential(secrets):
    mgr, libsecret, keyring = secrets
    a = FakeConn('A', hostname='a.example', username='alice')     # nothing stored anywhere
    cm = CredentialManager(FakeConnManager([a]), secret_manager=mgr)
    assert cm.list_credentials() == []


def test_dedup_same_account_across_connections(secrets):
    mgr, libsecret, keyring = secrets
    libsecret.store(password_spec('shared', 'u'), 'pw')
    a = FakeConn('A', hostname='shared', username='u')
    b = FakeConn('B', hostname='shared', username='u')
    cm = CredentialManager(FakeConnManager([a, b]), secret_manager=mgr)
    passwords = [c for c in cm.list_credentials() if c.type == TYPE_PASSWORD]
    assert len(passwords) == 1                                    # collapsed by (id, type)
    assert passwords[0].id == 'u@shared'


def test_merge_adds_enumerated_orphans(secrets):
    # Enumeration (adapter.load_all via iter_credentials) surfaces a stored secret with NO
    # matching connection as an orphan; a credential also present via a connection stays the
    # richer connection-derived one and is not marked orphan.
    mgr, libsecret, keyring = secrets
    a = FakeConn('A', hostname='a.example', username='alice')
    libsecret.store(password_spec('a.example', 'alice'), 'pw-a')         # belongs to conn A
    libsecret.rows = [
        ({'type': 'ssh_password', 'host': 'a.example', 'username': 'alice'}, 'pw-a'),
        ({'type': 'ssh_password', 'host': 'orphan', 'username': 'o'}, 'orphan-pw'),  # no conn
    ]
    cm = CredentialManager(FakeConnManager([a]), secret_manager=mgr)
    creds = {(c.type, c.id): c for c in cm.list_credentials()}

    derived = creds[(TYPE_PASSWORD, 'alice@a.example')]
    assert derived.secret == 'pw-a' and derived.metadata.get('orphan') is not True
    orphan = creds[(TYPE_PASSWORD, 'o@orphan')]
    assert orphan.secret == 'orphan-pw' and orphan.metadata['orphan'] is True


def test_include_orphans_false_skips_enumeration(secrets):
    # With include_orphans=False (the backup path) the enumeration/orphan merge is skipped,
    # so only the given connections' credentials are returned.
    mgr, libsecret, keyring = secrets
    a = FakeConn('A', hostname='a.example', username='alice')
    libsecret.store(password_spec('a.example', 'alice'), 'pw-a')
    libsecret.rows = [({'type': 'ssh_password', 'host': 'orphan', 'username': 'o'}, 'orphan-pw')]
    cm = CredentialManager(FakeConnManager([a]), secret_manager=mgr)
    ids = {(c.type, c.id) for c in cm.list_credentials(include_orphans=False)}
    assert (TYPE_PASSWORD, 'alice@a.example') in ids
    assert (TYPE_PASSWORD, 'o@orphan') not in ids          # orphan skipped
    # default still includes the orphan
    ids_all = {(c.type, c.id) for c in cm.list_credentials()}
    assert (TYPE_PASSWORD, 'o@orphan') in ids_all


def test_accepts_plain_connection_iterable(secrets):
    mgr, libsecret, keyring = secrets
    libsecret.store(password_spec('h', 'u'), 'pw')
    cm = CredentialManager([FakeConn('h', hostname='h', username='u')], secret_manager=mgr)
    creds = cm.list_credentials()
    assert len(creds) == 1 and creds[0].secret == 'pw'


def test_includes_resolved_identity_files(secrets):
    mgr, libsecret, keyring = secrets
    kp = os.path.realpath(os.path.expanduser('/home/u/.ssh/id_resolved'))
    libsecret.store(passphrase_spec(kp), 'pass')
    conn = FakeConn('A', hostname='a.example', username='alice')
    conn.resolved_identity_files = ['/home/u/.ssh/id_resolved']
    cm = CredentialManager(FakeConnManager([conn]), secret_manager=mgr)
    creds = {c.id: c for c in cm.list_credentials() if c.type == TYPE_KEY}
    assert kp in creds and creds[kp].secret == 'pass'

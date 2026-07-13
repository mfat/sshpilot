"""Tests for the credential adapters (SecretBackendAdapter + KdbxAdapter) and the model
translation helpers."""

import pytest

import sshpilot.secret_storage as ss
import sshpilot.credential_adapters as ca
from sshpilot.credential_model import (
    Credential,
    TYPE_PASSWORD,
    TYPE_SUDO,
    TYPE_KEY,
    credential_to_spec,
    credential_from_attributes,
)
from sshpilot.credential_adapters import SecretBackendAdapter, KdbxAdapter


# --- SecretBackendAdapter ----------------------------------------------------

class FakeBackend(ss.SecretBackend):
    """A passive backend WITHOUT an iter_credentials hook (can't enumerate)."""

    def __init__(self, name='libsecret'):
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


class FakeEnumBackend(FakeBackend):
    def __init__(self, name='libsecret', rows=None):
        super().__init__(name)
        self._rows = rows or []

    def iter_credentials(self):
        return list(self._rows)


def test_model_credential_to_spec_roundtrip():
    for cred, account in (
        (Credential(id='u@h', type=TYPE_PASSWORD, host='h', username='u'), 'u@h'),
        (Credential(id='sudo:u@h', type=TYPE_SUDO, host='h', username='u'), 'sudo:u@h'),
        (Credential(id='/k/id', type=TYPE_KEY, metadata={'key_path': '/k/id'}), '/k/id'),
    ):
        assert credential_to_spec(cred).keyring_account == account
    with pytest.raises(ValueError):
        credential_to_spec(Credential(id='x', type='bogus'))


def test_secret_adapter_save_delete_roundtrip():
    be = FakeBackend('libsecret')
    ad = SecretBackendAdapter(be)
    assert ad.can_enumerate is False
    cred = Credential(id='u@h', type=TYPE_PASSWORD, host='h', username='u', secret='pw')
    assert ad.save(cred) is True
    assert be.data['u@h'] == 'pw'                 # stored under password_spec('h','u') account
    assert ad.delete(cred) is True
    assert 'u@h' not in be.data
    assert ad.save(Credential(id='x', type=TYPE_PASSWORD, host='h', username='u')) is False  # no secret


def test_secret_adapter_load_all_maps_attributes():
    rows = [
        ({'type': 'ssh_password', 'host': 'h', 'username': 'u'}, 'pw'),
        ({'type': 'sudo_password', 'host': 'h', 'username': 'u'}, 'sudo'),
        ({'type': 'key_passphrase', 'key_path': '/k/id'}, 'passphrase'),
        ({'type': 'ssh_password', 'host': 'h2', 'username': 'u2'}, None),   # no value -> skipped
    ]
    ad = SecretBackendAdapter(FakeEnumBackend('libsecret', rows))
    assert ad.can_enumerate is True
    creds = {(c.type, c.id): c for c in ad.load_all()}
    assert creds[(TYPE_PASSWORD, 'u@h')].secret == 'pw'
    assert creds[(TYPE_SUDO, 'sudo:u@h')].secret == 'sudo'
    assert creds[(TYPE_KEY, '/k/id')].secret == 'passphrase'
    assert (TYPE_PASSWORD, 'u2@h2') not in creds


def test_watch_changes_default_is_noop():
    ad = SecretBackendAdapter(FakeBackend())
    unsub = ad.watch_changes(lambda: None)
    assert callable(unsub)
    assert unsub() is None


# --- KdbxAdapter (fake pykeepass) -------------------------------------------

class FakeEntry:
    def __init__(self, title, username='', password='', url=''):
        self.title = title
        self.username = username
        self.password = password
        self.url = url
        self._props = {}

    def get_custom_property(self, key):
        return self._props.get(key)

    def set_custom_property(self, key, value):
        self._props[key] = value


class FakeGroup:
    def __init__(self, name):
        self.name = name


class FakePyKeePass:
    _stores = {}             # path -> entries list (persists across reopen)
    raise_on_open = False

    def __init__(self, path, password=None, keyfile=None):
        if FakePyKeePass.raise_on_open:
            raise ca.CredentialsError("bad password")
        self.path = path
        store = FakePyKeePass._stores.setdefault(path, {'all': [], 'group': []})
        self.entries = store['all']
        self._group_entries = store['group']
        self.root_group = FakeGroup('root')
        self._group = FakeGroup('sshPilot')
        self.saved = 0

    def find_entries(self, title=None, first=False, group=None):
        pool = self._group_entries if group is not None else self.entries
        matches = [e for e in pool if (not title or e.title == title)]
        return (matches[0] if matches else None) if first else matches

    def find_groups(self, name=None, first=False):
        if name and name != 'sshPilot':
            return None if first else []
        return self._group if first else [self._group]

    def add_group(self, parent, name):
        return FakeGroup(name)

    def add_entry(self, group, title, username, password):
        e = FakeEntry(title, username, password)
        self.entries.append(e)
        if getattr(group, 'name', None) == 'sshPilot':
            self._group_entries.append(e)
        return e

    def delete_entry(self, entry):
        self.entries.remove(entry)
        if entry in self._group_entries:
            self._group_entries.remove(entry)

    def save(self):
        self.saved += 1


@pytest.fixture(autouse=True)
def _reset_fake_kdbx():
    FakePyKeePass._stores = {}
    FakePyKeePass.raise_on_open = False
    yield
    FakePyKeePass._stores = {}
    FakePyKeePass.raise_on_open = False

def test_kdbx_save_load_roundtrip(monkeypatch):
    monkeypatch.setattr(ca, 'PyKeePass', FakePyKeePass)
    ad = KdbxAdapter('/tmp/x.kdbx', password='pw')
    assert ad.is_available() is True

    ad.save(Credential(id='u@h', type=TYPE_PASSWORD, host='h', username='u', secret='pw1'))
    ad.save(Credential(id='/k/id', type=TYPE_KEY, secret='passphrase'))

    creds = {(c.type, c.id): c for c in ad.load_all()}
    p = creds[(TYPE_PASSWORD, 'u@h')]
    assert p.secret == 'pw1' and p.host == 'h' and p.username == 'u'
    k = creds[(TYPE_KEY, '/k/id')]                      # type preserved via custom property
    assert k.secret == 'passphrase' and k.metadata['key_path'] == '/k/id'

    # update existing, then delete
    ad.save(Credential(id='u@h', type=TYPE_PASSWORD, host='h', username='u', secret='pw2'))
    assert {c.id: c.secret for c in ad.load_all()}['u@h'] == 'pw2'
    assert ad.delete(Credential(id='u@h', type=TYPE_PASSWORD)) is True
    assert (TYPE_PASSWORD, 'u@h') not in {(c.type, c.id) for c in ad.load_all()}


def test_kdbx_load_all_ignores_entries_outside_sshpilot_group(monkeypatch):
    monkeypatch.setattr(ca, 'PyKeePass', FakePyKeePass)
    ad = KdbxAdapter('/tmp/x.kdbx', password='pw')
    store = FakePyKeePass._stores.setdefault('/tmp/x.kdbx', {'all': [], 'group': []})
    store['all'].append(FakeEntry('other-user@host', 'u', 'outside'))   # not in sshPilot group
    ad.save(Credential(id='u@h', type=TYPE_PASSWORD, host='h', username='u', secret='inside'))
    creds = ad.load_all()
    assert len(creds) == 1 and creds[0].secret == 'inside'


def test_kdbx_wrong_password_raises(monkeypatch):
    monkeypatch.setattr(ca, 'PyKeePass', FakePyKeePass)
    monkeypatch.setattr(FakePyKeePass, 'raise_on_open', True)
    ad = KdbxAdapter('/tmp/x.kdbx', password='bad')
    with pytest.raises(ca.CredentialsError):
        ad.load_all()


def test_kdbx_unavailable_without_pykeepass(monkeypatch):
    monkeypatch.setattr(ca, 'PyKeePass', None)
    ad = KdbxAdapter('/tmp/x.kdbx', password='pw')
    assert ad.is_available() is False

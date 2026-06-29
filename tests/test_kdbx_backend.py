"""Tests for the KDBX (KeePass file) secret backend, using a fake pykeepass."""

import os
import time

import pytest

import sshpilot.secret_storage as ss
from sshpilot.secret_storage import password_spec, sudo_password_spec, passphrase_spec


# --- fake pykeepass ----------------------------------------------------------

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


class FakeBacking:
    """Persistent backing for one .kdbx path: entries + the correct password + derived key."""
    def __init__(self):
        self.entries = []
        self.password = 'correct'
        self.key = b'TKEY-' + os.urandom(8)


class FakePyKeePass:
    stores = {}

    @classmethod
    def backing(cls, path):
        return cls.stores.setdefault(path, FakeBacking())

    def __init__(self, path, password=None, keyfile=None, transformed_key=None):
        self._b = FakePyKeePass.backing(path)
        if transformed_key is not None:
            if transformed_key != self._b.key:
                raise ss.CredentialsError("bad key")
        elif password != self._b.password:
            raise ss.CredentialsError("bad password")

    @property
    def entries(self):
        return self._b.entries

    @property
    def transformed_key(self):
        return self._b.key

    @property
    def root_group(self):
        return FakeGroup('root')

    def find_entries(self, title=None, first=False):
        matches = [e for e in self._b.entries if e.title == title]
        return (matches[0] if matches else None) if first else matches

    def find_groups(self, name=None, first=False):
        return FakeGroup(name)

    def add_group(self, parent, name):
        return FakeGroup(name)

    def add_entry(self, group, title, username, password):
        e = FakeEntry(title, username, password)
        self._b.entries.append(e)
        return e

    def delete_entry(self, entry):
        self._b.entries.remove(entry)

    def save(self):
        pass


@pytest.fixture
def kdbx(monkeypatch, tmp_path):
    db = tmp_path / "test.kdbx"
    db.write_bytes(b"")                                   # is_available() needs the file present
    FakePyKeePass.stores = {}
    monkeypatch.setattr(ss, 'PyKeePass', FakePyKeePass)
    monkeypatch.setenv('SSHPILOT_KDBX_DATABASE', str(db))
    monkeypatch.delenv('SSHPILOT_KDBX_KEYFILE', raising=False)
    monkeypatch.delenv('SSHPILOT_KDBX_KEY', raising=False)
    monkeypatch.delenv('SSHPILOT_SECRET_SESSION_TIMEOUT', raising=False)
    yield ss.KdbxBackend(), str(db)
    os.environ.pop('SSHPILOT_KDBX_KEY', None)


# --- tests -------------------------------------------------------------------

def test_registered_and_session_backed():
    mgr = ss.SecretManager()
    assert 'keepassxc' in mgr.registered_backends()
    assert mgr.is_session_backed('keepassxc') is True


def test_unlock_exports_key_and_is_unlocked(kdbx):
    backend, _db = kdbx
    assert backend.is_available() is True
    assert backend.unlock('correct') is True
    assert backend.is_unlocked() is True
    assert os.environ.get('SSHPILOT_KDBX_KEY')             # transformed key exported for subprocess


def test_unlock_wrong_password_fails(kdbx):
    backend, _db = kdbx
    assert backend.unlock('wrong') is False
    assert backend.is_unlocked() is False
    assert os.environ.get('SSHPILOT_KDBX_KEY') is None


def test_store_lookup_delete_roundtrip(kdbx):
    backend, _db = kdbx
    backend.unlock('correct')
    spec = password_spec('h.example', 'alice')
    assert backend.store(spec, 'pw-a') is True
    assert backend.lookup(spec) == 'pw-a'
    assert backend.lookup(password_spec('nope', 'x')) is None
    # sudo + key types coexist (distinct titles)
    assert backend.store(sudo_password_spec('h.example', 'alice'), 'sudo-a') is True
    assert backend.store(passphrase_spec('/home/u/.ssh/id_ed25519'), 'pass') is True
    assert backend.lookup(sudo_password_spec('h.example', 'alice')) == 'sudo-a'
    assert backend.lookup(passphrase_spec('/home/u/.ssh/id_ed25519')) == 'pass'
    assert backend.delete(spec) is True
    assert backend.lookup(spec) is None


def test_type_recorded_as_custom_property(kdbx):
    backend, _db = kdbx
    backend.unlock('correct')
    backend.store(password_spec('h', 'u'), 'pw')
    entry = backend._db().find_entries(title='u@h', first=True)
    assert entry.get_custom_property('sshpilot_type') == 'ssh_password'
    assert entry.username == 'u'


def test_subprocess_opens_from_env_key(kdbx):
    backend, _db = kdbx
    backend.unlock('correct')
    backend.store(password_spec('h', 'u'), 'pw')
    # A fresh backend (no in-process DB) — like the askpass subprocess — must open via the
    # inherited SSHPILOT_KDBX_KEY (transformed key), no password.
    sub = ss.KdbxBackend()
    assert sub._kp is None
    assert sub.lookup(password_spec('h', 'u')) == 'pw'


def test_lock_drops_session_and_env_key(kdbx):
    backend, _db = kdbx
    backend.unlock('correct')
    backend.lock()
    assert backend.is_unlocked() is False
    assert os.environ.get('SSHPILOT_KDBX_KEY') is None


def test_idle_timeout_drops_session(kdbx, monkeypatch):
    monkeypatch.setenv('SSHPILOT_SECRET_SESSION_TIMEOUT', '60')
    backend, _db = kdbx
    backend.unlock('correct')
    assert backend.is_unlocked() is True
    backend._deadline = time.monotonic() - 1                # force expiry
    assert backend.is_unlocked() is False
    assert os.environ.get('SSHPILOT_KDBX_KEY') is None


def test_selected_master_spec_keyed_by_db_path(monkeypatch):
    monkeypatch.setenv('SSHPILOT_KDBX_DATABASE', '/vaults/work.kdbx')
    mgr = ss.SecretManager()
    mgr.set_selected('keepassxc')
    assert ss.selected_master_spec(mgr).keyring_account == 'keepassxc-master:/vaults/work.kdbx'

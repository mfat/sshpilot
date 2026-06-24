"""Tests for the pluggable secret storage layer (sshpilot/secret_storage.py)."""

import pytest

import sshpilot.secret_storage as ss
from sshpilot.secret_storage import (
    SecretBackend,
    SecretManager,
    password_spec,
    passphrase_spec,
)


class FakeBackend(SecretBackend):
    def __init__(self, name, available=True):
        self.name = name
        self._available = available
        self.fail_store = False
        self.data = {}

    def is_available(self):
        return self._available

    def store(self, spec, secret):
        if self.fail_store:
            return False
        self.data[spec.keyring_account] = secret
        return True

    def lookup(self, spec):
        return self.data.get(spec.keyring_account)

    def delete(self, spec):
        return self.data.pop(spec.keyring_account, None) is not None


@pytest.fixture
def manager(monkeypatch):
    # Deterministic platform order: libsecret then keyring.
    monkeypatch.setattr(ss, 'is_macos', lambda: False)
    mgr = SecretManager()
    primary = FakeBackend('libsecret')
    fallback = FakeBackend('keyring')
    mgr._backends = {
        'libsecret': primary,
        'keyring': fallback,
        'pass': FakeBackend('pass', available=False),
    }
    return mgr, primary, fallback


# --- back-compat: spec format must match the legacy storage keys -------------

def test_password_spec_legacy_format():
    spec = password_spec('host.example', 'alice')
    assert spec.keyring_service == 'sshPilot'
    assert spec.keyring_account == 'alice@host.example'
    assert spec.attributes == {
        'application': 'sshPilot',
        'type': 'ssh_password',
        'host': 'host.example',
        'username': 'alice',
    }
    assert spec.label == 'sshPilot: alice@host.example'
    assert spec.pass_path == 'sshpilot/password/alice@host.example'


def test_passphrase_spec_legacy_format():
    spec = passphrase_spec('/home/u/.ssh/id_ed25519')
    assert spec.keyring_service == 'sshPilot'
    assert spec.keyring_account == '/home/u/.ssh/id_ed25519'
    assert spec.attributes == {
        'application': 'sshPilot',
        'type': 'key_passphrase',
        'key_path': '/home/u/.ssh/id_ed25519',
    }
    assert spec.pass_path == 'sshpilot/passphrase/home/u/.ssh/id_ed25519'


# --- manager semantics -------------------------------------------------------

def test_store_uses_primary_then_lookup(manager):
    mgr, primary, fallback = manager
    spec = password_spec('h', 'u')
    assert mgr.store(spec, 's3cret') is True
    assert primary.data[spec.keyring_account] == 's3cret'
    assert fallback.data == {}
    assert mgr.lookup(spec) == 's3cret'


def test_store_falls_back_when_primary_fails(manager):
    mgr, primary, fallback = manager
    primary.fail_store = True
    spec = password_spec('h', 'u')
    assert mgr.store(spec, 's3cret') is True
    assert spec.keyring_account not in primary.data
    assert fallback.data[spec.keyring_account] == 's3cret'


def test_lookup_falls_through_to_secondary(manager):
    mgr, primary, fallback = manager
    spec = password_spec('h', 'u')
    fallback.data[spec.keyring_account] = 'legacy'   # only in the fallback store
    assert mgr.lookup(spec) == 'legacy'


def test_delete_clears_all_backends(manager):
    mgr, primary, fallback = manager
    spec = password_spec('h', 'u')
    primary.data[spec.keyring_account] = 'x'
    fallback.data[spec.keyring_account] = 'x'
    assert mgr.delete(spec) is True
    assert spec.keyring_account not in primary.data
    assert spec.keyring_account not in fallback.data


def test_selected_backend_takes_priority(manager):
    mgr, primary, fallback = manager
    mgr.set_selected('keyring')
    spec = password_spec('h', 'u')
    mgr.store(spec, 's')
    assert fallback.data[spec.keyring_account] == 's'   # selected wins
    assert spec.keyring_account not in primary.data
    assert mgr.active_backend_name == 'keyring'


def test_register_backend_and_available(manager):
    mgr, primary, fallback = manager
    custom = FakeBackend('vault')
    mgr.register_backend('vault', custom)
    mgr.set_selected('vault')
    assert 'vault' in mgr.available_backends()
    spec = password_spec('h', 'u')
    mgr.store(spec, 's')
    assert custom.data[spec.keyring_account] == 's'


def test_unavailable_backends_skipped(manager):
    mgr, primary, fallback = manager
    primary._available = False
    spec = password_spec('h', 'u')
    mgr.store(spec, 's')
    assert fallback.data[spec.keyring_account] == 's'
    assert mgr.active_backend_name == 'keyring'


# --- pass backend argv -------------------------------------------------------

def test_pass_backend_argv(monkeypatch):
    calls = []

    class _Result:
        returncode = 0
        stdout = b'secretvalue\n'
        stderr = b''

    def fake_run(argv, input=None, capture_output=None, env=None, check=None):
        calls.append((list(argv), input))
        return _Result()

    monkeypatch.setattr(ss.subprocess, 'run', fake_run)
    backend = ss.PassBackend()
    backend._bin = '/usr/bin/pass'
    spec = password_spec('h', 'u')

    assert backend.store(spec, 'secretvalue') is True
    assert calls[-1] == (['/usr/bin/pass', 'insert', '-m', '-f', 'sshpilot/password/u@h'], b'secretvalue')

    assert backend.lookup(spec) == 'secretvalue'
    assert calls[-1][0] == ['/usr/bin/pass', 'show', 'sshpilot/password/u@h']

    assert backend.delete(spec) is True
    assert calls[-1][0] == ['/usr/bin/pass', 'rm', '-f', 'sshpilot/password/u@h']

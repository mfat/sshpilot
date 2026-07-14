"""Tests for the pluggable secret storage layer (sshpilot/secret_storage.py)."""

import base64
import json
import os

import pytest

import sshpilot.secret_storage as ss
from sshpilot.secret_storage import (
    SecretBackend,
    SecretManager,
    password_spec,
    passphrase_spec,
)


class FakeBackend(SecretBackend):
    def __init__(self, name, available=True, label=None):
        self.name = name
        self._available = available
        self._label = label
        self.fail_store = False
        self.data = {}

    def describe(self):
        return self._label or self.name

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


def test_sudo_password_spec_legacy_format():
    spec = ss.sudo_password_spec('host.example', 'alice')
    assert spec.keyring_service == 'sshPilot'
    assert spec.keyring_account == 'sudo:alice@host.example'   # legacy macOS account
    assert spec.attributes == {
        'application': 'sshPilot',
        'type': 'sudo_password',                              # distinct from ssh_password
        'host': 'host.example',
        'username': 'alice',
    }
    assert spec.pass_path == 'sshpilot/sudo/alice@host.example'


def test_passphrase_spec_legacy_format():
    spec = passphrase_spec('/home/u/.ssh/id_ed25519')
    assert spec.keyring_service == 'sshPilot'
    assert spec.keyring_account == '/home/u/.ssh/id_ed25519'
    assert spec.attributes == {
        'application': 'sshPilot',
        'type': 'key_passphrase',
        'key_path': '/home/u/.ssh/id_ed25519',
    }
    assert spec.pass_path == 'sshpilot/passphrase/_home_u_.ssh_id_ed25519'


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


def test_available_backends_cheap_uses_discoverable_not_is_available(manager):
    mgr, _primary, _fallback = manager

    class SessionBackend(FakeBackend):
        def is_available(self):  # would spawn `bw --version` — must NOT run when cheap
            raise AssertionError("is_available() must not be called in cheap mode")

        def is_discoverable(self):
            return True

    mgr.register_backend('bitwarden', SessionBackend('bitwarden'))
    assert 'bitwarden' in mgr.available_backends(cheap=True)
    # Non-session backends without is_discoverable still fall back to is_available.
    assert 'libsecret' in mgr.available_backends(cheap=True)


def test_unavailable_backends_skipped(manager):
    mgr, primary, fallback = manager
    primary._available = False
    spec = password_spec('h', 'u')
    mgr.store(spec, 's')
    assert fallback.data[spec.keyring_account] == 's'
    assert mgr.active_backend_name == 'keyring'


def test_auto_uses_platform_default_stores_only(manager):
    # `auto` == "use the platform default local store": store/lookup/delete all act on
    # libsecret/keyring only, never a non-default backend (e.g. `pass`). That backend is
    # reached only via explicit selection.
    mgr, primary, fallback = manager
    extra = mgr._backends['pass']
    extra._available = True
    spec = password_spec('h', 'u')
    extra.data[spec.keyring_account] = 'in-pass'

    assert mgr.lookup(spec) is None                          # auto: libsecret/keyring only
    assert mgr.delete(spec) is False                         # auto doesn't touch `pass`
    assert extra.data.get(spec.keyring_account) == 'in-pass'  # left untouched

    mgr.set_selected('pass')                                 # explicit selection reaches it
    assert mgr.lookup(spec) == 'in-pass'
    assert mgr.delete(spec) is True
    assert spec.keyring_account not in extra.data


def test_all_available_backends_public_api(manager):
    mgr, primary, fallback = manager
    assert mgr.all_available_backends() == mgr._all_available_backends()


def test_libsecret_iter_credentials_maps_search_results(monkeypatch):
    # iter_credentials enumerates via Secret.password_search and returns (attributes, secret).
    class FakeValue:
        def __init__(self, text):
            self._t = text

        def get_text(self):
            return self._t

    class FakeItem:
        def __init__(self, attrs, secret):
            self._a = attrs
            self._s = secret

        def get_attributes(self):
            return dict(self._a)

        def retrieve_secret_sync(self, _c):
            return FakeValue(self._s)

    class FakeFlags:
        ALL = 1
        LOAD_SECRETS = 2
        UNLOCK = 4

    class FakeSecret:
        SearchFlags = FakeFlags

        @staticmethod
        def password_search_sync(schema, attrs, flags, cancellable):
            assert attrs == {'application': 'sshPilot'}
            assert flags == (FakeFlags.ALL | FakeFlags.LOAD_SECRETS | FakeFlags.UNLOCK)
            return [FakeItem({'type': 'ssh_password', 'host': 'h', 'username': 'u'}, 'pw')]

    monkeypatch.setattr(ss, 'Secret', FakeSecret)
    monkeypatch.setattr(ss, 'get_schema', lambda: object())   # non-None schema
    rows = ss.LibSecretBackend().iter_credentials()
    assert rows == [({'type': 'ssh_password', 'host': 'h', 'username': 'u'}, 'pw')]


def test_lookup_everywhere_ignores_exclusive_selection(manager):
    # lookup_everywhere (used by the credential manager) scans ALL available backends and
    # names the one that held the secret — even one the user has switched away from.
    mgr, primary, fallback = manager
    spec = password_spec('h', 'u')
    fallback.data[spec.keyring_account] = 'in-keyring'   # only in the non-selected backend
    mgr.set_selected('libsecret')                        # explicit selection = exclusive
    assert mgr.lookup(spec) is None                      # normal lookup honors exclusivity
    assert mgr.lookup_everywhere(spec) == ('in-keyring', 'keyring')
    assert mgr.lookup_everywhere(password_spec('nope', 'x')) is None


def test_active_backend_label_uses_describe(monkeypatch):
    monkeypatch.setattr(ss, 'is_macos', lambda: False)
    mgr = SecretManager()
    mgr._backends = {
        'libsecret': FakeBackend('libsecret', label='libsecret'),
        'keyring': FakeBackend('keyring', label='keyring:KWalletKeyring'),
    }
    assert mgr.active_backend_label() == 'libsecret'
    mgr.set_selected('keyring')
    assert mgr.active_backend_label() == 'keyring:KWalletKeyring'


def test_keyring_backend_describe(monkeypatch):
    class _Backend:
        pass

    class FakeKeyring:
        @staticmethod
        def get_keyring():
            return _Backend()

    monkeypatch.setattr(ss, 'keyring', FakeKeyring)
    assert ss.KeyringBackend().describe() == 'keyring:_Backend'


def test_macos_keychain_store_uses_security_trusted_acl(monkeypatch, tmp_path):
    """macOS KeyringBackend.store must use `security -T` so Always Allow sticks."""
    ss._macos_acl_upgraded.clear()
    monkeypatch.setattr(ss, 'is_macos', lambda: True)

    fake_exe = tmp_path / 'SSHPilot'
    fake_exe.write_text('#!/bin/sh\n')
    fake_exe.chmod(0o755)
    security_bin = tmp_path / 'security'
    security_bin.write_text('#!/bin/sh\n')
    security_bin.chmod(0o755)
    monkeypatch.setattr(ss.sys, 'executable', str(fake_exe))
    monkeypatch.setattr(ss.sys, 'frozen', True, raising=False)

    calls = []

    class _Result:
        returncode = 0
        stdout = ''
        stderr = ''

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _Result()

    monkeypatch.setattr(ss.subprocess, 'run', fake_run)
    monkeypatch.setattr(ss.shutil, 'which', lambda name: str(security_bin))

    class FakeKeyring:
        @staticmethod
        def get_keyring():
            return object()

        @staticmethod
        def set_password(*_a, **_k):
            raise AssertionError('keyring.set_password must not be used on macOS success path')

    monkeypatch.setattr(ss, 'keyring', FakeKeyring)

    backend = ss.KeyringBackend()
    spec = password_spec('host.example', 'alice')
    assert backend.store(spec, 's3cret') is True
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[0] == str(security_bin)
    assert cmd[1] == 'add-generic-password'
    assert '-U' in cmd
    assert '-T' in cmd
    assert str(fake_exe.resolve()) in cmd
    assert cmd[cmd.index('-s') + 1] == 'sshPilot'
    assert cmd[cmd.index('-a') + 1] == 'alice@host.example'
    assert cmd[cmd.index('-w') + 1] == 's3cret'
    assert (spec.keyring_service, spec.keyring_account) in ss._macos_acl_upgraded


def test_macos_keychain_lookup_upgrades_acl_once(monkeypatch, tmp_path):
    """After a successful read, rewrite the item with -T so the next launch is silent."""
    ss._macos_acl_upgraded.clear()
    monkeypatch.setattr(ss, 'is_macos', lambda: True)

    fake_exe = tmp_path / 'python3'
    fake_exe.write_text('x')
    fake_exe.chmod(0o755)
    security_bin = tmp_path / 'security'
    security_bin.write_text('#!/bin/sh\n')
    security_bin.chmod(0o755)
    monkeypatch.setattr(ss.sys, 'executable', str(fake_exe))
    monkeypatch.setattr(ss.sys, 'frozen', False, raising=False)

    store_calls = []

    class _Result:
        returncode = 0
        stdout = ''
        stderr = ''

    def fake_run(argv, **kwargs):
        store_calls.append(list(argv))
        return _Result()

    monkeypatch.setattr(ss.subprocess, 'run', fake_run)
    monkeypatch.setattr(ss.shutil, 'which', lambda name: str(security_bin))

    class FakeKeyring:
        @staticmethod
        def get_keyring():
            return object()

        @staticmethod
        def get_password(service, account):
            return 'stored-secret'

    monkeypatch.setattr(ss, 'keyring', FakeKeyring)

    backend = ss.KeyringBackend()
    spec = passphrase_spec('~/.ssh/id_ed25519')
    assert backend.lookup(spec) == 'stored-secret'
    assert len(store_calls) == 1
    assert '-T' in store_calls[0]

    # Second lookup must not rewrite again (same process).
    assert backend.lookup(spec) == 'stored-secret'
    assert len(store_calls) == 1


def test_pass_path_sanitized_but_keyring_account_raw():
    spec = password_spec('ho/st', 'us/er')
    assert spec.pass_path == 'sshpilot/password/us_er@ho_st'   # no stray '/'
    assert spec.keyring_account == 'us/er@ho/st'               # legacy key unchanged

    pspec = passphrase_spec('/home/u/.ssh/id_ed25519')
    assert pspec.pass_path == 'sshpilot/passphrase/_home_u_.ssh_id_ed25519'
    assert pspec.keyring_account == '/home/u/.ssh/id_ed25519'  # legacy key unchanged


# --- pass backend argv -------------------------------------------------------

def test_pass_backend_argv(monkeypatch):
    calls = []

    class _Result:
        returncode = 0
        stdout = b'secretvalue\n'
        stderr = b''

    def fake_run(argv, input=None, capture_output=None, env=None, check=None, timeout=None):
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


# --- rbw (github.com/doy/rbw) CLI backend ------------------------------------

def _rbw_fake(*, unlocked=True, get_out=b'', folder_names=()):
    """Fake subprocess.run for rbw: dispatches on the subcommand.

    ``unlocked`` drives ``rbw unlocked``'s exit code. ``folder_names`` are the item
    names present in our folder — served by ``rbw list --fields name,folder`` (the
    cached membership source for lookup/existence). ``get_out`` is the ``rbw get``
    body for a present item."""
    calls = []

    class _R:
        def __init__(self, rc=0, out=b''):
            self.returncode = rc
            self.stdout = out
            self.stderr = b''

    def fake_run(argv, input=None, capture_output=None, env=None, check=None, timeout=None):
        calls.append((list(argv), input))
        sub = argv[1]
        if sub == 'unlocked':
            return _R(0 if unlocked else 1)
        if sub == 'list':
            out = ''.join(f'{n}\tsshPilot\n' for n in folder_names).encode()
            return _R(0, out)
        if sub == 'get':
            name = argv[-1]
            return _R(0, get_out) if name in folder_names else _R(1, b'')
        return _R(0)

    return calls, fake_run


def test_rbw_backend_gated_when_locked(monkeypatch):
    calls, fake = _rbw_fake(unlocked=False)
    monkeypatch.setattr(ss.subprocess, 'run', fake)
    b = ss.RbwBackend(); b._bin = '/usr/bin/rbw'
    spec = password_spec('h', 'u')
    assert b.lookup(spec) is None
    assert b.store(spec, 's') is False
    assert b.delete(spec) is False
    # Locked vault must never reach get/add/edit/remove — only `unlocked` was asked.
    assert calls and all(c[0][1] == 'unlocked' for c in calls)


def test_rbw_backend_lookup(monkeypatch):
    calls, fake = _rbw_fake(unlocked=True, get_out=b'secretvalue\n', folder_names=('u@h',))
    monkeypatch.setattr(ss.subprocess, 'run', fake)
    b = ss.RbwBackend(); b._bin = '/usr/bin/rbw'
    assert b.lookup(password_spec('h', 'u')) == 'secretvalue'
    assert calls[-1][0] == ['/usr/bin/rbw', 'get', '--folder', 'sshPilot', 'u@h']


def test_rbw_backend_lookup_miss_skips_get(monkeypatch):
    # A name not in our folder resolves from the cached `list` — no slow `rbw get`.
    calls, fake = _rbw_fake(unlocked=True, folder_names=())
    monkeypatch.setattr(ss.subprocess, 'run', fake)
    b = ss.RbwBackend(); b._bin = '/usr/bin/rbw'
    assert b.lookup(password_spec('h', 'u')) is None
    assert any(c[0][1] == 'list' for c in calls)
    assert not any(c[0][1] == 'get' for c in calls)


def test_rbw_backend_names_cache_reused(monkeypatch):
    # A burst of distinct misses triggers a single `rbw list`, not one per lookup.
    calls, fake = _rbw_fake(unlocked=True, folder_names=())
    monkeypatch.setattr(ss.subprocess, 'run', fake)
    b = ss.RbwBackend(); b._bin = '/usr/bin/rbw'
    for i in range(5):
        assert b.lookup(password_spec(f'h{i}', 'u')) is None
    assert sum(1 for c in calls if c[0][1] == 'list') == 1


def test_rbw_backend_value_cache(monkeypatch):
    # A hit is cached: the second lookup and peek() serve the value without a new `rbw get`.
    calls, fake = _rbw_fake(unlocked=True, get_out=b'topsecret\n', folder_names=('u@h',))
    monkeypatch.setattr(ss.subprocess, 'run', fake)
    b = ss.RbwBackend(); b._bin = '/usr/bin/rbw'
    spec = password_spec('h', 'u')
    assert b.lookup(spec) == 'topsecret'
    gets = sum(1 for c in calls if c[0][1] == 'get')
    assert b.lookup(spec) == 'topsecret'            # served from cache
    assert sum(1 for c in calls if c[0][1] == 'get') == gets   # no extra `get`
    assert b.peek('u@h') == 'topsecret'             # cache-only, still no new subprocess
    assert sum(1 for c in calls if c[0][1] == 'get') == gets
    # A fresh instance (the askpass subprocess) has no cache.
    assert ss.RbwBackend().peek('u@h') is None


def test_rbw_peek_refuses_when_locked_and_lock_clears_cache(monkeypatch):
    # Security: a locked agent must not serve cached secrets, and an explicit lock()
    # wipes the in-memory cache. rbw can auto-lock (lock_timeout) or be locked via the
    # Preferences button without clearing our cache, so peek() gates on live agent state.
    import time as _t
    b = ss.RbwBackend(); b._bin = '/usr/bin/rbw'
    b._values['u@h'] = ('cachedsecret', _t.monotonic())

    b.is_unlocked = lambda: True
    assert b.peek('u@h') == 'cachedsecret'
    b.is_unlocked = lambda: False
    assert b.peek('u@h') is None            # locked -> never serve the cached value

    b.is_unlocked = lambda: True
    b._values['u@h'] = ('cachedsecret', _t.monotonic())
    b.lock()                                 # explicit lock wipes plaintext from memory
    assert b.peek('u@h') is None


def test_rbw_value_cache_invalidated_on_store_and_delete(monkeypatch):
    calls, fake = _rbw_fake(unlocked=True, get_out=b'v1\n', folder_names=('u@h',))
    monkeypatch.setattr(ss.subprocess, 'run', fake)
    b = ss.RbwBackend(); b._bin = '/usr/bin/rbw'
    spec = password_spec('h', 'u')
    assert b.lookup(spec) == 'v1'
    assert b.peek('u@h') == 'v1'
    b.store(spec, 'v2')                 # a write must drop the stale cached value
    assert b.peek('u@h') is None
    b._values['u@h'] = ('v3', __import__('time').monotonic())
    b.delete(spec)                      # so must a delete
    assert b.peek('u@h') is None


def test_rbw_backend_store_add_then_edit(monkeypatch):
    spec = password_spec('h', 'u')
    # Not present (folder empty) -> `add`, item body piped on stdin (first line = password).
    calls, fake = _rbw_fake(unlocked=True, folder_names=())
    monkeypatch.setattr(ss.subprocess, 'run', fake)
    b = ss.RbwBackend(); b._bin = '/usr/bin/rbw'
    assert b.store(spec, 'secretvalue') is True
    assert calls[-1] == (['/usr/bin/rbw', 'add', '--folder', 'sshPilot', 'u@h'], b'secretvalue\nSaved by SSH Pilot\n')

    # Already present -> `edit` (update in place, no duplicate).
    calls, fake = _rbw_fake(unlocked=True, folder_names=('u@h',))
    monkeypatch.setattr(ss.subprocess, 'run', fake)
    b = ss.RbwBackend(); b._bin = '/usr/bin/rbw'
    assert b.store(spec, 'newsecret') is True
    assert calls[-1] == (['/usr/bin/rbw', 'edit', '--folder', 'sshPilot', 'u@h'], b'newsecret\nSaved by SSH Pilot\n')


def test_rbw_backend_delete(monkeypatch):
    calls, fake = _rbw_fake(unlocked=True)
    monkeypatch.setattr(ss.subprocess, 'run', fake)
    b = ss.RbwBackend(); b._bin = '/usr/bin/rbw'
    assert b.delete(password_spec('h', 'u')) is True
    assert calls[-1][0] == ['/usr/bin/rbw', 'remove', '--folder', 'sshPilot', 'u@h']


def test_rbw_registered_passive_and_availability(monkeypatch):
    monkeypatch.setattr(ss, 'is_macos', lambda: False)
    mgr = ss.SecretManager()
    assert 'rbw' in mgr.registered_backends()
    b = mgr.get_backend('rbw')
    assert isinstance(b, ss.RbwBackend)
    assert b.session_backed is False           # rbw-agent owns the unlock lifecycle
    assert mgr.is_session_backed('rbw') is False
    monkeypatch.setattr(ss, 'resolve_host_binary', lambda name: None)
    assert b.is_available() is False
    monkeypatch.setattr(ss, 'resolve_host_binary', lambda name: ['/usr/bin/rbw'])
    assert b.is_available() is True


# --- ssh-agent "don't store" null backend ------------------------------------

def test_ssh_agent_backend_is_null():
    a = ss.SSHAgentBackend()
    assert a.is_available() is True
    assert a.store(password_spec('h', 'u'), 's') is True   # claims success
    assert a.lookup(password_spec('h', 'u')) is None
    assert a.delete(password_spec('h', 'u')) is False


def test_persists_secrets_false_for_agent(manager):
    mgr, *_ = manager
    mgr.register_backend('agent', ss.SSHAgentBackend())
    assert mgr.persists_secrets() is True
    mgr.set_selected('agent')
    assert mgr.persists_secrets() is False
    mgr.set_selected('auto')
    assert mgr.persists_secrets() is True


def test_agent_selected_stores_nothing_and_blocks_fallback(manager):
    mgr, primary, fallback = manager
    mgr.register_backend('agent', ss.SSHAgentBackend())
    mgr.set_selected('agent')
    spec = password_spec('h', 'u')
    assert mgr.store(spec, 's') is True
    assert spec.keyring_account not in primary.data    # wrote nowhere
    assert spec.keyring_account not in fallback.data


def test_agent_selected_lookup_ignores_other_stores(manager):
    mgr, primary, fallback = manager
    mgr.register_backend('agent', ss.SSHAgentBackend())
    spec = password_spec('h', 'u')
    primary.data[spec.keyring_account] = 'leftover'
    mgr.set_selected('agent')
    assert mgr.lookup(spec) is None                     # authoritative: no read-through


def test_agent_selected_delete_only_consults_agent(manager):
    mgr, primary, fallback = manager
    mgr.register_backend('agent', ss.SSHAgentBackend())
    spec = password_spec('h', 'u')
    primary.data[spec.keyring_account] = 'x'
    fallback.data[spec.keyring_account] = 'y'
    mgr.set_selected('agent')
    assert mgr.delete(spec) is False
    assert spec.keyring_account in primary.data
    assert spec.keyring_account in fallback.data


# --- session-backed backends (unlock / lock / timeout) -----------------------

class FakeSessionBackend(ss.SecretBackend):
    session_backed = True

    def __init__(self, name='vault'):
        self.name = name
        self._unlocked = False
        self.data = {}

    def is_available(self):
        return True

    def is_unlocked(self):
        return self._unlocked

    def unlock(self, secret):
        self._unlocked = (secret == 'correct')
        return self._unlocked

    def lock(self):
        self._unlocked = False

    def store(self, spec, secret):
        if not self._unlocked:
            return False
        self.data[spec.keyring_account] = secret
        return True

    def lookup(self, spec):
        return self.data.get(spec.keyring_account) if self._unlocked else None

    def delete(self, spec):
        return self.data.pop(spec.keyring_account, None) is not None


def test_selected_needs_unlock_and_unlock_selected(manager):
    mgr, *_ = manager
    mgr.register_backend('vault', FakeSessionBackend())
    mgr.set_selected('vault')
    assert mgr.selected_needs_unlock() is True
    assert mgr.unlock_selected('wrong') is False
    assert mgr.selected_needs_unlock() is True
    assert mgr.unlock_selected('correct') is True
    assert mgr.selected_needs_unlock() is False


def test_lock_all_relocks_session_backend(manager):
    mgr, *_ = manager
    mgr.register_backend('vault', FakeSessionBackend())
    mgr.set_selected('vault')
    assert mgr.unlock_selected('correct') is True
    mgr.lock_all()
    assert mgr.selected_needs_unlock() is True


def test_store_no_fallback_when_selected_session_backend_locked(manager):
    # A selected, available-but-locked session backend must NOT silently fall back
    # to libsecret/keyring — otherwise the secret lands somewhere the user didn't pick.
    mgr, primary, fallback = manager
    vault = FakeSessionBackend('vault')          # session_backed, available, locked
    mgr.register_backend('vault', vault)
    mgr.set_selected('vault')
    spec = password_spec('h', 'u')

    assert mgr.store(spec, 's') is False          # stored nowhere
    assert spec.keyring_account not in primary.data
    assert spec.keyring_account not in fallback.data

    # Once unlocked, the secret goes to the selected backend (still no fallback).
    assert vault.unlock('correct') is True
    assert mgr.store(spec, 's') is True
    assert vault.data[spec.keyring_account] == 's'
    assert spec.keyring_account not in primary.data


def test_lookup_no_fallthrough_when_selected_session_backend_locked(manager):
    # Security: a locked (or failed-to-unlock) selected vault must NOT serve a stale
    # copy of the secret from another store. A wrong master password => no access.
    mgr, primary, fallback = manager
    vault = FakeSessionBackend('vault')          # session_backed, available, locked
    mgr.register_backend('vault', vault)
    mgr.set_selected('vault')
    spec = password_spec('h', 'u')
    primary.data[spec.keyring_account] = 'stale-libsecret-pw'   # legacy copy elsewhere

    assert mgr.unlock_selected('wrong') is False  # failed unlock -> still locked
    assert mgr.lookup(spec) is None               # must NOT fall through to libsecret

    # Unlocked but secret not in vault: still no read-through to legacy stores.
    assert vault.unlock('correct') is True
    assert mgr.lookup(spec) is None


def test_lookup_no_fallthrough_when_explicit_session_backend_unlocked(manager):
    mgr, primary, fallback = manager
    vault = FakeSessionBackend('vault')
    vault.unlock('correct')
    mgr.register_backend('vault', vault)
    mgr.set_selected('vault')
    spec = password_spec('h', 'u')
    primary.data[spec.keyring_account] = 'stale-libsecret-pw'
    fallback.data[spec.keyring_account] = 'stale-keyring-pw'
    assert mgr.lookup(spec) is None


def test_delete_only_selected_backend_when_explicit(manager):
    mgr, primary, fallback = manager
    spec = password_spec('h', 'u')
    primary.data[spec.keyring_account] = 'in-libsecret'
    fallback.data[spec.keyring_account] = 'in-keyring'
    mgr.set_selected('keyring')
    assert mgr.delete(spec) is True
    assert spec.keyring_account not in fallback.data
    assert spec.keyring_account in primary.data


def test_store_no_fallback_when_explicit_session_backend_unlocked(manager):
    # Unlocked vault selected but store fails -> must not land in libsecret/keyring.
    mgr, primary, fallback = manager
    vault = FakeSessionBackend('vault')
    vault.unlock('correct')

    def fail_store(spec, secret):
        return False

    vault.store = fail_store
    mgr.register_backend('vault', vault)
    mgr.set_selected('vault')
    spec = password_spec('h', 'u')
    assert mgr.store(spec, 's') is False
    assert spec.keyring_account not in primary.data
    assert spec.keyring_account not in fallback.data


def test_unavailable_explicit_backend_warns_and_no_fallthrough(manager, caplog):
    # Selecting a backend that is UNAVAILABLE (e.g. bitwarden with no `bw`) must not
    # silently resolve to nothing — it logs a warning — and must NOT read/write a stale
    # copy in another store.
    import logging
    mgr, primary, fallback = manager
    broken = FakeBackend('broken', available=False)
    broken.fail_store = True                       # an unavailable store fails
    mgr.register_backend('broken', broken)
    mgr.set_selected('broken')
    spec = password_spec('h', 'u')
    fallback.data[spec.keyring_account] = 'stale-keyring'   # a stale copy elsewhere

    with caplog.at_level(logging.WARNING):
        assert mgr.lookup(spec) is None            # no fallthrough to keyring
        assert mgr.store(spec, 's') is False       # not stored anywhere
    assert spec.keyring_account not in primary.data
    assert 'unavailable' in caplog.text.lower() and 'broken' in caplog.text.lower()


def test_unavailable_backend_warning_is_deduped(manager, caplog):
    import logging
    mgr, primary, fallback = manager
    mgr.register_backend('broken', FakeBackend('broken', available=False))
    mgr.set_selected('broken')
    spec = password_spec('h', 'u')
    with caplog.at_level(logging.WARNING):
        mgr.lookup(spec)
        mgr.lookup(spec)
        mgr.lookup(spec)
    hits = [r for r in caplog.records if 'unavailable' in r.getMessage().lower()]
    assert len(hits) == 1                          # warned once, not per call


def test_master_password_spec_uses_only_schema_attrs():
    spec = ss.master_password_spec('bitwarden', '')
    assert spec.keyring_account == 'bitwarden-master:default'
    assert spec.attributes == {
        'application': 'sshPilot',
        'type': 'vault_master',
        'key_path': 'bitwarden-master:default',
    }
    # libsecret's schema only has these attribute names; must not invent new ones.
    assert set(spec.attributes) <= {'application', 'type', 'key_path', 'host', 'username'}
    # distinct per account/profile so multiple accounts don't collide.
    assert ss.master_password_spec('bitwarden', '/data/work').keyring_account \
        != spec.keyring_account


def test_master_password_stored_in_keyring_not_selected_vault(manager):
    # The master password must land in the platform keyring (libsecret) even when a
    # session vault is the selected backend — it can't live in the vault it unlocks.
    mgr, primary, fallback = manager
    vault = FakeSessionBackend('vault')
    vault.unlock('correct')
    mgr.register_backend('vault', vault)
    mgr.set_selected('vault')
    spec = ss.master_password_spec('vault', '')

    assert mgr.store_in_keyring(spec, 'master-pw') is True
    assert primary.data[spec.keyring_account] == 'master-pw'     # libsecret got it
    assert spec.keyring_account not in vault.data                # NOT the selected vault
    assert mgr.lookup_in_keyring(spec) == 'master-pw'
    assert mgr.delete_in_keyring(spec) is True
    assert mgr.lookup_in_keyring(spec) is None


def test_selected_master_spec_tracks_selection_and_profile(manager, monkeypatch):
    # The unlock dialog (save) and Preferences (forget) must key off the SAME spec.
    mgr, primary, fallback = manager
    mgr.register_backend('vault', FakeSessionBackend('vault'))
    mgr.set_selected('vault')
    monkeypatch.delenv('BITWARDENCLI_APPDATA_DIR', raising=False)
    assert ss.selected_master_spec(mgr).keyring_account == 'vault-master:default'
    monkeypatch.setenv('BITWARDENCLI_APPDATA_DIR', '/data/work')
    assert ss.selected_master_spec(mgr).keyring_account == 'vault-master:/data/work'


def test_lookup_in_keyring_ignores_non_keyring_backend_copy(manager):
    # A value present only in a non-keyring backend (e.g. `pass`) must not be returned by
    # the keyring-only lookup used for the master password.
    mgr, primary, fallback = manager
    extra = mgr._backends['pass']
    extra._available = True
    spec = ss.master_password_spec('bitwarden', '')
    extra.data[spec.keyring_account] = 'in-pass'
    assert mgr.lookup_in_keyring(spec) is None


def test_bitwarden_is_available_reresolves_bw(monkeypatch):
    # is_available() must reflect a `bw` that appears AFTER the backend is built,
    # so a newly-installed CLI is detected without restarting the app.
    from sshpilot import platform_utils

    present = {'ok': False}

    def fake_resolve():
        if not present['ok']:
            return None
        return ['/usr/local/bin/bw']

    # is_available() resolves the argv prefix via the non-verifying (cheap) path,
    # which never spawns `bw --version` on the GTK main thread.
    monkeypatch.setattr(platform_utils, 'resolve_bw_cli_unverified', fake_resolve)
    backend = ss.BitwardenBackend()
    assert backend.is_available() is False
    present['ok'] = True                          # bw installed after construction
    assert backend.is_available() is True


def test_bitwarden_flatpak_uses_host_spawn(monkeypatch):
    from sshpilot import platform_utils

    calls = []

    def fake_resolve():
        return ['/usr/bin/flatpak-spawn', '--host', 'bw']

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _Result(0, b'You are logged in!\n')

    monkeypatch.setattr(platform_utils, 'resolve_bw_cli_unverified', fake_resolve)
    monkeypatch.setattr(ss.subprocess, 'run', fake_run)
    backend = ss.BitwardenBackend()
    assert backend.is_available() is True
    assert backend.needs_login() is False
    assert calls == [['/usr/bin/flatpak-spawn', '--host', 'bw', '--nointeraction', 'login', '--check']]


def test_non_session_backend_needs_no_unlock(manager):
    mgr, *_ = manager
    mgr.set_selected('keyring')
    assert mgr.selected_needs_unlock() is False
    assert mgr.unlock_selected('whatever') is True   # no-op success


# --- Bitwarden / Vaultwarden backend argv & session --------------------------

class _Result:
    def __init__(self, returncode=0, stdout=b'', stderr=b''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeBw:
    """In-memory stand-in for the ``bw`` CLI, installed as ``ss.subprocess.run``."""

    def __init__(self, status="locked", items=None, *, unlock_ok=True, folders=None):
        self.status = status
        self.items = [dict(i) for i in (items or [])]
        self.folders = [dict(f) for f in (folders or [])]
        self.unlock_ok = unlock_ok
        self.calls = []            # each: bw args (without bin / --nointeraction)
        self.envs = []             # the env dict each call was spawned with
        self.search_terms = []     # search values seen on `list items` (None = full)
        self._n = 0

    @staticmethod
    def _bw_command(argv):
        args = list(argv)
        if len(args) >= 3 and os.path.basename(args[0]) == "flatpak-spawn" and args[1] == "--host":
            args = args[3:]
        elif args:
            args = args[1:]
        return [a for a in args if a != '--nointeraction']

    def run(self, argv, input=None, capture_output=None, env=None, check=None, timeout=None):
        cmd = self._bw_command(argv)
        self.calls.append(cmd)
        self.envs.append(dict(env or {}))
        if cmd[:1] == ['status']:
            return _Result(0, json.dumps({"status": self.status}).encode())
        if cmd[:2] == ['login', '--check']:
            if self.status == "unauthenticated":
                return _Result(1, b'', b'You are not logged in.')
            return _Result(0, b'You are logged in!\n')
        if cmd[:1] == ['unlock']:
            if self.status == "unauthenticated" or not self.unlock_ok:
                return _Result(1, b'', b'unlock failed')
            self.status = "unlocked"
            return _Result(0, b'SESSION\n')
        if cmd[:1] == ['sync'] or cmd[:1] == ['config']:
            return _Result(0, b'')
        if cmd[:1] == ['logout']:
            self.status = "unauthenticated"
            return _Result(0, b'')
        if cmd[:1] == ['login']:
            if '--apikey' in cmd:
                if not (env or {}).get('BW_CLIENTID') or not (env or {}).get('BW_CLIENTSECRET'):
                    return _Result(1, b'', b'client id and secret required')
                self.status = "locked"
                return _Result(0, b'')
            if cmd[:2] == ['login', '--sso']:
                self.status = "locked"
                return _Result(0, b'')
            email = cmd[1] if len(cmd) > 1 and not cmd[1].startswith('-') else ''
            if email == 'bad@example.com':
                return _Result(1, b'', b'Invalid credentials')
            if '--code' not in cmd and email == '2fa@example.com':
                return _Result(
                    1,
                    b'{"success":false,"message":"Login failed."}',
                )
            if email:
                if '--raw' in cmd:
                    self.status = "unlocked"
                    if '--response' in cmd:
                        return _Result(
                            0,
                            json.dumps({"success": True, "data": "LOGIN_SESSION"}).encode(),
                        )
                    return _Result(0, b'LOGIN_SESSION\n')
                self.status = "locked"
                return _Result(0, b'')
            return _Result(1, b'', b'email required')
        if cmd[:2] == ['list', 'items']:
            search = cmd[cmd.index('--search') + 1] if '--search' in cmd else None
            self.search_terms.append(search)
            items = self.items
            if search:
                items = [i for i in items if search.lower() in (i.get("name", "").lower())]
            return _Result(0, json.dumps(items).encode())
        if cmd[:2] == ['list', 'folders']:
            return _Result(0, json.dumps(self.folders).encode())
        if cmd[:2] == ['create', 'folder']:
            self._n += 1
            folder = json.loads(base64.b64decode(input).decode())
            folder["id"] = f"FOLDER{self._n}"
            self.folders.append(folder)
            return _Result(0, json.dumps(folder).encode())
        if cmd[:2] == ['create', 'item']:
            self._n += 1
            item = json.loads(base64.b64decode(input).decode())
            item["id"] = f"NEW{self._n}"
            self.items.append(item)
            return _Result(0, json.dumps(item).encode())
        if cmd[:2] == ['edit', 'item']:
            iid = cmd[2]
            item = json.loads(base64.b64decode(input).decode())
            for idx, it in enumerate(self.items):
                if it.get("id") == iid:
                    self.items[idx] = item
            return _Result(0, json.dumps(item).encode())
        if cmd[:2] == ['delete', 'item']:
            iid = cmd[2]
            self.items = [it for it in self.items if it.get("id") != iid]
            return _Result(0, b'')
        return _Result(1, b'', b'unknown')


def _boom_bw(*_a, **_k):
    raise AssertionError("warm-cache operation must not spawn bw")


def _make_backend(monkeypatch, fake):
    """A BitwardenBackend wired to a FakeBw via ss.subprocess.run."""
    b = ss.BitwardenBackend()
    b._bin = '/usr/bin/bw'
    monkeypatch.setattr(ss.subprocess, 'run', fake.run)
    return b


@pytest.fixture(autouse=True)
def _clean_bw_session():
    # unlock() exports BW_SESSION to os.environ directly (not via monkeypatch), so
    # isolate it around every test.
    os.environ.pop('BW_SESSION', None)
    yield
    os.environ.pop('BW_SESSION', None)


def test_bitwarden_unlock_warms_full_cache(monkeypatch):
    # unlock() warms the whole-vault cache (one full `bw list items`) while the spinner
    # is up, so lookups afterwards are in-memory hits.
    fake = FakeBw(status="locked",
                  items=[{"id": "ID1", "name": "u@h", "login": {"password": "hunter2"}}])
    b = _make_backend(monkeypatch, fake)
    assert b.unlock('master') is True
    assert fake.status == "unlocked"
    assert ['unlock'] in [c[:1] for c in fake.calls]
    assert fake.search_terms == [None]              # one whole-vault list (no --search)
    assert b._cache_complete is True
    monkeypatch.setattr(ss.subprocess, 'run', _boom_bw)
    assert b.lookup(password_spec('h', 'u')) == 'hunter2'   # warm-cache hit, no spawn


def test_lookup_after_unlock_is_cache_hit(monkeypatch):
    # After unlock, lookups hit the warm full cache — both a hit and a definitive miss
    # spawn no bw.
    fake = FakeBw(status="locked",
                  items=[{"id": "ID1", "name": "u@h", "login": {"password": "pw"}}])
    b = _make_backend(monkeypatch, fake)
    assert b.unlock('master') is True
    monkeypatch.setattr(ss.subprocess, 'run', _boom_bw)
    assert b.lookup(password_spec('h', 'u')) == 'pw'     # hit
    assert b.lookup(password_spec('nope', 'x')) is None  # definitive miss (complete cache)


def test_unlock_full_load_failure_falls_back_to_search(monkeypatch):
    # If the whole-vault warm load fails, the cache is not 'complete' and lookups fall
    # back to a targeted search rather than silently missing.
    fake = FakeBw(status="locked",
                  items=[{"id": "ID1", "name": "u@h", "login": {"password": "pw"}}])
    b = _make_backend(monkeypatch, fake)
    real = fake.run
    fail = {"full": True}

    def flaky(argv, **k):
        cmd = [a for a in list(argv)[1:] if a != '--nointeraction']
        if cmd[:2] == ['list', 'items'] and '--search' not in cmd and fail["full"]:
            return _Result(1, b'', b'boom')         # whole-vault load fails
        return real(argv, **k)

    monkeypatch.setattr(ss.subprocess, 'run', flaky)
    assert b.unlock('m') is True
    assert b._cache_complete is False               # warm load failed
    assert b.lookup(password_spec('h', 'u')) == 'pw'  # falls back to targeted search
    assert 'u@h' in fake.search_terms


def test_bitwarden_lookup_cache_hit_no_spawn(monkeypatch):
    # A cache hit makes no bw spawn at all.
    b = ss.BitwardenBackend()
    b._bin = '/usr/bin/bw'
    b._items = {'u@h': {'name': 'u@h', 'id': 'ID1', 'login': {'password': 'cached'}}}
    monkeypatch.setattr(ss.subprocess, 'run', _boom_bw)
    assert b.lookup(password_spec('h', 'u')) == 'cached'


def test_bitwarden_logout_runs_bw_logout(monkeypatch):
    fake = FakeBw(status="unlocked")
    b = _make_backend(monkeypatch, fake)
    b._unlocked = True
    b._token = "TOK"
    assert b.logout() is True
    assert fake.calls == [['logout']]
    assert fake.status == "unauthenticated"
    assert b.is_unlocked() is False


def test_bitwarden_login_with_password(monkeypatch):
    fake = FakeBw(status="unauthenticated")
    b = _make_backend(monkeypatch, fake)
    ok, detail, needs_2fa = b.login_with_password("user@host", "secret")
    assert ok is True
    assert needs_2fa is False
    assert fake.status == "unlocked"
    assert '--raw' in fake.calls[0]
    assert '--response' in fake.calls[0]
    assert fake.calls[0][:3] == ['login', 'user@host', '--passwordenv']
    assert b.is_unlocked() is True
    assert os.environ.get("BW_SESSION") == "LOGIN_SESSION"


def test_bitwarden_login_with_password_auth_challenge(monkeypatch):
    fake = FakeBw(status="unauthenticated")
    b = _make_backend(monkeypatch, fake)
    ok, _, _ = b.login_with_password(
        "user@host", "secret", auth_client_secret="challenge-secret",
    )
    assert ok is True
    assert fake.envs[0].get("BW_CLIENTSECRET") == "challenge-secret"


def test_bitwarden_login_with_password_needs_2fa(monkeypatch):
    fake = FakeBw(status="unauthenticated")
    b = _make_backend(monkeypatch, fake)
    ok, detail, needs_2fa = b.login_with_password("2fa@example.com", "secret")
    assert ok is False
    assert needs_2fa is True
    assert detail == "Login failed."
    ok, detail, needs_2fa = b.login_with_password(
        "2fa@example.com", "secret", twofa_method="1", twofa_code="123456",
    )
    assert ok is True
    assert needs_2fa is False


def test_bitwarden_login_needs_2fa_generic_login_failed():
    assert ss.BitwardenBackend._login_needs_2fa("Login failed.") is True
    assert ss.BitwardenBackend._login_needs_2fa("Login failed.", twofa_code="123456") is False
    assert ss.BitwardenBackend._login_needs_2fa("Invalid master password.") is False


def test_bitwarden_login_needs_2fa_no_provider_selected():
    # ``bw login`` (no --method) when 2FA is enabled fails with this message.
    assert ss.BitwardenBackend._login_needs_2fa("Login failed. No provider selected.") is True
    # Once a code is supplied, a repeat is a wrong-provider error, not a re-prompt.
    assert ss.BitwardenBackend._login_needs_2fa(
        "Login failed. No provider selected.", twofa_code="123456",
    ) is False


def test_bitwarden_login_with_api_key(monkeypatch):
    fake = FakeBw(status="unauthenticated")
    b = _make_backend(monkeypatch, fake)
    ok, detail = b.login_with_api_key("user.clientId", "secret")
    assert ok is True
    assert fake.status == "locked"


def test_bitwarden_cold_lookup_uses_targeted_search(monkeypatch):
    # The askpass-subprocess case: no warm cache, an inherited BW_SESSION -> one targeted
    # `bw list items --search` (not a whole-vault load), and the hit is cached.
    fake = FakeBw(status="unlocked",
                  items=[{"id": "ID1", "name": "u@h", "login": {"password": "pw"}},
                         {"id": "ID2", "name": "other", "login": {"password": "x"}}])
    b = _make_backend(monkeypatch, fake)
    monkeypatch.setenv('BW_SESSION', 'TOK')                 # inherited session
    assert b._items is None                                 # cold
    assert b.lookup(password_spec('h', 'u')) == 'pw'
    assert fake.search_terms == ['u@h']                     # targeted, not a full list
    assert (b._items or {}).get('u@h') is not None          # hit cached
    monkeypatch.setattr(ss.subprocess, 'run', _boom_bw)
    assert b.lookup(password_spec('h', 'u')) == 'pw'        # repeat is a cache hit


def test_bitwarden_unlock_reports_progress(monkeypatch):
    # The spinner dialog's status text rides these stages.
    fake = FakeBw(status="locked")
    b = _make_backend(monkeypatch, fake)
    stages = []
    assert b.unlock('m', progress=stages.append) is True
    assert stages == ['starting', 'unlocking', 'loading']


def test_unlock_selected_forwards_progress(monkeypatch):
    fake = FakeBw(status="locked")
    b = _make_backend(monkeypatch, fake)
    mgr = ss.SecretManager()
    mgr._backends = {'bitwarden': b}
    mgr.set_selected('bitwarden')
    stages = []
    assert mgr.unlock_selected('m', progress=stages.append) is True
    assert 'unlocking' in stages


def test_bitwarden_stays_unlocked_after_unlock(monkeypatch):
    # Regression: after a successful unlock, is_unlocked() stays True (no re-prompt on
    # every connection) without spawning bw.
    fake = FakeBw(status="locked",
                  items=[{"id": "ID1", "name": "u@h", "login": {"password": "pw"}}])
    b = _make_backend(monkeypatch, fake)
    assert b.unlock('m') is True
    monkeypatch.setattr(ss.subprocess, 'run', _boom_bw)  # is_unlocked must not spawn bw
    assert b.is_unlocked() is True

    mgr = SecretManager()
    mgr._backends = {'bitwarden': b}
    mgr.set_selected('bitwarden')
    assert mgr.selected_needs_unlock() is False   # -> no re-prompt on the next connect


def test_bitwarden_idle_timeout(monkeypatch):
    # With a non-zero idle timeout the session is dropped after the window, so the next
    # is_unlocked() is False (the connect will re-prompt).
    monkeypatch.setenv('SSHPILOT_SECRET_SESSION_TIMEOUT', '60')
    clock = {'t': 1000.0}
    monkeypatch.setattr(ss.time, 'monotonic', lambda: clock['t'])
    fake = FakeBw(status='locked')
    b = _make_backend(monkeypatch, fake)
    assert b.unlock('m') is True
    assert b.is_unlocked() is True            # within the window
    clock['t'] += 61                          # idle past 60s
    assert b.is_unlocked() is False           # expired -> re-locked
    assert b._token is None
    assert os.environ.get('BW_SESSION') is None


def test_bitwarden_no_idle_timeout_by_default(monkeypatch):
    monkeypatch.setenv('SSHPILOT_SECRET_SESSION_TIMEOUT', '0')   # default = keep unlocked
    clock = {'t': 1000.0}
    monkeypatch.setattr(ss.time, 'monotonic', lambda: clock['t'])
    fake = FakeBw(status='locked')
    b = _make_backend(monkeypatch, fake)
    assert b.unlock('m') is True
    clock['t'] += 10_000_000                   # far future
    assert b.is_unlocked() is True             # never expires


def test_bitwarden_is_unlocked_locked_no_spawn(monkeypatch):
    # When locked (no token, no inherited BW_SESSION), is_unlocked() is False and spawns
    # no bw.
    monkeypatch.delenv('BW_SESSION', raising=False)
    b = ss.BitwardenBackend()
    b._bin = '/usr/bin/bw'
    monkeypatch.setattr(ss.subprocess, 'run', _boom_bw)
    assert b.is_unlocked() is False


def test_bitwarden_store_creates_enriched_item(monkeypatch):
    fake = FakeBw(status="locked")   # empty vault
    b = _make_backend(monkeypatch, fake)
    assert b.unlock('m') is True
    assert b.store(password_spec('h', 'u'), 'secret') is True
    assert ['create', 'item'] in [c[:2] for c in fake.calls]
    created = [i for i in fake.items if i.get("name") == "u@h"]
    assert len(created) == 1
    item = created[0]
    assert item['login']['password'] == 'secret'
    assert item['login']['username'] == 'u'                        # enriched
    assert item['login']['uris'] == [{'match': None, 'uri': 'ssh://h'}]
    assert item['notes'] == 'Saved by SSH Pilot'


def test_bitwarden_store_updates_cache(monkeypatch):
    fake = FakeBw(status="locked")
    b = _make_backend(monkeypatch, fake)
    assert b.unlock('m') is True
    assert b.store(password_spec('h', 'u'), 'secret') is True
    monkeypatch.setattr(ss.subprocess, 'run', _boom_bw)
    assert b.lookup(password_spec('h', 'u')) == 'secret'   # served from updated cache, no spawn


def test_bitwarden_store_edits_existing(monkeypatch):
    fake = FakeBw(status="locked",
                  items=[{"id": "ID1", "name": "u@h", "login": {"password": "old"}}])
    b = _make_backend(monkeypatch, fake)
    assert b.unlock('m') is True
    assert b.store(password_spec('h', 'u'), 'new') is True
    assert ['edit', 'item'] in [c[:2] for c in fake.calls]
    assert fake.items[0]["login"]["password"] == "new"


def test_bitwarden_delete(monkeypatch):
    fake = FakeBw(status="locked",
                  items=[{"id": "ID1", "name": "u@h", "login": {"password": "pw"}}])
    b = _make_backend(monkeypatch, fake)
    assert b.unlock('m') is True
    assert b.delete(password_spec('h', 'u')) is True
    assert ['delete', 'item'] in [c[:2] for c in fake.calls]
    assert fake.items == []
    monkeypatch.setattr(ss.subprocess, 'run', _boom_bw)
    assert b.lookup(password_spec('h', 'u')) is None       # cache dropped, no spawn


def test_bitwarden_lock_clears_session(monkeypatch):
    # lock() drops the in-process session + cache and the BW_SESSION env, instantly,
    # without spawning `bw lock`.
    fake = FakeBw(status="locked")
    b = _make_backend(monkeypatch, fake)
    assert b.unlock('m') is True
    assert os.environ.get('BW_SESSION') == 'SESSION'
    monkeypatch.setattr(ss.subprocess, 'run', _boom_bw)  # lock must not spawn bw
    b.lock()
    assert b._items is None
    assert b._token is None
    assert b._unlocked is False
    assert b._cache_complete is False
    assert os.environ.get('BW_SESSION') is None


def test_bitwarden_needs_login(monkeypatch):
    # `bw login --check` exit 1 surfaces as needs_login() so the UI can tell the user
    # to run `bw login` instead of failing silently.
    fake = FakeBw(status="unauthenticated")
    b = _make_backend(monkeypatch, fake)
    assert b.needs_login() is True

    mgr = SecretManager()
    mgr._backends = {'bitwarden': b}
    mgr.set_selected('bitwarden')
    assert mgr.selected_needs_login() is True


def test_bitwarden_needs_login_is_cached_between_connections(monkeypatch):
    # The connect gate asks on every connection attempt. A signed-out result is stable,
    # so only the first attempt should pay for the slow `bw` Node process startup.
    fake = FakeBw(status="unauthenticated")
    b = _make_backend(monkeypatch, fake)

    assert b.needs_login() is True
    assert b.needs_login() is True
    assert b.needs_login() is True
    assert fake.calls == [['login', '--check']]


def test_bitwarden_needs_login_force_refresh_detects_external_login(monkeypatch):
    # Preferences can explicitly refresh after the account changes outside sshPilot.
    fake = FakeBw(status="unauthenticated")
    b = _make_backend(monkeypatch, fake)
    assert b.needs_login() is True

    fake.status = "locked"
    assert b.needs_login() is True                  # cached connection hot path
    assert b.needs_login(force_refresh=True) is False
    assert b.needs_login() is False                 # refreshed state is now cached
    assert fake.calls == [['login', '--check'], ['login', '--check']]


def test_bitwarden_needs_login_cache_is_scoped_to_profile(monkeypatch):
    fake = FakeBw(status="unauthenticated")
    b = _make_backend(monkeypatch, fake)
    monkeypatch.setenv("BITWARDENCLI_APPDATA_DIR", "/profiles/personal")
    assert b.needs_login() is True

    fake.status = "locked"
    monkeypatch.setenv("BITWARDENCLI_APPDATA_DIR", "/profiles/work")
    assert b.needs_login() is False
    assert fake.calls == [['login', '--check'], ['login', '--check']]


def test_bitwarden_login_and_logout_update_cached_account_state(monkeypatch):
    fake = FakeBw(status="unauthenticated")
    b = _make_backend(monkeypatch, fake)
    assert b.needs_login() is True

    assert b.login_with_api_key("client", "secret") == (True, "")
    monkeypatch.setattr(ss.subprocess, 'run', _boom_bw)
    assert b.needs_login() is False                 # no post-login status spawn

    monkeypatch.setattr(ss.subprocess, 'run', fake.run)
    assert b.logout() is True
    monkeypatch.setattr(ss.subprocess, 'run', _boom_bw)
    assert b.needs_login() is True                  # no post-logout status spawn


def test_one_session_backend_no_vaultwarden(monkeypatch):
    # Bitwarden + Vaultwarden were merged into one `bw` backend; there is no separate
    # 'vaultwarden' backend, and availability is bin-only (no server URL gate).
    assert not hasattr(ss, 'VaultwardenBackend')
    mgr = ss.SecretManager()
    assert 'vaultwarden' not in mgr.registered_backends()
    assert 'bitwarden' in mgr.registered_backends()
    b = ss.BitwardenBackend()
    b._bin = '/usr/bin/bw'
    assert b.is_available() is True            # no server URL required
    assert b.describe() == 'bitwarden'


def test_bitwarden_profile_env_passed_to_bw(monkeypatch):
    # Selecting an account profile sets BITWARDENCLI_APPDATA_DIR in the environment, which
    # every `bw` spawn (and the inherited askpass subprocess) must carry.
    monkeypatch.setenv('BITWARDENCLI_APPDATA_DIR', '/home/u/.config/Bitwarden CLI Work')
    fake = FakeBw(status='locked',
                  items=[{"id": "ID1", "name": "u@h", "login": {"password": "pw"}}])
    b = _make_backend(monkeypatch, fake)
    assert b.unlock('m') is True
    assert fake.envs, "no bw spawn captured"
    assert all(e.get('BITWARDENCLI_APPDATA_DIR') == '/home/u/.config/Bitwarden CLI Work'
               for e in fake.envs)


def test_bitwarden_no_profile_env_when_unset(monkeypatch):
    monkeypatch.delenv('BITWARDENCLI_APPDATA_DIR', raising=False)
    fake = FakeBw(status='locked')
    b = _make_backend(monkeypatch, fake)
    assert b.unlock('m') is True
    assert all('BITWARDENCLI_APPDATA_DIR' not in e for e in fake.envs)


# --- sshPilot folder organization -------------------------------------------

def test_bitwarden_creates_and_files_new_item_in_sshpilot_folder(monkeypatch):
    fake = FakeBw(status="unlocked", items=[], folders=[])
    b = _make_backend(monkeypatch, fake)
    monkeypatch.setenv('BW_SESSION', 'TOK')
    assert b.store(password_spec('h', 'u'), 'pw') is True
    # A sshPilot folder was created and the new login item filed under it.
    assert [f['name'] for f in fake.folders] == ['sshPilot']
    fid = fake.folders[0]['id']
    created = next(i for i in fake.items if i.get('name') == 'u@h')
    assert created.get('folderId') == fid
    # Folder id is cached — a second store does NOT create another folder.
    assert b.store(ss.sudo_password_spec('h', 'u'), 'pw2') is True
    assert sum(1 for c in fake.calls if c[:2] == ['create', 'folder']) == 1


def test_bitwarden_reuses_existing_sshpilot_folder(monkeypatch):
    fake = FakeBw(status="unlocked", items=[], folders=[{"id": "F1", "name": "sshPilot"}])
    b = _make_backend(monkeypatch, fake)
    monkeypatch.setenv('BW_SESSION', 'TOK')
    assert b.store(password_spec('h', 'u'), 'pw') is True
    assert sum(1 for c in fake.calls if c[:2] == ['create', 'folder']) == 0   # reused
    created = next(i for i in fake.items if i.get('name') == 'u@h')
    assert created.get('folderId') == 'F1'


def test_bitwarden_existing_item_keeps_its_folder(monkeypatch):
    fake = FakeBw(status="unlocked",
                  items=[{"id": "ID1", "name": "u@h",
                          "login": {"password": "old"}, "folderId": "OTHER"}],
                  folders=[{"id": "F1", "name": "sshPilot"}])
    b = _make_backend(monkeypatch, fake)
    monkeypatch.setenv('BW_SESSION', 'TOK')
    assert b.store(password_spec('h', 'u'), 'newpw') is True   # updates existing (edit path)
    updated = next(i for i in fake.items if i.get('id') == 'ID1')
    assert updated.get('folderId') == 'OTHER'                  # not moved into sshPilot


def test_bitwarden_secure_note_filed_in_sshpilot_folder(monkeypatch):
    fake = FakeBw(status="unlocked", items=[], folders=[])
    b = _make_backend(monkeypatch, fake)
    monkeypatch.setenv('BW_SESSION', 'TOK')
    item_id = b.create_or_update_secure_note("sshPilot Backup X", "SSHPILOT-BACKUP-v1\n...")
    assert item_id
    note = next(i for i in fake.items if i.get('id') == item_id)
    assert note.get('folderId') == fake.folders[0]['id']
    assert note.get('type') == 2

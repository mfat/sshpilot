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


def test_unavailable_backends_skipped(manager):
    mgr, primary, fallback = manager
    primary._available = False
    spec = password_spec('h', 'u')
    mgr.store(spec, 's')
    assert fallback.data[spec.keyring_account] == 's'
    assert mgr.active_backend_name == 'keyring'


def test_lookup_and_delete_reach_nonselected_available_backend(manager):
    # A secret living only in an available backend that is NOT part of the
    # selected/legacy order (e.g. `pass` while on `auto`) must still resolve and
    # delete — otherwise switching backends orphans secrets.
    mgr, primary, fallback = manager
    extra = mgr._backends['pass']
    extra._available = True
    spec = password_spec('h', 'u')
    extra.data[spec.keyring_account] = 'in-pass'
    assert mgr.lookup(spec) == 'in-pass'        # auto order is libsecret,keyring
    assert mgr.delete(spec) is True
    assert spec.keyring_account not in extra.data


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


# --- ssh-agent "don't store" null backend (authoritative) --------------------

def test_ssh_agent_backend_is_null():
    a = ss.SSHAgentBackend()
    assert a.is_available() is True
    assert a.authoritative is True
    assert a.store(password_spec('h', 'u'), 's') is True   # claims success
    assert a.lookup(password_spec('h', 'u')) is None
    assert a.delete(password_spec('h', 'u')) is False


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


def test_agent_selected_delete_still_clears_other_stores(manager):
    mgr, primary, fallback = manager
    mgr.register_backend('agent', ss.SSHAgentBackend())
    spec = password_spec('h', 'u')
    primary.data[spec.keyring_account] = 'x'
    mgr.set_selected('agent')
    assert mgr.delete(spec) is True
    assert spec.keyring_account not in primary.data


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


def test_bitwarden_is_available_reresolves_bw(monkeypatch):
    # is_available() must reflect a `bw` that appears AFTER the backend is built,
    # so a newly-installed CLI is detected without restarting the app.
    present = {'ok': False}

    def fake_which(name):
        return '/usr/local/bin/bw' if (name == 'bw' and present['ok']) else None

    monkeypatch.setattr(ss.shutil, 'which', fake_which)
    backend = ss.BitwardenBackend()
    assert backend.is_available() is False
    present['ok'] = True                          # bw installed after construction
    assert backend.is_available() is True


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


def _bw_cmd(argv):
    """bw args after the binary, ignoring the global --nointeraction flag."""
    return [a for a in list(argv)[1:] if a != '--nointeraction']


def test_bitwarden_unlock_prefetches_items(monkeypatch):
    calls = []

    def fake_run(argv, input=None, capture_output=None, env=None, check=None, timeout=None):
        calls.append({'cmd': _bw_cmd(argv), 'session': (env or {}).get('BW_SESSION'),
                      'master': (env or {}).get('BW_MASTER')})
        cmd = _bw_cmd(argv)
        if cmd[0] == 'unlock':
            return _Result(0, b'TOKEN123\n')
        if cmd[:2] == ['list', 'items']:
            return _Result(0, json.dumps(
                [{'name': 'u@h', 'id': 'ID1', 'login': {'password': 'hunter2'}}]).encode())
        return _Result(0, b'')

    monkeypatch.setattr(ss.subprocess, 'run', fake_run)
    monkeypatch.setenv('SSHPILOT_SECRET_SESSION_TIMEOUT', '0')
    monkeypatch.delenv('BW_SESSION', raising=False)

    b = ss.BitwardenBackend()
    b._bin = '/usr/bin/bw'
    try:
        assert b.unlock('master') is True
        assert calls[0]['cmd'] == ['unlock', '--passwordenv', 'BW_MASTER', '--raw']
        assert calls[0]['master'] == 'master'
        assert os.environ.get('BW_SESSION') == 'TOKEN123'
        # unlock prefetches items synchronously (warm cache); `bw sync` is backgrounded.
        assert any(c['cmd'][:2] == ['list', 'items'] for c in calls)
        # lookup is now served from the warm cache.
        assert b.lookup(password_spec('h', 'u')) == 'hunter2'
    finally:
        b.lock()
        assert os.environ.get('BW_SESSION') is None


def test_bitwarden_lookup_from_cache_no_spawn(monkeypatch):
    # With a warm cache, lookup must not spawn `bw` at all (hit or miss).
    monkeypatch.setenv('BW_SESSION', 'TOK')

    def boom(*a, **k):
        raise AssertionError("bw must not be spawned on a warm-cache lookup")

    monkeypatch.setattr(ss.subprocess, 'run', boom)
    b = ss.BitwardenBackend()
    b._bin = '/usr/bin/bw'
    b._items = {'u@h': {'name': 'u@h', 'id': 'ID1', 'login': {'password': 'cached'}}}
    assert b.lookup(password_spec('h', 'u')) == 'cached'
    assert b.lookup(password_spec('nope', 'x')) is None


def test_bitwarden_cold_lookup_loads_items_once(monkeypatch):
    # No prefetch (e.g. BW_SESSION inherited): the first lookup loads all items with a
    # single `bw list items`; later lookups hit the cache.
    monkeypatch.setenv('BW_SESSION', 'TOK')
    calls = []

    def fake_run(argv, input=None, capture_output=None, env=None, check=None, timeout=None):
        calls.append(_bw_cmd(argv))
        if _bw_cmd(argv)[:2] == ['list', 'items']:
            return _Result(0, json.dumps(
                [{'name': 'u@h', 'id': 'ID1', 'login': {'password': 'pw'}}]).encode())
        return _Result(0, b'')

    monkeypatch.setattr(ss.subprocess, 'run', fake_run)
    b = ss.BitwardenBackend()
    b._bin = '/usr/bin/bw'
    assert b.lookup(password_spec('h', 'u')) == 'pw'
    assert b.lookup(password_spec('h', 'u')) == 'pw'
    assert [c for c in calls if c[:2] == ['list', 'items']] == [
        ['list', 'items']
    ]  # loaded exactly once


def test_bitwarden_idle_timeout(monkeypatch):
    monkeypatch.setenv('SSHPILOT_SECRET_SESSION_TIMEOUT', '60')
    monkeypatch.delenv('BW_SESSION', raising=False)
    clock = {'now': 1000.0}
    monkeypatch.setattr(ss.time, 'monotonic', lambda: clock['now'])
    monkeypatch.setattr(ss.subprocess, 'run',
                        lambda *a, **k: _Result(0, b'TOKEN\n'))

    b = ss.BitwardenBackend()
    b._bin = '/usr/bin/bw'
    try:
        assert b.unlock('m') is True
        assert b.is_unlocked() is True       # within the 60s window
        clock['now'] = 1000.0 + 61           # idle past the timeout
        assert b.is_unlocked() is False
    finally:
        b.lock()


def test_bitwarden_store_creates_enriched_item(monkeypatch):
    calls = []

    def fake_run(argv, input=None, capture_output=None, env=None, check=None, timeout=None):
        calls.append((_bw_cmd(argv), input))
        cmd = _bw_cmd(argv)
        if cmd[0] == 'unlock':
            return _Result(0, b'TOK\n')
        if cmd[:2] == ['list', 'items']:
            return _Result(0, b'[]')          # empty vault
        if cmd[:2] == ['create', 'item']:
            return _Result(0, b'{}')
        return _Result(0, b'')

    monkeypatch.setattr(ss.subprocess, 'run', fake_run)
    monkeypatch.setenv('SSHPILOT_SECRET_SESSION_TIMEOUT', '0')
    monkeypatch.delenv('BW_SESSION', raising=False)

    b = ss.BitwardenBackend()
    b._bin = '/usr/bin/bw'
    try:
        assert b.unlock('m') is True
        assert b.store(password_spec('h', 'u'), 'secret') is True
        create = [(a, p) for (a, p) in calls if a[:2] == ['create', 'item']]
        assert len(create) == 1
        _argv, payload = create[-1]
        item = json.loads(base64.b64decode(payload).decode())
        assert item['name'] == 'u@h'
        assert item['login']['password'] == 'secret'
        assert item['login']['username'] == 'u'                        # enriched
        assert item['login']['uris'] == [{'match': None, 'uri': 'ssh://h'}]
        assert item['notes'] == 'Saved by SSH Pilot'
    finally:
        b.lock()


def test_bitwarden_store_updates_cache(monkeypatch):
    # After a successful store the cache reflects the new secret with no extra spawn.
    monkeypatch.setenv('BW_SESSION', 'TOK')
    calls = []

    def fake_run(argv, input=None, capture_output=None, env=None, check=None, timeout=None):
        calls.append(_bw_cmd(argv))
        cmd = _bw_cmd(argv)
        if cmd[:2] == ['list', 'items']:
            return _Result(0, b'[]')
        if cmd[:2] == ['create', 'item']:
            return _Result(0, json.dumps(
                {'name': 'u@h', 'id': 'NEW', 'login': {'password': 'secret'}}).encode())
        return _Result(0, b'')

    monkeypatch.setattr(ss.subprocess, 'run', fake_run)
    b = ss.BitwardenBackend()
    b._bin = '/usr/bin/bw'
    assert b.store(password_spec('h', 'u'), 'secret') is True
    n = len(calls)
    assert b.lookup(password_spec('h', 'u')) == 'secret'   # served from updated cache
    assert len(calls) == n                                  # no new bw spawn


def test_bitwarden_lock_clears_cache():
    b = ss.BitwardenBackend()
    b._token = 'TOK'
    b._items = {'u@h': {'login': {'password': 'x'}}}
    b.lock()
    assert b._items is None


def test_bitwarden_lock_spawns_no_bw(monkeypatch):
    # lock() must be instant on shutdown — no `bw lock` subprocess.
    def boom(*a, **k):
        raise AssertionError("lock() must not spawn bw")

    monkeypatch.setattr(ss.subprocess, 'run', boom)
    b = ss.BitwardenBackend()
    b._bin = '/usr/bin/bw'
    b._token = 'TOK'
    b._items = {'u@h': {'login': {'password': 'x'}}}
    monkeypatch.setenv('BW_SESSION', 'TOK')
    b.lock()
    assert b._token is None
    assert b._items is None
    assert os.environ.get('BW_SESSION') is None


def test_bitwarden_delete_finds_id(monkeypatch):
    calls = []

    def fake_run(argv, input=None, capture_output=None, env=None, check=None, timeout=None):
        calls.append(_bw_cmd(argv))
        cmd = _bw_cmd(argv)
        if cmd[0] == 'unlock':
            return _Result(0, b'TOK\n')
        if cmd[:2] == ['list', 'items']:
            return _Result(0, json.dumps([{'name': 'u@h', 'id': 'ID1'}]).encode())
        return _Result(0, b'')

    monkeypatch.setattr(ss.subprocess, 'run', fake_run)
    monkeypatch.setenv('SSHPILOT_SECRET_SESSION_TIMEOUT', '0')
    monkeypatch.delenv('BW_SESSION', raising=False)

    b = ss.BitwardenBackend()
    b._bin = '/usr/bin/bw'
    try:
        assert b.unlock('m') is True
        assert b.delete(password_spec('h', 'u')) is True
        # filter (not calls[-1]) — the background sync may append calls too.
        deletes = [c for c in calls if c[:2] == ['delete', 'item']]
        assert deletes[-1] == ['delete', 'item', 'ID1', '--permanent']
    finally:
        b.lock()


def test_bitwarden_subprocess_inherits_env_token(monkeypatch):
    # In the askpass subprocess there is no in-process token; the inherited
    # BW_SESSION env var must be used so lookups work without unlocking.
    monkeypatch.setenv('BW_SESSION', 'ENVTOK')
    seen = {}

    def fake_run(argv, input=None, capture_output=None, env=None, check=None, timeout=None):
        seen['session'] = (env or {}).get('BW_SESSION')
        if _bw_cmd(argv)[:2] == ['list', 'items']:
            return _Result(0, json.dumps(
                [{'name': 'u@h', 'id': 'ID1', 'login': {'password': 'pw'}}]).encode())
        return _Result(0, b'')

    monkeypatch.setattr(ss.subprocess, 'run', fake_run)
    b = ss.BitwardenBackend()
    b._bin = '/usr/bin/bw'
    assert b.is_unlocked() is True
    assert b.lookup(password_spec('h', 'u')) == 'pw'
    assert seen['session'] == 'ENVTOK'


def test_bitwarden_needs_login(monkeypatch):
    # `bw status` reporting `unauthenticated` surfaces as needs_login(), so the
    # UI can tell the user to run `bw login` instead of failing silently.
    monkeypatch.delenv('BW_SESSION', raising=False)

    def fake_run(argv, input=None, capture_output=None, env=None, check=None, timeout=None):
        if _bw_cmd(argv)[0] == 'status':
            return _Result(0, json.dumps({'status': 'unauthenticated'}).encode())
        return _Result(0, b'')

    monkeypatch.setattr(ss.subprocess, 'run', fake_run)
    b = ss.BitwardenBackend()
    b._bin = '/usr/bin/bw'
    assert b.needs_login() is True

    # And the manager exposes it for the selected session backend.
    mgr = SecretManager()
    mgr._backends = {'bitwarden': b}
    mgr.set_selected('bitwarden')
    assert mgr.selected_needs_login() is True


def test_vaultwarden_requires_server(monkeypatch):
    monkeypatch.delenv('SSHPILOT_VAULTWARDEN_SERVER', raising=False)
    v = ss.VaultwardenBackend()
    v._bin = '/usr/bin/bw'
    assert v.is_available() is False
    monkeypatch.setenv('SSHPILOT_VAULTWARDEN_SERVER', 'https://vw.example')
    assert v.is_available() is True
    assert v.describe() == 'vaultwarden:https://vw.example'

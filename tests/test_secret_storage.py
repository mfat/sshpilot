"""Tests for the pluggable secret storage layer (sshpilot/secret_storage.py)."""

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

class FakeServe:
    """In-memory stand-in for the `bw serve` REST API (used as BitwardenBackend._http)."""

    def __init__(self, status="locked", items=None):
        self.status = status
        self.items = [dict(i) for i in (items or [])]
        self.calls = []
        self.search_terms = []     # search= values seen on /list (None = full list)
        self._n = 0

    def http(self, method, path, *, params=None, body=None, timeout=10):
        self.calls.append((method, path))
        if path == "/status":
            return 200, {"success": True, "data": {"status": self.status}}
        if path == "/unlock":
            if self.status == "unauthenticated":
                return 200, {"success": False, "message": "not logged in"}
            self.status = "unlocked"
            return 200, {"success": True, "data": {"raw": "SESSION"}}
        if path == "/lock":
            self.status = "locked"
            return 200, {"success": True}
        if path == "/sync":
            return 200, {"success": True}
        if path == "/list/object/items":
            search = (params or {}).get("search")
            self.search_terms.append(search)
            items = self.items
            if search:
                items = [i for i in items if search.lower() in (i.get("name", "").lower())]
            return 200, {"success": True, "data": {"object": "list", "data": items}}
        if path == "/object/item" and method == "POST":
            self._n += 1
            item = dict(body or {})
            item["id"] = f"NEW{self._n}"
            self.items.append(item)
            return 200, {"success": True, "data": item}
        if path.startswith("/object/item/"):
            iid = path.rsplit("/", 1)[1]
            if method == "PUT":
                for idx, it in enumerate(self.items):
                    if it.get("id") == iid:
                        upd = dict(body or {})
                        upd["id"] = iid
                        self.items[idx] = upd
                        return 200, {"success": True, "data": upd}
                return 404, {"success": False}
            if method == "DELETE":
                self.items = [it for it in self.items if it.get("id") != iid]
                return 200, {"success": True, "data": None}
        return 404, {"success": False}


def _boom_http(*_a, **_k):
    raise AssertionError("warm-cache lookup must not call the serve API")


def _make_backend(monkeypatch, fake, *, owns_proc=True):
    """A BitwardenBackend wired to a FakeServe, with no real daemon spawned."""
    b = ss.BitwardenBackend()
    b._bin = '/usr/bin/bw'
    if owns_proc:
        b._proc = object()                    # pretend we own a daemon (skip reuse-lock)
    monkeypatch.setattr(b, '_http', fake.http)
    monkeypatch.setattr(b, '_ensure_serve', lambda: True)
    return b


def test_bitwarden_unlock_warms_full_cache(monkeypatch):
    # unlock() warms the whole-vault cache (one full /list) while the spinner is up, so
    # lookups afterwards are in-memory hits.
    fake = FakeServe(status="locked",
                     items=[{"id": "ID1", "name": "u@h", "login": {"password": "hunter2"}}])
    b = _make_backend(monkeypatch, fake)
    assert b.unlock('master') is True
    assert fake.status == "unlocked"
    assert ("POST", "/unlock") in fake.calls
    assert fake.search_terms == [None]              # one whole-vault list (no ?search)
    assert b._cache_complete is True
    monkeypatch.setattr(b, '_http', _boom_http)
    assert b.lookup(password_spec('h', 'u')) == 'hunter2'   # warm-cache hit, no HTTP


def test_lookup_after_unlock_is_cache_hit(monkeypatch):
    # After unlock, lookups hit the warm full cache — both a hit and a definitive miss
    # make no serve call.
    fake = FakeServe(status="locked",
                     items=[{"id": "ID1", "name": "u@h", "login": {"password": "pw"}}])
    b = _make_backend(monkeypatch, fake)
    assert b.unlock('master') is True
    monkeypatch.setattr(b, '_http', _boom_http)
    assert b.lookup(password_spec('h', 'u')) == 'pw'    # hit
    assert b.lookup(password_spec('nope', 'x')) is None  # definitive miss (complete cache)


def test_unlock_full_load_failure_falls_back_to_search(monkeypatch):
    # If the whole-vault warm load fails, the cache is not 'complete' and lookups fall
    # back to a targeted search rather than silently missing.
    fake = FakeServe(status="locked",
                     items=[{"id": "ID1", "name": "u@h", "login": {"password": "pw"}}])
    b = _make_backend(monkeypatch, fake)
    real = fake.http
    fail = {"full": True}

    def flaky(method, path, *, params=None, body=None, timeout=10):
        if path == "/list/object/items" and params is None and fail["full"]:
            return 500, {"success": False}          # whole-vault load fails
        return real(method, path, params=params, body=body, timeout=timeout)

    monkeypatch.setattr(b, '_http', flaky)
    assert b.unlock('m') is True
    assert b._cache_complete is False               # warm load failed
    assert b.lookup(password_spec('h', 'u')) == 'pw'  # falls back to targeted search
    assert 'u@h' in fake.search_terms


def test_bitwarden_lookup_cache_hit_no_http(monkeypatch):
    # A per-item cache hit makes no serve call at all.
    b = ss.BitwardenBackend()
    b._bin = '/usr/bin/bw'
    b._items = {'u@h': {'name': 'u@h', 'id': 'ID1', 'login': {'password': 'cached'}}}
    monkeypatch.setattr(b, '_http', _boom_http)
    assert b.lookup(password_spec('h', 'u')) == 'cached'


def test_bitwarden_cold_lookup_uses_targeted_search(monkeypatch):
    # The askpass-subprocess case: no cache -> one targeted /list?search (not a whole-
    # vault load), served by the shared daemon, and the hit is cached.
    fake = FakeServe(status="unlocked",
                     items=[{"id": "ID1", "name": "u@h", "login": {"password": "pw"}},
                            {"id": "ID2", "name": "other", "login": {"password": "x"}}])
    b = ss.BitwardenBackend()
    b._bin = '/usr/bin/bw'
    monkeypatch.setattr(b, '_http', fake.http)
    assert b._items is None                                  # cold
    assert b.lookup(password_spec('h', 'u')) == 'pw'
    assert fake.search_terms == ['u@h']                     # targeted, not a full list
    assert (b._items or {}).get('u@h') is not None          # hit cached
    monkeypatch.setattr(b, '_http', _boom_http)
    assert b.lookup(password_spec('h', 'u')) == 'pw'        # repeat is a cache hit


def test_bitwarden_unlock_reports_progress(monkeypatch):
    # The spinner dialog's status text rides these stages.
    fake = FakeServe(status="locked")
    b = _make_backend(monkeypatch, fake)
    stages = []
    assert b.unlock('m', progress=stages.append) is True
    assert stages == ['starting', 'unlocking', 'loading']


def test_unlock_selected_forwards_progress(monkeypatch):
    fake = FakeServe(status="locked")
    b = _make_backend(monkeypatch, fake)
    mgr = ss.SecretManager()
    mgr._backends = {'bitwarden': b}
    mgr.set_selected('bitwarden')
    stages = []
    assert mgr.unlock_selected('m', progress=stages.append) is True
    assert 'unlocking' in stages


def test_bitwarden_stays_unlocked_after_unlock(monkeypatch):
    # Regression: after a successful unlock, is_unlocked() must stay True (so the user
    # is NOT re-prompted on every connection) while our daemon is alive — even if a
    # /status probe is unavailable.
    fake = FakeServe(status="locked",
                     items=[{"id": "ID1", "name": "u@h", "login": {"password": "pw"}}])
    b = _make_backend(monkeypatch, fake)        # b._proc is an alive sentinel
    assert b.unlock('m') is True
    monkeypatch.setattr(b, '_http', _boom_http)  # /status unanswerable
    assert b.is_unlocked() is True

    mgr = SecretManager()
    mgr._backends = {'bitwarden': b}
    mgr.set_selected('bitwarden')
    assert mgr.selected_needs_unlock() is False   # -> no re-prompt on the next connect


def test_bitwarden_is_unlocked_never_starts_daemon(monkeypatch):
    # is_unlocked() must not spawn a daemon; with none reachable it's just False.
    def _refused(*_a, **_k):
        raise ConnectionRefusedError()
    b = ss.BitwardenBackend()
    b._bin = '/usr/bin/bw'
    monkeypatch.setattr(b, '_http', _refused)
    assert b.is_unlocked() is False


def test_bitwarden_port_from_env(monkeypatch):
    b = ss.BitwardenBackend()
    monkeypatch.delenv('SSHPILOT_BW_SERVE_PORT', raising=False)
    assert b._port() == ss.BitwardenBackend._DEFAULT_PORT
    monkeypatch.setenv('SSHPILOT_BW_SERVE_PORT', '9999')
    assert b._port() == 9999


def test_bitwarden_store_creates_enriched_item(monkeypatch):
    fake = FakeServe(status="locked")   # empty vault
    b = _make_backend(monkeypatch, fake)
    assert b.unlock('m') is True
    assert b.store(password_spec('h', 'u'), 'secret') is True
    assert ("POST", "/object/item") in fake.calls
    created = [i for i in fake.items if i.get("name") == "u@h"]
    assert len(created) == 1
    item = created[0]
    assert item['login']['password'] == 'secret'
    assert item['login']['username'] == 'u'                        # enriched
    assert item['login']['uris'] == [{'match': None, 'uri': 'ssh://h'}]
    assert item['notes'] == 'Saved by SSH Pilot'


def test_bitwarden_store_updates_cache(monkeypatch):
    fake = FakeServe(status="locked")
    b = _make_backend(monkeypatch, fake)
    assert b.unlock('m') is True
    assert b.store(password_spec('h', 'u'), 'secret') is True
    monkeypatch.setattr(b, '_http', _boom_http)
    assert b.lookup(password_spec('h', 'u')) == 'secret'   # served from updated cache, no HTTP


def test_bitwarden_store_edits_existing(monkeypatch):
    fake = FakeServe(status="locked",
                     items=[{"id": "ID1", "name": "u@h", "login": {"password": "old"}}])
    b = _make_backend(monkeypatch, fake)
    assert b.unlock('m') is True
    assert b.store(password_spec('h', 'u'), 'new') is True
    assert ("PUT", "/object/item/ID1") in fake.calls
    assert fake.items[0]["login"]["password"] == "new"


def test_bitwarden_delete(monkeypatch):
    fake = FakeServe(status="locked",
                     items=[{"id": "ID1", "name": "u@h", "login": {"password": "pw"}}])
    b = _make_backend(monkeypatch, fake)
    assert b.unlock('m') is True
    assert b.delete(password_spec('h', 'u')) is True
    assert ("DELETE", "/object/item/ID1") in fake.calls
    assert fake.items == []
    monkeypatch.setattr(b, '_http', _boom_http)
    assert b.lookup(password_spec('h', 'u')) is None       # cache dropped, no HTTP


def test_bitwarden_lock_clears_cache_and_stops_daemon(monkeypatch):
    fake = FakeServe(status="unlocked")
    b = _make_backend(monkeypatch, fake)
    b._items = {'u@h': {'login': {'password': 'pw'}}}
    stopped = {'n': 0}
    monkeypatch.setattr(b, '_terminate_proc',
                        lambda: stopped.__setitem__('n', stopped['n'] + 1))
    monkeypatch.setenv('SSHPILOT_BW_SERVE_PORT', '8765')
    b.lock()
    assert b._items is None
    assert fake.status == "locked"                # POST /lock was issued
    assert stopped['n'] == 1                       # the daemon we own is stopped
    assert os.environ.get('SSHPILOT_BW_SERVE_PORT') is None


def test_bitwarden_needs_login(monkeypatch):
    # /status reporting 'unauthenticated' surfaces as needs_login() so the UI can tell
    # the user to run `bw login` instead of failing silently.
    fake = FakeServe(status="unauthenticated")
    b = ss.BitwardenBackend()
    b._bin = '/usr/bin/bw'
    monkeypatch.setattr(b, '_http', fake.http)
    monkeypatch.setattr(b, '_serve_reachable', lambda: True)
    monkeypatch.setattr(b, '_ensure_serve', lambda: True)
    assert b.needs_login() is True

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

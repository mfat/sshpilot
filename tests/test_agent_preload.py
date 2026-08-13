"""Tests for keyring-gated ssh-agent preloading (stop disabling the agent)."""

import subprocess



# --- ensure_key_in_agent: force / lifetime behavior -------------------------

class _FakeRun:
    """Records ssh-add invocations and returns canned results.

    ``listed`` controls whether ``ssh-add -l`` reports the key as present.
    """

    def __init__(self, key_path, listed):
        self.key_path = key_path
        self.listed = listed
        self.calls = []

    def __call__(self, argv, *args, **kwargs):
        self.calls.append(list(argv))
        if argv[:2] == ['ssh-add', '-l']:
            out = self.key_path if self.listed else ''
            return subprocess.CompletedProcess(argv, 0, stdout=out, stderr='')
        # actual `ssh-add <key>` (with optional -t <secs>)
        return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')

    @property
    def add_calls(self):
        return [c for c in self.calls if c[:2] != ['ssh-add', '-l']]

    @property
    def list_calls(self):
        return [c for c in self.calls if c[:2] == ['ssh-add', '-l']]


def _patch_common(monkeypatch, fake):
    from sshpilot import askpass_utils

    monkeypatch.setattr(askpass_utils.os.path, 'isfile', lambda _p: True)
    monkeypatch.setattr(askpass_utils, '_askpass_enabled', lambda: True)
    monkeypatch.setattr(askpass_utils, 'get_ssh_env_with_askpass', lambda _r: {})
    monkeypatch.setattr(askpass_utils.subprocess, 'run', fake)


def test_ensure_key_in_agent_presence_skip_without_force(monkeypatch):
    from sshpilot import askpass_utils

    fake = _FakeRun('/home/u/.ssh/k', listed=True)
    _patch_common(monkeypatch, fake)

    assert askpass_utils.ensure_key_in_agent('/home/u/.ssh/k') is True
    # Listed + no force → we skip the actual ssh-add.
    assert fake.add_calls == []
    assert fake.list_calls  # the presence check ran


def test_ensure_key_in_agent_force_reads_locked_listed_key(monkeypatch):
    from sshpilot import askpass_utils

    fake = _FakeRun('/home/u/.ssh/k', listed=True)
    _patch_common(monkeypatch, fake)

    assert askpass_utils.ensure_key_in_agent('/home/u/.ssh/k', force=True) is True
    # force=True → no presence check, always ssh-add (the gnome-keyring fix).
    assert fake.list_calls == []
    assert fake.add_calls == [['ssh-add', '/home/u/.ssh/k']]


def test_ensure_key_in_agent_lifetime_adds_t_flag(monkeypatch):
    from sshpilot import askpass_utils

    fake = _FakeRun('/home/u/.ssh/k', listed=False)
    _patch_common(monkeypatch, fake)

    askpass_utils.ensure_key_in_agent('/home/u/.ssh/k', force=True, lifetime=300)
    assert fake.add_calls == [['ssh-add', '-t', '300', '/home/u/.ssh/k']]


def test_prepare_key_for_connection_forces_by_default(monkeypatch):
    from sshpilot import askpass_utils

    seen = {}

    def fake_ensure(path, *, force=False, lifetime=0):
        seen['path'] = path
        seen['force'] = force
        return True

    monkeypatch.setattr(askpass_utils, 'ensure_key_in_agent', fake_ensure)
    assert askpass_utils.prepare_key_for_connection('/home/u/.ssh/k') is True
    assert seen == {'path': '/home/u/.ssh/k', 'force': True}


# --- HeadlessConnectionView._preload_keys_into_agent: keyring-gated --------

class _Cfg:
    def __init__(self, preload=True, lifetime=0):
        self._vals = {
            'ssh.agent_preload_keys': preload,
            'ssh.agent_preload_lifetime': lifetime,
        }

    def get_setting(self, key, default=None):
        return self._vals.get(key, default)


class CredentialLookup:
    """Narrow credential fake: stored passphrases + an observable preparer.

    Deliberately has no repository, group, Config, GTK, or persistence
    behavior — it only answers the preload lookup/prepare surface.
    """

    def __init__(self, *, passphrases=None, preload_result=True):
        self.passphrases = dict(passphrases or {})
        self.preload_result = preload_result
        self.prepared = []

    def get_key_passphrase(self, key_path):
        return self.passphrases.get(key_path)

    def prepare_key_for_connection(self, key_path, *, force=True, lifetime=0):
        self.prepared.append((key_path, force, lifetime))
        return self.preload_result


def _make_view(**attrs):
    """HeadlessConnectionView whose record carries the requested SSH policy."""
    from sshpilot.core.connections.models import ConnectionRecord
    from sshpilot.daemon.connection_launch_provider import HeadlessConnectionView

    record_data = {'auth_method': attrs.get('auth_method', 0)}
    identity_agent = attrs.get('identity_agent')
    if identity_agent is not None:
        record_data['identity_agent'] = identity_agent
    record = ConnectionRecord(
        id='h',
        nickname='h',
        host='h',
        hostname='h',
        username='u',
        port=22,
        protocol='ssh',
        data=record_data,
    )
    view = HeadlessConnectionView(record)
    if 'resolved_identity_files' in attrs:
        view.resolved_identity_files = list(attrs['resolved_identity_files'])
    return view


def test_preload_only_adds_stored_keys():
    view = _make_view(resolved_identity_files=['/k/stored', '/k/unstored'])
    lookup = CredentialLookup(passphrases={'/k/stored': 'secret'})

    view._preload_keys_into_agent(_Cfg(lifetime=600), credential_lookup=lookup)

    # Keyring-only: the stored key loads (force-unlock, with the configured
    # lifetime); the unstored key is never ssh-added — the user gets the
    # natural OS/agent prompt for it.
    assert lookup.prepared == [('/k/stored', True, 600)]


def test_preload_skips_all_unstored_keys():
    view = _make_view(resolved_identity_files=['/k/a', '/k/b', '/k/c'])
    lookup = CredentialLookup(passphrases={})  # none stored

    view._preload_keys_into_agent(_Cfg(), credential_lookup=lookup)

    # No stored passphrases → nothing is added to the agent.
    assert lookup.prepared == []


def test_preload_skipped_for_password_auth():
    view = _make_view(auth_method=1, resolved_identity_files=['/home/u/.ssh/k'])
    lookup = CredentialLookup(passphrases={'/home/u/.ssh/k': 'secret'})
    view._preload_keys_into_agent(_Cfg(), credential_lookup=lookup)
    assert lookup.prepared == []


def test_preload_skipped_when_identity_agent_disabled():
    view = _make_view(
        identity_agent='none', resolved_identity_files=['/home/u/.ssh/k']
    )
    lookup = CredentialLookup(passphrases={'/home/u/.ssh/k': 'secret'})
    view._preload_keys_into_agent(_Cfg(), credential_lookup=lookup)
    assert lookup.prepared == []


def test_preload_skipped_when_custom_identity_agent():
    view = _make_view(
        identity_agent='/run/user/1000/agent.sock',
        resolved_identity_files=['/home/u/.ssh/k'],
    )
    lookup = CredentialLookup(passphrases={'/home/u/.ssh/k': 'secret'})
    view._preload_keys_into_agent(_Cfg(), credential_lookup=lookup)
    assert lookup.prepared == []


def test_preload_skipped_when_setting_disabled():
    view = _make_view(resolved_identity_files=['/home/u/.ssh/k'])
    lookup = CredentialLookup(passphrases={'/home/u/.ssh/k': 'secret'})
    view._preload_keys_into_agent(_Cfg(preload=False), credential_lookup=lookup)
    assert lookup.prepared == []


def test_identity_agent_policy_derived_from_record():
    # Empty value -> defaults; "none" (case-insensitive) -> disabled;
    # any other value -> a custom IdentityAgent directive.
    plain = _make_view()
    assert plain.identity_agent_disabled is False
    assert plain.identity_agent_directive == ""

    none_view = _make_view(identity_agent="none")
    assert none_view.identity_agent_disabled is True
    assert none_view.identity_agent_directive == ""

    mixed_case = _make_view(identity_agent="NONE")
    assert mixed_case.identity_agent_disabled is True

    custom = _make_view(identity_agent="/run/user/1000/agent.sock")
    assert custom.identity_agent_disabled is False
    assert custom.identity_agent_directive == "/run/user/1000/agent.sock"


def test_preload_collects_candidates_when_cache_empty():
    # An empty resolved_identity_files cache falls back to
    # collect_identity_file_candidates() and stores the shared candidate set.
    view = _make_view()
    assert view.resolved_identity_files == []
    view.collect_identity_file_candidates = lambda: ["/home/u/.ssh/k"]
    lookup = CredentialLookup(passphrases={"/home/u/.ssh/k": "secret"})

    view._preload_keys_into_agent(_Cfg(), credential_lookup=lookup)

    assert view.resolved_identity_files == ["/home/u/.ssh/k"]
    assert lookup.prepared == [("/home/u/.ssh/k", True, 0)]


def test_preload_forwards_configured_lifetime():
    view = _make_view(resolved_identity_files=['/home/u/.ssh/k'])
    lookup = CredentialLookup(passphrases={'/home/u/.ssh/k': 'secret'})
    view._preload_keys_into_agent(_Cfg(lifetime=300), credential_lookup=lookup)
    assert lookup.prepared == [('/home/u/.ssh/k', True, 300)]


def test_askpass_autofill_stays_enabled_by_default(monkeypatch):
    # The master askpass/autofill switch must remain on by default.
    from sshpilot import askpass_utils

    monkeypatch.setattr(
        askpass_utils, '_read_app_setting', lambda key, default: default
    )
    assert askpass_utils._askpass_enabled() is True


def test_preload_swallows_errors():
    view = _make_view(resolved_identity_files=['/home/u/.ssh/k'])
    lookup = CredentialLookup(passphrases={'/home/u/.ssh/k': 'secret'})

    def boom(*_a, **_k):
        raise RuntimeError("ssh-add blew up")

    lookup.prepare_key_for_connection = boom
    # Must not raise.
    view._preload_keys_into_agent(_Cfg(), credential_lookup=lookup)

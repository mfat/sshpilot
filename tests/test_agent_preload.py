"""Tests for keyring-gated ssh-agent preloading (stop disabling the agent)."""

import subprocess

import pytest


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


# --- Connection._preload_keys_into_agent: keyring-gated, guarded ------------

class _Cfg:
    def __init__(self, preload=True, lifetime=0):
        self._vals = {
            'ssh.agent_preload_keys': preload,
            'ssh.agent_preload_lifetime': lifetime,
        }

    def get_setting(self, key, default=None):
        return self._vals.get(key, default)


def _make_connection(**attrs):
    from sshpilot.connection_manager import Connection

    conn = Connection({'host': 'h', 'hostname': 'h', 'auth_method': attrs.get('auth_method', 0)})
    conn.identity_agent_disabled = attrs.get('identity_agent_disabled', False)
    conn.identity_agent_directive = attrs.get('identity_agent_directive', '')
    conn.resolved_identity_files = attrs.get('resolved_identity_files', ['/home/u/.ssh/k'])
    return conn


def _patch_preload(monkeypatch, stored_paths):
    """Patch askpass_utils so lookup returns a passphrase only for stored_paths."""
    from sshpilot import askpass_utils

    added = []
    monkeypatch.setattr(
        askpass_utils, 'lookup_passphrase',
        lambda p: 'secret' if p in stored_paths else '',
    )

    def fake_ensure(path, *, force=False, lifetime=0):
        added.append((path, force, lifetime))
        return True

    monkeypatch.setattr(askpass_utils, 'ensure_key_in_agent', fake_ensure)
    return added


def test_preload_silent_stored_and_one_prompt_for_unstored(monkeypatch):
    conn = _make_connection(resolved_identity_files=['/k/stored', '/k/unstored'])
    added = _patch_preload(monkeypatch, stored_paths={'/k/stored'})

    conn._preload_keys_into_agent(_Cfg(lifetime=600))

    # stored loads silently; the unstored key is still added so OUR askpass
    # prompts (instead of gnome-keyring's OS prompt).
    assert added == [('/k/stored', True, 600), ('/k/unstored', True, 600)]


def test_preload_caps_interactive_prompts_to_one(monkeypatch):
    conn = _make_connection(resolved_identity_files=['/k/a', '/k/b', '/k/c'])
    added = _patch_preload(monkeypatch, stored_paths=set())  # none stored

    conn._preload_keys_into_agent(_Cfg())

    # Only the first unstored key is added → at most one interactive prompt.
    assert added == [('/k/a', True, 0)]


def test_preload_skipped_for_password_auth(monkeypatch):
    conn = _make_connection(auth_method=1)
    added = _patch_preload(monkeypatch, stored_paths={'/home/u/.ssh/k'})
    conn._preload_keys_into_agent(_Cfg())
    assert added == []


def test_preload_skipped_when_identity_agent_disabled(monkeypatch):
    conn = _make_connection(identity_agent_disabled=True)
    added = _patch_preload(monkeypatch, stored_paths={'/home/u/.ssh/k'})
    conn._preload_keys_into_agent(_Cfg())
    assert added == []


def test_preload_skipped_when_custom_identity_agent(monkeypatch):
    conn = _make_connection(identity_agent_directive='/run/user/1000/agent.sock')
    added = _patch_preload(monkeypatch, stored_paths={'/home/u/.ssh/k'})
    conn._preload_keys_into_agent(_Cfg())
    assert added == []


def test_preload_skipped_when_setting_disabled(monkeypatch):
    conn = _make_connection()
    added = _patch_preload(monkeypatch, stored_paths={'/home/u/.ssh/k'})
    conn._preload_keys_into_agent(_Cfg(preload=False))
    assert added == []


def test_preload_swallows_errors(monkeypatch):
    from sshpilot import askpass_utils

    conn = _make_connection()
    monkeypatch.setattr(askpass_utils, 'lookup_passphrase', lambda p: 'secret')

    def boom(*_a, **_k):
        raise RuntimeError("ssh-add blew up")

    monkeypatch.setattr(askpass_utils, 'ensure_key_in_agent', boom)
    # Must not raise.
    conn._preload_keys_into_agent(_Cfg())

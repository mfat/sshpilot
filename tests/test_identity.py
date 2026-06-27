"""Tests for the SSH identity provider abstraction (sshpilot/identity.py and
sshpilot/providers/)."""

import pytest

import sshpilot.askpass_utils as askpass_utils
from sshpilot.authorized_keys_parser import compute_fingerprint
from sshpilot.identity import (
    Identity,
    IdentityManager,
    IdentityProvider,
    get_identity_manager,
)
import sshpilot.providers.system_agent as system_agent
from sshpilot.providers.file_key import FileKeyProvider
from sshpilot.providers.system_agent import SystemAgentProvider


# --- base abstraction --------------------------------------------------------

def test_identity_dataclass_fields():
    ident = Identity(id="i", display_name="d", fingerprint=None, provider_name="p")
    assert (ident.id, ident.display_name, ident.fingerprint, ident.provider_name) == (
        "i", "d", None, "p",
    )


def test_identity_provider_is_abstract():
    with pytest.raises(TypeError):
        IdentityProvider()  # cannot instantiate the ABC directly


# --- SystemAgentProvider -----------------------------------------------------

def test_system_agent_apply_to_env_returns_copy(monkeypatch):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/agent.sock")
    monkeypatch.setenv("SSH_AGENT_PID", "4321")
    original = {"FOO": "bar"}
    out = SystemAgentProvider().apply_to_env(original)
    assert original == {"FOO": "bar"}             # input not mutated
    assert out is not original
    assert out["FOO"] == "bar"                    # original keys preserved
    assert out["SSH_AUTH_SOCK"] == "/run/agent.sock"
    assert out["SSH_AGENT_PID"] == "4321"


def test_system_agent_apply_to_env_without_agent(monkeypatch):
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.delenv("SSH_AGENT_PID", raising=False)
    out = SystemAgentProvider().apply_to_env({"A": "1"})
    assert out == {"A": "1"}                       # nothing injected
    assert "SSH_AUTH_SOCK" not in out


def test_system_agent_is_available(monkeypatch):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/agent.sock")
    assert SystemAgentProvider().is_available() is True
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    assert SystemAgentProvider().is_available() is False


def test_system_agent_list_identities_parses_ssh_add(monkeypatch):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/agent.sock")

    class _R:
        returncode = 0
        stdout = (
            "256 SHA256:AAAA alice@host (ED25519)\n"
            "3072 SHA256:BBBB my work key (RSA)\n"
        )

    monkeypatch.setattr(system_agent.subprocess, "run", lambda *a, **k: _R())
    ids = SystemAgentProvider().list_identities()
    assert [i.fingerprint for i in ids] == ["SHA256:AAAA", "SHA256:BBBB"]
    assert ids[0].display_name == "alice@host"
    assert ids[1].display_name == "my work key"   # multi-word comment preserved
    assert all(i.provider_name == "system-agent" for i in ids)
    assert ids[0].id == ids[0].fingerprint        # id stable == fingerprint


def test_system_agent_list_identities_empty_agent(monkeypatch):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/agent.sock")

    class _R:
        returncode = 1                            # "agent has no identities"
        stdout = "The agent has no identities.\n"

    monkeypatch.setattr(system_agent.subprocess, "run", lambda *a, **k: _R())
    assert SystemAgentProvider().list_identities() == []


def test_system_agent_list_identities_no_agent(monkeypatch):
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    # is_available() short-circuits before any subprocess call.
    assert SystemAgentProvider().list_identities() == []


# --- FileKeyProvider ---------------------------------------------------------

@pytest.fixture
def key_pair(tmp_path):
    """A fake private key file plus a sibling .pub with a known fingerprint."""
    key = tmp_path / "id_test"
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nx\n-----END-----\n")
    pub = tmp_path / "id_test.pub"
    pub.write_text("ssh-ed25519 aGVsbG8= alice@host\n")  # b64('hello')
    return key, pub


def test_file_key_availability(tmp_path, key_pair):
    key, _ = key_pair
    assert FileKeyProvider(str(key)).is_available() is True
    assert FileKeyProvider(str(tmp_path / "missing")).is_available() is False


def test_file_key_apply_to_env(key_pair):
    key, _ = key_pair
    original = {"A": "1"}
    out = FileKeyProvider(str(key)).apply_to_env(original)
    assert original == {"A": "1"}                  # not mutated
    assert out["SSH_IDENTITY_FILE"] == str(key)


def test_file_key_apply_to_env_missing_key_is_noop(tmp_path):
    out = FileKeyProvider(str(tmp_path / "missing")).apply_to_env({"A": "1"})
    assert out == {"A": "1"}
    assert "SSH_IDENTITY_FILE" not in out


def test_file_key_list_identities_fingerprint(key_pair):
    key, _ = key_pair
    ids = FileKeyProvider(str(key)).list_identities()
    assert len(ids) == 1
    assert ids[0].provider_name == "file-key"
    assert ids[0].display_name == "id_test"
    assert ids[0].fingerprint == compute_fingerprint("ssh-ed25519", "aGVsbG8=")


def test_file_key_list_identities_without_pub(tmp_path):
    key = tmp_path / "id_nopub"
    key.write_text("private\n")
    ids = FileKeyProvider(str(key)).list_identities()
    assert len(ids) == 1
    assert ids[0].fingerprint is None


def test_file_key_passphrase_delegates_to_credential_backend(monkeypatch, key_pair):
    key, _ = key_pair
    seen = {}

    def fake_lookup(path):
        seen["path"] = path
        return "s3cret"

    monkeypatch.setattr(askpass_utils, "lookup_passphrase", fake_lookup)
    provider = FileKeyProvider(str(key))
    assert provider.has_stored_passphrase() is True
    assert seen["path"] == str(key)               # delegated, with the key path


def test_file_key_unlock_uses_agent_loader(monkeypatch, key_pair):
    key, _ = key_pair
    calls = {}

    def fake_ensure(path, *, force, lifetime):
        calls["args"] = (path, force, lifetime)
        return True

    monkeypatch.setattr(askpass_utils, "ensure_key_in_agent", fake_ensure)
    assert FileKeyProvider(str(key)).unlock(lifetime=30) is True
    assert calls["args"] == (str(key), True, 30)


# --- IdentityManager ---------------------------------------------------------

class _FakeProvider(IdentityProvider):
    def __init__(self, name, available=True, identities=None, raises=False):
        self._name = name
        self._available = available
        self._identities = identities or []
        self._raises = raises

    @property
    def name(self):
        return self._name

    def is_available(self):
        return self._available

    def apply_to_env(self, env):
        return dict(env)

    def list_identities(self):
        if self._raises:
            raise RuntimeError("boom")
        return list(self._identities)


def test_manager_default_registers_system_agent():
    mgr = IdentityManager()
    assert mgr.SYSTEM_AGENT in [p.name for p in mgr.providers()]
    assert isinstance(mgr.system_agent(), SystemAgentProvider)


def test_manager_register_and_get():
    mgr = IdentityManager()
    fake = _FakeProvider("file-key")
    mgr.register(fake)
    assert mgr.get("file-key") is fake


def test_manager_list_identities_aggregates_available_only():
    mgr = IdentityManager()
    here = Identity(id="1", display_name="a", fingerprint=None, provider_name="x")
    mgr._providers = {
        "x": _FakeProvider("x", available=True, identities=[here]),
        "y": _FakeProvider("y", available=False, identities=[
            Identity(id="2", display_name="b", fingerprint=None, provider_name="y"),
        ]),
    }
    out = mgr.list_identities()
    assert out == [here]                           # unavailable provider skipped


def test_manager_list_identities_skips_failing_provider():
    mgr = IdentityManager()
    good = Identity(id="1", display_name="a", fingerprint=None, provider_name="g")
    mgr._providers = {
        "bad": _FakeProvider("bad", available=True, raises=True),
        "good": _FakeProvider("good", available=True, identities=[good]),
    }
    assert mgr.list_identities() == [good]         # failure logged, not raised


def test_get_identity_manager_singleton():
    assert get_identity_manager() is get_identity_manager()

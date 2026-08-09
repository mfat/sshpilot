"""M1: KeyManager routes every key operation through the daemon client.

No local key service, no directory scanning, no ``ssh-keygen``, and no local
fallback: discovery, public-key reads, and generation all come from the
injected daemon-backed client (via ``KeyController``).
"""

import ast
from pathlib import Path

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.keys import (
    GenerateKeyRequest,
    GenerateKeyResult,
    KeyId,
    KeyList,
    KeyStoreScope,
    KeySummary,
    PublicKeyResult,
)
from sshpilot.api.models import SessionId
from sshpilot.key_manager import KeyManager, SSHKey

_SOURCE = Path(__file__).resolve().parents[1] / "src" / "sshpilot" / "key_manager.py"


def _summary(key_id="key-1", name="id_ed25519"):
    private_path = f"/daemon/keys/{name}"
    return KeySummary(
        key_id=KeyId(key_id),
        name=name,
        private_path=private_path,
        public_path=private_path + ".pub",
        public_key_available=True,
    )


class _FakeClient:
    """Minimal daemon-client stand-in for the controller."""

    def __init__(self):
        self.list_scopes: list[KeyStoreScope] = []
        self.read_requests: list = []
        self.generate_requests: list[GenerateKeyRequest] = []
        self.key_list = KeyList(keys=(_summary(),))
        self.public_result = PublicKeyResult(
            key_id=KeyId("key-1"), text="ssh-ed25519 AAAA-public\n"
        )
        self.generate_result = GenerateKeyResult(key=_summary("key-2", "new_key"))
        self.generate_error: BaseException | None = None

    def list_keys(self, request):
        self.list_scopes.append(request.scope)
        return self.key_list

    def read_public_key(self, request):
        self.read_requests.append(request)
        return self.public_result

    def generate_key(self, request):
        self.generate_requests.append(request)
        if self.generate_error is not None:
            raise self.generate_error
        return self.generate_result


class _RecordingKeyManager(KeyManager):
    """Subclass that records signal emissions (real GObject or the stub)."""

    def __init__(self, client, scope=KeyStoreScope.DEFAULT):
        super().__init__(client, scope)
        self.emitted = []

    def emit(self, name, key):
        self.emitted.append((name, key))


def _manager():
    client = _FakeClient()
    return KeyManager(client, KeyStoreScope.DEFAULT), client


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def test_discover_keys_projects_daemon_summaries():
    manager, client = _manager()
    keys = manager.discover_keys()
    assert len(keys) == 1
    key = keys[0]
    assert isinstance(key, SSHKey)
    assert key.name == "id_ed25519"
    assert key.key_id == "key-1"
    assert key.private_path == "/daemon/keys/id_ed25519"
    assert key.public_path == "/daemon/keys/id_ed25519.pub"
    assert key.public_key_available is True
    assert client.list_scopes == [KeyStoreScope.DEFAULT]


def test_discover_keys_uses_manager_scope():
    client = _FakeClient()
    manager = KeyManager(client, KeyStoreScope.ISOLATED)
    manager.discover_keys()
    assert client.list_scopes == [KeyStoreScope.ISOLATED]


# ---------------------------------------------------------------------------
# Public-key reads
# ---------------------------------------------------------------------------
def test_read_public_key_returns_daemon_text():
    manager, _client = _manager()
    key = manager.discover_keys()[0]
    assert manager.read_public_key(key) == "ssh-ed25519 AAAA-public\n"


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------
def test_generate_key_sends_daemon_request_and_returns_dto():
    manager, client = _manager()
    key = manager.generate_key(
        key_name="my_key",
        key_type="rsa",
        key_size=3072,
        comment="work",
        encrypted=True,
        interaction_scope_id=SessionId("key-operation-generate-1"),
    )
    assert key.name == "new_key"
    assert key.key_id == "key-2"
    request = client.generate_requests[0]
    assert request.name == "my_key"
    assert request.key_type == "rsa"
    assert request.key_size == 3072
    assert request.comment == "work"
    assert request.encrypted is True
    assert request.interaction_scope_id == "key-operation-generate-1"
    assert "passphrase" not in request.__dataclass_fields__
    assert request.scope is KeyStoreScope.DEFAULT


def test_generate_key_none_comment_and_unencrypted_defaults():
    manager, client = _manager()
    manager.generate_key(key_name="k")
    request = client.generate_requests[0]
    assert request.comment == ""
    assert request.encrypted is False
    assert request.interaction_scope_id is None


def test_generate_key_rsa_defaults_to_historical_3072():
    """RSA callers that omit key_size keep the historical adapter contract."""
    manager, client = _manager()
    manager.generate_key(key_name="id_rsa", key_type="rsa")
    request = client.generate_requests[0]
    assert request.key_size == 3072
    assert request.key_type == "rsa"


def test_generate_key_emits_key_generated_signal():
    client = _FakeClient()
    manager = _RecordingKeyManager(client)
    key = manager.generate_key(key_name="k")
    assert manager.emitted == [("key-generated", key)]


# ---------------------------------------------------------------------------
# Exception mapping (historical GTK contract)
# ---------------------------------------------------------------------------
def test_duplicate_key_maps_to_file_exists_error():
    manager, client = _manager()
    client.generate_error = SshPilotError(
        ErrorCode.KEY_ALREADY_EXISTS, "a key named id_ed25519 already exists"
    )
    with pytest.raises(FileExistsError):
        manager.generate_key(key_name="id_ed25519")


def test_validation_failure_maps_to_value_error():
    manager, client = _manager()
    client.generate_error = SshPilotError(
        ErrorCode.VALIDATION_FAILED, "invalid key type"
    )
    with pytest.raises(ValueError):
        manager.generate_key(key_name="x", key_type="dsa")


def test_other_api_error_maps_to_runtime_error():
    manager, client = _manager()
    client.generate_error = SshPilotError(
        ErrorCode.KEY_GENERATION_FAILED, "ssh-keygen failed"
    )
    with pytest.raises(RuntimeError):
        manager.generate_key(key_name="x")


def test_daemon_failure_propagates_without_local_fallback():
    """A transport-level failure must surface — never a silent local key store."""
    manager, client = _manager()
    client.generate_error = RuntimeError("daemon down")
    with pytest.raises(RuntimeError):
        manager.generate_key(key_name="x")


# ---------------------------------------------------------------------------
# Passphrase / path hygiene
# ---------------------------------------------------------------------------
def test_generate_request_never_carries_passphrase():
    manager, client = _manager()
    key = manager.generate_key(key_name="k")
    assert "passphrase" not in client.generate_requests[0].__dataclass_fields__
    assert "passphrase" not in vars(manager)
    assert key.name == "new_key"


def test_manager_rejects_key_directory_path(tmp_path):
    with pytest.raises(TypeError):
        KeyManager(Path(tmp_path) / ".ssh")
    with pytest.raises(TypeError):
        KeyManager(str(tmp_path))


# ---------------------------------------------------------------------------
# Static source checks (no local key I/O)
# ---------------------------------------------------------------------------
def test_key_manager_source_has_no_local_key_io():
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.Import):
            used.update(a.asname or a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            used.update(a.asname or a.name for a in node.names)
    forbidden = {
        "KeyService",
        "KeyGenerateSpec",
        "SSHKeyInfo",
        "get_ssh_dir",
        "subprocess",
        "ssh-keygen",
        "rglob",
        "read_text",
        "read_bytes",
        "write_text",
        "write_bytes",
        "open",
    }
    hits = sorted(forbidden & used)
    assert not hits, f"key_manager.py uses forbidden key-I/O symbols: {hits}"


def test_key_manager_init_accepts_no_path_parameters():
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "__init__":
            arg_names = {arg.arg for arg in node.args.args}
            assert not (arg_names & {"ssh_dir", "path", "key_dir"}), arg_names

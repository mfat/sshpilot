"""Daemon-owned SSH-key service tests."""

import logging
import threading
from pathlib import Path

import pytest

import sshpilot.daemon.key_service as key_service_module
from sshpilot.api.errors import ErrorCode as ApiErrorCode, SshPilotError
from sshpilot.api.models.keys import (
    GenerateKeyRequest,
    KeyId,
    KeyStoreScope,
    ListKeysRequest,
    ReadPublicKeyRequest,
)
from sshpilot.core.errors import CoreError, ErrorCode as CoreErrorCode
from sshpilot.core.keys import KeyService, SSHKeyInfo
from sshpilot.daemon.key_service import DaemonKeyService

PRIVATE_HEADER = (
    b"-----BEGIN OPENSSH PRIVATE KEY-----\n"
    b"b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAFzAAAC\n"
)


def _write_key(root: Path, rel: str, with_pub: bool = True) -> Path:
    private = root / rel
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_bytes(PRIVATE_HEADER)
    if with_pub:
        private.with_suffix(private.suffix + ".pub").write_text(
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI example\n"
        )
    return private


def _service(tmp_path, *, isolated=None):
    default = tmp_path / "default"
    isolated = isolated or (tmp_path / "isolated")
    return DaemonKeyService(
        lambda scope: default if scope is KeyStoreScope.DEFAULT else isolated
    )


def _summary_of(key_list, key_id):
    for summary in key_list.keys:
        if summary.key_id == key_id:
            return summary
    return None


# ---------------------------------------------------------------------------
# Listing: roots, ids, scope separation, nested duplicates
# ---------------------------------------------------------------------------
def test_list_keys_resolves_default_root(tmp_path):
    service = _service(tmp_path)
    _write_key(tmp_path / "default", "id_ed25519")
    key_list = service.list_keys(ListKeysRequest())
    assert len(key_list.keys) == 1
    assert key_list.keys[0].name == "id_ed25519"


def test_list_keys_isolated_scope(tmp_path):
    default = tmp_path / "default"
    isolated = tmp_path / "isolated"
    service = DaemonKeyService(
        lambda scope: default if scope is KeyStoreScope.DEFAULT else isolated
    )
    _write_key(default, "default_key")
    _write_key(isolated, "isolated_key")
    default_list = service.list_keys(ListKeysRequest(scope=KeyStoreScope.DEFAULT))
    isolated_list = service.list_keys(ListKeysRequest(scope=KeyStoreScope.ISOLATED))
    assert [k.name for k in default_list.keys] == ["default_key"]
    assert [k.name for k in isolated_list.keys] == ["isolated_key"]


def test_stable_ids_across_calls(tmp_path):
    service = _service(tmp_path)
    _write_key(tmp_path / "default", "id_ed25519")
    first = service.list_keys(ListKeysRequest())
    second = service.list_keys(ListKeysRequest())
    assert first.keys[0].key_id == second.keys[0].key_id


def test_same_relative_path_in_different_scopes_gets_distinct_ids(tmp_path):
    default = tmp_path / "default"
    isolated = tmp_path / "isolated"
    service = DaemonKeyService(
        lambda scope: default if scope is KeyStoreScope.DEFAULT else isolated
    )
    _write_key(default, "id_ed25519")
    _write_key(isolated, "id_ed25519")
    default_list = service.list_keys(ListKeysRequest(scope=KeyStoreScope.DEFAULT))
    isolated_list = service.list_keys(ListKeysRequest(scope=KeyStoreScope.ISOLATED))
    assert default_list.keys[0].key_id != isolated_list.keys[0].key_id


def test_nested_duplicate_basenames_get_distinct_ids(tmp_path):
    service = _service(tmp_path)
    _write_key(tmp_path / "default", "a/id_ed25519")
    _write_key(tmp_path / "default", "b/id_ed25519")
    key_list = service.list_keys(ListKeysRequest())
    ids = {k.key_id for k in key_list.keys}
    assert len(ids) == 2


def test_ids_are_opaque_and_not_uuids(tmp_path):
    service = _service(tmp_path)
    _write_key(tmp_path / "default", "id_ed25519")
    key_list = service.list_keys(ListKeysRequest())
    key_id = key_list.keys[0].key_id
    assert key_id.startswith("key-")
    assert "-" not in key_id[4:]


def test_list_skips_paths_outside_root(tmp_path):
    service = _service(tmp_path)
    outside = tmp_path / "outside"
    _write_key(outside, "id_ed25519")
    key_list = service.list_keys(ListKeysRequest())
    assert key_list.keys == ()


def test_no_private_material_in_summaries(tmp_path):
    service = _service(tmp_path)
    _write_key(tmp_path / "default", "id_ed25519")
    key_list = service.list_keys(ListKeysRequest())
    summary = key_list.keys[0]
    fields = set(summary.__dataclass_fields__)
    assert "private_key" not in fields
    assert "public_key" not in fields
    assert "private_path" not in repr(summary)


def test_public_key_availability_flag(tmp_path):
    service = _service(tmp_path)
    _write_key(tmp_path / "default", "with_pub")
    _write_key(tmp_path / "default", "no_pub", with_pub=False)
    key_list = service.list_keys(ListKeysRequest())
    by_name = {k.name: k.public_key_available for k in key_list.keys}
    assert by_name["with_pub"] is True
    assert by_name["no_pub"] is False


# ---------------------------------------------------------------------------
# Public-key reading
# ---------------------------------------------------------------------------
def test_read_public_key_success(tmp_path):
    service = _service(tmp_path)
    _write_key(tmp_path / "default", "id_ed25519")
    key_list = service.list_keys(ListKeysRequest())
    result = service.read_public_key(
        ReadPublicKeyRequest(key_id=key_list.keys[0].key_id)
    )
    assert result.key_id == key_list.keys[0].key_id
    assert result.text == "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI example\n"


def test_read_public_key_unknown_id(tmp_path):
    service = _service(tmp_path)
    _write_key(tmp_path / "default", "id_ed25519")
    with pytest.raises(SshPilotError) as excinfo:
        service.read_public_key(ReadPublicKeyRequest(key_id=KeyId("key-nope")))
    assert excinfo.value.code is ApiErrorCode.KEY_NOT_FOUND


def test_read_public_key_missing_public_file(tmp_path):
    service = _service(tmp_path)
    private = _write_key(tmp_path / "default", "id_ed25519", with_pub=False)
    key_list = service.list_keys(ListKeysRequest())
    with pytest.raises(SshPilotError) as excinfo:
        service.read_public_key(
            ReadPublicKeyRequest(key_id=key_list.keys[0].key_id)
        )
    assert excinfo.value.code is ApiErrorCode.KEY_PUBLIC_UNAVAILABLE
    assert str(private) not in str(excinfo.value)


def test_read_public_key_rejects_symlink_escape(tmp_path):
    service = _service(tmp_path)
    _write_key(tmp_path / "default", "id_ed25519")
    key_list = service.list_keys(ListKeysRequest())
    key_id = key_list.keys[0].key_id
    public = tmp_path / "default" / "id_ed25519.pub"
    outside = tmp_path / "outside.pub"
    outside.write_text("ssh-ed25519 AAAA-outside\n")
    public.unlink()
    public.symlink_to(outside)
    with pytest.raises(SshPilotError) as excinfo:
        service.read_public_key(ReadPublicKeyRequest(key_id=key_id))
    assert excinfo.value.code is ApiErrorCode.KEY_PUBLIC_UNAVAILABLE


def test_read_public_key_rejects_oversized_file(tmp_path, monkeypatch):
    monkeypatch.setattr(key_service_module, "_MAX_PUBLIC_KEY_BYTES", 8)
    service = _service(tmp_path)
    _write_key(tmp_path / "default", "id_ed25519")
    key_list = service.list_keys(ListKeysRequest())
    with pytest.raises(SshPilotError) as excinfo:
        service.read_public_key(
            ReadPublicKeyRequest(key_id=key_list.keys[0].key_id)
        )
    assert excinfo.value.code is ApiErrorCode.KEY_PUBLIC_UNAVAILABLE


def test_read_public_key_rejects_invalid_utf8(tmp_path):
    service = _service(tmp_path)
    private = _write_key(tmp_path / "default", "id_ed25519")
    private.with_suffix(".pub").write_bytes(b"\xff\xfe\xfd")
    key_list = service.list_keys(ListKeysRequest())
    with pytest.raises(SshPilotError) as excinfo:
        service.read_public_key(
            ReadPublicKeyRequest(key_id=key_list.keys[0].key_id)
        )
    assert excinfo.value.code is ApiErrorCode.KEY_PUBLIC_UNAVAILABLE


def test_read_public_key_rejects_nul(tmp_path):
    service = _service(tmp_path)
    private = _write_key(tmp_path / "default", "id_ed25519")
    private.with_suffix(".pub").write_bytes(b"ssh-ed25519 AAAA\x00comment\n")
    key_list = service.list_keys(ListKeysRequest())
    with pytest.raises(SshPilotError) as excinfo:
        service.read_public_key(
            ReadPublicKeyRequest(key_id=key_list.keys[0].key_id)
        )
    assert excinfo.value.code is ApiErrorCode.KEY_PUBLIC_UNAVAILABLE


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------
class _FakeKeyService:
    def __init__(self, root: Path, *, generate_error=None):
        self.root = root
        self.generate_error = generate_error
        self.generated: list[object] = []
        self.last_spec = None

    def discover_keys(self):
        return []

    def generate_key(self, spec):
        self.last_spec = spec
        if self.generate_error is not None:
            raise self.generate_error
        path = self.root / spec.key_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PRIVATE_HEADER)
        path.with_suffix(path.suffix + ".pub").write_text(
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI generated\n"
        )
        self.generated.append(spec)
        return SSHKeyInfo(str(path))


def _fake_service_factory(root: Path, fake, recorder):
    def factory(resolved_root: Path):
        recorder.append(resolved_root)
        return fake

    return factory


def test_generate_key_success(tmp_path):
    default = tmp_path / "default"
    fake = _FakeKeyService(default)
    calls = []
    service = DaemonKeyService(
        lambda scope: default,
        service_factory=_fake_service_factory(default, fake, calls),
    )
    result = service.generate_key(
        GenerateKeyRequest(name="id_ed25519", passphrase="a-secret")
    )
    assert calls == [default]
    assert fake.last_spec.key_name == "id_ed25519"
    assert fake.last_spec.passphrase == "a-secret"
    assert result.key.name == "id_ed25519"
    assert result.key.public_key_available is True
    assert result.key.private_path == str(default / "id_ed25519")
    assert "a-secret" not in repr(result)


def test_generate_key_maps_exists_error_with_suggestion(tmp_path):
    default = tmp_path / "default"
    fake = _FakeKeyService(
        default,
        generate_error=CoreError(
            CoreErrorCode.KEY_EXISTS,
            "A key named 'x' already exists",
            details={"suggestion": "x_1"},
        ),
    )
    service = DaemonKeyService(
        lambda scope: default,
        service_factory=lambda root: fake,
    )
    with pytest.raises(SshPilotError) as excinfo:
        service.generate_key(GenerateKeyRequest(name="x"))
    assert excinfo.value.code is ApiErrorCode.KEY_ALREADY_EXISTS
    assert excinfo.value.details == {"suggestion": "x_1"}


def test_generate_key_maps_validation_error(tmp_path):
    default = tmp_path / "default"
    fake = _FakeKeyService(
        default,
        generate_error=CoreError(CoreErrorCode.VALIDATION_ERROR, "bad"),
    )
    service = DaemonKeyService(
        lambda scope: default,
        service_factory=lambda root: fake,
    )
    with pytest.raises(SshPilotError) as excinfo:
        service.generate_key(GenerateKeyRequest(name="x"))
    assert excinfo.value.code is ApiErrorCode.VALIDATION_FAILED


def test_generate_key_maps_generation_failure(tmp_path):
    default = tmp_path / "default"
    fake = _FakeKeyService(
        default,
        generate_error=CoreError(CoreErrorCode.KEY_INVALID, "ssh-keygen failed"),
    )
    service = DaemonKeyService(
        lambda scope: default,
        service_factory=lambda root: fake,
    )
    with pytest.raises(SshPilotError) as excinfo:
        service.generate_key(GenerateKeyRequest(name="x"))
    assert excinfo.value.code is ApiErrorCode.KEY_GENERATION_FAILED
    assert "ssh-keygen failed" not in str(excinfo.value)


def test_generate_error_messages_never_include_passphrase(tmp_path, caplog):
    default = tmp_path / "default"
    fake = _FakeKeyService(
        default,
        generate_error=CoreError(CoreErrorCode.KEY_INVALID, "boom"),
    )
    service = DaemonKeyService(
        lambda scope: default,
        service_factory=lambda root: fake,
    )
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(SshPilotError):
            service.generate_key(
                GenerateKeyRequest(name="x", passphrase="super-secret-passphrase")
            )
    assert "super-secret-passphrase" not in caplog.text


def test_generate_key_rejects_result_outside_root(tmp_path):
    default = tmp_path / "default"
    outside = tmp_path / "outside"

    class _EscapeFake(_FakeKeyService):
        def generate_key(self, spec):
            self.last_spec = spec
            path = outside / spec.key_name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(PRIVATE_HEADER)
            return SSHKeyInfo(str(path))

    fake = _EscapeFake(default)
    service = DaemonKeyService(
        lambda scope: default,
        service_factory=lambda root: fake,
    )
    with pytest.raises(SshPilotError) as excinfo:
        service.generate_key(GenerateKeyRequest(name="x"))
    assert excinfo.value.code is ApiErrorCode.KEY_GENERATION_FAILED


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
def test_concurrent_operations_serialized_by_lock(tmp_path):
    default = tmp_path / "default"
    state = {"active": 0, "max_active": 0, "lock": threading.Lock()}

    class _CountingFake(_FakeKeyService):
        def discover_keys(self):
            with state["lock"]:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            try:
                return []
            finally:
                with state["lock"]:
                    state["active"] -= 1

    fake = _CountingFake(default)
    service = DaemonKeyService(
        lambda scope: default,
        service_factory=lambda root: fake,
    )

    def worker():
        service.list_keys(ListKeysRequest())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert state["max_active"] == 1


# ---------------------------------------------------------------------------
# Resolver receives only semantic scopes
# ---------------------------------------------------------------------------
def test_resolver_receives_scope_not_path(tmp_path):
    default = tmp_path / "default"
    isolated = tmp_path / "isolated"
    seen = []

    def resolve(scope):
        seen.append(scope)
        return default if scope is KeyStoreScope.DEFAULT else isolated

    service = DaemonKeyService(resolve)
    _write_key(default, "a")
    _write_key(isolated, "b")
    service.list_keys(ListKeysRequest(scope=KeyStoreScope.DEFAULT))
    service.list_keys(ListKeysRequest(scope=KeyStoreScope.ISOLATED))
    assert seen == [KeyStoreScope.DEFAULT, KeyStoreScope.ISOLATED]
    assert all(type(scope) is KeyStoreScope for scope in seen)

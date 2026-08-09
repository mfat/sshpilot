"""Daemon-owned SSH-key service tests."""

import logging
import os
import stat
import threading
import types
from pathlib import Path

import pytest

import sshpilot.daemon.key_service as key_service_module
from sshpilot.api.errors import ErrorCode as ApiErrorCode, SshPilotError
from sshpilot.api.models import ClientId, SessionId
from sshpilot.api.models.keys import (
    GenerateKeyRequest,
    KeyId,
    KeyStoreScope,
    ListKeysRequest,
    ReadPublicKeyRequest,
    VerifyKeyPassphraseRequest,
)
from sshpilot.core.errors import CoreError, ErrorCode as CoreErrorCode
from sshpilot.core.keys import SSHKeyInfo
from sshpilot.daemon.key_service import DaemonKeyService

PRIVATE_HEADER = (
    b"-----BEGIN OPENSSH PRIVATE KEY-----\n"
    b"b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAFzAAAC\n"
)
OWNER = ClientId("client-keys-1")


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


@pytest.mark.parametrize(
    "marker",
    [
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "-----BEGIN RSA PRIVATE KEY-----",
        "-----BEGIN EC PRIVATE KEY-----",
        "-----BEGIN PRIVATE KEY-----",
    ],
)
def test_read_public_key_rejects_private_key_markers(tmp_path, marker):
    service = _service(tmp_path)
    private = _write_key(tmp_path / "default", "id_ed25519")
    private.with_suffix(".pub").write_text(
        f"{marker}\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAFzAAAC\n"
    )
    key_list = service.list_keys(ListKeysRequest())
    with pytest.raises(SshPilotError) as excinfo:
        service.read_public_key(
            ReadPublicKeyRequest(key_id=key_list.keys[0].key_id)
        )
    assert excinfo.value.code is ApiErrorCode.KEY_PUBLIC_UNAVAILABLE
    assert marker not in str(excinfo.value)


def test_read_public_key_rejects_empty_file(tmp_path):
    service = _service(tmp_path)
    private = _write_key(tmp_path / "default", "id_ed25519")
    private.with_suffix(".pub").write_bytes(b"")
    key_list = service.list_keys(ListKeysRequest())
    with pytest.raises(SshPilotError) as excinfo:
        service.read_public_key(
            ReadPublicKeyRequest(key_id=key_list.keys[0].key_id)
        )
    assert excinfo.value.code is ApiErrorCode.KEY_PUBLIC_UNAVAILABLE


def test_read_public_key_rejects_whitespace_only_file(tmp_path):
    service = _service(tmp_path)
    private = _write_key(tmp_path / "default", "id_ed25519")
    private.with_suffix(".pub").write_bytes(b"   \n\t\n")
    key_list = service.list_keys(ListKeysRequest())
    with pytest.raises(SshPilotError) as excinfo:
        service.read_public_key(
            ReadPublicKeyRequest(key_id=key_list.keys[0].key_id)
        )
    assert excinfo.value.code is ApiErrorCode.KEY_PUBLIC_UNAVAILABLE


def test_read_public_key_rejects_symlink_within_root(tmp_path):
    service = _service(tmp_path)
    _write_key(tmp_path / "default", "id_ed25519")
    key_list = service.list_keys(ListKeysRequest())
    key_id = key_list.keys[0].key_id
    public = tmp_path / "default" / "id_ed25519.pub"
    inside = tmp_path / "default" / "other.pub"
    inside.write_text("ssh-ed25519 AAAA-inside\n")
    public.unlink()
    public.symlink_to(inside)
    with pytest.raises(SshPilotError) as excinfo:
        service.read_public_key(ReadPublicKeyRequest(key_id=key_id))
    assert excinfo.value.code is ApiErrorCode.KEY_PUBLIC_UNAVAILABLE


def test_read_public_key_handles_short_partial_reads(tmp_path, monkeypatch):
    """os.read may return partial data; the loop must assemble it correctly."""
    service = _service(tmp_path)
    _write_key(tmp_path / "default", "id_ed25519")
    key_list = service.list_keys(ListKeysRequest())
    key_id = key_list.keys[0].key_id
    real_read = os.read

    def _short_read(fd, n):
        return real_read(fd, min(n, 3))

    monkeypatch.setattr(os, "read", _short_read)
    result = service.read_public_key(ReadPublicKeyRequest(key_id=key_id))
    assert result.text == "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI example\n"


def test_read_public_key_rejects_oversized_despite_misleading_metadata(
    tmp_path, monkeypatch
):
    """A file that grows past the limit after fstat must still be rejected."""
    monkeypatch.setattr(key_service_module, "_MAX_PUBLIC_KEY_BYTES", 8)
    service = _service(tmp_path)
    _write_key(tmp_path / "default", "id_ed25519")
    key_list = service.list_keys(ListKeysRequest())
    key_id = key_list.keys[0].key_id
    real_read = os.read

    def _oversized_read(fd, n):
        data = real_read(fd, n)
        if data:
            return data + b"padding-over-the-limit"
        return data

    monkeypatch.setattr(os, "read", _oversized_read)
    with pytest.raises(SshPilotError) as excinfo:
        service.read_public_key(ReadPublicKeyRequest(key_id=key_id))
    assert excinfo.value.code is ApiErrorCode.KEY_PUBLIC_UNAVAILABLE


def test_read_public_key_rejects_symlink_swapped_before_open(tmp_path, monkeypatch):
    """A target swapped to a symlink before os.open must be refused."""
    service = _service(tmp_path)
    _write_key(tmp_path / "default", "id_ed25519")
    key_list = service.list_keys(ListKeysRequest())
    key_id = key_list.keys[0].key_id
    real_open = os.open

    def _refuse_follow(public_path, flags, *a):
        # Simulate O_NOFOLLOW's ELOOP rejection for a now-symlinked target.
        try:
            mode = os.lstat(public_path).st_mode
        except OSError:
            mode = 0
        if stat.S_ISLNK(mode):
            raise OSError(40, "Too many levels of symbolic links")
        return real_open(public_path, flags, *a)

    monkeypatch.setattr(os, "open", _refuse_follow)
    public = tmp_path / "default" / "id_ed25519.pub"
    public.unlink()
    public.symlink_to(tmp_path / "elsewhere.pub")
    with pytest.raises(SshPilotError) as excinfo:
        service.read_public_key(ReadPublicKeyRequest(key_id=key_id))
    assert excinfo.value.code is ApiErrorCode.KEY_PUBLIC_UNAVAILABLE


def test_read_public_key_rejects_non_regular_descriptor(tmp_path, monkeypatch):
    """A non-regular opened descriptor (e.g. FIFO/dir) is refused via fstat."""
    service = _service(tmp_path)
    _write_key(tmp_path / "default", "id_ed25519")
    key_list = service.list_keys(ListKeysRequest())
    key_id = key_list.keys[0].key_id
    real_fstat = os.fstat

    def _fake_fstat(fd):
        st = real_fstat(fd)
        return types.SimpleNamespace(
            st_mode=stat.S_IFDIR, st_size=st.st_size
        )

    monkeypatch.setattr(os, "fstat", _fake_fstat)
    with pytest.raises(SshPilotError) as excinfo:
        service.read_public_key(ReadPublicKeyRequest(key_id=key_id))
    assert excinfo.value.code is ApiErrorCode.KEY_PUBLIC_UNAVAILABLE


def test_read_public_key_closes_descriptor_on_success_and_failure(
    tmp_path, monkeypatch
):
    """The opened descriptor is closed after success and every failure path."""
    service = _service(tmp_path)
    _write_key(tmp_path / "default", "id_ed25519")
    key_list = service.list_keys(ListKeysRequest())
    key_id = key_list.keys[0].key_id
    closed: list[int] = []
    real_close = os.close
    monkeypatch.setattr(
        os, "close", lambda fd: (closed.append(fd), real_close(fd))[1]
    )

    service.read_public_key(ReadPublicKeyRequest(key_id=key_id))
    assert closed, "descriptor was never closed after a successful read"

    # Failure path: an unreadable/empty public file must also close its fd.
    closed.clear()
    monkeypatch.setattr(key_service_module, "_MAX_PUBLIC_KEY_BYTES", 8)
    with pytest.raises(SshPilotError):
        service.read_public_key(ReadPublicKeyRequest(key_id=key_id))
    assert closed, "descriptor was never closed after a failed read"


def test_public_key_available_false_for_unsafe_files(tmp_path):
    service = _service(tmp_path)
    _write_key(tmp_path / "default", "good")
    _write_key(tmp_path / "default", "no_pub", with_pub=False)
    empty = _write_key(tmp_path / "default", "empty")
    empty.with_suffix(".pub").write_bytes(b"")
    linked = _write_key(tmp_path / "default", "linked")
    target = tmp_path / "default" / "target.pub"
    target.write_text("ssh-ed25519 AAAA-target\n")
    linked.with_suffix(".pub").unlink()
    linked.with_suffix(".pub").symlink_to(target)

    key_list = service.list_keys(ListKeysRequest())
    by_name = {k.name: k.public_key_available for k in key_list.keys}
    assert by_name["good"] is True
    assert by_name["no_pub"] is False
    assert by_name["empty"] is False
    assert by_name["linked"] is False


def test_public_key_errors_expose_no_paths(tmp_path):
    """Every failure keeps targets, roots, and temp paths out of errors/logs."""
    service = _service(tmp_path)
    private = _write_key(tmp_path / "default", "id_ed25519")
    private.with_suffix(".pub").write_bytes(b"")
    key_list = service.list_keys(ListKeysRequest())
    with pytest.raises(SshPilotError) as excinfo:
        service.read_public_key(
            ReadPublicKeyRequest(key_id=key_list.keys[0].key_id)
        )
    rendered = str(excinfo.value)
    assert str(private) not in rendered
    assert str(tmp_path) not in rendered
    assert ".pub" not in rendered


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------
class _FakeKeyService:
    def __init__(self, root: Path, *, generate_error=None):
        self.root = root
        self.generate_error = generate_error
        self.generated: list[object] = []
        self.last_spec = None
        self.prepared_launch = None

    def discover_keys(self):
        return []

    def generate_key(self, spec, prepare_launch=None):
        self.last_spec = spec
        if prepare_launch is not None:
            self.prepared_launch = prepare_launch(
                ("ssh-keygen", "-t", spec.key_type, "-f", str(self.root / spec.key_name))
            )
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

    def verify_key_passphrase(self, path, *, prepare_launch):
        self.prepared_launch = prepare_launch(
            ("ssh-keygen", "-y", "-f", str(path))
        )
        return True


class _FakeBroker:
    def __init__(self):
        self.prepared = []
        self.cancelled = []

    def prepare_operation_launch(self, argv, environment, **kwargs):
        self.prepared.append((tuple(argv), dict(environment), kwargs))
        return tuple(argv), {"SAFE_ENV": "1"}

    def cancel_session(self, scope_id):
        self.cancelled.append(scope_id)



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
        GenerateKeyRequest(name="id_ed25519"),
        owner_client_id=OWNER,
    )
    assert calls == [default]
    assert fake.last_spec.key_name == "id_ed25519"
    assert fake.last_spec.encrypted is False
    assert result.key.name == "id_ed25519"
    assert result.key.public_key_available is True
    assert result.key.private_path == str(default / "id_ed25519")


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
        service.generate_key(GenerateKeyRequest(name="x"), owner_client_id=OWNER)
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
        service.generate_key(GenerateKeyRequest(name="x"), owner_client_id=OWNER)
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
        service.generate_key(GenerateKeyRequest(name="x"), owner_client_id=OWNER)
    assert excinfo.value.code is ApiErrorCode.KEY_GENERATION_FAILED
    assert "ssh-keygen failed" not in str(excinfo.value)


def test_generate_error_messages_never_include_request_details(tmp_path, caplog):
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
                GenerateKeyRequest(name="x"),
                owner_client_id=OWNER,
            )
    assert "GenerateKeyRequest" not in caplog.text


def test_generate_key_rejects_result_outside_root(tmp_path):
    default = tmp_path / "default"
    outside = tmp_path / "outside"

    class _EscapeFake(_FakeKeyService):
        def generate_key(self, spec, prepare_launch=None):
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
        service.generate_key(GenerateKeyRequest(name="x"), owner_client_id=OWNER)
    assert excinfo.value.code is ApiErrorCode.KEY_GENERATION_FAILED


def test_encrypted_generation_uses_owned_broker_scope(tmp_path):
    default = tmp_path / "default"
    fake = _FakeKeyService(default)
    broker = _FakeBroker()
    service = DaemonKeyService(
        lambda scope: default,
        service_factory=lambda root: fake,
    )
    service.attach_interaction_broker(broker)
    scope_id = SessionId("key-operation-generate-1")

    result = service.generate_key(
        GenerateKeyRequest(
            name="protected",
            encrypted=True,
            interaction_scope_id=scope_id,
        ),
        owner_client_id=OWNER,
    )

    assert result.key.name == "protected"
    assert fake.last_spec.encrypted is True
    argv, environment, kwargs = broker.prepared[0]
    assert argv[0] == "ssh-keygen"
    assert "-N" not in argv
    assert "-P" not in argv
    assert kwargs["scope_id"] == scope_id
    assert kwargs["allow_stored_secrets"] is False
    assert kwargs["confirm_passphrase"] is True
    assert service.client_can_interact(scope_id, OWNER) is False
    assert broker.cancelled == [scope_id]
    assert all("PASSPHRASE" not in key for key in environment)


def test_verify_passphrase_uses_owned_broker_scope(tmp_path):
    key = _write_key(tmp_path / "default", "id_ed25519")
    fake = _FakeKeyService(tmp_path / "default")
    broker = _FakeBroker()
    service = DaemonKeyService(
        lambda scope: tmp_path / "default",
        service_factory=lambda root: fake,
    )
    service.attach_interaction_broker(broker)
    scope_id = SessionId("key-operation-verify-1")

    result = service.verify_key_passphrase(
        VerifyKeyPassphraseRequest(
            key_path=str(key),
            interaction_scope_id=scope_id,
        ),
        owner_client_id=OWNER,
    )

    assert result.valid is True
    argv, _environment, kwargs = broker.prepared[0]
    assert argv == ("ssh-keygen", "-y", "-f", str(key))
    assert "-N" not in argv
    assert "-P" not in argv
    assert kwargs["scope_id"] == scope_id
    assert kwargs["allow_stored_secrets"] is False
    assert kwargs["confirm_passphrase"] is False
    assert broker.cancelled == [scope_id]


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

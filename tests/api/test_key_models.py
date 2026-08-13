"""SSH-key protocol model construction and validation tests."""

import pytest

from sshpilot.api.models import SessionId
from sshpilot.api.models.keys import (
    DeleteKeyRequest,
    DeleteKeyResult,
    GenerateKeyRequest,
    GenerateKeyResult,
    KeyId,
    KeyList,
    KeyStoreScope,
    KeySummary,
    ListKeysRequest,
    PublicKeyResult,
    ReadPublicKeyRequest,
    VerifyKeyPassphraseRequest,
    VerifyKeyPassphraseResult,
)

PRIVATE = "/home/user/.ssh/id_ed25519"
PUBLIC = PRIVATE + ".pub"


def _summary(
    key_id="key-1",
    name="id_ed25519",
    private_path=PRIVATE,
    public_key_available=True,
):
    return KeySummary(
        key_id=KeyId(key_id),
        name=name,
        private_path=private_path,
        public_path=private_path + ".pub",
        public_key_available=public_key_available,
    )


# ---------------------------------------------------------------------------
# Valid construction
# ---------------------------------------------------------------------------
def test_key_summary_constructs():
    summary = _summary()
    assert summary.key_id == "key-1"
    assert summary.name == "id_ed25519"
    assert summary.private_path == PRIVATE
    assert summary.public_path == PUBLIC
    assert summary.public_key_available is True


def test_list_keys_request_defaults_to_default_scope():
    request = ListKeysRequest()
    assert request.scope is KeyStoreScope.DEFAULT


def test_list_keys_request_accepts_isolated_scope():
    request = ListKeysRequest(scope=KeyStoreScope.ISOLATED)
    assert request.scope is KeyStoreScope.ISOLATED


def test_delete_key_request_and_result_use_opaque_id_and_scope():
    request = DeleteKeyRequest(
        key_id=KeyId("key-1"),
        scope=KeyStoreScope.ISOLATED,
    )
    assert request.scope is KeyStoreScope.ISOLATED
    result = DeleteKeyResult(key_id=request.key_id)
    assert result.deleted is True


def test_delete_key_request_rejects_empty_id():
    with pytest.raises((TypeError, ValueError)):
        DeleteKeyRequest(key_id=KeyId(""))


def test_key_list_constructs_with_tuple():
    key_list = KeyList(keys=(_summary("a"), _summary("b")))
    assert len(key_list.keys) == 2
    assert key_list.keys[0].key_id == "a"
    assert key_list.keys[1].key_id == "b"


def test_read_public_key_request_constructs():
    request = ReadPublicKeyRequest(key_id=KeyId("key-1"))
    assert request.key_id == "key-1"
    assert request.scope is KeyStoreScope.DEFAULT


def test_public_key_result_allows_multiline_and_trailing_newline():
    result = PublicKeyResult(
        key_id=KeyId("key-1"),
        text="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI comment\n",
    )
    assert result.text.endswith("\n")


def test_generate_key_request_defaults():
    request = GenerateKeyRequest(name="id_ed25519")
    assert request.key_type == "ed25519"
    assert request.key_size == 0
    assert request.comment == ""
    assert request.encrypted is False
    assert request.interaction_scope_id is None
    assert request.scope is KeyStoreScope.DEFAULT


def test_encrypted_generate_request_requires_key_interaction_scope():
    scope_id = SessionId("key-operation-1")
    request = GenerateKeyRequest(
        name="id_ed25519",
        encrypted=True,
        interaction_scope_id=scope_id,
    )
    assert request.encrypted is True
    assert request.interaction_scope_id == scope_id


@pytest.mark.parametrize(
    "kwargs",
    [
        {"encrypted": True},
        {"interaction_scope_id": SessionId("key-operation-1")},
        {
            "encrypted": True,
            "interaction_scope_id": SessionId("session-1"),
        },
    ],
)
def test_generate_request_rejects_invalid_interaction_scope(kwargs):
    with pytest.raises(ValueError):
        GenerateKeyRequest(name="id_ed25519", **kwargs)


def test_verify_key_passphrase_models_are_secret_free():
    request = VerifyKeyPassphraseRequest(
        key_path=PRIVATE,
        interaction_scope_id=SessionId("key-operation-verify-1"),
    )
    assert set(request.__dataclass_fields__) == {
        "key_path",
        "interaction_scope_id",
    }
    assert VerifyKeyPassphraseResult(valid=True).valid is True


def test_generate_key_result_constructs():
    result = GenerateKeyResult(key=_summary())
    assert result.key.key_id == "key-1"


# ---------------------------------------------------------------------------
# Tuple / member strictness
# ---------------------------------------------------------------------------
def test_key_list_rejects_list_keys():
    with pytest.raises(TypeError):
        KeyList(keys=[_summary()])  # type: ignore[arg-type]


def test_key_list_rejects_wrong_member_type():
    with pytest.raises(TypeError):
        KeyList(keys=(object(),))  # type: ignore[arg-type]


def test_key_list_rejects_duplicate_key_ids():
    with pytest.raises(ValueError):
        KeyList(keys=(_summary("dup"), _summary("dup")))


# ---------------------------------------------------------------------------
# Scopes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "build",
    [
        lambda: ListKeysRequest(scope="default"),  # type: ignore[arg-type]
        lambda: ReadPublicKeyRequest(key_id=KeyId("k"), scope="isolated"),  # type: ignore[arg-type]
        lambda: GenerateKeyRequest(name="id", scope=None),  # type: ignore[arg-type]
    ],
)
def test_requests_reject_non_key_store_scope(build):
    with pytest.raises((TypeError, ValueError)):
        build()


# ---------------------------------------------------------------------------
# IDs and names
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key_id", ["", "   "])
def test_key_summary_rejects_empty_key_id(key_id):
    with pytest.raises(ValueError):
        KeySummary(
            key_id=KeyId(key_id),
            name="id_ed25519",
            private_path=PRIVATE,
            public_path=PUBLIC,
            public_key_available=True,
        )


@pytest.mark.parametrize("key_id", ["", "   "])
def test_read_public_key_request_rejects_empty_key_id(key_id):
    with pytest.raises(ValueError):
        ReadPublicKeyRequest(key_id=KeyId(key_id))


@pytest.mark.parametrize("name", ["", "   "])
def test_generate_key_request_rejects_blank_name(name):
    with pytest.raises(ValueError):
        GenerateKeyRequest(name=name)


# ---------------------------------------------------------------------------
# NUL rejection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("field_name", ["name", "private_path", "public_path"])
def test_key_summary_rejects_nul_in_text_fields(field_name):
    kwargs = {
        "key_id": KeyId("key-1"),
        "name": "id_ed25519",
        "private_path": PRIVATE,
        "public_path": PUBLIC,
        "public_key_available": True,
    }
    kwargs[field_name] = "bad\x00text"
    with pytest.raises(ValueError):
        KeySummary(**kwargs)


def test_generate_key_request_rejects_nul_in_name():
    with pytest.raises(ValueError):
        GenerateKeyRequest(name="bad\x00name")


def test_generate_key_request_rejects_nul_in_comment():
    with pytest.raises(ValueError):
        GenerateKeyRequest(name="id_ed25519", comment="bad\x00comment")


def test_public_key_result_rejects_nul():
    with pytest.raises(ValueError):
        PublicKeyResult(key_id=KeyId("key-1"), text="ssh-ed25519 \x00 AAAA")


@pytest.mark.parametrize("text", ["", "   ", "\n", "\t \n"])
def test_public_key_result_rejects_blank_text(text):
    with pytest.raises(ValueError):
        PublicKeyResult(key_id=KeyId("key-1"), text=text)


@pytest.mark.parametrize(
    "marker",
    [
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "-----BEGIN RSA PRIVATE KEY-----",
        "-----BEGIN EC PRIVATE KEY-----",
        "-----BEGIN PRIVATE KEY-----",
    ],
)
def test_public_key_result_rejects_private_key_markers(marker):
    text = f"{marker}\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAFzAAAC\n"
    with pytest.raises(ValueError):
        PublicKeyResult(key_id=KeyId("key-1"), text=text)


# ---------------------------------------------------------------------------
# Generate name rules
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    ["a/b", "a\\b", ".", "..", ".hidden"],
)
def test_generate_key_request_rejects_path_separators_and_hidden_names(name):
    with pytest.raises(ValueError):
        GenerateKeyRequest(name=name)


def test_generate_key_request_rejects_non_string_name():
    with pytest.raises(TypeError):
        GenerateKeyRequest(name=123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# KeySummary path / name consistency
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "kwargs",
    [
        {"private_path": "id_ed25519", "public_path": "id_ed25519.pub"},
        {"private_path": "/home/user/.ssh/id_ed25519", "public_path": "relative.pub"},
    ],
)
def test_key_summary_rejects_relative_paths(kwargs):
    with pytest.raises(ValueError):
        KeySummary(
            key_id=KeyId("key-1"),
            name="id_ed25519",
            public_key_available=True,
            **kwargs,
        )


def test_key_summary_rejects_inconsistent_name():
    with pytest.raises(ValueError):
        KeySummary(
            key_id=KeyId("key-1"),
            name="other_name",
            private_path=PRIVATE,
            public_path=PUBLIC,
            public_key_available=True,
        )


def test_key_summary_rejects_inconsistent_public_path():
    with pytest.raises(ValueError):
        KeySummary(
            key_id=KeyId("key-1"),
            name="id_ed25519",
            private_path=PRIVATE,
            public_path="/home/user/.ssh/other.pub",
            public_key_available=True,
        )


# ---------------------------------------------------------------------------
# key_type / key_size rules
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key_type", ["dsa", "ecdsa", "", "ED25519"])
def test_generate_key_request_rejects_invalid_key_types(key_type):
    with pytest.raises(ValueError):
        GenerateKeyRequest(name="id", key_type=key_type)


def test_generate_key_request_rejects_non_string_key_type():
    with pytest.raises(TypeError):
        GenerateKeyRequest(name="id", key_type=123)  # type: ignore[arg-type]


def test_ed25519_rejects_nonzero_key_size():
    with pytest.raises(ValueError):
        GenerateKeyRequest(name="id", key_type="ed25519", key_size=3072)


def test_ed25519_accepts_zero_key_size():
    request = GenerateKeyRequest(name="id", key_type="ed25519", key_size=0)
    assert request.key_size == 0


def test_rsa_rejects_small_key_size():
    with pytest.raises(ValueError):
        GenerateKeyRequest(name="id", key_type="rsa", key_size=512)


def test_rsa_accepts_minimum_key_size():
    request = GenerateKeyRequest(name="id", key_type="rsa", key_size=1024)
    assert request.key_size == 1024


def test_key_size_rejects_bool():
    with pytest.raises(TypeError):
        GenerateKeyRequest(name="id", key_type="ed25519", key_size=True)  # type: ignore[arg-type]


def test_key_size_rejects_non_integer():
    with pytest.raises(TypeError):
        GenerateKeyRequest(name="id", key_type="rsa", key_size="3072")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Exact boolean validation
# ---------------------------------------------------------------------------
def test_key_summary_rejects_non_boolean_availability():
    with pytest.raises(TypeError):
        KeySummary(
            key_id=KeyId("key-1"),
            name="id_ed25519",
            private_path=PRIVATE,
            public_path=PUBLIC,
            public_key_available=1,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# repr safety
# ---------------------------------------------------------------------------
def test_generate_request_has_no_passphrase_field():
    request = GenerateKeyRequest(name="id_ed25519")
    assert "passphrase" not in request.__dataclass_fields__


def test_repr_does_not_expose_paths():
    summary = _summary()
    assert PRIVATE not in repr(summary)
    assert PUBLIC not in repr(summary)


def test_repr_does_not_expose_public_key_text():
    result = PublicKeyResult(
        key_id=KeyId("key-1"),
        text="ssh-ed25519 AAAASECRET comment",
    )
    assert "AAAASECRET" not in repr(result)


# ---------------------------------------------------------------------------
# No key contents on KeySummary
# ---------------------------------------------------------------------------
def test_key_summary_has_no_key_content_fields():
    summary = _summary()
    fields = {
        field_name
        for field_name in summary.__dataclass_fields__
    }
    assert "private_key" not in fields
    assert "public_key" not in fields
    assert "private_key_text" not in fields
    assert "public_key_text" not in fields

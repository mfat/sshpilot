"""SSH-key wire codec tests."""

import pytest

from sshpilot.api.models import SessionId
from sshpilot.api.models.connections import StoreKeyPassphraseRequest
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
from sshpilot.api.transport.codec import (
    delete_key_request_from_wire,
    delete_key_request_to_wire,
    delete_key_result_from_wire,
    delete_key_result_to_wire,
    generate_key_request_from_wire,
    generate_key_request_to_wire,
    generate_key_result_from_wire,
    generate_key_result_to_wire,
    key_list_from_wire,
    key_list_to_wire,
    key_summary_from_wire,
    key_summary_to_wire,
    list_keys_request_from_wire,
    list_keys_request_to_wire,
    public_key_result_from_wire,
    public_key_result_to_wire,
    read_public_key_request_from_wire,
    read_public_key_request_to_wire,
    store_key_passphrase_request_from_wire,
    store_key_passphrase_request_to_wire,
    verify_key_passphrase_request_from_wire,
    verify_key_passphrase_request_to_wire,
    verify_key_passphrase_result_from_wire,
    verify_key_passphrase_result_to_wire,
)

PRIVATE = "/home/user/.ssh/id_ed25519"
PUBLIC = PRIVATE + ".pub"


def _summary(key_id="key-1", name="id_ed25519", available=True):
    return KeySummary(
        key_id=KeyId(key_id),
        name=name,
        private_path=PRIVATE,
        public_path=PUBLIC,
        public_key_available=available,
    )


def _summary_wire(key_id="key-1", name="id_ed25519", available=True):
    return {
        "key_id": key_id,
        "name": name,
        "private_path": PRIVATE,
        "public_path": PUBLIC,
        "public_key_available": available,
    }


# ---------------------------------------------------------------------------
# KeySummary
# ---------------------------------------------------------------------------
def test_key_summary_round_trip():
    summary = _summary()
    assert key_summary_from_wire(key_summary_to_wire(summary)) == summary


def test_delete_key_round_trip_has_no_path_field():
    request = DeleteKeyRequest(key_id=KeyId("key-1"), scope=KeyStoreScope.DEFAULT)
    wire = delete_key_request_to_wire(request)
    assert wire == {"key_id": "key-1", "scope": "default"}
    assert delete_key_request_from_wire(wire) == request
    result = DeleteKeyResult(key_id=KeyId("key-1"))
    assert delete_key_result_from_wire(delete_key_result_to_wire(result)) == result


def test_delete_key_codec_rejects_path_and_unknown_fields():
    with pytest.raises(ValueError):
        delete_key_request_from_wire(
            {"key_id": "key-1", "scope": "default", "private_path": "/tmp/x"}
        )


def test_key_summary_to_wire_rejects_wrong_type():
    with pytest.raises(TypeError):
        key_summary_to_wire(object())  # type: ignore[arg-type]


def test_key_summary_from_wire_rejects_missing_fields():
    with pytest.raises(ValueError):
        key_summary_from_wire({"key_id": "key-1", "name": "id_ed25519"})


def test_key_summary_from_wire_rejects_unknown_fields():
    with pytest.raises(ValueError):
        key_summary_from_wire({**_summary_wire(), "extra": 1})


def test_key_summary_from_wire_rejects_non_boolean_availability():
    with pytest.raises(ValueError):
        key_summary_from_wire(_summary_wire(available=1))


def test_key_summary_from_wire_rejects_malformed_paths():
    # Inconsistent name/path must surface as a validation error.
    with pytest.raises(ValueError):
        key_summary_from_wire({**_summary_wire(), "name": "other_name"})


# ---------------------------------------------------------------------------
# ListKeysRequest
# ---------------------------------------------------------------------------
def test_list_keys_request_round_trip_default_and_isolated():
    for scope in (KeyStoreScope.DEFAULT, KeyStoreScope.ISOLATED):
        request = ListKeysRequest(scope=scope)
        assert (
            list_keys_request_from_wire(list_keys_request_to_wire(request)) == request
        )


def test_list_keys_request_to_wire_rejects_wrong_type():
    with pytest.raises(TypeError):
        list_keys_request_to_wire(object())  # type: ignore[arg-type]


def test_list_keys_request_from_wire_rejects_missing_scope():
    with pytest.raises(ValueError):
        list_keys_request_from_wire({})


@pytest.mark.parametrize("scope", ["other", "", 1, None])
def test_list_keys_request_from_wire_rejects_malformed_scope(scope):
    with pytest.raises(ValueError):
        list_keys_request_from_wire({"scope": scope})


def test_list_keys_request_from_wire_rejects_unknown_fields():
    with pytest.raises(ValueError):
        list_keys_request_from_wire({"scope": "default", "extra": 1})


# ---------------------------------------------------------------------------
# KeyList
# ---------------------------------------------------------------------------
def test_key_list_round_trip():
    key_list = KeyList(keys=(_summary("a"), _summary("b")))
    assert key_list_from_wire(key_list_to_wire(key_list)) == key_list


def test_key_list_to_wire_rejects_wrong_type():
    with pytest.raises(TypeError):
        key_list_to_wire(object())  # type: ignore[arg-type]


def test_key_list_from_wire_encodes_tuple_as_array():
    wire = key_list_to_wire(KeyList(keys=(_summary(),)))
    assert type(wire["keys"]) is list


def test_key_list_from_wire_decodes_array_into_tuple():
    result = key_list_from_wire({"keys": [_summary_wire("a"), _summary_wire("b")]})
    assert type(result.keys) is tuple
    assert [k.key_id for k in result.keys] == ["a", "b"]


def test_key_list_from_wire_rejects_non_array_keys():
    with pytest.raises(ValueError):
        key_list_from_wire({"keys": {"key_id": "k"}})


def test_key_list_from_wire_rejects_malformed_nested_summary():
    with pytest.raises(ValueError):
        key_list_from_wire({"keys": [_summary_wire(), {"key_id": "k"}]})


# ---------------------------------------------------------------------------
# ReadPublicKeyRequest
# ---------------------------------------------------------------------------
def test_read_public_key_request_round_trip():
    request = ReadPublicKeyRequest(key_id=KeyId("key-1"))
    assert (
        read_public_key_request_from_wire(
            read_public_key_request_to_wire(request)
        )
        == request
    )


def test_read_public_key_request_to_wire_rejects_wrong_type():
    with pytest.raises(TypeError):
        read_public_key_request_to_wire(object())  # type: ignore[arg-type]


def test_read_public_key_request_from_wire_rejects_missing_fields():
    with pytest.raises(ValueError):
        read_public_key_request_from_wire({"key_id": "key-1"})


def test_read_public_key_request_from_wire_rejects_malformed_scope():
    with pytest.raises(ValueError):
        read_public_key_request_from_wire({"key_id": "key-1", "scope": "bad"})


# ---------------------------------------------------------------------------
# PublicKeyResult
# ---------------------------------------------------------------------------
def test_public_key_result_round_trip_preserves_text():
    result = PublicKeyResult(
        key_id=KeyId("key-1"),
        text="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI comment\n",
    )
    assert public_key_result_from_wire(public_key_result_to_wire(result)) == result


def test_public_key_result_to_wire_rejects_wrong_type():
    with pytest.raises(TypeError):
        public_key_result_to_wire(object())  # type: ignore[arg-type]


def test_public_key_result_from_wire_rejects_empty_text():
    with pytest.raises(ValueError):
        public_key_result_from_wire({"key_id": "key-1", "text": ""})


def test_public_key_result_from_wire_rejects_non_string_text():
    with pytest.raises(ValueError):
        public_key_result_from_wire({"key_id": "key-1", "text": 123})


# ---------------------------------------------------------------------------
# GenerateKeyRequest
# ---------------------------------------------------------------------------
def test_generate_key_request_round_trip():
    request = GenerateKeyRequest(
        name="id_rsa",
        key_type="rsa",
        key_size=3072,
        comment="my comment",
        encrypted=True,
        interaction_scope_id=SessionId("key-operation-generate-1"),
        scope=KeyStoreScope.ISOLATED,
    )
    assert (
        generate_key_request_from_wire(generate_key_request_to_wire(request))
        == request
    )


def test_generate_key_request_to_wire_rejects_wrong_type():
    with pytest.raises(TypeError):
        generate_key_request_to_wire(object())  # type: ignore[arg-type]


def test_generate_key_request_preserves_empty_comment_and_unencrypted_mode():
    request = GenerateKeyRequest(name="id_ed25519")
    wire = generate_key_request_to_wire(request)
    assert wire["comment"] == ""
    assert wire["encrypted"] is False
    assert "interaction_scope_id" not in wire
    assert "passphrase" not in wire
    decoded = generate_key_request_from_wire(wire)
    assert decoded.comment == ""
    assert decoded.encrypted is False


def test_generate_key_request_from_wire_does_not_normalize():
    wire = {
        "name": "  id_ed25519  ",
        "key_type": "ed25519",
        "key_size": 0,
        "comment": "  kept  ",
        "encrypted": False,
        "scope": "default",
    }
    decoded = generate_key_request_from_wire(wire)
    assert decoded.name == "  id_ed25519  "
    assert decoded.comment == "  kept  "


def test_generate_key_request_from_wire_rejects_non_integer_size():
    with pytest.raises(ValueError):
        generate_key_request_from_wire(
            {
                "name": "id",
                "key_type": "rsa",
                "key_size": "3072",
                "comment": "",
                "encrypted": False,
                "scope": "default",
            }
        )


def test_generate_key_request_from_wire_rejects_malformed_scope():
    with pytest.raises(ValueError):
        generate_key_request_from_wire(
            {
                "name": "id",
                "key_type": "ed25519",
                "key_size": 0,
                "comment": "",
                "encrypted": False,
                "scope": "weird",
            }
        )


def test_generate_key_request_from_wire_rejects_unknown_fields():
    with pytest.raises(ValueError):
        generate_key_request_from_wire(
            {
                "name": "id",
                "key_type": "ed25519",
                "key_size": 0,
                "comment": "",
                "encrypted": False,
                "scope": "default",
                "extra": 1,
            }
        )


def test_generate_key_request_rejects_legacy_passphrase_wire_field():
    sentinel = "KEY_PASSPHRASE_SENTINEL_8F1C29"
    with pytest.raises(ValueError) as excinfo:
        generate_key_request_from_wire(
            {
                "name": "bad/name",
                "key_type": "ed25519",
                "key_size": 0,
                "comment": "",
                "encrypted": False,
                "passphrase": sentinel,
                "scope": "default",
            }
        )
    assert sentinel not in str(excinfo.value)


def test_verify_key_passphrase_request_and_result_round_trip_without_secret():
    sentinel = "KEY_PASSPHRASE_SENTINEL_8F1C29"
    request = VerifyKeyPassphraseRequest(
        key_path=PRIVATE,
        interaction_scope_id=SessionId("key-operation-verify-1"),
    )
    wire = verify_key_passphrase_request_to_wire(request)
    assert verify_key_passphrase_request_from_wire(wire) == request
    assert "passphrase" not in wire
    assert sentinel not in repr(wire)
    result = VerifyKeyPassphraseResult(valid=True)
    assert verify_key_passphrase_result_from_wire(
        verify_key_passphrase_result_to_wire(result)
    ) == result


def test_store_key_passphrase_request_round_trip_has_no_secret_field():
    sentinel = "KEY_PASSPHRASE_SENTINEL_8F1C29"
    request = StoreKeyPassphraseRequest(
        key_path=PRIVATE,
        interaction_scope_id=SessionId("key-operation-store-1"),
    )
    wire = store_key_passphrase_request_to_wire(request)
    assert store_key_passphrase_request_from_wire(wire) == request
    assert "passphrase" not in wire
    assert sentinel not in repr(wire)

    with pytest.raises(ValueError) as excinfo:
        store_key_passphrase_request_from_wire(
            {
                **wire,
                "passphrase": sentinel,
            }
        )
    assert sentinel not in str(excinfo.value)


# ---------------------------------------------------------------------------
# GenerateKeyResult
# ---------------------------------------------------------------------------
def test_generate_key_result_round_trip():
    result = GenerateKeyResult(key=_summary())
    assert generate_key_result_from_wire(generate_key_result_to_wire(result)) == result


def test_generate_key_result_to_wire_rejects_wrong_type():
    with pytest.raises(TypeError):
        generate_key_result_to_wire(object())  # type: ignore[arg-type]


def test_generate_key_result_from_wire_rejects_malformed_nested_summary():
    with pytest.raises(ValueError):
        generate_key_result_from_wire({"key": {"key_id": "k"}})

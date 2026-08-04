"""SSH-key wire codec tests."""

import pytest

from sshpilot.api.models.keys import (
    GenerateKeyRequest,
    GenerateKeyResult,
    KeyId,
    KeyList,
    KeyStoreScope,
    KeySummary,
    ListKeysRequest,
    PublicKeyResult,
    ReadPublicKeyRequest,
)
from sshpilot.api.transport.codec import (
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
        passphrase="a-secret",
        scope=KeyStoreScope.ISOLATED,
    )
    assert (
        generate_key_request_from_wire(generate_key_request_to_wire(request))
        == request
    )


def test_generate_key_request_to_wire_rejects_wrong_type():
    with pytest.raises(TypeError):
        generate_key_request_to_wire(object())  # type: ignore[arg-type]


def test_generate_key_request_preserves_empty_comment_and_passphrase():
    request = GenerateKeyRequest(name="id_ed25519")
    assert generate_key_request_to_wire(request)["comment"] == ""
    assert generate_key_request_to_wire(request)["passphrase"] == ""
    decoded = generate_key_request_from_wire(generate_key_request_to_wire(request))
    assert decoded.comment == ""
    assert decoded.passphrase == ""


def test_generate_key_request_from_wire_does_not_normalize():
    wire = {
        "name": "  id_ed25519  ",
        "key_type": "ed25519",
        "key_size": 0,
        "comment": "  kept  ",
        "passphrase": "",
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
                "passphrase": "",
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
                "passphrase": "",
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
                "passphrase": "",
                "scope": "default",
                "extra": 1,
            }
        )


def test_generate_key_request_exceptions_never_include_passphrase():
    with pytest.raises(ValueError) as excinfo:
        generate_key_request_from_wire(
            {
                "name": "bad/name",
                "key_type": "ed25519",
                "key_size": 0,
                "comment": "",
                "passphrase": "super-secret-passphrase",
                "scope": "default",
            }
        )
    assert "super-secret-passphrase" not in str(excinfo.value)


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

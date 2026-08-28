from datetime import datetime, timedelta, timezone

import pytest

from sshpilot.api.interaction_identity import new_interaction_id
from sshpilot.api.models import (
    ClientId,
    ConnectionId,
    HostKeyPrompt,
    HostKeyStatus,
    InteractionId,
    InteractionState,
    InteractionSummary,
    InteractionType,
    PassphrasePrompt,
    PasswordPrompt,
    SecretPromptKind,
    SessionId,
)
from sshpilot.api.transport.codec import (
    interaction_summary_from_wire,
    interaction_summary_to_wire,
)
from sshpilot.api.transport.framing import MultiplexedFrameDecoder, encode_binary_frame
from sshpilot.api.transport.secret_frames import (
    MAX_SECRET_PAYLOAD_SIZE,
    SecretFrame,
    SecretFrameKind,
    decode_secret_payload,
    encode_secret_payload,
)
from sshpilot.api.transport.terminal_frames import (
    TerminalFrame,
    TerminalFrameFlags,
    TerminalFrameKind,
)


def _interaction_id() -> InteractionId:
    return new_interaction_id()


def test_interaction_id_is_sequential() -> None:
    identifier = _interaction_id()
    assert identifier.startswith("interaction-")


def test_typed_interaction_codec_round_trip_and_payload_match() -> None:
    now = datetime.now(timezone.utc)
    summary = InteractionSummary(
        id=_interaction_id(),
        session_id=SessionId("session-1"),
        connection_id=ConnectionId("production"),
        type=InteractionType.PASSWORD,
        state=InteractionState.CLAIMED,
        created_at=now,
        expires_at=now + timedelta(seconds=120),
        attempt=1,
        prompt=PasswordPrompt(
            username="alice",
            hostname="example.test",
            port=22,
            attempt=1,
            can_remember=True,
            stored_secret_available=False,
        ),
        responder_client_id=ClientId("client-a"),
    )
    password_wire = interaction_summary_to_wire(summary)
    assert password_wire["metadata"]["secret_prompt_kind"] is None
    assert password_wire["metadata"]["secret_prompt_parameters"] == {}
    assert interaction_summary_from_wire(password_wire) == summary

    wire = interaction_summary_to_wire(summary)
    wire["metadata"] = {
        "hostname": "example.test",
        "port": 22,
        "key_type": "ssh-ed25519",
        "fingerprint": "SHA256:abc",
        "status": HostKeyStatus.UNKNOWN.value,
    }
    with pytest.raises(ValueError):
        interaction_summary_from_wire(wire)


def test_passphrase_prompt_confirmation_requirement_round_trips() -> None:
    now = datetime.now(timezone.utc)
    summary = InteractionSummary(
        id=_interaction_id(),
        session_id=SessionId("key-operation-1"),
        connection_id=ConnectionId("key-key-operation-1"),
        type=InteractionType.PRIVATE_KEY_PASSPHRASE,
        state=InteractionState.PENDING,
        created_at=now,
        expires_at=now + timedelta(seconds=120),
        attempt=1,
        prompt=PassphrasePrompt(
            key_display_name="new_key",
            key_fingerprint=None,
            attempt=1,
            can_remember=False,
            stored_secret_available=False,
            confirmation_required=True,
        ),
    )

    wire = interaction_summary_to_wire(summary)

    assert wire["metadata"]["confirmation_required"] is True
    assert interaction_summary_from_wire(wire) == summary

    wire["metadata"].pop("confirmation_required")
    decoded = interaction_summary_from_wire(wire)
    assert decoded.prompt.confirmation_required is False


def test_structured_secret_prompt_round_trips_without_rendered_text() -> None:
    now = datetime.now(timezone.utc)
    summary = InteractionSummary(
        id=_interaction_id(),
        session_id=SessionId("secret-session-1"),
        connection_id=ConnectionId("secret-secret-session-1"),
        type=InteractionType.PASSWORD,
        state=InteractionState.PENDING,
        created_at=now,
        expires_at=now + timedelta(seconds=120),
        attempt=1,
        prompt=PasswordPrompt(
            username="",
            hostname="",
            port=22,
            attempt=1,
            can_remember=False,
            stored_secret_available=False,
            secret_prompt_kind=SecretPromptKind.BITWARDEN_SIGN_IN,
            secret_prompt_parameters={"email": "alice@example.com"},
        ),
    )

    wire = interaction_summary_to_wire(summary)

    assert wire["metadata"]["username"] == ""
    assert wire["metadata"]["hostname"] == ""
    assert wire["metadata"]["secret_prompt_kind"] == "bitwarden_sign_in"
    assert wire["metadata"]["secret_prompt_parameters"] == {
        "email": "alice@example.com"
    }
    assert interaction_summary_from_wire(wire) == summary


def test_structured_secret_prompt_rejects_unknown_kind_and_invalid_parameters() -> None:
    now = datetime.now(timezone.utc)
    summary = InteractionSummary(
        id=_interaction_id(),
        session_id=SessionId("secret-session-1"),
        connection_id=ConnectionId("secret-secret-session-1"),
        type=InteractionType.PASSWORD,
        state=InteractionState.PENDING,
        created_at=now,
        expires_at=now + timedelta(seconds=120),
        attempt=1,
        prompt=PasswordPrompt(
            username="",
            hostname="",
            port=22,
            attempt=1,
            can_remember=False,
            stored_secret_available=False,
            secret_prompt_kind=SecretPromptKind.BITWARDEN_SIGN_IN,
            secret_prompt_parameters={"email": "alice@example.com"},
        ),
    )
    wire = interaction_summary_to_wire(summary)
    wire["metadata"]["secret_prompt_kind"] = "unknown_prompt"
    with pytest.raises(ValueError, match="secret prompt kind is invalid"):
        interaction_summary_from_wire(wire)

    wire = interaction_summary_to_wire(summary)
    wire["metadata"]["secret_prompt_parameters"] = {"name": "vault"}
    with pytest.raises(
        ValueError, match="secret prompt parameters do not match the prompt kind"
    ):
        interaction_summary_from_wire(wire)


def test_passphrase_prompt_confirmation_requirement_is_strictly_boolean() -> None:
    with pytest.raises(TypeError):
        PassphrasePrompt(
            key_display_name="new_key",
            key_fingerprint=None,
            attempt=1,
            can_remember=False,
            stored_secret_available=False,
            confirmation_required=1,
        )


def test_host_key_prompt_has_no_raw_prompt_field() -> None:
    prompt = HostKeyPrompt(
        hostname="example.test",
        port=22,
        key_type="ssh-ed25519",
        fingerprint="SHA256:abc",
        status=HostKeyStatus.UNKNOWN,
    )
    assert "raw" not in repr(prompt).lower()


def test_secret_binary_frame_round_trip_clear_and_multiplexing() -> None:
    secret = bytearray(b"one-use-value")
    frame = SecretFrame(
        kind=SecretFrameKind.RESPONSE,
        interaction_id=_interaction_id(),
        nonce=bytes.fromhex("00112233445566778899aabbccddeeff"),
        secret=secret,
    )
    payload = encode_secret_payload(frame)
    decoded = decode_secret_payload(payload)
    assert bytes(decoded.secret) == bytes(secret)
    assert bytes(secret) not in repr(decoded).encode()

    decoder = MultiplexedFrameDecoder()
    multiplexed = decoder.feed(encode_binary_frame(payload))
    assert len(multiplexed) == 1
    assert isinstance(multiplexed[0], SecretFrame)
    assert bytes(multiplexed[0].secret) == bytes(secret)
    decoded.clear()
    assert decoded.secret == bytearray()


def test_secret_frame_allows_empty_but_rejects_nul_and_oversized_values() -> None:
    common = {
        "kind": SecretFrameKind.RESPONSE,
        "interaction_id": _interaction_id(),
        "nonce": bytes.fromhex("00112233445566778899aabbccddeeff"),
    }
    empty = SecretFrame(secret=bytearray(), **common)
    assert decode_secret_payload(encode_secret_payload(empty)).secret == bytearray()
    with pytest.raises(ValueError):
        SecretFrame(secret=bytearray(b"contains\0nul"), **common)
    with pytest.raises(ValueError):
        SecretFrame(secret=bytearray(MAX_SECRET_PAYLOAD_SIZE + 1), **common)


def test_terminal_frame_flags_are_strict_per_kind() -> None:
    session_id = SessionId(
        "session-1"
    )
    with pytest.raises(ValueError):
        TerminalFrame(
            kind=TerminalFrameKind.INPUT,
            session_id=session_id,
            sequence=0,
            attachment_id="attachment-3",
            flags=TerminalFrameFlags.REPLAY,
        )
    with pytest.raises(ValueError):
        TerminalFrame(
            kind=TerminalFrameKind.CONTINUITY_LOST,
            session_id=session_id,
            sequence=0,
            flags=TerminalFrameFlags.EOF,
        )

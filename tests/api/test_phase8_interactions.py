from datetime import datetime, timedelta, timezone

import pytest

from sshpilot.api.interaction_identity import (
    interaction_id_from_uuid,
    interaction_uuid_from_id,
    new_interaction_id,
)
from sshpilot.api.models import (
    ClientId,
    ConnectionId,
    HostKeyPrompt,
    HostKeyStatus,
    InteractionId,
    InteractionState,
    InteractionSummary,
    InteractionType,
    PasswordPrompt,
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


def test_interaction_id_is_strict_canonical_uuid() -> None:
    identifier = _interaction_id()
    parsed = interaction_uuid_from_id(identifier)
    assert interaction_id_from_uuid(parsed) == identifier
    for malformed in (
        "interaction:00000000-0000-0000-0000-000000000000",
        "interaction:550E8400-E29B-41D4-A716-446655440000",
        "wrong:550e8400-e29b-41d4-a716-446655440000",
        "interaction:not-a-uuid",
    ):
        with pytest.raises(ValueError):
            interaction_uuid_from_id(InteractionId(malformed))


def test_typed_interaction_codec_round_trip_and_payload_match() -> None:
    now = datetime.now(timezone.utc)
    summary = InteractionSummary(
        id=_interaction_id(),
        session_id=SessionId("session:550e8400-e29b-41d4-a716-446655440000"),
        connection_id=ConnectionId(
            "connection:550e8400-e29b-41d4-a716-446655440001"
        ),
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
    assert interaction_summary_from_wire(interaction_summary_to_wire(summary)) == summary

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


def test_secret_frame_rejects_empty_nul_and_oversized_values() -> None:
    common = {
        "kind": SecretFrameKind.RESPONSE,
        "interaction_id": _interaction_id(),
        "nonce": bytes.fromhex("00112233445566778899aabbccddeeff"),
    }
    with pytest.raises(ValueError):
        SecretFrame(secret=bytearray(), **common)
    with pytest.raises(ValueError):
        SecretFrame(secret=bytearray(b"contains\0nul"), **common)
    with pytest.raises(ValueError):
        SecretFrame(secret=bytearray(MAX_SECRET_PAYLOAD_SIZE + 1), **common)


def test_terminal_frame_flags_are_strict_per_kind() -> None:
    session_id = SessionId(
        "session:550e8400-e29b-41d4-a716-446655440000"
    )
    with pytest.raises(ValueError):
        TerminalFrame(
            kind=TerminalFrameKind.INPUT,
            session_id=session_id,
            sequence=0,
            attachment_id="attachment:550e8400-e29b-41d4-a716-446655440002",
            flags=TerminalFrameFlags.REPLAY,
        )
    with pytest.raises(ValueError):
        TerminalFrame(
            kind=TerminalFrameKind.CONTINUITY_LOST,
            session_id=session_id,
            sequence=0,
            flags=TerminalFrameFlags.EOF,
        )

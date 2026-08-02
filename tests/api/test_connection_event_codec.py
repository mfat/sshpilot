from dataclasses import replace
from types import SimpleNamespace

import pytest

from sshpilot.api import EventType
from sshpilot.api.events import CoreEvent
from sshpilot.api.models import ConnectionId, ConnectionSummary
from sshpilot.api.transport.codec import (
    connection_event_from_envelope,
    connection_event_to_envelope,
    decode_envelope,
    encode_envelope,
)
def _connection(name="demo"):
    return ConnectionSummary(
        id=ConnectionId(name),
        nickname=name,
        host=name,
        hostname=f"{name}.example",
        username="alice",
        port=22,
    )


@pytest.mark.parametrize(
    "event_type",
    [
        EventType.CONNECTION_CREATED,
        EventType.CONNECTION_UPDATED,
        EventType.CONNECTION_DELETED,
    ],
)
def test_connection_event_codec_round_trip_is_typed(event_type):
    source = CoreEvent(
        type=event_type,
        payload=_connection(),
        sequence=2,
        connection_id="demo",
    )

    envelope = connection_event_to_envelope(
        source,
        sequence=17,
        protocol_version="1.0",
    )
    decoded_envelope = decode_envelope(encode_envelope(envelope))
    decoded = connection_event_from_envelope(decoded_envelope)

    assert decoded.type is event_type
    assert decoded.sequence == 17
    assert decoded.payload == source.payload
    assert decoded.connection_id == source.payload.id
    assert "password" not in encode_envelope(envelope)["payload"]


def test_event_name_and_payload_shape_must_match():
    valid = connection_event_to_envelope(
        CoreEvent(
            type=EventType.CONNECTION_CREATED,
            payload=_connection(),
            sequence=0,
        ),
        sequence=4,
        protocol_version="1.0",
    )
    mismatched = replace(valid, event="session.exited")

    with pytest.raises(ValueError, match="unsupported"):
        connection_event_from_envelope(mismatched)


def test_malformed_connection_event_payload_is_rejected_without_secret_repr():
    class SecretBearing:
        def __repr__(self):
            return "password=must-not-appear"

    unsafe = SecretBearing()
    with pytest.raises(TypeError) as caught:
        connection_event_to_envelope(
            SimpleNamespace(
                type=EventType.CONNECTION_UPDATED,
                payload=unsafe,
            ),
            sequence=4,
            protocol_version="1.0",
        )

    assert "must-not-appear" not in str(caught.value)


def test_wire_payload_allows_ssh_config_keys():
    """Event payloads may carry any dict key; transport validation is structural only.

    Sensitive-key rejection belongs in error details (``copy_safe_details``),
    not in event payloads or request params.
    """
    envelope = connection_event_to_envelope(
        CoreEvent(
            type=EventType.CONNECTION_UPDATED,
            payload=_connection(),
            sequence=0,
        ),
        sequence=5,
        protocol_version="1.0",
    )
    # Transport-safe validation should accept these keys without error
    replaced = replace(
        envelope,
        payload={**envelope.payload, "remote_command": "echo hello"},
    )
    assert replaced.payload["remote_command"] == "echo hello"

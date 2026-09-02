import json
import socket
import struct

import pytest

from sshpilot.api import ErrorCode
from sshpilot.api.models import ConnectionSummary
from sshpilot.api.transport.codec import connection_summary_to_wire
from sshpilot.api.transport import (
    MAX_FRAME_SIZE,
    ErrorData,
    ErrorResponseEnvelope,
    EventEnvelope,
    FrameDecoder,
    FramingError,
    RequestEnvelope,
    SuccessResponseEnvelope,
    decode_envelope,
    encode_envelope,
    encode_frame,
    receive_frame,
)


def _request():
    return RequestEnvelope(
        protocol_version="1.0",
        request_id="request-1",
        method="connections.list",
        params={},
        client_id="client-1",
    )


def test_complete_frame_round_trip():
    encoded = encode_frame(encode_envelope(_request()))
    decoder = FrameDecoder()

    messages = decoder.feed(encoded)

    assert messages == [encode_envelope(_request())]
    assert decode_envelope(messages[0]) == _request()
    decoder.finish()


def test_fragmented_header_and_body():
    encoded = encode_frame(encode_envelope(_request()))
    decoder = FrameDecoder()

    assert decoder.feed(encoded[:2]) == []
    assert decoder.feed(encoded[2:7]) == []
    assert decoder.feed(encoded[7:]) == [encode_envelope(_request())]
    decoder.finish()


def test_multiple_frames_in_one_read():
    first = encode_envelope(_request())
    second = encode_envelope(
        SuccessResponseEnvelope("1.0", "request-1", {"ok": True})
    )

    assert FrameDecoder().feed(encode_frame(first) + encode_frame(second)) == [
        first,
        second,
    ]


@pytest.mark.parametrize(
    "encoded,code",
    [
        (struct.pack(">I", 0), ErrorCode.INVALID_FRAME),
        (struct.pack(">I", MAX_FRAME_SIZE + 1), ErrorCode.FRAME_TOO_LARGE),
        (struct.pack(">I", 1) + b"{", ErrorCode.INVALID_FRAME),
        (struct.pack(">I", 1) + b"\xff", ErrorCode.INVALID_FRAME),
    ],
)
def test_invalid_frame_inputs_are_rejected(encoded, code):
    with pytest.raises(FramingError) as caught:
        FrameDecoder().feed(encoded)

    assert caught.value.code is code


def test_connection_closure_mid_frame_is_rejected():
    decoder = FrameDecoder()
    decoder.feed(struct.pack(">I", 20) + b"partial")

    with pytest.raises(FramingError) as caught:
        decoder.finish()

    assert caught.value.code is ErrorCode.INVALID_FRAME


def test_blocking_socket_round_trip_and_mid_frame_close():
    reader, writer = socket.socketpair()
    try:
        writer.sendall(encode_frame({"type": "probe"}))
        assert receive_frame(reader) == {"type": "probe"}
        writer.sendall(struct.pack(">I", 10) + b"part")
        writer.close()
        with pytest.raises(FramingError):
            receive_frame(reader)
    finally:
        reader.close()
        writer.close()


def test_encoder_rejects_non_json_and_oversized_values():
    with pytest.raises(FramingError) as unsafe:
        encode_frame({"unsafe": object()})
    with pytest.raises(FramingError) as oversized:
        encode_frame({"value": "x" * MAX_FRAME_SIZE})

    assert unsafe.value.code is ErrorCode.INVALID_FRAME
    assert oversized.value.code is ErrorCode.FRAME_TOO_LARGE


@pytest.mark.parametrize(
    "value",
    [
        {"type": "unknown"},
        {
            "type": "request",
            "protocol_version": "1.0",
            "request_id": "request-1",
        },
        {
            "type": "request",
            "protocol_version": "1.0",
            "request_id": "request-1",
            "method": "connections.list",
            "params": {},
            "client_id": "client-1",
            "extra": True,
        },
    ],
)
def test_unknown_incomplete_and_extra_envelope_fields_are_rejected(value):
    with pytest.raises(ValueError):
        decode_envelope(value)


def test_all_envelope_types_have_distinct_shapes():
    success = SuccessResponseEnvelope("1.0", "request-1", {"ok": True})
    error = ErrorResponseEnvelope(
        "1.0",
        "request-1",
        ErrorData(ErrorCode.INVALID_REQUEST, "Invalid request"),
    )
    event = EventEnvelope(
        "1.0",
        "connection.updated",
        4,
        connection_summary_to_wire(
            ConnectionSummary(
                id="demo",
                nickname="demo",
                host="demo",
                hostname="example.test",
                username="alice",
                port=22,
            )
        ),
    )

    assert decode_envelope(encode_envelope(success)) == success
    assert decode_envelope(encode_envelope(error)) == error
    assert decode_envelope(encode_envelope(event)) == event


def test_generic_transport_values_reject_objects_without_using_their_repr():
    class SecretBearing:
        def __repr__(self):
            return "token=must-not-appear"

    unsafe = SecretBearing()

    with pytest.raises(TypeError) as response_error:
        SuccessResponseEnvelope("1.0", "request-1", {"value": unsafe})
    with pytest.raises(TypeError) as request_error:
        RequestEnvelope(
            "1.0",
            "request-1",
            "connections.list",
            {"value": unsafe},
            "client-1",
        )

    assert repr(unsafe) not in str(response_error.value)
    assert repr(unsafe) not in str(request_error.value)


def test_json_payload_is_deterministic_and_not_pickle():
    frame = encode_frame({"z": 1, "a": 2})
    payload = frame[4:]

    assert payload == b'{"a":2,"z":1}'
    assert json.loads(payload) == {"a": 2, "z": 1}


def test_update_command_fields_envelope_round_trip():
    """Command-field edits (RemoteCommand/LocalCommand/PreCommand/ProxyJump)
    must survive full envelope encoding — a regression for the empty-command
    fields silently dropping out of the wire payload."""
    from sshpilot.api.models.common import ClientId, RequestId
    from sshpilot.api.version import PROTOCOL_VERSION

    request = RequestEnvelope(
        protocol_version=PROTOCOL_VERSION,
        request_id=RequestId("update-command-fields"),
        method="connections.update",
        params={
            "connection_id": "demo",
            "update": {
                "config_patch": {
                    "proxy_jump": ["bastion"],
                    "pre_command": "",
                    "local_command": "",
                    "remote_command": "",
                }
            },
        },
        client_id=ClientId("test-client"),
    )

    assert decode_envelope(encode_envelope(request)) == request


def test_surrogate_escaped_local_paths_survive_a_round_trip():
    """A local filename whose bytes are not valid UTF-8 reaches the daemon intact.

    ``os.scandir`` hands such names back surrogate-escaped, so the frontend has
    no valid-UTF-8 spelling of the path to send; strict encoding made those
    files impossible to transfer at all (issue #1235).
    """
    bad_path = "/home/user/\udce5\udcb1 broken.txt"
    request = RequestEnvelope(
        protocol_version="1.0",
        request_id="request-1",
        method="sftp.upload",
        params={"local_path": bad_path},
        client_id="client-1",
    )

    decoded = FrameDecoder().feed(encode_frame(encode_envelope(request)))

    assert decoded == [encode_envelope(request)]
    assert decoded[0]["params"]["local_path"] == bad_path
    # The bytes on the wire are the file's real name once the escape is undone.
    assert bad_path.encode("utf-8", "surrogateescape").endswith(
        b"\xe5\xb1 broken.txt"
    )

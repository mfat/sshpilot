import pytest

from sshpilot.api.models.common import AttachmentId, SessionId
from sshpilot.api.models.terminal import (
    BroadcastTerminalInputRequest,
    ReplayBounds,
    ReplayRequest,
    ReplayResult,
    TerminalDimensions,
    TerminalInput,
    TerminalOutput,
)
from sshpilot.api.transport.codec import (
    broadcast_terminal_input_request_from_wire,
    broadcast_terminal_input_request_to_wire,
)


def test_terminal_input_and_output_preserve_arbitrary_bytes():
    raw = b"\xff\xfe\x00hello"
    input_record = TerminalInput(
        session_id=SessionId("session-1"),
        attachment_id=AttachmentId("attachment-1"),
        data=raw,
    )
    output_record = TerminalOutput(
        session_id=SessionId("session-1"),
        sequence=7,
        data=raw,
    )

    assert input_record.data is raw
    assert output_record.data is raw
    assert raw.hex() not in repr(output_record)
    assert repr(raw) not in repr(output_record)


def test_terminal_broadcast_request_round_trips_session_targets_and_command():
    request = BroadcastTerminalInputRequest(
        session_ids=(SessionId("session-a"), SessionId("session-b")),
        command="cd /tmp",
    )

    assert broadcast_terminal_input_request_from_wire(
        broadcast_terminal_input_request_to_wire(request)
    ) == request


def test_terminal_broadcast_request_rejects_duplicate_sessions():
    with pytest.raises(ValueError, match="duplicate"):
        BroadcastTerminalInputRequest(
            session_ids=(SessionId("session-a"), SessionId("session-a")),
            command="true",
        )


def test_replay_bytes_are_excluded_from_repr():
    raw = b"potentially-sensitive-output"
    replay = ReplayResult(
        session_id=SessionId("session-1"),
        data=raw,
        first_sequence=0,
        next_sequence=1,
        bounds=ReplayBounds(
            earliest_sequence=0,
            latest_sequence=1,
            retained_bytes=len(raw),
        ),
    )

    assert repr(raw) not in repr(replay)


@pytest.mark.parametrize("rows,columns", [(0, 80), (24, 0), (10_001, 80), (24, 10_001)])
def test_terminal_dimensions_validate_bounds(rows, columns):
    with pytest.raises(ValueError):
        TerminalDimensions(rows=rows, columns=columns)


def test_replay_models_validate_sequences_and_limits():
    with pytest.raises(ValueError):
        ReplayRequest(session_id=SessionId("session-1"), max_bytes=0)
    with pytest.raises(ValueError):
        ReplayBounds(earliest_sequence=10, latest_sequence=9, retained_bytes=1)

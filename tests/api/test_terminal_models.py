import pytest

from sshpilot.api.models.common import AttachmentId, SessionId
from sshpilot.api.models.terminal import (
    ReplayBounds,
    ReplayRequest,
    TerminalDimensions,
    TerminalInput,
    TerminalOutput,
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


@pytest.mark.parametrize("rows,columns", [(0, 80), (24, 0), (10_001, 80), (24, 10_001)])
def test_terminal_dimensions_validate_bounds(rows, columns):
    with pytest.raises(ValueError):
        TerminalDimensions(rows=rows, columns=columns)


def test_replay_models_validate_sequences_and_limits():
    with pytest.raises(ValueError):
        ReplayRequest(session_id=SessionId("session-1"), max_bytes=0)
    with pytest.raises(ValueError):
        ReplayBounds(earliest_sequence=10, latest_sequence=9, retained_bytes=1)


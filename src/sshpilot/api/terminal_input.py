"""Client-side pacing for terminal input larger than one frame."""

from __future__ import annotations

import time
from typing import Callable

from .models.common import AttachmentId, SessionId
from .models.terminal import TerminalInput, TerminalInputStatusRequest
from .transport.terminal_frames import MAX_TERMINAL_PAYLOAD_SIZE

# Queue room left free so input held behind a paste fits when the paste ends.
_HEADROOM_BYTES = 16 * 1024
_POLL_SECONDS = 0.02


def send_paced_terminal_input(
    client,
    session_id: SessionId,
    attachment_id: AttachmentId,
    data: bytes,
    *,
    should_stop: Callable[[], bool],
) -> int:
    """Write ``data`` in order, never faster than the session's queue drains.

    Input frames are fire-and-report and the daemon rejects whatever does not
    fit, so frames sent blindly lose bytes from the middle of the input. Each
    frame here is sized from a status read that the daemon answers after every
    earlier frame. Blocks; returns how many bytes were sent.
    """
    status_request = TerminalInputStatusRequest(
        session_id=session_id,
        attachment_id=attachment_id,
    )
    view = memoryview(data)
    sent = 0
    while sent < len(data) and not should_stop():
        status = client.get_terminal_input_status(status_request)
        headroom = min(_HEADROOM_BYTES, status.capacity_bytes // 4)
        room = status.capacity_bytes - status.queued_bytes - headroom
        # Wait for a useful amount, so a full queue is not fed a byte at a time.
        if room <= 0 or room < min(len(data) - sent, status.capacity_bytes // 4):
            time.sleep(_POLL_SECONDS)
            continue
        end = sent + min(room, MAX_TERMINAL_PAYLOAD_SIZE, len(data) - sent)
        client.send_terminal_input(
            TerminalInput(
                session_id=session_id,
                attachment_id=attachment_id,
                data=bytes(view[sent:end]),
            )
        )
        sent = end
    return sent

"""Pause emulator feed while the user is selecting (PTY-less VTE).

VTE disconnects its PTY reader for the duration of a drag-select so child
output cannot rewrite the highlighted cells and call ``deselect_all``.
Daemon-backed tabs have no VTE PTY: the daemon pushes bytes with
``feed()`` instead, so that pause is a no-op and busy TUIs (htop, top)
wipe the selection mid-drag.

This buffer is the Ptyxis-equivalent pause for the feed path. Only arm it
when VTE itself would start a local selection on the primary press
(mouse tracking off, or Shift held while tracking is on).
"""

from __future__ import annotations


def selection_press_owns_pointer(
    *,
    mouse_tracking_active: bool,
    shift_held: bool,
) -> bool:
    """True when VTE would begin a local selection on this primary press."""
    if mouse_tracking_active and not shift_held:
        return False
    return True


class DeferredDisplayFeed:
    """Accumulate display bytes while a local selection gesture is active."""

    def __init__(self) -> None:
        self._paused = False
        self._buf = bytearray()

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def buffered_length(self) -> int:
        return len(self._buf)

    def begin(self) -> None:
        self._paused = True

    def accept(self, data: bytes) -> bytes | None:
        """Return *data* to paint now, or ``None`` when it was deferred."""
        if not data:
            return b""
        if self._paused:
            self._buf.extend(data)
            return None
        return data

    def end(self) -> bytes:
        """Leave the pause and return every deferred byte (may be empty)."""
        self._paused = False
        data = bytes(self._buf)
        self._buf.clear()
        return data

    def reset(self) -> None:
        """Drop any deferred bytes without painting them (session teardown)."""
        self._paused = False
        self._buf.clear()

"""PTY-bound terminal input helpers (GTK-free).

Daemon-backed emulators expose user input as Python strings (VTE ``commit``,
xterm.js ``onData``) even when the payload is an 8-bit protocol such as X10
mouse. Re-encoding those strings as UTF-8 corrupts high bytes, so ncurses
apps on older hosts (Ubuntu 18.04/20.04 htop) never see clicks. SGR mouse
is ASCII-only and is unaffected — which is why Ubuntu 24.04 still worked.
"""

from __future__ import annotations

import re
from typing import Optional, Union

CommitPayload = Union[str, bytes, bytearray, memoryview]

# DECSET/DECRST mouse modes used by ncurses/xterm. 9 is X10, 1000+ are
# X11/SGR variants; any one of them means the remote app owns pointer events.
_MOUSE_MODES = frozenset({9, 1000, 1001, 1002, 1003, 1005, 1006, 1015, 1016})
_MOUSE_CSI_RE = re.compile(rb"\x1b\[\?([\d;]+)([hl])")
_RESET_RE = re.compile(rb"\x1bc|\x1b\[!p")


def commit_payload_to_bytes(text: CommitPayload, size: Optional[int] = None) -> bytes:
    """Convert an emulator commit payload into bytes for the PTY.

    Keyboard UTF-8 round-trips as UTF-8. X10 mouse reports (and any other
    8-bit sequence) arrive as latin-1-mapped characters whose original byte
    length is ``size``. Prefer latin-1 when that matches ``size`` and UTF-8
    would not.
    """
    if isinstance(text, (bytes, bytearray, memoryview)):
        return bytes(text)
    if not text:
        return b""
    utf8 = text.encode("utf-8")
    reported = None if size is None else int(size)
    if reported is None or reported == len(utf8):
        return utf8
    try:
        raw = text.encode("latin-1")
    except UnicodeEncodeError:
        return utf8
    if len(raw) == reported:
        return raw
    return utf8


class MouseTrackingState:
    """Track whether the remote application currently wants mouse reports."""

    def __init__(self) -> None:
        self._active: set[int] = set()
        self._tail = b""

    @property
    def active(self) -> bool:
        return bool(self._active)

    def reset(self) -> None:
        self._active.clear()
        self._tail = b""

    def feed(self, data: bytes) -> bool:
        """Consume display bytes and update tracking state.

        Incomplete CSI sequences at the end of a chunk are held until the
        next call so ``ESC[?1000h`` split across writes is not missed.
        """
        if not data:
            return self.active
        buf = self._tail + bytes(data)
        start = 0
        for reset in _RESET_RE.finditer(buf):
            start = reset.end()
            self._active.clear()
        scan = buf[start:]
        consumed = start
        for match in _MOUSE_CSI_RE.finditer(scan):
            consumed = start + match.end()
            enable = match.group(2) == b"h"
            for part in match.group(1).split(b";"):
                try:
                    mode = int(part)
                except ValueError:
                    continue
                if mode not in _MOUSE_MODES:
                    continue
                if enable:
                    self._active.add(mode)
                else:
                    self._active.discard(mode)
        suffix = buf[consumed:]
        esc = suffix.rfind(b"\x1b")
        if esc >= 0 and len(suffix) - esc < 64:
            self._tail = suffix[esc:]
        else:
            self._tail = b""
        return self.active

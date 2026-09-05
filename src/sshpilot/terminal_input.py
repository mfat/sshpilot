"""PTY-bound terminal input helpers (GTK-free).

Daemon-backed emulators expose user input as Python strings (VTE ``commit``,
xterm.js ``onData``) even when the payload is an 8-bit protocol such as X10
mouse. Re-encoding those strings as UTF-8 corrupts high bytes, so keep the
original byte length around and prefer latin-1 when it round-trips.

That alone does not carry legacy mouse on the VTE path, because VTE never
offers those bytes to us. ``Terminal::feed_child_binary()`` — the only
encoder for the legacy ``ESC[M`` report — returns early when the widget has
no PTY, *before* it emits ``commit``; SGR (1006) reports take
``send_child()``, which deliberately emits ``commit`` with no PTY. A
daemon-backed VTE has no PTY, so remotes whose ncurses still asks for the
legacy encoding (Ubuntu 18.04/20.04 htop) saw no clicks at all, while
Ubuntu 24.04 asks for SGR and worked (GH #1212). ``MouseTrackingState``
therefore drives VTE in SGR
locally and re-encodes the reports on the way out, which also keeps the
payload ASCII and away from the UTF-8 problem above.
"""

from __future__ import annotations

import re
from typing import Optional, Union

CommitPayload = Union[str, bytes, bytearray, memoryview]

# DECSET/DECRST mouse modes used by ncurses/xterm. 9 is X10, 1000+ are
# X11/SGR variants; any one of them means the remote app owns pointer events.
_MOUSE_MODES = frozenset({9, 1000, 1001, 1002, 1003, 1005, 1006, 1015, 1016})
# The modes that make VTE pick its legacy ESC[M encoder, mirroring
# Terminal::update_mouse_protocol(). 1005/1015/1016 are absent because VTE
# does not implement them: with those set alone it still emits legacy.
_LEGACY_MOUSE_MODES = frozenset({9, 1000, 1001, 1002, 1003})
# The one extended encoding VTE implements (Terminal::feed_mouse_event only
# branches on XTERM_MOUSE_EXT_SGR). While the remote holds this, it is
# already getting reports we do not need to touch.
_SGR_MOUSE_MODE = 1006
_MOUSE_CSI_RE = re.compile(rb"\x1b\[\?([\d;]+)([hl])")
_RESET_RE = re.compile(rb"\x1bc|\x1b\[!p")
_SGR_REPORT_RE = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
# Modifier and drag bits in a report's button field (4 shift, 8 alt,
# 16 control, 32 drag). A legacy release keeps these but replaces the button
# itself with 3, exactly as Terminal::feed_mouse_event does.
_MOUSE_MODIFIER_BITS = 4 | 8 | 16 | 32
# Legacy coordinates are sent as a single ``32 + value`` byte, so VTE drops
# any report that would not fit. Match that rather than sending a truncated
# one.
_LEGACY_MOUSE_MAX = 223


def commit_payload_to_bytes(text: CommitPayload, size: Optional[int] = None) -> bytes:
    """Convert an emulator commit payload into bytes for the PTY.

    Keyboard UTF-8 round-trips as UTF-8. X10 mouse reports (and any other
    8-bit sequence) arrive as latin-1-mapped characters whose original byte
    length is ``size``. Prefer latin-1 when that matches ``size`` and UTF-8
    would not.

    ``size`` also rescues the NUL bytes ``G_TYPE_STRING`` throws away. VTE
    declares ``commit`` with a C string, so PyGObject stops at the first NUL
    while ``size`` still counts the whole payload: Ctrl+Space (and Ctrl+@,
    Ctrl+2) arrives as ``("", 1)`` instead of ``"\0"``, Ctrl+Alt+Space as
    ``("\x1b", 2)`` instead of ``"\x1b\0"``. Only the daemon path notices,
    because a PTY-backed VTE writes those bytes itself (GH #1240). ``size``
    is what tells Ctrl+[ (``("\x1b", 1)``) apart from Ctrl+Alt+Space, so pad
    the short encoding back out rather than guessing from the text alone.
    """
    if isinstance(text, (bytes, bytearray, memoryview)):
        return bytes(text)
    reported = None if size is None else int(size)
    if not text:
        # Truncated to nothing: the payload was NUL bytes all the way.
        return b"\x00" * reported if reported and reported > 0 else b""
    utf8 = text.encode("utf-8")
    if reported is None or reported == len(utf8):
        return utf8
    try:
        raw: Optional[bytes] = text.encode("latin-1")
    except UnicodeEncodeError:
        raw = None
    if raw is not None and len(raw) == reported:
        return raw
    # Neither encoding reaches ``size``, so trailing NULs were dropped. Try
    # UTF-8 first: every key VTE encodes with a NUL is a keyboard payload.
    for candidate in (utf8, raw):
        if candidate is not None and len(candidate) < reported:
            return candidate + b"\x00" * (reported - len(candidate))
    return utf8


def sgr_reports_to_legacy(payload: bytes) -> bytes:
    """Rewrite SGR (1006) mouse reports in ``payload`` as legacy ``ESC[M``.

    Only the reports are touched; anything else in the payload (a keystroke
    racing a click, say) is passed through. Reports whose button or
    coordinates do not fit a ``32 + value`` byte are dropped rather than
    truncated, which is what VTE does with them too.
    """
    def encode(match: "re.Match[bytes]") -> bytes:
        button = int(match.group(1))
        column = int(match.group(2))
        row = int(match.group(3))
        if match.group(4) == b"m":
            # Legacy has no release button, only the "any release" code 3.
            button = 3 | (button & _MOUSE_MODIFIER_BITS)
        if max(button, column, row) > _LEGACY_MOUSE_MAX:
            return b""
        return b"\x1b[M" + bytes((32 + button, 32 + column, 32 + row))

    return _SGR_REPORT_RE.sub(encode, payload)


class MouseTrackingState:
    """Track whether the remote application currently wants mouse reports.

    Also decides when the emulator must be driven in SGR behind the remote's
    back so its reports survive the trip to us — see the module docstring.
    """

    def __init__(self) -> None:
        self._active: set[int] = set()
        self._tail = b""
        self._sgr_forced = False

    @property
    def active(self) -> bool:
        return bool(self._active)

    @property
    def modes(self) -> tuple[int, ...]:
        """Enabled DECSET mouse modes, sorted for stable log output."""
        return tuple(sorted(self._active))

    @property
    def remote_uses_sgr(self) -> bool:
        """Whether the remote enabled SGR itself, so reports already reach us."""
        return _SGR_MOUSE_MODE in self._active

    @property
    def translating_legacy(self) -> bool:
        """Whether commit payloads must be re-encoded as legacy reports."""
        return self._sgr_forced

    def take_local_mode_feed(self) -> bytes:
        """DECSET bytes owed to the emulator alone — never sent to the remote.

        Call after feeding a chunk to the tracker and to the emulator, so the
        remote's own mode changes land first and this settles on top.
        """
        wanted = bool(self._active & _LEGACY_MOUSE_MODES) and not self.remote_uses_sgr
        if wanted == self._sgr_forced:
            return b""
        self._sgr_forced = wanted
        if wanted:
            return b"\x1b[?1006h"
        if self.remote_uses_sgr:
            # The remote took SGR over; its own DECSET already left the
            # emulator where it wants it, so resetting here would undo that.
            return b""
        return b"\x1b[?1006l"

    def reset(self) -> None:
        self._active.clear()
        self._tail = b""
        self._sgr_forced = False

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
            # The emulator sees the same RIS/DECSTR and drops the mode we
            # forced on it, so take_local_mode_feed() must re-issue it.
            self._sgr_forced = False
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

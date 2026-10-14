"""Model-independent pieces of the built-in terminal backend.

Shared by the pyte model (``pyte_terminal_model``) and the libvterm model
(``vterm_terminal_model``): DECSET mode constants, the absolute-row text,
selection and search helpers, and xterm key and mouse report encoding.
Nothing here imports pyte, GTK or libvterm.
"""

from __future__ import annotations

import re
import time
from typing import Callable, List, Optional, Tuple

#: DECSET modes, shifted the way pyte stores private modes. The libvterm model
#: reports its modes as the same numbers so the view can test either.
MODE_APP_CURSOR = 1 << 5
MODE_MOUSE_X10 = 9 << 5
MODE_MOUSE_NORMAL = 1000 << 5
MODE_MOUSE_BUTTON_MOTION = 1002 << 5
MODE_MOUSE_ANY_MOTION = 1003 << 5
MODE_MOUSE_SGR = 1006 << 5
MODE_ALTERNATE_SCROLL = 1007 << 5
MODE_BRACKETED_PASTE = 2004 << 5
MODE_FOCUS_EVENTS = 1004 << 5


def default_builtin_model() -> str:
    """The built-in backend's emulator model when the config names none.

    libvterm on Windows, where the built-in backend is the only terminal and
    the bundled ConPTY forwards full-screen streams pyte handles badly; pyte
    elsewhere, where the built-in backend is opt-in.
    """

    import sys

    return "vterm" if sys.platform == "win32" else "pyte"


class SynchronizedOutput:
    """Mode 2026: hold repaints while an application redraws.

    Between ``CSI ? 2026 h`` and ``CSI ? 2026 l`` a TUI rewrites the screen
    in pieces; painting in between shows it half-drawn. Neither pyte nor
    libvterm 0.3.3 knows the mode, so the view tracks it from the byte
    stream, as Neovim's terminal does. A redraw left open longer than
    :attr:`TIMEOUT` is treated as finished, so a crashed app cannot freeze
    the screen.

    The DECRQM query ``CSI ? 2026 $ p`` is answered here and removed from
    the stream; the models would otherwise report the mode as unknown and
    applications would not use it.
    """

    TIMEOUT = 0.2
    _SEQUENCE = re.compile(rb"\x1b\[\?2026(?:([hl])|\$p)")
    _PREFIXES = tuple(b"\x1b[?2026$p"[:n] for n in range(1, 10)) + (b"\x1b[?2026",)

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._since: Optional[float] = None
        self._tail = b""

    @property
    def active(self) -> bool:
        if self._since is None:
            return False
        if self._clock() - self._since >= self.TIMEOUT:
            self._since = None
        return self._since is not None

    def filter(self, data: bytes) -> Tuple[bytes, bytes]:
        """``(data for the model, reply for the remote)``."""

        if self._tail:
            data, self._tail = self._tail + data, b""
        if b"\x1b[?2026" not in data and not data.endswith(self._PREFIXES):
            return data, b""
        reply = b""
        pieces = []
        position = 0
        for match in self._SEQUENCE.finditer(data):
            action = match.group(1)
            if action == b"h":
                self._since = self._clock()
            elif action == b"l":
                self._since = None
            else:
                reply += b"\x1b[?2026;%d$y" % (1 if self.active else 2)
                pieces.append(data[position:match.start()])
                position = match.end()
        pieces.append(data[position:])
        data = b"".join(pieces)
        start = data.rfind(b"\x1b")
        if start >= 0 and data[start:] in self._PREFIXES:
            data, self._tail = data[:start], data[start:]
        return data, reply

    def remaining(self) -> float:
        """Seconds until an open redraw times out (0 when none is open)."""

        if not self.active:
            return 0.0
        return max(0.0, self.TIMEOUT - (self._clock() - self._since))


class LineIndexMixin:
    """Text access over absolute rows (scrollback first, then the screen).

    Needs ``columns``, ``lines``, ``history_length`` and ``line(row)``,
    where a line maps a column to a cell with a ``data`` string.
    """

    @property
    def total_rows(self) -> int:
        return self.history_length + self.lines

    def viewport_top(self, scroll_offset: int) -> int:
        """Absolute row shown at the top when scrolled ``scroll_offset`` up."""

        offset = max(0, min(scroll_offset, self.history_length))
        return self.history_length - offset

    # -- text extraction -------------------------------------------------------

    def row_text(self, row: int, start: int = 0, end: Optional[int] = None) -> str:
        line = self.line(row)
        default = " "
        end = self.columns if end is None else min(end, self.columns)
        cells = []
        for x in range(max(0, start), end):
            char = line.get(x)
            data = char.data if char is not None else default
            cells.append(data)  # "" for the stub after a wide character
        return "".join(cells)

    def text_between(self, start: Tuple[int, int], end: Tuple[int, int]) -> str:
        """Text from ``start`` to ``end`` (inclusive), as ``(row, column)``."""

        (r0, c0), (r1, c1) = sorted((start, end))
        r1 = min(r1, self.total_rows - 1)
        pieces: List[str] = []
        for row in range(max(0, r0), r1 + 1):
            first = c0 if row == r0 else 0
            last = c1 + 1 if row == r1 else self.columns
            pieces.append(self.row_text(row, first, last).rstrip())
        return "\n".join(pieces)

    def all_text(self) -> str:
        rows = [self.row_text(row).rstrip() for row in range(self.total_rows)]
        while rows and not rows[-1]:
            rows.pop()
        return "\n".join(rows)

    def word_bounds(self, row: int, column: int) -> Tuple[int, int]:
        text = self.row_text(row)
        if not (0 <= column < len(text)) or _is_word_break(text[column]):
            return column, column
        start = column
        while start > 0 and not _is_word_break(text[start - 1]):
            start -= 1
        end = column
        while end + 1 < len(text) and not _is_word_break(text[end + 1]):
            end += 1
        return start, end

    # -- search ----------------------------------------------------------------

    def find(
        self,
        matcher: Callable[[str, int], Optional[Tuple[int, int]]],
        *,
        after: Optional[Tuple[int, int]] = None,
        backwards: bool = False,
        wrap: bool = True,
    ) -> Optional[Tuple[int, int, int]]:
        """Find the next hit as ``(row, start_col, end_col_exclusive)``.

        ``matcher(text, from_index)`` returns the first ``(start, end)`` at
        or after ``from_index`` in ``text``, or ``None``.
        """

        total = self.total_rows
        if total == 0:
            return None
        texts = [self.row_text(row) for row in range(total)]
        if backwards:
            return self._find_backwards(texts, matcher, after, wrap)
        row0, col0 = after if after is not None else (0, -1)
        order = list(range(row0, total)) + (list(range(0, row0 + 1)) if wrap else [])
        for index, row in enumerate(order):
            start_at = col0 + 1 if index == 0 else 0
            hit = matcher(texts[row], max(0, start_at))
            if hit is not None and (index == 0 or row != row0 or hit[0] <= col0):
                return row, hit[0], hit[1]
        return None

    def _find_backwards(self, texts, matcher, before, wrap):
        total = len(texts)
        row0, col0 = before if before is not None else (total - 1, self.columns)
        rows = list(range(row0, -1, -1)) + (list(range(total - 1, row0 - 1, -1)) if wrap else [])
        for index, row in enumerate(rows):
            hits = []
            position = 0
            while True:
                hit = matcher(texts[row], position)
                if hit is None:
                    break
                hits.append(hit)
                position = hit[0] + 1
            if index == 0:
                hits = [hit for hit in hits if hit[0] < col0]
            if hits:
                start, end = hits[-1]
                return row, start, end
        return None



def _is_word_break(char: str) -> bool:
    return char in " \t\"'`()[]{}<>|;" or not char


# ---------------------------------------------------------------------------
# Key encoding (xterm conventions)

_CSI_LETTER_KEYS = {
    "Up": "A", "Down": "B", "Right": "C", "Left": "D",
    "Home": "H", "End": "F",
    "KP_Up": "A", "KP_Down": "B", "KP_Right": "C", "KP_Left": "D",
    "KP_Home": "H", "KP_End": "F",
}
_CSI_TILDE_KEYS = {
    "Insert": 2, "Delete": 3, "Page_Up": 5, "Page_Down": 6,
    "KP_Insert": 2, "KP_Delete": 3, "KP_Page_Up": 5, "KP_Page_Down": 6,
    "F5": 15, "F6": 17, "F7": 18, "F8": 19, "F9": 20, "F10": 21,
    "F11": 23, "F12": 24,
}
_SS3_FUNCTION_KEYS = {"F1": "P", "F2": "Q", "F3": "R", "F4": "S"}


def _modifier_param(*, shift: bool, alt: bool, ctrl: bool) -> int:
    return 1 + (1 if shift else 0) + (2 if alt else 0) + (4 if ctrl else 0)


def encode_key(
    name: str,
    text: str = "",
    *,
    ctrl: bool = False,
    alt: bool = False,
    shift: bool = False,
    app_cursor: bool = False,
) -> Optional[bytes]:
    """Bytes a key press sends to the remote, or ``None`` if it sends nothing.

    ``name`` is the GDK keyval name (``"Up"``, ``"Return"``, ``"a"``) and
    ``text`` the character it produces, if any.
    """

    modifier = _modifier_param(shift=shift, alt=alt, ctrl=ctrl)

    if name in _CSI_LETTER_KEYS:
        letter = _CSI_LETTER_KEYS[name]
        if modifier > 1:
            return f"\x1b[1;{modifier}{letter}".encode()
        if app_cursor and letter in "ABCD":
            return f"\x1bO{letter}".encode()
        return f"\x1b[{letter}".encode()
    if name in _CSI_TILDE_KEYS:
        number = _CSI_TILDE_KEYS[name]
        if modifier > 1:
            return f"\x1b[{number};{modifier}~".encode()
        return f"\x1b[{number}~".encode()
    if name in _SS3_FUNCTION_KEYS:
        letter = _SS3_FUNCTION_KEYS[name]
        if modifier > 1:
            return f"\x1b[1;{modifier}{letter}".encode()
        return f"\x1bO{letter}".encode()

    prefix = b"\x1b" if alt else b""
    if name in ("Return", "KP_Enter", "ISO_Enter"):
        return prefix + b"\r"
    if name == "BackSpace":
        return prefix + (b"\x08" if ctrl else b"\x7f")
    if name == "Tab" and not shift:
        return prefix + b"\t"
    if name in ("ISO_Left_Tab", "Tab"):
        return b"\x1b[Z"
    if name == "Escape":
        return prefix + b"\x1b"

    if ctrl:
        control = _control_character(name, text)
        if control is not None:
            return prefix + control
    if text:
        return prefix + text.encode("utf-8")
    return None


def _control_character(name: str, text: str) -> Optional[bytes]:
    key = (text or (name if len(name) == 1 else "")).lower()
    if len(key) != 1:
        return {"space": b"\x00"}.get(name.lower())
    if "a" <= key <= "z":
        return bytes([ord(key) - ord("a") + 1])
    return {
        " ": b"\x00", "@": b"\x00", "2": b"\x00",
        "[": b"\x1b", "3": b"\x1b",
        "\\": b"\x1c", "4": b"\x1c",
        "]": b"\x1d", "5": b"\x1d",
        "^": b"\x1e", "6": b"\x1e",
        "_": b"\x1f", "-": b"\x1f", "7": b"\x1f",
        "/": b"\x1f", "?": b"\x7f", "8": b"\x7f",
    }.get(key)


# ---------------------------------------------------------------------------
# Mouse reports

MOUSE_LEFT, MOUSE_MIDDLE, MOUSE_RIGHT = 0, 1, 2
MOUSE_WHEEL_UP, MOUSE_WHEEL_DOWN = 64, 65


def encode_mouse(
    button: int,
    column: int,
    row: int,
    *,
    action: str,
    sgr: bool,
    shift: bool = False,
    alt: bool = False,
    ctrl: bool = False,
) -> Optional[bytes]:
    """xterm mouse report for a 0-based cell; ``action`` is press/release/motion."""

    code = button
    if action == "motion":
        code += 32
    code += (4 if shift else 0) + (8 if alt else 0) + (16 if ctrl else 0)
    if sgr:
        final = "m" if action == "release" else "M"
        return f"\x1b[<{code};{column + 1};{row + 1}{final}".encode()
    if action == "release":
        code = 3 + (code & ~3 & ~64)
    x, y = column + 1, row + 1
    if x > 223 or y > 223:
        return None  # not representable in the legacy encoding
    return bytes([0x1B, 0x5B, 0x4D, 32 + code, 32 + x, 32 + y])

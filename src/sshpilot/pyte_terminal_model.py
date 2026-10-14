"""Headless VT emulator model for the pyte terminal backend.

GTK-free so it can be unit-tested anywhere. ``pyte`` does the escape-sequence
parsing; this module adds what an interactive terminal needs on top of it:

* scrollback that survives full-screen scrolling and shrinking resizes,
* the alternate screen (DECSET 47/1047/1049) used by vim, less, htop, ...,
* replies to the remote (DA/DSR) routed to a callback instead of dropped,
* selection/search over scrollback plus screen,
* xterm key and mouse report encoding.
"""

from __future__ import annotations

import copy
from collections import defaultdict, deque
from typing import Callable, Deque, Dict, List, Optional, Tuple

import pyte
from pyte import modes as mo
from pyte.screens import Char, StaticDefaultDict

Line = Dict[int, Char]

DEFAULT_SCROLLBACK_LINES = 10000

_ALT_SCREEN_MODES = frozenset({47, 1047, 1049})

#: DECSET modes, shifted the way pyte stores private modes.
MODE_APP_CURSOR = 1 << 5
MODE_MOUSE_X10 = 9 << 5
MODE_MOUSE_NORMAL = 1000 << 5
MODE_MOUSE_BUTTON_MOTION = 1002 << 5
MODE_MOUSE_ANY_MOTION = 1003 << 5
MODE_MOUSE_SGR = 1006 << 5
MODE_ALTERNATE_SCROLL = 1007 << 5
MODE_BRACKETED_PASTE = 2004 << 5
MODE_FOCUS_EVENTS = 1004 << 5


class EmulatorScreen(pyte.Screen):
    """``pyte.Screen`` with scrollback, an alternate screen, and replies."""

    def __init__(
        self,
        columns: int,
        lines: int,
        *,
        scrollback_limit: int = DEFAULT_SCROLLBACK_LINES,
        on_reply: Optional[Callable[[bytes], None]] = None,
    ) -> None:
        self.scrollback: Deque[Line] = deque(maxlen=max(0, scrollback_limit))
        self.on_reply = on_reply
        self.title_changed = False
        self.bell_rung = False
        self._main_buffer = None
        self._main_cursor = None
        #: Bumped whenever lines enter the scrollback, so a scrolled-back
        #: viewport can stay anchored on the same text.
        self.scrollback_pushes = 0
        super().__init__(columns, lines)

    # -- small helpers -------------------------------------------------------

    def _new_buffer(self):
        return defaultdict(lambda: StaticDefaultDict[int, Char](self.default_char))

    @property
    def alternate_active(self) -> bool:
        return self._main_buffer is not None

    def _push_scrollback(self, line: Line) -> None:
        if self.scrollback.maxlen == 0 or self.alternate_active:
            return
        self.scrollback.append(dict(line))
        self.scrollback_pushes += 1

    # -- pyte event overrides ------------------------------------------------

    def write_process_input(self, data: str) -> None:
        if self.on_reply is not None:
            self.on_reply(data.encode("utf-8"))

    def set_title(self, param: str) -> None:
        super().set_title(param)
        self.title_changed = True

    def bell(self, *args) -> None:
        self.bell_rung = True

    def reset(self) -> None:
        self._main_buffer = None
        self._main_cursor = None
        super().reset()

    def index(self) -> None:
        top, bottom = self.margins or (0, self.lines - 1)
        if self.cursor.y == bottom and top == 0:
            self._push_scrollback(self.buffer[0])
        super().index()

    def erase_in_display(self, how: int = 0, *args, **kwargs) -> None:
        if how == 3:
            # xterm "ED 3": clear the saved lines only (``clear`` sends it).
            self.scrollback.clear()
            self.dirty.update(range(self.lines))
            return
        super().erase_in_display(how, *args, **kwargs)

    def set_mode(self, *modes: int, **kwargs) -> None:
        if kwargs.get("private"):
            for mode in modes:
                if mode in _ALT_SCREEN_MODES:
                    self._enter_alternate(mode)
            modes = tuple(mode for mode in modes if mode not in _ALT_SCREEN_MODES)
        if modes:
            super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes: int, **kwargs) -> None:
        if kwargs.get("private"):
            for mode in modes:
                if mode in _ALT_SCREEN_MODES:
                    self._leave_alternate(mode)
            modes = tuple(mode for mode in modes if mode not in _ALT_SCREEN_MODES)
        if modes:
            super().reset_mode(*modes, **kwargs)

    def _enter_alternate(self, mode: int) -> None:
        if self.alternate_active:
            return
        self._main_buffer = self.buffer
        self._main_cursor = copy.copy(self.cursor) if mode == 1049 else None
        self.buffer = self._new_buffer()
        self.dirty.update(range(self.lines))

    def _leave_alternate(self, mode: int) -> None:
        if not self.alternate_active:
            return
        self.buffer = self._main_buffer
        self._main_buffer = None
        if self._main_cursor is not None:
            self.cursor = self._main_cursor
            self._main_cursor = None
            self.ensure_hbounds()
            self.ensure_vbounds()
        self.dirty.update(range(self.lines))

    def resize(self, lines: Optional[int] = None, columns: Optional[int] = None) -> None:
        lines = max(1, lines or self.lines)
        columns = max(1, columns or self.columns)
        if lines == self.lines and columns == self.columns:
            return

        if lines < self.lines:
            # Keep the cursor row on screen: scroll the top rows away (into
            # the scrollback on the main screen) rather than clipping the
            # bottom, which is where the prompt is.
            excess = max(0, self.cursor.y - (lines - 1))
            if excess:
                for y in range(excess):
                    self._push_scrollback(self.buffer[y])
                self._shift_up(excess)
                self.cursor.y -= excess
            for y in [y for y in self.buffer if y >= lines]:
                self.buffer.pop(y, None)
        elif lines > self.lines and not self.alternate_active and self.scrollback:
            # Growing: pull history back down, like xterm and VTE do.
            pulled = min(lines - self.lines, len(self.scrollback))
            self._shift_down(pulled)
            for y in range(pulled - 1, -1, -1):
                restored = StaticDefaultDict[int, Char](self.default_char)
                restored.update(self.scrollback.pop())
                self.buffer[y] = restored
            self.cursor.y += pulled

        if columns < self.columns:
            for buffer in (self.buffer, self._main_buffer or {}):
                for line in buffer.values():
                    for x in [x for x in line if x >= columns]:
                        line.pop(x, None)

        self.lines, self.columns = lines, columns
        self.tabstops = set(range(8, self.columns, 8))
        self.set_margins()
        self.ensure_hbounds()
        self.ensure_vbounds()
        self.dirty.update(range(self.lines))

    def _shift_up(self, count: int) -> None:
        moved = self._new_buffer()
        for y, line in self.buffer.items():
            if y >= count:
                moved[y - count] = line
        self.buffer = moved

    def _shift_down(self, count: int) -> None:
        moved = self._new_buffer()
        for y, line in self.buffer.items():
            moved[y + count] = line
        self.buffer = moved

    # -- mode queries ----------------------------------------------------------

    @property
    def app_cursor_keys(self) -> bool:
        return MODE_APP_CURSOR in self.mode

    @property
    def bracketed_paste(self) -> bool:
        return MODE_BRACKETED_PASTE in self.mode

    @property
    def mouse_sgr(self) -> bool:
        return MODE_MOUSE_SGR in self.mode

    @property
    def mouse_tracking(self) -> Optional[str]:
        """``"any"``, ``"drag"``, ``"click"``, ``"x10"`` or ``None``."""

        if MODE_MOUSE_ANY_MOTION in self.mode:
            return "any"
        if MODE_MOUSE_BUTTON_MOTION in self.mode:
            return "drag"
        if MODE_MOUSE_NORMAL in self.mode:
            return "click"
        if MODE_MOUSE_X10 in self.mode:
            return "x10"
        return None

    @property
    def cursor_visible(self) -> bool:
        return mo.DECTCEM in self.mode and not self.cursor.hidden


class TerminalModel:
    """Screen + byte stream + a combined scrollback/screen line index.

    Rows are addressed absolutely: ``0`` is the oldest scrollback line and
    ``history_length + screen.lines - 1`` is the bottom screen row.
    """

    def __init__(
        self,
        columns: int = 80,
        lines: int = 24,
        *,
        scrollback_limit: int = DEFAULT_SCROLLBACK_LINES,
        on_reply: Optional[Callable[[bytes], None]] = None,
    ) -> None:
        self.screen = EmulatorScreen(
            columns,
            lines,
            scrollback_limit=scrollback_limit,
            on_reply=on_reply,
        )
        self.stream = pyte.ByteStream(self.screen)

    # -- input -----------------------------------------------------------------

    def feed(self, data: bytes) -> None:
        self.stream.feed(bytes(data))

    def resize(self, columns: int, lines: int) -> None:
        self.screen.resize(lines=lines, columns=columns)

    def reset(self, *, clear_scrollback: bool) -> None:
        self.screen.reset()
        if clear_scrollback:
            self.screen.scrollback.clear()

    # -- geometry --------------------------------------------------------------

    @property
    def columns(self) -> int:
        return self.screen.columns

    @property
    def lines(self) -> int:
        return self.screen.lines

    @property
    def history_length(self) -> int:
        return len(self.screen.scrollback)

    @property
    def total_rows(self) -> int:
        return self.history_length + self.screen.lines

    def line(self, row: int) -> Line:
        history = self.history_length
        if row < history:
            return self.screen.scrollback[row]
        return self.screen.buffer[row - history]

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

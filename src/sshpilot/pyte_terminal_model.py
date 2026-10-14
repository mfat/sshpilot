"""Headless VT emulator model for the pyte terminal backend.

GTK-free so it can be unit-tested anywhere. ``pyte`` does the escape-sequence
parsing; this module adds what an interactive terminal needs on top of it:

* scrollback that survives full-screen scrolling and shrinking resizes,
* the alternate screen (DECSET 47/1047/1049) used by vim, less, htop, ...,
* replies to the remote (DA/DSR) routed to a callback instead of dropped,
* selection/search over scrollback plus screen (``terminal_model_common``).
"""

from __future__ import annotations

import copy
import re
from collections import defaultdict, deque
from typing import Callable, Deque, Dict, List, Optional

import pyte
from pyte import modes as mo
from pyte.screens import Char, StaticDefaultDict

from .terminal_model_common import (  # noqa: F401  (re-exported for callers)
    MODE_ALTERNATE_SCROLL,
    MODE_APP_CURSOR,
    MODE_BRACKETED_PASTE,
    MODE_FOCUS_EVENTS,
    MODE_MOUSE_ANY_MOTION,
    MODE_MOUSE_BUTTON_MOTION,
    MODE_MOUSE_NORMAL,
    MODE_MOUSE_SGR,
    MODE_MOUSE_X10,
    MOUSE_LEFT,
    MOUSE_MIDDLE,
    MOUSE_RIGHT,
    MOUSE_WHEEL_DOWN,
    MOUSE_WHEEL_UP,
    LineIndexMixin,
    encode_key,
    encode_mouse,
)

Line = Dict[int, Char]

DEFAULT_SCROLLBACK_LINES = 10000

_ALT_SCREEN_MODES = frozenset({47, 1047, 1049})


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
        #: Growing pulls history back onto the screen (xterm, VTE). Off for a
        #: stream already rendered by a terminal that repaints its own
        #: top-anchored viewport after a resize (ConPTY), where the pulled
        #: rows would be painted over and lost.
        self.pull_history_on_grow = True
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
        elif (
            lines > self.lines
            and self.pull_history_on_grow
            and not self.alternate_active
            and self.scrollback
        ):
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


# ---------------------------------------------------------------------------
# CSI forms pyte 0.8 mis-parses

#: CSIs pyte gets wrong: a ``<=>?`` leader, ``:`` sub-parameters,
#: intermediate bytes, or an SGR underline colour (58). Everything else
#: (plain SGR, cursor moves, erases) goes to pyte untouched.
_UNUSUAL_CSI = re.compile(
    rb"\x1b\[(?P<leader>[<=>?]?)(?P<params>[0-9;:]*)(?P<inter>[\x20-\x2f]*)(?P<final>[\x40-\x7e])"
)
_MAYBE_UNUSUAL = re.compile(
    rb"\x1b\[(?:[<=>?]|[0-9;]*:|[0-9;:]*[\x20-\x2f]|(?:[0-9;]*;)?58[;:m])"
)
#: An ESC or a CSI that is still open at the end of a feed.
_OPEN_CSI = re.compile(rb"\x1b(?:\[[<=>?]?[0-9;:]*[\x20-\x2f]*)?\Z")
#: How much of an unfinished CSI is held back for the next feed.
_MAX_HELD = 256

#: Secondary DA reply, the same one libvterm sends.
_DA2_REPLY = b"\x1b[>0;100;0c"


def _sgr_params(params: bytes) -> Optional[bytes]:
    """Plain ``;`` SGR parameters pyte understands, or ``None`` to drop."""

    out: List[str] = []
    fields = params.decode("ascii").split(";") if params else [""]
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if ":" in field:
            sub = field.split(":")
            head = sub[0]
            if head in ("38", "48"):
                rest = sub[2:]
                if sub[1:2] == ["2"] and len(rest) >= 3:
                    out.extend([head, "2"] + [value or "0" for value in rest[-3:]])
                elif sub[1:2] == ["5"] and rest:
                    out.extend([head, "5", rest[0] or "0"])
            elif head == "4":
                # Curly/dotted underline styles: plain underline, or off.
                out.append("24" if sub[1:2] == ["0"] else "4")
            elif head != "58":
                out.append(head)
            continue
        if field == "58":
            # Underline colour: pyte has none, and would read its R;G;B as SGR codes.
            if index < len(fields) and fields[index] == "2":
                index += 4
            elif index < len(fields) and fields[index] == "5":
                index += 2
            continue
        out.append(field)
    if not out:
        return None
    return ";".join(out).encode("ascii")


class _CsiNormalizer:
    """Rewrite or drop the CSI forms pyte 0.8 mis-parses, across feeds.

    pyte ignores ``>`` and ``=`` leaders (so vim's ``CSI > 4 ; 2 m`` turns on
    underline), prints the rest of a CSI with ``=``, ``:`` or an
    intermediate byte as text, and raises ``TypeError`` for a ``?`` leader
    on most final bytes (``CSI ? 4 m``).
    """

    def __init__(self, on_reply: Optional[Callable[[bytes], None]] = None) -> None:
        self._held = b""
        self.on_reply = on_reply

    def reset(self) -> None:
        self._held = b""

    def filter(self, data: bytes) -> bytes:
        if self._held:
            data, self._held = self._held + data, b""
        tail = data.rfind(b"\x1b")
        if tail >= 0 and len(data) - tail <= _MAX_HELD and _OPEN_CSI.match(data, tail):
            data, self._held = data[:tail], data[tail:]
        if not _MAYBE_UNUSUAL.search(data):
            return data
        return _UNUSUAL_CSI.sub(self._rewrite, data)

    def _rewrite(self, match: "re.Match[bytes]") -> bytes:
        leader, params, inter, final = match.group("leader", "params", "inter", "final")
        if inter:
            return b""  # DECSCUSR, DECSTR, DECRQM, ...: not supported by pyte
        if leader == b"?":
            if b":" in params:
                return b""
            if final in b"hl":
                return match.group(0)
            if final in b"JK":
                return b"\x1b[" + params + final  # selective erase as plain erase
            return b""
        if leader:
            if leader == b">" and final == b"c" and params in (b"", b"0"):
                if self.on_reply is not None:
                    self.on_reply(_DA2_REPLY)
            return b""
        if final == b"m":
            sgr = _sgr_params(params)
            return b"" if sgr is None else b"\x1b[" + sgr + b"m"
        if b":" in params:
            return b""
        return match.group(0)


class TerminalModel(LineIndexMixin):
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
        self._normalizer = _CsiNormalizer(on_reply)

    @property
    def pull_history_on_grow(self) -> bool:
        return self.screen.pull_history_on_grow

    @pull_history_on_grow.setter
    def pull_history_on_grow(self, value: bool) -> None:
        self.screen.pull_history_on_grow = bool(value)

    # -- input -----------------------------------------------------------------

    def feed(self, data: bytes) -> None:
        data = self._normalizer.filter(bytes(data))
        if data:
            self.stream.feed(data)

    def resize(self, columns: int, lines: int) -> None:
        self.screen.resize(lines=lines, columns=columns)

    def reset(self, *, clear_scrollback: bool) -> None:
        self._normalizer.reset()
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

    def line(self, row: int) -> Line:
        history = self.history_length
        if row < history:
            return self.screen.scrollback[row]
        return self.screen.buffer[row - history]

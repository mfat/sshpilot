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
from collections import defaultdict, deque
from typing import Callable, Deque, Dict, Optional

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

    @property
    def pull_history_on_grow(self) -> bool:
        return self.screen.pull_history_on_grow

    @pull_history_on_grow.setter
    def pull_history_on_grow(self, value: bool) -> None:
        self.screen.pull_history_on_grow = bool(value)

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

    def line(self, row: int) -> Line:
        history = self.history_length
        if row < history:
            return self.screen.scrollback[row]
        return self.screen.buffer[row - history]

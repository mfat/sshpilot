"""Headless VT emulator model backed by libvterm (proof of concept).

A drop-in alternative to :class:`sshpilot.pyte_terminal_model.TerminalModel`
for the built-in GTK terminal view. libvterm (0.3.x, the C library Neovim and
Emacs' vterm use) does the parsing and keeps the screen and the alternate
screen; this module binds it with :mod:`ctypes` and adds what the view expects
on top:

* scrollback fed by libvterm's ``sb_pushline``/``sb_popline`` callbacks, kept
  as raw cell bytes and decoded only when a row is drawn or searched,
* replies to the remote (DA/DSR, ...) routed to a callback,
* a ``screen`` object with the attributes the view reads from pyte's screen
  (title, cursor, mode flags, ...),
* the same absolute-row text/selection/search helpers
  (:class:`~sshpilot.terminal_model_common.LineIndexMixin`).

Nothing here imports pyte or GTK. The shared library is located by
:func:`load_libvterm`; when it is missing, constructing a model raises
:class:`VtermUnavailable` (an ``ImportError``).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import re
import struct
import sys
from collections import deque, namedtuple
from typing import Callable, Deque, Dict, List, Optional

from .terminal_model_common import (  # noqa: F401  (re-exported for callers)
    MODE_APP_CURSOR,
    MODE_BRACKETED_PASTE,
    MODE_FOCUS_EVENTS,
    MODE_MOUSE_ANY_MOTION,
    MODE_MOUSE_BUTTON_MOTION,
    MODE_MOUSE_NORMAL,
    MODE_MOUSE_SGR,
    LineIndexMixin,
    encode_key,
    encode_mouse,
)

DEFAULT_SCROLLBACK_LINES = 10000

#: Same fields as ``pyte.screens.Char`` so the view draws either model's cells.
Char = namedtuple(
    "Char", "data fg bg bold italics underscore strikethrough reverse blink"
)
Line = Dict[int, Char]


class VtermUnavailable(ImportError):
    """libvterm could not be loaded."""


# ---------------------------------------------------------------------------
# C declarations (vterm.h, 0.3.x)


class _VTermPos(ctypes.Structure):
    _fields_ = [("row", ctypes.c_int), ("col", ctypes.c_int)]


class _VTermRect(ctypes.Structure):
    _fields_ = [
        ("start_row", ctypes.c_int), ("end_row", ctypes.c_int),
        ("start_col", ctypes.c_int), ("end_col", ctypes.c_int),
    ]


class _VTermStringFragment(ctypes.Structure):
    # ctypes lays bitfields out like the platform C compiler (MSVC-style on
    # Windows, which is what MinGW's default -mms-bitfields produces too).
    _fields_ = [
        ("str", ctypes.c_void_p),
        ("len", ctypes.c_size_t, 30),
        ("initial", ctypes.c_bool, 1),
        ("final", ctypes.c_bool, 1),
    ]


class _VTermValue(ctypes.Union):
    _fields_ = [
        ("boolean", ctypes.c_int),
        ("number", ctypes.c_int),
        ("string", _VTermStringFragment),
        ("color", ctypes.c_uint8 * 4),
    ]


class _VTermScreenCallbacks(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_void_p)
        for name in (
            "damage", "moverect", "movecursor", "settermprop", "bell",
            "resize", "sb_pushline", "sb_popline", "sb_clear",
        )
    ]


#: ``VTermScreenCell``: uint32 chars[6]; char width; attrs (one 32-bit
#: bitfield word); VTermColor fg, bg (4 bytes each).
_CELL = struct.Struct("<6IbxxxI4B4B")
CELL_SIZE = _CELL.size  # 40

_PROP_CURSORVISIBLE = 1
_PROP_ALTSCREEN = 3
_PROP_TITLE = 4
_PROP_MOUSE = 8
_PROP_FOCUSREPORT = 9
_MOUSE_PROPS = {0: None, 1: "click", 2: "drag", 3: "any"}

_COLOR_INDEXED = 0x01
_COLOR_DEFAULT_FG = 0x02
_COLOR_DEFAULT_BG = 0x04

_DAMAGE_SCROLL = 3  # VTermDamageSize: merge damage and scrolls until flushed

_KEY_UP = 5
_WHEEL_BUTTON = 4

_OUTPUT_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p)
_SETTERMPROP_CB = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.c_int, ctypes.POINTER(_VTermValue), ctypes.c_void_p
)
_USER_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)
_SB_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
_DAMAGE_CB = ctypes.CFUNCTYPE(ctypes.c_int, _VTermRect, ctypes.c_void_p)
_MOVERECT_CB = ctypes.CFUNCTYPE(ctypes.c_int, _VTermRect, _VTermRect, ctypes.c_void_p)

_LIB: Optional[ctypes.CDLL] = None
_LIB_ERROR: Optional[str] = None


def _candidate_paths() -> List[str]:
    """Places to look for the shared library, most specific first."""

    names = (
        ["libvterm-0.dll", "vterm.dll", "libvterm.dll"]
        if sys.platform == "win32"
        else ["libvterm.so.0", "libvterm.so", "libvterm.0.dylib", "libvterm.dylib"]
    )
    override = os.environ.get("SSHPILOT_LIBVTERM")
    paths: List[str] = [override] if override else []
    directories = []
    if sys.platform == "win32" and not getattr(sys, "frozen", False):
        # A source checkout: the patched build from packaging/windows/libvterm.
        directories.append(os.path.join(_source_checkout_root(), "vendor", "libvterm", _machine_arch()))
    if getattr(sys, "frozen", False):
        # PyInstaller one-dir/one-file: DLLs sit next to the exe or in _MEIPASS.
        directories.append(getattr(sys, "_MEIPASS", ""))
        directories.append(os.path.dirname(sys.executable))
    # MSYS2: python.exe and the mingw DLLs share <prefix>/bin.
    directories.append(os.path.dirname(os.path.realpath(sys.executable)))
    directories.append(os.path.join(sys.base_prefix, "bin"))
    for directory in directories:
        if directory:
            paths.extend(os.path.join(directory, name) for name in names)
    found = ctypes.util.find_library("vterm")
    if found:
        paths.append(found)
    paths.extend(names)  # last resort: the loader's own search path
    return paths


def _source_checkout_root() -> str:
    # src/sshpilot/vterm_terminal_model.py -> the checkout root
    return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))


def _machine_arch() -> str:
    machine = (os.environ.get("PROCESSOR_ARCHITEW6432")
               or os.environ.get("PROCESSOR_ARCHITECTURE") or "").upper()
    return {"ARM64": "arm64", "X86": "x86"}.get(machine, "x64")


def csi_args_bounded(lib: ctypes.CDLL) -> bool:
    """True for sshPilot's patched build, whose parser bounds CSI arguments."""

    marker = getattr(lib, "sshpilot_vterm_csi_args_bounded", None)
    if marker is None:
        return False
    marker.restype = ctypes.c_int
    marker.argtypes = []
    return marker() >= 16


def _abi_self_test(lib: ctypes.CDLL) -> Optional[str]:
    """Read back known cells; a reason string if the layout is not ours."""

    vt = lib.vterm_new(1, 4)
    if not vt:
        return "vterm_new failed"
    try:
        lib.vterm_set_utf8(vt, 1)
        screen = lib.vterm_obtain_screen(vt)
        lib.vterm_screen_reset(screen, 1)
        probe = "\x1b[1;3;4;7;9;38;2;10;20;30;48;5;196mA\x1b[0mé中".encode()
        lib.vterm_input_write(vt, probe, len(probe))
        # Room past one cell, so a larger struct cannot write out of bounds.
        buffer = ctypes.create_string_buffer(CELL_SIZE * 4)
        cells = []
        for column in range(3):
            if not lib.vterm_screen_get_cell(screen, _VTermPos(0, column), buffer):
                return f"vterm_screen_get_cell({column}) failed"
            try:
                cells.append(_decode_cell(buffer.raw[:CELL_SIZE]))
            except (ValueError, IndexError, struct.error) as exc:
                return f"cell layout differs: {exc}"
        first, second, third = cells
        expected = (
            first.data == "A" and first.bold and first.italics and first.underscore
            and first.reverse and first.strikethrough
            and first.fg == "0a141e" and first.bg == _INDEXED[196]
            and second.data == "é" and not second.bold and second.fg == "default"
            and third.data == "中"
        )
        if not expected:
            return f"cell layout differs: {cells!r}"
        return None
    finally:
        lib.vterm_free(vt)


def load_libvterm() -> ctypes.CDLL:
    """Load and declare libvterm once; raise :class:`VtermUnavailable`."""

    global _LIB, _LIB_ERROR
    if _LIB is not None:
        return _LIB
    if _LIB_ERROR is not None:
        raise VtermUnavailable(_LIB_ERROR)
    errors = []
    for path in _candidate_paths():
        if os.path.isabs(path) and not os.path.exists(path):
            continue
        try:
            lib = ctypes.CDLL(path)
        except OSError as exc:
            errors.append(f"{path}: {exc}")
            continue
        try:
            _declare(lib)
        except AttributeError as exc:  # not libvterm 0.3, or a stub
            errors.append(f"{path}: {exc}")
            continue
        problem = _abi_self_test(lib)
        if problem is not None:
            errors.append(f"{path}: {problem}")
            continue
        _LIB = lib
        return lib
    _LIB_ERROR = "libvterm not found (" + "; ".join(errors[-3:] or ["no candidates"]) + ")"
    raise VtermUnavailable(_LIB_ERROR)


def _declare(lib: ctypes.CDLL) -> None:
    vp, ci, sz = ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t
    signatures = {
        "vterm_new": (vp, [ci, ci]),
        "vterm_free": (None, [vp]),
        "vterm_set_utf8": (None, [vp, ci]),
        "vterm_set_size": (None, [vp, ci, ci]),
        "vterm_input_write": (sz, [vp, ctypes.c_char_p, sz]),
        "vterm_output_set_callback": (None, [vp, vp, vp]),
        "vterm_obtain_screen": (vp, [vp]),
        "vterm_obtain_state": (vp, [vp]),
        "vterm_screen_set_callbacks": (None, [vp, vp, vp]),
        "vterm_screen_enable_altscreen": (None, [vp, ci]),
        "vterm_screen_reset": (None, [vp, ci]),
        "vterm_screen_set_damage_merge": (None, [vp, ci]),
        "vterm_screen_flush_damage": (None, [vp]),
        "vterm_screen_get_cell": (ci, [vp, _VTermPos, vp]),
        "vterm_state_get_cursorpos": (None, [vp, ctypes.POINTER(_VTermPos)]),
        "vterm_keyboard_key": (None, [vp, ci, ci]),
        "vterm_keyboard_start_paste": (None, [vp]),
        "vterm_mouse_button": (None, [vp, ci, ctypes.c_bool, ci]),
    }
    for name, (restype, argtypes) in signatures.items():
        function = getattr(lib, name)
        function.restype = restype
        function.argtypes = argtypes


# ---------------------------------------------------------------------------
# Cell decoding


def _xterm_256() -> List[str]:
    levels = (0, 95, 135, 175, 215, 255)
    table = [""] * 16
    for index in range(216):
        r, g, b = levels[index // 36], levels[(index // 6) % 6], levels[index % 6]
        table.append(f"{r:02x}{g:02x}{b:02x}")
    for index in range(24):
        level = 8 + index * 10
        table.append(f"{level:02x}{level:02x}{level:02x}")
    return table


_NAMES = (
    "black", "red", "green", "brown", "blue", "magenta", "cyan", "white",
    "brightblack", "brightred", "brightgreen", "brightyellow",
    "brightblue", "brightmagenta", "brightcyan", "brightwhite",
)
_INDEXED = list(_NAMES) + _xterm_256()[16:]

# Blank cell libvterm-style: default colours, width 1.
_BLANK_CELL = _CELL.pack(0, 0, 0, 0, 0, 0, 1, 0, _COLOR_DEFAULT_FG, 240, 240, 240,
                         _COLOR_DEFAULT_BG, 0, 0, 0)


def _color(kind: int, a: int, b: int, c: int, default_flag: int) -> str:
    if kind & default_flag:
        return "default"
    if kind & _COLOR_INDEXED:
        return _INDEXED[a]
    return f"{a:02x}{b:02x}{c:02x}"


def _decode_cell(raw: bytes) -> Char:
    (c0, c1, c2, c3, c4, c5, _width, attrs,
     fk, fr, fgr, fb, bk, br, bgr, bb) = _CELL.unpack(raw)
    if c0 == 0:
        data = " "
    elif c0 == 0xFFFFFFFF:
        data = ""  # right half of a wide character, as pyte stores it
    else:
        chars = [c0]
        for extra in (c1, c2, c3, c4, c5):
            if not extra:
                break
            chars.append(extra)
        data = "".join(map(chr, chars))
    return Char(
        data,
        _color(fk, fr, fgr, fb, _COLOR_DEFAULT_FG),
        _color(bk, br, bgr, bb, _COLOR_DEFAULT_BG),
        bool(attrs & 0x01),          # bold
        bool(attrs & 0x08),          # italic
        bool(attrs & 0x06),          # underline (any style)
        bool(attrs & 0x80),          # strike
        bool(attrs & 0x20),          # reverse
        bool(attrs & 0x10),          # blink
    )


class _CellDecoder:
    """Decodes raw cell rows, memoising the (few) distinct cells seen."""

    _LIMIT = 8192

    def __init__(self) -> None:
        self._cells: Dict[bytes, Char] = {}

    def row(self, raw: bytes) -> Line:
        cells = self._cells
        if len(cells) > self._LIMIT:
            cells.clear()
        line: Line = {}
        for x, offset in enumerate(range(0, len(raw), CELL_SIZE)):
            key = raw[offset:offset + CELL_SIZE]
            char = cells.get(key)
            if char is None:
                char = cells[key] = _decode_cell(key)
            line[x] = char
        return line


# ---------------------------------------------------------------------------
# Screen facade


class _Cursor:
    __slots__ = ("x", "y", "hidden")

    def __init__(self, x: int, y: int, hidden: bool) -> None:
        self.x, self.y, self.hidden = x, y, hidden


class VtermScreen:
    """What the view reads from ``model.screen``, answered from libvterm."""

    def __init__(self, model: "VtermTerminalModel", scrollback_limit: int) -> None:
        self._model = model
        self.scrollback: Deque[bytes] = deque(maxlen=max(0, scrollback_limit))
        self.scrollback_pushes = 0
        self.title = ""
        self.title_changed = False
        self.bell_rung = False
        self.alternate_active = False
        self.mouse_tracking: Optional[str] = None
        self.focus_events = False
        self._cursor_visible = True

    @property
    def lines(self) -> int:
        return self._model.lines

    @property
    def columns(self) -> int:
        return self._model.columns

    @property
    def cursor(self) -> _Cursor:
        pos = _VTermPos()
        self._model._lib.vterm_state_get_cursorpos(self._model._state, ctypes.byref(pos))
        return _Cursor(pos.col, pos.row, not self._cursor_visible)

    @property
    def cursor_visible(self) -> bool:
        return self._cursor_visible

    # libvterm keeps these modes private; ask it to encode something and see
    # which form it chose.

    @property
    def app_cursor_keys(self) -> bool:
        model = self._model
        return model._probe(lambda: model._lib.vterm_keyboard_key(model._vt, _KEY_UP, 0)) == b"\x1bOA"

    @property
    def bracketed_paste(self) -> bool:
        model = self._model
        return bool(model._probe(lambda: model._lib.vterm_keyboard_start_paste(model._vt)))

    @property
    def mouse_sgr(self) -> bool:
        if self.mouse_tracking is None:
            return False
        model = self._model
        report = model._probe(
            lambda: model._lib.vterm_mouse_button(model._vt, _WHEEL_BUTTON, True, 0)
        )
        return report.startswith(b"\x1b[<")

    @property
    def mode(self) -> frozenset:
        """The pyte-style mode numbers the view tests with ``in``."""

        modes = set()
        if self.app_cursor_keys:
            modes.add(MODE_APP_CURSOR)
        if self.bracketed_paste:
            modes.add(MODE_BRACKETED_PASTE)
        if self.focus_events:
            modes.add(MODE_FOCUS_EVENTS)
        tracking = self.mouse_tracking
        if tracking is not None:
            modes.add({
                "click": MODE_MOUSE_NORMAL,
                "drag": MODE_MOUSE_BUTTON_MOTION,
                "any": MODE_MOUSE_ANY_MOTION,
            }[tracking])
            if self.mouse_sgr:
                modes.add(MODE_MOUSE_SGR)
        return frozenset(modes)


# ---------------------------------------------------------------------------
# Resizing inside the alternate screen

#: DECSET/DECRST of the alternate screen, in the single-parameter form
#: vim, less, htop, tmux and ncurses send.
_ALT_SWITCH = re.compile(rb"\x1b\[\?(1049|1047|47)[hl]")


# ---------------------------------------------------------------------------
# CSI argument overflow guard

#: libvterm 0.3.3 stores CSI arguments in ``long args[16]`` and bumps the
#: index on every ``;`` or ``:`` without a bound, so a 16th separator writes
#: past the array (a remotely triggerable crash). Neovim's fork raised the
#: array to 32 but kept the unbounded index.
_CSI_MAX_SEPARATORS = 15

#: Bytes libvterm keeps parsing as CSI parameters: parameter bytes, DEL, and
#: C0 controls other than CAN, SUB and ESC (which end or restart the CSI).
_CSI_BODY = rb"(?:[\x30-\x3f\x7f]|[\x00-\x17\x19\x1c-\x1f])*"
_CSI_RUN = re.compile(rb"\x1b\[(" + _CSI_BODY + rb")")
_CSI_CONTINUE = re.compile(_CSI_BODY)
_CSI_PARAM_BYTE = re.compile(rb"[\x30-\x3f]")
#: A CSI with more separators than libvterm can hold (the slow path's trigger).
_CSI_TOO_LONG = re.compile(
    rb"\x1b\[(?:[\x30-\x39\x3c-\x3f\x7f\x00-\x17\x19\x1c-\x1f]*[;:]){%d}"
    % (_CSI_MAX_SEPARATORS + 1)
)


class _CsiArgGuard:
    """Drop CSI parameters past libvterm's 16-argument array, across feeds."""

    def __init__(self) -> None:
        #: Separators already seen in a CSI left open by the previous feed.
        self._open: Optional[int] = None
        #: The previous feed ended with a bare ESC (a CSI may start next).
        self._esc = False

    def filter(self, data: bytes) -> bytes:
        if not data:
            return data
        out = []
        position = 0
        if self._esc:
            self._esc = False
            if data[:1] == b"[":
                out.append(b"[")
                position, self._open = 1, 0
        if self._open is not None:
            # Finish the CSI the previous feed left open.
            match = _CSI_CONTINUE.match(data, position)
            out.append(self._clip(match.group(0), self._open, match.end() == len(data)))
            position = match.end()
            if position == len(data):
                return b"".join(out)
        rest = data[position:] if position else data
        if not _CSI_TOO_LONG.search(rest):
            self._note_tail(rest)
            if not out:
                return rest
            out.append(rest)
            return b"".join(out)
        position = 0
        for match in _CSI_RUN.finditer(rest):
            out.append(rest[position:match.start(1)])
            out.append(self._clip(match.group(1), 0, match.end() == len(rest)))
            position = match.end()
        out.append(rest[position:])
        if position < len(rest):
            self._open = None
            self._esc = rest.endswith(b"\x1b")
        return b"".join(out)

    def _clip(self, body: bytes, seen: int, open_at_end: bool) -> bytes:
        separators = seen + body.count(b";") + body.count(b":")
        self._open = separators if open_at_end else None
        if separators <= _CSI_MAX_SEPARATORS:
            return body
        kept = []
        count = seen
        overflowed = count > _CSI_MAX_SEPARATORS
        for byte in body:
            char = bytes((byte,))
            if byte in b";:":
                count += 1
                if count > _CSI_MAX_SEPARATORS:
                    overflowed = True
            if overflowed and _CSI_PARAM_BYTE.match(char):
                continue
            kept.append(char)
        return b"".join(kept)

    def _note_tail(self, data: bytes) -> None:
        """Remember a CSI or ESC left open at the end of a fast-path feed."""
        self._open = None
        self._esc = data.endswith(b"\x1b")
        start = data.rfind(b"\x1b[")
        if start < 0:
            return
        match = _CSI_RUN.match(data, start)
        if match.end() == len(data):
            body = match.group(1)
            self._open = body.count(b";") + body.count(b":")


class _AltResize:
    """Works around libvterm 0.3.3 resizing the main screen while the
    alternate screen is shown.

    libvterm resizes the hidden main buffer correctly, but (1) the rows it
    pushes to the scrollback are read from the *active* buffer, so
    alternate-screen content (the pager, the editor) lands in the history
    instead of the main screen's top rows, and (2) the cursor saved by
    DECSET 1049 is not moved when rows are pushed out or pulled back, so the
    prompt comes back on the wrong row. This keeps a copy of the main screen
    taken just before the switch, hands out its rows in place of the wrong
    ones, and counts the net shift to correct the restored cursor.
    """

    def __init__(self, rows: List[bytes], cursor: Optional[_Cursor]) -> None:
        self.rows = deque(rows)
        self.cursor = (cursor.y, cursor.x) if cursor is not None else None
        self.shift = 0

    def pushed(self) -> bytes:
        self.shift -= 1
        return self.rows.popleft() if self.rows else _BLANK_CELL

    def popped(self, raw: bytes) -> None:
        self.shift += 1
        self.rows.appendleft(raw)

    def restored_cursor(self, lines: int, columns: int) -> Optional[bytes]:
        """CUP that puts the cursor where 1049 should have restored it."""

        if self.cursor is None or self.shift == 0:
            return None
        row = max(0, self.cursor[0] + self.shift)
        column = max(0, min(columns - 1, self.cursor[1]))
        # libvterm sizes the main screen around the *alternate* cursor, so
        # it may drop the (blank) saved-cursor row rather than push one more
        # line out. Scroll that line out now instead of clamping onto the
        # command line above it.
        overflow = max(0, row - (lines - 1))
        row -= overflow
        return f"\x1b[{row + 1};{column + 1}H".encode() + b"\n" * overflow


# ---------------------------------------------------------------------------
# Model


class VtermTerminalModel(LineIndexMixin):
    """libvterm screen + scrollback behind the ``TerminalModel`` interface.

    Rows are addressed absolutely: ``0`` is the oldest scrollback line and
    ``history_length + lines - 1`` is the bottom screen row.
    """

    def __init__(
        self,
        columns: int = 80,
        lines: int = 24,
        *,
        scrollback_limit: int = DEFAULT_SCROLLBACK_LINES,
        on_reply: Optional[Callable[[bytes], None]] = None,
    ) -> None:
        self._lib = lib = load_libvterm()
        self.on_reply = on_reply
        self._columns, self._lines = max(1, columns), max(1, lines)
        self._capture: Optional[bytearray] = None
        self._title_parts: List[bytes] = []
        self._decoder = _CellDecoder()
        self._screen_rows: Dict[int, Line] = {}
        self._history_rows: Dict[bytes, Line] = {}
        self._set_row_buffer()
        self._resizing = False
        self._alt: Optional[_AltResize] = None
        # sshPilot's patched build bounds CSI arguments itself.
        self._csi_guard = None if csi_args_bounded(self._lib) else _CsiArgGuard()
        #: See ``EmulatorScreen.pull_history_on_grow``. When off, libvterm is
        #: refused every history row and adds blank rows at the bottom.
        self.pull_history_on_grow = True
        self.screen = VtermScreen(self, scrollback_limit)

        self._vt = lib.vterm_new(self._lines, self._columns)
        if not self._vt:
            raise MemoryError("vterm_new failed")
        lib.vterm_set_utf8(self._vt, 1)
        self._state = lib.vterm_obtain_state(self._vt)
        self._screen = lib.vterm_obtain_screen(self._vt)

        # libvterm keeps these pointers; the model must keep them alive.
        self._output_cb = _OUTPUT_CB(self._on_output)
        self._callbacks_keep = (
            _DAMAGE_CB(self._on_damage),
            _MOVERECT_CB(self._on_moverect),
            _SETTERMPROP_CB(self._on_settermprop),
            _USER_CB(self._on_bell),
            _SB_CB(self._on_sb_pushline),
            _SB_CB(self._on_sb_popline),
            _USER_CB(self._on_sb_clear),
        )
        damage, moverect, settermprop, bell, pushline, popline, sb_clear = (
            ctypes.cast(cb, ctypes.c_void_p).value for cb in self._callbacks_keep
        )
        self._callbacks = _VTermScreenCallbacks(
            damage=damage, moverect=moverect, settermprop=settermprop, bell=bell,
            sb_pushline=pushline, sb_popline=popline, sb_clear=sb_clear,
        )
        lib.vterm_output_set_callback(
            self._vt, ctypes.cast(self._output_cb, ctypes.c_void_p), None
        )
        lib.vterm_screen_set_callbacks(self._screen, ctypes.byref(self._callbacks), None)
        lib.vterm_screen_set_damage_merge(self._screen, _DAMAGE_SCROLL)
        lib.vterm_screen_enable_altscreen(self._screen, 1)
        lib.vterm_screen_reset(self._screen, 1)
        # Screen rows are cached as decoded lines until libvterm reports them
        # damaged. Raw calls skip ctypes' per-argument conversion: a redraw
        # reads every cell of every uncached row.
        raw_lib = ctypes.CDLL(lib._name, handle=lib._handle)
        self._get_cell = raw_lib.vterm_screen_get_cell
        self._get_cell.restype = ctypes.c_int
        # sshPilot's patched build reads a whole row per call; stock builds
        # are read cell by cell.
        self._get_row = getattr(raw_lib, "sshpilot_vterm_screen_get_row", None)
        if self._get_row is not None:
            self._get_row.restype = ctypes.c_int
        self._screen_ptr = ctypes.c_void_p(self._screen)
        self._pos = _VTermPos()

    def close(self) -> None:
        vt, self._vt = getattr(self, "_vt", None), None
        if vt:
            self._lib.vterm_free(vt)

    def __del__(self) -> None:  # pragma: no cover - best effort
        try:
            self.close()
        except Exception:
            pass

    # -- libvterm callbacks ----------------------------------------------------

    def _on_damage(self, rect, _user) -> int:
        rows = self._screen_rows
        if rows:
            for y in range(rect.start_row, rect.end_row):
                rows.pop(y, None)
        return 1

    def _on_moverect(self, dest, src, _user) -> int:
        self._screen_rows.clear()
        return 1

    def _on_output(self, data, length, _user) -> None:
        chunk = ctypes.string_at(data, length)
        if self._capture is not None:
            self._capture += chunk
        elif self.on_reply is not None:
            self.on_reply(chunk)

    def _on_settermprop(self, prop, value, _user) -> int:
        screen = self.screen
        val = value.contents
        if prop == _PROP_TITLE:
            fragment = val.string
            if fragment.initial:
                self._title_parts = []
            if fragment.len:
                self._title_parts.append(ctypes.string_at(fragment.str, fragment.len))
            if fragment.final:
                screen.title = b"".join(self._title_parts).decode("utf-8", "replace")
                screen.title_changed = True
                self._title_parts = []
        elif prop == _PROP_CURSORVISIBLE:
            screen._cursor_visible = bool(val.boolean)
        elif prop == _PROP_ALTSCREEN:
            screen.alternate_active = bool(val.boolean)
        elif prop == _PROP_MOUSE:
            screen.mouse_tracking = _MOUSE_PROPS.get(val.number)
        elif prop == _PROP_FOCUSREPORT:
            screen.focus_events = bool(val.boolean)
        return 1

    def _on_bell(self, _user) -> int:
        self.screen.bell_rung = True
        return 1

    def _on_sb_pushline(self, cols, cells, _user) -> int:
        screen = self.screen
        alt = self._alt if self._resizing and screen.alternate_active else None
        if alt is not None:
            raw = alt.pushed()
        else:
            raw = ctypes.string_at(cells, cols * CELL_SIZE)
        if screen.scrollback.maxlen == 0:
            return 1
        screen.scrollback.append(raw)
        screen.scrollback_pushes += 1
        return 1

    def _on_sb_popline(self, cols, cells, _user) -> int:
        scrollback = self.screen.scrollback
        if not scrollback or not self.pull_history_on_grow:
            return 0
        raw = scrollback.pop()
        if self._resizing and self.screen.alternate_active and self._alt is not None:
            self._alt.popped(raw)
        wanted = cols * CELL_SIZE
        if len(raw) < wanted:
            raw += _BLANK_CELL * ((wanted - len(raw)) // CELL_SIZE)
        ctypes.memmove(cells, raw, wanted)
        return 1

    def _on_sb_clear(self, _user) -> int:
        self.screen.scrollback.clear()
        self._history_rows.clear()
        return 1

    def _probe(self, action: Callable[[], None]) -> bytes:
        """Run ``action`` and return what libvterm wrote instead of sending it."""

        self._capture = bytearray()
        try:
            action()
            return bytes(self._capture)
        finally:
            self._capture = None

    # -- input -----------------------------------------------------------------

    def feed(self, data: bytes) -> None:
        data = bytes(data)
        if self._csi_guard is not None:
            data = self._csi_guard.filter(data)
        if not data:
            return
        write = self._lib.vterm_input_write
        if b"\x1b[?" not in data:
            write(self._vt, data, len(data))
            self._lib.vterm_screen_flush_damage(self._screen)
            return
        # Alternate-screen switches are fed on their own so the main screen
        # can be snapshotted first (see _AltResize).
        position = 0
        for match in _ALT_SWITCH.finditer(data):
            if match.start() > position:
                chunk = data[position:match.start()]
                write(self._vt, chunk, len(chunk))
            sequence = match.group(0)
            entering = sequence.endswith(b"h")
            if entering and not self.screen.alternate_active:
                self._alt = _AltResize(
                    [self._screen_row_raw(y) for y in range(self._lines)],
                    self.screen.cursor if match.group(1) == b"1049" else None,
                )
            write(self._vt, sequence, len(sequence))
            if not entering and self._alt is not None and not self.screen.alternate_active:
                alt, self._alt = self._alt, None
                fix = alt.restored_cursor(self._lines, self._columns)
                if fix is not None:
                    write(self._vt, fix, len(fix))
            position = match.end()
        if position < len(data):
            rest = data[position:]
            write(self._vt, rest, len(rest))
        self._lib.vterm_screen_flush_damage(self._screen)

    def resize(self, columns: int, lines: int) -> None:
        columns, lines = max(1, columns), max(1, lines)
        if (columns, lines) == (self._columns, self._lines):
            return
        self._columns, self._lines = columns, lines
        self._set_row_buffer()
        self._resizing = True
        try:
            self._lib.vterm_set_size(self._vt, lines, columns)
        finally:
            self._resizing = False
        self._screen_rows.clear()

    def reset(self, *, clear_scrollback: bool) -> None:
        self._lib.vterm_screen_reset(self._screen, 1)
        self._alt = None
        if clear_scrollback:
            self.screen.scrollback.clear()
            self._history_rows.clear()
        self._screen_rows.clear()

    # -- geometry --------------------------------------------------------------

    @property
    def columns(self) -> int:
        return self._columns

    @property
    def lines(self) -> int:
        return self._lines

    @property
    def history_length(self) -> int:
        return len(self.screen.scrollback)

    def line(self, row: int) -> Line:
        history = self.history_length
        if row < history:
            raw = self.screen.scrollback[row]
            cached = self._history_rows.get(raw)
            if cached is None:
                if len(self._history_rows) > 1024:
                    self._history_rows.clear()
                cached = self._history_rows[raw] = self._decoder.row(raw)
            return cached
        y = row - history
        cached = self._screen_rows.get(y)
        if cached is None:
            cached = self._screen_rows[y] = self._decoder.row(self._screen_row_raw(y))
        return cached

    def _set_row_buffer(self) -> None:
        self._row_buffer = buffer = ctypes.create_string_buffer(CELL_SIZE * self._columns)
        base = ctypes.addressof(buffer)
        self._cell_slots = [
            ctypes.c_void_p(base + x * CELL_SIZE) for x in range(self._columns)
        ]

    def _screen_row_raw(self, y: int) -> bytes:
        if not (0 <= y < self._lines):
            return b""
        if self._get_row is not None:
            # Zeroes the buffer itself, like the per-cell path below.
            self._get_row(self._screen_ptr, y, self._row_buffer, self._columns)
            return self._row_buffer.raw
        return self._screen_row_raw_per_cell(y)

    def _screen_row_raw_per_cell(self, y: int) -> bytes:
        buffer = self._row_buffer
        # Zeroed so unused trailing chars[] slots compare equal in the cache.
        ctypes.memset(buffer, 0, len(buffer))
        get_cell, screen, pos = self._get_cell, self._screen_ptr, self._pos
        pos.row = y
        for x, slot in enumerate(self._cell_slots):
            pos.col = x
            get_cell(screen, pos, slot)
        return buffer.raw

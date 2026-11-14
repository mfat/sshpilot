"""Headless VT emulator model backed by libghostty-vt.

A drop-in alternative to :class:`sshpilot.vterm_terminal_model.VtermTerminalModel`
for the built-in GTK terminal view. libghostty-vt (the terminal core of
Ghostty, MIT, stable C ABI since 1.0) parses the stream and keeps the
screens and the scrollback; this module binds it with :mod:`ctypes` and
answers what the view reads:

* rows as ``{column: Char}`` lines, read through libghostty's render state
  one row per call (``ROW_DATA_CELLS_RAW``) and decoded with the cell bit
  layout the loaded library publishes in ``ghostty_type_json``,
* scrollback rows, reached by moving libghostty's viewport,
* replies to the remote (DA/DSR, ...), the title and the bell through
  libghostty's effect callbacks,
* a ``screen`` object with the attributes the view reads from pyte's screen.

Keys and mouse reports are encoded by the view
(``terminal_model_common.encode_key``), not by libghostty's encoders, on
purpose: on Windows every session runs under ConPTY, which passes a
program's kitty keyboard request (``CSI > 1 u``) through to us but cannot
parse kitty-encoded keys coming back (``CSI 99;5u`` reached cmd as literal
text instead of Ctrl+C). The legacy encoding is what conhost understands.

OSC 8 link targets survive ConPTY and are answered by :meth:`hyperlink_uri`.

Nothing here imports pyte or GTK. The shared library is located by
:func:`load_libghostty`; when it is missing, constructing a model raises
:class:`GhosttyUnavailable` (an ``ImportError``).
"""

from __future__ import annotations

import array
import ctypes
import ctypes.util
import json
import os
import sys
from collections import namedtuple
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

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

#: Same fields as ``pyte.screens.Char`` so the view draws any model's cells.
Char = namedtuple(
    "Char", "data fg bg bold italics underscore strikethrough reverse blink"
)
Line = Dict[int, Char]


class GhosttyUnavailable(ImportError):
    """libghostty-vt could not be loaded."""


# ---------------------------------------------------------------------------
# C declarations (ghostty/vt.h, 1.x)

_SUCCESS = 0
_OUT_OF_SPACE = -3
_NO_VALUE = -4

# GhosttyTerminalOption
_OPT_USERDATA = 1
_OPT_WRITE_PTY = 2
_OPT_BELL = 3
_OPT_TITLE_CHANGED = 6
_OPT_DEVICE_ATTRIBUTES = 9
_OPT_SCROLLBACK_MAX_BYTES = 28
_OPT_SCROLLBACK_MAX_LINES = 29
_OPT_RESIZE_PULL_SCROLLBACK = 41

# GhosttyTerminalData
_DATA_CURSOR_X = 3
_DATA_CURSOR_Y = 4
_DATA_ACTIVE_SCREEN = 6
_DATA_CURSOR_VISIBLE = 7
_DATA_MOUSE_TRACKING = 11
_DATA_TITLE = 12
_DATA_SCROLLBACK_ROWS = 15
_DATA_MODE = 37

_SCREEN_PRIMARY = 0

# GhosttyTerminalScrollViewportTag
_VIEWPORT_BOTTOM = 1
_VIEWPORT_ROW = 3

# GhosttyPointTag
_POINT_ACTIVE = 0
_POINT_SCREEN = 2

# GhosttyRenderStateData / RowData / RowCellsData
_RS_DATA_ROW_ITERATOR = 4
_RS_ROW_DIRTY = 1
_RS_ROW_CELLS = 3
_RS_ROW_CELLS_RAW = 5
_RS_ROW_ID = 7
_RS_CELLS_STYLE = 2
_RS_CELLS_GRAPHEMES_BUF = 4

def _dec_mode(number: int) -> int:
    """``ghostty_mode_new(number, ansi=false)``: a DEC private mode."""
    return number & 0x7FFF


#: Mouse tracking modes, most reporting first (``DATA_MOUSE_TRACKING`` is
#: only a flag), in the view's names.
_MOUSE_TRACKING_MODES = (
    (_dec_mode(1003), "any"),
    (_dec_mode(1002), "drag"),
    (_dec_mode(1000), "click"),
    (_dec_mode(9), "click"),
)

_MODE_APP_CURSOR = _dec_mode(1)
_MODE_FOCUS = _dec_mode(1004)
_MODE_SGR_MOUSE = _dec_mode(1006)
_MODE_BRACKETED_PASTE = _dec_mode(2004)


class _PointCoordinate(ctypes.Structure):
    _fields_ = [("x", ctypes.c_uint16), ("y", ctypes.c_uint32)]


class _PointValue(ctypes.Union):
    _fields_ = [("coordinate", _PointCoordinate), ("_padding", ctypes.c_uint64 * 2)]


class _Point(ctypes.Structure):
    _fields_ = [("tag", ctypes.c_int32), ("value", _PointValue)]


class _ScrollValue(ctypes.Union):
    _fields_ = [("delta", ctypes.c_ssize_t), ("row", ctypes.c_size_t),
                ("_padding", ctypes.c_uint64 * 2)]


class _ScrollViewport(ctypes.Structure):
    _fields_ = [("tag", ctypes.c_int32), ("value", _ScrollValue)]


class _String(ctypes.Structure):
    _fields_ = [("ptr", ctypes.c_void_p), ("len", ctypes.c_size_t)]


class _ModeConfig(ctypes.Structure):
    _fields_ = [("mode", ctypes.c_uint16), ("value", ctypes.c_bool)]


class _GridRef(ctypes.Structure):
    _fields_ = [("node", ctypes.c_void_p), ("x", ctypes.c_uint16), ("y", ctypes.c_uint16)]


class _CellsView(ctypes.Structure):
    _fields_ = [("ptr", ctypes.c_void_p), ("len", ctypes.c_size_t)]


class _CodepointBuffer(ctypes.Structure):
    _fields_ = [("ptr", ctypes.POINTER(ctypes.c_uint32)), ("cap", ctypes.c_size_t),
                ("len", ctypes.c_size_t)]


_WRITE_PTY_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t)
_PLAIN_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)
_DEVICE_ATTRIBUTES_CB = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)

#: What a full reset (RIS) does, short of clearing the history: leave the
#: alternate screen, soft-reset (DECSTR: SGR, margins, origin, insert, ...),
#: turn off the input and mouse modes programs set, show the cursor, clear.
_RESET_KEEPING_HISTORY = (
    b"\x1b[?1049l\x1b[!p"
    b"\x1b[?1;9;1000;1002;1003;1004;1005;1006;1015;1016;2004l"
    b"\x1b[?25h\x1b[H\x1b[2J"
)

_STYLE_SIZE = 16
_ROW_ID_SIZE = 16


class _CellLayout(NamedTuple):
    """Bit positions of a packed ``GhosttyCell``, from the library's manifest."""

    tag_shift: int
    tag_mask: int
    codepoint_shift: int
    codepoint_mask: int
    palette_shift: int
    rgb_shift: int
    style_shift: int
    style_mask: int
    wide_shift: int
    wide_mask: int
    tag_codepoint: int
    tag_grapheme: int
    tag_bg_palette: int
    tag_bg_rgb: int
    wide_tail: int


class _StyleLayout(NamedTuple):
    fg: int
    bg: int
    flags: int
    color_none: int
    color_palette: int
    color_rgb: int
    bold: int
    italic: int
    blink: int
    inverse: int
    strike: int
    underline_shift: int
    underline_mask: int


_LIB: Optional[ctypes.CDLL] = None
_LIB_ERROR: Optional[str] = None
_LAYOUT: Optional[Tuple[_CellLayout, _StyleLayout]] = None


def _library_names() -> List[str]:
    if sys.platform == "win32":
        return ["ghostty-vt.dll", "libghostty-vt.dll"]
    if sys.platform == "darwin":
        return ["libghostty-vt.1.dylib", "libghostty-vt.dylib"]
    return ["libghostty-vt.so.1", "libghostty-vt.so"]


def _candidate_paths() -> List[str]:
    """Places to look for the shared library, most specific first."""

    names = _library_names()
    override = os.environ.get("SSHPILOT_LIBGHOSTTY_VT")
    paths: List[str] = [override] if override else []
    directories = []
    if not getattr(sys, "frozen", False):
        # A source checkout: the build from packaging/windows/libghostty-vt.
        directories.append(
            os.path.join(_source_checkout_root(), "vendor", "libghostty-vt", _machine_arch())
        )
    if getattr(sys, "frozen", False):
        # PyInstaller one-dir/one-file: DLLs sit next to the exe or in _MEIPASS.
        directories.append(getattr(sys, "_MEIPASS", ""))
        directories.append(os.path.dirname(sys.executable))
    for directory in directories:
        if directory:
            paths.extend(os.path.join(directory, name) for name in names)
    found = ctypes.util.find_library("ghostty-vt")
    if found:
        paths.append(found)
    paths.extend(names)  # last resort: the loader's own search path
    return paths


def _source_checkout_root() -> str:
    # src/sshpilot/ghostty_terminal_model.py -> the checkout root
    return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))


def _machine_arch() -> str:
    if sys.platform == "win32":
        machine = (os.environ.get("PROCESSOR_ARCHITEW6432")
                   or os.environ.get("PROCESSOR_ARCHITECTURE") or "").upper()
        return {"ARM64": "arm64", "X86": "x86"}.get(machine, "x64")
    import platform

    machine = platform.machine().lower()
    return {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64"}.get(machine, machine)


def _declare(lib: ctypes.CDLL) -> None:
    vp, sz, i32, u16 = ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int32, ctypes.c_uint16
    signatures = {
        "ghostty_version": (ctypes.c_char_p, []),
        "ghostty_type_json": (ctypes.c_char_p, []),
        "ghostty_terminal_new": (i32, [vp, ctypes.POINTER(vp), u16, u16]),
        "ghostty_terminal_free": (None, [vp]),
        "ghostty_terminal_reset": (None, [vp]),
        "ghostty_terminal_resize": (i32, [vp, u16, u16, ctypes.c_uint32, ctypes.c_uint32]),
        "ghostty_terminal_vt_write": (None, [vp, ctypes.c_char_p, sz]),
        "ghostty_terminal_set": (i32, [vp, i32, vp]),
        "ghostty_terminal_get": (i32, [vp, i32, vp]),
        "ghostty_terminal_scroll_viewport": (i32, [vp, _ScrollViewport]),
        "ghostty_terminal_grid_ref": (i32, [vp, _Point, ctypes.POINTER(_GridRef)]),
        "ghostty_grid_ref_hyperlink_uri": (i32, [ctypes.POINTER(_GridRef), vp, sz, ctypes.POINTER(sz)]),
        "ghostty_terminal_grid_ref_track": (i32, [vp, _Point, ctypes.POINTER(vp)]),
        "ghostty_tracked_grid_ref_free": (None, [vp]),
        "ghostty_tracked_grid_ref_has_value": (ctypes.c_bool, [vp]),
        "ghostty_tracked_grid_ref_point": (i32, [vp, i32, ctypes.POINTER(_PointCoordinate)]),
        "ghostty_tracked_grid_ref_set": (i32, [vp, vp, _Point]),
        "ghostty_render_state_new": (i32, [vp, ctypes.POINTER(vp)]),
        "ghostty_render_state_free": (None, [vp]),
        "ghostty_render_state_update": (i32, [vp, vp]),
        "ghostty_render_state_clean": (i32, [vp]),
        "ghostty_render_state_get": (i32, [vp, i32, vp]),
        "ghostty_render_state_row_iterator_new": (i32, [vp, ctypes.POINTER(vp)]),
        "ghostty_render_state_row_iterator_free": (None, [vp]),
        "ghostty_render_state_row_iterator_next": (ctypes.c_bool, [vp]),
        "ghostty_render_state_row_get": (i32, [vp, i32, vp]),
        "ghostty_render_state_row_cells_new": (i32, [vp, ctypes.POINTER(vp)]),
        "ghostty_render_state_row_cells_free": (None, [vp]),
        "ghostty_render_state_row_cells_select": (i32, [vp, u16]),
        "ghostty_render_state_row_cells_get": (i32, [vp, i32, vp]),
    }
    for name, (restype, argtypes) in signatures.items():
        function = getattr(lib, name)
        function.restype = restype
        function.argtypes = argtypes


def _bits(entry: dict, name: str) -> Tuple[int, int]:
    field = entry["bits"][name]
    return field["lsb"], (1 << field["width"]) - 1


def _parse_layout(manifest: dict) -> Tuple[_CellLayout, _StyleLayout]:
    """Read the packed cell and style layouts; ``KeyError``/``ValueError`` if
    the library describes something this module does not understand."""

    if manifest.get("schema") != 1:
        raise ValueError(f"unknown type manifest schema {manifest.get('schema')!r}")
    types = manifest["types"]
    cell = types["GhosttyCell"]
    if cell.get("size") != 8 or cell.get("kind") != "packed":
        raise ValueError("GhosttyCell is not a packed 64-bit value")
    tag_shift, tag_mask = _bits(cell, "content_tag")
    content = cell["bits"]["content"]
    arms = content["arms"]
    cp_shift, cp_mask = _bits(arms["CODEPOINT"], "codepoint")
    pal_shift, _ = _bits(arms["BG_COLOR_PALETTE"], "index")
    r_shift, _ = _bits(arms["BG_COLOR_RGB"], "r")
    g_shift, _ = _bits(arms["BG_COLOR_RGB"], "g")
    b_shift, _ = _bits(arms["BG_COLOR_RGB"], "b")
    if (g_shift, b_shift) != (r_shift + 8, r_shift + 16):
        raise ValueError("unexpected RGB background layout")
    style_shift, style_mask = _bits(cell, "style_id")
    wide_shift, wide_mask = _bits(cell, "wide")
    tags = types["GhosttyCellContentTag"]["values"]
    wides = types["GhosttyCellWide"]["values"]
    cell_layout = _CellLayout(
        tag_shift, tag_mask,
        content["lsb"] + cp_shift, cp_mask,
        content["lsb"] + pal_shift,
        content["lsb"] + r_shift,
        style_shift, style_mask,
        wide_shift, wide_mask,
        tags["CODEPOINT"], tags["CODEPOINT_GRAPHEME"],
        tags["BG_COLOR_PALETTE"], tags["BG_COLOR_RGB"],
        wides["SPACER_TAIL"],
    )

    style = types["GhosttyStyle"]
    if style.get("size") != _STYLE_SIZE:
        raise ValueError("unexpected GhosttyStyle size")
    fields = style["fields"]
    color = types["GhosttyStyleColor"]["fields"]
    if color["tag"]["size"] != 1 or color["value"]["offset"] != 1:
        raise ValueError("unexpected GhosttyStyleColor layout")
    color_tags = types["GhosttyStyleColorTag"]["values"]
    flags = types["GhosttyStyleFlags"]
    if fields["flags"]["size"] != 2:
        raise ValueError("unexpected GhosttyStyleFlags size")
    ul_shift, ul_mask = _bits(flags, "underline")
    style_layout = _StyleLayout(
        fields["fg_color"]["offset"], fields["bg_color"]["offset"], fields["flags"]["offset"],
        color_tags["NONE"], color_tags["PALETTE"], color_tags["RGB"],
        1 << _bits(flags, "bold")[0], 1 << _bits(flags, "italic")[0],
        1 << _bits(flags, "blink")[0], 1 << _bits(flags, "inverse")[0],
        1 << _bits(flags, "strikethrough")[0],
        ul_shift, ul_mask,
    )
    return cell_layout, style_layout


def load_libghostty() -> ctypes.CDLL:
    """Load and declare libghostty-vt once; raise :class:`GhosttyUnavailable`."""

    global _LIB, _LIB_ERROR, _LAYOUT
    if _LIB is not None:
        return _LIB
    if _LIB_ERROR is not None:
        raise GhosttyUnavailable(_LIB_ERROR)
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
        except AttributeError as exc:  # not libghostty-vt 1.x
            errors.append(f"{path}: {exc}")
            continue
        version = (lib.ghostty_version() or b"").decode("ascii", "replace")
        if version.split(".")[0] != "1":
            errors.append(f"{path}: unsupported libghostty-vt {version}")
            continue
        try:
            layout = _parse_layout(json.loads(lib.ghostty_type_json()))
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"{path}: cell layout not understood ({exc})")
            continue
        _LIB, _LAYOUT = lib, layout
        return lib
    _LIB_ERROR = "libghostty-vt not found (" + "; ".join(errors[-3:] or ["no candidates"]) + ")"
    raise GhosttyUnavailable(_LIB_ERROR)


def library_version() -> Optional[str]:
    """The loaded library's version, or ``None`` when it cannot be loaded."""

    try:
        return load_libghostty().ghostty_version().decode("ascii", "replace")
    except GhosttyUnavailable:
        return None


# ---------------------------------------------------------------------------
# Colours (the names and table the view and the libvterm model use)


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

_BLANK = Char(" ", "default", "default", False, False, False, False, False, False)

#: (fg, bg, bold, italics, underscore, strikethrough, reverse, blink)
_DEFAULT_STYLE = ("default", "default", False, False, False, False, False, False)


def _style_color(raw: bytes, offset: int, layout: _StyleLayout) -> str:
    tag = raw[offset]
    if tag == layout.color_palette:
        return _INDEXED[raw[offset + 1]]
    if tag == layout.color_rgb:
        return raw[offset + 1:offset + 4].hex()
    return "default"


def _decode_style(raw: bytes, layout: _StyleLayout) -> tuple:
    flags = int.from_bytes(raw[layout.flags:layout.flags + 2], "little")
    return (
        _style_color(raw, layout.fg, layout),
        _style_color(raw, layout.bg, layout),
        bool(flags & layout.bold),
        bool(flags & layout.italic),
        bool((flags >> layout.underline_shift) & layout.underline_mask),
        bool(flags & layout.strike),
        bool(flags & layout.inverse),
        bool(flags & layout.blink),
    )


# ---------------------------------------------------------------------------
# Screen facade


class _Cursor:
    __slots__ = ("x", "y", "hidden")

    def __init__(self, x: int, y: int, hidden: bool) -> None:
        self.x, self.y, self.hidden = x, y, hidden


class _Scrollback:
    """Stands in for the ``deque`` the view expects at ``screen.scrollback``.

    libghostty keeps the history itself; this answers its length and limit.
    The view replaces it with ``deque(old, maxlen=n)`` to change the limit,
    which :class:`GhosttyScreen` turns into a libghostty setting.
    """

    def __init__(self, model: "GhosttyTerminalModel", maxlen: int) -> None:
        self._model = model
        self.maxlen = maxlen

    def __len__(self) -> int:
        return self._model.history_length

    def __bool__(self) -> bool:
        return len(self) > 0

    def __iter__(self):  # the view only copies it to build a new deque
        return iter(())

    def clear(self) -> None:
        self._model.feed(b"\x1b[3J")


class GhosttyScreen:
    """What the view reads from ``model.screen``, answered from libghostty."""

    def __init__(self, model: "GhosttyTerminalModel", scrollback_limit: int) -> None:
        self._model = model
        self._scrollback = _Scrollback(model, max(0, scrollback_limit))
        self.scrollback_pushes = 0
        self.title = ""
        self.title_changed = False
        self.bell_rung = False

    @property
    def scrollback(self) -> _Scrollback:
        return self._scrollback

    @scrollback.setter
    def scrollback(self, value) -> None:
        limit = getattr(value, "maxlen", None)
        limit = DEFAULT_SCROLLBACK_LINES if limit is None else max(0, int(limit))
        self._scrollback.maxlen = limit
        self._model._apply_scrollback_limit(limit)

    @property
    def lines(self) -> int:
        return self._model.lines

    @property
    def columns(self) -> int:
        return self._model.columns

    @property
    def cursor(self) -> _Cursor:
        model = self._model
        return _Cursor(
            model._get_u16(_DATA_CURSOR_X),
            model._get_u16(_DATA_CURSOR_Y),
            not self.cursor_visible,
        )

    @property
    def cursor_visible(self) -> bool:
        return self._model._get_bool(_DATA_CURSOR_VISIBLE)

    @property
    def alternate_active(self) -> bool:
        return self._model._get_i32(_DATA_ACTIVE_SCREEN) != _SCREEN_PRIMARY

    @property
    def mouse_tracking(self) -> Optional[str]:
        model = self._model
        if not model._get_bool(_DATA_MOUSE_TRACKING):
            return None
        for mode, name in _MOUSE_TRACKING_MODES:
            if model._mode(mode):
                return name
        return "click"

    @property
    def focus_events(self) -> bool:
        return self._model._mode(_MODE_FOCUS)

    @property
    def app_cursor_keys(self) -> bool:
        return self._model._mode(_MODE_APP_CURSOR)

    @property
    def bracketed_paste(self) -> bool:
        return self._model._mode(_MODE_BRACKETED_PASTE)

    @property
    def mouse_sgr(self) -> bool:
        return self.mouse_tracking is not None and self._model._mode(_MODE_SGR_MOUSE)

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
            if self._model._mode(_MODE_SGR_MOUSE):
                modes.add(MODE_MOUSE_SGR)
        return frozenset(modes)


# ---------------------------------------------------------------------------
# Model


class _CachedRow:
    __slots__ = ("line", "final")

    def __init__(self, line: Line, final: bool) -> None:
        self.line = line
        #: Decoded while in the scrollback, which programs cannot rewrite.
        self.final = final


class GhosttyTerminalModel(LineIndexMixin):
    """libghostty-vt terminal behind the ``TerminalModel`` interface.

    Rows are addressed absolutely: ``0`` is the oldest scrollback line and
    ``history_length + lines - 1`` is the bottom screen row.
    """

    #: Decoded rows kept across reads, keyed by libghostty's row id.
    _ROW_CACHE_LIMIT = 20000

    def __init__(
        self,
        columns: int = 80,
        lines: int = 24,
        *,
        scrollback_limit: int = DEFAULT_SCROLLBACK_LINES,
        on_reply: Optional[Callable[[bytes], None]] = None,
    ) -> None:
        self._lib = lib = load_libghostty()
        assert _LAYOUT is not None  # set together with _LIB
        self._cell_layout, self._style_layout = _LAYOUT
        self.on_reply = on_reply
        self._columns, self._lines = max(1, columns), max(1, lines)
        self._pull_history_on_grow = True
        self._generation = 0
        self._window: List[Line] = []
        self._window_top = 0
        self._window_generation = -1
        self._history_cache: Tuple[int, int] = (-1, 0)
        self._row_cache: Dict[bytes, _CachedRow] = {}
        self._row_id = ctypes.create_string_buffer(_ROW_ID_SIZE)
        self._dirty = ctypes.c_bool()
        self._cells_view = _CellsView()
        self._style_buffer = ctypes.create_string_buffer(_STYLE_SIZE)
        self._grapheme_storage = (ctypes.c_uint32 * 32)()
        self._u16 = ctypes.c_uint16()
        self._i32 = ctypes.c_int32()
        self._bool = ctypes.c_bool()
        self._size = ctypes.c_size_t()
        self._mode_config = _ModeConfig()
        #: Resolved style -> {cell content bits: Char}, see _decode_row.
        self._styled_cells: Dict[tuple, Dict[int, Char]] = {}
        self._decoded_styles: Dict[bytes, tuple] = {}
        layout = self._cell_layout
        self._content_mask = ~(layout.style_mask << layout.style_shift) & 0xFFFFFFFFFFFFFFFF
        self._terminal = None
        self._render_state = None
        # Handles libghostty fills in place (row iterator, row cells).
        self._rows = ctypes.c_void_p()
        self._cells = ctypes.c_void_p()
        self._anchor = None

        terminal = ctypes.c_void_p()
        if lib.ghostty_terminal_new(None, ctypes.byref(terminal), self._columns, self._lines) != _SUCCESS:
            raise MemoryError("ghostty_terminal_new failed")
        self._terminal = terminal.value
        handle = ctypes.c_void_p()
        if lib.ghostty_render_state_new(None, ctypes.byref(handle)) != _SUCCESS:
            self.close()
            raise MemoryError("ghostty_render_state_new failed")
        self._render_state = handle.value
        lib.ghostty_render_state_row_iterator_new(None, ctypes.byref(self._rows))
        lib.ghostty_render_state_row_cells_new(None, ctypes.byref(self._cells))

        # Callbacks run inside ghostty_terminal_vt_write; libghostty keeps the
        # pointers, so the model keeps the ctypes objects alive.
        self._callbacks = (
            _WRITE_PTY_CB(self._on_write_pty),
            _PLAIN_CB(self._on_bell),
            _PLAIN_CB(self._on_title_changed),
            _DEVICE_ATTRIBUTES_CB(lambda _t, _u, _out: False),  # library defaults
        )
        for option, callback in zip(
            (_OPT_WRITE_PTY, _OPT_BELL, _OPT_TITLE_CHANGED, _OPT_DEVICE_ATTRIBUTES),
            self._callbacks,
        ):
            lib.ghostty_terminal_set(self._terminal, option, ctypes.cast(callback, ctypes.c_void_p))

        self.screen = GhosttyScreen(self, scrollback_limit)
        self._apply_scrollback_limit(self.screen.scrollback.maxlen)

        # Counts rows pushed into the scrollback (screen.scrollback_pushes):
        # a reference that follows the top row of the active area.
        anchor = ctypes.c_void_p()
        if lib.ghostty_terminal_grid_ref_track(self._terminal, _active_top(), ctypes.byref(anchor)) == _SUCCESS:
            self._anchor = anchor.value

    def close(self) -> None:
        lib = self._lib
        anchor, self._anchor = getattr(self, "_anchor", None), None
        if anchor:
            lib.ghostty_tracked_grid_ref_free(anchor)
        cells = getattr(self, "_cells", None)
        if cells is not None and cells.value:
            lib.ghostty_render_state_row_cells_free(cells)
            cells.value = None
        rows = getattr(self, "_rows", None)
        if rows is not None and rows.value:
            lib.ghostty_render_state_row_iterator_free(rows)
            rows.value = None
        state, self._render_state = getattr(self, "_render_state", None), None
        if state:
            lib.ghostty_render_state_free(state)
        terminal, self._terminal = getattr(self, "_terminal", None), None
        if terminal:
            lib.ghostty_terminal_free(terminal)

    def __del__(self) -> None:  # pragma: no cover - best effort
        try:
            self.close()
        except Exception:
            pass

    # -- libghostty callbacks ----------------------------------------------------

    def _on_write_pty(self, _terminal, _userdata, data, length) -> None:
        try:
            if self.on_reply is not None and length:
                self.on_reply(ctypes.string_at(data, length))
        except Exception:  # noqa: BLE001 - never unwind through C
            pass

    def _on_bell(self, _terminal, _userdata) -> None:
        self.screen.bell_rung = True

    def _on_title_changed(self, _terminal, _userdata) -> None:
        try:
            title = _String()
            if self._lib.ghostty_terminal_get(self._terminal, _DATA_TITLE, ctypes.byref(title)) == _SUCCESS:
                raw = ctypes.string_at(title.ptr, title.len) if title.ptr and title.len else b""
                self.screen.title = raw.decode("utf-8", "replace")
                self.screen.title_changed = True
        except Exception:  # noqa: BLE001 - never unwind through C
            pass

    # -- terminal queries ----------------------------------------------------------

    def _get_u16(self, key: int) -> int:
        self._lib.ghostty_terminal_get(self._terminal, key, ctypes.byref(self._u16))
        return self._u16.value

    def _get_i32(self, key: int) -> int:
        self._i32.value = 0
        self._lib.ghostty_terminal_get(self._terminal, key, ctypes.byref(self._i32))
        return self._i32.value

    def _get_bool(self, key: int) -> bool:
        self._bool.value = False
        self._lib.ghostty_terminal_get(self._terminal, key, ctypes.byref(self._bool))
        return self._bool.value

    def _mode(self, mode: int) -> bool:
        config = self._mode_config
        config.mode, config.value = mode, False
        self._lib.ghostty_terminal_get(self._terminal, _DATA_MODE, ctypes.byref(config))
        return bool(config.value)

    def _set_size_t(self, option: int, value: int) -> None:
        number = ctypes.c_size_t(value)
        self._lib.ghostty_terminal_set(self._terminal, option, ctypes.cast(ctypes.byref(number), ctypes.c_void_p))

    def _apply_scrollback_limit(self, limit: int) -> None:
        # The byte limit defaults to one page (~10 KB); the line limit decides.
        # libghostty prunes whole pages (a page holds ~2000 narrow rows), so
        # the history it keeps is a bit over the limit and drops in steps.
        self._set_size_t(_OPT_SCROLLBACK_MAX_BYTES, ctypes.c_size_t(-1).value if limit else 0)
        if limit:
            self._set_size_t(_OPT_SCROLLBACK_MAX_LINES, limit)
        self._changed()

    @property
    def pull_history_on_grow(self) -> bool:
        """See ``EmulatorScreen.pull_history_on_grow``: off under ConPTY."""
        return self._pull_history_on_grow

    @pull_history_on_grow.setter
    def pull_history_on_grow(self, value: bool) -> None:
        self._pull_history_on_grow = flag = bool(value)
        boolean = ctypes.c_bool(flag)
        self._lib.ghostty_terminal_set(
            self._terminal, _OPT_RESIZE_PULL_SCROLLBACK,
            ctypes.cast(ctypes.byref(boolean), ctypes.c_void_p),
        )

    # -- input -----------------------------------------------------------------

    def feed(self, data: bytes) -> None:
        data = bytes(data)
        if not data:
            return
        lib, terminal = self._lib, self._terminal
        primary = self._get_i32(_DATA_ACTIVE_SCREEN) == _SCREEN_PRIMARY
        lib.ghostty_terminal_vt_write(terminal, data, len(data))
        self._changed()
        if primary and self._get_i32(_DATA_ACTIVE_SCREEN) == _SCREEN_PRIMARY:
            self.screen.scrollback_pushes += self._pushed_rows()
        self._reanchor()

    def _pushed_rows(self) -> int:
        """Rows the last write moved from the screen into the scrollback."""

        anchor = self._anchor
        if not anchor:
            return 0
        history = self.history_length
        if not self._lib.ghostty_tracked_grid_ref_has_value(anchor):
            return history + self._lines  # pruned away: everything moved
        point = _PointCoordinate()
        if self._lib.ghostty_tracked_grid_ref_point(anchor, _POINT_SCREEN, ctypes.byref(point)) != _SUCCESS:
            return 0
        return max(0, history - point.y)

    def _reanchor(self) -> None:
        if self._anchor:
            self._lib.ghostty_tracked_grid_ref_set(self._anchor, self._terminal, _active_top())

    def resize(self, columns: int, lines: int) -> None:
        columns, lines = max(1, columns), max(1, lines)
        if (columns, lines) == (self._columns, self._lines):
            return
        self._columns, self._lines = columns, lines
        self._lib.ghostty_terminal_resize(self._terminal, columns, lines, 10, 20)
        self._row_cache.clear()  # reflow rewrites rows
        self._changed()
        self._reanchor()

    def reset(self, *, clear_scrollback: bool) -> None:
        if clear_scrollback:
            self._lib.ghostty_terminal_reset(self._terminal)
            self._apply_scrollback_limit(self.screen.scrollback.maxlen)
            self.pull_history_on_grow = self._pull_history_on_grow
        else:
            # Like the other models' hard reset: modes and the screen go,
            # the history stays. (RIS would clear libghostty's history.)
            sequence = _RESET_KEEPING_HISTORY
            self._lib.ghostty_terminal_vt_write(self._terminal, sequence, len(sequence))
        self._row_cache.clear()
        self._changed()
        self._reanchor()

    def _changed(self) -> None:
        self._generation += 1

    # -- geometry --------------------------------------------------------------

    @property
    def columns(self) -> int:
        return self._columns

    @property
    def lines(self) -> int:
        return self._lines

    @property
    def history_length(self) -> int:
        generation, length = self._history_cache
        if generation != self._generation:
            self._size.value = 0
            self._lib.ghostty_terminal_get(self._terminal, _DATA_SCROLLBACK_ROWS, ctypes.byref(self._size))
            length = self._size.value
            self._history_cache = (self._generation, length)
        return length

    def line(self, row: int) -> Line:
        history = self.history_length
        if not (0 <= row < history + self._lines):
            return {}
        top = self._window_top
        if self._window_generation != self._generation or not (top <= row < top + len(self._window)):
            self._load_window(row, history)
        return self._window[row - self._window_top]

    def hyperlink_uri(self, row: int, column: int) -> Optional[str]:
        """The OSC 8 link target of the cell at absolute ``(row, column)``."""

        if not (0 <= row < self.history_length + self._lines and 0 <= column < self._columns):
            return None
        point = _Point()
        point.tag = _POINT_SCREEN
        point.value.coordinate.x = column
        point.value.coordinate.y = row
        ref = _GridRef()
        lib = self._lib
        if lib.ghostty_terminal_grid_ref(self._terminal, point, ctypes.byref(ref)) != _SUCCESS:
            return None
        length = ctypes.c_size_t()
        buffer = ctypes.create_string_buffer(512)
        result = lib.ghostty_grid_ref_hyperlink_uri(ctypes.byref(ref), buffer, len(buffer), ctypes.byref(length))
        if result == _OUT_OF_SPACE and length.value <= 65536:
            buffer = ctypes.create_string_buffer(length.value)
            result = lib.ghostty_grid_ref_hyperlink_uri(ctypes.byref(ref), buffer, len(buffer), ctypes.byref(length))
        if result != _SUCCESS or not length.value:
            return None
        return buffer.raw[:length.value].decode("utf-8", "replace")

    # -- reading rows --------------------------------------------------------------

    def _load_window(self, row: int, history: int) -> None:
        """Point libghostty's viewport at ``row`` and decode what it shows."""

        lib = self._lib
        top = min(row, history)
        viewport = _ScrollViewport()
        if top >= history:
            viewport.tag = _VIEWPORT_BOTTOM
        else:
            viewport.tag = _VIEWPORT_ROW
            viewport.value.row = top
        lib.ghostty_terminal_scroll_viewport(self._terminal, viewport)
        state = self._render_state
        lib.ghostty_render_state_update(state, self._terminal)
        rows = self._rows
        lib.ghostty_render_state_get(state, _RS_DATA_ROW_ITERATOR, ctypes.byref(rows))
        cache = self._row_cache
        if len(cache) > self._ROW_CACHE_LIMIT:
            cache.clear()
        row_get = lib.ghostty_render_state_row_get
        row_id, dirty = self._row_id, self._dirty
        window: List[Line] = []
        y = 0
        while lib.ghostty_render_state_row_iterator_next(rows):
            row_get(rows, _RS_ROW_ID, row_id)
            row_get(rows, _RS_ROW_DIRTY, ctypes.byref(dirty))
            key = row_id.raw
            in_history = top + y < history
            cached = cache.get(key)
            if cached is not None and (cached.final or not dirty.value):
                if in_history:
                    cached.final = True
                line = cached.line
            else:
                line = self._decode_row(rows)
                cache[key] = _CachedRow(line, in_history)
            window.append(line)
            y += 1
        lib.ghostty_render_state_clean(state)
        while len(window) < self._lines:
            window.append({})
        self._window = window
        self._window_top = top
        self._window_generation = self._generation

    def _decode_row(self, rows) -> Line:
        lib = self._lib
        view = self._cells_view
        if lib.ghostty_render_state_row_get(rows, _RS_ROW_CELLS_RAW, ctypes.byref(view)) != _SUCCESS or not view.ptr:
            return {}
        values = array.array("Q")
        values.frombytes(ctypes.string_at(view.ptr, view.len * 8))
        cl = self._cell_layout
        tag_shift, tag_mask = cl.tag_shift, cl.tag_mask
        cp_shift, cp_mask = cl.codepoint_shift, cl.codepoint_mask
        style_shift, style_mask = cl.style_shift, cl.style_mask
        wide_shift, wide_mask = cl.wide_shift, cl.wide_mask
        tag_cp, tag_grapheme, wide_tail = cl.tag_codepoint, cl.tag_grapheme, cl.wide_tail
        content_mask = self._content_mask
        # Cells are shared between rows: one table per resolved style, from a
        # cell's content bits (style id cleared) to its Char. Style ids are
        # page-local, so they are resolved again for every row; the style id
        # 0 is the default style everywhere.
        styled = self._styled_cells
        if len(styled) > 512 or len(styled.get(_DEFAULT_STYLE, ())) > 8192:
            styled.clear()
        default_table = styled.setdefault(_DEFAULT_STYLE, {})
        tables: Dict[int, tuple] = {0: (_DEFAULT_STYLE, default_table)}
        cells_ready = False
        line: Line = {}
        for x, value in enumerate(values):
            style_id = (value >> style_shift) & style_mask
            entry = tables.get(style_id)
            if entry is None:
                if not cells_ready:
                    lib.ghostty_render_state_row_get(rows, _RS_ROW_CELLS, ctypes.byref(self._cells))
                    cells_ready = True
                style = self._cell_style(x)
                table = styled.get(style)
                if table is None:
                    table = styled[style] = {}
                entry = tables[style_id] = (style, table)
            key = value & content_mask
            char = entry[1].get(key)
            if char is not None:
                line[x] = char
                continue
            style = entry[0]
            tag = (value >> tag_shift) & tag_mask
            if (value >> wide_shift) & wide_mask == wide_tail:
                data = ""
            elif tag == tag_cp:
                codepoint = (value >> cp_shift) & cp_mask
                data = chr(codepoint) if codepoint else " "
            elif tag == tag_grapheme:
                # The cluster lives outside the cell value: never shared.
                if not cells_ready:
                    lib.ghostty_render_state_row_get(rows, _RS_ROW_CELLS, ctypes.byref(self._cells))
                    cells_ready = True
                data = self._cell_grapheme(x) or chr((value >> cp_shift) & cp_mask)
                line[x] = Char(data, *style)
                continue
            else:
                data = " "
                style = (style[0], self._content_background(value, tag)) + style[2:]
            char = entry[1][key] = Char(data, *style)
            line[x] = char
        return line

    def _cell_style(self, x: int) -> tuple:
        lib = self._lib
        buffer = self._style_buffer
        lib.ghostty_render_state_row_cells_select(self._cells, x)
        if lib.ghostty_render_state_row_cells_get(self._cells, _RS_CELLS_STYLE, buffer) != _SUCCESS:
            return _DEFAULT_STYLE
        raw = buffer.raw
        decoded = self._decoded_styles.get(raw)
        if decoded is None:
            if len(self._decoded_styles) > 1024:
                self._decoded_styles.clear()
            decoded = self._decoded_styles[raw] = _decode_style(raw, self._style_layout)
        return decoded

    def _cell_grapheme(self, x: int) -> str:
        lib = self._lib
        storage = self._grapheme_storage
        buffer = _CodepointBuffer(
            ctypes.cast(storage, ctypes.POINTER(ctypes.c_uint32)), len(storage), 0
        )
        lib.ghostty_render_state_row_cells_select(self._cells, x)
        lib.ghostty_render_state_row_cells_get(self._cells, _RS_CELLS_GRAPHEMES_BUF, ctypes.byref(buffer))
        count = min(buffer.len, len(storage))
        return "".join(chr(storage[index]) for index in range(count))

    def _content_background(self, value: int, tag: int) -> str:
        cl = self._cell_layout
        if tag == cl.tag_bg_palette:
            return _INDEXED[(value >> cl.palette_shift) & 0xFF]
        rgb = (value >> cl.rgb_shift) & 0xFFFFFF
        return f"{rgb & 0xFF:02x}{(rgb >> 8) & 0xFF:02x}{rgb >> 16:02x}"


def _active_top() -> _Point:
    point = _Point()
    point.tag = _POINT_ACTIVE
    point.value.coordinate.x = 0
    point.value.coordinate.y = 0
    return point

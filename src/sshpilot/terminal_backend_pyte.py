"""Pure-GTK terminal backend: the pyte emulator drawn on a Gtk.DrawingArea.

Used where neither VTE nor WebKitGTK exists for GTK 4 (Windows), and
selectable elsewhere with ``terminal.backend = "pyte"``. It renders daemon
sessions only: the daemon owns the pseudo terminal, this widget turns its
bytes into pixels and key/mouse events back into bytes through the same
``commit`` / grid-size signals the VTE backend exposes.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import sys
import threading
from collections import deque
from typing import Any, Callable, Deque, Mapping, Optional, Sequence, Tuple

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GLib, GObject, Gtk, Pango, PangoCairo  # noqa: E402

from .terminal_model_common import (  # noqa: E402
    SynchronizedOutput,
    is_console_host_title,
    url_at,
    default_builtin_model,
    MOUSE_LEFT,
    MOUSE_MIDDLE,
    MOUSE_RIGHT,
    MOUSE_WHEEL_DOWN,
    MOUSE_WHEEL_UP,
    MODE_FOCUS_EVENTS,
    encode_key,
    encode_mouse,
)
from .terminal_backends import (  # noqa: E402
    TerminalBackendCapabilityError,
    _log_clipboard_copy,
)
from .terminal_color_utils import get_contrast_color  # noqa: E402
from .terminal_cursor import normalize_cursor_shape  # noqa: E402

logger = logging.getLogger(__name__)

#: Emulator models the view can draw: ``terminal.builtin_model`` in the
#: config, overridden by this environment variable ("pyte" or "vterm").
MODEL_ENV = "SSHPILOT_TERMINAL_MODEL"
DEFAULT_MODEL = default_builtin_model()


def _pyte_model(columns: int, lines: int, on_reply):
    from .pyte_terminal_model import TerminalModel

    return TerminalModel(columns, lines, on_reply=on_reply)


def _vterm_model(columns: int, lines: int, on_reply):
    from .vterm_terminal_model import VtermTerminalModel

    return VtermTerminalModel(columns, lines, on_reply=on_reply)


_MODELS = {"pyte": _pyte_model, "vterm": _vterm_model}


def create_terminal_model(preferred: Optional[str], columns: int, lines: int, on_reply):
    """``(name, model)`` for ``preferred``, falling back to the other model.

    Raises ``ImportError`` when neither pyte nor libvterm can be loaded.
    """

    name = (os.environ.get(MODEL_ENV) or preferred or DEFAULT_MODEL).strip().lower()
    if name not in _MODELS:
        logger.warning("Unknown terminal model %r, using %s", name, DEFAULT_MODEL)
        name = DEFAULT_MODEL
    errors = []
    for candidate in [name] + [other for other in _MODELS if other != name]:
        try:
            model = _MODELS[candidate](columns, lines, on_reply)
        except ImportError as exc:
            errors.append(f"{candidate}: {exc}")
            continue
        if candidate != name:
            logger.warning("Terminal model %s unavailable (%s); using %s", name, errors[0], candidate)
        return candidate, model
    raise ImportError("No terminal emulator model available (" + "; ".join(errors) + ")")


def daemon_output_is_rendered() -> bool:
    """True when daemon session output was already rendered by ConPTY.

    The daemon is always local (a Unix socket, or loopback TCP on Windows) and
    ``daemon/server.py`` runs sessions under ConPTY on Windows. conhost then
    sends repaints of its own screen rather than the remote's raw stream.
    """

    return sys.platform == "win32"


def _any_model_importable() -> bool:
    if importlib.util.find_spec("pyte") is not None:
        return True
    from .vterm_terminal_model import VtermUnavailable, load_libvterm

    try:
        load_libvterm()
    except VtermUnavailable:
        return False
    return True


if not _any_model_importable():
    # Keeps TerminalWidget's "fall back to VTE" path working where neither
    # emulator is installed.
    raise ImportError("The built-in terminal needs pyte or libvterm")

_DEFAULT_PALETTE = (
    "#000000", "#CC0000", "#4E9A06", "#C4A000",
    "#3465A4", "#75507B", "#06989A", "#D3D7CF",
    "#555753", "#EF2929", "#8AE234", "#FCE94F",
    "#729FCF", "#AD7FA8", "#34E2E2", "#EEEEEC",
)
# pyte's color names (and its "bfightmagenta" typo) -> palette index.
_NAMED_COLORS = {
    "black": 0, "red": 1, "green": 2, "brown": 3, "yellow": 3,
    "blue": 4, "magenta": 5, "cyan": 6, "white": 7,
    "brightblack": 8, "brightred": 9, "brightgreen": 10, "brightbrown": 11,
    "brightyellow": 11, "brightblue": 12, "brightmagenta": 13,
    "bfightmagenta": 13, "brightcyan": 14, "brightwhite": 15,
}
_HEX_COLOR = re.compile(r"^[0-9a-fA-F]{6}$")
_SEARCH_HIT = (0xF5 / 255, 0xA6 / 255, 0x23 / 255)

Rgb = Tuple[float, float, float]


def _parse_rgb(value: str, fallback: Rgb = (0.0, 0.0, 0.0)) -> Rgb:
    rgba = Gdk.RGBA()
    if value and rgba.parse(value):
        return (rgba.red, rgba.green, rgba.blue)
    return fallback


# Inner padding around the text grid, matching the CSS padding VTE gets on
# Linux/macOS (terminal.py) so text clears the card's rounded corners.
_PAD_X = 8
_PAD_Y = 4


class PyteTerminalView(Gtk.DrawingArea):
    """Draws a :class:`TerminalModel` and turns input into terminal bytes."""

    __gtype_name__ = "SshPilotPyteTerminalView"
    __gsignals__ = {
        # Same shapes as Vte.Terminal so TerminalWidget's handlers fit. The
        # payload is bytes (mouse reports are not valid UTF-8);
        # commit_payload_to_bytes passes bytes through unchanged.
        "commit": (GObject.SignalFlags.RUN_LAST, None, (object, int)),
        "grid-size-changed": (GObject.SignalFlags.RUN_LAST, None, (int, int)),
        "window-title-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
        "contents-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
        "selection-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
        "child-exited": (GObject.SignalFlags.RUN_LAST, None, (int,)),
    }

    def __init__(self, model_name: Optional[str] = None) -> None:
        super().__init__()
        self.set_focusable(True)
        self.set_can_focus(True)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_cursor_from_name("text")

        self.model_name, self.model = create_terminal_model(model_name, 80, 24, self._send)
        self.scroll_offset = 0
        self._seen_pushes = 0
        self._font_desc = Pango.FontDescription.from_string("Monospace 12")
        self._font_scale = 1.0
        self._cell_w = 8.0
        self._cell_h = 16.0
        self._baseline = 12.0
        self._metrics_dirty = True
        self._fg: Rgb = (0.0, 0.0, 0.0)
        self._bg: Rgb = (1.0, 1.0, 1.0)
        self._cursor_rgb: Rgb = (0.0, 0.0, 0.0)
        self._palette = [_parse_rgb(c) for c in _DEFAULT_PALETTE]
        self._cursor_shape = "block"
        self._selection: Optional[Tuple[Tuple[int, int], Tuple[int, int]]] = None
        self._select_anchor: Optional[Tuple[int, int]] = None
        self._select_mode = "char"
        self._search_hit: Optional[Tuple[int, int, int]] = None
        self._search_highlight = False
        self._mouse_button_down: Optional[int] = None
        self._last_motion_cell: Optional[Tuple[int, int]] = None
        self._grid_tracking = True
        self._reported_grid: Optional[Tuple[int, int]] = None
        self._focused = False
        self._last_pointer = (0.0, 0.0)
        self._sync_output = SynchronizedOutput()
        self._sync_timer = 0
        self._shown_title = ""

        self.vadjustment = Gtk.Adjustment(
            value=0, lower=0, upper=24, step_increment=1, page_increment=24, page_size=24
        )
        self._adjustment_guard = False
        self.vadjustment.connect("value-changed", self._on_adjustment_changed)

        self.set_draw_func(self._draw)
        self.connect("resize", self._on_resize)
        self._install_controllers()

    # ------------------------------------------------------------------ API

    def feed(self, data: bytes) -> None:
        if not data:
            return
        data, reply = self._sync_output.filter(bytes(data))
        if reply:
            self._send(reply)
        if not data:
            return
        self.model.feed(data)
        screen = self.model.screen
        pushes = screen.scrollback_pushes
        if self.scroll_offset and pushes != self._seen_pushes:
            # Keep a scrolled-back view on the same text as output arrives.
            self.scroll_offset = min(
                self.model.history_length,
                self.scroll_offset + (pushes - self._seen_pushes),
            )
        self._seen_pushes = pushes
        if screen.title_changed:
            screen.title_changed = False
            title = screen.title or ""
            # ConPTY reports the console host's own title (the client's
            # executable path) before the shell sets one; never show it.
            if not is_console_host_title(title) and title != self._shown_title:
                self._shown_title = title
                self.emit("window-title-changed")
        if screen.bell_rung:
            screen.bell_rung = False
            try:
                self.error_bell()
            except Exception:
                pass
        self._sync_adjustment()
        self._queue_output_draw()
        self.emit("contents-changed")

    def _queue_output_draw(self) -> None:
        """Repaint for new output, unless a synchronized update is open."""

        if self._sync_output.active:
            if not self._sync_timer:
                delay = int(self._sync_output.remaining() * 1000) + 1
                self._sync_timer = GLib.timeout_add(delay, self._on_sync_timeout)
            return
        if self._sync_timer:
            GLib.source_remove(self._sync_timer)
            self._sync_timer = 0
        self.queue_draw()

    def _on_sync_timeout(self) -> bool:
        self._sync_timer = 0
        self._queue_output_draw()
        return GLib.SOURCE_REMOVE

    def get_window_title(self) -> str:
        return self._shown_title

    def get_row_count(self) -> int:
        return self.model.lines

    def get_column_count(self) -> int:
        return self.model.columns

    def get_char_height(self) -> int:
        return max(1, int(round(self._cell_h)))

    def get_char_width(self) -> int:
        return max(1, int(round(self._cell_w)))

    def set_font(self, font_desc: Optional[Pango.FontDescription]) -> None:
        if font_desc is None:
            return
        self._font_desc = font_desc.copy()
        self._metrics_dirty = True
        self._recompute_grid()
        self.queue_draw()

    def set_font_scale(self, scale: float) -> None:
        self._font_scale = max(0.25, min(4.0, float(scale)))
        self._metrics_dirty = True
        self._recompute_grid()
        self.queue_draw()

    def get_font_scale(self) -> float:
        return self._font_scale

    def set_colors(self, fg: Rgb, bg: Rgb, palette: Optional[Sequence[Rgb]], cursor: Rgb) -> None:
        self._fg, self._bg, self._cursor_rgb = fg, bg, cursor
        if palette:
            self._palette = list(palette)[:16]
            while len(self._palette) < 16:
                self._palette.append((0.0, 0.0, 0.0))
        self.queue_draw()

    def set_cursor_shape(self, shape: str) -> None:
        self._cursor_shape = shape
        self.queue_draw()

    def reset(self, clear_scrollback: bool) -> None:
        self.model.reset(clear_scrollback=clear_scrollback)
        self.scroll_offset = 0
        self._seen_pushes = self.model.screen.scrollback_pushes
        self._selection = None
        self._sync_adjustment()
        self.queue_draw()

    def has_selection(self) -> bool:
        return self.get_selected_text() != ""

    def get_selected_text(self) -> str:
        if self._selection is None:
            return ""
        start, end = self._selection
        return self.model.text_between(start, end)

    def select_all(self) -> None:
        self._selection = ((0, 0), (self.model.total_rows - 1, self.model.columns - 1))
        self.queue_draw()
        self.emit("selection-changed")

    def clear_selection(self) -> None:
        if self._selection is not None:
            self._selection = None
            self.queue_draw()
            self.emit("selection-changed")

    def set_search_hit(self, hit: Optional[Tuple[int, int, int]]) -> None:
        self._search_hit = hit
        if hit is not None:
            row = hit[0]
            top = self.model.viewport_top(self.scroll_offset)
            if not (top <= row < top + self.model.lines):
                self.scroll_offset = max(
                    0, min(self.model.history_length, self.model.history_length - row + self.model.lines // 2)
                )
                self._sync_adjustment()
        self.queue_draw()

    def set_search_highlight(self, active: bool) -> None:
        self._search_highlight = bool(active)
        if not active:
            self._search_hit = None
        self.queue_draw()

    def paste_text(self, text: str) -> None:
        if not text:
            return
        text = text.replace("\r\n", "\r").replace("\n", "\r")
        if self.model.screen.bracketed_paste:
            # Strip embedded end markers so a paste cannot break out early.
            text = "\x1b[200~" + text.replace("\x1b[201~", "") + "\x1b[201~"
        self._scroll_to_bottom()
        self._send(text.encode("utf-8"))

    def invalidate_grid_size(self) -> None:
        self._reported_grid = None
        self._recompute_grid()

    def disable_grid_tracking(self) -> None:
        self._grid_tracking = False

    # -------------------------------------------------------------- output

    def _send(self, data: bytes) -> None:
        if data:
            self.emit("commit", bytes(data), len(data))

    # ------------------------------------------------------------- metrics

    def _effective_font(self) -> Pango.FontDescription:
        desc = self._font_desc.copy()
        size = desc.get_size() or 12 * Pango.SCALE
        if desc.get_size_is_absolute():
            desc.set_absolute_size(size * self._font_scale)
        else:
            desc.set_size(max(1, int(size * self._font_scale)))
        return desc

    def _update_metrics(self) -> None:
        if not self._metrics_dirty:
            return
        layout = self.create_pango_layout("M" * 32)
        layout.set_font_description(self._effective_font())
        _ink, logical = layout.get_pixel_extents()
        self._cell_w = max(1.0, logical.width / 32.0)
        self._cell_h = max(1.0, float(logical.height))
        self._baseline = layout.get_baseline() / Pango.SCALE
        self._metrics_dirty = False

    def _on_resize(self, _area, _width: int, _height: int) -> None:
        self._recompute_grid()

    def _recompute_grid(self) -> None:
        self._update_metrics()
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return
        columns = max(2, int((width - 2 * _PAD_X) // self._cell_w))
        rows = max(1, int((height - 2 * _PAD_Y) // self._cell_h))
        if (columns, rows) != (self.model.columns, self.model.lines):
            self.model.resize(columns, rows)
            self._clamp_scroll()
            self._sync_adjustment()
            self.queue_draw()
        if self._grid_tracking and self._reported_grid != (rows, columns):
            self._reported_grid = (rows, columns)
            self.emit("grid-size-changed", columns, rows)

    # ------------------------------------------------------------ scrolling

    def _clamp_scroll(self) -> None:
        self.scroll_offset = max(0, min(self.scroll_offset, self.model.history_length))

    def _scroll_to_bottom(self) -> None:
        if self.scroll_offset:
            self.scroll_offset = 0
            self._sync_adjustment()
            self.queue_draw()

    def scroll_lines(self, delta: int) -> None:
        """Scroll ``delta`` lines back into the scrollback (negative: forward)."""
        self.scroll_offset += int(delta)
        self._clamp_scroll()
        self._sync_adjustment()
        self.queue_draw()

    def _sync_adjustment(self) -> None:
        self._adjustment_guard = True
        try:
            total = self.model.total_rows
            page = self.model.lines
            self.vadjustment.configure(
                value=self.model.viewport_top(self.scroll_offset),
                lower=0,
                upper=total,
                step_increment=1,
                page_increment=max(1, page - 1),
                page_size=page,
            )
        finally:
            self._adjustment_guard = False

    def _on_adjustment_changed(self, adjustment: Gtk.Adjustment) -> None:
        if self._adjustment_guard:
            return
        top = int(round(adjustment.get_value()))
        self.scroll_offset = self.model.history_length - top
        self._clamp_scroll()
        self.queue_draw()

    # -------------------------------------------------------------- drawing

    def _resolve(self, color: str, *, foreground: bool, bold: bool) -> Rgb:
        if color == "default" or not color:
            return self._fg if foreground else self._bg
        index = _NAMED_COLORS.get(color)
        if index is not None:
            return self._palette[index]
        if _HEX_COLOR.match(color):
            return (
                int(color[0:2], 16) / 255,
                int(color[2:4], 16) / 255,
                int(color[4:6], 16) / 255,
            )
        return self._fg if foreground else self._bg

    def _cell_selected(self, row: int, column: int) -> bool:
        if self._selection is None:
            return False
        (r0, c0), (r1, c1) = sorted(self._selection)
        if row < r0 or row > r1:
            return False
        if r0 == r1:
            return c0 <= column <= c1
        if row == r0:
            return column >= c0
        if row == r1:
            return column <= c1
        return True

    def _draw(self, _area, cr, width: int, height: int) -> None:
        self._update_metrics()
        cr.set_source_rgb(*self._bg)
        cr.paint()
        cr.translate(_PAD_X, _PAD_Y)

        model = self.model
        top = model.viewport_top(self.scroll_offset)
        columns = model.columns
        cell_w, cell_h = self._cell_w, self._cell_h
        base_font = self._effective_font()
        fonts = {}
        layout = self.create_pango_layout("")

        def font_for(bold: bool, italic: bool) -> Pango.FontDescription:
            key = (bold, italic)
            if key not in fonts:
                desc = base_font.copy()
                if bold:
                    desc.set_weight(Pango.Weight.BOLD)
                if italic:
                    desc.set_style(Pango.Style.ITALIC)
                fonts[key] = desc
            return fonts[key]

        search_hit = self._search_hit if self._search_highlight else None
        for screen_row in range(model.lines):
            row = top + screen_row
            if row >= model.total_rows:
                break
            line = model.line(row)
            y = screen_row * cell_h
            column = 0
            while column < columns:
                char = line.get(column)
                key = self._style_key(char, row, column, search_hit)
                run_start = column
                text_parts = []
                while column < columns:
                    char = line.get(column)
                    if self._style_key(char, row, column, search_hit) != key:
                        break
                    data = char.data if char is not None else " "
                    text_parts.append(data)
                    column += 1
                    if data and len(data.encode("utf-16-le")) > 2:
                        break  # astral glyphs: keep runs short to limit drift
                self._draw_run(cr, layout, font_for, key, run_start, column, y, "".join(text_parts))

        self._draw_cursor(cr, top, font_for, layout)

    def _style_key(self, char, row: int, column: int, search_hit) -> tuple:
        if char is None:
            fg, bg, bold, italic, underline, strike, reverse = (
                "default", "default", False, False, False, False, False,
            )
        else:
            fg, bg = char.fg, char.bg
            bold, italic = char.bold, char.italics
            underline, strike, reverse = char.underscore, char.strikethrough, char.reverse
        selected = self._cell_selected(row, column)
        hit = bool(
            search_hit is not None
            and search_hit[0] == row
            and search_hit[1] <= column < search_hit[2]
        )
        return (fg, bg, bold, italic, underline, strike, reverse != selected, hit)

    def _draw_run(self, cr, layout, font_for, key, start: int, end: int, y: float, text: str) -> None:
        fg_name, bg_name, bold, italic, underline, strike, reverse, hit = key
        fg = self._resolve(fg_name, foreground=True, bold=bold)
        bg = self._resolve(bg_name, foreground=False, bold=bold)
        if reverse:
            fg, bg = bg, fg
        if hit:
            fg, bg = (0.0, 0.0, 0.0), _SEARCH_HIT
        x = start * self._cell_w
        width = (end - start) * self._cell_w
        if bg != self._bg:
            cr.set_source_rgb(*bg)
            cr.rectangle(x, y, width, self._cell_h)
            cr.fill()
        if text.strip():
            layout.set_font_description(font_for(bold, italic))
            layout.set_text(text, -1)
            cr.set_source_rgb(*fg)
            cr.move_to(x, y)
            PangoCairo.show_layout(cr, layout)
        if underline or strike:
            cr.set_source_rgb(*fg)
            cr.set_line_width(1.0)
            line_y = y + (self._baseline + 1.5 if underline else self._cell_h / 2)
            cr.move_to(x, line_y)
            cr.line_to(x + width, line_y)
            cr.stroke()

    def _draw_cursor(self, cr, top: int, font_for, layout) -> None:
        model = self.model
        screen = model.screen
        if not screen.cursor_visible:
            return
        row = model.history_length + screen.cursor.y
        screen_row = row - top
        if not (0 <= screen_row < model.lines):
            return
        column = min(screen.cursor.x, model.columns - 1)
        x, y = column * self._cell_w, screen_row * self._cell_h
        cr.set_source_rgb(*self._cursor_rgb)
        if not self._focused:
            cr.set_line_width(1.0)
            cr.rectangle(x + 0.5, y + 0.5, self._cell_w - 1, self._cell_h - 1)
            cr.stroke()
            return
        shape = self._cursor_shape
        if shape == "ibeam":
            cr.rectangle(x, y, max(1.0, self._cell_w / 8), self._cell_h)
            cr.fill()
        elif shape == "underline":
            cr.rectangle(x, y + self._cell_h - 2, self._cell_w, 2)
            cr.fill()
        else:
            cr.rectangle(x, y, self._cell_w, self._cell_h)
            cr.fill()
            char = model.line(row).get(column)
            if char is not None and char.data.strip():
                layout.set_font_description(font_for(char.bold, char.italics))
                layout.set_text(char.data, -1)
                cr.set_source_rgb(*self._bg)
                cr.move_to(x, y)
                PangoCairo.show_layout(cr, layout)

    # --------------------------------------------------------------- input

    def _install_controllers(self) -> None:
        self._im = Gtk.IMMulticontext()
        self._im.connect("commit", self._on_im_commit)
        keys = Gtk.EventControllerKey()
        keys.set_im_context(self._im)
        keys.connect("key-pressed", self._on_key_pressed)
        self.add_controller(keys)

        focus = Gtk.EventControllerFocus()
        focus.connect("enter", self._on_focus_in)
        focus.connect("leave", self._on_focus_out)
        self.add_controller(focus)

        click = Gtk.GestureClick()
        click.set_button(0)
        click.connect("pressed", self._on_click_pressed)
        click.connect("released", self._on_click_released)
        self.add_controller(click)

        drag = Gtk.GestureDrag()
        drag.set_button(Gdk.BUTTON_PRIMARY)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)
        self._drag = drag

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        self.add_controller(motion)

        scroll = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL
            | Gtk.EventControllerScrollFlags.DISCRETE
        )
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)

    def _on_focus_in(self, *_args) -> None:
        self._focused = True
        self._im.focus_in()
        self._im.set_client_widget(self)
        if MODE_FOCUS_EVENTS in self.model.screen.mode:
            self._send(b"\x1b[I")
        self.queue_draw()

    def _on_focus_out(self, *_args) -> None:
        self._focused = False
        self._im.focus_out()
        if MODE_FOCUS_EVENTS in self.model.screen.mode:
            self._send(b"\x1b[O")
        self.queue_draw()

    def _on_im_commit(self, _im, text: str) -> None:
        if text:
            self._scroll_to_bottom()
            self._send(text.encode("utf-8"))

    def _on_key_pressed(self, controller, keyval: int, _keycode: int, state) -> bool:
        name = Gdk.keyval_name(keyval) or ""
        if name in {
            "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
            "Meta_L", "Meta_R", "Super_L", "Super_R", "ISO_Level3_Shift",
            "Caps_Lock", "Num_Lock",
        }:
            return False
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        alt = bool(state & Gdk.ModifierType.ALT_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if shift and not ctrl and not alt and name in ("Page_Up", "Page_Down"):
            self.scroll_lines(
                (1 if name == "Page_Up" else -1) * max(1, self.model.lines - 1)
            )
            return True
        if shift and name in ("Insert", "KP_Insert") and not ctrl:
            self.paste_clipboard_async()
            return True
        codepoint = Gdk.keyval_to_unicode(keyval)
        text = chr(codepoint) if codepoint else ""
        if ctrl and alt and text and not ("a" <= text.lower() <= "z") and text.isprintable():
            # Windows reports AltGr as Ctrl+Alt: AltGr+Q on a German layout
            # is "@", which must reach the remote as text, not as Ctrl+@.
            ctrl = alt = False
        data = encode_key(
            name,
            text,
            ctrl=ctrl,
            alt=alt,
            shift=shift,
            app_cursor=self.model.screen.app_cursor_keys,
        )
        if data is None:
            return False
        self._scroll_to_bottom()
        self.clear_selection()
        self._send(data)
        return True

    def _cell_at(self, x: float, y: float) -> Tuple[int, int]:
        x, y = x - _PAD_X, y - _PAD_Y
        column = int(max(0, min(self.model.columns - 1, x // self._cell_w)))
        screen_row = int(max(0, min(self.model.lines - 1, y // self._cell_h)))
        return self.model.viewport_top(self.scroll_offset) + screen_row, column

    def _screen_cell_at(self, x: float, y: float) -> Tuple[int, int]:
        x, y = x - _PAD_X, y - _PAD_Y
        column = int(max(0, min(self.model.columns - 1, x // self._cell_w)))
        screen_row = int(max(0, min(self.model.lines - 1, y // self._cell_h)))
        return screen_row, column

    def _reporting_mouse(self, state) -> bool:
        tracking = self.model.screen.mouse_tracking
        if tracking is None:
            return False
        # Shift bypasses mouse reporting so text can still be selected.
        return not bool(state & Gdk.ModifierType.SHIFT_MASK)

    def _report_mouse(self, button: int, x: float, y: float, action: str, state) -> None:
        row, column = self._screen_cell_at(x, y)
        screen = self.model.screen
        if screen.mouse_tracking == "x10" and action != "press":
            return
        data = encode_mouse(
            button,
            column,
            row,
            action=action,
            sgr=screen.mouse_sgr,
            alt=bool(state & Gdk.ModifierType.ALT_MASK),
            ctrl=bool(state & Gdk.ModifierType.CONTROL_MASK),
        )
        if data:
            self._send(data)

    @staticmethod
    def _button_code(gdk_button: int) -> Optional[int]:
        return {1: MOUSE_LEFT, 2: MOUSE_MIDDLE, 3: MOUSE_RIGHT}.get(gdk_button)

    def _on_click_pressed(self, gesture, n_press: int, x: float, y: float) -> None:
        self.grab_focus()
        button = gesture.get_current_button()
        state = gesture.get_current_event_state()
        if self._reporting_mouse(state):
            code = self._button_code(button)
            if code is not None:
                self._mouse_button_down = code
                self._report_mouse(code, x, y, "press", state)
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            return
        if button == Gdk.BUTTON_MIDDLE:
            self.paste_clipboard_async(primary=True)
            return
        if button != Gdk.BUTTON_PRIMARY:
            return
        row, column = self._cell_at(x, y)
        if n_press >= 3:
            self._select_mode = "line"
            self._selection = ((row, 0), (row, self.model.columns - 1))
        elif n_press == 2:
            self._select_mode = "word"
            start, end = self.model.word_bounds(row, column)
            self._selection = ((row, start), (row, end))
        else:
            self._select_mode = "char"
            self._selection = None
        self._select_anchor = (row, column)
        self.queue_draw()
        if self._selection is not None:
            self._selection_finished()

    def _on_click_released(self, gesture, _n_press: int, x: float, y: float) -> None:
        if self._mouse_button_down is not None:
            state = gesture.get_current_event_state()
            self._report_mouse(self._mouse_button_down, x, y, "release", state)
            self._mouse_button_down = None

    def _on_drag_update(self, gesture, offset_x: float, offset_y: float) -> None:
        if self._mouse_button_down is not None or self._select_anchor is None:
            return
        ok, start_x, start_y = gesture.get_start_point()
        if not ok:
            return
        x, y = start_x + offset_x, start_y + offset_y
        # Auto-scroll while dragging past the edges.
        if y < 0:
            self.scroll_lines(1)
        elif y > self.get_height():
            self.scroll_lines(-1)
        row, column = self._cell_at(x, y)
        anchor = self._select_anchor
        if self._select_mode == "line":
            first, last = sorted((anchor[0], row))
            self._selection = ((first, 0), (last, self.model.columns - 1))
        elif self._select_mode == "word":
            a_start, a_end = self.model.word_bounds(*anchor)
            b_start, b_end = self.model.word_bounds(row, column)
            if (row, column) >= anchor:
                self._selection = ((anchor[0], a_start), (row, b_end))
            else:
                self._selection = ((row, b_start), (anchor[0], a_end))
        elif (row, column) != anchor:
            self._selection = (anchor, (row, column))
        self.queue_draw()

    def _on_drag_end(self, gesture, offset_x: float, offset_y: float) -> None:
        if self._mouse_button_down is not None:
            ok, start_x, start_y = gesture.get_start_point()
            if not ok:
                # Cancelled because the click gesture claimed the press for
                # mouse reporting; its "released" reports the button-up.
                return
            # The click gesture gives up once the pointer moves, so a drag's
            # button release is reported from here instead.
            state = gesture.get_current_event_state()
            self._report_mouse(
                self._mouse_button_down,
                start_x + offset_x,
                start_y + offset_y,
                "release",
                state,
            )
            self._mouse_button_down = None
            return
        if self._selection is not None:
            self._selection_finished()

    def _selection_finished(self) -> None:
        text = self.get_selected_text()
        if text:
            primary = self.get_primary_clipboard()
            if primary is not None:
                try:
                    primary.set(text)
                except Exception:
                    logger.debug("Could not set PRIMARY selection", exc_info=True)
        self.emit("selection-changed")

    def _on_motion(self, controller, x: float, y: float) -> None:
        self._last_pointer = (x, y)
        tracking = self.model.screen.mouse_tracking
        if tracking not in ("drag", "any"):
            return
        state = controller.get_current_event_state()
        if not self._reporting_mouse(state):
            return
        cell = self._screen_cell_at(x, y)
        if cell == self._last_motion_cell:
            return
        self._last_motion_cell = cell
        if self._mouse_button_down is not None:
            self._report_mouse(self._mouse_button_down, x, y, "motion", state)
        elif tracking == "any":
            self._report_mouse(3, x, y, "motion", state)

    def _on_scroll(self, controller, _dx: float, dy: float) -> bool:
        if dy == 0:
            return False
        state = controller.get_current_event_state()
        screen = self.model.screen
        if self._reporting_mouse(state):
            button = MOUSE_WHEEL_UP if dy < 0 else MOUSE_WHEEL_DOWN
            x, y = self._last_pointer
            self._report_mouse(button, x, y, "press", state)
            return True
        if screen.alternate_active:
            # Like VTE/xterm alternateScroll: full-screen apps get arrow keys.
            key = "Up" if dy < 0 else "Down"
            data = encode_key(key, app_cursor=screen.app_cursor_keys)
            self._send(data * max(1, int(abs(dy)) * 3))
            return True
        self.scroll_lines(int(-dy * 3) or (-1 if dy > 0 else 1))
        return True

    # ----------------------------------------------------------- clipboard

    def paste_clipboard_async(self, *, primary: bool = False) -> None:
        clipboard = self.get_primary_clipboard() if primary else self.get_clipboard()
        if clipboard is None:
            return

        def _done(source, result) -> None:
            try:
                text = source.read_text_finish(result)
            except GLib.Error:
                logger.debug("Clipboard read failed", exc_info=True)
                return
            if text:
                self.paste_text(text)

        clipboard.read_text_async(None, _done)


class PyteTerminalBackend:
    """``BaseTerminalBackend`` over :class:`PyteTerminalView`."""

    supports_pty_adoption = False

    def __init__(self, owner: Any) -> None:
        self.owner = owner
        config = getattr(owner, "config", None)
        try:
            model_name = config.get_setting("terminal.builtin_model", DEFAULT_MODEL) if config else None
        except Exception:
            model_name = None
        self.view = PyteTerminalView(model_name)
        logger.info("Built-in terminal using the %s emulator model", self.view.model_name)
        #: Most daemon output bytes fed per GTK dispatch, about one frame's
        #: worth of parsing at 120 columns (libvterm ~20 ms per 64 KB, pyte
        #: ~180 ms), so typing and repaints run between slices.
        self.output_slice_bytes = 32 * 1024 if self.view.model_name == "vterm" else 8 * 1024
        # A local shell this backend spawned (spawn_async), if any.
        self._local = None
        self._local_handlers: list = []
        self._local_lock = threading.Lock()
        self._local_pending: Deque[bytes] = deque()
        self._local_scheduled = False
        self._local_exit_status: Optional[int] = None
        self._scrollbar = Gtk.Scrollbar(
            orientation=Gtk.Orientation.VERTICAL, adjustment=self.view.vadjustment
        )
        self._scrollbar.add_css_class("overlay-indicator")
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        box.set_hexpand(True)
        box.set_vexpand(True)
        box.append(self.view)
        box.append(self._scrollbar)
        self.widget = box
        self._initialized = False
        self._search_query = None
        self._search_position: Optional[Tuple[int, int]] = None
        self._selection_from_search = False

    # -- lifecycle -------------------------------------------------------------

    def initialize(self) -> None:
        self._initialized = True

    def destroy(self) -> None:
        self._close_local()
        self.view.disable_grid_tracking()
        if self.view._sync_timer:
            GLib.source_remove(self.view._sync_timer)
            self.view._sync_timer = 0

    def configure(self, settings: Optional[Mapping[str, Any]] = None) -> None:
        settings = settings or {}
        self.set_cursor_options(settings.get("cursor_shape"), settings.get("cursor_blink"))
        scrollback = settings.get("scrollback_lines")
        if scrollback is not None:
            try:
                limit = max(0, int(scrollback))
            except (TypeError, ValueError):
                limit = None
            if limit is not None and limit != self.view.model.screen.scrollback.maxlen:
                from collections import deque

                screen = self.view.model.screen
                screen.scrollback = deque(screen.scrollback, maxlen=limit)

    def set_cursor_options(self, shape: Any = None, blink: Any = None) -> None:
        self.view.set_cursor_shape(normalize_cursor_shape(shape))

    def apply_theme(self, theme_name: Optional[str] = None) -> None:
        owner = self.owner
        config = getattr(owner, "config", None)
        try:
            if theme_name is None and config:
                theme_name = config.get_setting("terminal.theme", "default")
            profile = config.get_terminal_profile(theme_name) if config else {}
        except Exception:
            logger.debug("Could not read terminal profile", exc_info=True)
            profile = {}
        fg = _parse_rgb(profile.get("foreground", "#000000"))
        bg = _parse_rgb(profile.get("background", "#FFFFFF"), (1.0, 1.0, 1.0))
        cursor = _parse_rgb(profile.get("cursor_color", profile.get("foreground", "#000000")))
        try:
            use_group = bool(config.get_setting("ui.use_group_color_in_terminal", False)) if config else False
            override = owner._get_group_color_rgba() if hasattr(owner, "_get_group_color_rgba") else None
            if use_group and override is not None:
                bg = (override.red, override.green, override.blue)
                contrast = get_contrast_color(override)
                fg = cursor = (contrast.red, contrast.green, contrast.blue)
        except Exception:
            logger.debug("Group color override failed", exc_info=True)
        palette = [_parse_rgb(c) for c in (profile.get("palette") or _DEFAULT_PALETTE)]
        self.view.set_colors(fg, bg, palette, cursor)
        font = profile.get("font")
        if font:
            self.view.set_font(Pango.FontDescription.from_string(font))

    def set_font(self, font_desc: Pango.FontDescription) -> None:
        self.view.set_font(font_desc)

    def grab_focus(self) -> None:
        self.view.grab_focus()

    def queue_draw(self) -> None:
        self.view.queue_draw()

    def show(self) -> None:
        self.widget.set_visible(True)

    # -- process ownership: the daemon owns the PTY ------------------------------

    def spawn_async(
        self,
        argv=None,
        env=None,
        cwd=None,
        flags=0,
        child_setup=None,
        callback=None,
        user_data=None,
        **_kwargs,
    ) -> None:
        """Run a local shell on a pseudo terminal this backend owns.

        Local tabs belong to the frontend (frontend-closure-audit.md), as with
        VTE: typed input goes straight to the process and its output straight
        to the view; no daemon session is involved. ``callback`` gets VTE's
        ``(terminal, pid, error, user_data)``.
        """

        from .local_shell_pty import LocalShellProcess

        if self._local is not None:
            raise TerminalBackendCapabilityError("This terminal already runs a local shell")
        view = self.view
        process = LocalShellProcess(
            on_output=self._queue_local_output,
            on_exit=lambda status: GLib.idle_add(self._on_local_exit, process, status),
        )
        if daemon_output_is_rendered():
            # ConPTY repaints its viewport after a resize, as for daemon
            # sessions: pulling history back on a grow would be painted over.
            view.model.pull_history_on_grow = False
        try:
            pid = process.start(
                list(argv or ()),
                env=env,
                cwd=cwd,
                columns=view.get_column_count(),
                rows=view.get_row_count(),
            )
        except Exception as exc:  # noqa: BLE001 - reported through the callback
            logger.warning("Local shell spawn failed: %s", exc)
            error = exc  # ``exc`` is unbound once the except block ends
            if callback is not None:
                GLib.idle_add(lambda: (callback(view, -1, error, user_data), False)[1])
            return
        self._local = process
        self._local_handlers = [
            view.connect("commit", lambda _view, data, _length: process.write(
                data if isinstance(data, bytes) else str(data).encode("utf-8")
            )),
            view.connect("grid-size-changed", lambda _view, columns, rows: process.resize(columns, rows)),
        ]
        logger.info("Local shell started on the built-in backend pid=%s", pid)
        if callback is not None:
            GLib.idle_add(lambda: (callback(view, pid, None, user_data), False)[1])

    def _queue_local_output(self, data: bytes) -> None:
        """Reader thread: queue output and wake the main loop once."""
        with self._local_lock:
            self._local_pending.append(data)
            if self._local_scheduled:
                return
            self._local_scheduled = True
        GLib.idle_add(self._drain_local_output)

    def _drain_local_output(self) -> bool:
        # One frame-sized slice per dispatch, like daemon output.
        budget = self.output_slice_bytes
        chunks = []
        with self._local_lock:
            while self._local_pending and budget > 0:
                chunk = self._local_pending.popleft()
                if len(chunk) > budget:
                    self._local_pending.appendleft(chunk[budget:])
                    chunk = chunk[:budget]
                chunks.append(chunk)
                budget -= len(chunk)
            more = bool(self._local_pending)
            if not more:
                self._local_scheduled = False
        if chunks:
            self.view.feed(b"".join(chunks))
        if not more and self._local_exit_status is not None:
            self._emit_local_exit()
        return more

    def _on_local_exit(self, process, status: int) -> bool:
        if process is not self._local:
            return False
        self._local_exit_status = int(status)
        with self._local_lock:
            pending = self._local_scheduled or bool(self._local_pending)
        if not pending:
            self._emit_local_exit()
        return False

    def _emit_local_exit(self) -> None:
        status, self._local_exit_status = self._local_exit_status, None
        if status is None:
            return
        for handler in self._local_handlers:
            try:
                self.view.disconnect(handler)
            except Exception:
                pass
        self._local_handlers = []
        logger.info("Local shell exited status=%s", status)
        self.view.emit("child-exited", status)

    def _close_local(self) -> None:
        process, self._local = self._local, None
        if process is not None:
            process.close()

    def adopt_pty(self, master_fd: int, watch_pid: int) -> None:
        raise TerminalBackendCapabilityError(
            "The pyte backend renders daemon sessions only"
        )

    def prepare_pty_less_emulation(self) -> None:
        """Adapt the model to the daemon's stream (called on daemon paths only).

        After a resize conhost repaints its viewport from the top, so rows
        the model pulled back from the scrollback on a grow would be painted
        over and drop out of the transcript. Under ConPTY the model leaves
        history alone and adds the new rows at the bottom. A raw stream (the
        POSIX daemon) keeps the xterm-like pull.
        """

        self.view.model.pull_history_on_grow = not daemon_output_is_rendered()

    def get_pty(self) -> Optional[Any]:
        return None

    def get_child_pid(self) -> Optional[int]:
        if self._local is not None:
            return self._local.pid
        return getattr(self.owner, "process_pid", None)

    def get_child_pgid(self) -> Optional[int]:
        if self._local is not None:
            return self._local.pgid
        return None

    def local_shell_running(self) -> bool:
        return self._local is not None and self._local.is_running()

    # -- signals -----------------------------------------------------------------

    def connect_child_exited(self, callback: Callable[..., None]) -> Any:
        return self.view.connect("child-exited", callback)

    def connect_title_changed(self, callback: Callable[..., None]) -> Any:
        return self.view.connect("window-title-changed", callback)

    def connect_termprops_changed(self, callback: Callable[..., None]) -> Optional[Any]:
        return None

    def connect_commit(self, callback: Callable[..., None]) -> Optional[Any]:
        return self.view.connect("commit", callback)

    def connect_size_changed(self, callback: Callable[..., None]) -> Optional[Any]:
        handler = self.view.connect("grid-size-changed", callback)
        self.view.invalidate_grid_size()
        return handler

    def invalidate_size_tracking(self) -> None:
        self.view.invalidate_grid_size()

    def connect_content_changed(self, callback: Callable[..., None]) -> Optional[Any]:
        return self.view.connect("contents-changed", callback)

    def disconnect(self, handler_id: Any) -> None:
        if handler_id is None:
            return
        try:
            self.view.disconnect(handler_id)
        except Exception:
            logger.debug("Failed to disconnect pyte handler", exc_info=True)

    # -- I/O -----------------------------------------------------------------------

    def feed(self, data: bytes) -> None:
        self.view.feed(data)

    def feed_child(self, data: bytes) -> None:
        self.feed_child_data(data)

    def feed_child_data(self, data: bytes) -> None:
        self.view._send(bytes(data))

    def get_size(self) -> tuple:
        return (max(1, self.view.model.lines), max(1, self.view.model.columns))

    def get_content(self, max_chars: Optional[int] = None) -> Optional[str]:
        content = self.view.model.all_text()
        if content and max_chars and len(content) > max_chars:
            return content[-max_chars:]
        return content

    def save_contents(self, stream: Any) -> None:
        data = (self.view.model.all_text() + "\n").encode("utf-8")
        stream.write_all(data, None)

    def reset(self, clear_scrollback: bool, clear_screen: bool) -> None:
        self.view.reset(clear_scrollback)

    def set_font_scale(self, scale: float) -> None:
        self.view.set_font_scale(scale)

    def get_font_scale(self) -> float:
        return self.view.get_font_scale()

    # -- clipboard -----------------------------------------------------------------

    def copy_clipboard(
        self,
        format: str = "text",
        on_complete: Optional[Callable[[bool], None]] = None,
    ) -> None:
        text = self.view.get_selected_text()
        copied = False
        reason = "empty-selection"
        if text:
            try:
                self.view.get_clipboard().set(text)
                copied, reason = True, "written"
            except Exception:
                reason = "exception"
                logger.debug("Failed to copy pyte selection", exc_info=True)
        _log_clipboard_copy(
            backend="pyte", copied=copied, reason=reason, text_len=len(text), format=format
        )
        if on_complete is not None:
            on_complete(copied)

    def get_has_selection(self) -> bool:
        return self.view.has_selection()

    def paste_clipboard(self) -> None:
        self.view.paste_clipboard_async()

    def select_all(self) -> None:
        self.view.select_all()

    def selection_change_is_copyable(self) -> bool:
        return not self._selection_from_search

    # -- search --------------------------------------------------------------------

    def search_set_regex(self, regex: Optional[Any]) -> None:
        if regex is None:
            self._search_query = None
            self.view.set_search_hit(None)

    def search_set_query(
        self,
        term: Optional[str],
        *,
        case_sensitive: bool = False,
        regex: bool = False,
    ) -> None:
        self._search_position = None
        if not term:
            self._search_query = None
            self.view.set_search_hit(None)
            return
        pattern = term if regex else re.escape(term)
        try:
            self._search_query = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
        except re.error:
            self._search_query = re.compile(re.escape(term), 0 if case_sensitive else re.IGNORECASE)

    def _matcher(self, text: str, start: int):
        match = self._search_query.search(text, start) if self._search_query else None
        if match is None or match.end() == match.start():
            return None
        return match.start(), match.end()

    def _search(self, *, backwards: bool) -> bool:
        if self._search_query is None:
            return False
        hit = self.view.model.find(
            self._matcher, after=self._search_position, backwards=backwards
        )
        if hit is None:
            return False
        row, start, end = hit
        self._search_position = (row, start)
        self._selection_from_search = True
        try:
            self.view._selection = ((row, start), (row, max(start, end - 1)))
            self.view.set_search_hit(hit)
            self.view.emit("selection-changed")
        finally:
            self._selection_from_search = False
        return True

    def search_find_next(self) -> bool:
        return self._search(backwards=False)

    def search_find_previous(self) -> bool:
        return self._search(backwards=True)

    def clear_search_decorations(self) -> None:
        self.set_search_highlight(False)

    def set_search_highlight(self, active: bool) -> None:
        self.view.set_search_highlight(active)

    # -- unsupported VTE extras -------------------------------------------------------

    def setup_link_handling(self, *_args, **_kwargs) -> None:
        return None

    def setup_native_context_menu(self, menu: Any, callback: Callable) -> bool:
        return False

    def clear_native_context_menu(self) -> None:
        return None

    def hyperlink_at(self, x: float, y: float) -> Optional[str]:
        # Neither model keeps OSC 8 targets per cell, so plain-text links are
        # the only kind.
        return self.plain_text_link_at(x, y)

    def plain_text_link_at(self, x: float, y: float) -> Optional[str]:
        view = self.view
        row, column = view._cell_at(x, y)
        try:
            text = view.model.row_text(row)
        except Exception:
            return None
        return url_at(text, column)

    def set_pointer_over_link(self, over_link: bool) -> None:
        return None

    def get_supported_encodings(self) -> list[str]:
        return ["UTF-8"]

    def set_encoding(self, encoding: str) -> None:
        return None

    def supports_feature(self, feature: str) -> bool:
        return feature in {
            "terminal_search",
            "search",
            "dynamic_font",
            "font-scaling",
            "clipboard",
            "content_extraction",
            "daemon_input",
            "daemon_resize",
            "save_output",
            "hyperlinks",
        }

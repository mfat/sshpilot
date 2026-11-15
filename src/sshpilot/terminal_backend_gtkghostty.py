"""Terminal backend on the gtkghostty widget (libghostty-vt drawn in C).

``GtkGhostty.Terminal`` (https://github.com/mfat/gtkghostty) is a GTK 4
widget around libghostty-vt: it parses, draws, encodes keys and mouse, and
owns selection, search and scrollback. This backend adapts it to
``BaseTerminalBackend`` the way ``PyteTerminalBackend`` adapts its Python
view, and reuses the same process plumbing:

* daemon sessions: output arrives through :meth:`feed`; typed input and the
  emulator's replies leave through the ``commit`` callbacks;
* local shells: :class:`LocalShellProcess`, exactly as the built-in backend.

Selected by ``terminal.backend = "gtkghostty"``; importing raises
``ImportError`` when the widget's typelib is not installed, which
``TerminalWidget`` treats as "fall back".
"""

from __future__ import annotations

import itertools
import logging
import threading
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Tuple

import gi

gi.require_version("Gtk", "4.0")
try:
    gi.require_version("GtkGhostty", "1")
except ValueError as exc:  # the typelib is not installed
    raise ImportError(f"gtkghostty is not installed: {exc}") from exc

from gi.repository import Gdk, GLib, Gtk, GtkGhostty, Pango  # noqa: E402

from .terminal_backends import (  # noqa: E402
    TerminalBackendCapabilityError,
    _log_clipboard_copy,
)
from .terminal_cursor import normalize_cursor_blink, normalize_cursor_shape  # noqa: E402
from .terminal_model_common import url_at  # noqa: E402
from .terminal_theme_resolve import Rgb, daemon_output_is_rendered, resolve_theme  # noqa: E402

logger = logging.getLogger(__name__)

#: Inner padding around the grid, as the built-in backend and VTE's CSS
#: padding, so text clears the card's rounded corners.
_PAD_X = 8
_PAD_Y = 4

_CURSOR_SHAPES = {
    "block": GtkGhostty.CursorShape.BLOCK,
    "ibeam": GtkGhostty.CursorShape.IBEAM,
    "underline": GtkGhostty.CursorShape.UNDERLINE,
}

_instance_ids = itertools.count(1)


def gtkghostty_available() -> Tuple[bool, Optional[str]]:
    """Whether the widget can be created here, and why not."""
    try:
        GtkGhostty.Terminal  # noqa: B018 - resolves the library
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    return True, None


def _rgba(rgb: Rgb) -> Gdk.RGBA:
    colour = Gdk.RGBA()
    colour.red, colour.green, colour.blue = rgb
    colour.alpha = 1.0
    return colour


class GtkGhosttyTerminalBackend:
    """``BaseTerminalBackend`` over :class:`GtkGhostty.Terminal`."""

    supports_pty_adoption = False
    #: Daemon output fed per GTK dispatch: libghostty-vt parses as fast as
    #: the native built-in models, so it gets their slice size.
    output_slice_bytes = 32 * 1024

    def __init__(self, owner: Any) -> None:
        self.owner = owner
        self.view = GtkGhostty.Terminal()
        self.view.set_hexpand(True)
        self.view.set_vexpand(True)
        self.view.set_margin_start(_PAD_X)
        self.view.set_margin_end(_PAD_X)
        self.view.set_margin_top(_PAD_Y)
        self.view.set_margin_bottom(_PAD_Y)
        self.vadjustment = Gtk.Adjustment()
        self.view.set_vadjustment(self.vadjustment)
        logger.info("Terminal using the gtkghostty widget")

        self._scrollbar = Gtk.Scrollbar(
            orientation=Gtk.Orientation.VERTICAL, adjustment=self.vadjustment
        )
        self._scrollbar.add_css_class("overlay-indicator")
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        box.set_hexpand(True)
        box.set_vexpand(True)
        box.append(self.view)
        box.append(self._scrollbar)
        # The padding shows the box, so it takes the terminal's background.
        self._css_class = f"sshpilot-gtkghostty-{next(_instance_ids)}"
        box.add_css_class(self._css_class)
        self._css = Gtk.CssProvider()
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, self._css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        self.widget = box
        #: Where TerminalWidget attaches its key controllers: on the terminal
        #: itself, so they run ahead of its key handling, as on VTE.
        self.controller_widget = self.view

        # Callbacks registered through connect_* (TerminalWidget's handlers
        # take VTE's shapes, so they are called here rather than connected
        # to the widget's own signals).
        self._handler_ids = itertools.count(1)
        self._callbacks: Dict[str, Dict[int, Callable[..., Any]]] = {
            kind: {} for kind in ("commit", "size", "title", "contents", "exited", "selection")
        }
        self._widget_handlers = [
            self.view.connect("input", self._on_view_bytes),
            self.view.connect("reply", self._on_view_bytes),
            self.view.connect("size-changed", self._on_view_size),
            self.view.connect("notify::title", self._on_view_title),
            self.view.connect("contents-changed", lambda _v: self._emit("contents")),
            self.view.connect("selection-changed", lambda _v: self._emit("selection")),
        ]

        self._local = None
        self._local_lock = threading.Lock()
        self._local_pending: Deque[bytes] = deque()
        self._local_scheduled = False
        self._local_exit_status: Optional[int] = None
        self._selection_from_search = False
        self._initialized = False

    # -- callback plumbing ---------------------------------------------------------

    def _add(self, kind: str, callback: Callable[..., Any]) -> Tuple[str, int]:
        handler = next(self._handler_ids)
        self._callbacks[kind][handler] = callback
        return (kind, handler)

    def _emit(self, kind: str, *args: Any) -> None:
        for callback in list(self._callbacks[kind].values()):
            try:
                callback(self, *args)
            except Exception:
                logger.debug("gtkghostty %s callback failed", kind, exc_info=True)

    def _on_view_bytes(self, _view, data: GLib.Bytes) -> None:
        payload = bytes(data.get_data() or b"")
        if payload:
            self._send(payload)

    def _send(self, payload: bytes) -> None:
        local = self._local
        if local is not None:
            local.write(payload)
            return
        self._emit("commit", payload, len(payload))

    def _on_view_size(self, _view, columns: int, rows: int, _cw: int, _ch: int) -> None:
        if self._local is not None:
            self._local.resize(columns, rows)
        self._emit("size", columns, rows)

    def _on_view_title(self, _view, _pspec) -> None:
        self._emit("title")

    # -- lifecycle -------------------------------------------------------------

    def initialize(self) -> None:
        self._initialized = True

    def destroy(self) -> None:
        self._close_local()
        for handler in self._widget_handlers:
            try:
                self.view.disconnect(handler)
            except Exception:
                pass
        self._widget_handlers = []
        for callbacks in self._callbacks.values():
            callbacks.clear()
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.remove_provider_for_display(display, self._css)

    def configure(self, settings: Optional[Mapping[str, Any]] = None) -> None:
        settings = settings or {}
        self.set_cursor_options(settings.get("cursor_shape"), settings.get("cursor_blink"))
        scrollback = settings.get("scrollback_lines")
        if scrollback is not None:
            try:
                self.view.set_scrollback_lines(max(0, int(scrollback)))
            except (TypeError, ValueError):
                pass

    def set_cursor_options(self, shape: Any = None, blink: Any = None) -> None:
        self.view.set_cursor_shape(_CURSOR_SHAPES[normalize_cursor_shape(shape)])
        mode = normalize_cursor_blink(blink)
        if mode == "system":
            settings = Gtk.Settings.get_default()
            on = bool(settings.props.gtk_cursor_blink) if settings is not None else True
        else:
            on = mode == "on"
        self.view.set_cursor_blink(on)

    def apply_theme(self, theme_name: Optional[str] = None) -> None:
        fg, bg, cursor, palette, font = resolve_theme(self.owner, theme_name)
        self.view.set_colors(_rgba(fg), _rgba(bg), _rgba(cursor), [_rgba(c) for c in palette])
        self._css.load_from_string(
            ".%s { background-color: rgb(%d, %d, %d); }"
            % (self._css_class, *(round(c * 255) for c in bg))
        )
        highlighter = getattr(self.owner, "_highlighter", None)
        if highlighter is not None:
            highlighter.set_theme(bg, palette)
        if font:
            self.view.set_font_desc(Pango.FontDescription.from_string(font))

    def set_font(self, font_desc: Pango.FontDescription) -> None:
        self.view.set_font_desc(font_desc)

    def grab_focus(self) -> None:
        self.view.grab_focus()

    def queue_draw(self) -> None:
        self.view.queue_draw()

    def show(self) -> None:
        self.widget.set_visible(True)

    # -- process ownership -----------------------------------------------------------

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

        As in the built-in backend: input goes straight to the process and
        its output straight to the widget; ``callback`` gets VTE's
        ``(terminal, pid, error, user_data)``.
        """

        from .local_shell_pty import LocalShellProcess

        if self._local is not None:
            raise TerminalBackendCapabilityError("This terminal already runs a local shell")
        process = LocalShellProcess(
            on_output=self._queue_local_output,
            on_exit=lambda status: GLib.idle_add(self._on_local_exit, process, status),
        )
        self._prepare_for_stream()
        try:
            pid = process.start(
                list(argv or ()),
                env=env,
                cwd=cwd,
                columns=self.view.get_columns(),
                rows=self.view.get_rows(),
            )
        except Exception as exc:  # noqa: BLE001 - reported through the callback
            logger.warning("Local shell spawn failed: %s", exc)
            error = exc
            if callback is not None:
                GLib.idle_add(lambda: (callback(self, -1, error, user_data), False)[1])
            return
        self._local = process
        logger.info("Local shell started on the gtkghostty backend pid=%s", pid)
        if callback is not None:
            GLib.idle_add(lambda: (callback(self, pid, None, user_data), False)[1])

    def _queue_local_output(self, data: bytes) -> None:
        """Reader thread: queue output and wake the main loop once."""
        with self._local_lock:
            self._local_pending.append(data)
            if self._local_scheduled:
                return
            self._local_scheduled = True
        GLib.idle_add(self._drain_local_output)

    def _drain_local_output(self) -> bool:
        budget = self.output_slice_bytes
        chunks: List[bytes] = []
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
            self.view.feed_data(b"".join(chunks))
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
        process, self._local = self._local, None
        if process is not None:
            process.close()
        logger.info("Local shell exited status=%s", status)
        self._emit("exited", status)

    def _close_local(self) -> None:
        process, self._local = self._local, None
        if process is not None:
            process.close()

    def _prepare_for_stream(self) -> None:
        # Under ConPTY (Windows: local shells, and every daemon session),
        # conhost repaints its viewport after a resize, so history pulled back
        # on a grow would be painted over; and it passes a program's kitty
        # keyboard request through but turns kitty-encoded keys into text.
        rendered = daemon_output_is_rendered()
        self.view.set_pull_history_on_grow(not rendered)
        self.view.set_legacy_keyboard(rendered)

    def adopt_pty(self, master_fd: int, watch_pid: int) -> None:
        raise TerminalBackendCapabilityError("The gtkghostty backend does not adopt PTYs")

    def prepare_pty_less_emulation(self) -> None:
        """Adapt the widget to the daemon's stream (daemon paths only)."""
        self._prepare_for_stream()

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
        return self._add("exited", callback)

    def connect_title_changed(self, callback: Callable[..., None]) -> Any:
        return self._add("title", callback)

    def get_window_title(self) -> Optional[str]:
        """For title callbacks, which get the backend as their terminal."""
        return self.view.get_title()

    def connect_termprops_changed(self, callback: Callable[..., None]) -> Optional[Any]:
        return None

    def connect_commit(self, callback: Callable[..., None]) -> Optional[Any]:
        return self._add("commit", callback)

    def connect_size_changed(self, callback: Callable[..., None]) -> Optional[Any]:
        handler = self._add("size", callback)
        self.invalidate_size_tracking()
        return handler

    def invalidate_size_tracking(self) -> None:
        # Redeliver the current grid once layout settles (GH #1164).
        def redeliver() -> bool:
            self._emit("size", self.view.get_columns(), self.view.get_rows())
            return False

        self.view.queue_resize()
        GLib.idle_add(redeliver)

    def connect_content_changed(self, callback: Callable[..., None]) -> Optional[Any]:
        return self._add("contents", callback)

    def disconnect(self, handler_id: Any) -> None:
        if isinstance(handler_id, tuple) and len(handler_id) == 2:
            kind, handler = handler_id
            self._callbacks.get(kind, {}).pop(handler, None)

    # -- I/O -----------------------------------------------------------------------

    def feed(self, data: bytes) -> None:
        if data:
            self.view.feed_data(bytes(data))

    def feed_child(self, data: bytes) -> None:
        self.feed_child_data(data)

    def feed_child_data(self, data: bytes) -> None:
        if data:
            self._send(bytes(data))

    def get_size(self) -> tuple:
        return (max(1, self.view.get_rows()), max(1, self.view.get_columns()))

    def get_content(self, max_chars: Optional[int] = None) -> Optional[str]:
        content = self.view.get_text()
        if content and max_chars and len(content) > max_chars:
            return content[-max_chars:]
        return content

    def save_contents(self, stream: Any) -> None:
        stream.write_all((self.view.get_text() + "\n").encode("utf-8"), None)

    def reset(self, clear_scrollback: bool, clear_screen: bool) -> None:
        # RIS clears history too; a soft reset (DECSTR) keeps it.
        if clear_scrollback:
            self.view.feed_data(b"\x1bc")
        else:
            self.view.feed_data(b"\x1b[!p" + (b"\x1b[2J\x1b[H" if clear_screen else b""))

    def set_font_scale(self, scale: float) -> None:
        self.view.set_font_scale(float(scale))

    def get_font_scale(self) -> float:
        return self.view.get_font_scale()

    # -- clipboard -----------------------------------------------------------------

    def copy_clipboard(
        self,
        format: str = "text",
        on_complete: Optional[Callable[[bool], None]] = None,
    ) -> None:
        text = self.view.get_selected_text() or ""
        copied = False
        reason = "empty-selection"
        if text:
            try:
                self.view.get_clipboard().set(text)
                copied, reason = True, "written"
            except Exception:
                reason = "exception"
                logger.debug("Failed to copy gtkghostty selection", exc_info=True)
        _log_clipboard_copy(
            backend="gtkghostty", copied=copied, reason=reason, text_len=len(text), format=format
        )
        if on_complete is not None:
            on_complete(copied)

    def get_has_selection(self) -> bool:
        return bool(self.view.has_selection())

    def paste_clipboard(self) -> None:
        self.view.paste_clipboard()

    def select_all(self) -> None:
        self.view.select_all()

    def selection_change_is_copyable(self) -> bool:
        return not self._selection_from_search

    # -- search --------------------------------------------------------------------

    def search_set_regex(self, regex: Optional[Any]) -> None:
        if regex is None:
            self.view.search_set_pattern(None, False, False)

    def search_set_query(
        self,
        term: Optional[str],
        *,
        case_sensitive: bool = False,
        regex: bool = False,
    ) -> None:
        self.view.search_set_pattern(term or None, bool(regex), bool(case_sensitive))

    def _search(self, *, backwards: bool) -> bool:
        self._selection_from_search = True
        try:
            return bool(self.view.search_find(backwards))
        finally:
            self._selection_from_search = False

    def search_find_next(self) -> bool:
        return self._search(backwards=False)

    def search_find_previous(self) -> bool:
        return self._search(backwards=True)

    def clear_search_decorations(self) -> None:
        return None

    def set_search_highlight(self, active: bool) -> None:
        return None

    # -- links, menus, encodings ---------------------------------------------------------

    def setup_link_handling(self, motion_callback=None, enter_callback=None,
                            selection_callback=None, hover_callback=None) -> None:
        if selection_callback is not None and not self._callbacks["selection"]:
            self._add("selection", lambda _backend: selection_callback(self.view))

    def setup_native_context_menu(self, menu: Any, callback: Callable) -> bool:
        return False

    def clear_native_context_menu(self) -> None:
        return None

    def _view_point(self, x: float, y: float) -> Tuple[float, float]:
        # Callers measure from the backend box; the grid starts at the padding.
        return x - _PAD_X, y - _PAD_Y

    def hyperlink_at(self, x: float, y: float) -> Optional[str]:
        """OSC 8 first, then a plain-text link, as the VTE backend does."""
        vx, vy = self._view_point(x, y)
        uri = self.view.hyperlink_at(vx, vy)
        if uri:
            return uri
        return self.plain_text_link_at(x, y)

    def plain_text_link_at(self, x: float, y: float) -> Optional[str]:
        vx, vy = self._view_point(x, y)
        try:
            text, column = self.view.get_line_at(vx, vy)
        except Exception:
            return None
        return url_at(text or "", int(column))

    def set_pointer_over_link(self, over_link: bool) -> None:
        self.view.set_cursor_from_name("pointer" if over_link else "text")

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

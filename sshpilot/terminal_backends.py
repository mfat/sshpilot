"""Terminal backend abstractions for sshPilot."""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GObject, Gtk


logger = logging.getLogger(__name__)

# CSS absolute units: 1pt = 1/72in, 1px = 1/96in → 1pt = 96/72 px.
# Pango/VTE and the Preferences font preview use points; xterm.js fontSize is CSS px.
_PT_TO_CSS_PX = 96.0 / 72.0


def pango_points_to_xterm_px(points: float, scale: float = 1.0) -> int:
    """Convert a Pango/CSS point size to an xterm.js ``fontSize`` (CSS pixels).

    Preferences ▸ Terminal font preview uses ``font-size: Npt``; without this
    conversion, PyXterm treated ``N`` as pixels and rendered ~25% smaller than
    the preview (and than VTE at the same setting).
    """
    return max(1, int(round(float(points) * _PT_TO_CSS_PX * float(scale))))


class BaseTerminalBackend(Protocol):
    """Protocol describing the behaviour a terminal backend must implement."""

    widget: Gtk.Widget

    def initialize(self) -> None:
        """Perform backend specific initialisation and register internal state."""

    def destroy(self) -> None:
        """Release backend resources."""

    def apply_theme(self, theme_name: Optional[str] = None) -> None:
        """Apply the current theme to the terminal widget."""

    def grab_focus(self) -> None:
        """Give keyboard focus to the terminal widget."""

    def spawn_async(
        self,
        argv: Sequence[str],
        env: Optional[Mapping[str, str]] = None,
        cwd: Optional[str] = None,
        flags: int = 0,
        child_setup: Optional[Callable[..., None]] = None,
        callback: Optional[Callable[[GObject.Object, Optional[Exception]], None]] = None,
        user_data: Optional[Any] = None,
    ) -> None:
        """Spawn a new process attached to the backend terminal."""

    def connect_child_exited(self, callback: Callable[[Gtk.Widget, int], None]) -> Any:
        """Connect to the child exited signal for the backend."""

    def connect_title_changed(self, callback: Callable[[Gtk.Widget, str], None]) -> Any:
        """Connect to a signal emitted when the terminal title changes."""

    def connect_termprops_changed(self, callback: Callable[..., None]) -> Optional[Any]:
        """Connect to the termprops-changed signal if supported."""

    def disconnect(self, handler_id: Any) -> None:
        """Disconnect a previously registered signal handler."""

    def copy_clipboard(self) -> None:
        """Copy the current terminal selection to the clipboard."""

    def get_has_selection(self) -> bool:
        """Whether the terminal currently has a text selection."""

    def paste_clipboard(self) -> None:
        """Paste clipboard contents into the terminal."""

    def select_all(self) -> None:
        """Select all content in the terminal."""

    def reset(self, clear_scrollback: bool, clear_screen: bool) -> None:
        """Reset the terminal content."""

    def set_font_scale(self, scale: float) -> None:
        """Set the font scale for the terminal."""

    def get_font_scale(self) -> float:
        """Return the current font scale."""

    def feed(self, data: bytes) -> None:
        """Feed raw data to the terminal display."""

    def search_set_regex(self, regex: Optional[Any]) -> None:
        """Configure the search regex for the backend, if supported."""

    def search_set_query(
        self,
        term: Optional[str],
        *,
        case_sensitive: bool = False,
        regex: bool = False,
    ) -> None:
        """Set the user-facing search term and options (preferred over search_set_regex)."""

    def search_find_next(self) -> bool:
        """Search forward using the configured regex."""

    def search_find_previous(self) -> bool:
        """Search backwards using the configured regex."""

    def clear_search_decorations(self) -> None:
        """Clear search match decorations without necessarily clearing the query."""

    def get_child_pid(self) -> Optional[int]:
        """Return the PID of the running child process if available."""

    def get_child_pgid(self) -> Optional[int]:
        """Return the process group id of the running child process if available."""

    def supports_feature(self, feature: str) -> bool:
        """Return True if the backend supports the given feature name."""

    def get_pty(self) -> Optional[Any]:
        """Return the PTY instance if the backend exposes one."""

    def set_font(self, font_desc: "Pango.FontDescription") -> None:
        """Set the font for the backend if supported."""

    def queue_draw(self) -> None:
        """Queue a redraw of the backend widget if supported."""

    def show(self) -> None:
        """Ensure the backend widget is visible."""

    def feed_child(self, data: bytes) -> None:
        """Feed raw bytes to the child process input if supported."""

    def get_content(self, max_chars: Optional[int] = None) -> Optional[str]:
        """Return the terminal contents if the backend can provide it."""



from typing import TYPE_CHECKING

from gi.repository import Gdk, GLib, Pango, Vte

if TYPE_CHECKING:  # pragma: no cover - import only for type checking
    from .terminal import TerminalWidget


class VTETerminalBackend:
    """VTE based terminal backend."""

    def __init__(self, owner: "TerminalWidget") -> None:
        self.owner = owner
        self.vte = Vte.Terminal()
        self.widget = self.vte
        self._termprops_handler: Optional[int] = None

    def initialize(self) -> None:
        self.vte.set_hexpand(True)
        self.vte.set_vexpand(True)

        font_desc = Pango.FontDescription()
        font_desc.set_family("Monospace")
        font_desc.set_size(12 * Pango.SCALE)
        self.vte.set_font(font_desc)

        try:
            self.vte.set_cursor_blink_mode(Vte.CursorBlinkMode.ON)
            self.vte.set_cursor_shape(Vte.CursorShape.BLOCK)
        except Exception:
            logger.debug("Failed to set cursor properties", exc_info=True)

        try:
            self.vte.set_scrollback_lines(10000)
        except Exception:
            logger.debug("Failed to set scrollback lines", exc_info=True)

        try:
            if hasattr(self.vte, "set_scroll_on_keystroke"):
                self.vte.set_scroll_on_keystroke(True)
            if hasattr(self.vte, "set_scroll_on_output"):
                self.vte.set_scroll_on_output(False)
        except Exception:
            logger.debug("Failed to set scroll behavior", exc_info=True)

        try:
            if hasattr(self.vte, "set_word_char_exceptions"):
                self.vte.set_word_char_exceptions("@-./_~")
            elif hasattr(self.vte, "set_word_char_options"):
                self.vte.set_word_char_options("@-./_~")
        except Exception:
            logger.debug("Failed to set word char options", exc_info=True)

        try:
            cursor_color = Gdk.RGBA()
            cursor_color.parse("black")
            if hasattr(self.vte, "set_color_cursor"):
                self.vte.set_color_cursor(cursor_color)

            if hasattr(self.vte, "set_color_highlight"):
                highlight_bg = Gdk.RGBA()
                highlight_bg.parse("#4A90E2")
                self.vte.set_color_highlight(highlight_bg)

                highlight_fg = Gdk.RGBA()
                highlight_fg.parse("white")
                if hasattr(self.vte, "set_color_highlight_foreground"):
                    self.vte.set_color_highlight_foreground(highlight_fg)
        except Exception:
            logger.debug("Failed to set cursor and highlight colors", exc_info=True)

        if hasattr(self.vte, "set_mouse_autohide"):
            try:
                self.vte.set_mouse_autohide(True)
            except Exception:
                logger.debug("Failed to enable mouse autohide", exc_info=True)

        try:
            self.vte.set_encoding("UTF-8")
        except Exception:
            logger.debug("Failed to set encoding", exc_info=True)

        try:
            if hasattr(self.vte, "set_allow_bold"):
                self.vte.set_allow_bold(True)
        except Exception:
            logger.debug("Failed to enable bold text", exc_info=True)

        try:
            self.vte.show()
        except Exception:
            logger.debug("Failed to show VTE widget", exc_info=True)

    def destroy(self) -> None:
        try:
            if self._termprops_handler is not None:
                self.vte.disconnect(self._termprops_handler)  # type: ignore[arg-type]
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------
    def apply_theme(self, theme_name: Optional[str] = None) -> None:  # type: ignore[override]
        owner = self.owner
        try:
            if theme_name is None and owner.config:
                theme_name = owner.config.get_setting("terminal.theme", "default")

            if owner.config:
                profile = owner.config.get_terminal_profile(theme_name)
            else:
                profile = {
                    "foreground": "#000000",
                    "background": "#FFFFFF",
                    "font": "Monospace 12",
                    "cursor_color": "#000000",
                    "highlight_background": "#4A90E2",
                    "highlight_foreground": "#FFFFFF",
                    "palette": [
                        "#000000",
                        "#CC0000",
                        "#4E9A06",
                        "#C4A000",
                        "#3465A4",
                        "#75507B",
                        "#06989A",
                        "#D3D7CF",
                        "#555753",
                        "#EF2929",
                        "#8AE234",
                        "#FCE94F",
                        "#729FCF",
                        "#AD7FA8",
                        "#34E2E2",
                        "#EEEEEC",
                    ],
                }

            fg_color = Gdk.RGBA()
            fg_color.parse(profile["foreground"])

            bg_color = Gdk.RGBA()
            bg_color.parse(profile["background"])

            cursor_color = Gdk.RGBA()
            cursor_color.parse(profile.get("cursor_color", profile["foreground"]))

            highlight_bg = Gdk.RGBA()
            highlight_bg.parse(profile.get("highlight_background", "#4A90E2"))

            highlight_fg = Gdk.RGBA()
            highlight_fg.parse(profile.get("highlight_foreground", profile["foreground"]))

            # Handle group color override if enabled
            override_rgba = None
            if hasattr(owner, '_get_group_color_rgba'):
                override_rgba = owner._get_group_color_rgba()
            
            use_group_color = False
            if owner.config:
                try:
                    use_group_color = bool(
                        owner.config.get_setting('ui.use_group_color_in_terminal', False)
                    )
                except Exception:
                    use_group_color = False

            if use_group_color and override_rgba is not None:
                bg_color = self._clone_rgba(override_rgba)  # Use exact group color
                fg_color = self._get_contrast_color(bg_color)
                highlight_bg = self._clone_rgba(override_rgba)
                highlight_fg = self._get_contrast_color(highlight_bg)
                cursor_color = self._clone_rgba(highlight_fg)

            palette_colors = None
            if profile.get("palette"):
                palette_colors = []
                for color_hex in profile["palette"]:
                    color = Gdk.RGBA()
                    if color.parse(color_hex):
                        palette_colors.append(color)
                    else:
                        fallback = Gdk.RGBA()
                        fallback.parse("#000000")
                        palette_colors.append(fallback)

                while len(palette_colors) < 16:
                    fallback = Gdk.RGBA()
                    fallback.parse("#000000")
                    palette_colors.append(fallback)
                palette_colors = palette_colors[:16]

            self.vte.set_colors(fg_color, bg_color, palette_colors)
            self.vte.set_color_cursor(cursor_color)
            self.vte.set_color_highlight(highlight_bg)
            self.vte.set_color_highlight_foreground(highlight_fg)

            try:
                rgba = bg_color
                provider = Gtk.CssProvider()
                css = (
                    f".terminal-bg {{ background-color: rgba({int(rgba.red * 255)},"
                    f" {int(rgba.green * 255)}, {int(rgba.blue * 255)}, {rgba.alpha}); }}"
                )
                provider.load_from_data(css.encode("utf-8"))
                display = Gdk.Display.get_default()
                if display:
                    Gtk.StyleContext.add_provider_for_display(
                        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                    )
                if hasattr(owner, "add_css_class"):
                    owner.add_css_class("terminal-bg")
                if hasattr(owner.scrolled_window, "add_css_class"):
                    owner.scrolled_window.add_css_class("terminal-bg")
                if hasattr(self.vte, "add_css_class"):
                    self.vte.add_css_class("terminal-bg")
            except Exception:
                logger.debug("Failed to set container background", exc_info=True)

            font_desc = Pango.FontDescription.from_string(profile["font"])
            self.vte.set_font(font_desc)
            self.vte.queue_draw()
        except Exception:
            logger.error("Failed to apply terminal theme", exc_info=True)

    def _clone_rgba(self, rgba: Gdk.RGBA) -> Gdk.RGBA:
        """Clone an RGBA color"""
        clone = Gdk.RGBA()
        clone.red = rgba.red
        clone.green = rgba.green
        clone.blue = rgba.blue
        clone.alpha = rgba.alpha
        return clone

    def _get_contrast_color(self, background: Gdk.RGBA) -> Gdk.RGBA:
        """Get a contrasting color for the given background"""
        luminance = self._relative_luminance(background)
        contrast = Gdk.RGBA()
        if luminance > 0.5:
            contrast.parse("#000000")  # Use black on light backgrounds
        else:
            contrast.parse("#FFFFFF")  # Use white on dark backgrounds
        return contrast

    def _relative_luminance(self, rgba: Gdk.RGBA) -> float:
        """Calculate relative luminance of a color"""
        def to_linear(channel: float) -> float:
            if channel <= 0.03928:
                return channel / 12.92
            return ((channel + 0.055) / 1.055) ** 2.4

        r_lin = to_linear(rgba.red)
        g_lin = to_linear(rgba.green)
        b_lin = to_linear(rgba.blue)
        return 0.2126 * r_lin + 0.7152 * g_lin + 0.0722 * b_lin

    def set_font(self, font_desc: Pango.FontDescription) -> None:
        try:
            self.vte.set_font(font_desc)
        except Exception:
            logger.debug("Failed to set font on VTE backend", exc_info=True)

    def grab_focus(self) -> None:
        try:
            self.vte.grab_focus()
        except Exception:
            logger.debug("Failed to grab focus for VTE backend", exc_info=True)

    def spawn_async(
        self,
        argv: Sequence[str],
        env: Optional[Mapping[str, str]] = None,
        cwd: Optional[str] = None,
        flags: int = 0,
        child_setup: Optional[Callable[..., None]] = None,
        callback: Optional[Callable[[GObject.Object, Optional[Exception]], None]] = None,
        user_data: Optional[Any] = None,
    ) -> None:
        env_list: Optional[list[str]] = None
        if env is not None:
            env_list = [f"{key}={value}" for key, value in env.items()]
        cwd = cwd or None
        pty_flags = Vte.PtyFlags(flags) if flags else Vte.PtyFlags.DEFAULT
        self.vte.spawn_async(
            pty_flags,
            cwd,
            list(argv),
            env_list,
            GLib.SpawnFlags.DEFAULT,
            child_setup,
            user_data,
            -1,
            None,
            callback,
            (),
        )

    def connect_child_exited(self, callback: Callable[[Gtk.Widget, int], None]) -> Any:
        return self.vte.connect("child-exited", callback)

    def connect_title_changed(self, callback: Callable[[Gtk.Widget, str], None]) -> Any:
        return self.vte.connect("window-title-changed", callback)

    def connect_termprops_changed(self, callback: Callable[..., None]) -> Optional[Any]:
        try:
            self._termprops_handler = self.vte.connect("termprops-changed", callback)
        except Exception:
            self._termprops_handler = None
        return self._termprops_handler

    def disconnect(self, handler_id: Any) -> None:
        try:
            if handler_id:
                self.vte.disconnect(handler_id)
        except Exception:
            logger.debug("Failed to disconnect VTE handler", exc_info=True)

    def queue_draw(self) -> None:
        try:
            self.vte.queue_draw()
        except Exception:
            logger.debug("Failed to queue draw on VTE backend", exc_info=True)

    def show(self) -> None:
        try:
            self.vte.show()
        except Exception:
            logger.debug("Failed to show VTE widget", exc_info=True)

    # ------------------------------------------------------------------
    # Clipboard helpers
    # ------------------------------------------------------------------
    def copy_clipboard(self) -> None:
        if self.vte.get_has_selection():
            self.vte.copy_clipboard_format(Vte.Format.TEXT)

    def get_has_selection(self) -> bool:
        return bool(self.vte.get_has_selection())

    def paste_clipboard(self) -> None:
        self.vte.paste_clipboard()

    def select_all(self) -> None:
        self.vte.select_all()

    def reset(self, clear_scrollback: bool, clear_screen: bool) -> None:
        self.vte.reset(clear_scrollback, clear_screen)

    def set_font_scale(self, scale: float) -> None:
        self.vte.set_font_scale(scale)

    def get_font_scale(self) -> float:
        return self.vte.get_font_scale()

    def feed(self, data: bytes) -> None:
        self.vte.feed(data)

    def feed_child(self, data: bytes) -> None:
        try:
            self.vte.feed_child(data)
        except Exception:
            logger.debug("Failed to feed child on VTE backend", exc_info=True)

    def get_content(self, max_chars: Optional[int] = None) -> Optional[str]:
        content = None
        # Modern API (VTE 0.76+). get_text_range is deprecated and yields no text
        # on current VTE, which blinded callers like the connect-evidence poller.
        try:
            if hasattr(self.vte, "get_text_format"):
                content = self.vte.get_text_format(Vte.Format.TEXT)
        except Exception:
            content = None
        if content is None:
            try:
                if hasattr(self.vte, "get_text_range_format"):
                    rows = self.vte.get_row_count()
                    res = self.vte.get_text_range_format(Vte.Format.TEXT, 0, 0, rows, -1)
                    content = res[0] if res else None
            except Exception:
                content = None
        if content is None:
            # Legacy fallback for very old VTE that predates the *_format APIs.
            try:
                res = self.vte.get_text_range(0, 0, -1, -1, lambda *args: True)
                content = res[0] if res else None
            except Exception:
                logger.debug("Failed to read VTE content", exc_info=True)
                content = None
        if content and max_chars and len(content) > max_chars:
            return content[-max_chars:]
        return content

    def search_set_regex(self, regex: Optional[Any]) -> None:
        if regex is None:
            self.vte.search_set_regex(None, 0)
        else:
            self.vte.search_set_regex(regex, 0)

    def search_set_query(
        self,
        term: Optional[str],
        *,
        case_sensitive: bool = False,
        regex: bool = False,
    ) -> None:
        import re as _re
        if not term:
            self.vte.search_set_regex(None, 0)
            return
        pattern = term if regex else _re.escape(term)
        if not case_sensitive and not pattern.startswith("(?i)"):
            pattern = "(?i)" + pattern
        self.vte.search_set_regex(Vte.Regex.new_for_search(pattern, -1, 0), 0)
        if hasattr(self.vte, "search_set_wrap_around"):
            self.vte.search_set_wrap_around(True)

    def search_find_next(self) -> bool:
        return self.vte.search_find_next()

    def search_find_previous(self) -> bool:
        return self.vte.search_find_previous()

    def clear_search_decorations(self) -> None:
        # VTE highlight colors are restored by TerminalWidget on overlay hide.
        return None

    def get_child_pid(self) -> Optional[int]:
        try:
            return self.vte.get_current_process_id()
        except Exception:
            return None

    def get_child_pgid(self) -> Optional[int]:
        pid = self.get_child_pid()
        if pid is None:
            return None
        try:
            return os.getpgid(pid)
        except Exception:
            return None

    def supports_feature(self, feature: str) -> bool:
        supported = {
            "search",
            "font-scaling",
            "clipboard",
            "termprops",
        }
        return feature in supported

    def get_pty(self) -> Optional[Any]:
        try:
            return self.vte.get_pty()
        except Exception:
            return None



class PyXtermTerminalBackend:
    """pyxterm.js based backend.

    The implementation degrades gracefully when the optional dependencies are
    missing. The caller should check the :attr:`available` attribute before
    using the backend and fall back to :class:`VTETerminalBackend` when it is
    ``False``.
    """

    def __init__(self, owner: "TerminalWidget") -> None:
        self.owner = owner
        self.available = False
        self.import_error: Optional[Exception] = None
        self._pyxterm = None
        self._vendored_pyxterm = None
        self._webview = None
        self._terminal_id: Optional[str] = None
        self._child_pid: Optional[int] = None
        self._child_exited_callback: Optional[Callable] = None
        self._font_scale: float = 1.0
        self._base_font_size: Optional[float] = None  # Base size in points for zoom
        self._search_addon_loaded = False  # Track if search addon is loaded
        self._current_search_term: Optional[str] = None  # Current search term
        self._current_search_is_regex: bool = False  # Whether current search is regex
        self._current_search_case_sensitive: bool = False  # Whether current search is case sensitive
        self._pending_spawn_callback: Optional[Callable] = None  # Store callback until WebView is ready
        self._pending_spawn_user_data: Optional[Any] = None  # Store user_data for callback

        # Initialize with a fallback widget
        self.widget: Gtk.Widget = Gtk.Box()

        try:
            # The embedded backend needs only WebKit 6 + the xterm.js assets (served
            # by xterm_shell); it does NOT import the old Flask/WebSocket server, so
            # the app carries no runtime dependency on flask/simple-websocket.
            self._vendored_pyxterm = None
            pyxterm_module = None

            # Use WebKit 6.0 (GTK4 compatible) - this is the preferred approach
            try:
                gi.require_version("WebKit", "6.0")
                from gi.repository import WebKit
                self.WebKit = WebKit
                # Factory hook: PyXtermBridgeBackend overrides this to construct the
                # WebView with a UserContentManager (JS->Python bridge). Base impl is
                # a plain WebView.
                self._webview = self._make_webview_webkit6(WebKit)

                # Configure WebView settings to ensure JavaScript is enabled
                # According to WebKit 6.0 API, JavaScript is enabled by default,
                # but we explicitly set it for clarity
                try:
                    settings = self._webview.get_settings()
                    if settings:
                        settings.set_property('enable-javascript', True)
                        logger.debug("WebView JavaScript enabled via settings")
                except Exception as settings_error:
                    logger.debug(f"Could not configure WebView settings (may be enabled by default): {settings_error}")
                
                logger.debug("Using WebKit 6.0 (GTK4 compatible)")
            except Exception as webkit6_error:
                logger.debug(f"WebKit 6.0 not available: {webkit6_error}")
                
                # Check if GTK 4.0 is already loaded (which conflicts with WebKit2)
                if hasattr(Gtk, 'get_major_version') and Gtk.get_major_version() == 4:
                    raise ImportError("PyXterm backend requires WebKit 6.0 for GTK 4.0 compatibility, but WebKit 6.0 is not available") from webkit6_error
                
                # Fall back to WebKit2 4.0 (only if GTK 3.0 is available)
                gi.require_version("WebKit2", "4.0")
                from gi.repository import WebKit2
                self.WebKit2 = WebKit2
                self._webview = WebKit2.WebView()
                logger.debug("Using WebKit2 4.0 (GTK3 compatible)")
            
            # Disable WebView's native context menu. Claiming a GTK gesture does NOT
            # stop WebKit's built-in menu (WebKit handles it internally); the reliable
            # way is the WebView's own ``context-menu`` signal — returning True
            # suppresses the default menu so only sshPilot's custom menu shows.
            try:
                self._webview.connect("context-menu", lambda *args: True)
                logger.debug("Suppressed WebView native context menu via context-menu signal")
            except Exception as e:
                logger.debug(f"Failed to disable WebView native context menu: {e}", exc_info=True)

        except Exception as exc:  # pragma: no cover - optional dependency
            self.import_error = exc
            logger.debug("PyXterm backend unavailable", exc_info=True)
            return

        self._pyxterm = pyxterm_module
        self.widget = self._webview
        self.available = True

    def _make_webview_webkit6(self, WebKit):
        """Construct the WebKit 6 WebView. Overridden by the embedded backend to
        attach a UserContentManager for the JS->Python bridge."""
        return WebKit.WebView()

    # The pyxterm backend exposes only a subset of the behaviour for now. Each
    # method contains guards so the widget can fall back cleanly.

    def initialize(self) -> None:
        if not self.available:
            return
        self.widget.set_hexpand(True)
        self.widget.set_vexpand(True)

    def destroy(self) -> None:
        # Embedded backend: nothing server-side to tear down. Subclasses
        # (PyXtermBridgeBackend) close their PTY bridge before calling super().
        pass

    def _run_javascript(self, script: str) -> None:
        """Execute JavaScript in the WebView"""
        if not self.available or not self._webview:
            return
        try:
            # WebKit 6.0 (GTK4) - uses evaluate_javascript
            if hasattr(self, 'WebKit') and self.WebKit:
                try:
                    # WebKit 6.0 API: evaluate_javascript(script, length, world_name, source_uri, cancellable, callback, user_data)
                    def on_js_finished(webview, result, user_data):
                        try:
                            # Finish the async operation (may raise exception if JS failed)
                            if hasattr(webview, 'evaluate_javascript_finish'):
                                webview.evaluate_javascript_finish(result)
                        except Exception as e:
                            logger.debug(f"JavaScript execution finished with error: {e}")
                    # Pass script length (len(script) for Python strings)
                    # According to WebKit 6.0 API, -1 works for auto-detect, but using actual length is more explicit
                    # PyGObject handles string conversion, so len(script) is appropriate
                    self._webview.evaluate_javascript(script, len(script), None, None, None, on_js_finished, None)
                except Exception as e:
                    logger.debug(f"Failed to evaluate JavaScript (WebKit 6.0): {e}", exc_info=True)
            # WebKit2 4.0 (GTK3) - uses run_javascript
            elif hasattr(self, 'WebKit2') and self.WebKit2:
                try:
                    def on_js_finished(webview, result, user_data):
                        try:
                            if hasattr(webview, 'run_javascript_finish'):
                                webview.run_javascript_finish(result)
                        except Exception as e:
                            logger.debug(f"JavaScript execution finished with error: {e}")
                    self._webview.run_javascript(script, None, on_js_finished, None)
                except Exception as e:
                    logger.debug(f"Failed to run JavaScript (WebKit2): {e}", exc_info=True)
            else:
                logger.debug("WebView does not support JavaScript execution")
        except Exception as e:
            logger.debug(f"Failed to execute JavaScript: {e}", exc_info=True)

    def apply_theme(self, theme_name: Optional[str] = None) -> None:  # type: ignore[override]
        """Apply theme to xterm.js terminal via JavaScript"""
        if not self.available:
            return
        
        owner = self.owner
        try:
            if theme_name is None and owner.config:
                theme_name = owner.config.get_setting("terminal.theme", "default")

            if owner.config:
                profile = owner.config.get_terminal_profile(theme_name)
            else:
                profile = {
                    "foreground": "#FFFFFF",
                    "background": "#000000",
                    "cursor_color": "#FFFFFF",
                    "highlight_background": "#4A90E2",
                    "highlight_foreground": "#FFFFFF",
                }

            # Convert colors to xterm.js theme format
            import json
            palette = profile.get("palette", [])
            palette_js = json.dumps(palette) if palette else "[]"
            
            # fit.fit() sizes the terminal to whole character cells; any leftover
            # strip at the bottom shows the page background. Keep html/body in
            # sync with the theme so that gap matches (VTE paints the full widget).
            bg = profile.get("background", "#000000")
            fg = profile.get("foreground", "#FFFFFF")
            cursor = profile.get("cursor_color", fg)
            sel_bg = profile.get("highlight_background", "#4A90E2")
            sel_fg = profile.get("highlight_foreground", fg)
            theme_js = f"""
            (function() {{
                if (typeof window.term !== 'undefined') {{
                    var theme = {{
                        background: {json.dumps(bg)},
                        foreground: {json.dumps(fg)},
                        cursor: {json.dumps(cursor)},
                        selectionBackground: {json.dumps(sel_bg)},
                        selectionForeground: {json.dumps(sel_fg)}
                    }};
                    // Apply palette colors if available
                    var palette = {palette_js};
                    if (palette && palette.length >= 16) {{
                        theme.black = palette[0];
                        theme.red = palette[1];
                        theme.green = palette[2];
                        theme.yellow = palette[3];
                        theme.blue = palette[4];
                        theme.magenta = palette[5];
                        theme.cyan = palette[6];
                        theme.white = palette[7];
                        theme.brightBlack = palette[8];
                        theme.brightRed = palette[9];
                        theme.brightGreen = palette[10];
                        theme.brightYellow = palette[11];
                        theme.brightBlue = palette[12];
                        theme.brightMagenta = palette[13];
                        theme.brightCyan = palette[14];
                        theme.brightWhite = palette[15];
                    }}
                    window.term.options.theme = theme;
                    var pageBg = theme.background || '#000000';
                    document.documentElement.style.background = pageBg;
                    document.body.style.background = pageBg;
                    var terminalEl = document.getElementById('terminal');
                    if (terminalEl) terminalEl.style.background = pageBg;
                }}
            }})();
            """
            self._run_javascript(theme_js)
            logger.debug(f"Applied theme {theme_name} to PyXterm backend")
        except Exception as e:
            logger.error(f"Failed to apply theme to PyXterm backend: {e}", exc_info=True)

    def set_font(self, font_desc: Pango.FontDescription) -> None:
        """Set font for xterm.js terminal using xterm.js options API"""
        if not self.available:
            return
        
        try:
            # Extract font family and size from Pango font description.
            # Pango size is in points (same unit as Preferences font preview CSS).
            font_family = font_desc.get_family() or "Monospace"
            font_size_pt = font_desc.get_size() / Pango.SCALE

            # Store base size in points for zoom (convert to px only when applying).
            if self._base_font_size is None or self._font_scale == 1.0:
                self._base_font_size = font_size_pt

            # xterm.js fontSize is CSS pixels, not points.
            scaled_font_size_px = pango_points_to_xterm_px(
                font_size_pt, self._font_scale
            )

            # Escape single quotes in font family for JavaScript
            font_family_escaped = font_family.replace("'", "\\'").replace('"', '\\"')

            # Use xterm.js options API to set font (per xterm.js documentation)
            # According to https://xtermjs.org/docs/api/terminal/classes/terminal/
            # We can set multiple options at once or individually
            # fontFamily and fontSize are Terminal options that can be set via term.options
            font_js = f"""
            (function() {{
                if (typeof window.term !== 'undefined') {{
                    // Set font options (can set individually or together)
                    window.term.options.fontFamily = '{font_family_escaped}';
                    window.term.options.fontSize = {scaled_font_size_px};
                    
                    // Use setTimeout to ensure font size change is applied before fitting
                    setTimeout(function() {{
                        // Call fit.fit() to properly resize terminal and maintain background area
                        // This ensures the colored background area doesn't resize incorrectly
                        if (typeof window.fit !== 'undefined' && window.fit.fit) {{
                            window.fit.fit();
                            // Trigger a resize event to ensure container recalculates
                            if (typeof window.dispatchEvent !== 'undefined') {{
                                window.dispatchEvent(new Event('resize'));
                            }}
                        }}
                    }}, 10);
                    
                    // Force a refresh to apply the changes
                    // refresh(start: number, end: number): void
                    if (window.term.rows > 0) {{
                        window.term.refresh(0, window.term.rows - 1);
                    }}
                }}
            }})();
            """
            self._run_javascript(font_js)
            logger.debug(
                "Set font to %s %spx (base: %spt, scale: %sx) for PyXterm backend",
                font_family,
                scaled_font_size_px,
                font_size_pt,
                self._font_scale,
            )
        except Exception as e:
            logger.debug(f"Failed to set font for PyXterm backend: {e}", exc_info=True)

    def grab_focus(self) -> None:
        """Give keyboard focus to the terminal widget and the xterm.js terminal inside it."""
        if not self.available:
            return
        try:
            # First focus the WebView
            self.widget.grab_focus()
            
            # Then use JavaScript to focus the xterm.js terminal inside the WebView
            # This ensures the terminal receives keyboard input
            focus_script = """
            (function() {
                if (window.term && window.term.focus) {
                    window.term.focus();
                    return true;
                }
                return false;
            })();
            """
            self._run_javascript(focus_script)
            logger.debug("Focused PyXterm.js terminal (WebView + xterm.js)")
        except Exception as e:
            logger.debug(f"Failed to focus pyxterm widget: {e}", exc_info=True)
    
    def grab_focus_with_js(self) -> None:
        """Special focus method for pyxtermjs - simplified approach"""
        if not self.available:
            logger.debug("Pyxtermjs backend not available")
            return
        try:
            logger.debug("Starting grab_focus_with_js (simplified)")
            # Just focus the WebView - the HTML template will handle terminal focus
            self.widget.grab_focus()
            logger.debug("WebView focused - terminal should auto-focus from HTML template")
        except Exception as e:
            logger.debug(f"Failed to focus pyxterm widget: {e}", exc_info=True)

    def _wrap_command_with_encoding(self, argv: Sequence[str], encoding: str) -> Sequence[str]:
        """
        Wrap command with encoding transcoder if needed.
        
        According to https://xtermjs.org/docs/guides/encoding/:
        - UTF-8 and UTF-16 are natively supported by xterm.js
        - Legacy encodings should be handled at PTY bridge level using luit or iconv
        
        Returns the command array, possibly wrapped with luit for legacy encodings.
        """
        # UTF-8 and UTF-16 are natively supported, no wrapper needed
        if encoding.upper() in ('UTF-8', 'UTF-16', 'UTF-16LE', 'UTF-16BE'):
            return argv
        
        # For legacy encodings, wrap with luit if available
        # Check if luit is available
        import shutil
        luit_path = shutil.which('luit')
        if luit_path:
            # Wrap command with luit for encoding transcoding
            # luit syntax: luit -encoding ENCODING -- command [args...]
            wrapped = [luit_path, '-encoding', encoding, '--'] + list(argv)
            logger.debug(f"Wrapping command with luit for encoding {encoding}: {wrapped}")
            return wrapped
        else:
            # luit not available, log warning but proceed
            # The encoding won't be transcoded, which may cause issues
            logger.warning(
                f"Encoding {encoding} requested but luit not found. "
                f"xterm.js will use UTF-8. Install luit for legacy encoding support."
            )
            return argv

    def connect_title_changed(self, callback: Callable[[Gtk.Widget, str], None]) -> Any:
        return None

    def connect_termprops_changed(self, callback: Callable[..., None]) -> Optional[Any]:
        return None

    def disconnect(self, handler_id: Any) -> None:
        # Handle PyXtermJS-specific handler IDs
        if handler_id == "pyxtermjs_child_exited":
            # Clear the callback for PyXtermJS
            self._child_exited_callback = None
        # No-op for other handlers, as the backend does not expose Gtk signal handlers.
        return

    def queue_draw(self) -> None:
        if self.widget:
            try:
                self.widget.queue_draw()
            except Exception:
                logger.debug("Failed to queue draw on pyxterm widget", exc_info=True)

    def show(self) -> None:
        if self.widget:
            try:
                if hasattr(self.widget, "set_visible"):
                    self.widget.set_visible(True)
                if hasattr(self.widget, "show"):
                    self.widget.show()
            except Exception:
                logger.debug("Failed to show pyxterm widget", exc_info=True)

    def _get_system_clipboard(self):
        """Return the GDK clipboard for this WebView (system clipboard)."""
        display = None
        if self._webview is not None:
            try:
                display = self._webview.get_display()
            except Exception:  # noqa: BLE001
                display = None
        if display is None:
            display = Gdk.Display.get_default()
        if display is None:
            return None
        return display.get_clipboard()

    def _set_system_clipboard_text(self, text: str) -> None:
        if not text:
            return
        clipboard = self._get_system_clipboard()
        if clipboard is None:
            return
        clipboard.set(text)

    def _paste_text(self, text: str) -> None:
        """Inject clipboard text into xterm.js (fires onData → PTY bridge)."""
        if not text or not self.available:
            return
        import json

        # json.dumps produces a JS string literal safe for evaluate_javascript.
        script = (
            "if (typeof window.term !== 'undefined') { window.term.paste(%s); }"
            % json.dumps(text)
        )
        self._run_javascript(script)

    def copy_clipboard(self) -> None:
        """Copy selected text from xterm.js to the system clipboard.

        Selection is read in JS and posted to Python so we can write the GTK
        clipboard. ``navigator.clipboard`` is unreliable for cross-app use in
        WebKitGTK (and paste from other apps fails for the same reason).
        """
        if not self.available:
            return
        try:
            # IIFE returns a boolean so evaluate_javascript_finish does not see
            # a Promise/"undefined" completion value as an unsupported type.
            script = """
            (function() {
                if (typeof window.term !== 'undefined' && window.term.hasSelection()) {
                    var selection = window.term.getSelection();
                    if (selection && typeof window.ptySend === 'function') {
                        window.ptySend({type: "copy", text: selection});
                    }
                }
                return true;
            })();
            """
            self._run_javascript(script)
        except Exception as e:
            logger.debug(f"Failed to copy from PyXterm backend: {e}", exc_info=True)

    def paste_clipboard(self) -> None:
        """Paste system clipboard content into xterm.js.

        Reads via GTK (not ``navigator.clipboard.readText``), which is what
        other apps write to. WebKit's Clipboard API often cannot see that
        content, so Ctrl+Shift+V / context-menu paste would no-op.
        """
        if not self.available:
            return
        try:
            clipboard = self._get_system_clipboard()
            if clipboard is None:
                return

            def on_text(_clipboard, result):
                try:
                    text = clipboard.read_text_finish(result)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "Failed to read system clipboard for PyXterm paste: %s",
                        exc,
                    )
                    return
                if text:
                    self._paste_text(text)

            clipboard.read_text_async(None, on_text)
        except Exception as e:
            logger.debug(f"Failed to paste to PyXterm backend: {e}", exc_info=True)

    def select_all(self) -> None:
        """Select all text in xterm.js terminal"""
        if not self.available:
            return
        try:
            script = """
            if (typeof window.term !== 'undefined') {
                window.term.selectAll();
            }
            """
            self._run_javascript(script)
        except Exception as e:
            logger.debug(f"Failed to select all in PyXterm backend: {e}", exc_info=True)

    def reset(self, clear_scrollback: bool, clear_screen: bool) -> None:
        if self._terminal_id:
            manager = getattr(self._pyxterm, "TerminalManager", None)
            if manager:
                manager().reset(self._terminal_id)  # type: ignore[attr-defined]

    def set_font_scale(self, scale: float) -> None:
        """Set font scale/zoom for xterm.js terminal by changing fontSize option"""
        if not self.available:
            return
        try:
            # Store previous scale before updating
            previous_scale = getattr(self, '_font_scale', 1.0)
            
            # Store new scale for later retrieval
            self._font_scale = scale
            
            # Base size is stored in points; xterm.js wants CSS pixels.
            if self._base_font_size is not None:
                new_font_size = pango_points_to_xterm_px(self._base_font_size, scale)
                # Change fontSize via xterm.js options API (per xterm.js documentation)
                # According to https://xtermjs.org/docs/api/terminal/classes/terminal/
                # fontSize is a Terminal option that can be set via term.options.fontSize
                zoom_js = f"""
                (function() {{
                    if (typeof window.term !== 'undefined') {{
                        // Set new font size via xterm.js options API
                        window.term.options.fontSize = {new_font_size};
                        
                        // Use setTimeout to ensure font size change is applied before fitting
                        setTimeout(function() {{
                            // Call fit.fit() to properly resize terminal and maintain background area
                            // This ensures the colored background area doesn't resize incorrectly
                            if (typeof window.fit !== 'undefined' && window.fit.fit) {{
                                window.fit.fit();
                                // Trigger a resize event to ensure container recalculates
                                if (typeof window.dispatchEvent !== 'undefined') {{
                                    window.dispatchEvent(new Event('resize'));
                                }}
                            }}
                        }}, 10);
                        
                        // Force a refresh to apply the changes
                        // refresh(start: number, end: number): void
                        if (window.term.rows > 0) {{
                            window.term.refresh(0, window.term.rows - 1);
                        }}
                    }}
                }})();
                """
            else:
                # Fallback: calculate from current font size
                # This happens if base_font_size wasn't set yet (shouldn't normally happen)
                zoom_js = f"""
                (function() {{
                    if (typeof window.term !== 'undefined') {{
                        // Get current font size
                        var currentSize = window.term.options.fontSize || 12;
                        // Estimate base size from current (assuming previous scale was applied)
                        var previousScale = {previous_scale if previous_scale != 0 else 1.0};
                        var estimatedBase = Math.round(currentSize / previousScale);
                        // Calculate new size with new scale
                        var newSize = Math.round(estimatedBase * {scale});
                        
                        // Set new font size via xterm.js options API
                        window.term.options.fontSize = newSize;
                        
                        // Use setTimeout to ensure font size change is applied before fitting
                        setTimeout(function() {{
                            // Call fit.fit() to properly resize terminal and maintain background area
                            // This ensures the colored background area doesn't resize incorrectly
                            if (typeof window.fit !== 'undefined' && window.fit.fit) {{
                                window.fit.fit();
                                // Trigger a resize event to ensure container recalculates
                                if (typeof window.dispatchEvent !== 'undefined') {{
                                    window.dispatchEvent(new Event('resize'));
                                }}
                            }}
                        }}, 10);
                        
                        // Force a refresh to apply the changes
                        if (window.term.rows > 0) {{
                            window.term.refresh(0, window.term.rows - 1);
                        }}
                    }}
                }})();
                """
                new_font_size = None  # For logging
            
            self._run_javascript(zoom_js)
            logger.debug(f"Set font scale to {scale}x for PyXterm backend (changing fontSize to {new_font_size if new_font_size else 'calculated'})")
        except Exception as e:
            logger.debug(f"Failed to set font scale for PyXterm backend: {e}", exc_info=True)

    def get_font_scale(self) -> float:
        """Get current font scale"""
        # Try to get actual scale from terminal if available
        if self.available and self._base_font_size is not None:
            # We could query the terminal, but for now return stored scale
            # The scale is updated when set_font_scale is called
            return getattr(self, '_font_scale', 1.0)
        return getattr(self, '_font_scale', 1.0)

    def feed(self, data: bytes) -> None:
        # pyxterm.js receives data via websocket; nothing to do here.
        return

    def feed_child(self, data: bytes) -> None:
        """Feed raw bytes to the child process input over the pyxtermjs WebSocket."""
        if not self.available:
            return

        try:
            # Convert bytes to string for JavaScript
            # pyxtermjs expects string input and will encode it to bytes for the PTY
            data_str = data.decode('utf-8', errors='replace')

            # Use JSON.stringify to safely escape the string for JavaScript
            # This handles all special characters including quotes, backslashes, newlines, etc.
            import json
            data_str_json = json.dumps(data_str)

            # Send input over the native WebSocket. The HTML template exposes a
            # window.ptySend(obj) helper (and window.socket) for this purpose;
            # the server expects {type: "input", data: <str>}.
            write_js = f"""
            (function() {{
                var payload = {{ type: "input", data: {data_str_json} }};
                if (typeof window.ptySend === 'function') {{
                    return window.ptySend(payload);
                }}
                if (window.socket && window.socket.readyState === WebSocket.OPEN) {{
                    window.socket.send(JSON.stringify(payload));
                    return true;
                }}
                console.error("PyXterm: websocket not available for feed_child");
                return false;
            }})();
            """
            self._run_javascript(write_js)
            logger.debug(f"Sent {len(data)} bytes to PyXterm terminal via WebSocket")
        except Exception as e:
            logger.error(f"Failed to feed child data to PyXterm backend: {e}", exc_info=True)

    def get_content(self, max_chars: Optional[int] = None) -> Optional[str]:
        # pyxterm.js does not currently expose scrollback; return None for compatibility
        return None

    # Match VTE's amber search highlight for multi-match decorations.
    # matchOverviewRuler / activeMatchColorOverviewRuler are required by ISearchDecorationOptions.
    _SEARCH_DECORATIONS = {
        "matchBackground": "#F4D03F",
        "matchBorder": "#D4AC0D",
        "matchOverviewRuler": "#F4D03F",
        "activeMatchBackground": "#E67E22",
        "activeMatchBorder": "#D35400",
        "activeMatchColorOverviewRuler": "#E67E22",
    }

    def search_set_regex(self, regex: Optional[Any]) -> None:
        """Legacy entry point — prefer :meth:`search_set_query`."""
        if regex is None:
            self.search_set_query(None)
        elif isinstance(regex, dict):
            self.search_set_query(
                regex.get("term"),
                case_sensitive=bool(regex.get("case_sensitive", False)),
                regex=bool(regex.get("regex", False)),
            )
        elif isinstance(regex, str):
            # Best-effort legacy string (may be VTE-shaped with (?i)/escapes).
            term = regex
            case_sensitive = True
            if term.startswith("(?i)"):
                term = term[4:]
                case_sensitive = False
            self.search_set_query(term, case_sensitive=case_sensitive, regex=False)

    def search_set_query(
        self,
        term: Optional[str],
        *,
        case_sensitive: bool = False,
        regex: bool = False,
    ) -> None:
        """Store search term/options only — do not search (avoids double findNext)."""
        if not self.available or not self._webview:
            return
        if not term:
            self._current_search_term = None
            self._current_search_is_regex = False
            self._current_search_case_sensitive = False
            self.clear_search_decorations()
            return
        self._current_search_term = term
        self._current_search_is_regex = bool(regex)
        self._current_search_case_sensitive = bool(case_sensitive)
        self._search_addon_loaded = True

    def clear_search_decorations(self) -> None:
        if not self.available or not self._webview:
            return
        self._run_javascript(
            "(function(){"
            "if(window.term&&window.term.searchAddon){"
            "window.term.searchAddon.clearDecorations();"
            "if(window.term.searchAddon.clearActiveDecoration)"
            "window.term.searchAddon.clearActiveDecoration();"
            "}})();"
        )

    def _search_options_dict(self, *, forward: bool = True) -> dict:
        opts = {
            "caseSensitive": bool(self._current_search_case_sensitive),
            "regex": bool(self._current_search_is_regex),
            "decorations": dict(self._SEARCH_DECORATIONS),
        }
        # incremental only affects findNext (SearchAddon typings).
        if forward:
            opts["incremental"] = True
        return opts

    def _run_search_js(self, *, forward: bool) -> bool:
        """Invoke SearchAddon and report found via ``search-result`` message."""
        if not self.available or not self._current_search_term:
            return False
        import json
        payload = {
            "term": self._current_search_term,
            "opts": self._search_options_dict(forward=forward),
            "forward": bool(forward),
        }
        script = (
            "window.sshpilotSearch && window.sshpilotSearch(%s);"
            % json.dumps(payload)
        )
        self._run_javascript(script)
        # Real found/not-found arrives asynchronously as search-result.
        return True

    def search_find_next(self) -> bool:
        return self._run_search_js(forward=True)

    def search_find_previous(self) -> bool:
        return self._run_search_js(forward=False)

    def get_child_pid(self) -> Optional[int]:
        return self._child_pid

    def get_child_pgid(self) -> Optional[int]:
        pid = self.get_child_pid()
        if pid is None:
            return None
        try:
            return os.getpgid(pid)
        except Exception:
            return None

    def supports_feature(self, feature: str) -> bool:
        return False

    def get_pty(self) -> Optional[Any]:
        return None


class PyXtermBridgeBackend(PyXtermTerminalBackend):
    """Embedded (Cursor-model) PyXterm backend.

    xterm.js runs in the app's own WebView (assets pre-loaded via ``load_html``)
    and the PTY is owned + bridged **in-process** via
    :class:`sshpilot.xterm_pty_bridge.XtermPtyBridge` — no Flask server, no
    localhost socket, no argv re-serialization. Selected by
    ``terminal.backend = pyxterm2``.

    Reuses the parent's JS-injection methods verbatim (``apply_theme``,
    ``set_font``, ``copy_clipboard``, ``paste_clipboard``, ``select_all``,
    ``set_font_scale``, ``search_*``, ``grab_focus``, ``_run_javascript`` — they
    all drive ``window.term``); overrides only construction, spawn, PTY, and
    teardown. Requires WebKit 6 (UserContentManager); falls back to VTE if absent.
    """

    _PREREADY_MAX_BYTES = 256_000
    # xterm.js flow control (pending-callback watermark):
    # https://xtermjs.org/docs/guides/flowcontrol/
    _FC_CALLBACK_BYTE_LIMIT = 100_000
    _FC_HIGH = 5
    _FC_LOW = 2
    _FC_SAFETY_MS = 2000

    def __init__(self, owner: "TerminalWidget") -> None:
        self._ucm = None
        self._bridge = None
        self._js_ready = False
        self._pending_spawn: Optional[dict] = None
        self._real_child_pid: Optional[int] = None
        self._child_exited_cb: Optional[Callable] = None
        self._last_size = (24, 80)
        self._stored_font = None
        self._recent_output = ""            # rolling tail of PTY output (for get_content)
        self._output_hooks: list = []       # zero-arg callbacks fired after each flush
        self._preready_output: list = []    # output produced before the page is ready
        self._preready_bytes = 0
        self._shell_entry = None
        self._shell_attached = False
        self._shell_loaded = False
        self._autocompleter = None          # lazy (see _feed_autocomplete)
        self._fc_written = 0
        self._fc_pending = 0
        self._fc_paused = False
        self._fc_safety_id = None
        super().__init__(owner)
        # WebKit2 (GTK3) lacks the UCM script-message bridge this backend needs.
        if getattr(self, "WebKit", None) is None:
            self.available = False
            self.import_error = RuntimeError(
                "PyXterm bridge backend requires WebKit 6.0 (UserContentManager)"
            )
            return

    # ---- construction: WebView with a JS->Python bridge ----------------------

    def _make_webview_webkit6(self, WebKit):
        from .xterm_prewarm import XtermShellPool

        pooled = XtermShellPool.acquire_for_owner(self)
        if pooled is not None:
            self._shell_entry = pooled
            self._ucm = pooled.ucm
            # Ready pool entries are hot; warming adoptions may still be loading.
            self._js_ready = bool(pooled.js_ready)
            self._shell_loaded = bool(pooled.loaded)
            return pooled.webview

        entry = XtermShellPool.create_for_owner(self, WebKit)
        self._shell_entry = entry
        self._ucm = entry.ucm
        return entry.webview

    def _apply_attached_shell_settings(self) -> None:
        try:
            self.apply_theme()
        except Exception:  # noqa: BLE001
            pass
        if self._stored_font is not None:
            try:
                super().set_font(self._stored_font)
            except Exception:  # noqa: BLE001
                pass
        if self._bridge is not None:
            self._bridge.resize(*self._last_size)

    def ensure_shell_loaded(self) -> None:
        """Load the xterm shell once the WebView is attached to the widget tree."""
        if not self.available or self._webview is None or self._shell_attached:
            return
        self._shell_attached = True
        if not self._shell_loaded:
            self._shell_loaded = True
            if self._shell_entry is not None:
                from .xterm_prewarm import XtermShellPool

                XtermShellPool.load_for_entry(self._shell_entry)
            else:
                self._load_shell()
        elif self._js_ready:
            self._apply_attached_shell_settings()

    def _load_shell(self):
        try:
            from .xterm_shell import build_shell_html
            html = build_shell_html()
            # Base URI must be a localhost origin (secure context). "about:blank"
            # is not. Copy/paste goes through GTK nowadays; keep localhost for
            # any remaining Web APIs that require a secure context.
            self._webview.load_html(html, "http://localhost/")
            logger.debug("Loaded embedded xterm shell via load_html")
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to load embedded xterm shell: %s", e, exc_info=True)

    # ---- JS -> Python messages ----------------------------------------------

    def _set_owner_hovered_link(self, url: Optional[str]) -> None:
        """Mirror WebLinks hover into TerminalWidget for Open/Copy Link menu items."""
        owner = self.owner
        if owner is None:
            return
        text = (url or "").strip() or None
        if text is not None and not text.startswith(("http://", "https://")):
            text = None
        try:
            owner._hovered_hyperlink_uri = text
        except Exception:  # noqa: BLE001
            logger.debug("Failed to set hovered hyperlink on owner", exc_info=True)

    def _on_pty_message(self, ucm, js_value):
        import json
        try:
            if hasattr(js_value, "to_json"):
                raw = js_value.to_json(0)
            else:  # older binding
                raw = js_value.get_js_value().to_json(0)
            payload = json.loads(raw)
            if isinstance(payload, str):
                payload = json.loads(payload)
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring malformed pty message: %s", e)
            return
        kind = payload.get("type")
        if kind == "ready":
            self._js_ready = True
            self._last_size = (payload.get("rows", 24), payload.get("cols", 80))
            # Flush buffered shell output BEFORE theme/font JS so the prompt is
            # not queued behind those evaluate_javascript calls (first paint).
            # One base64 evaluate_javascript — never replay chunk-by-chunk.
            if self._preready_output:
                buffered, self._preready_output = "".join(self._preready_output), []
                self._preready_bytes = 0
                self._write_to_term(buffered, bulk=True)
            # Re-apply configured theme/font now that window.term exists.
            try:
                self.apply_theme()
            except Exception:  # noqa: BLE001
                pass
            if self._stored_font is not None:
                try:
                    super().set_font(self._stored_font)
                except Exception:  # noqa: BLE001
                    pass
            # Resize the already-running shell to the real terminal size (it was
            # spawned at a default size in parallel with the page load).
            if self._bridge is not None:
                self._bridge.resize(*self._last_size)
            if self._pending_spawn is not None:  # fallback (spawn normally happens early)
                self._do_spawn()
        elif kind == "write-ack":
            self._on_write_ack()
        elif kind == "search-result":
            owner = self.owner
            if owner is not None and hasattr(owner, "handle_search_result"):
                try:
                    owner.handle_search_result(
                        bool(payload.get("found")),
                        result_index=int(payload.get("resultIndex", -1)),
                        result_count=int(payload.get("resultCount", 0)),
                    )
                except Exception:  # noqa: BLE001
                    logger.debug("handle_search_result raised", exc_info=True)
        elif kind == "search-results":
            owner = self.owner
            if owner is not None and hasattr(owner, "handle_search_results"):
                try:
                    owner.handle_search_results(
                        int(payload.get("resultIndex", -1)),
                        int(payload.get("resultCount", 0)),
                    )
                except Exception:  # noqa: BLE001
                    logger.debug("handle_search_results raised", exc_info=True)
        elif kind == "input":
            if self._bridge is not None:
                self._bridge.write(payload.get("data", ""))
            self._feed_autocomplete(payload.get("data", ""))
        elif kind == "resize":
            self._last_size = (payload.get("rows", 24), payload.get("cols", 80))
            if self._bridge is not None:
                self._bridge.resize(*self._last_size)
        elif kind == "title":
            # xterm.js OSC 0/2 title change — parity with VTE's window title +
            # termprops-based CONNECTING→CONNECTED promotion.
            owner = self.owner
            if owner is not None and hasattr(owner, "handle_backend_title"):
                try:
                    owner.handle_backend_title(payload.get("title", ""))
                except Exception:  # noqa: BLE001
                    logger.debug("handle_backend_title raised", exc_info=True)
        elif kind == "open-url":
            # WebLinksAddon click — open in the system browser (VTE parity).
            url = (payload.get("url") or "").strip()
            if url.startswith(("http://", "https://")):
                try:
                    from .web_tab import open_url_in_browser
                    if open_url_in_browser(url):
                        logger.debug("Opened PyXterm URL: %s", url)
                    else:
                        logger.warning("Failed to open PyXterm URL: %s", url)
                except Exception:  # noqa: BLE001
                    logger.warning("Failed to open PyXterm URL: %s", url, exc_info=True)
            else:
                logger.debug("Ignoring non-http(s) PyXterm URL: %s", url)
        elif kind == "link-hover":
            # WebLinksAddon hover — feed TerminalWidget context menu (Open/Copy Link).
            self._set_owner_hovered_link(payload.get("url"))
        elif kind == "link-leave":
            self._set_owner_hovered_link(None)
        elif kind == "paste":
            # Ctrl+Shift+V inside the WebView — use GTK clipboard (other apps).
            self.paste_clipboard()
        elif kind == "copy":
            # Selection posted from JS (shortcut or copy_clipboard).
            try:
                self._set_system_clipboard_text(payload.get("text") or "")
            except Exception:  # noqa: BLE001
                logger.debug("Failed to set system clipboard from PyXterm", exc_info=True)

    # ---- autocomplete (Termius-style popup, engine in autocomplete.py) -------

    def _get_autocompleter(self):
        if self._autocompleter is None:
            from .autocomplete import (
                Autocompleter, CommandBlockProvider, RemoteHistoryProvider,
                SessionProvider, ShellHistoryProvider, fetch_remote_history,
            )
            root = store = None
            try:
                root = self.owner.get_root()
                ensure = getattr(root, "_ensure_command_block_store", None)
                store = ensure() if callable(ensure) else getattr(root, "command_block_store", None)
            except Exception:  # noqa: BLE001
                pass
            session = SessionProvider()
            # Provider order = source rank: session > remote > history > snippets.
            providers = [session, ShellHistoryProvider(), CommandBlockProvider(store)]
            conn = getattr(self.owner, "connection", None)
            try:
                is_ssh = conn is not None and not self.owner._is_local_terminal()
            except Exception:  # noqa: BLE001
                is_ssh = False
            # Remote history is opt-in: it opens a second SSH connection to the host.
            remote_on = False
            try:
                remote_on = bool(self.owner.config.get_setting(
                    'terminal.autocomplete_remote', False))
            except Exception:  # noqa: BLE001
                pass
            if is_ssh and remote_on:
                key = str(getattr(conn, "nickname", "") or getattr(conn, "hostname", "")
                          or getattr(conn, "host", ""))
                if key:
                    cm = getattr(root, "connection_manager", None)
                    config = getattr(self.owner, "config", None)
                    providers.insert(1, RemoteHistoryProvider(
                        key, lambda: fetch_remote_history(conn, cm, config)))
            self._autocompleter = Autocompleter(providers, session=session)
        return self._autocompleter

    def _feed_autocomplete(self, data: str) -> None:
        """Feed a keystroke to the popup engine; must never break the input path."""
        try:
            if not self._js_ready:
                return
            config = getattr(self.owner, "config", None)
            if config is None or not config.get_setting("terminal.autocomplete", True):
                return
            payload = self._get_autocompleter().feed(
                data, output_tail=self._recent_output[-200:])
            if payload is not None:
                import json
                self._run_javascript(
                    "window.sshpilotAC && window.sshpilotAC.update(%s);"
                    % json.dumps(payload))
        except Exception:  # noqa: BLE001
            logger.debug("autocomplete feed failed", exc_info=True)

    # ---- spawn: in-process PTY, reusing the shared argv/env ------------------

    def spawn_async(
        self,
        argv: Sequence[str],
        env: Optional[Mapping[str, str]] = None,
        cwd: Optional[str] = None,
        flags: int = 0,
        child_setup: Optional[Callable[..., None]] = None,
        callback: Optional[Callable[[GObject.Object, Optional[Exception]], None]] = None,
        user_data: Optional[Any] = None,
    ) -> None:
        if not self.available:
            raise RuntimeError("pyxterm bridge backend is not available")
        self.ensure_shell_loaded()
        command = list(argv) if argv else ["bash"]
        # Same luit encoding wrap as the server-backed pyxterm path.
        encoding = "UTF-8"
        if self.owner and getattr(self.owner, "config", None):
            try:
                encoding = self.owner.config.get_setting("terminal.encoding", "UTF-8")
            except Exception:  # noqa: BLE001
                pass
        command = list(self._wrap_command_with_encoding(command, encoding))
        env_list = [f"{k}={v}" for k, v in env.items()] if env else None
        self._pending_spawn = {
            "argv": command, "env": env_list, "cwd": cwd,
            "callback": callback, "user_data": user_data,
        }
        # Spawn the child NOW, in parallel with the WebView loading xterm.js, so the
        # shell/ssh startup overlaps the (slower) page load instead of running after
        # it. Output produced before the page is ready is buffered and flushed on
        # the "ready" message, so the prompt appears the instant the terminal paints.
        self._do_spawn()
        # Warm the autocomplete providers (remote history is an SSH round-trip)
        # so suggestions exist by the first keystroke, not one line later.
        try:
            config = getattr(self.owner, "config", None)
            if config is not None and config.get_setting("terminal.autocomplete", True):
                self._get_autocompleter().prefetch()
        except Exception:  # noqa: BLE001 — best-effort warmup
            logger.debug("autocomplete prefetch failed", exc_info=True)

    def _do_spawn(self):
        pending, self._pending_spawn = self._pending_spawn, None
        if not pending:
            return
        from .xterm_pty_bridge import XtermPtyBridge
        self._reset_flow_control()
        self._bridge = XtermPtyBridge(
            on_output=self._on_pty_output,
            on_exit=self._on_bridge_exit,
            flush_ms=16,
        )
        rows, cols = self._last_size

        def on_spawned(pid, err):
            cb = pending["callback"]
            if err is not None:
                logger.error("PyXterm bridge spawn failed: %s", err)
                if cb:
                    cb(self.widget, 0, err, pending["user_data"])
                return
            self._real_child_pid = pid
            if cb:
                cb(self.widget, pid or 0, None, pending["user_data"])

        self._bridge.spawn(
            pending["argv"], env=pending["env"], cwd=pending["cwd"],
            rows=rows, cols=cols, on_spawned=on_spawned,
        )

    def _reset_flow_control(self) -> None:
        self._fc_written = 0
        self._fc_pending = 0
        self._cancel_fc_safety()
        if self._fc_paused and self._bridge is not None:
            try:
                self._bridge.resume()
            except Exception:  # noqa: BLE001
                pass
        self._fc_paused = False

    def _cancel_fc_safety(self) -> None:
        if self._fc_safety_id is None:
            return
        try:
            from gi.repository import GLib
            GLib.source_remove(self._fc_safety_id)
        except Exception:  # noqa: BLE001
            pass
        self._fc_safety_id = None

    def _pause_pty_flow(self) -> None:
        if self._fc_paused or self._bridge is None:
            return
        self._fc_paused = True
        try:
            self._bridge.pause()
        except Exception:  # noqa: BLE001
            logger.debug("PTY flow-control pause failed", exc_info=True)
            self._fc_paused = False
            return
        if self._fc_safety_id is None:
            try:
                from gi.repository import GLib
                self._fc_safety_id = GLib.timeout_add(
                    self._FC_SAFETY_MS, self._fc_safety_resume
                )
            except Exception:  # noqa: BLE001
                pass

    def _resume_pty_flow(self) -> None:
        if not self._fc_paused:
            return
        self._fc_paused = False
        self._cancel_fc_safety()
        if self._bridge is not None:
            try:
                self._bridge.resume()
            except Exception:  # noqa: BLE001
                logger.debug("PTY flow-control resume failed", exc_info=True)

    def _fc_safety_resume(self) -> bool:
        """Unstick the PTY if write-ack messages are lost."""
        self._fc_safety_id = None
        if self._fc_paused:
            logger.debug("PyXterm flow-control safety resume (missing write-ack)")
            self._fc_pending = 0
            self._fc_written = 0
            self._resume_pty_flow()
        return False

    def _on_write_ack(self) -> None:
        self._fc_pending = max(self._fc_pending - 1, 0)
        if self._fc_pending < self._FC_LOW:
            self._resume_pty_flow()

    def _write_to_term(self, text: str, *, bulk: bool = False):
        import json
        # Pending-callback watermark (xterm.js flowcontrol guide). Fast path
        # skips the write callback until ~100KB has been queued.
        self._fc_written += len(text)
        want_ack = bool(bulk) or self._fc_written >= self._FC_CALLBACK_BYTE_LIMIT
        if want_ack:
            self._fc_written = 0
            self._fc_pending += 1
        ack_js = "true" if want_ack else "false"
        if bulk:
            import base64
            b64 = base64.b64encode(text.encode("utf-8", "replace")).decode("ascii")
            self._run_javascript(
                "window.termWriteB64 && window.termWriteB64(%s, %s);"
                % (json.dumps(b64), ack_js)
            )
        else:
            self._run_javascript(
                "window.termWrite && window.termWrite(%s, %s);"
                % (json.dumps(text), ack_js)
            )
        if want_ack and self._fc_pending > self._FC_HIGH:
            self._pause_pty_flow()

    def _on_pty_output(self, chunk: str):
        # Keep a rolling tail so get_content() works (PTY auto-fill / failure
        # classification).
        self._recent_output = (self._recent_output + chunk)[-8000:]
        if self._js_ready:
            self._write_to_term(chunk)
        else:
            # Page not painted yet: buffer, flush on "ready".
            self._preready_output.append(chunk)
            self._preready_bytes += len(chunk)
            while self._preready_bytes > self._PREREADY_MAX_BYTES and self._preready_output:
                dropped = self._preready_output.pop(0)
                self._preready_bytes -= len(dropped)
        for hook in list(self._output_hooks):
            try:
                hook()
            except Exception:  # noqa: BLE001
                logger.debug("output hook raised", exc_info=True)

    def add_output_hook(self, callback) -> None:
        """Subscribe a zero-arg callback invoked after each batch of PTY output.
        Multiple consumers (PTY auto-fill watcher, connect-evidence scanner) can
        register; this stands in for VTE's ``contents-changed`` signal."""
        if callback is not None and callback not in self._output_hooks:
            self._output_hooks.append(callback)

    def remove_output_hook(self, callback) -> None:
        try:
            self._output_hooks.remove(callback)
        except ValueError:
            pass

    def get_content(self, max_chars: Optional[int] = None) -> Optional[str]:
        if not self._recent_output:
            return None
        if max_chars:
            return self._recent_output[-max_chars:]
        return self._recent_output

    # ---- PTY-backed capabilities (real ssh child, unlike the server path) -----

    def get_pty(self) -> Optional[Any]:
        return self._bridge.get_pty() if self._bridge is not None else None

    def get_child_pid(self) -> Optional[int]:
        return self._real_child_pid

    def feed_child(self, data: bytes) -> None:
        # Direct to the PTY (keystrokes/broadcast) — no JS round-trip.
        if self._bridge is not None:
            self._bridge.write(data)

    def set_font(self, font_desc) -> None:
        self._stored_font = font_desc
        super().set_font(font_desc)

    # ---- child-exit + teardown ----------------------------------------------

    def connect_child_exited(self, callback: Callable[[Gtk.Widget, int], None]) -> Any:
        self._child_exited_cb = callback
        return "pyxterm_bridge_child_exited"

    def _on_bridge_exit(self, status: int):
        cb = self._child_exited_cb
        if cb:
            try:
                cb(self.widget, status)
            except Exception:  # noqa: BLE001
                logger.debug("child-exited callback raised", exc_info=True)

    def disconnect(self, handler_id: Any) -> None:
        if handler_id == "pyxterm_bridge_child_exited":
            self._child_exited_cb = None
            return
        super().disconnect(handler_id)

    def destroy(self) -> None:
        # Ordered, synchronous teardown (see shutdown-segfault history): close the
        # bridge (removes fd/flush/child sources + PTY) before dropping the WebView.
        self._reset_flow_control()
        try:
            if self._bridge is not None:
                self._bridge.close()
        finally:
            self._bridge = None
            if self._shell_entry is not None:
                try:
                    from .xterm_prewarm import XtermShellPool

                    XtermShellPool.release(self._shell_entry)
                except Exception:  # noqa: BLE001
                    logger.debug("Failed to release pooled xterm shell", exc_info=True)
                self._shell_entry = None
                self._webview = None
            super().destroy()

    def supports_feature(self, feature: str) -> bool:
        return feature in ("pty",)



"""Terminal backend abstractions for sshPilot."""

from __future__ import annotations

import importlib
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GObject, Gtk

logger = logging.getLogger(__name__)


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

    def search_find_next(self) -> bool:
        """Search forward using the configured regex."""

    def search_find_previous(self) -> bool:
        """Search backwards using the configured regex."""

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
        self._populate_popup_handler: Optional[int] = None

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
        
        # Disable VTE's built-in context menu to prevent duplication with our custom menu
        try:
            if hasattr(self.vte, "connect"):
                def _on_populate_popup(vte, menu):
                    # Prevent VTE's default context menu from appearing
                    # We use our own custom context menu instead
                    menu.set_visible(False)
                    return True
                self._populate_popup_handler = self.vte.connect("populate-popup", _on_populate_popup)
                logger.debug("Disabled VTE built-in context menu")
        except Exception as e:
            logger.debug(f"Failed to disable VTE context menu: {e}", exc_info=True)

    def destroy(self) -> None:
        try:
            if self._termprops_handler is not None:
                self.vte.disconnect(self._termprops_handler)  # type: ignore[arg-type]
        except Exception:
            pass
        try:
            if self._populate_popup_handler is not None:
                self.vte.disconnect(self._populate_popup_handler)  # type: ignore[arg-type]
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
        try:
            content_result = self.vte.get_text_range(
                0,
                0,
                -1,
                -1,
                lambda *args: True,
            )
            content = content_result[0] if content_result else None
            if content and max_chars and len(content) > max_chars:
                return content[-max_chars:]
            return content
        except Exception:
            logger.debug("Failed to read VTE content", exc_info=True)
            return None

    def search_set_regex(self, regex: Optional[Any]) -> None:
        if regex is None:
            self.vte.search_set_regex(None, 0)
        else:
            self.vte.search_set_regex(regex, 0)

    def search_find_next(self) -> bool:
        return self.vte.search_find_next()

    def search_find_previous(self) -> bool:
        return self.vte.search_find_previous()

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
        self._pyxterm_cli_module = "sshpilot.vendor.pyxtermjs"
        self._webview = None
        self._server = None
        self._terminal_id: Optional[str] = None
        self._child_pid: Optional[int] = None
        self._child_exited_callback: Optional[Callable] = None
        self._template_backed_up = False
        self._temp_script_path: Optional[str] = None
        self._font_scale: float = 1.0
        self._base_font_size: Optional[int] = None  # Store base font size for zoom calculations
        self._search_addon_loaded = False  # Track if search addon is loaded
        self._current_search_term: Optional[str] = None  # Current search term
        self._current_search_is_regex: bool = False  # Whether current search is regex
        self._current_search_case_sensitive: bool = False  # Whether current search is case sensitive

        # Initialize with a fallback widget
        self.widget: Gtk.Widget = Gtk.Box()

        try:
            vendored_module = None
            try:
                vendored_module = importlib.import_module("sshpilot.vendor.pyxtermjs")
                self._vendored_pyxterm = vendored_module
            except ModuleNotFoundError:
                logger.debug("Vendored pyxtermjs module not found")
                self._vendored_pyxterm = None
            except Exception as e:
                logger.debug(f"Failed to import vendored pyxtermjs: {e}")
                self._vendored_pyxterm = None

            # Try external module first, fall back to vendored if available
            try:
                external_module = importlib.import_module("pyxtermjs")
                pyxterm_module = external_module
            except ModuleNotFoundError:
                if vendored_module is None:
                    # Neither module found - will be caught by outer exception handler
                    raise ImportError("Neither external nor vendored pyxtermjs module found")
                logger.debug("External pyxtermjs package not found; using vendored copy")
                pyxterm_module = vendored_module
            except Exception as e:
                if vendored_module is None:
                    # No fallback available - will be caught by outer exception handler
                    raise ImportError(f"Failed to import pyxtermjs: {e}")
                logger.debug(f"External pyxtermjs import failed; using vendored copy: {e}")
                pyxterm_module = vendored_module

            # Use WebKit 6.0 (GTK4 compatible) - this is the preferred approach
            try:
                gi.require_version("WebKit", "6.0")
                from gi.repository import WebKit
                self.WebKit = WebKit
                self._webview = WebKit.WebView()
                logger.debug("Using WebKit 6.0 (GTK4 compatible)")
            except Exception as webkit6_error:
                logger.debug(f"WebKit 6.0 not available: {webkit6_error}")
                
                # Check if GTK 4.0 is already loaded (which conflicts with WebKit2)
                if hasattr(Gtk, 'get_major_version') and Gtk.get_major_version() == 4:
                    raise ImportError("PyXterm backend requires WebKit 6.0 for GTK 4.0 compatibility, but WebKit 6.0 is not available")
                
                # Fall back to WebKit2 4.0 (only if GTK 3.0 is available)
                gi.require_version("WebKit2", "4.0")
                from gi.repository import WebKit2
                self.WebKit2 = WebKit2
                self._webview = WebKit2.WebView()
                logger.debug("Using WebKit2 4.0 (GTK3 compatible)")
            
            # Disable WebView's native context menu at GTK level
            try:
                # Add a gesture controller to intercept right-click events
                # This prevents the WebView's native context menu from appearing
                # We claim the event but don't show a menu - the terminal widget's gesture will handle it
                context_gesture = Gtk.GestureClick()
                context_gesture.set_button(Gdk.BUTTON_SECONDARY)
                def _on_webview_right_click(gesture, n_press, x, y):
                    # Claim the event to prevent WebView's native context menu
                    # The terminal widget's gesture will handle showing our custom menu
                    gesture.set_state(Gtk.EventSequenceState.CLAIMED)
                context_gesture.connect("pressed", _on_webview_right_click)
                self._webview.add_controller(context_gesture)
                logger.debug("Added gesture controller to disable WebView native context menu")
            except Exception as e:
                logger.debug(f"Failed to disable WebView native context menu: {e}", exc_info=True)

        except Exception as exc:  # pragma: no cover - optional dependency
            self.import_error = exc
            logger.debug("PyXterm backend unavailable", exc_info=True)
            return

        self._pyxterm = pyxterm_module
        self.widget = self._webview
        self.available = True

    # The pyxterm backend exposes only a subset of the behaviour for now. Each
    # method contains guards so the widget can fall back cleanly.

    def initialize(self) -> None:
        if not self.available:
            return
        self.widget.set_hexpand(True)
        self.widget.set_vexpand(True)

    def _on_webview_load_changed(self, webview, load_event, *args):
        """Called when the WebView load state changes"""
        try:
            # Handle different load events
            if load_event == 1:  # WEBKIT_LOAD_STARTED
                logger.debug("WebView load started")
            elif load_event == 2:  # WEBKIT_LOAD_REDIRECTED
                logger.debug("WebView load redirected")
            elif load_event == 3:  # WEBKIT_LOAD_COMMITTED
                logger.debug("WebView load committed")
                # Disable context menu early (on load-committed) to catch it before page fully loads
                disable_context_menu_js = """
                (function() {
                    // Disable browser's default context menu
                    document.addEventListener('contextmenu', function(e) {
                        e.preventDefault();
                        e.stopPropagation();
                        return false;
                    }, true);
                    // Also disable on the terminal element if it exists
                    if (typeof window.term !== 'undefined' && window.term.element) {
                        window.term.element.addEventListener('contextmenu', function(e) {
                            e.preventDefault();
                            e.stopPropagation();
                            return false;
                        }, true);
                    }
                })();
                """
                self._run_javascript(disable_context_menu_js)
                logger.debug("Disabled WebView context menu (early, on load-committed)")
            elif load_event == 4:  # WEBKIT_LOAD_FINISHED
                logger.debug("WebView load finished, applying focus and settings")
                # Apply focus and settings after WebView is fully loaded
                def apply_settings():
                    try:
                        logger.debug("WebView load-finished: applying focus and settings")
                        # Disable context menu again on load-finished (in case it wasn't caught earlier)
                        disable_context_menu_js = """
                        (function() {
                            // Disable browser's default context menu
                            document.addEventListener('contextmenu', function(e) {
                                e.preventDefault();
                                e.stopPropagation();
                                return false;
                            }, true);
                            // Also disable on the terminal element if it exists
                            if (typeof window.term !== 'undefined' && window.term.element) {
                                window.term.element.addEventListener('contextmenu', function(e) {
                                    e.preventDefault();
                                    e.stopPropagation();
                                    return false;
                                }, true);
                            }
                        })();
                        """
                        self._run_javascript(disable_context_menu_js)
                        logger.debug("Disabled WebView context menu (on load-finished)")
                        
                        # Just focus the WebView - HTML template handles terminal focus
                        self.widget.grab_focus()
                        
                        # Apply theme and font after page is loaded
                        if hasattr(self.owner, 'config'):
                            theme_name = self.owner.config.get_setting("terminal.theme", "default")
                            self.apply_theme(theme_name)
                            
                            font_string = self.owner.config.get_setting("terminal.font", "Monospace 12")
                            font_desc = Pango.FontDescription.from_string(font_string)
                            self.set_font(font_desc)
                            
                            # Apply font scale if set
                            if hasattr(self, '_font_scale'):
                                self.set_font_scale(self._font_scale)
                        
                        logger.debug("Settings applied after WebView load finished")
                    except Exception:
                        logger.debug("Failed to apply settings after WebView load", exc_info=True)
                    return False  # Don't repeat
                
                # Small delay to ensure page is ready
                GLib.timeout_add(500, apply_settings)
            elif load_event == 5:  # WEBKIT_LOAD_FAILED
                logger.error("WebView load failed - this may indicate connection refused error")
                # Emit connection failed signal to the terminal widget
                if hasattr(self.owner, 'emit'):
                    self.owner.emit('connection-failed', 'Could not connect to PyXterm server: Connection refused')
        except Exception:
            logger.debug("Error in WebView load-changed handler", exc_info=True)

    def _backup_pyxtermjs_template(self) -> None:
        """Backup the original pyxtermjs template"""
        try:
            import shutil

            module = self._vendored_pyxterm or self._pyxterm
            if not module:
                return

            pyxtermjs_path = Path(module.__file__).resolve().parent
            original_template = pyxtermjs_path / "index.html"
            backup_template = pyxtermjs_path / "index.html.backup"

            if original_template.exists() and not backup_template.exists():
                shutil.copy2(original_template, backup_template)
                self._template_backed_up = True
                logger.debug("Backed up original pyxtermjs template")
        except Exception as e:
            logger.debug(f"Failed to backup pyxtermjs template: {e}")
            self._template_backed_up = False

    def _replace_pyxtermjs_template(self) -> None:
        """Replace the pyxtermjs template with our version that includes theme and font settings"""
        try:
            import shutil
            import json
            import re

            module = self._vendored_pyxterm or self._pyxterm
            if not module:
                return

            pyxtermjs_path = Path(module.__file__).resolve().parent
            original_template = pyxtermjs_path / "index.html"
            
            # Get theme and font settings from config
            owner = self.owner
            if owner and hasattr(owner, 'config') and owner.config:
                theme_name = owner.config.get_setting("terminal.theme", "default")
                profile = owner.config.get_terminal_profile(theme_name)
                font_string = owner.config.get_setting("terminal.font", "Monospace 12")
            else:
                # Default fallback
                profile = {
                    "foreground": "#FFFFFF",
                    "background": "#000000",
                    "cursor_color": "#FFFFFF",
                    "highlight_background": "#4A90E2",
                    "highlight_foreground": "#FFFFFF",
                    "palette": [
                        "#000000", "#CC0000", "#4E9A06", "#C4A000",
                        "#3465A4", "#75507B", "#06989A", "#D3D7CF",
                        "#555753", "#EF2929", "#8AE234", "#FCE94F",
                        "#729FCF", "#AD7FA8", "#34E2E2", "#EEEEEC"
                    ]
                }
                font_string = "Monospace 12"
            
            # Parse font
            font_desc = Pango.FontDescription.from_string(font_string)
            font_family = font_desc.get_family() or "Monospace"
            font_size = font_desc.get_size() / Pango.SCALE
            
            # Build theme object for JavaScript
            palette = profile.get("palette", [])
            theme_obj = {
                "background": profile.get("background", "#000000"),
                "foreground": profile.get("foreground", "#FFFFFF"),
                "cursor": profile.get("cursor_color", profile.get("foreground", "#FFFFFF")),
                "selectionBackground": profile.get("highlight_background", "#4A90E2"),
                "selectionForeground": profile.get("highlight_foreground", profile.get("foreground", "#FFFFFF"))
            }
            
            # Add palette colors if available
            if palette and len(palette) >= 16:
                theme_obj["black"] = palette[0]
                theme_obj["red"] = palette[1]
                theme_obj["green"] = palette[2]
                theme_obj["yellow"] = palette[3]
                theme_obj["blue"] = palette[4]
                theme_obj["magenta"] = palette[5]
                theme_obj["cyan"] = palette[6]
                theme_obj["white"] = palette[7]
                theme_obj["brightBlack"] = palette[8]
                theme_obj["brightRed"] = palette[9]
                theme_obj["brightGreen"] = palette[10]
                theme_obj["brightYellow"] = palette[11]
                theme_obj["brightBlue"] = palette[12]
                theme_obj["brightMagenta"] = palette[13]
                theme_obj["brightCyan"] = palette[14]
                theme_obj["brightWhite"] = palette[15]
            
            # Read the original template
            if original_template.exists():
                template_content = original_template.read_text(encoding='utf-8')
                
                # Replace the hardcoded theme in the Terminal constructor
                theme_json = json.dumps(theme_obj, indent=10).replace('\n', '\n        ')
                font_family_escaped = font_family.replace("'", "\\'").replace('"', '\\"')
                
                # Replace theme in Terminal constructor
                theme_pattern = r"theme:\s*\{[^}]*\}"
                theme_replacement = f"theme: {theme_json}"
                template_content = re.sub(theme_pattern, theme_replacement, template_content, flags=re.DOTALL)
                
                # Update or add font settings in Terminal constructor
                # Replace existing fontFamily if present
                font_family_pattern = r"fontFamily:\s*['\"][^'\"]*['\"]"
                template_content = re.sub(font_family_pattern, f"fontFamily: '{font_family_escaped}'", template_content)
                
                # Replace existing fontSize if present
                font_size_pattern = r"fontSize:\s*\d+"
                template_content = re.sub(font_size_pattern, f"fontSize: {int(font_size)}", template_content)
                
                # If fontFamily or fontSize weren't found, add them after cursorBlink
                if 'fontFamily:' not in template_content or 'fontSize:' not in template_content:
                    if 'cursorBlink:' in template_content:
                        terminal_pattern = r"(cursorBlink:\s*true,)"
                        font_settings = f"\\1\n        fontFamily: '{font_family_escaped}',\n        fontSize: {int(font_size)},"
                        template_content = re.sub(terminal_pattern, font_settings, template_content)
                    elif 'macOptionIsMeta:' in template_content:
                        terminal_pattern = r"(macOptionIsMeta:\s*true,)"
                        font_settings = f"\\1\n        fontFamily: '{font_family_escaped}',\n        fontSize: {int(font_size)},"
                        template_content = re.sub(terminal_pattern, font_settings, template_content)
                    else:
                        # Fallback: add after the opening brace of Terminal constructor
                        terminal_pattern = r"(const term = new Terminal\(\{)"
                        font_settings = f"\\1\n        fontFamily: '{font_family_escaped}',\n        fontSize: {int(font_size)},"
                        template_content = re.sub(terminal_pattern, font_settings, template_content)
                
                # Also add CSS for font (in case JavaScript font settings don't work)
                if 'sshpilot-terminal-font' not in template_content:
                    css_insertion = f"""
      <style id="sshpilot-terminal-font">
        .xterm {{
          font-family: '{font_family_escaped}', monospace !important;
          font-size: {font_size}pt !important;
        }}
        .xterm .xterm-screen {{
          font-family: '{font_family_escaped}', monospace !important;
          font-size: {font_size}pt !important;
        }}
      </style>
"""
                    # Insert before the xterm.css link
                    template_content = template_content.replace(
                        '<link\n      rel="stylesheet"\n      href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css"\n    />',
                        css_insertion + '    <link\n      rel="stylesheet"\n      href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css"\n    />'
                    )
                
                # Write the modified template
                original_template.write_text(template_content, encoding='utf-8')
                logger.debug(f"Modified pyxtermjs template with theme {theme_name} and font {font_family} {font_size}pt")
            else:
                logger.warning(f"Template file not found: {original_template}")
        except Exception as e:
            logger.debug(f"Failed to replace pyxtermjs template: {e}", exc_info=True)

    def _restore_pyxtermjs_template(self) -> None:
        """Restore the original pyxtermjs template"""
        try:
            import shutil

            module = self._vendored_pyxterm or self._pyxterm
            if not module:
                return

            pyxtermjs_path = Path(module.__file__).resolve().parent
            original_template = pyxtermjs_path / "index.html"
            backup_template = pyxtermjs_path / "index.html.backup"

            if self._template_backed_up and backup_template.exists():
                shutil.copy2(backup_template, original_template)
                backup_template.unlink()
                logger.debug("Restored original pyxtermjs template")
        except Exception as e:
            logger.debug(f"Failed to restore pyxtermjs template: {e}")

    def destroy(self) -> None:
        if hasattr(self, '_server_process') and self._server_process:
            import subprocess
            try:
                self._server_process.terminate()
                self._server_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._server_process.kill()
                self._server_process.wait()
            except Exception:
                logger.debug("Failed to close pyxterm server", exc_info=True)
        self._server_process = None
        
        # Restore the original pyxtermjs template
        self._restore_pyxtermjs_template()
        
        # Clean up temporary script if it exists
        if hasattr(self, '_temp_script_path') and self._temp_script_path:
            try:
                import os
                os.unlink(self._temp_script_path)
            except Exception:
                logger.debug("Failed to clean up temporary script", exc_info=True)
            self._temp_script_path = None

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
                    # Pass script length (-1 for null-terminated), None for world_name, None for source_uri, None for cancellable
                    self._webview.evaluate_javascript(script, -1, None, None, None, on_js_finished, None)
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
            
            theme_js = f"""
            (function() {{
                if (typeof window.term !== 'undefined') {{
                    var theme = {{
                        background: '{profile.get("background", "#000000")}',
                        foreground: '{profile.get("foreground", "#FFFFFF")}',
                        cursor: '{profile.get("cursor_color", profile.get("foreground", "#FFFFFF"))}',
                        selectionBackground: '{profile.get("highlight_background", "#4A90E2")}',
                        selectionForeground: '{profile.get("highlight_foreground", profile.get("foreground", "#FFFFFF"))}'
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
            # Extract font family and size from Pango font description
            font_family = font_desc.get_family() or "Monospace"
            font_size = int(font_desc.get_size() / Pango.SCALE)  # Convert from Pango units to points
            
            # Store base font size for zoom calculations (only if not already set or if scale is 1.0)
            if self._base_font_size is None or self._font_scale == 1.0:
                self._base_font_size = font_size
            
            # Apply current zoom scale to font size
            scaled_font_size = int(font_size * self._font_scale)
            
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
                    window.term.options.fontSize = {scaled_font_size};
                    
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
            logger.debug(f"Set font to {font_family} {scaled_font_size}pt (base: {font_size}pt, scale: {self._font_scale}x) for PyXterm backend")
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
            raise RuntimeError("pyxterm backend is not available")

        import subprocess
        import threading
        import time
        import os

        # Find an available port
        import socket
        def find_free_port():
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('', 0))
                s.listen(1)
                port = s.getsockname()[1]
            return port

        # Start pyxtermjs server
        port = find_free_port()
        command = list(argv) if argv else None
        
        # Get encoding setting from config and wrap command if needed
        encoding = 'UTF-8'  # Default
        if self.owner and hasattr(self.owner, 'config') and self.owner.config:
            encoding = self.owner.config.get_setting('terminal.encoding', 'UTF-8')
        
        # Wrap command with encoding transcoder if needed (for legacy encodings)
        # UTF-8 and UTF-16 are natively supported by xterm.js
        if command:
            command = self._wrap_command_with_encoding(command, encoding)
        
        # Build pyxtermjs command
        pyxterm_cmd = [
            sys.executable,
            '-m',
            self._pyxterm_cli_module,
            '--port', str(port),
            '--host', '127.0.0.1'
        ]
        
        # Handle the command and arguments properly
        if command:
            # For SSH commands, we need to pass the full command as a single string
            # to avoid issues with argument parsing
            if command[0] == 'ssh' and len(command) > 1:
                # Create a temporary script to handle the SSH command properly
                import tempfile
                
                # Create a temporary script file that properly handles the SSH command
                script_content = '#!/bin/bash\n'
                script_content += 'exec '
                
                # Properly quote each argument to handle spaces and special characters
                for arg in command:
                    # Escape any single quotes in the argument
                    escaped_arg = arg.replace("'", "'\"'\"'")
                    script_content += f"'{escaped_arg}' "
                
                script_content += '\n'
                
                # Write to temporary file
                with tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False) as f:
                    f.write(script_content)
                    script_path = f.name
                
                # Make it executable
                os.chmod(script_path, 0o755)
                
                # Store the script path for cleanup
                self._temp_script_path = script_path
                
                # Use the script as the command (no additional args needed)
                pyxterm_cmd.extend(['--command', script_path])
            elif command[0] == 'bash' and len(command) >= 3 and command[1] == '-lc':
                # For bash -lc commands, the third argument is the command string
                # We need to create a script to properly execute it
                import tempfile
                
                # The command string is already shell-quoted, so we can use it directly
                command_string = command[2] if len(command) > 2 else ''
                
                # Create a temporary script that executes the command
                script_content = '#!/bin/bash\n'
                script_content += f'{command_string}\n'
                
                # Write to temporary file
                with tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False) as f:
                    f.write(script_content)
                    script_path = f.name
                
                # Make it executable
                os.chmod(script_path, 0o755)
                
                # Store the script path for cleanup
                self._temp_script_path = script_path
                
                # Use the script as the command (no additional args needed)
                pyxterm_cmd.extend(['--command', script_path])
            else:
                # For other commands, separate the executable from arguments
                # pyxtermjs expects --command to be just the executable and --cmd-args for arguments
                executable = command[0]
                args = command[1:] if len(command) > 1 else []
                
                pyxterm_cmd.extend(['--command', executable])
                if args:
                    # Join arguments with spaces for --cmd-args
                    args_string = ' '.join(args)
                    pyxterm_cmd.extend([f'--cmd-args={args_string}'])
        else:
            pyxterm_cmd.extend(['--command', 'bash'])

        try:
            # Replace the pyxtermjs template with our clean version
            self._backup_pyxtermjs_template()
            self._replace_pyxtermjs_template()
            
            # Start the pyxtermjs server in its own process group/session so the
            # parent process remains isolated from termination signals.
            # Capture stderr for better error reporting
            import tempfile
            stderr_file = tempfile.NamedTemporaryFile(mode='w+', delete=False, suffix='.log')
            stderr_file.close()
            
            # Ensure the subprocess can find the sshpilot module
            subprocess_env = dict(env) if env else dict(os.environ)
            current_pythonpath = subprocess_env.get('PYTHONPATH', '')
            # Get the project root (sshpilot directory)
            project_root = os.path.dirname(os.path.dirname(__file__))
            if project_root not in current_pythonpath:
                if current_pythonpath:
                    subprocess_env['PYTHONPATH'] = f"{project_root}:{current_pythonpath}"
                else:
                    subprocess_env['PYTHONPATH'] = project_root
            
            popen_kwargs: dict[str, Any] = {
                "stdout": subprocess.DEVNULL,
                "stderr": open(stderr_file.name, 'w'),
                "env": subprocess_env,
            }

            if cwd:
                popen_kwargs["cwd"] = cwd

            if os.name == "nt":  # pragma: no cover - Windows specific behaviour
                creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                if creationflags:
                    popen_kwargs["creationflags"] = creationflags
            else:
                # start_new_session ensures pyxtermjs becomes the leader of a new
                # session; this mirrors `preexec_fn=os.setsid` for older Python
                # versions while using the modern API when available.
                popen_kwargs["start_new_session"] = True

            logger.debug(f"Starting PyXterm server with command: {' '.join(pyxterm_cmd)}")
            logger.debug(f"Working directory: {cwd}")
            logger.debug(f"Environment PATH: {env.get('PATH', 'NOT_SET') if env else 'NOT_SET'}")

            self._server_process = subprocess.Popen(
                pyxterm_cmd,
                **popen_kwargs,
            )
            self._child_pid = self._server_process.pid
            
            # Wait for the server to be ready with retry logic
            max_retries = 10
            retry_delay = 0.5
            server_ready = False
            
            for attempt in range(max_retries):
                try:
                    # Test if the server is responding
                    import socket
                    with socket.create_connection(('127.0.0.1', port), timeout=1):
                        server_ready = True
                        logger.debug(f"PyXterm server ready on port {port} after {attempt + 1} attempts")
                        break
                except (socket.error, ConnectionRefusedError) as e:
                    logger.debug(f"Server not ready yet, attempt {attempt + 1}/{max_retries}: {e}")
                    # Check if the process is still running
                    if self._server_process and self._server_process.poll() is not None:
                        # Read stderr for error details
                        stderr_content = ""
                        try:
                            with open(stderr_file.name, 'r') as f:
                                stderr_content = f.read().strip()
                        except Exception:
                            pass
                        
                        error_msg = f"PyXterm server process exited early with return code: {self._server_process.returncode}"
                        if stderr_content:
                            error_msg += f"\nStderr output: {stderr_content}"
                        logger.error(error_msg)
                        break
                    time.sleep(retry_delay)
            
            if not server_ready:
                # Read stderr for error details
                stderr_content = ""
                try:
                    with open(stderr_file.name, 'r') as f:
                        stderr_content = f.read().strip()
                except Exception:
                    pass
                
                error_msg = f"PyXterm server failed to start on port {port} after {max_retries} attempts"
                if stderr_content:
                    error_msg += f"\nStderr output: {stderr_content}"
                logger.error(error_msg)
                raise RuntimeError(error_msg)
            
            # Clean up stderr file
            try:
                os.unlink(stderr_file.name)
            except Exception:
                pass
            
            # Load the terminal in WebView
            if self._webview:
                uri = f"http://127.0.0.1:{port}"
                logger.debug(f"Loading WebView with URI: {uri}")
                self._webview.load_uri(uri)
                
                # Connect to load-changed signal to track when WebView is ready
                try:
                    self._webview.connect('load-changed', self._on_webview_load_changed)
                except Exception:
                    logger.debug("Failed to connect to WebView load-changed signal", exc_info=True)

            if callback:
                def _notify() -> bool:
                    try:
                        callback(self.widget, self._child_pid or 0, None, user_data)
                    except TypeError:
                        callback(self.widget, None)
                    return False

                GLib.idle_add(_notify)

        except Exception as e:
            # Clean up stderr file if it exists
            try:
                if 'stderr_file' in locals():
                    os.unlink(stderr_file.name)
            except Exception:
                pass
                
            logger.error(f"Failed to start pyxtermjs server: {e}")
            if callback:
                def _notify_error(error=e) -> bool:
                    try:
                        callback(self.widget, None, error, user_data)
                    except TypeError:
                        callback(self.widget, None)
                    return False
                GLib.idle_add(_notify_error)

    def connect_child_exited(self, callback: Callable[[Gtk.Widget, int], None]) -> Any:
        # pyxtermjs does not expose child-exited notifications directly, but we can
        # monitor the server process to detect when it exits
        if not self._server_process:
            return None
            
        # Store the callback for later use
        self._child_exited_callback = callback
        
        # Start monitoring the server process
        self._monitor_server_process()
        
        # Return a dummy handler ID
        return "pyxtermjs_child_exited"

    def _monitor_server_process(self):
        """Monitor the pyxtermjs server process for exit"""
        if not self._server_process or not hasattr(self, '_child_exited_callback'):
            return
            
        def check_process():
            if self._server_process and self._server_process.poll() is not None:
                # Process has exited
                exit_code = self._server_process.returncode
                logger.debug(f"PyXtermJS server process exited with code: {exit_code}")
                
                # Call the child-exited callback
                if self._child_exited_callback:
                    try:
                        self._child_exited_callback(self.widget, exit_code)
                    except Exception as e:
                        logger.error(f"Error in child-exited callback: {e}")
                
                return False  # Stop monitoring
            return True  # Continue monitoring
            
        # Check every 100ms
        GLib.timeout_add(100, check_process)

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

    def copy_clipboard(self) -> None:
        """Copy selected text from xterm.js to clipboard"""
        if not self.available:
            return
        try:
            script = """
            if (typeof window.term !== 'undefined' && window.term.hasSelection()) {
                var selection = window.term.getSelection();
                if (selection) {
                    navigator.clipboard.writeText(selection).then(function() {
                        console.log('Text copied to clipboard');
                    }).catch(function(err) {
                        console.error('Failed to copy text:', err);
                    });
                }
            }
            """
            self._run_javascript(script)
        except Exception as e:
            logger.debug(f"Failed to copy from PyXterm backend: {e}", exc_info=True)

    def paste_clipboard(self) -> None:
        """Paste clipboard content into xterm.js terminal"""
        if not self.available:
            return
        try:
            script = """
            if (typeof navigator !== 'undefined' && navigator.clipboard) {
                navigator.clipboard.readText().then(function(text) {
                    if (typeof window.term !== 'undefined') {
                        window.term.paste(text);
                    }
                }).catch(function(err) {
                    console.error('Failed to paste text:', err);
                });
            }
            """
            self._run_javascript(script)
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
            
            # Calculate new font size based on base font size and scale
            if self._base_font_size is not None:
                new_font_size = int(self._base_font_size * scale)
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
        # pyxterm.js handles user input via websocket.
        return

    def get_content(self, max_chars: Optional[int] = None) -> Optional[str]:
        # pyxterm.js does not currently expose scrollback; return None for compatibility
        return None

    def search_set_regex(self, regex: Optional[Any]) -> None:
        """Set the search pattern for xterm.js search addon.
        
        According to xterm.js search addon API:
        - findNext(term: string, searchOptions?: ISearchOptions): boolean
        - ISearchOptions includes: regex, caseSensitive, wholeWord, incremental, decorations
        """
        if not self.available or not self._webview:
            return
        
        # Extract search term and options from regex or use it directly if it's a string
        search_term = None
        is_regex = False
        case_sensitive = True  # Default to case-sensitive
        
        if regex is None:
            search_term = None
        elif isinstance(regex, str):
            # For PyXterm, we receive the pattern as a string
            # Check if it has (?i) prefix (case-insensitive flag from VTE)
            search_term = regex
            if search_term.startswith("(?i)"):
                search_term = search_term[4:]
                case_sensitive = False
            
            # Detect if this is a regex pattern
            # terminal.py does: pattern = text if regex else re.escape(text)
            # So if regex=True, pattern has unescaped special chars
            # If regex=False, pattern has all special chars escaped
            # Heuristic: if pattern has unescaped regex special chars, it's likely a regex
            import re as re_module
            # Check for unescaped regex special characters
            # Pattern: not preceded by backslash, followed by regex special char
            unescaped_regex_chars = re_module.search(r'(?<!\\)[*+?|()[\]{}^$]', search_term)
            # Also check for common regex patterns like ^ at start or $ at end
            has_regex_anchors = search_term.startswith('^') or search_term.endswith('$')
            
            if unescaped_regex_chars or has_regex_anchors:
                # Likely a regex - verify it compiles
                try:
                    re_module.compile(search_term)
                    is_regex = True
                except re_module.error:
                    # Invalid regex, treat as literal
                    is_regex = False
            else:
                # No unescaped special chars, likely a literal (escaped) pattern
                is_regex = False
        else:
            # For VTE regex object, we can't extract the pattern easily
            # This shouldn't happen for PyXterm, but handle it gracefully
            return
        
        self._current_search_term = search_term
        self._current_search_is_regex = is_regex
        self._current_search_case_sensitive = case_sensitive
        
        # Ensure search addon is accessible
        self._ensure_search_addon_accessible()
        
        # Set the search term according to xterm.js search addon API
        if search_term is not None:
            # Escape the search term for JavaScript string
            escaped_term = search_term.replace('\\', '\\\\').replace("'", "\\'").replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r')
            # Build search options according to ISearchOptions interface
            search_options = {
                'caseSensitive': case_sensitive,
                'regex': is_regex
            }
            options_json = f"caseSensitive: {str(case_sensitive).lower()}, regex: {str(is_regex).lower()}"
            search_js = f"""
            (function() {{
                if (typeof window.term !== 'undefined' && window.term.searchAddon) {{
                    window.term.searchAddon.findNext('{escaped_term}', {{{options_json}}});
                }}
            }})();
            """
            self._run_javascript(search_js)
        else:
            # Clear search using clearDecorations() according to API
            clear_js = """
            (function() {
                if (typeof window.term !== 'undefined' && window.term.searchAddon) {
                    window.term.searchAddon.clearDecorations();
                }
            })();
            """
            self._run_javascript(clear_js)

    def _ensure_search_addon_accessible(self) -> None:
        """Ensure the search addon is accessible via window.term.searchAddon."""
        if self._search_addon_loaded or not self.available:
            return
        # The search addon is now stored in the template, so it should be accessible
        # Mark as loaded (we'll check availability in the search methods)
        self._search_addon_loaded = True

    def search_find_next(self) -> bool:
        """Find next occurrence of the search term.
        
        According to xterm.js search addon API:
        - findNext(term: string, searchOptions?: ISearchOptions): boolean
        """
        if not self.available or not self._current_search_term:
            return False
        
        self._ensure_search_addon_accessible()
        
        escaped_term = self._current_search_term.replace('\\', '\\\\').replace("'", "\\'").replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r')
        options_json = f"caseSensitive: {str(self._current_search_case_sensitive).lower()}, regex: {str(self._current_search_is_regex).lower()}"
        search_js = f"""
        (function() {{
            if (typeof window.term !== 'undefined' && window.term.searchAddon) {{
                window.term.searchAddon.findNext('{escaped_term}', {{{options_json}}});
            }}
        }})();
        """
        self._run_javascript(search_js)
        return True  # API returns boolean, but we can't get return value from async JS

    def search_find_previous(self) -> bool:
        """Find previous occurrence of the search term.
        
        According to xterm.js search addon API:
        - findPrevious(term: string, searchOptions?: ISearchOptions): boolean
        """
        if not self.available or not self._current_search_term:
            return False
        
        self._ensure_search_addon_accessible()
        
        escaped_term = self._current_search_term.replace('\\', '\\\\').replace("'", "\\'").replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r')
        options_json = f"caseSensitive: {str(self._current_search_case_sensitive).lower()}, regex: {str(self._current_search_is_regex).lower()}"
        search_js = f"""
        (function() {{
            if (typeof window.term !== 'undefined' && window.term.searchAddon) {{
                window.term.searchAddon.findPrevious('{escaped_term}', {{{options_json}}});
            }}
        }})();
        """
        self._run_javascript(search_js)
        return True  # API returns boolean, but we can't get return value from async JS

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



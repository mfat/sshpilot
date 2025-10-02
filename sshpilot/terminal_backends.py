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

        # Initialize with a fallback widget
        self.widget: Gtk.Widget = Gtk.Box()

        try:
            vendored_module = importlib.import_module("sshpilot.vendor.pyxtermjs")
            self._vendored_pyxterm = vendored_module

            try:
                external_module = importlib.import_module("pyxtermjs")
            except Exception:
                logger.debug("External pyxtermjs package not found; using vendored copy")
                pyxterm_module = vendored_module
            else:
                pyxterm_module = external_module

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
            elif load_event == 4:  # WEBKIT_LOAD_FINISHED
                logger.debug("WebView load finished, applying focus")
                # Apply focus after WebView is fully loaded
                def apply_focus():
                    try:
                        logger.debug("WebView load-finished: applying focus")
                        # Just focus the WebView - HTML template handles terminal focus
                        self.widget.grab_focus()
                        logger.debug("Focus applied after WebView load finished")
                    except Exception:
                        logger.debug("Failed to apply focus after WebView load", exc_info=True)
                    return False  # Don't repeat
                
                # Small delay to ensure page is ready
                GLib.timeout_add(200, apply_focus)
            elif load_event == 5:  # WEBKIT_LOAD_FAILED
                logger.error("WebView load failed - this may indicate connection refused error")
                # Emit connection failed signal to the terminal widget
                if hasattr(self.owner, 'emit'):
                    self.owner.emit('connection-failed', 'Could not connect to PyXterm server: Connection refused')
        except Exception:
            logger.debug("Error in WebView load-changed handler", exc_info=True)

    def set_font(self, font_desc: "Pango.FontDescription") -> None:
        # Web backend manages fonts via CSS; ignore request.
        return

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
        """Replace the pyxtermjs template with our clean version"""
        try:
            import shutil

            module = self._vendored_pyxterm or self._pyxterm
            if not module:
                return

            pyxtermjs_path = Path(module.__file__).resolve().parent
            original_template = pyxtermjs_path / "index.html"
            clean_template = Path(__file__).resolve().parent / "resources" / "pyxtermjs_clean.html"

            if clean_template.exists():
                shutil.copy2(clean_template, original_template)
                logger.debug("Replaced pyxtermjs template with clean version")
        except Exception as e:
            logger.debug(f"Failed to replace pyxtermjs template: {e}")

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

    def apply_theme(self, theme_name: Optional[str] = None) -> None:  # type: ignore[override]
        # Web based terminal handles its own theming through CSS.
        pass

    def set_font(self, font_desc: Pango.FontDescription) -> None:
        # PyXterm backend doesn't support font changes at runtime
        # Font is handled through CSS in the web interface
        logger.debug("PyXterm backend: Font changes not supported at runtime")

    def grab_focus(self) -> None:
        if not self.available:
            return
        try:
            self.widget.grab_focus()
        except Exception:
            logger.debug("Failed to focus pyxterm widget", exc_info=True)
    
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
        command = list(argv)
        
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
        # Web view handles clipboard internally; no-op here.
        return

    def paste_clipboard(self) -> None:
        return

    def select_all(self) -> None:
        return

    def reset(self, clear_scrollback: bool, clear_screen: bool) -> None:
        if self._terminal_id:
            manager = getattr(self._pyxterm, "TerminalManager", None)
            if manager:
                manager().reset(self._terminal_id)  # type: ignore[attr-defined]

    def set_font_scale(self, scale: float) -> None:
        # Font scaling handled through CSS by the embedded terminal.
        return

    def get_font_scale(self) -> float:
        return 1.0

    def feed(self, data: bytes) -> None:
        # pyxterm.js receives data via websocket; nothing to do here.
        return

    def feed_child(self, data: bytes) -> None:
        # pyxterm.js handles user input via websocket.
        return

    def search_set_regex(self, regex: Optional[Any]) -> None:
        # Search is handled by the web component.
        return

    def search_find_next(self) -> bool:
        return False

    def search_find_previous(self) -> bool:
        return False

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



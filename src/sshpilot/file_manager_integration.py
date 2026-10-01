"""Helpers for launching the appropriate file manager integration."""

from __future__ import annotations

import logging
import os
import shutil
import shlex
from gettext import gettext as _
from typing import Any, Optional, Tuple

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk

from .platform_utils import is_flatpak, is_macos

logger = logging.getLogger(__name__)


def quote_remote_path(path: str) -> str:
    """Quote a remote path for the shell, leaving a leading ~ to expand."""
    if path == "~":
        return "~"
    if path.startswith("~/"):
        return "~/" + shlex.quote(path[2:])
    return shlex.quote(path)


# --- "should hide/show X" capability helpers -------------------------------
# These live here (not in preferences.py) so callers that only need a boolean
# don't drag the full Preferences module onto the startup import path.

def macos_third_party_terminal_available() -> bool:
    """Check if a third-party terminal is available on macOS."""
    if not is_macos():
        return False

    terminals = [
        "iterm2",
        "ghostty",
        "alacritty",
        "iterm",
        "terminator",
        "kitty",
        "tmux",
        "warp",
    ]

    applications_dir = "/Applications"
    try:
        for entry in os.listdir(applications_dir):
            lower = entry.lower()
            if any(lower.startswith(t) and entry.endswith(".app") for t in terminals):
                return True
    except Exception:
        pass

    for terminal in terminals:
        if shutil.which(terminal):
            return True

    return False


def should_hide_external_terminal_options() -> bool:
    """Check if external terminal options should be hidden.

    Returns True when running in Flatpak or when on macOS without a supported
    third-party terminal.
    """
    return is_flatpak() or (
        is_macos() and not macos_third_party_terminal_available()
    )


def open_internal_file_manager(
    *,
    user: str,
    host: str,
    port: Optional[int] = None,
    parent_window: Any = None,
    nickname: Optional[str] = None,
    connection: Any = None,
    connection_manager: Any = None,
    ssh_config: Optional[dict] = None,
):
    """Instantiate and present the built-in file manager window."""

    from .file_manager_window import launch_file_manager_window

    window = launch_file_manager_window(
        host=host,
        username=user,
        port=port or 22,
        path="~",
        parent=parent_window,
        nickname=nickname,
        connection=connection,
        connection_manager=connection_manager,
        ssh_config=ssh_config,
    )

    return window


if isinstance(getattr(Gtk, 'Box', None), type):

    class FileManagerTabEmbed(Gtk.Box):
        """Container for hosting the built-in file manager inside a tab."""

        def __init__(self, controller: Any, content: Gtk.Widget) -> None:
            super().__init__(orientation=Gtk.Orientation.VERTICAL)
            self.set_hexpand(True)
            self.set_vexpand(True)
            self._controller = controller

            if content.get_parent() is not None:
                content.unparent()

            self._content = content
            self.append(content)
            self.connect('destroy', self._on_destroy)

            # Terminal panels (TerminalWidgets shown below the file manager),
            # one per side: "local" under the Local pane, "remote" under the
            # Remote one; mirror of TerminalWidget's files panel.
            self._terminal_panels: dict = {}
            self._terminal_teardowns: dict = {}
            self._terminal_panel_paned = None
            self._terminal_split = None
            self._terminal_split_binding = None

        # ── terminal panels (terminals below the file manager) ──────────────

        def has_terminal_panel(self) -> bool:
            return bool(self._terminal_panels)

        def get_terminal_panel(self, side: str = "remote"):
            return self._terminal_panels.get(side)

        def set_terminal_panel(self, terminal, teardown=None, side: str = "remote") -> None:
            """Show *terminal* below the file manager, on *side*.

            The page child stays this embed, so tab bookkeeping and the
            embed-subtree teardown search are unaffected. *teardown* is invoked
            from clear_terminal_panel() so the caller can disconnect the
            terminal and drop it from the window tracking dicts.
            """
            if side in self._terminal_panels:
                self.clear_terminal_panel(side)
            self._terminal_panels[side] = terminal
            self._terminal_teardowns[side] = teardown
            self._layout_terminal_panels()

        def clear_terminal_panel(self, side: Optional[str] = None) -> None:
            """Remove the terminal on *side* (all of them when None)."""
            sides = [side] if side is not None else list(self._terminal_panels)
            for name in sides:
                if self._terminal_panels.pop(name, None) is None:
                    continue
                teardown = self._terminal_teardowns.pop(name, None)
                # Disconnect the terminal while the tree is still intact.
                if teardown is not None:
                    try:
                        teardown()
                    except Exception:
                        logger.debug("Terminal panel teardown failed", exc_info=True)
            self._layout_terminal_panels()

        @staticmethod
        def _new_paned(orientation) -> Gtk.Paned:
            paned = Gtk.Paned(orientation=orientation)
            # A wide handle draws two hairlines in libadwaita; the thin one keeps
            # its larger invisible grab area, so dragging is unaffected.
            paned.set_wide_handle(False)
            paned.set_hexpand(True)
            paned.set_vexpand(True)
            paned.set_shrink_start_child(False)
            paned.set_shrink_end_child(False)
            return paned

        def _release_terminal_split(self) -> None:
            split = self._terminal_split
            self._terminal_split = None
            if self._terminal_split_binding is not None:
                self._terminal_split_binding.unbind()
                self._terminal_split_binding = None
            if split is not None:
                # set_*_child(None) instead of unparent(): unparenting a Paned
                # child can silently fail in GTK4 (see split_view._release_paned).
                split.set_start_child(None)
                split.set_end_child(None)

        def _layout_terminal_panels(self) -> None:
            """Lay the terminals out below the manager: one fills the width,
            two sit side by side under their file panes (local left)."""
            terminals = [
                self._terminal_panels[name] for name in ("local", "remote")
                if name in self._terminal_panels
            ]
            paned = self._terminal_panel_paned
            try:
                if paned is not None:
                    paned.set_end_child(None)
                self._release_terminal_split()

                if not terminals:
                    if paned is not None:
                        self._terminal_panel_paned = None
                        paned.set_start_child(None)
                        self.remove(paned)
                        self.append(self._content)
                    return

                if paned is None:
                    paned = self._new_paned(Gtk.Orientation.VERTICAL)
                    self.remove(self._content)
                    paned.set_start_child(self._content)
                    self.append(paned)
                    self._terminal_panel_paned = paned
                    self._place_terminal_divider(paned)

                if len(terminals) == 1:
                    paned.set_end_child(terminals[0])
                    return
                split = self._new_paned(Gtk.Orientation.HORIZONTAL)
                split.set_start_child(terminals[0])
                split.set_end_child(terminals[1])
                # Keep each terminal under its file pane as either divider moves.
                file_panes = getattr(self._controller, "_panes", None)
                if isinstance(file_panes, Gtk.Paned):
                    from gi.repository import GObject

                    self._terminal_split_binding = file_panes.bind_property(
                        "position", split, "position",
                        GObject.BindingFlags.BIDIRECTIONAL
                        | GObject.BindingFlags.SYNC_CREATE,
                    )
                self._terminal_split = split
                paned.set_end_child(split)
            except Exception:
                logger.debug("Terminal panel layout failed", exc_info=True)

        def _place_terminal_divider(self, paned: Gtk.Paned) -> None:
            def _apply_position() -> bool:
                if self._terminal_panel_paned is not paned:
                    return False  # panel was cleared before allocation
                height = self.get_height()
                if height <= 0:
                    return True  # not allocated yet — retry on idle
                paned.set_position(int(height * 0.6))
                return False

            if _apply_position():
                GLib.idle_add(_apply_position)

        def _on_destroy(self, *_args) -> None:
            controller = getattr(self, '_controller', None)
            if controller is None:
                return

            self._controller = None

            cleanup = getattr(controller, "_cleanup_manager", None)
            if callable(cleanup):
                try:
                    cleanup()
                except Exception as exc:  # pragma: no cover - defensive cleanup
                    logger.debug("Failed to cleanup embedded file manager backend: %s", exc)
            else:
                manager = getattr(controller, '_manager', None)
                if manager is not None and hasattr(manager, 'close'):
                    try:
                        manager.close()
                    except Exception as exc:  # pragma: no cover - defensive cleanup
                        logger.debug("Failed to close embedded file manager backend: %s", exc)

            try:
                controller.destroy()
            except Exception as exc:  # pragma: no cover - defensive cleanup
                logger.debug("Failed to destroy embedded file manager controller: %s", exc)

else:  # pragma: no cover - fallback for test doubles

    class FileManagerTabEmbed:  # type: ignore[misc]
        """Lightweight fallback used when Gtk.Box is unavailable (test doubles)."""

        def __init__(self, controller: Any, content: Any) -> None:
            self._controller = controller
            self._content = content
            self._destroy_handlers: list[Any] = []
            self._terminal_panels: dict = {}

        def has_terminal_panel(self) -> bool:
            return False

        def get_terminal_panel(self, side: str = "remote"):
            return None

        def clear_terminal_panel(self, side: Optional[str] = None) -> None:
            return None

        # Compatibility shims used by window code
        def set_hexpand(self, *_args, **_kwargs):
            return None

        def set_vexpand(self, *_args, **_kwargs):
            return None

        def append(self, *_args, **_kwargs):
            return None

        def connect(self, signal: str, callback):
            if signal == 'destroy':
                self._destroy_handlers.append(callback)
            return None

        # Manual cleanup helper for tests to simulate GTK destroy
        def destroy(self):
            for handler in list(self._destroy_handlers):
                try:
                    handler(self)
                except Exception:
                    pass


def create_internal_file_manager_tab(
    *,
    user: str,
    host: str,
    port: Optional[int] = None,
    nickname: Optional[str] = None,
    parent_window: Any = None,
    connection: Any = None,
    connection_manager: Any = None,
    ssh_config: Optional[dict] = None,
) -> Tuple[Gtk.Widget, Any]:
    """Create an embedded file manager suitable for use inside a tab."""

    app = Gtk.Application.get_default()
    if app is None:
        raise RuntimeError("An application instance is required to embed the file manager")

    from .file_manager_window import FileManagerWindow

    daemon_client = getattr(parent_window, "client", None) if parent_window else None
    bridge = getattr(parent_window, "client_bridge", None) if parent_window else None
    connection_id = None
    if connection is not None:
        connection_id = str(getattr(connection, "nickname", None) or getattr(connection, "id", None) or "")

    controller = FileManagerWindow(
        application=app,
        host=host,
        username=user,
        port=port or 22,
        initial_path="~",
        nickname=nickname,
        connection=connection,
        connection_manager=connection_manager,
        ssh_config=ssh_config,
        daemon_client=daemon_client,
        bridge=bridge,
        connection_id=connection_id,
        config=getattr(parent_window, "config", None) if parent_window else None,
    )
    # Remove the controller window from the application so it does not count
    # as a top-level window while embedded in a tab.
    # GTK 4.18 (GNOME Platform 50) calls gdk_surface_get_display() inside
    # remove_window() for D-Bus shell integration cleanup.  That path
    # asserts GDK_IS_SURFACE(surface), which fails when the window was never
    # realized.  Realizing before removal gives GTK a valid surface to query.
    if app is not None:
        try:
            controller.realize()
            app.remove_window(controller)
        except Exception:
            pass

    content = controller.detach_for_embedding(parent_window)
    widget = FileManagerTabEmbed(controller, content)
    return widget, controller


def launch_remote_file_manager(
    *,
    user: str,
    host: str,
    port: Optional[int] = None,
    nickname: Optional[str] = None,
    parent_window: Any = None,
    error_callback: Optional[Any] = None,
    connection: Any = None,
    connection_manager: Any = None,
    ssh_config: Optional[dict] = None,
) -> Tuple[bool, Optional[str], Optional[Any]]:
    """Open the built-in file manager window for the supplied connection."""

    try:
        window = open_internal_file_manager(
            user=user,
            host=host,
            port=port,
            parent_window=parent_window,
            nickname=nickname,
            connection=connection,
            connection_manager=connection_manager,
            ssh_config=ssh_config,
        )
        return True, None, window
    except Exception as exc:
        logger.error("Internal file manager failed: %s", exc)
        message = (
            _("Failed to open internal file manager: {error}").format(error=exc)
            if str(exc) else _("Failed to open internal file manager")
        )
        if error_callback:
            try:
                error_callback(message)
            except Exception:  # pragma: no cover - defensive
                logger.debug("Error callback failed when reporting internal manager error")
        return False, message, None

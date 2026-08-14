"""Helpers for launching the appropriate file manager integration."""

from __future__ import annotations

import logging
import os
import shutil
from functools import lru_cache
from typing import Any, Optional, Tuple

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk

from .platform_utils import is_flatpak, is_macos
from .sftp_utils import open_remote_in_file_manager

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def has_native_gvfs_support() -> bool:
    """Return True when the platform supports GVFS based file management."""

    if is_macos() or is_flatpak():
        return False

    try:
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio  # noqa: F401  # pylint: disable=unused-import
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Gio not available for GVFS integration: %s", exc)
        return False

    if shutil.which("gio") or shutil.which("gvfs-mount"):
        return True

    gvfs_paths = [
        f"/run/user/{os.getuid()}/gvfs",
        f"/var/run/user/{os.getuid()}/gvfs",
        os.path.expanduser("~/.gvfs"),
    ]

    for path in gvfs_paths:
        try:
            if os.path.isdir(path):
                return True
        except Exception:  # pragma: no cover - filesystem quirks
            continue

    return False


@lru_cache(maxsize=1)
def has_internal_file_manager() -> bool:
    """Return True when the built-in file manager window is available."""

    try:
        from . import file_manager_window as _file_manager_window
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Internal file manager unavailable: %s", exc)
        return False

    return hasattr(_file_manager_window, "FileManagerWindow")


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


def should_show_force_internal_file_manager_toggle() -> bool:
    """Return True when the built-in toggle for forcing the internal manager should be shown."""

    try:
        if is_macos():
            return False
        return bool(has_native_gvfs_support())
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Failed to determine GVFS availability: %s", exc)
        return False


def should_hide_file_manager_options() -> bool:
    """Check if file manager options should be hidden.

    File manager UI should only be hidden when neither the native GVFS
    integration nor the built-in manager are available. This allows the
    Manage Files button to remain visible on platforms like macOS or Flatpak
    where the new in-app manager is preferred.
    """

    try:
        if has_native_gvfs_support():
            return False
        if has_internal_file_manager():
            return False
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("File manager capability detection failed: %s", exc)
    return True


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

            # Terminal panel (a TerminalWidget shown below the file manager);
            # mirror of TerminalWidget's files panel — see set_terminal_panel().
            self._terminal_panel = None
            self._terminal_panel_paned = None
            self._terminal_panel_teardown = None

        # ── terminal panel (terminal below the file manager) ────────────────

        def has_terminal_panel(self) -> bool:
            return self._terminal_panel is not None

        def set_terminal_panel(self, terminal, teardown=None) -> None:
            """Show *terminal* below the file manager in a vertical Gtk.Paned.

            The page child stays this embed, so tab bookkeeping and the
            embed-subtree teardown search are unaffected. *teardown* is invoked
            from clear_terminal_panel() so the caller can disconnect the
            terminal and drop it from the window tracking dicts.
            """
            if self._terminal_panel is not None:
                self.clear_terminal_panel()

            paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
            paned.set_wide_handle(True)
            paned.set_hexpand(True)
            paned.set_vexpand(True)
            paned.set_shrink_start_child(False)
            paned.set_shrink_end_child(False)
            self.remove(self._content)
            paned.set_start_child(self._content)
            paned.set_end_child(terminal)
            self.append(paned)

            self._terminal_panel = terminal
            self._terminal_panel_paned = paned
            self._terminal_panel_teardown = teardown

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

        def clear_terminal_panel(self) -> None:
            """Remove the terminal panel and restore the manager-only layout."""
            paned = self._terminal_panel_paned
            teardown = self._terminal_panel_teardown
            self._terminal_panel = None
            self._terminal_panel_paned = None
            self._terminal_panel_teardown = None
            if paned is None:
                return
            # Disconnect the terminal while the tree is still intact.
            if teardown is not None:
                try:
                    teardown()
                except Exception:
                    logger.debug("Terminal panel teardown failed", exc_info=True)
            try:
                # set_*_child(None) instead of unparent(): unparenting a Paned
                # child can silently fail in GTK4 (see split_view._release_paned).
                paned.set_end_child(None)
                paned.set_start_child(None)
                self.remove(paned)
                self.append(self._content)
            except Exception:
                logger.debug("Terminal panel layout restore failed", exc_info=True)

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
            self._terminal_panel = None

        def has_terminal_panel(self) -> bool:
            return False

        def clear_terminal_panel(self) -> None:
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
    """Launch the appropriate file manager for the supplied connection."""

    if has_native_gvfs_support():
        success, error_msg = open_remote_in_file_manager(
            user=user,
            host=host,
            port=port,
            error_callback=error_callback,
            parent_window=parent_window,
            connection=connection,
            connection_manager=connection_manager,
            ssh_config=ssh_config,
        )
        return success, error_msg, None

    if has_internal_file_manager():
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
            message = str(exc) or "Failed to open internal file manager"
            if error_callback:
                try:
                    error_callback(message)
                except Exception:  # pragma: no cover - defensive
                    logger.debug("Error callback failed when reporting internal manager error")
            return False, message, None

    message = "No compatible file manager integration is available."
    if error_callback:
        try:
            error_callback(message)
        except Exception:  # pragma: no cover - defensive
            logger.debug("Error callback failed when reporting missing integrations")

    logger.warning("No file manager integration available for %s@%s", user, host)
    return False, message, None

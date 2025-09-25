"""Helpers for launching the appropriate file manager integration."""

from __future__ import annotations

import logging
import os
import shutil
from functools import lru_cache
from typing import Any, Optional, Tuple

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk

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

            self.append(content)
            self.connect('destroy', self._on_destroy)

        def _on_destroy(self, *_args) -> None:
            controller = getattr(self, '_controller', None)
            if controller is None:
                return

            self._controller = None

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
    )

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

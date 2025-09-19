"""Helpers for launching the appropriate file manager integration."""

from __future__ import annotations

import logging
import os
import shutil
from functools import lru_cache
from typing import Any, Optional, Tuple

import gi

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
):
    """Instantiate and present the built-in file manager window."""

    from .file_manager_window import FileManagerWindow

    window = FileManagerWindow(
        user=user,
        host=host,
        port=port,
        parent=parent_window,
        nickname=nickname,
    )

    try:
        window.present()
    except Exception:  # pragma: no cover - testing stubs may not implement present
        pass

    return window


def launch_remote_file_manager(
    *,
    user: str,
    host: str,
    port: Optional[int] = None,
    nickname: Optional[str] = None,
    parent_window: Any = None,
    error_callback: Optional[Any] = None,
) -> Tuple[bool, Optional[str], Optional[Any]]:
    """Launch the appropriate file manager for the supplied connection."""

    if has_native_gvfs_support():
        success, error_msg = open_remote_in_file_manager(
            user=user,
            host=host,
            port=port,
            error_callback=error_callback,
            parent_window=parent_window,
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

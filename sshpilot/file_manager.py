"""High-level APIs for launching remote file manager sessions."""
from __future__ import annotations

from typing import Callable, Optional, Tuple

from .sftp_utils import open_remote_in_file_manager as _open_remote_in_file_manager


def open_connection_in_file_manager(
    connection,
    *,
    path: Optional[str] = None,
    parent_window=None,
    error_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, Optional[str]]:
    """Open the provided connection in the system file manager.

    The helper normalizes connection attributes before delegating to the
    lower-level SFTP utilities so callers don't need to duplicate logic for
    stripping default ports or extracting credentials from the connection
    object.
    """

    username = getattr(connection, "username", None)
    host = getattr(connection, "host", None)
    port = getattr(connection, "port", None)

    if port == 22:
        port = None

    return open_remote_location(
        user=username,
        host=host,
        port=port,
        path=path,
        parent_window=parent_window,
        error_callback=error_callback,
    )


def open_remote_location(
    *,
    user: str,
    host: str,
    port: Optional[int] = None,
    path: Optional[str] = None,
    parent_window=None,
    error_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, Optional[str]]:
    """Open a remote SFTP location in the system file manager."""

    return _open_remote_in_file_manager(
        user=user,
        host=host,
        port=port,
        path=path,
        error_callback=error_callback,
        parent_window=parent_window,
    )


__all__ = [
    "open_connection_in_file_manager",
    "open_remote_location",
]

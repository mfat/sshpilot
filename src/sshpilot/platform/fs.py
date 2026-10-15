"""File-system helpers whose correct form differs on Windows."""
from __future__ import annotations

import os
import sys
from typing import Optional


def makedirs_private(path: "os.PathLike[str] | str") -> None:
    """``os.makedirs(path, mode=0o700, exist_ok=True)``, safe for OpenSSH.

    For directories holding files OpenSSH for Windows checks (private keys,
    ``authorized_keys``). Since Python 3.13, ``mkdir(mode=0o700)`` on Windows
    gives the new directory a protected ACL with an ``OWNER RIGHTS`` entry;
    files Python writes inside inherit it, and OpenSSH then refuses them
    ("Bad permissions. Try removing permissions for user: OWNER RIGHTS").
    Windows therefore creates the directory without a mode, so it inherits
    the user profile's owner-only ACL.
    """
    if sys.platform == "win32":
        os.makedirs(path, exist_ok=True)
    else:
        os.makedirs(path, mode=0o700, exist_ok=True)


def fsync_directory(directory: "os.PathLike[str] | str") -> None:
    """Make a completed rename or unlink inside *directory* durable.

    Raises ``OSError`` when the directory cannot be opened or synced; callers
    convert it to their own error. A failure to close the descriptor after a
    successful sync is ignored, since the sync already happened.

    A no-op on Windows, which cannot open a directory to fsync it; NTFS
    journals the rename itself.
    """
    if sys.platform == "win32":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    dir_fd: Optional[int] = None
    try:
        dir_fd = os.open(directory, flags)
        os.fsync(dir_fd)
    finally:
        if dir_fd is not None:
            try:
                os.close(dir_fd)
            except OSError:
                pass

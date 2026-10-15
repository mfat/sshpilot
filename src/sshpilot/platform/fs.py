"""File-system helpers whose correct form differs on Windows."""
from __future__ import annotations

import os
import sys
from typing import Optional


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

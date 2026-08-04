"""Atomic known-hosts byte storage (GTK-free).

Provides byte-level read and atomic write for the known-hosts document. The
write path refuses symlink destinations, creates missing parents, preserves
the existing file mode (``0o644`` for new files), writes through a
same-directory temporary file with ``fsync``, atomically replaces the target,
and ``fsync``s the parent directory so the rename is durable. File contents
and filesystem paths are never logged or surfaced in errors.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import Optional

from ..errors import CoreError, ErrorCode


def _core_error(exc: BaseException) -> CoreError:
    # Deliberately generic: the original OS error text can embed the full
    # target or temporary path, which callers must never surface.
    return CoreError(
        ErrorCode.KNOWN_HOST_IO_ERROR,
        "Known-hosts file operation failed",
    )


def _cleanup_temp(fd: Optional[int], tmp_name: Optional[str]) -> None:
    """Close and unlink a half-written temporary file after a failure."""
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    if tmp_name is not None:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def _refuse_symlink(path: Path) -> None:
    """Raise ``CoreError`` if *path* is a symbolic link (non-following)."""
    if os.path.islink(path):
        raise CoreError(
            ErrorCode.KNOWN_HOST_IO_ERROR,
            "Refusing to write through a symbolic link",
        )


def read_known_hosts_bytes(path: Path) -> bytes:
    """Read exact known-hosts bytes; a missing file yields ``b\"\"``."""

    path = Path(path)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""
    except OSError as exc:
        raise _core_error(exc) from exc


def atomic_write_known_hosts_bytes(path: Path, content: bytes) -> None:
    """Atomically write *content* to *path*, preserving mode and refusing symlinks."""

    if type(content) is not bytes:
        raise TypeError("content must be bytes")
    path = Path(path)

    try:
        _refuse_symlink(path)
        path.parent.mkdir(parents=True, exist_ok=True)
    except CoreError:
        raise
    except OSError as exc:
        raise _core_error(exc) from exc

    # Read existing metadata WITHOUT following the target. If the target
    # exists but its metadata cannot be read, that is an error, not a silent
    # fallback to 0644. Only a truly absent target gets the default mode.
    existing_mode: Optional[int] = None
    try:
        st = os.lstat(path)
        existing_mode = stat.S_IMODE(st.st_mode)
    except FileNotFoundError:
        existing_mode = None
    except OSError as exc:
        raise _core_error(exc) from exc
    mode = existing_mode if existing_mode is not None else 0o644

    fd: Optional[int] = None
    tmp_name: Optional[str] = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=".known_hosts-", suffix=".tmp"
        )
        # os.fdopen takes ownership of the descriptor; do not close it twice.
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)

        # Re-check the destination non-following right before the swap: if it
        # changed into a symlink since the initial validation, abort rather
        # than replacing whatever it points at.
        _refuse_symlink(path)

        os.replace(tmp_name, path)
        tmp_name = None  # consumed by the replace
    except CoreError:
        _cleanup_temp(fd, tmp_name)
        raise
    except Exception as exc:
        _cleanup_temp(fd, tmp_name)
        raise _core_error(exc) from exc

    # Make the rename durable. The temporary file is already gone by this
    # point; the replacement may already have succeeded when this fails, so
    # callers must treat a raised CoreError here as "durability unknown".
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    dir_fd: Optional[int] = None
    try:
        dir_fd = os.open(path.parent, flags)
        os.fsync(dir_fd)
    except OSError as exc:
        raise _core_error(exc) from exc
    finally:
        if dir_fd is not None:
            os.close(dir_fd)

"""Atomic known-hosts byte storage (GTK-free).

Provides byte-level read and atomic write for the known-hosts document. The
write path refuses symlink destinations, creates missing parents, preserves
the existing file mode (``0o644`` for new files), writes through a
same-directory temporary file with ``fsync``, atomically replaces the target,
and ``fsync``s the parent directory so the rename is durable. File contents are
never logged.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import Optional

from ..errors import CoreError, ErrorCode


def _core_error(exc: BaseException) -> CoreError:
    return CoreError(ErrorCode.KNOWN_HOST_IO_ERROR, str(exc))


def read_known_hosts_bytes(path: Path) -> bytes:
    """Read exact known-hosts bytes; a missing file yields ``b""``."""

    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""
    except OSError as exc:
        raise _core_error(exc) from exc


def atomic_write_known_hosts_bytes(path: Path, content: bytes) -> None:
    """Atomically write *content* to *path*, preserving mode and refusing symlinks."""

    path = Path(path)
    if path.is_symlink():
        raise CoreError(
            ErrorCode.KNOWN_HOST_IO_ERROR,
            f"Refusing to write through symlink: {path}",
        )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _core_error(exc) from exc
    existing_mode: Optional[int] = None
    if path.exists():
        try:
            existing_mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            existing_mode = None
    mode = existing_mode if existing_mode is not None else 0o644

    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".known_hosts-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except Exception as exc:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise _core_error(exc) from exc

    # Make the rename durable. The temporary file is already gone by this
    # point, so nothing needs cleanup when this fails.
    dir_fd: Optional[int] = None
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
        os.fsync(dir_fd)
    except OSError as exc:
        raise _core_error(exc) from exc
    finally:
        if dir_fd is not None:
            os.close(dir_fd)

"""Small formatting helpers used by the file-manager UI (sizes, times, mode)."""

from __future__ import annotations

import stat
from datetime import datetime
from typing import Optional

from ..api.models.operations import RemoteFileType


def _type_char(mode: int, file_type: Optional[RemoteFileType]) -> str:
    """Leading ``ls -l`` style type character.

    Remote entries carry permission bits only (``mode & 0o7777``), so their
    file type must come from ``RemoteFileEntry.file_type`` rather than the
    stat bits.
    """
    if file_type is RemoteFileType.DIRECTORY:
        return "d"
    if file_type is RemoteFileType.SYMLINK:
        return "l"
    if file_type is RemoteFileType.BLOCK:
        return "b"
    if file_type is RemoteFileType.CHARACTER:
        return "c"
    if file_type is RemoteFileType.SOCKET:
        return "s"
    if file_type is RemoteFileType.FIFO:
        return "p"
    if file_type is not None:
        return "-"
    return "d" if stat.S_ISDIR(mode) else "-"


def _human_size(n: int) -> str:
    """Convert bytes to human readable format."""
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or unit == "PB":
            return f"{n:.0f} {unit}" if n >= 10 or unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return "0 B"


def _human_time(ts: float) -> str:
    """Convert timestamp to human readable format."""
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "—"


def _mode_to_str(mode: int, file_type: Optional[RemoteFileType] = None) -> str:
    """Convert file mode to string representation like -rw-r--r--."""
    type_char = _type_char(mode, file_type)
    perm = ""
    for who, shift in (("USR", 6), ("GRP", 3), ("OTH", 0)):
        r = "r" if mode & (4 << shift) else "-"
        w = "w" if mode & (2 << shift) else "-"
        x = "x" if mode & (1 << shift) else "-"
        perm += r + w + x
    return type_char + perm


def _mode_to_octal(mode: int) -> str:
    """Convert file mode to octal representation like 755."""
    # Extract only the permission bits (last 9 bits)
    perm_bits = mode & 0o777
    return oct(perm_bits)[2:]  # Remove '0o' prefix

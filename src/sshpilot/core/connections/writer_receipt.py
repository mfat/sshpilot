"""Immutable writer-issued write receipts.

A :class:`WriteReceipt` is issued by an atomic writer immediately after it has
replaced (or created) a target file.  It records the path the writer wrote,
the exact bytes the writer committed, and the on-disk identity (device, inode,
mode) the writer observed on the freshly-written file.

The repository never rediscovers the post-write file by reopening the path and
trusting whatever it finds.  Instead it asks the writer for a receipt and then
*verifies* that the file currently at the path still matches the receipt's
identity, bytes, and mode.  This closes the write-to-verification window in
which an external actor could swap the file at the path: if the current file
no longer matches the receipt, the repository refuses to mark the file
daemon-owned, refuses to overwrite or delete it, resynchronizes from disk, and
raises ``MUTATION_AMBIGUOUS``.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..errors import CoreError, ErrorCode


@dataclass(frozen=True)
class WriteReceipt:
    """Immutable record of a single successful atomic write.

    Attributes:
        path: the absolute path the writer wrote to.
        expected_bytes: the exact bytes the writer committed.
        dev: device id of the freshly-written file (``os.fstat``).
        inode: inode number of the freshly-written file (``os.fstat``).
        mode: permission bits the writer applied (``stat.S_IMODE``).
    """

    path: Path
    expected_bytes: bytes
    dev: int
    inode: int
    mode: int

    @property
    def identity(self) -> tuple:
        return (self.dev, self.inode)


def issue_receipt(path: Path, expected_bytes: bytes, mode: int) -> WriteReceipt:
    """Build a :class:`WriteReceipt` for a freshly-written *path*.

    Opens *path* with ``O_NOFOLLOW`` and requires a regular file before
    capturing the device, inode, and mode.  Must be called only after the
    writer has atomically replaced the target so the captured identity is the
    writer's own product, not whatever an external actor may leave behind.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags)
    try:
        st = os.fstat(fd)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise CoreError(
                ErrorCode.MUTATION_AMBIGUOUS,
                "The mutation target became unsafe after daemon write",
            )
        return WriteReceipt(
            path=Path(path),
            expected_bytes=bytes(expected_bytes),
            dev=st.st_dev,
            inode=st.st_ino,
            mode=stat.S_IMODE(st.st_mode),
        )
    finally:
        os.close(fd)


def verify_receipt(receipt: Optional[WriteReceipt]) -> None:
    """Verify the file at ``receipt.path`` still matches the receipt.

    Required checks, all performed with ``O_NOFOLLOW``:

    * the receipt is present (no missing receipt is trusted);
    * the current file is a regular file (not a symlink or directory);
    * the device/inode matches the receipt identity;
    * the current bytes equal the receipt ``expected_bytes``;
    * the current mode equals the receipt mode.

    Any failure raises :class:`CoreError` with ``MUTATION_AMBIGUOUS`` **without
    modifying the file at the path**.  Callers must resynchronize from disk.

    A ``None`` receipt means no atomic writer ever issued a receipt for this
    write — that is rejected as ambiguous rather than trusted.
    """
    if receipt is None:
        raise CoreError(
            ErrorCode.MUTATION_AMBIGUOUS,
            "The daemon write has no authenticated receipt",
        )
    path = Path(receipt.path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(path), flags)
    except (FileNotFoundError, OSError) as exc:
        # ``O_NOFOLLOW`` on a symlink yields ELOOP; either way the file at
        # the path is not the regular file the writer produced.
        raise CoreError(
            ErrorCode.MUTATION_AMBIGUOUS,
            "The mutation target changed during receipt verification",
        ) from exc
    try:
        st = os.fstat(fd)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise CoreError(
                ErrorCode.MUTATION_AMBIGUOUS,
                "The mutation target became unsafe during receipt verification",
            )
        if (st.st_dev, st.st_ino) != receipt.identity:
            raise CoreError(
                ErrorCode.MUTATION_AMBIGUOUS,
                "The mutation target was replaced externally",
            )
        if stat.S_IMODE(st.st_mode) != receipt.mode:
            raise CoreError(
                ErrorCode.MUTATION_AMBIGUOUS,
                "The mutation target mode was changed externally",
            )
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        current = b"".join(chunks)
        if current != receipt.expected_bytes:
            raise CoreError(
                ErrorCode.MUTATION_AMBIGUOUS,
                "The mutation target was modified externally",
            )
    finally:
        os.close(fd)
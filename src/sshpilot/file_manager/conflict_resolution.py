"""Pre-transfer conflict resolution session (GTK-free).

Drives Nautilus-style per-item Cancel / Skip / Replace / Rename decisions
before a batch upload or download starts. The file-manager dialog presents
each pending conflict; this module owns the apply-to-all bookkeeping and the
final ``(source, destination)`` list.
"""

from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, Optional, Sequence, Tuple

TransferPair = Tuple[str, str]


class ConflictAction(str, Enum):
    CANCEL = "cancel"
    SKIP = "skip"
    REPLACE = "replace"
    RENAME = "rename"


@dataclass(frozen=True)
class ConflictSideInfo:
    """Display metadata for one side of a conflict (source or destination)."""

    path: str
    is_directory: bool
    size: Optional[int] = None
    mtime: Optional[float] = None
    is_symlink: bool = False


@dataclass(frozen=True)
class ConflictItem:
    """One destination path that already exists for a pending transfer."""

    source: ConflictSideInfo
    destination: ConflictSideInfo
    destination_directory_name: str
    suggested_name: Optional[str] = None

    @property
    def pair(self) -> TransferPair:
        return (self.source.path, self.destination.path)

    @property
    def is_merge(self) -> bool:
        return self.source.is_directory and self.destination.is_directory

    @property
    def conflict_name(self) -> str:
        return os.path.basename(self.destination.path.rstrip("/")) or self.destination.path


@dataclass(frozen=True)
class ConflictResponse:
    action: ConflictAction
    apply_to_all: bool = False
    new_name: Optional[str] = None


class ConflictResolutionSession:
    """Walk conflicts one-by-one, honouring apply-to-all skip/replace."""

    def __init__(
        self,
        files_to_transfer: Sequence[TransferPair],
        conflicts: Sequence[ConflictItem],
    ) -> None:
        conflict_pairs = {item.pair for item in conflicts}
        self._pending: List[ConflictItem] = list(conflicts)
        self._resolved: List[TransferPair] = [
            pair for pair in files_to_transfer if pair not in conflict_pairs
        ]
        self._skip_all = False
        self._replace_all = False
        self.cancelled = False

    @property
    def remaining_count(self) -> int:
        return len(self._pending)

    @property
    def resolved_pairs(self) -> List[TransferPair]:
        return list(self._resolved)

    def next_conflict(self) -> Optional[ConflictItem]:
        """Return the next conflict that still needs a user decision."""
        while self._pending:
            if self._skip_all:
                self._pending.pop(0)
                continue
            if self._replace_all:
                item = self._pending.pop(0)
                self._resolved.append(item.pair)
                continue
            return self._pending[0]
        return None

    def apply(self, response: ConflictResponse) -> bool:
        """Apply *response* to the current conflict.

        Returns ``False`` when the whole transfer should be aborted (cancel).
        """
        if not self._pending:
            return response.action is not ConflictAction.CANCEL
        item = self._pending.pop(0)
        if response.action is ConflictAction.CANCEL:
            self.cancelled = True
            self._pending.clear()
            self._resolved.clear()
            return False
        if response.action is ConflictAction.SKIP:
            if response.apply_to_all:
                self._skip_all = True
            return True
        if response.action is ConflictAction.REPLACE:
            self._resolved.append(item.pair)
            if response.apply_to_all:
                self._replace_all = True
            return True
        if response.action is ConflictAction.RENAME:
            new_name = (response.new_name or "").strip()
            if not new_name or new_name in (".", "..") or "/" in new_name or "\\" in new_name:
                # Treat invalid rename as cancel of this item only by re-queueing.
                self._pending.insert(0, item)
                return True
            new_dest = _replace_basename(item.destination.path, new_name)
            self._resolved.append((item.source.path, new_dest))
            return True
        self._pending.insert(0, item)
        return True


def _replace_basename(path: str, new_name: str) -> str:
    """Replace the final path component, preserving remote vs local separators."""
    if path.startswith("/"):
        parent = posixpath.dirname(path.rstrip("/")) or "/"
        return f"/{new_name}" if parent == "/" else f"{parent}/{new_name}"
    parent = os.path.dirname(path)
    return os.path.join(parent, new_name) if parent else new_name


def local_side_info(path: str) -> ConflictSideInfo:
    """Build :class:`ConflictSideInfo` from a local filesystem path."""
    try:
        st = os.lstat(path)
    except OSError:
        return ConflictSideInfo(path=path, is_directory=False)
    return ConflictSideInfo(
        path=path,
        is_directory=os.path.isdir(path),
        size=None if os.path.isdir(path) else int(st.st_size),
        mtime=float(st.st_mtime),
        is_symlink=os.path.islink(path),
    )


def remote_side_info_from_entry(path: str, entry: object) -> ConflictSideInfo:
    """Build side info from a daemon :class:`RemoteFileEntry`-like object."""
    from sshpilot.api.models.operations import RemoteFileType

    file_type = getattr(entry, "file_type", None)
    is_directory = file_type is RemoteFileType.DIRECTORY
    is_symlink = file_type is RemoteFileType.SYMLINK
    size = getattr(entry, "size", None)
    modified_at = getattr(entry, "modified_at", None)
    mtime: Optional[float] = None
    if modified_at is not None:
        try:
            mtime = float(modified_at.timestamp())
        except Exception:
            mtime = None
    return ConflictSideInfo(
        path=path,
        is_directory=is_directory,
        size=None if is_directory else size,
        mtime=mtime,
        is_symlink=is_symlink,
    )


def destination_taken_checker(
    destination_path: str,
    *,
    reserved_basenames: Optional[Sequence[str]] = None,
    remote_exists: Optional[Callable[[str], bool]] = None,
) -> Callable[[str], bool]:
    """Return ``is_taken(basename)`` for suggested-name generation."""

    parent = os.path.dirname(destination_path)
    # Remote absolute paths use posix parents.
    if destination_path.startswith("/"):
        parent = posixpath.dirname(destination_path.rstrip("/")) or "/"

    reserved = {name for name in (reserved_basenames or ()) if name}

    def _is_taken(basename: str) -> bool:
        if basename in reserved:
            return True
        if destination_path.startswith("/"):
            candidate = (
                f"/{basename}" if parent == "/" else f"{parent}/{basename}"
            )
            if remote_exists is not None:
                return bool(remote_exists(candidate))
            return False
        candidate = os.path.join(parent, basename) if parent else basename
        return os.path.exists(candidate)

    return _is_taken

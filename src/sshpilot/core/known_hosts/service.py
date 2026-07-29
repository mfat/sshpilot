"""Known-hosts parsing and atomic edits (GTK-free)."""
from __future__ import annotations

import logging
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from ..errors import CoreError, ErrorCode

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KnownHostEntry:
    """One known_hosts line, parsed for display / matching."""

    line: str
    hostname: str = ""
    key_type: str = ""
    key_data: str = ""

    @classmethod
    def parse(cls, line: str) -> "KnownHostEntry":
        raw = line.rstrip("\n")
        parts = raw.split()
        if len(parts) >= 3:
            return cls(line=raw, hostname=parts[0], key_type=parts[1], key_data=parts[2])
        return cls(line=raw)


@dataclass(frozen=True)
class KnownHostsEditPlan:
    """Result of planning a known_hosts mutation (apply separately)."""

    path: Path
    entries: Tuple[str, ...]
    removed: Tuple[str, ...] = ()


def load_known_hosts(path: Path | str) -> List[KnownHostEntry]:
    """Load and parse known_hosts entries (blank lines skipped)."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise CoreError(ErrorCode.KNOWN_HOST_IO_ERROR, str(exc)) from exc
    entries: List[KnownHostEntry] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        entries.append(KnownHostEntry.parse(line))
    return entries


def filter_entries(
    entries: Sequence[KnownHostEntry], query: str
) -> List[KnownHostEntry]:
    """Case-insensitive substring filter over the raw line."""
    q = (query or "").lower().strip()
    if not q:
        return list(entries)
    return [e for e in entries if q in e.line.lower()]


def plan_remove_entries(
    path: Path | str,
    entries: Sequence[KnownHostEntry],
    to_remove: Iterable[str],
) -> KnownHostsEditPlan:
    """Plan removal of exact line matches; returns remaining lines."""
    remove_set = set(to_remove)
    remaining = [e.line for e in entries if e.line not in remove_set]
    removed = [e.line for e in entries if e.line in remove_set]
    return KnownHostsEditPlan(
        path=Path(path),
        entries=tuple(remaining),
        removed=tuple(removed),
    )


def find_host_matches(
    entries: Sequence[KnownHostEntry], host: str
) -> List[KnownHostEntry]:
    """Match entries whose hostname field mentions *host* (hash-aware substring)."""
    needle = (host or "").strip().lower()
    if not needle:
        return []
    matches: List[KnownHostEntry] = []
    for entry in entries:
        # Hashed hostnames start with '|1|'; still allow substring on raw line.
        if needle in entry.hostname.lower() or needle in entry.line.lower():
            matches.append(entry)
    return matches


def save_known_hosts(
    path: Path | str,
    lines: Sequence[str],
    *,
    preserve_mode: bool = True,
) -> None:
    """Atomically write known_hosts, preserving mode and refusing symlink clobber."""
    path = Path(path)
    if path.is_symlink():
        raise CoreError(
            ErrorCode.KNOWN_HOST_IO_ERROR,
            f"Refusing to write through symlink: {path}",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode: Optional[int] = None
    if preserve_mode and path.exists():
        try:
            existing_mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            existing_mode = None

    text = ("\n".join(lines) + "\n") if lines else ""
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".known_hosts-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        mode = existing_mode if existing_mode is not None else 0o644
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except Exception as exc:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise CoreError(ErrorCode.KNOWN_HOST_IO_ERROR, str(exc)) from exc

"""Shared helpers for SSH key validation and metadata."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional, Set

logger = logging.getLogger(__name__)

_SKIPPED_FILENAMES: Set[str] = {"config", "known_hosts", "authorized_keys"}


def _looks_like_private_key(file_path: Path) -> bool:
    """Cheap header sniff for a private SSH key — reads only the first bytes.

    Recognizes the PEM/OpenSSH armor ``-----BEGIN [<type> ]PRIVATE KEY-----`` (which
    is present for encrypted keys too) and PuTTY ``.ppk`` files. This replaces a
    per-file ``ssh-keygen`` spawn during key discovery, which was a startup hotspot
    (one Node/subprocess per ``~/.ssh`` file). Actual key *use* still validates via
    the SSH connection path.
    """
    try:
        with open(file_path, "rb") as fh:
            head = fh.read(256)
    except OSError:
        return False
    text = head.decode("utf-8", "ignore")
    if "PuTTY-User-Key-File-" in text:
        return True
    return "-----BEGIN" in text and "PRIVATE KEY-----" in text


def _is_private_key(
    file_path: Path,
    *,
    cache: Optional[Dict[str, bool]] = None,
    skipped_filenames: Optional[Set[str]] = None,
) -> bool:
    """Return ``True`` when *file_path* looks like a private SSH key.

    Uses a cheap header sniff (no ``ssh-keygen`` subprocess). Results are optionally
    memoized in *cache*. ``skipped_filenames`` overrides the default skip list.
    """
    name = file_path.name
    skip_names = skipped_filenames if skipped_filenames is not None else _SKIPPED_FILENAMES
    if not file_path.is_file() or name.endswith(".pub") or name in skip_names:
        return False

    key_path = str(file_path)
    if cache is not None:
        cached = cache.get(key_path)
        if cached is not None:
            return cached

    result = _looks_like_private_key(file_path)
    if cache is not None:
        cache[key_path] = result
    return result


__all__ = ["_SKIPPED_FILENAMES", "_is_private_key", "_looks_like_private_key"]

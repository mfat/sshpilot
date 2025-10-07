"""Shared helpers for SSH key validation and metadata."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Dict, Optional, Set

logger = logging.getLogger(__name__)

_SKIPPED_FILENAMES: Set[str] = {"config", "known_hosts", "authorized_keys"}


def _is_private_key(
    file_path: Path,
    *,
    cache: Optional[Dict[str, bool]] = None,
    skipped_filenames: Optional[Set[str]] = None,
) -> bool:
    """Return ``True`` when *file_path* looks like a private SSH key.

    The helper optionally caches validation results in *cache* to avoid
    redundant ``ssh-keygen`` executions. ``skipped_filenames`` allows callers
    to provide an alternate skip list while defaulting to the shared set.
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

    cmd = ["ssh-keygen", "-y", "-f", key_path, "-P", ""]
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        raise
    except Exception as exc:
        logger.debug("Failed to run ssh-keygen for %s: %s", key_path, exc, exc_info=True)
        if cache is not None:
            cache[key_path] = False
        return False

    stderr = completed.stderr or ""
    stdout = completed.stdout or ""
    stderr_lower = stderr.lower()

    success = completed.returncode == 0
    if not success:
        passphrase_hints = (
            "incorrect passphrase",
            "passphrase is required",
            "passphrase required",
            "no passphrase supplied",
            "bad passphrase",
        )
        if any(hint in stderr_lower for hint in passphrase_hints):
            success = True
            logger.debug(
                "ssh-keygen reported a protected key for %s: %s",
                key_path,
                stderr.strip() or stdout.strip() or "passphrase required",
            )
        elif "passphrase" in stderr_lower:
            # Future-proofing: log unexpected passphrase-related output but do not
            # treat it as success.
            logger.debug(
                "ssh-keygen returned passphrase-related output for %s: %s",
                key_path,
                stderr.strip() or stdout.strip(),
            )

    if success:
        if cache is not None:
            cache[key_path] = True
        return True

    message = stderr.strip() or stdout.strip() or f"ssh-keygen exited with {completed.returncode}"
    logger.debug("ssh-keygen rejected %s: %s", key_path, message)
    if cache is not None:
        cache[key_path] = False
    return False


__all__ = ["_is_private_key", "_SKIPPED_FILENAMES"]

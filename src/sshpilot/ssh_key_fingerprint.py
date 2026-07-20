"""SSH key fingerprint helpers (type · SHA256 · comment).

Extracted verbatim from connection_dialog.py into a leaf module so the parsing
and fingerprint lookup can be imported and tested without the GTK dialog. These
use ``ssh-keygen -l`` for *local* key fingerprinting only — they never open an
SSH connection, so they are unrelated to the single connection/auth path.
"""

import os
import logging
import subprocess
from typing import Dict

logger = logging.getLogger(__name__)

_FINGERPRINT_CACHE: Dict[str, tuple] = {}


def _parse_keygen_line(line: str) -> tuple:
    """Parse an ``ssh-keygen -l`` line into (type_label, "SHA256:…", comment).

    Example line: ``256 SHA256:abc… user@host (ED25519)``.
    """
    parts = (line or "").strip().split()
    if len(parts) < 2:
        return ("", "", "")
    fingerprint = parts[1]
    key_type = parts[-1].strip("()") if parts[-1].startswith("(") else ""
    comment = " ".join(parts[2:-1]) if len(parts) > 3 else (parts[2] if len(parts) > 2 else "")
    return (key_type, fingerprint, comment)


def _fingerprint_for_path(path: str) -> tuple:
    """(type_label, fingerprint, comment) for a key file, via ``ssh-keygen -lf``."""
    expanded = os.path.expanduser(path or "")
    if expanded in _FINGERPRINT_CACHE:
        return _FINGERPRINT_CACHE[expanded]
    result = ("", "", "")
    try:
        proc = subprocess.run(["ssh-keygen", "-lf", expanded],
                              capture_output=True, text=True, timeout=5)
        if proc.returncode == 0:
            result = _parse_keygen_line(proc.stdout)
    except Exception:
        logger.debug("ssh-keygen fingerprint failed for %s", path, exc_info=True)
    _FINGERPRINT_CACHE[expanded] = result
    return result


def _fingerprint_for_pub_line(pub_line: str) -> tuple:
    """(type_label, fingerprint, comment) for a public-key line (e.g. ssh-add -L)."""
    try:
        proc = subprocess.run(["ssh-keygen", "-lf", "-"],
                              input=(pub_line or "") + "\n",
                              capture_output=True, text=True, timeout=5)
        if proc.returncode == 0:
            return _parse_keygen_line(proc.stdout)
    except Exception:
        logger.debug("ssh-keygen fingerprint (stdin) failed", exc_info=True)
    return ("", "", "")

"""Pure metadata helpers for public SSH keys.

The frontend must not launch ``ssh-keygen``. These compatibility helpers read
only a public-key line and compute its OpenSSH-style SHA256 fingerprint; they
never open a private key or handle a passphrase.
"""

import base64
import hashlib
import logging
import os
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

_FINGERPRINT_CACHE: Dict[str, tuple] = {}


def _parse_keygen_line(line: str) -> tuple:
    """Parse a public-key line into ``(type, fingerprint, comment)``."""
    parts = (line or "").strip().split()
    if len(parts) < 2:
        return ("", "", "")
    key_name, encoded = parts[:2]
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return ("", "", "")
    fingerprint = (
        base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
    )
    key_type = {
        "ssh-ed25519": "ED25519",
        "ssh-rsa": "RSA",
    }.get(key_name, key_name.removeprefix("ecdsa-sha2-").upper())
    return key_type, f"SHA256:{fingerprint}", " ".join(parts[2:])


def _fingerprint_for_path(path: str) -> tuple:
    """Return public metadata for *path* without opening its private key."""
    expanded = os.path.expanduser(path or "")
    if expanded in _FINGERPRINT_CACHE:
        return _FINGERPRINT_CACHE[expanded]
    result = ("", "", "")
    public_path = Path(expanded)
    if public_path.suffix != ".pub":
        public_path = Path(f"{public_path}.pub")
    try:
        with public_path.open(encoding="utf-8") as handle:
            result = _parse_keygen_line(handle.readline(64 * 1024))
    except (OSError, UnicodeError):
        logger.debug("Public-key fingerprint unavailable", exc_info=True)
    _FINGERPRINT_CACHE[expanded] = result
    return result


def _fingerprint_for_pub_line(pub_line: str) -> tuple:
    """Return metadata for one public-key line (for example ``ssh-add -L``)."""
    return _parse_keygen_line(pub_line)

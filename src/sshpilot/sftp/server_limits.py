"""SSH/SFTP server-specific transfer limits.

OpenWrt and other appliances often speak Dropbear on the SSH channel while
still advertising a full OpenSSH SFTP subsystem (``limits@openssh.com``,
``posix-rename@openssh.com``, …). Concurrent uploads on that shared channel
stall; OpenSSH's own sshd tolerates them. Detect the SSH software banner —
not SFTP extensions — and serialize transfers only for Dropbear.
"""

from __future__ import annotations

import re
from typing import Optional

# OpenSSH logs this at DEBUG1 during the handshake, before the SFTP subsystem
# speaks. Captured from the client stderr of ``ssh … -s sftp``.
_REMOTE_SOFTWARE_RE = re.compile(
    r"remote software version\s+(\S+)",
    re.IGNORECASE,
)

# Matches ``dropbear``, ``dropbear_2024.85``, ``Dropbear``, …
_DROPBEAR_RE = re.compile(r"dropbear", re.IGNORECASE)


def parse_remote_software_version(stderr_text: str) -> Optional[str]:
    """Extract the remote SSH software token from OpenSSH client stderr."""
    if not stderr_text:
        return None
    match = _REMOTE_SOFTWARE_RE.search(stderr_text)
    if match is None:
        return None
    return match.group(1)


def is_dropbear_software(remote_software: Optional[str]) -> bool:
    """True when *remote_software* identifies a Dropbear SSH server."""
    if not remote_software:
        return False
    return _DROPBEAR_RE.search(remote_software) is not None


def transfer_concurrency_for_remote_software(
    remote_software: Optional[str],
    *,
    default: int,
    dropbear: int = 1,
) -> int:
    """Return the per-SFTP-service concurrent-transfer cap for this SSH server."""
    if is_dropbear_software(remote_software):
        return dropbear
    return default

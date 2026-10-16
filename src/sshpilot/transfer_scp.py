"""GTK-free native SCP operand and compatibility helpers."""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Tuple

# The characters shlex.quote() leaves alone; any other may be special to a
# glob or to a shell.
_REMOTE_PATH_UNSAFE_RE = re.compile(r"[^\w@%+=:,./-]")
SFTP_UNAVAILABLE_MESSAGE = (
    "Could not start an SFTP session on the remote host. The remote SSH server "
    "may not have an SFTP server enabled."
)
_SFTP_UNAVAILABLE_MARKERS = (
    "subsystem request failed",
    "sftp-server",
    "subsystem sftp",
    "received message too long",
    "eof during negotiation",
)
_LEGACY_FLAG_UNSUPPORTED_MARKERS = (
    "unknown option",
    "illegal option",
    "invalid option",
)


def escape_remote_source(path: str) -> str:
    """Make scp copy exactly *path*; a leading ``~/`` stays the remote home.

    scp expands a remote source as a glob, where ``.*`` and ``.[a.b]`` also
    match ``..``, and the legacy ``-O`` protocol hands it to the remote shell.
    Both take a backslash-escaped character literally.
    """
    if path == "~":
        return path
    home = "~/" if path.startswith("~/") else ""
    return home + _REMOTE_PATH_UNSAFE_RE.sub(r"\\\g<0>", path[len(home):])


def assemble_scp_transfer_args(
    target: str,
    sources: Iterable[str],
    destination: str,
    direction: str,
) -> Tuple[List[str], str]:
    direction = (direction or "").lower()
    if direction not in {"upload", "download"}:
        raise ValueError(f"Unsupported scp direction: {direction}")
    if direction == "upload":
        return [source for source in sources if source], f"{target}:{destination}"
    # A source is a path on ``target``, never an scp operand of its own: a
    # remote entry may be named ``user@host:path``.
    normalized = [
        f"{target}:{escape_remote_source(source)}" for source in sources if source
    ]
    return normalized, destination


def insert_legacy_scp_flag(argv: List[str]) -> List[str]:
    if not argv or "-O" in argv:
        return list(argv)
    return [argv[0], "-O", *argv[1:]]


def legacy_scp_flag_unsupported(error_text: Optional[str]) -> bool:
    lowered = str(error_text or "").lower()
    return any(marker in lowered for marker in _LEGACY_FLAG_UNSUPPORTED_MARKERS)


def classify_sftp_error(error_text: Optional[str]) -> Optional[str]:
    lowered = str(error_text or "").lower()
    if any(marker in lowered for marker in _SFTP_UNAVAILABLE_MARKERS):
        return SFTP_UNAVAILABLE_MESSAGE
    if "sftp" in lowered and "not found" in lowered:
        return SFTP_UNAVAILABLE_MESSAGE
    return None

"""GTK-free native SCP operand and compatibility helpers."""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Tuple

_REMOTE_SPEC_RE = re.compile(r"^[^@]+@(?:\[[^\]]+\]|[^:]+):.+$")
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


def _extract_host(target: str) -> str:
    host = target.split("@", 1)[-1]
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


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
    host = _extract_host(target)
    variants = {host, f"[{host}]"} if ":" in host else {host}
    normalized = []
    for source in sources:
        if not source:
            continue
        if source.startswith(f"{target}:") or any(
            source.startswith(f"{variant}:") for variant in variants
        ) or _REMOTE_SPEC_RE.match(source):
            normalized.append(source)
        else:
            normalized.append(f"{target}:{source}")
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

"""GTK-free classification of SSH connection evidence in terminal output."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from .interaction import classify_prompt


ConnectionEvidenceVerdict = Literal["connected", "failed", "pending"]


@dataclass(frozen=True)
class ConnectionEvidence:
    """Classification result for the recent output of a connecting session."""

    verdict: ConnectionEvidenceVerdict
    failure_reason: str = ""


# Substrings in terminal output that mean an SSH attempt is failing.
SSH_FAILURE_MARKERS = (
    "permission denied",
    "connection refused",
    "no route to host",
    "network is unreachable",
    "could not resolve",
    "name or service not known",
    "nodename nor servname",
    "host key verification failed",
    "connection timed out",
    "operation timed out",
    "too many authentication failures",
    "connection reset",
    "connection closed",
    "broken pipe",
    # telnet's connect failure ("telnet: Unable to connect to remote host")
    "unable to connect",
)

# Positive login evidence from SSH diagnostics or a remote login banner.
SSH_SUCCESS_MARKERS = (
    "authenticated to",
    "entering interactive session",
    "last login:",
    "pseudo-terminal will",
)

# Local SSH chatter is not evidence that the remote session is usable.
SSH_NOISE_PREFIXES = (
    "debug",
    "ssh:",
    "warning:",
    "openssh",
    "kex",
    "channel ",
    "authenticated",
    "connecting to",
    "pledge",
    "trying ",
    "the authenticity of",
)

SSH_HOSTKEY_BANNER_MARKERS = (
    "key fingerprint is",
    "key is not known by any other names",
    "known by the following other names",
    "known_hosts:",
)

# The GTK path scrapes already-rendered text, while the daemon sees raw PTY
# bytes. Normalize the common VT sequences so both paths classify the same
# visible text, including coloured shell prompts and OSC window titles.
_VT_ESCAPE_RE = re.compile(
    r"\x1b(?:"
    r"\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\[[0-?]*[ -/]*[@-~]"  # CSI
    r"|[@-_]"  # two-byte escape
    r")"
)


def visible_terminal_text(text: str) -> str:
    """Return terminal output reduced to the visible text used for evidence."""

    if not text:
        return ""
    cleaned = _VT_ESCAPE_RE.sub("", text)
    return cleaned.replace("\r\n", "\n").replace("\r", "\n")


def classify_connection_evidence(text: str) -> ConnectionEvidence:
    """Classify recent terminal output as connected, failed, or pending.

    Authentication prompts and local SSH diagnostics remain pending. Explicit
    success markers, login banners, shell prompts, and other remote output are
    connection evidence. This is shared by legacy GTK terminals and daemon
    sessions so they expose identical connection-status semantics.
    """

    normalized = visible_terminal_text(text).lower()
    if not normalized:
        return ConnectionEvidence("pending")

    evidence = ConnectionEvidence("pending")
    for line in normalized.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(marker in stripped for marker in SSH_FAILURE_MARKERS):
            evidence = ConnectionEvidence("failed", stripped)
            continue
        if any(marker in stripped for marker in SSH_SUCCESS_MARKERS):
            evidence = ConnectionEvidence("connected")
            continue
        # Authentication may legitimately retry. A prompt following a failure
        # means SSH is still trying, while later remote output supersedes both.
        if classify_prompt(stripped) is not None:
            if evidence.verdict != "connected":
                evidence = ConnectionEvidence("pending")
            continue
        if stripped.startswith(SSH_NOISE_PREFIXES):
            continue
        if any(marker in stripped for marker in SSH_HOSTKEY_BANNER_MARKERS):
            continue
        if stripped in {"yes", "no"}:
            continue
        evidence = ConnectionEvidence("connected")

    return evidence

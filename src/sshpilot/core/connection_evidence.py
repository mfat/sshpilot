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
    # ExitOnForwardFailure / port-forward setup (often after Authenticated to)
    "port forwarding failed",
    "address already in use",
    "cannot assign requested address",
    "channel_setup_failure",
    "administratively prohibited",
    "could not request local forwarding",
    "could not request remote forwarding",
    "cannot listen to port",
    "master forward request failed",
    "remote forward failure",
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

# Extra post-auth scan when classify_connection_evidence stayed connected
# (e.g. Authenticated to appeared before the fatal forward line was parsed
# as noise). Keep these specific — a bare \"forward\" matches happy-path mux
# chatter.
_POST_AUTH_FAILURE_HINTS = (
    "port forwarding failed",
    "forwarding failed",
    "forward request failed",
    "could not request",
    "cannot listen",
    "address already in use",
    "bind [",
    "bind:",
    "channel_setup",
    "administratively prohibited",
    "error: remote",
    "error: local",
    "fatal:",
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


def post_auth_exit_failure_reason(
    text: str,
    *,
    exit_code: int | None,
) -> str | None:
    """Best-effort failure text when a RUNNING session dies with a non-zero exit.

    Prefer a concrete OpenSSH line from the PTY. When the PTY is empty (common
    with LogLevel QUIET or some ControlMaster paths) but ssh still exited 255,
    return a stable generic message so the UI does not fall back to
    \"Connection lost\".
    """

    evidence = classify_connection_evidence(text)
    if evidence.verdict == "failed" and evidence.failure_reason:
        return evidence.failure_reason.strip()

    normalized = visible_terminal_text(text)
    last_hint = ""
    for line in normalized.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        if any(marker in lower for marker in SSH_FAILURE_MARKERS):
            last_hint = stripped
            continue
        if any(hint in lower for hint in _POST_AUTH_FAILURE_HINTS):
            # Skip pure debug chatter that happens to mention \"forward\".
            if lower.startswith("debug"):
                continue
            last_hint = stripped
    if last_hint:
        return last_hint

    # ssh(1) reserves 255 for its own fatals (including ExitOnForwardFailure).
    # Other non-zero codes are usually a remote shell/command exit — leave
    # those as a clean EXITED without inventing a SessionFailure.
    if exit_code == 255:
        return "The SSH session exited with status 255"
    return None

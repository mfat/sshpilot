"""GTK-free parsing of local OpenSSH diagnostics for daemon session readiness.

The daemon instruments owned SSH terminal launches with a private ``-v -E``
pair so OpenSSH's own verbose stream becomes authoritative local evidence of a
successful login.  This module turns that stream into typed results without
ever exposing the diagnostics to the user terminal:

* ``AUTHENTICATED`` — a direct connection completed::
    ``Authenticated to <target> (...) using "<method>".``
* ``MUX_SESSION_OPENED`` — a ControlMaster client session was handed off::
    ``... master session id: <integer>``
* ``FAILED`` — OpenSSH gave up (``Permission denied``, ``Connection refused``,
  ``Host key verification failed``, ExitOnForwardFailure bind errors, ...).
* ``PENDING`` — no decisive marker yet (prompts, KEX, retries).

The parser is stateful and feeds incrementally.  Authentication success is
reported once; after that the parser keeps reading so post-auth fatals that
OpenSSH writes only to ``-E`` (notably ExitOnForwardFailure) can still become
``FAILED``.  A ``FAILED`` verdict is terminal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .connection_evidence import SSH_FAILURE_MARKERS

# ``debug1: Authenticated to example.com ([127.0.0.1]:22) using "publickey".``
_AUTHENTICATED_RE = re.compile(
    r"authenticated to \S+ \([^)]*\) using \"[^\"]+\"\.?",
    re.IGNORECASE,
)
# ``debug1: mux_client_request_session: master session id: 0``
_MUX_SESSION_RE = re.compile(r"master session id:\s*(\d+)", re.IGNORECASE)
# OpenSSH debug chatter is prefixed ``debug1:``/``debug2:``/``debug3:``.
_DEBUG_LINE_RE = re.compile(r"debug\d*:", re.IGNORECASE)
# A terminal ``Permission denied`` line carries the ``user@host:`` prefix:
# ``alice@example.com: Permission denied (publickey,password).``
_TERMINAL_PERMISSION_DENIED_RE = re.compile(
    r"\S+@\S+:\s*permission denied"
)
# The retriable form appears once per failed password attempt and is not
# terminal; OpenSSH keeps prompting until NumberOfPasswordPrompts is spent.
_RETRIABLE_PERMISSION_DENIED_RE = re.compile(
    r"permission denied,?\s+please try again\.?"
)

_AUTH_SUCCESS_STATES = frozenset(
    {
        "authenticated",
        "mux_session_opened",
    }
)


class SshDiagnosticState(str, Enum):
    """Decisive readiness verdicts for one daemon SSH terminal session.

    Internal daemon vocabulary; never exposed through the public API.
    """

    PENDING = "pending"
    AUTHENTICATED = "authenticated"
    MUX_SESSION_OPENED = "mux_session_opened"
    FAILED = "failed"


@dataclass(frozen=True)
class SshDiagnosticResult:
    """One decisive verdict plus the triggering diagnostic line."""

    state: SshDiagnosticState
    detail: str = ""


def _terminal_failure_detail(line: str) -> Optional[str]:
    """Return the failure detail for a non-debug line, or ``None``.

    Only genuinely terminal failures are returned: connect/name errors and
    the final ``user@host: Permission denied (...).`` line.  Retriable
    password rejections stay pending so interactive auth can continue.
    """
    lowered = line.lower()
    if _RETRIABLE_PERMISSION_DENIED_RE.search(lowered):
        return None
    if _TERMINAL_PERMISSION_DENIED_RE.search(lowered):
        return line.strip()
    for marker in SSH_FAILURE_MARKERS:
        if marker in lowered:
            return line.strip()
    return None


class SshDiagnosticParser:
    """Incremental parser for one session's ``-v -E`` diagnostic stream.

    Feed raw bytes as they are appended; partial lines are retained across
    calls.  Authentication success is returned once; further input is still
    scanned for post-auth ``FAILED`` lines (ExitOnForwardFailure).  ``FAILED``
    latches permanently.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._latched: Optional[SshDiagnosticResult] = None
        self._pending_emit: Optional[SshDiagnosticResult] = None

    def feed(self, data: bytes) -> Optional[SshDiagnosticResult]:
        """Consume *data* and return a newly observed decisive result."""
        if (
            self._latched is not None
            and self._latched.state is SshDiagnosticState.FAILED
        ):
            return None
        if data:
            self._buffer.extend(data)
        while b"\n" in self._buffer:
            line = self._buffer.partition(b"\n")[0]
            del self._buffer[: len(line) + 1]
            if (
                self._latched is not None
                and self._latched.state is SshDiagnosticState.FAILED
            ):
                break
            self._consume_line(line.decode("utf-8", errors="replace"))
            # One decisive event per feed() so AUTHENTICATED is not skipped
            # when ExitOnForwardFailure lines arrive in the same read chunk.
            if self._pending_emit is not None:
                break
        return self._take_pending()

    def close(self) -> Optional[SshDiagnosticResult]:
        """Flush a trailing unterminated line and return any new result."""
        if (
            self._latched is not None
            and self._latched.state is SshDiagnosticState.FAILED
        ):
            return None
        if self._buffer:
            line = bytes(self._buffer)
            self._buffer.clear()
            self._consume_line(line.decode("utf-8", errors="replace"))
        return self._take_pending()

    @property
    def pending(self) -> bool:
        return self._latched is None

    def _take_pending(self) -> Optional[SshDiagnosticResult]:
        result = self._pending_emit
        self._pending_emit = None
        return result

    def _emit(self, result: SshDiagnosticResult) -> None:
        self._latched = result
        self._pending_emit = result

    def _consume_line(self, line: str) -> None:
        if not line:
            return
        if (
            self._latched is not None
            and self._latched.state.value in _AUTH_SUCCESS_STATES
        ):
            # Auth already proven. OpenSSH still writes ExitOnForwardFailure
            # lines to -E after Authenticated to; keep scanning for those.
            if _DEBUG_LINE_RE.match(line):
                return
            detail = _terminal_failure_detail(line)
            if detail is not None:
                self._emit(SshDiagnosticResult(SshDiagnosticState.FAILED, detail))
            return
        if _AUTHENTICATED_RE.search(line):
            self._emit(
                SshDiagnosticResult(SshDiagnosticState.AUTHENTICATED, line.strip())
            )
            return
        if _MUX_SESSION_RE.search(line):
            self._emit(
                SshDiagnosticResult(
                    SshDiagnosticState.MUX_SESSION_OPENED, line.strip()
                )
            )
            return
        if _DEBUG_LINE_RE.match(line):
            return
        detail = _terminal_failure_detail(line)
        if detail is not None:
            self._emit(SshDiagnosticResult(SshDiagnosticState.FAILED, detail))

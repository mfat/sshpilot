"""Shared pieces of the built-in backends' one-line summaries and suggestions."""

from __future__ import annotations

import shlex
from typing import Any, List, Optional, Sequence

from .._command_failure import LocalCommandFailure, LocalCommandFailureReason


def host_port(host: Any, port: Any, default: Optional[int]) -> str:
    """``host``, or ``host:port`` when the port is not the protocol's own."""
    host = str(host or "").strip()
    if not host:
        return ""
    try:
        port = int(port) if port not in (None, "") else default
    except (TypeError, ValueError):
        port = default
    if port and port != default:
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{host}:{port}"
    return host


def with_user(user: Any, target: str) -> str:
    user = str(user or "").strip()
    return f"{user}@{target}" if user and target else target


def run_lines(ctx: Any, argv: Sequence[str], timeout: float = 8) -> List[str]:
    """Run a local tool for suggestions; its non-empty output lines.

    Failures carry a stable reason and the tool's output separately. The
    connection editor owns wording; external diagnostics stay opaque.
    """
    result = ctx._run_local_command(shlex.join(argv), timeout=timeout)
    if result.exit_code != 0:
        raise LocalCommandFailure(
            LocalCommandFailureReason.EXITED,
            program=argv[0],
            status=result.exit_code,
            diagnostic=result.stderr or result.stdout or "",
        )
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]

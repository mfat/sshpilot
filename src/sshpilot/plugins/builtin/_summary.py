"""Shared pieces of the built-in backends' one-line summaries and suggestions."""

from __future__ import annotations

import shlex
from typing import Any, List, Optional, Sequence


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

    Raises with the tool's own first error line, which the editor shows in
    place of the list -- "no context named x" is the answer the user needs.
    """
    result = ctx.run_local_command(shlex.join(argv), timeout=timeout)
    if result.exit_code != 0:
        message = (result.stderr or result.stdout or "").strip().splitlines()
        raise RuntimeError(message[0] if message else f"{argv[0]} failed")
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]

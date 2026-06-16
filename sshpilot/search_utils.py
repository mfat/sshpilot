from __future__ import annotations

from typing import Any


def connection_matches(connection: Any, query: str) -> bool:
    """Return True if connection matches the search query.

    The search checks the connection's nickname, host/IP address and tags in
    a case-insensitive manner.

    The query may contain several whitespace-separated keywords. Every keyword
    must match at least one field (logical AND across keywords, OR across
    fields), so ``"prod web"`` selects a host tagged both ``production`` and
    ``web``. A single keyword keeps the previous substring behaviour, so IPs
    such as ``"10.0.0.5"`` still match as one term.
    """
    if not query:
        return True
    keywords = query.lower().split()
    if not keywords:
        return True
    fields = [
        getattr(connection, "nickname", ""),
        getattr(connection, "host", ""),
        " ".join(getattr(connection, "tags", None) or []),
    ]
    fields = [(field or "").lower() for field in fields]
    return all(
        any(keyword in field for field in fields)
        for keyword in keywords
    )


__all__ = ["connection_matches"]

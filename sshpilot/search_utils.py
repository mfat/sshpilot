from __future__ import annotations

from typing import Any


def connection_matches(connection: Any, query: str) -> bool:
    """Return True if connection matches the search query.

    The search checks the connection's nickname, host, and IP address in a
    case-insensitive manner.
    """
    if not query:
        return True
    text = query.lower()
    fields = [
        getattr(connection, "nickname", ""),
        getattr(connection, "host", ""),
        getattr(connection, "ip", ""),
    ]
    return any(text in (field or "").lower() for field in fields)


__all__ = ["connection_matches"]

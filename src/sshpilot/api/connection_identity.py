"""Stable connection identity helpers shared by daemon API consumers."""
from __future__ import annotations
from typing import Any
from .errors import ErrorCode, SshPilotError
from .models.common import ConnectionId


def connection_id_for(connection: Any) -> ConnectionId:
    """Return the durable API identifier for a connection snapshot."""
    nickname = str(
        getattr(connection, "nickname", None)
        or getattr(connection, "id", None)
        or ""
    ).strip()
    if not nickname:
        raise SshPilotError(
            ErrorCode.INTERNAL_ERROR,
            "A stored connection has no durable identity",
        )
    return ConnectionId(nickname)

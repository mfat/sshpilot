"""Stable connection identity helpers shared by daemon API consumers."""
from __future__ import annotations
from typing import Any
from .errors import ErrorCode, SshPilotError
from .models.common import ConnectionId


def connection_id_for(connection: Any) -> ConnectionId:
    """Return the durable API identifier for a connection snapshot.

    An unsaved target (``ssh user@host`` typed in the search box) keeps its
    user-facing nickname for display and the save dialog, and carries the id
    the daemon registered it under in ``transient_connection_id``.
    """
    transient = getattr(connection, "transient_connection_id", None)
    if isinstance(transient, str) and transient.strip():
        return ConnectionId(transient.strip())
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

"""Stable connection identity helpers shared by daemon API consumers."""
from __future__ import annotations
from typing import Any
from .errors import ErrorCode, SshPilotError
from .models.common import ConnectionId


def connection_id_for(connection: Any) -> ConnectionId:
    """Return the durable API identifier for a connection snapshot."""
    raw_identifier = getattr(connection, "id", None)
    identifier = raw_identifier.strip() if isinstance(raw_identifier, str) else ""
    if not identifier:
        raw_identifier = getattr(connection, "nickname", None)
        identifier = raw_identifier.strip() if isinstance(raw_identifier, str) else ""
    if not identifier:
        raise SshPilotError(
            ErrorCode.INTERNAL_ERROR,
            "A stored connection has no durable identity",
        )
    return ConnectionId(identifier)

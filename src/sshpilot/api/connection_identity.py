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


def file_manager_connection_id(connection: Any) -> str:
    """The daemon id the file manager opens SFTP under, or ``""``.

    Same identity as :func:`connection_id_for` -- an unsaved target opened
    from the search box is registered under its transient id, not its
    nickname -- but a connection with no identity yields ``""`` instead of
    raising, as the file manager callers always did.
    """
    try:
        return str(connection_id_for(connection))
    except SshPilotError:
        return ""

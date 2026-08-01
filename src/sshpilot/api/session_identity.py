"""Session identity helpers (non-UUID)."""

from __future__ import annotations

from typing import Any
from sshpilot.api.models.common import SessionId, require_identifier
from sshpilot.runtime_identity import new_session_id as new_session_id


def session_id_from_raw(value: Any) -> SessionId:
    """Format or validate one session identifier."""
    val = require_identifier(str(value or "").strip(), "session id")
    return SessionId(val)

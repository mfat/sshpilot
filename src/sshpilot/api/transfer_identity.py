"""Transfer identity helpers (non-UUID)."""

from __future__ import annotations

from typing import Any
from sshpilot.api.models.common import TransferId, require_identifier
from sshpilot.runtime_identity import new_transfer_id as new_transfer_id


def transfer_id_from_raw(value: Any) -> TransferId:
    """Format or validate one transfer identifier."""
    val = require_identifier(str(value or "").strip(), "transfer id")
    return TransferId(val)

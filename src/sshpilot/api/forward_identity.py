"""Port-forward identity helpers (non-UUID)."""

from __future__ import annotations

from typing import Any
from sshpilot.api.models.common import ForwardId, require_identifier
from sshpilot.runtime_identity import new_forward_id as new_forward_id


def forward_id_from_raw(value: Any) -> ForwardId:
    """Format or validate one forward identifier."""
    val = require_identifier(str(value or "").strip(), "forward id")
    return ForwardId(val)

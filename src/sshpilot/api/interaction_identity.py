"""Interaction identity helpers (non-UUID)."""

from __future__ import annotations

from typing import Any
from sshpilot.api.models.common import InteractionId, require_identifier
from sshpilot.runtime_identity import new_interaction_id as new_interaction_id


def interaction_id_from_raw(value: Any) -> InteractionId:
    """Format or validate one interaction identifier."""
    val = require_identifier(str(value or "").strip(), "interaction id")
    return InteractionId(val)

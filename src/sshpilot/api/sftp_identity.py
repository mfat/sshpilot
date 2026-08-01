"""SFTP service identity helpers (non-UUID)."""

from __future__ import annotations

from typing import Any
from sshpilot.api.models.common import SftpServiceId, require_identifier
from sshpilot.runtime_identity import new_sftp_id as new_sftp_id


def sftp_id_from_raw(value: Any) -> SftpServiceId:
    """Format or validate one SFTP service identifier."""
    val = require_identifier(str(value or "").strip(), "SFTP service id")
    return SftpServiceId(val)

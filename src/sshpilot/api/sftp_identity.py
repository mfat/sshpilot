"""Strict daemon-lifetime SFTP service identity helpers."""

from __future__ import annotations

import uuid as uuid_module
from typing import Any

from .models.common import SftpServiceId


SFTP_ID_PREFIX = "sftp:"
MAX_SFTP_ID_LENGTH = len(SFTP_ID_PREFIX) + 36


def canonical_sftp_uuid(value: Any) -> str:
    """Return canonical UUID text for one non-nil SFTP service UUID."""

    if type(value) is not str:
        raise ValueError("SFTP UUID must be a string")
    text = value.strip()
    if not text or len(text) > 36:
        raise ValueError("SFTP UUID is invalid")
    try:
        parsed = uuid_module.UUID(text)
    except (AttributeError, ValueError):
        raise ValueError("SFTP UUID is invalid") from None
    if parsed.int == 0:
        raise ValueError("nil SFTP UUID is invalid")
    return str(parsed)


def new_sftp_uuid() -> str:
    """Generate one canonical random UUIDv4 string."""

    return str(uuid_module.uuid4())


def sftp_id_from_uuid(value: Any) -> SftpServiceId:
    """Format one opaque public SFTP service identifier."""

    return SftpServiceId(f"{SFTP_ID_PREFIX}{canonical_sftp_uuid(value)}")


def new_sftp_id() -> SftpServiceId:
    """Allocate one new daemon-lifetime SFTP service identifier."""

    return sftp_id_from_uuid(new_sftp_uuid())


def sftp_uuid_from_id(value: Any) -> str:
    """Parse one canonical public SFTP service identifier strictly."""

    if type(value) is not str:
        raise ValueError("SFTP service ID must be a string")
    if not value or len(value) > MAX_SFTP_ID_LENGTH:
        raise ValueError("SFTP service ID is invalid")
    if not value.startswith(SFTP_ID_PREFIX):
        raise ValueError("SFTP service ID prefix is invalid")
    suffix = value[len(SFTP_ID_PREFIX) :]
    if len(suffix) != 36:
        raise ValueError("SFTP service ID UUID is not in standard text form")
    return canonical_sftp_uuid(suffix)

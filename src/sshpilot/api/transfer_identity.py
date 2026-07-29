"""Strict daemon-lifetime transfer identity helpers."""

from __future__ import annotations

import uuid as uuid_module
from typing import Any

from .models.common import TransferId


TRANSFER_ID_PREFIX = "transfer:"
MAX_TRANSFER_ID_LENGTH = len(TRANSFER_ID_PREFIX) + 36


def canonical_transfer_uuid(value: Any) -> str:
    """Return canonical UUID text for one non-nil transfer UUID."""

    if type(value) is not str:
        raise ValueError("transfer UUID must be a string")
    text = value.strip()
    if not text or len(text) > 36:
        raise ValueError("transfer UUID is invalid")
    try:
        parsed = uuid_module.UUID(text)
    except (AttributeError, ValueError):
        raise ValueError("transfer UUID is invalid") from None
    if parsed.int == 0:
        raise ValueError("nil transfer UUID is invalid")
    return str(parsed)


def new_transfer_uuid() -> str:
    """Generate one canonical random UUIDv4 string."""

    return str(uuid_module.uuid4())


def transfer_id_from_uuid(value: Any) -> TransferId:
    """Format one opaque public transfer identifier."""

    return TransferId(f"{TRANSFER_ID_PREFIX}{canonical_transfer_uuid(value)}")


def new_transfer_id() -> TransferId:
    """Allocate one new daemon-lifetime transfer identifier."""

    return transfer_id_from_uuid(new_transfer_uuid())


def transfer_uuid_from_id(value: Any) -> str:
    """Parse one canonical public transfer identifier strictly."""

    if type(value) is not str:
        raise ValueError("transfer ID must be a string")
    if not value or len(value) > MAX_TRANSFER_ID_LENGTH:
        raise ValueError("transfer ID is invalid")
    if not value.startswith(TRANSFER_ID_PREFIX):
        raise ValueError("transfer ID prefix is invalid")
    suffix = value[len(TRANSFER_ID_PREFIX) :]
    if len(suffix) != 36:
        raise ValueError("transfer ID UUID is not in standard text form")
    return canonical_transfer_uuid(suffix)

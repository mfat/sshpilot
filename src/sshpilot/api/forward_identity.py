"""Strict daemon-lifetime port-forward identity helpers."""

from __future__ import annotations

import uuid as uuid_module
from typing import Any

from .models.common import ForwardId


FORWARD_ID_PREFIX = "forward:"
MAX_FORWARD_ID_LENGTH = len(FORWARD_ID_PREFIX) + 36


def canonical_forward_uuid(value: Any) -> str:
    """Return canonical UUID text for one non-nil forward UUID."""

    if type(value) is not str:
        raise ValueError("forward UUID must be a string")
    text = value.strip()
    if not text or len(text) > 36:
        raise ValueError("forward UUID is invalid")
    try:
        parsed = uuid_module.UUID(text)
    except (AttributeError, ValueError):
        raise ValueError("forward UUID is invalid") from None
    if parsed.int == 0:
        raise ValueError("nil forward UUID is invalid")
    return str(parsed)


def new_forward_uuid() -> str:
    """Generate one canonical random UUIDv4 string."""

    return str(uuid_module.uuid4())


def forward_id_from_uuid(value: Any) -> ForwardId:
    """Format one opaque public forward identifier."""

    return ForwardId(f"{FORWARD_ID_PREFIX}{canonical_forward_uuid(value)}")


def new_forward_id() -> ForwardId:
    """Allocate one new daemon-lifetime forward identifier."""

    return forward_id_from_uuid(new_forward_uuid())


def forward_uuid_from_id(value: Any) -> str:
    """Parse one canonical public forward identifier strictly."""

    if type(value) is not str:
        raise ValueError("forward ID must be a string")
    if not value or len(value) > MAX_FORWARD_ID_LENGTH:
        raise ValueError("forward ID is invalid")
    if not value.startswith(FORWARD_ID_PREFIX):
        raise ValueError("forward ID prefix is invalid")
    suffix = value[len(FORWARD_ID_PREFIX) :]
    if len(suffix) != 36:
        raise ValueError("forward ID UUID is not in standard text form")
    return canonical_forward_uuid(suffix)

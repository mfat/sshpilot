"""Strict daemon-lifetime session identity helpers."""

from __future__ import annotations

import uuid as uuid_module
from typing import Any


SESSION_ID_PREFIX = "session:"
MAX_SESSION_ID_LENGTH = len(SESSION_ID_PREFIX) + 36


def canonical_session_uuid(value: Any) -> str:
    """Return canonical UUID text for one non-nil session UUID."""

    if type(value) is not str:
        raise ValueError("session UUID must be a string")
    text = value.strip()
    if not text or len(text) > 36:
        raise ValueError("session UUID is invalid")
    try:
        parsed = uuid_module.UUID(text)
    except (AttributeError, ValueError):
        raise ValueError("session UUID is invalid") from None
    if parsed.int == 0:
        raise ValueError("nil session UUID is invalid")
    return str(parsed)


def new_session_uuid() -> str:
    """Generate one canonical random UUIDv4 string."""

    return str(uuid_module.uuid4())


def session_id_from_uuid(value: Any) -> str:
    """Format one opaque public session identifier."""

    return f"{SESSION_ID_PREFIX}{canonical_session_uuid(value)}"


def session_uuid_from_id(value: Any) -> str:
    """Parse one canonical public session identifier strictly."""

    if type(value) is not str:
        raise ValueError("session ID must be a string")
    if not value or len(value) > MAX_SESSION_ID_LENGTH:
        raise ValueError("session ID is invalid")
    if not value.startswith(SESSION_ID_PREFIX):
        raise ValueError("session ID prefix is invalid")
    suffix = value[len(SESSION_ID_PREFIX):]
    if len(suffix) != 36:
        raise ValueError("session ID UUID is not in standard text form")
    return canonical_session_uuid(suffix)

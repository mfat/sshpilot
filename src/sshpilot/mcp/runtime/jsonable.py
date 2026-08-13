"""Safe JSON conversion for runtime MCP tool output.

Mirrors the dataclass-walking shape of ``sshpilot.cli.output.to_jsonable`` but
lives in the MCP package so the runtime server needs no frontend dependency.
Unlike the CLI, the MCP boundary treats ``field(repr=False)`` as a daemon
declared "not for unstructured output" marker and redacts those values before
they reach model context; content-bearing reads require an explicit opt-in
(see :class:`RuntimePolicy`).
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import date, datetime, time
from enum import Enum
from typing import Any, Mapping

#: Placeholder used where a daemon-declared sensitive DTO field was withheld.
REDACTED = "<redacted>"


def to_jsonable(value: Any, *, redact_sensitive: bool = True) -> Any:
    """Convert public API values to stable JSON-compatible values.

    When ``redact_sensitive`` is true (the default), dataclass fields that the
    API declared with ``field(repr=False)`` are emitted as :data:`REDACTED`
    instead of their real value. The call site flips this off only when the
    operator explicitly opted into content reads.
    """
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: (
                REDACTED
                if redact_sensitive and item.repr is False
                else to_jsonable(getattr(value, item.name), redact_sensitive=redact_sensitive)
            )
            for item in fields(value)
        }
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): to_jsonable(item, redact_sensitive=redact_sensitive)
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [to_jsonable(item, redact_sensitive=redact_sensitive) for item in value]
    if isinstance(value, (set, frozenset)):
        converted = [
            to_jsonable(item, redact_sensitive=redact_sensitive) for item in value
        ]
        return sorted(converted, key=lambda item: str(item))
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return value
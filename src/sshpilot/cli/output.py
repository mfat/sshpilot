"""Safe presentation helpers for the reference CLI."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import date, datetime, time
from enum import Enum
from typing import Any, Mapping


def to_jsonable(value: Any) -> Any:
    """Convert public API values to stable JSON-compatible values.

    This deliberately walks dataclass fields instead of serializing ``__dict__``;
    the CLI therefore cannot accidentally expose implementation state.
    """

    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: to_jsonable(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        converted = [to_jsonable(item) for item in value]
        return sorted(converted, key=lambda item: str(item))
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return value


def write_json(stream, value: Any) -> None:
    import json

    json.dump(to_jsonable(value), stream, indent=2, sort_keys=True)
    stream.write("\n")


def write_human(stream, value: Any, *, title: str | None = None) -> None:
    """Render a compact, deterministic view of a public DTO."""

    if title:
        stream.write(f"{title}\n")
    converted = to_jsonable(value)
    if isinstance(converted, list):
        for item in converted:
            write_human(stream, item)
        return
    if isinstance(converted, dict):
        for key, item in converted.items():
            if isinstance(item, (dict, list)) and item:
                stream.write(f"{key}:\n")
                write_human(stream, item)
            else:
                stream.write(f"{key}: {item}\n")
        return
    stream.write(f"{converted}\n")


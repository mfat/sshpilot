"""Small validation helpers for frontend-safe public values."""

from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional, Set


_SENSITIVE_DETAIL_KEY_PARTS = frozenset(
    {
        "argv",
        "authorization",
        "cookie",
        "credential",
        "env",
        "environment",
        "passphrase",
        "password",
        "private_key",
        "secret",
        "token",
    }
)


def _safe_key(key: Any, path: str) -> str:
    if type(key) is not str or not key:
        raise TypeError(f"{path} keys must be non-empty strings")
    normalized = key.lower().replace("-", "_")
    parts = set(normalized.split("_"))
    if parts & _SENSITIVE_DETAIL_KEY_PARTS:
        raise ValueError(f"{path} contains a disallowed sensitive detail key")
    return key


def _copy_safe_detail_value(value: Any, path: str, active: Set[int]) -> Any:
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, list):
        identity = id(value)
        if identity in active:
            raise ValueError(f"{path} contains a cyclic list")
        active.add(identity)
        try:
            return [
                _copy_safe_detail_value(item, f"{path}[]", active)
                for item in value
            ]
        finally:
            active.remove(identity)
    if isinstance(value, dict):
        identity = id(value)
        if identity in active:
            raise ValueError(f"{path} contains a cyclic dictionary")
        active.add(identity)
        try:
            return {
                _safe_key(key, path): _copy_safe_detail_value(
                    item,
                    f"{path}.{key}",
                    active,
                )
                for key, item in value.items()
            }
        finally:
            active.remove(identity)
    raise TypeError(
        f"{path} contains unsupported public value type "
        f"{type(value).__name__}"
    )


def copy_safe_details(
    details: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Validate and detach a structured public error/event details mapping.

    Only JSON-safe scalar, list, and dictionary values are accepted. Keys that
    conventionally carry credentials, environments, or process command lines
    are rejected so callers cannot accidentally expose those structures.
    """

    if details is None:
        return {}
    if not isinstance(details, dict):
        raise TypeError("public details must be a dictionary")
    return {
        _safe_key(key, "details"): _copy_safe_detail_value(
            value,
            f"details.{key}",
            set(),
        )
        for key, value in details.items()
    }
